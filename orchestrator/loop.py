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
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from agents.knowledge import (
    ALLOWED_TOOLS,
    EVIDENCE_TEMPLATE,
    build_system_prompt,
    render_gap,
)
from core import db as dbmod
from core.config import get_config
from core.llm import LLMClient, LLMError, TurnBudget, get_llm_client
from orchestrator import citation as cit
from orchestrator.binding import (
    BindingResult,
    Evidence,
    StreamingGate,
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


def _prepare_tools() -> list[dict[str, Any]]:
    """导出工具 schema，并先把 `kb_search` 的 category 枚举同步为实际有数据的分类。

    每轮同步一次而不是启动时一次：采集随时可能跑，启动时的快照会过期，
    而过期的枚举会让模型拿一个空分类去过滤，得到 0 条证据。
    代价是一次 24 行的 SELECT。
    """
    try:
        from tools.impl.kb_search import sync_category_enum  # noqa: PLC0415

        sync_category_enum()
    except Exception as exc:  # noqa: BLE001
        logger.warning("同步工具 schema 失败，用现有定义继续：%s", exc)
    return REGISTRY.schemas(ALLOWED_TOOLS)


def _gap_answer(question: str, data: dict[str, Any]) -> tuple[str, str]:
    """依据不足时生成缺据声明，并给出台账分类。

    返回 (答案文本, 台账 guessed_category)。归因失败不影响主流程——
    退化成没有归因的缺据声明，仍然是安全侧。
    """
    reason = str(data.get("reason") or "检索未命中足够依据")
    missing_terms = list(data.get("missing_terms") or [])
    attribution = ""
    urls: list[str] = []
    category = "unknown"
    try:
        from kb.coverage import attribute_gap  # noqa: PLC0415  延迟导入避免循环

        attr = attribute_gap(dbmod.get_conn(), question)
        attribution = attr.explain()
        urls = [d.spec.authority_url for d in attr.missing_domains]
        category = attr.category_label()
    except Exception as exc:  # noqa: BLE001
        logger.warning("缺据归因失败，退化为无归因声明：%s", exc)
    return (
        render_gap(question, missing_terms, reason,
                   attribution=attribution, authority_urls=urls),
        category,
    )


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


_CRAFT_COMBINATION_INTENTS = (
    "一起出现", "同时出现", "同时存在", "同时有", "共存", "冲突", "互斥",
    "词缀组合是否可能", "词缀组合能否", "这些词缀能否",
)
_CRAFT_ROUTE_NEGATIONS = (
    "不要", "请勿", "不用", "无需", "不需要", "不必", "别",
    "不是问", "并非询问",
)
_CRAFT_ROUTE_META_QUESTIONS = (
    "什么意思", "含义是什么", "概念是什么",
    "为什么要检查", "为什么要判断", "为什么需要检查", "为什么需要判断",
    "为何要检查", "为何要判断",
)
_CRAFT_POSITIVE_TURNS = (
    "我是问", "而是问", "而是判断", "真正想问", "实际想问",
    "但请判断", "但判断", "再判断", "然后判断", "接着判断",
)
_CRAFT_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;：:！？?!\n]+")
_CRAFT_TARGET_SPLIT_RE = re.compile(r"(?:以及|并且|和|与|及|跟|同|、|\+)")


def _normalized(text: str) -> str:
    return "".join((text or "").lower().split())


def _positive_craft_clause(question: str) -> str:
    """返回最后一个未被否定的组合判断子句；没有则返回空串。"""
    positive: list[str] = []
    for raw in _CRAFT_CLAUSE_SPLIT_RE.split(question or ""):
        clause = _normalized(raw)
        if not clause:
            continue
        # 同一标点子句里先否定、后转问时，只看最后一个明确的正向转折。
        turn_at = max((clause.rfind(turn) for turn in _CRAFT_POSITIVE_TURNS), default=-1)
        if turn_at > 0:
            clause = clause[turn_at:]
        if not any(intent in clause for intent in _CRAFT_COMBINATION_INTENTS):
            continue
        if any(marker in clause for marker in _CRAFT_ROUTE_NEGATIONS):
            continue
        if any(marker in clause for marker in _CRAFT_ROUTE_META_QUESTIONS):
            continue
        positive.append(clause)
    return positive[-1] if positive else ""


