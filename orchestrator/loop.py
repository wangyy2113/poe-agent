"""Orchestrator 主循环（任务 3.3）。

单轮流程：

    强制检索 → 依据是否充分？
      不充分 → 由代码生成缺据声明，**不进入生成**
      充分   → 编号证据 → LLM（可继续调工具）→ 出口绑定校验 → 装配引用

两个「由代码而非模型决定」的地方是这套设计的要点：
  1. 依据充分性判定在检索侧，不充分就根本不给模型机会编答案（需求 1.3、3.10）。
  2. 出口绑定校验在生成之后，模型不听话就拦截（需求 1.7、15.2）。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from agents.knowledge import ALLOWED_TOOLS, EVIDENCE_TEMPLATE, SYSTEM_PROMPT, render_gap
from core import db as dbmod
from core.config import get_config
from core.llm import LLMClient, LLMError, TurnBudget, get_llm_client
from orchestrator import citation as cit
from orchestrator.binding import (
    BindingResult,
    Evidence,
    number_evidence,
    render_evidence_block,
    verify,
)
from tools.registry import REGISTRY, ToolResult

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """一轮问答的完整结果。`answer` 是**校验后**可安全下发的文本。"""

    trace_id: str
    question: str
    answer: str
    #: 依据不足而未进入生成
    evidence_gap: bool = False
    citations: list[cit.RenderedCitation] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    binding: BindingResult | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    retrieval_meta: dict[str, Any] = field(default_factory=dict)
    #: 实际使用的模型。故障转移会切模型，不记下来排查时会误判推理 token 行为。
    model: str = ""
    tokens: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0
    degraded_note: str = ""

    @property
    def blocked(self) -> int:
        return self.binding.blocked if self.binding else 0

    def full_text(self) -> str:
        """答案 + 引用块。给最终用户看的完整内容。"""
        return self.answer + cit.render_block(self.citations)


def _domain_terms() -> tuple[str, ...]:
    """事实句判定用的领域词。来自 `retrieval/vocab.py` 生成的词表。

    读不到就返回空——退化为「判不出类型即按事实句处理」，仍然是安全侧。
    """
    try:
        from retrieval.vocab import load_vocab

        # 只取长度 >= 2 的词：单字词（「弓」「爪」）在自然语句里到处都是，
        # 用它判事实句会把「下面分两点说明」也判成事实句，制造错误拒答。
        return tuple(t for t in load_vocab() if len(t) >= 2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("领域词表不可用（%s），退化为「判不出即按事实句处理」", exc)
        return ()


def run_turn(
    question: str,
    *,
    conversation_id: str | None = None,
    client: LLMClient | None = None,
) -> TurnResult:
    started = time.monotonic()
    cfg = get_config()
    client = client or get_llm_client()
    trace_id = uuid.uuid4().hex
    result = TurnResult(trace_id=trace_id, question=question, answer="")

    # ── 第一步：强制检索。不让模型决定「要不要查」 ──
    first = REGISTRY.call("kb_search", json.dumps({"query": question}, ensure_ascii=False))
    result.tool_calls.append(_tool_record("kb_search", {"query": question}, first))
    result.retrieval_meta = dict(first.meta)

    if not first.ok:
        result.answer = (
            f"检索不可用（{first.error}），因此无法确认依据。"
            "本助手在无依据时不作答。"
        )
        result.evidence_gap = True
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        _persist(result, conversation_id)
        return result

    data = first.data or {}
    if not data.get("sufficient"):
        # 依据不足 → 由代码生成缺据声明，模型完全不参与
        result.evidence_gap = True
        result.answer = render_gap(
            question,
            list(data.get("missing_terms") or []),
            str(data.get("reason") or "检索未命中足够依据"),
        )
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        _record_gaps(question, [str(data.get("reason") or "依据不足")])
        _persist(result, conversation_id)
        return result

    # ── 第二步：编号证据并生成 ──
    evidence = number_evidence(first.citations)
    result.evidence = evidence
    if first.meta.get("degraded"):
        result.degraded_note = (
            f"检索以降级通道完成（{', '.join(first.meta.get('channels') or [])}）："
            f"{first.meta.get('note') or '向量与重排未启用'}"
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": EVIDENCE_TEMPLATE.format(
                evidence=render_evidence_block(evidence), question=question
            ),
        },
    ]

    budget = TurnBudget(limit=int(cfg.get("llm.max_tokens_per_turn", 60000)))
    max_calls = int(cfg.get("llm.max_tool_calls", 12))
    tools = REGISTRY.schemas(ALLOWED_TOOLS)
    answer_text = ""

    try:
        for _ in range(max_calls + 1):
            resp = client.chat(
                messages,
                tools=tools,
                budget=budget,
                allow_empty_content=True,  # 纯 tool_call 回复是合法的
            )
            result.model = resp.model
            if not resp.tool_calls:
                answer_text = resp.content
                break

            messages.append({
                "role": "assistant",
                "content": resp.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in resp.tool_calls
                ],
            })
            for tc in resp.tool_calls:
                tr = REGISTRY.call(tc.name, tc.arguments)
                result.tool_calls.append(
                    _tool_record(tc.name, _safe_json(tc.arguments), tr)
                )
                # 追加检索到的新证据并重新编号，让模型后续能引用它们
                if tr.citations:
                    evidence = _merge_evidence(evidence, tr.citations)
                    result.evidence = evidence
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tr.for_model(),
                })
            # 证据集变了，把最新编号重新给模型，否则它会引用旧编号
            messages.append({
                "role": "user",
                "content": "当前可用证据（编号已更新，请据此标注）：\n\n"
                           + render_evidence_block(evidence),
            })
        else:
            logger.warning("工具调用达到上限 %d，用现有内容作答", max_calls)
    except LLMError as exc:
        # 必须记下具体原因：只报异常类型的话，线上出问题时无从下手。
        # 消息已由 core/llm.py 脱敏，可安全写日志。
        logger.error("生成失败（trace=%s）：%s", trace_id, exc)
        result.answer = f"生成失败（{type(exc).__name__}），本轮不作答以免输出无据内容。"
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        _persist(result, conversation_id)
        return result

    result.tokens = {
        "prompt": budget.used.prompt_tokens,
        "output": budget.used.completion_tokens,
        "reasoning": budget.used.reasoning_tokens,
        "total": budget.used.total_tokens,
    }

    # ── 第三步：出口闸门 ──
    binding = verify(
        answer_text,
        evidence,
        factual_terms=list(cfg.get("retrieval.factual_terms", []) or []),
        domain_terms=_domain_terms(),
    )
    result.binding = binding
    result.answer = binding.text
    result.citations = cit.collect(evidence, binding.used_labels)
    if result.degraded_note:
        result.answer += f"\n\n> {result.degraded_note}"

    if binding.blocked:
        logger.warning(
            "绑定校验拦截 %d 句（trace=%s）：%s",
            binding.blocked, trace_id,
            "; ".join(v.reason for v in binding.verdicts if not v.ok)[:300],
        )
        _record_gaps(question, binding.gaps)

    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    _persist(result, conversation_id)
    return result


# ── 辅助 ───────────────────────────────────────────────────

def _merge_evidence(
    existing: list[Evidence], new_citations: list[dict[str, Any]]
) -> list[Evidence]:
    """把新检索到的证据并入已有编号序列，已有的 chunk 不重新编号。

    重新编号会让模型之前标注的 `[[E1]]` 指向别的东西——出口校验会因此
    误判为编造。编号一旦分配就不能变。
    """
    seen = {e.chunk_id for e in existing}
    merged = list(existing)
    for c in new_citations:
        cid = str(c.get("chunk_id") or "")
        if cid and cid in seen:
            continue
        seen.add(cid)
        extra = number_evidence([c])
        if not extra:
            continue
        e = extra[0]
        merged.append(
            Evidence(
                label=f"E{len(merged) + 1}",
                chunk_id=e.chunk_id,
                entity_id=e.entity_id,
                title=e.title,
                url=e.url,
                source=e.source,
                source_priority=e.source_priority,
                text=e.text,
                can_support_facts=e.can_support_facts,
            )
        )
    return merged


def _tool_record(name: str, args: Any, result: ToolResult) -> dict[str, Any]:
    return {
        "tool": name,
        "arguments": args,
        "ok": result.ok,
        "error": result.error,
        "elapsed_ms": result.elapsed_ms,
        "evidence_count": len(result.citations),
        "data": result.data,
        "meta": result.meta,
    }


def _safe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


def _record_gaps(question: str, gaps: list[str]) -> None:
    """写缺口台账。按频次排序即为采集优先级（需求 2.12）。"""
    if not gaps:
        return
    try:
        conn = dbmod.get_conn()
        for point in gaps:
            conn.execute(
                """
                INSERT INTO knowledge_gap(question, missing_point, hit_count,
                                          first_seen, last_seen)
                VALUES (?,?,1,datetime('now'),datetime('now'))
                ON CONFLICT DO NOTHING
                """,
                (question, point[:500]),
            )
            conn.execute(
                "UPDATE knowledge_gap SET hit_count = hit_count + 1, "
                "last_seen = datetime('now') WHERE question = ? AND missing_point = ?",
                (question, point[:500]),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001  台账失败不该让整轮失败
        logger.warning("写缺口台账失败：%s", exc)


def _persist(result: TurnResult, conversation_id: str | None) -> None:
    """轨迹落库（需求 4.5）。含绑定校验结果与拦截原因，供 /admin 追溯。"""
    try:
        conn = dbmod.get_conn()
        conn.execute(
            """
            INSERT INTO trace(trace_id, conversation_id, intent, plan, tool_calls,
                              retrieval_hits, binding_result, tokens_prompt,
                              tokens_output, tokens_reasoning, elapsed_ms, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            """,
            (
                result.trace_id,
                conversation_id,
                "knowledge_qa",
                "强制检索 → 充分性判定 → 生成 → 绑定校验",
                json.dumps(result.tool_calls, ensure_ascii=False, default=str),
                json.dumps(
                    {
                        "evidence": [
                            {"label": e.label, "chunk_id": e.chunk_id,
                             "source": e.source, "priority": e.source_priority,
                             "title": e.title}
                            for e in result.evidence
                        ],
                        "meta": result.retrieval_meta,
                        "evidence_gap": result.evidence_gap,
                    },
                    ensure_ascii=False, default=str,
                ),
                json.dumps(
                    result.binding.summary() if result.binding else {},
                    ensure_ascii=False, default=str,
                ),
                result.tokens.get("prompt", 0),
                result.tokens.get("output", 0),
                result.tokens.get("reasoning", 0),
                result.elapsed_ms,
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("轨迹落库失败：%s", exc)
