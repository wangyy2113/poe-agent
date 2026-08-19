"""真实响应的录制与回放。

design.md 14 节定的集成测试策略是「LLM 与外部源以录制响应打桩，保证可离线跑」。
所以评测**不长期联网**：录一次真实响应，之后离线重放，出口闸门的指标就可回归。

## 录的是未过闸门的原文

回放要量的是「闸门拦得对不对」，因此必须存 `TurnResult.raw_answer`——模型在被拦
之前写下的东西。这也意味着 `recordings/*.json` 里含**可能无据的断言**：它们是
待检材料，不是答案，任何情况下都不要当成事实引用。推理内容 (`reasoning_content`)
一律不录——`core/llm.py` 在类型层面就不让它流出，这里也不给它留位置。

## 为什么要存证据指纹

回放用**录制里的证据**，这样结果确定可复现。但知识库会变：一旦某条证据的文本被
重新抽取，录制里的 `[[E2]]` 就可能指向了别的内容，此时算出来的指标是错的。
所以录制里存证据指纹，回放时重跑一次检索比对，不一致就报「录制过期需重录」
并以非零退出——**绝不静默给出错数字**。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.binding import Evidence

RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"

#: 录制格式版本。结构变更时 +1，回放遇到旧版本直接要求重录而不是猜字段。
FORMAT_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def evidence_fingerprint(evidence: list[Evidence]) -> str:
    """证据集指纹：编号 → chunk_id → 正文哈希。

    含正文哈希而不只是 chunk_id：同一个 chunk 被重新抽取后 id 可能不变而内容变了，
    那时录制同样已经失效。
    """
    h = hashlib.sha256()
    for e in evidence:
        h.update(e.label.encode())
        h.update(b"\x00")
        h.update(e.chunk_id.encode())
        h.update(b"\x00")
        h.update(hashlib.sha256(e.text.encode()).digest())
        h.update(b"\x1e")
    return f"sha256:{h.hexdigest()[:32]}"


def _evidence_to_dict(e: Evidence) -> dict[str, Any]:
    return {
        "label": e.label,
        "chunk_id": e.chunk_id,
        "entity_id": e.entity_id,
        "title": e.title,
        "url": e.url,
        "source": e.source,
        "source_priority": e.source_priority,
        "text": e.text,
        "can_support_facts": e.can_support_facts,
        "claim_scope": e.claim_scope,
        "claim_data": e.claim_data,
    }


def _evidence_from_dict(d: dict[str, Any]) -> Evidence:
    return Evidence(
        label=str(d["label"]),
        chunk_id=str(d["chunk_id"]),
        entity_id=d.get("entity_id"),
        title=str(d.get("title") or ""),
        url=str(d.get("url") or ""),
        source=str(d.get("source") or ""),
        source_priority=int(d.get("source_priority") or 99),
        text=str(d.get("text") or ""),
        can_support_facts=bool(d.get("can_support_facts", True)),
        claim_scope=str(d.get("claim_scope") or "general"),
        claim_data=dict(d.get("claim_data") or {}),
    )


class StaleRecording(RuntimeError):
    """录制与当前知识库不一致。必须重录，不能拿它算指标。"""


@dataclass
class Recording:
    case_id: str
    query: str
    #: 模型未过闸门的原始正文。**待检材料，不是答案。**
    raw_answer: str
    evidence: list[Evidence] = field(default_factory=list)
    evidence_fingerprint: str = ""
    #: 缺据路径（不经模型）也录：它同样要回归，且 tokens 必须为空
    evidence_gap: bool = False
    #: 是否走的是 **POE1/POE2 越界**声明（`orchestrator.loop.poe2_marker` 命中）。
    #: 与缺据声明分开记：两者的契约不同——越界**不能**说「查不到」，
    #: 那会让用户以为游戏里没这东西（`agents/knowledge.py` 与 CLAUDE.md 都写着这条）。
    out_of_scope: bool = False
    #: 精确工具路由（`orchestrator.loop._precision_tool_route`）。非空时这一轮的
    #: 不作答理由由**工具**给出（「来源该格为空」「没形成确定调用」），
    #: 契约与另两条路径又不同，见 `scripts/eval_run.replay_case`。
    precision_route: str = ""
    gap_answer: str = ""
    model: str = ""
    tokens: dict[str, int] = field(default_factory=dict)
    tool_calls: list[str] = field(default_factory=list)
    blocked_claims: int = 0
    elapsed_ms: int = 0
    recorded_at: str = ""
    format_version: int = FORMAT_VERSION

    # ── 序列化 ──

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "case_id": self.case_id,
            "query": self.query,
            "recorded_at": self.recorded_at,
            "model": self.model,
            "tokens": self.tokens,
            "tool_calls": self.tool_calls,
            "evidence_gap": self.evidence_gap,
            "out_of_scope": self.out_of_scope,
            "precision_route": self.precision_route,
            "gap_answer": self.gap_answer,
            "blocked_claims": self.blocked_claims,
            "elapsed_ms": self.elapsed_ms,
            "evidence_fingerprint": self.evidence_fingerprint,
            "_warning": "raw_answer 是未过出口闸门的模型原文，含可能无据的断言，不得当作事实引用",
            "raw_answer": self.raw_answer,
            "evidence": [_evidence_to_dict(e) for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Recording:
        version = int(d.get("format_version") or 0)
        if version != FORMAT_VERSION:
            raise StaleRecording(
                f"录制格式版本 {version} 与当前 {FORMAT_VERSION} 不一致，请重录"
            )
        return cls(
            case_id=str(d["case_id"]),
            query=str(d["query"]),
            raw_answer=str(d.get("raw_answer") or ""),
            evidence=[_evidence_from_dict(x) for x in (d.get("evidence") or [])],
            evidence_fingerprint=str(d.get("evidence_fingerprint") or ""),
            evidence_gap=bool(d.get("evidence_gap")),
            out_of_scope=bool(d.get("out_of_scope")),
            precision_route=str(d.get("precision_route") or ""),
            gap_answer=str(d.get("gap_answer") or ""),
            model=str(d.get("model") or ""),
            tokens=dict(d.get("tokens") or {}),
            tool_calls=list(d.get("tool_calls") or []),
            blocked_claims=int(d.get("blocked_claims") or 0),
            elapsed_ms=int(d.get("elapsed_ms") or 0),
            recorded_at=str(d.get("recorded_at") or ""),
            format_version=version,
        )

    # ── 落盘 ──

    def path(self, base: Path | None = None) -> Path:
        return (base or RECORDINGS_DIR) / f"{self.case_id}.json"

    def save(self, base: Path | None = None) -> Path:
        target = self.path(base)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    def check_fresh(self, conn: Any) -> tuple[int, int]:
        """逐条比对录制里的证据与库里**当前的 chunk 正文**，不一致即抛 `StaleRecording`。

        返回 `(已核对, 无法核对)`。

        为什么按 chunk_id 逐条比而不是重跑一次检索比整体指纹：一轮问答的证据集
        不只来自那次强制检索——模型还会调 `mod_tiers`、`stat_sources` 等工具追加证据。
        拿一次裸检索的结果去比整体指纹，只要这一轮调过工具就必然对不上，
        于是**所有**这类录制都被误报为过期（实测 3 条）。而真正要防的是
        「证据正文被重新抽取后变了内容，录制里的 `[[E2]]` 指向了别的东西」，
        那按 chunk_id 比正文即可精确回答。

        工具合成的证据（chunk 表里查不到那个 id）计入「无法核对」而不是过期——
        它们由工具当场渲染，本来就不存在于索引里。
        """
        checked = unverifiable = 0
        for e in self.evidence:
            row = conn.execute(
                "SELECT text FROM chunk WHERE chunk_id = ?", (e.chunk_id,)
            ).fetchone()
            if row is None:
                unverifiable += 1
                continue
            checked += 1
            current = str(row[0])
            # 判据是**包含**而不是相等：证据的 text 是切片的快照，工具会按上限截断
            # （实测 1200 字，chunk 本体 1729 字）。要相等就会把每条被截断的证据
            # 都报成过期。而内容真被重新抽取过时，包含关系同样不成立，仍然抓得到。
            if e.text not in current and current not in e.text:
                raise StaleRecording(
                    f"{self.case_id} 的证据 {e.label}（chunk {e.chunk_id}）正文已变，"
                    "录制里的绑定可能已指向别的内容，请重录"
                )
        return checked, unverifiable


def load(case_id: str, base: Path | None = None) -> Recording | None:
    p = (base or RECORDINGS_DIR) / f"{case_id}.json"
    if not p.is_file():
        return None
    return Recording.from_dict(json.loads(p.read_text(encoding="utf-8")))


def load_all(base: Path | None = None) -> dict[str, Recording]:
    out: dict[str, Recording] = {}
    d = base or RECORDINGS_DIR
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        rec = Recording.from_dict(json.loads(p.read_text(encoding="utf-8")))
        out[rec.case_id] = rec
    return out