_BENCH_TERMS = ("工艺工作台", "工艺台", "改造台")
_BENCH_EXISTS_MARKERS = ("有没有", "是否有", "有无")
_BENCH_FACT_MARKERS = (
    "原始费用", "费用", "花费", "解锁地点", "解锁说明", "如何解锁", "怎么解锁",
    "适用类别", "适用itemclasses", "适用于哪些",
)
_BENCH_QUERY_PREFIXES = ("页面里的", "页面中的", "页面的", "里的", "中的", "的", "配方", "选项")
_BENCH_QUERY_SUFFIXES = ("这个选项", "这项", "吗", "么")


def _strip_bench_query_scaffolding(value: str) -> str:
    changed = True
    while changed and value:
        changed = False
        for token in _BENCH_QUERY_PREFIXES:
            if value.startswith(token):
                value = value[len(token):]
                changed = True
                break
        for token in _BENCH_QUERY_SUFFIXES:
            if value.endswith(token):
                value = value[:-len(token)]
                changed = True
                break
    return value


def _known_bench_item_classes() -> tuple[str, ...]:
    """从已入库目录读取真实 ItemClasses；不可用时返回空，绝不手写猜测。"""
    try:
        rows = dbmod.get_conn().execute(
            "SELECT DISTINCT j.value "
            "FROM craft_bench_recipe AS r, json_each(r.item_classes) AS j "
            "WHERE typeof(j.value)='text' AND j.value<>''"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("工艺台 ItemClasses 不可用，不拆分目录参数：%s", exc)
        return ()
    return tuple(sorted({str(row[0]).strip() for row in rows if row[0]}, key=len, reverse=True))


def _split_bench_item_class(raw_query: str) -> tuple[str, str]:
    """仅按目录中真实存在的 ItemClass 前缀拆分“类别的效果”。"""
    for item_class in _known_bench_item_classes():
        prefix = f"{_normalized(item_class)}的"
        if raw_query.startswith(prefix) and len(raw_query) > len(prefix):
            return raw_query[len(prefix):], _normalized(item_class)
    return raw_query, ""


def _bench_precision_query(question: str) -> tuple[str, str, str]:
    """提取明确目录问法中的效果原文与显式 ItemClass；无法冻结就不路由。"""
    candidates: list[tuple[str, str, str]] = []
    for raw in _CRAFT_CLAUSE_SPLIT_RE.split(question or ""):
        clause = _normalized(raw)
        if not clause or not any(term in clause for term in _BENCH_TERMS):
            continue
        if any(marker in clause for marker in _CRAFT_ROUTE_NEGATIONS):
            continue

        # 「工艺台有没有 X」：X 是完整目录关键词；若是“真实 ItemClass 的效果”，
        # 则把类别单独冻结，不能让模型自行改参。
        for marker in _BENCH_EXISTS_MARKERS:
            at = clause.find(marker)
            if at < 0:
                continue
            raw_query = _strip_bench_query_scaffolding(clause[at + len(marker):])
            query, item_class = _split_bench_item_class(raw_query)
            if len(query) >= 2:
                candidates.append((clause, query, item_class))

        # 「工艺工作台页面的 X 原始费用是什么」：取工艺台词之后、事实字段之前。
        fact_hits = [
            (clause.find(marker), -len(marker), marker)
            for marker in _BENCH_FACT_MARKERS
            if clause.find(marker) >= 0
        ]
        if fact_hits:
            marker_at, _neg_len, marker = min(fact_hits)
            term_hits = [
                (clause.rfind(term, 0, marker_at), term)
                for term in _BENCH_TERMS
                if clause.rfind(term, 0, marker_at) >= 0
            ]
            if term_hits:
                term_at, term = max(term_hits, key=lambda item: item[0])
                raw_query = _strip_bench_query_scaffolding(
                    clause[term_at + len(term):marker_at]
                )
                query, item_class = _split_bench_item_class(raw_query)
                if len(query) >= 2:
                    candidates.append((clause, query, item_class))
    return candidates[-1] if candidates else ("", "", "")


def _precision_tool_route(question: str) -> str:
    """只为参数可从问题逐字冻结的结构化能力放行精确工具。"""
    if _positive_craft_clause(question):
        return "check_mod_combination"
    return "bench_recipe_catalog" if _bench_precision_query(question)[1] else ""


def _strip_target_scaffolding(segment: str) -> str:
    leading = (
        "请帮我判断", "请判断", "帮我判断", "真正想问", "实际想问", "我是问",
        "再判断", "然后判断", "接着判断", "判断", "请帮我看", "帮我看",
        "看看", "想知道", "对比", "比较", "这个", "这些",
    )
    trailing = ("能否", "是否", "会不会", "能不能", "可以", "可否", "会", "能", "在")
    prefixes = ("上的", "上面有", "上有", "上", "里面有", "里有", "中有", "的", "有", "带有", "包含")
    changed = True
    while changed and segment:
        changed = False
        for token in (*leading, *prefixes):
            if segment.startswith(token):
                segment = segment[len(token):]
                changed = True
                break
        for token in trailing:
            if segment.endswith(token):
                segment = segment[:-len(token)]
                changed = True
                break
    return segment


def _target_segment(clause: str, item_class: str) -> str:
    """从正向判断子句抽出完整目标列表文本；不做词缀知识猜测。"""
    positions = [
        (clause.rfind(intent), intent)
        for intent in _CRAFT_COMBINATION_INTENTS
        if intent in clause
    ]
    if not positions:
        return ""
    marker_at, intent = max(positions, key=lambda item: item[0])

    # 同一意图同时支持“目标 + 能否共存”与“能同时存在 + 目标”两种语序。
    before_raw = clause[:marker_at]
    after_intent = _strip_target_scaffolding(
        clause[marker_at + len(intent):].removesuffix("吗")
    )
    if item_class in before_raw and after_intent:
        return after_intent

    before_intent = _strip_target_scaffolding(before_raw)
    item_at = before_intent.rfind(item_class)
    if item_at < 0:
        return ""
    after_item = _strip_target_scaffolding(before_intent[item_at + len(item_class):])
    if after_item:
        return after_item
    return _strip_target_scaffolding(before_intent[:item_at])


def _validate_precision_call(
    question: str,
    tool_name: str,
    arguments: Any,
) -> str:
    """冻结精确路由的事实查询参数，阻止模型改参后混淆证据归属。"""
    if not isinstance(arguments, dict):
        return "参数必须是 JSON 对象"

    if tool_name == "bench_recipe_catalog":
        clause, expected_query, expected_item_class = _bench_precision_query(question)
        if not clause or not expected_query:
            return "当前问题没有可精确提取的工艺台目录查询"
        if set(arguments) - {"query", "item_class"}:
            return "工艺台目录调用含未允许的参数"
        query = _normalized(str(arguments.get("query") or ""))
        if query != expected_query:
            return "query 必须逐字对应工艺台目录问题中的效果关键词"
        item_class_raw = arguments.get("item_class")
        item_class = _normalized(str(item_class_raw or ""))
        if expected_item_class:
            if item_class != expected_item_class:
                return "item_class 必须逐字对应工艺台目录问题中的显式适用类别"
        elif item_class_raw is not None:
            return "当前工艺台目录问题没有显式 item_class，不得额外添加类别限定"
        return ""

    if tool_name != "check_mod_combination":
        return "当前精确路由不支持该工具"
    clause = _positive_craft_clause(question)
    if not clause:
        return "当前问题没有未被否定的组合判断子句"

    item_class = _normalized(str(arguments.get("item_class") or ""))
    if not item_class or item_class not in clause:
        return "item_class 未对应正向组合判断子句中的底材类别"

    rarity_raw = arguments.get("rarity")
    if rarity_raw is not None:
        rarity = _normalized(str(rarity_raw))
        if not rarity or rarity not in clause:
            return "rarity 未对应正向组合判断子句中的稀有度限定"

    targets = arguments.get("targets")
    if not isinstance(targets, list) or len(targets) < 2:
        return "组合检查至少需要两个目标词缀"
    target_specs: list[tuple[str, str]] = []
    for raw in targets:
        if not isinstance(raw, dict):
            return "targets 中每项都必须是对象"
        query = _normalized(str(raw.get("query") or ""))
        if len(query) < 2:
            return "每个 target.query 至少需要两个字符"
        source = _normalized(str(raw.get("source") or ""))
        target_specs.append((query, source))
    queries = [query for query, _source in target_specs]
    if len(set(queries)) != len(queries):
        return "组合检查的目标词缀不得重复"

    segment = _target_segment(clause, item_class)
    expected = [part for part in _CRAFT_TARGET_SPLIT_RE.split(segment) if part]
    remaining = list(expected)
    # 先绑定带 source 的目标，避免来源词留在分段里让无来源目标抢先报成普通缺失。
    ordered_specs = sorted(target_specs, key=lambda spec: bool(spec[1]), reverse=True)
    for query, source in ordered_specs:
        matches: list[int] = []
        for index, part in enumerate(remaining):
            if source:
                if source not in part or part.replace(source, "", 1) != query:
                    continue
            elif part != query:
                continue
            matches.append(index)
        if len(matches) != 1:
            if source:
                return "target.source 必须与对应 target.query 位于同一目标词组"
            return "targets 必须完整且精确覆盖正向判断子句中的全部目标词组"
        remaining.pop(matches[0])
    if remaining:
        return "targets 必须完整且精确覆盖正向判断子句中的全部目标词组"

    influences = arguments.get("influences") or []
    if not isinstance(influences, list):
        return "influences 必须是数组"
    normalized_influences = [_normalized(str(value)) for value in influences]
    if any(not value or value not in clause for value in normalized_influences):
        return "influences 中存在正向判断子句未提及的条件"
    return ""


def _precision_citations(
    tool_name: str, result: ToolResult
) -> list[dict[str, Any]]:
    """只接受与精确工具类型一致的结论/目录证据。"""
    if tool_name == "check_mod_combination":
        return [
            citation
            for citation in result.citations
            if str(citation.get("chunk_id") or "").startswith("craft_solver:")
            and citation.get("channel") == "derived"
        ]
    if tool_name == "bench_recipe_catalog":
        return [
            citation
            for citation in result.citations
            if str(citation.get("chunk_id") or "").startswith("bench_recipe:")
            and citation.get("channel") == "structured"
            and citation.get("claim_scope") == "bench_catalog"
            and isinstance(citation.get("claim_data"), dict)
            and citation.get("claim_data")
            and citation.get("url")
        ]
    return []


def _call_tool(
    question: str,
    precision_tool: str,
    name: str,
    raw_arguments: str,
) -> tuple[ToolResult, bool]:
    """执行工具，并返回它是否是参数归属正确且带 solver 证据的精确调用。"""
    parsed = _safe_json(raw_arguments)
    if precision_tool and name == precision_tool:
        reason = _validate_precision_call(question, name, parsed)
        if reason:
            return ToolResult(ok=False, error=f"精确工具参数与当前问题不一致：{reason}"), False
    result = REGISTRY.call(name, raw_arguments)
    matched = bool(
        precision_tool
        and name == precision_tool
        and result.ok
        and _precision_citations(precision_tool, result)
    )
    return result, matched


def _initial_user_content(
    question: str, evidence: list[Evidence], precision_tool: str
) -> str:
    if not precision_tool:
        return EVIDENCE_TEMPLATE.format(
            evidence=render_evidence_block(evidence), question=question
        )
    initial = render_evidence_block(evidence) or "（首轮全文检索未形成可引用证据）"
    if precision_tool == "check_mod_combination":
        instruction = (
            "工具参数中的底材、每个目标、来源和影响力都必须逐字对应用户问题。"
            "不得依据首轮候选或其它工具证据自行判断组合可行性。"
        )
    else:
        instruction = (
            "工具参数中的目录关键词和可选 ItemClass 必须逐字对应用户问题。"
            "目录证据只可回答页面列出的效果、原始费用、适用类别与解锁信息；"
            "不得扩写为可执行制作步骤、概率、市场成本或路线比较。"
        )
    return (
        f"以下是首轮证据：\n\n{initial}\n\n---\n"
        f"精确工具路由已命中 `{precision_tool}`。回答前必须先调用该工具；"
        f"{instruction}\n\n用户的问题：{question}"
    )


def _precision_failure_answer(tool_name: str) -> str:
    if tool_name == "check_mod_combination":
        action = "因此不作组合判断"
        evidence_kind = "确定性求解证据"
    else:
        action = "因此不陈述工艺台目录事实"
        evidence_kind = "对应的结构化目录证据"
    return (
        f"已识别为需要精确工具 `{tool_name}` 的问题，但本轮没有形成唯一、"
        f"参数与当前问题一致且带{evidence_kind}的调用；{action}。"
    )


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
    precision_tool = _precision_tool_route(question)
    if precision_tool:
        result.retrieval_meta["precision_route"] = precision_tool
    if not data.get("sufficient") and not precision_tool:
        # 依据不足且没有确定性精确工具路由 → 由代码生成缺据声明，模型完全不参与
        result.evidence_gap = True
        result.answer, gap_category = _gap_answer(question, data)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        _record_gaps(
            question, [str(data.get("reason") or "依据不足")], category=gap_category
        )
        _persist(result, conversation_id)
        return result

    # ── 第二步：编号证据并生成 ──
    # BM25 不充分时不能把「最不差」的命中当证据；精确路由只给模型调用工具的机会。
    evidence = number_evidence(
        [] if precision_tool else (first.citations if data.get("sufficient") else [])
    )
    result.evidence = evidence
    if first.meta.get("degraded"):
        result.degraded_note = (
            f"检索以降级通道完成（{', '.join(first.meta.get('channels') or [])}）："
            f"{first.meta.get('note') or '向量与重排未启用'}"
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt()},
        {
            "role": "user",
            "content": _initial_user_content(question, evidence, precision_tool),
        },
    ]

    budget = TurnBudget(limit=int(cfg.get("llm.max_tokens_per_turn", 60000)))
    max_calls = int(cfg.get("llm.max_tool_calls", 12))
    tools = _prepare_tools()
    answer_text = ""
    final_tool_results: dict[str, ToolResult] = {}
    precision_tool_succeeded = not bool(precision_tool)
    precision_call_invalidated = False

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
                if precision_tool and tc.name == precision_tool and precision_tool_succeeded:
                    precision_call_invalidated = True
                    tr = ToolResult(
                        ok=False,
                        error="精确工具已成功完成；本轮再次改参会使证据归属不唯一",
                    )
                    precision_match = False
                else:
                    tr, precision_match = _call_tool(
                        question, precision_tool, tc.name, tc.arguments
                    )
                result.tool_calls.append(
                    _tool_record(tc.name, _safe_json(tc.arguments), tr)
                )
                final_tool_results[tc.name] = tr
                if precision_match:
                    precision_tool_succeeded = True
                    # 精确回答只允许使用本次、参数归属正确且类型匹配的证据。
                    evidence = number_evidence(_precision_citations(precision_tool, tr))
                    result.evidence = evidence
                elif not precision_tool and tr.citations:
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

    if not precision_tool_succeeded or precision_call_invalidated:
        result.evidence_gap = True
        result.answer = _precision_failure_answer(precision_tool)
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        _persist(result, conversation_id)
        return result

    _record_final_tool_result_gaps(question, final_tool_results)
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

