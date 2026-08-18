"""黄金问答集的加载与**严格校验**。

校验刻意严格：未知字段、非法 kind、与 kind 不相容的期望字段一律抛错而不是忽略。
理由与抽取器一致——一条写错的用例会让评测给出看起来正常的错数字，
那比直接报错危险得多。

期望值的两种 slug 写法见 `matches_slug`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

GOLDEN_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = GOLDEN_DIR / "questions.yaml"
RECORDINGS_DIR = GOLDEN_DIR / "recordings"

#: 六类样本。`tasks.md` 2.7 要求前四类 + 拒答类；`tool_route` 是本项目自加的
#: 附加观测（ALLOWED_TOOLS 的顺序即推荐优先级，值得量化但不设门槛）。
KINDS = ("name", "description", "numeric", "cross_source", "gap", "tool_route")

KIND_ZH = {
    "name": "专名查询",
    "description": "描述性查询",
    "numeric": "数值类",
    "cross_source": "跨来源",
    "gap": "确定不覆盖（必须拒答）",
    "tool_route": "工具路由（附加观测）",
}

_ALLOWED_KEYS = {
    "id", "query", "kind", "expect_slug", "expect_any_hit", "expect_gap",
    "expect_evidence_text", "expect_values", "expect_in_answer", "forbid_in_answer",
    "expect_tool", "note", "lesson",
}


class GoldenSetError(ValueError):
    """黄金问答集本身写错了。修用例，不要放宽校验。"""


@dataclass(frozen=True)
class Case:
    id: str
    query: str
    kind: str
    note: str = ""
    lesson: str = ""
    expect_slug: str = ""
    expect_any_hit: bool = False
    expect_gap: bool = False
    expect_evidence_text: str = ""
    expect_values: tuple[str, ...] = field(default_factory=tuple)
    expect_in_answer: tuple[str, ...] = field(default_factory=tuple)
    forbid_in_answer: tuple[str, ...] = field(default_factory=tuple)
    expect_tool: str = ""

    @property
    def expects_sufficient(self) -> bool:
        """这条用例是否**应当**判为依据充分。

        `expect_gap` 的反面不是「有命中」而是「应当有依据」——两者分列统计：
        正类判成不充分是错误拒答，负类判成充分是无据漏出的上游。
        """
        return not self.expect_gap

    @property
    def has_retrieval_target(self) -> bool:
        """离线检索侧能不能给这条用例算出 Recall。"""
        return bool(self.expect_slug)

    @property
    def retrieval_scorable(self) -> bool:
        """检索侧能不能给这条用例判定。

        只有答案级期望（`expect_in_answer`）而无检索目标的用例**不参与**检索侧判定：
        见 `cross_pob_priority` 的 lesson —— 来源优先级写在 config 与提示词里，
        知识库里没有对应文档，用检索去判它只会得到一个假的「错误拒答」。
        """
        if self.kind == "tool_route":
            return False
        return bool(
            self.expect_gap or self.expect_slug or self.expect_any_hit
            or self.expect_evidence_text or self.expect_values
        )


def _as_tuple(value: Any, *, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise GoldenSetError(f"{where} 必须是列表而不是字符串：{value!r}")
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise GoldenSetError(f"{where} 必须是字符串列表，实际为 {value!r}")
    return tuple(value)


def load_cases(path: Path | None = None) -> list[Case]:
    """读 questions.yaml。任何结构问题都抛 `GoldenSetError`。"""
    p = path or QUESTIONS_PATH
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or "cases" not in raw:
        raise GoldenSetError(f"{p} 顶层必须是映射且含 cases 键")
    items = raw["cases"]
    if not isinstance(items, list) or not items:
        raise GoldenSetError(f"{p} 的 cases 必须是非空列表")

    cases: list[Case] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        where = f"{p.name} cases[{i}]"
        if not isinstance(item, dict):
            raise GoldenSetError(f"{where} 必须是映射")
        unknown = set(item) - _ALLOWED_KEYS
        if unknown:
            raise GoldenSetError(f"{where} 出现未知字段：{sorted(unknown)}")
        cid = str(item.get("id") or "").strip()
        query = str(item.get("query") or "").strip()
        kind = str(item.get("kind") or "").strip()
        if not cid:
            raise GoldenSetError(f"{where} 缺 id")
        if cid in seen:
            raise GoldenSetError(f"{where} id 重复：{cid}（录制与审计结论按 id 索引，不得重复）")
        seen.add(cid)
        if not query:
            raise GoldenSetError(f"{where}（{cid}）缺 query")
        if kind not in KINDS:
            raise GoldenSetError(f"{where}（{cid}）kind={kind!r} 不在 {KINDS}")

        case = Case(
            id=cid,
            query=query,
            kind=kind,
            note=str(item.get("note") or ""),
            lesson=str(item.get("lesson") or ""),
            expect_slug=str(item.get("expect_slug") or ""),
            expect_any_hit=bool(item.get("expect_any_hit", False)),
            expect_gap=bool(item.get("expect_gap", False)),
            expect_evidence_text=str(item.get("expect_evidence_text") or ""),
            expect_values=_as_tuple(item.get("expect_values"), where=f"{where} expect_values"),
            expect_in_answer=_as_tuple(
                item.get("expect_in_answer"), where=f"{where} expect_in_answer"
            ),
            forbid_in_answer=_as_tuple(
                item.get("forbid_in_answer"), where=f"{where} forbid_in_answer"
            ),
            expect_tool=str(item.get("expect_tool") or ""),
        )
        _check_coherence(case, where)
        cases.append(case)
    return cases


def _check_coherence(case: Case, where: str) -> None:
    """期望字段与 kind 的相容性。拒答类和有据类的期望不能混在一条里。"""
    if case.expect_gap:
        for attr in ("expect_slug", "expect_any_hit", "expect_evidence_text", "expect_values"):
            if getattr(case, attr):
                raise GoldenSetError(
                    f"{where}（{case.id}）expect_gap 与 {attr} 互斥："
                    "既然应当拒答，就不该同时期望命中或期望数值"
                )
    elif case.kind != "tool_route" and not (
        case.expect_slug or case.expect_any_hit or case.expect_values
        or case.expect_in_answer
    ):
        raise GoldenSetError(
            f"{where}（{case.id}）非拒答用例必须至少给一项期望："
            "expect_slug / expect_any_hit / expect_values / expect_in_answer"
        )
    if case.kind == "tool_route" and not case.expect_tool:
        raise GoldenSetError(f"{where}（{case.id}）tool_route 用例必须给 expect_tool")
    if case.expect_tool and case.kind != "tool_route":
        raise GoldenSetError(f"{where}（{case.id}）expect_tool 只用于 tool_route 类")


def matches_slug(entity_id: str | None, expect: str) -> bool:
    """期望值的两种写法：

    ``slug``            只比 entity_id 末段。唯一装备等 id 稳定的类型沿用这种写法。
    ``类型:slug*``      比完整 entity_id 前缀，并顺带断言实体类型。天赋类实体的 id
                        末尾带内容哈希（同名节点按内容分成多个实体），写死哈希会在
                        数据更新时变成假失败；而断言到类型这一层是有意义的——
                        实测 29 个天赋名与唯一装备重名。
    """
    full = entity_id or ""
    if not full:
        return False
    if ":" in expect:
        return full.startswith(expect.rstrip("*"))
    return full.split(":")[-1] == expect


def by_kind(cases: list[Case]) -> dict[str, list[Case]]:
    out: dict[str, list[Case]] = {k: [] for k in KINDS}
    for c in cases:
        out[c.kind].append(c)
    return out