def stream_turn(
    question: str,
    *,
    conversation_id: str | None = None,
    client: LLMClient | None = None,
) -> Iterator[dict[str, Any]]:
    """流式版本。产出事件字典，由 `api/chat.py` 包成 SSE。

    **粒度必然是句，不是字。** 校验单位是句子——判断一句是不是事实断言、绑定编号
    是否有效、句中数值能否溯源，都得等这句说完。逐字推送等于把未校验内容先给了
    用户，之后没法撤回。前端可以对已通过的句子做打字机效果，观感接近逐字流，
    但不会显示任何未校验内容。

    与 `run_turn` 共用同一套检索、闸门与轨迹逻辑，只是把「一次性校验」换成
    `StreamingGate` 增量校验，判定结果两条路径一致（有单测保证）。
    """
    started = time.monotonic()
    cfg = get_config()
    client = client or get_llm_client()
    trace_id = uuid.uuid4().hex
    result = TurnResult(trace_id=trace_id, question=question, answer="")
    yield {"type": "meta", "trace_id": trace_id, "conversation_id": conversation_id}

    # ── 强制检索 ──
    first = REGISTRY.call("kb_search", json.dumps({"query": question}, ensure_ascii=False))
    result.tool_calls.append(_tool_record("kb_search", {"query": question}, first))
    result.retrieval_meta = dict(first.meta)

    if not first.ok:
        result.evidence_gap = True
        result.answer = f"检索不可用（{first.error}），无法确认依据，本轮不作答。"
        yield {"type": "gap", "text": result.answer}
        yield from _finish(result, conversation_id, started)
        return

    data = first.data or {}
    precision_tool = _precision_tool_route(question)
    if precision_tool:
        result.retrieval_meta["precision_route"] = precision_tool
    if not data.get("sufficient") and not precision_tool:
        # 依据不足且没有确定性精确工具路由 → 缺据声明由代码生成
        result.evidence_gap = True
        result.answer, gap_category = _gap_answer(question, data)
        _record_gaps(
            question, [str(data.get("reason") or "依据不足")], category=gap_category
        )
        yield {"type": "gap", "text": result.answer}
        yield from _finish(result, conversation_id, started)
        return

    evidence = number_evidence(
        [] if precision_tool else (first.citations if data.get("sufficient") else [])
    )
    result.evidence = evidence
    if first.meta.get("degraded"):
        result.degraded_note = (
            f"检索以降级通道完成（{', '.join(first.meta.get('channels') or [])}）："
            f"{first.meta.get('note') or '向量与重排未启用'}"
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt()},
        {
            "role": "user",
            "content": _initial_user_content(question, evidence, precision_tool),
        },
    ]
    budget = TurnBudget(limit=int(cfg.get("llm.max_tokens_per_turn", 60000)))
    max_calls = int(cfg.get("llm.max_tool_calls", 12))
    tools = _prepare_tools()

    gate = StreamingGate(
        evidence,
        factual_terms=list(cfg.get("retrieval.factual_terms", []) or []),
        domain_terms=_domain_terms(),
    )
    announced_thinking = False
    final_tool_results: dict[str, ToolResult] = {}
    precision_tool_succeeded = not bool(precision_tool)
    precision_call_invalidated = False

    try:
        for _round in range(max_calls + 1):
            pending: list[Any] = []
            round_content: list[str] = []
            for event in client.stream(messages, tools=tools, budget=budget):
                if event.kind == "thinking":
                    if not announced_thinking:
                        announced_thinking = True
                        yield {"type": "thinking"}
                elif event.kind == "content":
                    if precision_tool:
                        # 精确路径整轮缓冲：直到确认该轮没有后续工具调用，正文才进闸门。
                        round_content.append(event.text)
                    else:
                        for verdict in gate.feed(event.text):
                            yield _sentence_event(verdict)
                elif event.kind == "tool_calls":
                    pending = list(event.tool_calls)
                elif event.kind == "usage" and event.usage is not None:
                    # 模型名必须记下来：故障转移会切模型，不同模型的推理 token
                    # 行为差别很大（实测 claude-opus-5 的 reasoning 恒为 0），
                    # 少了这个字段就会把「换了模型」误判成 bug
                    result.model = event.model or result.model
                    result.tokens = {
                        "prompt": budget.used.prompt_tokens,
                        "output": budget.used.completion_tokens,
                        "reasoning": budget.used.reasoning_tokens,
                        "total": budget.used.total_tokens,
                    }
                elif event.kind == "error":
                    yield {"type": "error", "message": event.detail}
                    pending = []
                    break

            if not pending:
                if precision_tool_succeeded and not precision_call_invalidated:
                    for piece in round_content:
                        for verdict in gate.feed(piece):
                            yield _sentence_event(verdict)
                break

            # 模型要调工具：执行后开新的一轮流
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in pending
                ],
            })
            for tc in pending:
                yield {"type": "tool", "name": tc.name}
                if precision_tool and tc.name == precision_tool and precision_tool_succeeded:
                    precision_call_invalidated = True
                    tr = ToolResult(
                        ok=False,
                        error="精确工具已成功完成；本轮再次改参会使证据归属不唯一",
                    )
                    precision_match = False
                else:
                    tr, precision_match = _call_tool(
                        question, precision_tool, tc.name, tc.arguments
                    )
                result.tool_calls.append(
                    _tool_record(tc.name, _safe_json(tc.arguments), tr)
                )
                final_tool_results[tc.name] = tr
                if precision_match:
                    precision_tool_succeeded = True
                    evidence = number_evidence(_precision_citations(precision_tool, tr))
                    result.evidence = evidence
                    gate.add_evidence(evidence)
                elif not precision_tool and tr.citations:
                    evidence = _merge_evidence(evidence, tr.citations)
                    result.evidence = evidence
                    # 编号是追加式的，旧编号不变，所以只需把新证据补进闸门的映射
                    gate.add_evidence(evidence)
                messages.append({
                    "role": "tool", "tool_call_id": tc.id, "content": tr.for_model(),
                })
            messages.append({
                "role": "user",
                "content": "当前可用证据（编号已更新，请据此标注）：\n\n"
                           + render_evidence_block(evidence),
            })
        else:
            logger.warning("流式工具调用达到上限 %d", max_calls)
    except LLMError as exc:
        logger.error("流式生成失败（trace=%s）：%s", trace_id, exc)
        yield {"type": "error", "message": f"生成失败（{type(exc).__name__}）"}

    if not precision_tool_succeeded or precision_call_invalidated:
        result.evidence_gap = True
        result.answer = _precision_failure_answer(precision_tool)
        yield {"type": "gap", "text": result.answer}
        yield from _finish(result, conversation_id, started)
        return

    _record_final_tool_result_gaps(question, final_tool_results)
    for verdict in gate.flush():
        yield _sentence_event(verdict)

    binding = gate.result()
    result.binding = binding
    result.answer = binding.text
    result.citations = cit.collect(evidence, binding.used_labels)

    if result.citations:
        yield {
            "type": "citations",
            "items": [
                {"label": c.label, "source": c.source, "title": c.title,
                 "url": c.url, "priority": c.priority}
                for c in result.citations
            ],
        }
    if result.degraded_note:
        yield {"type": "note", "text": result.degraded_note}
    if binding.blocked:
        logger.warning(
            "流式绑定校验拦截 %d 句（trace=%s）", binding.blocked, trace_id
        )
        _record_gaps(question, binding.gaps)

    yield from _finish(result, conversation_id, started)


def _sentence_event(verdict) -> dict[str, Any]:  # noqa: ANN001
    """一句校验结果 → 下发事件。

    未通过的句子下发的是**替换文本**（`output`），不是原文——原文是无据断言，
    不能出现在用户面前。
    """
    if verdict.ok:
        return {
            "type": "sentence",
            "text": verdict.text,
            "kind": verdict.kind,
            "labels": list(verdict.labels),
        }
    return {
        "type": "blocked",
        "text": verdict.output,
        "reason": verdict.reason,
    }


def _finish(
    result: TurnResult, conversation_id: str | None, started: float
) -> Iterator[dict[str, Any]]:
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    _persist(result, conversation_id)
    yield {
        "type": "done",
        "trace_id": result.trace_id,
        "evidence_gap": result.evidence_gap,
        "blocked_claims": result.blocked,
        "model": result.model,
        "tokens": result.tokens,
        "elapsed_ms": result.elapsed_ms,
    }


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
                claim_scope=e.claim_scope,
                claim_data=e.claim_data,
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


_CRAFT_GAP_CATEGORIES = {
    "target_not_resolved": "not_recalled/mod_affix",
    "candidate_search_truncated": "analysis_limit/craft_feasibility",
    "assignment_search_truncated": "analysis_limit/craft_feasibility",
    "item_class_not_resolved": "input_clarification/item_class",
    "base_page_scope": "input_clarification/base_item",
    "affix_capacity": "not_collected/craft_rule",
    "source_requirement": "not_collected/craft_rule",
    "influence_requirement": "not_collected/craft_rule",
}


def _record_final_tool_result_gaps(
    question: str,
    final_results: dict[str, ToolResult],
) -> None:
    """只记录每种工具最后一次调用仍未解决的缺口，避免把中间重试污染成欠债。"""
    seen: set[tuple[str, str]] = set()
    for result in final_results.values():
        _record_tool_result_gaps(question, result, seen=seen)


def _record_tool_result_gaps(
    question: str,
    result: ToolResult,
    *,
    seen: set[tuple[str, str]],
) -> None:
    """把结构化工具的缺据写入统一台账；同一轮同一缺口只计一次。"""
    if not result.ok or not isinstance(result.data, dict):
        return
    raw_gaps = result.data.get("gaps")
    if not isinstance(raw_gaps, list):
        return
    by_category: dict[str, list[str]] = {}
    for raw in raw_gaps:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "structured_tool_gap")
        message = str(raw.get("message") or "").strip()
        if not message or (code, message) in seen:
            continue
        seen.add((code, message))
        category = _CRAFT_GAP_CATEGORIES.get(code, "not_collected/craft_rule")
        by_category.setdefault(category, []).append(f"[{code}] {message}")
    for category, points in by_category.items():
        _record_gaps(question, points, category=category)


def _record_gaps(question: str, gaps: list[str], *, category: str = "") -> None:
    """写缺口台账。按频次排序即为采集优先级（需求 2.12）。

    `category` 来自覆盖归因（`not_collected/<域>` 或 `not_recalled/<域>`），
    它让台账能区分「该去采集」与「该改检索」——这是两类完全不同的待办。

    用 `ON CONFLICT(question, missing_point) DO UPDATE` 而非「先插再更新」：
    先前那种写法依赖一个**并不存在**的唯一约束，结果每次都插新行，随后的
    UPDATE 又把所有同题行一起加一，hit_count 因此虚高数倍
    （实测一条问题 18 行、计数 189）。唯一索引已由 `core/db._migrate` 补上。
    """
    if not gaps:
        return
    try:
        conn = dbmod.get_conn()
        for point in gaps:
            conn.execute(
                """
                INSERT INTO knowledge_gap(question, missing_point, guessed_category,
                                          hit_count, first_seen, last_seen)
                VALUES (?,?,?,1,datetime('now'),datetime('now'))
                ON CONFLICT(question, missing_point) DO UPDATE SET
                    hit_count = hit_count + 1,
                    last_seen = datetime('now'),
                    guessed_category = COALESCE(excluded.guessed_category,
                                                knowledge_gap.guessed_category)
                """,
                (question, point[:500], category or None),
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
                             "title": e.title, "claim_scope": e.claim_scope}
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
