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
    #: 模型**未过闸门**的原始正文。给评测录制回放用（`scripts/eval_run.py --record`）：
    #: 要量「闸门拦得对不对」，就必须留下它拦之前的东西。
    #:
    #: **绝不外发。** `api/chat.py` 的 `ChatResponse` 不含这个字段，也不要加进去——
    #: 它里面正是可能无据的断言。与 `reasoning_content` 同等对待。
    raw_answer: str = ""
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


def _tools_for_question(question: str) -> list[dict[str, Any]]:
    """按本轮精确来源授权裁剪 schema；执行层仍会做第二次检查。"""
    tools = _prepare_tools()
    affix_authorized = bool(_affix_pool_precision_query(question)[0])
    metamod_excluded = _metamod_source_policy(question) == "excluded"
    return [
        schema
        for schema in tools
        if not (
            str((schema.get("function") or {}).get("name") or "")
            == "metamod_interaction_matrix"
            and metamod_excluded
        )
        and not (
            str((schema.get("function") or {}).get("name") or "")
            == "affix_pool_odds"
            and not affix_authorized
        )
    ]


#: 《流放之路 2》（Path of Exile 2 / 流放之路：降临）的标记词。
#:
#: **本项目只覆盖 POE1。** 这不是一句声明，而是必须由代码执行的边界：两款游戏大量概念
#: 同名（升华、词缀、通货、地图、暴击），拿 POE1 的数值回答 POE2 的问题，用户看不出来，
#: 而「一个错误的小前提会让整条结论反向」正是本项目的第一原则要防的。
#:
#: **在原始问题串上匹配，不走分词**——这是关键。实测分词会把数字标记吃掉：
#: `poe2 词缀怎么洗` → `['poe', '词缀']`、`流放之路2 的升华机制` → `['流放', '升华', '机制']`。
#: 于是「poe2 词缀怎么洗」的查询词覆盖率变成 100%，被判**依据充分**——一个 POE2 问题
#: 拿到了 POE1 的词缀数据。靠覆盖率拦不住这类问题，只有专指标记能拦。
#:
#: 每个标记都在 POE1 语料里核对过出现次数为 0。**刻意不收单独的「降临」**：
#: POE1 有天赋节点「先祖降临」「惊寒降临」，拿它当标记会把这两个正常问题拒掉——
#: 与 boss 领域曾把「怪物」当意图词是同一种错，意图词必须专指。
_POE2_MARKERS: tuple[str, ...] = (
    "流放之路2", "流放之路 2", "流放之路二", "流放之路：降临", "流放之路:降临",
    "poe2", "poe 2", "poe ii", "path of exile 2", "pathofexile2",
    "第二部", "二代",
)

#: POE2 的权威来源。缺据声明必须指路，否则用户以为「游戏里没这东西」。
_POE2_AUTHORITIES = (
    "https://poe2db.tw/cn/  （POE2 的数据站，与本库用的 poedb.tw 是两个站）",
    "https://pathofbuilding.community/  的 Path of Building 2（POE2 是独立的 app）",
)


def poe2_marker(question: str) -> str:
    """问题里出现的第一个 POE2 标记；没有则返回空串。

    公开函数而非私有：黄金问答集与单测都要按同一套判据核对，
    复制一份判据出去迟早分叉。
    """
    lowered = (question or "").lower()
    for marker in _POE2_MARKERS:
        if marker in lowered:
            return marker
    return ""


def _out_of_scope_answer(question: str, marker: str) -> str:
    """POE2 越界声明。由代码生成、不经模型——与缺据声明同一个理由：必须完全可预测。"""
    lines = "\n".join(f"- {url}" for url in _POE2_AUTHORITIES)
    return (
        f"这个问题指向《流放之路 2》（问题里出现了「{marker}」），"
        "而本助手只覆盖**《流放之路 1》**，因此不作答。\n\n"
        "**为什么不能拿 POE1 的数据回答**\n"
        "两款游戏大量概念同名（升华、词缀、通货、地图、暴击），但机制与数值并不通用。"
        "用 POE1 的数值回答 POE2 的问题，得到的是一个看起来合理、实际错误的答案，"
        "而你无法从答案里看出这一点。\n\n"
        f"**建议的查证途径**\n{lines}\n"
    )


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


def _exact_entity_ids(citations: list[dict[str, Any]]) -> list[str]:
    """别名通道命中的实体 id —— 维度闸门的第二条领域识别信号。

    判据本身在 `retrieval.alias.exact_entity_ids`，这里只做形态适配（citation 字典
    → (通道, 实体 id)）。评测侧拿到的是 `Hit` 对象，走同一个函数——两边各写一份
    过滤就会让「评测量到的路径」与线上分叉。
    """
    from retrieval import alias as alias_mod  # noqa: PLC0415  与本模块其它处一致，避免导入环

    return alias_mod.exact_entity_ids(
        (str(c.get("channel") or ""), str(c.get("entity_id") or "")) for c in citations
    )


def _dimension_verdict(question: str, citations: list[dict[str, Any]] | None):  # noqa: ANN202
    """覆盖层对「被问的维度有没有数据」的判决。探测失败返回 None（不阻塞问答）。

    **与检索是否充分无关**：先前只在 `sufficient=True` 时才问它，于是
    「BM25 命不中 + 偏好工具路由」这条路上它根本没被咨询过——实测
    「大星团珠宝的斧剑伤害能开出哪些核心天赋」因此进了模型（那个映射本库没有）。
    """
    try:
        from kb.coverage import dimension_gap  # noqa: PLC0415  延迟导入避免循环

        return dimension_gap(
            dbmod.get_conn(),
            question,
            exact_entity_ids=_exact_entity_ids(citations or []),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("维度覆盖检查失败，按检索判定继续：%s", exc)
        return None


def _apply_dimension_gate(
    question: str, data: dict[str, Any], *, citations: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """充分性判定之后的第二道：**被问的维度**有没有数据（见 `kb.coverage.dimension_gap`）。

    别名精确命中只证明「这个实体在库里」，不证明「被问的那一面有数据」。实测
    「猎首现在值多少混沌石」命中猎首即判依据充分，而价格维度一个字都没有；
    「点到某节点要花几点」命中天赋节点即判充分，而天赋点消耗未入库；
    「冰霜脉冲的伤害是多少」命中宝石即判充分，而伤害数值本轮明确未入库。

    只做一个方向的改判（充分 → 不足）：放宽依据需要新证据，这里没有引入任何新证据。
    探测失败时按检索判定继续——覆盖层不可用不该让问答停摆。
    """
    verdict = _dimension_verdict(question, citations)
    if verdict is None or not verdict.is_gap:
        return data
    logger.info("维度覆盖闸门改判为依据不足：%s", verdict.reason())
    out = dict(data)
    out["sufficient"] = False
    out["reason"] = verdict.reason()
    out["missing_terms"] = verdict.missing_points()
    out["dimension_gate"] = True
    return out


#: 工具 `data` 里那些**要求必须传达给用户**的声明字段，逐字取用。
#: 它们不是游戏事实而是「这份答案的边界」，所以由代码原样附在答案末尾。
#: `boundary` 是第四次撞上同一个模式：**项目要求模型说的话，被项目自己的闸门拦了。**
#:
#: 实测（元词缀矩阵刚入库后的第一次真实调用）：模型照提示词写了
#: 「该矩阵单元格仅复述原始结果，不证明给定装备可执行，也不提供完整状态转移…[[E1]]」，
#: 而闸门判「Metamod 矩阵证据仅支撑页面单元格原字段，不能支撑给定装备状态下的可执行性」
#: —— 拦得**没错**：E1 确实支撑不了那些语义。错的是让模型来说这句话。
#:
#: 这句话的原文就在工具返回的 `data.boundary` 里（`metamods` / `bench` / `craft_pool`
#: 三个工具都给），所以走与截断计数、覆盖声明同一条路：代码取值、附在闸门之后。
_NOTE_FIELDS: tuple[str, ...] = (
    "coverage_note", "channels_no_match_note", "boundary",
    # 跨来源确认冲突的并列对照（需求 15.5）。由 `citation.conflict_block` 渲染好放进
    # 工具的 `data`，代码逐字附上——**不能让模型转述**：那几个数字来自两个来源，
    # 模型说出来只能无绑定地说，然后被出口闸门拦掉。
    # 在 2026-08-19 之前这条路是不可达的：`kind='mod'` 的已确认冲突一直是 0 条，
    # 直到幽魂对账给出 8 条（`kind='spectre'`）。
    "conflict_block",
)

#: 权重占比的写法：`1500（1.888%）`。出现即说明证据里带组内占比。
_WEIGHT_PCT_RE = re.compile(r"\d+\s*（\s*\d+(?:\.\d+)?%\s*）")


def _answer_notes(
    tool_calls: list[dict[str, Any]], evidence: list[Evidence]
) -> list[str]:
    """由**代码**生成的答案附注：截断、覆盖不完整、零命中、权重口径。

    ## 为什么必须由代码说

    这几件事项目自己明文要求说出来：

      「枚举类工具静默截断等同于漏答，超出上限必须写出还剩多少」
      「上面的清单在这些维度上必然不完整——回答时必须如实说明」（stat_sources 的 coverage_note）
      「这个数不是任何通货或具体工艺的一次使用成功率」（affix_pool / 权重占比）

    但它们**都不在任何证据里**：截断计数只在工具 `data`（实测 `mod_tiers` 返回
    `ladders_total=21 / ladders_returned=6`，卡片里一个字都没有），覆盖声明同理。
    于是模型只能无绑定地说出来，然后被出口闸门当「事实句未绑定引用」拦掉——
    **项目要求说的话被项目自己拦了**，用户拿到一份残缺清单却看不到截断提示。

    所以走 `degraded_note` 同一条路：代码生成、附在闸门之后、完全可预测。
    这也比「把这类句子加进白名单豁免」安全——豁免等于允许模型自己编这些数字。
    """
    notes: list[str] = []

    def add(note: str) -> None:
        text = note.strip()
        if text and text not in notes:
            notes.append(text)

    for record in tool_calls:
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        tool = str(record.get("tool") or "工具")
        if data.get("truncated"):
            total = data.get("ladders_total") or data.get("total")
            returned = data.get("ladders_returned") or data.get("returned")
            if isinstance(total, int) and isinstance(returned, int):
                add(
                    f"**本轮枚举被截断**：`{tool}` 命中 {total} 项，只返回了 {returned} 项。"
                    f"上面的清单不完整，剩余 {total - returned} 项未列出——"
                    "需要完整清单请缩小查询范围（指定底材类别或来源渠道）后再问。"
                )
            else:
                add(
                    f"**本轮枚举被截断**：`{tool}` 的结果超出返回上限，上面的清单不完整。"
                )
        for field in _NOTE_FIELDS:
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                add(value)

    if any(_WEIGHT_PCT_RE.search(e.text) for e in evidence):
        add(
            "**括号内的百分比是该档在同组内的生成权重占比**，"
            "不等于任何通货或具体工艺的一次使用成功率，也不等于期望消耗。"
        )
    return notes


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


_METAMOD_MATRIX_TERMS = (
    "元词缀矩阵", "工艺互动矩阵", "metamodmatrix", "metamods矩阵",
    "craftofexile矩阵", "来源矩阵",
)


def _known_metamod_pairs() -> tuple[tuple[str, str], ...]:
    """读取事实键及最近成功快照的空白键，防止长操作回退成有值短子串。"""
    try:
        conn = dbmod.get_conn()
        rows = conn.execute(
            "SELECT operation_text, guard_text FROM craft_metamod_interaction "
            "ORDER BY length(operation_text) DESC, operation_text, guard_text"
        ).fetchall()
        pairs = [
            (str(row[0]), str(row[1])) for row in rows if row[0] and row[1]
        ]
        audit = conn.execute(
            "SELECT note FROM ingest_run WHERE category='metamod_matrix' "
            "AND succeeded>0 ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if audit and audit[0]:
            note = json.loads(audit[0])
            for cell in note.get("blank_cells", []) if isinstance(note, dict) else []:
                if not isinstance(cell, dict):
                    continue
                operation = cell.get("operation_text")
                guard = cell.get("guard_text")
                if operation and guard:
                    pairs.append((str(operation), str(guard)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Metamod 矩阵键不可用，不启用精确路由：%s", exc)
        return ()
    return tuple(
        sorted(set(pairs), key=lambda pair: (-len(pair[0]), pair[0], pair[1]))
    )


def _metamod_source_policy(question: str) -> str:
    """返回 positive / excluded / absent；来源排除同时约束路由与工具权限。"""
    clauses = [
        clause for raw in _CRAFT_CLAUSE_SPLIT_RE.split(question or "")
        if (clause := _normalized(raw))
    ]
    negative_prefixes = ("不要", "请勿", "别", "不想", "不愿", "不用", "无需", "不需要", "不")
    source_actions = (
        "把", "将", "要", "用", "使用", "采用", "参考", "查看", "查询", "看", "查", "考虑",
    )
    source_left_negations = tuple(
        prefix + action for prefix in negative_prefixes for action in source_actions
    ) + ("不是问", "并非问", "并非询问", "排除")
    source_left_positives = (
        "只给", "只看", "只查", "只用", "仅给", "仅看", "仅查", "仅用", "而是问",
        "改问", "实际想问", "真正想问",
    )
    source_right_negations = (
        "不是我想问", "不是要问", "并非我想问", "并非要问", "我不想要", "我不想用",
        "我不想参考", "我不想使用", "我不想采用", "我不愿用", "我不愿参考",
        "我不要用", "不要用", "请勿使用", "不用", "无需", "不需要", "不看", "不查",
        "别用", "不作为来源", "不要作为来源", "排除", "排除掉",
    )
    output_constraint_prefixes = (
        "不要解释", "不用解释", "无需解释", "不需要解释", "别解释",
        "不要展开", "不用展开", "无需展开", "不需要展开", "别展开",
    )
    source_scaffolding = (
        "里的", "里", "中的", "中", "这个", "这类", "的话", "作为来源", "也", "则", "就", "先",
    )

    def strip_scaffolding(value: str) -> str:
        changed = True
        while changed and value:
            changed = False
            for scaffold in source_scaffolding:
                if value.startswith(scaffold):
                    value = value[len(scaffold):]
                    changed = True
                    break
        return value

    decisions: list[tuple[int, int, str]] = []
    for clause_index, clause in enumerate(clauses):
        next_clause = clauses[clause_index + 1] if clause_index + 1 < len(clauses) else ""
        occurrences: list[tuple[int, str]] = []
        for term in _METAMOD_MATRIX_TERMS:
            start = 0
            while (term_at := clause.find(term, start)) >= 0:
                occurrences.append((term_at, term))
                start = term_at + len(term)
        for term_at, term in sorted(occurrences):
            prefix = clause[max(0, term_at - 24):term_at]
            suffix = strip_scaffolding(clause[term_at + len(term):])
            adjacent = strip_scaffolding(next_clause)
            left_negative_at = max(
                (prefix.rfind(marker) for marker in source_left_negations),
                default=-1,
            )
            left_positive_at = max(
                (prefix.rfind(marker) for marker in source_left_positives),
                default=-1,
            )
            left_negated = left_negative_at > left_positive_at
            right_is_output_constraint = any(
                suffix.startswith(marker) for marker in output_constraint_prefixes
            )
            adjacent_is_output_constraint = any(
                adjacent.startswith(marker) for marker in output_constraint_prefixes
            )
            right_targets_other_source = any(
                marker in suffix
                for marker in (
                    "其他来源", "其它来源", "其他资料", "其它资料", "其他证据", "其它证据",
                )
            )
            adjacent_targets_other_source = any(
                marker in adjacent
                for marker in (
                    "其他来源", "其它来源", "其他资料", "其它资料", "其他证据", "其它证据",
                )
            )
            right_negated = (
                not right_is_output_constraint
                and not right_targets_other_source
                and any(suffix.startswith(marker) for marker in source_right_negations)
            )
            adjacent_negated = (
                not adjacent_is_output_constraint
                and not adjacent_targets_other_source
                and any(adjacent.startswith(marker) for marker in source_right_negations)
            )
            policy = (
                "excluded"
                if left_negated or right_negated or adjacent_negated
                else "positive"
            )
            decisions.append((clause_index, term_at, policy))
    if not decisions:
        return "absent"
    # 同一问题可先提及后改口；只采用文本中最后一次明确来源动作。
    return max(decisions, key=lambda decision: (decision[0], decision[1]))[2]


def _metamod_precision_query(question: str) -> tuple[str, str, str]:
    """从明确矩阵问法中逐字冻结 operation + guard；歧义时拒绝路由。"""
    normalized = _normalized(question)
    if not normalized or _metamod_source_policy(question) != "positive":
        return "", "", ""

    positive_turns = (
        "而是", "只给", "只看", "只查", "改查", "改看", "真正想问", "实际想问",
    )
    pair_negations = (
        "不是", "不要", "不要用", "不要使用", "请勿", "请勿使用", "不用",
        "无需", "不需要", "不必", "不看", "不查", "别", "别用", "排除", "忽略",
    )
    known_pairs = _known_metamod_pairs()
    matched: list[tuple[str, str]] = []
    for raw in _CRAFT_CLAUSE_SPLIT_RE.split(question or ""):
        segment = _normalized(raw)
        if not segment:
            continue
        # 正向转折/限定词之前可以是否定范围；之后只按 pair 自身 span 判极性。
        turns = [
            (segment.rfind(turn), turn)
            for turn in positive_turns
            if segment.rfind(turn) >= 0
        ]
        if turns:
            turn_at, turn = max(turns, key=lambda item: item[0])
            segment = segment[turn_at + len(turn):]

        candidates = [
            (operation, guard)
            for operation, guard in known_pairs
            if _normalized(operation) in segment and _normalized(guard) in segment
        ]
        if not candidates:
            continue
        # 先保留当前子句的最长 operation，再判极性；被否定的长 operation
        # 不能以其内部短 operation（如“混沌石”）回退。
        max_len = max(len(_normalized(operation)) for operation, _guard in candidates)
        for operation, guard in candidates:
            operation_norm = _normalized(operation)
            guard_norm = _normalized(guard)
            if len(operation_norm) != max_len:
                continue
            operation_at = segment.find(operation_norm)
            guard_at = segment.find(guard_norm)
            pair_start = min(operation_at, guard_at)
            pair_end = max(
                operation_at + len(operation_norm), guard_at + len(guard_norm)
            )
            prefix = segment[max(0, pair_start - 12):pair_start]
            pair_span = segment[pair_start:pair_end]
            if any(prefix.endswith(marker) for marker in pair_negations):
                continue
            if any(marker in pair_span for marker in pair_negations):
                continue
            matched.append((operation, guard))

    if not matched:
        # 已明确限定矩阵来源但键无法唯一冻结时仍保持精确 fail-closed 路径，
        # 不能退回 general Citation 绕过矩阵 claim scope。
        return normalized, "", ""
    # 空白键参与最长匹配，绝不回退；多键歧义则拒绝路由。
    max_operation_len = max(len(_normalized(operation)) for operation, _guard in matched)
    unique = list(dict.fromkeys(
        pair for pair in matched if len(_normalized(pair[0])) == max_operation_len
    ))
    if len(unique) != 1:
        return normalized, "", ""
    operation, guard = unique[0]
    return normalized, operation, guard


_AFFIX_POOL_UNSUPPORTED_TERMS = (
    "影响力", "裂界", "塑界", "精华", "化石", "收获", "harvest", "隐匿",
    "异能", "eldritch", "synthesis", "metamod", "元词缀", "当前装备",
    "已有词缀", "family排除", "词缀容量", "剩余前缀", "剩余后缀",
    "改造石", "增幅石", "富豪石", "混沌石", "崇高石", "成功率",
    "期望通货", "平均消耗", "制作步骤", "制作路线", "最快", "最经济",
    "造价", "操作流程", "流程", "最划算", "划算", "多少通货", "成本",
    "推荐", "怎么做", "如何做", "预计消耗",
)
_AFFIX_POOL_EXACT_QUERY_RE = re.compile(
    r'^\s*(?:请)?(?:查询|计算|给出)?\s*'
    r'(?:(?:normal|普通)\s*){1,2}(?:来源)?\s*(?:普通)?词缀(?:有效池|池)\s*[:：]\s*'
    r'底材\s*[“"](?P<base>[^”"]+)[”"]\s*[，,]\s*'
    r'(?:物品等级|物等|ilvl)\s*[:：]?\s*(?P<level>\d{1,3})\s*[，,]\s*'
    r'(?P<side>前缀|后缀)侧\s*[，,]\s*'
    r'目标(?:词缀)?\s*[“"](?P<target>[^”"]+)[”"]\s*的\s*'
    r'(?:(?:原始权重|条件权重|条件概率|加权选择)(?:\s*(?:和|与|、)\s*)?)+'
    r'[。！？!?]?\s*$',
    re.I,
)


def _affix_pool_precision_query(
    question: str,
) -> tuple[str, str, int | None, str, str]:
    """只接受完整固定语法，并逐字冻结四个条件；任何混合意图都拒绝。"""
    raw = question or ""
    normalized = _normalized(raw)
    if not normalized or any(term in normalized for term in _AFFIX_POOL_UNSUPPORTED_TERMS):
        return "", "", None, "", ""
    match = _AFFIX_POOL_EXACT_QUERY_RE.fullmatch(raw)
    if match is None:
        return "", "", None, "", ""
    base = match.group("base").strip()
    target = match.group("target").strip()
    level = int(match.group("level"))
    side = "prefix" if match.group("side") == "前缀" else "suffix"
    if not base or not target or not 1 <= level <= 100:
        return "", "", None, "", ""
    return raw, base, level, side, target


#: 问哪类信息 → 该由哪个精确工具回答。判据是**意图词**，与 `_precision_tool_route`
#: 的区别在强度：那个要求参数能从问题里逐字冻结、工具不成功就整轮拒答；这个只
#: 「强制第一次调用走这个工具」，参数仍由模型填，失败就把错误回传让它换个问法。
#:
#: 为什么要用代码判：`agents/knowledge.py` 的提示词已经逐条写了选工具规则，
#: 而 2026-08-17 的评测实测 6 条路由用例只命中 3 条——`mod_tiers`、`entity_lookup`、
#: `kb_coverage` 三类问题模型仍退回 `kb_search`。提示词约束不住，就落到代码。
#: 强制手段是协议层的 `tool_choice`，不是再写一句「请务必调用」。
_PREFERRED_TOOL_INTENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 词缀档位 / 物品等级 / 权重：kb_search 只能命中一张卡再读表格，容易读错行
    ("mod_tiers", (
        "几档", "多少档", "有几级", "档位", "阶级", "梯度", "T级", "t级",
        "最高档", "最高级", "各档", "逐档", "生成权重", "词缀权重",
        # 工具描述里明确列为触发问法的写法，词表漏了就等于自相矛盾
        # （「T1 需要多少物品等级」实测不路由）。T10 含 T1 子串也仍是档位问题
        "T1", "T2", "T3", "数值区间",
    )),
    # 星团珠宝的尺寸、小点数与基石清单只有表、没有卡片。判据要专指「星团」——
    # 单独的「珠宝」会把「珠宝上的词缀」这类有卡片可答的问题抢过来。
    ("cluster_jewel_info", ("星团", "星团珠宝")),
    # 天赋点消耗是**在图上算**出来的，证据不在任何 chunk 里（卡片只有节点属性）。
    # 判据要专指「花几点 / 怎么走」，不能只放「天赋」——那会把
    # 「某天赋给什么属性」这类有卡片可答的问题也抢过来。
    ("passive_path", (
        "几个天赋点", "几点天赋", "要花几点", "多少天赋点", "怎么走到", "怎么点到",
        "点到", "加点路径",
    )),
    # 幽魂倍率表**没有卡片**（1915 行名字 + 两个倍率，卡片之间高度雷同，而怪物名里
    # 有大量通用词），所以它只能靠工具到达——不挂在这里它就是一个死了的能力。
    ("spectre_stats", ("幽魂", "亡灵召唤")),
    # 「某属性能从哪来」是枚举问题：不完整的枚举被当成完整的呈现就是错误答案
    ("stat_sources", (
        "从哪些渠道", "从哪来", "哪些渠道", "哪里获得", "怎么获得", "从哪获得",
        "有哪些来源", "哪些来源",
    )),
    # 关于知识库**覆盖范围**自身的元问题，答案不在 chunk 里。
    # 意图词必须专指「覆盖」这件事：单独的「知识库」「你的库」太宽——
    # 「知识库里有哪些已确认的跨来源数据冲突」问的是冲突不是覆盖，
    # 误路由过去会把一个正确的缺据声明换成一份答非所问的覆盖清单（实测）。
    # 与 boss 领域曾经把「怪物」当意图词是同一种错。
    ("kb_coverage", (
        "覆盖哪些", "覆盖了什么", "covered", "支持哪些领域", "采集了哪些",
        "有哪些领域", "你能回答哪些", "你有哪些数据", "库里有什么数据",
    )),
)

#: `entity_lookup` 单独判：它要求问题**就是**一个已知实体名，
#: 靠意图词判不出来，得查别名表（见 `_preferred_tool_route`）。
_ENTITY_LOOKUP_INTENTS = (
    "完整词缀", "全部词缀", "有哪些词缀", "的词缀列表", "基础信息", "属性是什么",
    "什么属性", "是什么装备",
)


def _preferred_tool_route(question: str) -> str:
    """代码决定本轮该先调哪个精确工具；无把握时返回空串，交回模型自选。

    只在**第一次**调用时强制（`tool_choice`），后续轮次放开——工具结果可能说明
    需要换工具，锁死会让它无路可走。
    """
    for tool, intents in _PREFERRED_TOOL_INTENTS:
        if any(intent in question for intent in intents):
            return tool
    if any(intent in question for intent in _ENTITY_LOOKUP_INTENTS):
        # 实体名必须真的在别名表里才走精确查询，否则这条路由只会让工具空手而归
        try:
            from retrieval import alias as alias_mod  # noqa: PLC0415

            if alias_mod.lookup(dbmod.get_conn(), question):
                return "entity_lookup"
        except Exception as exc:  # noqa: BLE001  别名表不可用不该影响主流程
            logger.debug("实体路由探测失败，交回模型自选：%s", exc)
    return ""


#: 参数能在代码里推导的偏好工具 → 直接由代码调用，模型没有裁量权。
#: 与强制检索 `kb_search` 同一个模式。
#:
#: **为什么不用协议层的 `tool_choice`**：2026-08-18 实测该网关**完全忽略**它——
#: `auto` / `required` / 指定函数名三种写法都不产生工具调用（`tools=[get_weather]` +
#: 「随便聊两句」时 `tool_calls` 恒为 0）。所以「强制调用」在这个网关上只能靠
#: 代码自己调，或者靠裁剪 schema 把别的选项拿掉。
def _mod_tiers_args(question: str) -> dict[str, str] | None:
    """从问题里抽 `mod_tiers` 的参数。抽不出**唯一解**就返回 None，交回模型。

    两个参数都从 `retrieval/vocab.py` 生成的领域词表里逐字取，不做同义词猜测：

        底材类别   `item_class` kind 的 74 个词（含单字「弓」「爪」，
                   正因为 jieba 会把「弓上」粘成一个词才需要这份词表）
        词缀查询   其余领域词，剔掉前后缀标签与来源标签这类**不是词缀内容**的词

    任一侧不唯一就放弃：`mod_tiers` 的返回是精确的 T 级梯度，参数猜错了得到的是
    另一个底材类别的档位，而那种错误在答案里看不出来——宁可退回让模型自己选工具。
    """
    try:
        from retrieval.vocab import scan_terms, terms_of_kind  # noqa: PLC0415

        terms = scan_terms(question)
        classes = terms_of_kind("item_class")
        gen_labels = terms_of_kind("gen")
        sources = terms_of_kind("mod_source")
    except Exception as exc:  # noqa: BLE001  词表不可用就交回模型
        logger.debug("词表不可用，mod_tiers 不走代码路由：%s", exc)
        return None

    hit_classes = [t for t in terms if t in classes]
    if len(hit_classes) != 1:
        return None
    candidates = [
        t for t in terms
        if t not in classes and t not in gen_labels and t not in sources
    ]
    if len(candidates) != 1:
        return None
    return {"item_class": hit_classes[0], "mod_query": candidates[0]}


#: 参数解析器。返回 None 表示抽不出唯一解 → 不代码调用，退回 schema 裁剪。
_CODE_INVOKED_TOOLS: dict[str, Any] = {
    # 路由时已确认该名字在别名表里命中，参数就是问题本身
    "entity_lookup": lambda question: {"name": question},
    # 无参数
    "kb_coverage": lambda question: {},
    # 参数需要从领域词表里抽，可能抽不出来
    "mod_tiers": _mod_tiers_args,
}

#: 这些工具按设计**不产出引用**（覆盖声明是元信息不是游戏事实）。
#: 「一条证据都没拿到就走缺据」的兜底要放过它们，否则它们永远进不了生成。
_TOOLS_WITHOUT_CITATIONS = frozenset({"kb_coverage"})


def _coverage_answer(tool_result: ToolResult) -> str:
    """覆盖声明由**代码**生成，不经模型。

    与缺据声明同一个理由：它必须完全可预测。让模型转述工具里的清单，会得到一篇
    带数字却无引用的答案——实测 33 句里全部被出口闸门拦下（引用有效率从 59% 掉到 13%），
    用户看到的是一堆拦截说明。而这些数字本来就来自运行时探测，不是需要溯源的游戏事实。
    """
    data = tool_result.data or {}
    manifest = str(data.get("manifest") or "").strip()
    total = data.get("domains_total")
    usable = data.get("domains_usable")
    head = "以下是本知识库当前的覆盖范围（由运行时探测生成，非模型转述）"
    if isinstance(total, int) and isinstance(usable, int):
        head += f"：共声明 {total} 个领域，其中 {usable} 个已有数据可作为依据"
    return f"{head}。\n\n{manifest}"


def _invoke_preferred_tool(
    question: str, tool: str, result: TurnResult
) -> ToolResult | None:
    """由代码直接调用偏好工具。不可代码调用的返回 None，交回模型（schema 已裁剪）。"""
    resolver = _CODE_INVOKED_TOOLS.get(tool)
    if resolver is None:
        return None
    args = resolver(question)
    if args is None:
        # 参数抽不出唯一解。宁可交回模型，也不拿猜出来的参数去查精确工具
        logger.info("偏好工具 %s 的参数无法从问题唯一确定，交回模型自选", tool)
        return None
    tr = REGISTRY.call(tool, json.dumps(args, ensure_ascii=False))
    result.tool_calls.append(_tool_record(tool, args, tr))
    if not tr.ok:
        logger.warning("偏好工具 %s 代码调用失败：%s", tool, tr.error)
    return tr


def _first_round_tools(
    tool: str, tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """第一轮只offer偏好工具本身。

    网关忽略 `tool_choice`，所以「强制」只能靠**把别的选项拿掉**：强制检索已经
    由代码跑过 `kb_search`，模型这一轮没有理由再退回它。裁剪只作用于第一轮，
    后续轮次放开——工具结果可能说明需要换工具，锁死会让它无路可走。
    """
    if not tool:
        return tools
    subset = [
        s for s in tools
        if str((s.get("function") or {}).get("name") or "") == tool
    ]
    return subset or tools


def _precision_tool_route(question: str) -> str:
    """只为参数可从问题逐字冻结的结构化能力放行精确工具。"""
    if _positive_craft_clause(question):
        return "check_mod_combination"
    metamod_query = _metamod_precision_query(question)
    if metamod_query[0]:
        return "metamod_interaction_matrix"
    if _affix_pool_precision_query(question)[0]:
        return "affix_pool_odds"
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

    if tool_name == "metamod_interaction_matrix":
        clause, expected_operation, expected_guard = _metamod_precision_query(question)
        if not clause or not expected_operation or not expected_guard:
            return "当前问题没有唯一可逐字冻结的 Metamod 矩阵键"
        if set(arguments) != {"operation", "guard"}:
            return "Metamod 矩阵调用必须且只能包含 operation 与 guard"
        operation = str(arguments.get("operation") or "").strip()
        guard = str(arguments.get("guard") or "").strip()
        if operation != expected_operation:
            return "operation 必须逐字对应来源矩阵中的操作名"
        if guard != expected_guard:
            return "guard 必须逐字对应来源矩阵中的列名"
        return ""

    if tool_name == "affix_pool_odds":
        (
            clause,
            expected_base,
            expected_level,
            expected_side,
            expected_target,
        ) = _affix_pool_precision_query(question)
        if not clause or expected_level is None:
            return "当前问题没有可逐字冻结的普通词缀条件池查询"
        if set(arguments) != {
            "base_type", "item_level", "generation_side", "target_query"
        }:
            return (
                "条件池调用必须且只能包含 base_type、item_level、"
                "generation_side 与 target_query"
            )
        if str(arguments.get("base_type") or "").strip() != expected_base:
            return "base_type 必须逐字对应问题中引号内的底材"
        item_level = arguments.get("item_level")
        if isinstance(item_level, bool) or item_level != expected_level:
            return "item_level 必须等于问题中明确给出的物品等级整数"
        if str(arguments.get("generation_side") or "") != expected_side:
            return "generation_side 必须对应问题中唯一的前缀或后缀侧"
        if str(arguments.get("target_query") or "").strip() != expected_target:
            return "target_query 必须逐字对应问题中引号内的目标词缀"
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
    if tool_name == "metamod_interaction_matrix":
        return [
            citation
            for citation in result.citations
            if str(citation.get("chunk_id") or "").startswith("metamod_interaction:")
            and citation.get("channel") == "structured"
            and citation.get("claim_scope") == "metamod_matrix"
            and isinstance(citation.get("claim_data"), dict)
            and citation.get("claim_data", {}).get("operation_text")
            and citation.get("claim_data", {}).get("guard_text")
            and citation.get("claim_data", {}).get("raw_value")
            and citation.get("url")
        ]
    if tool_name == "affix_pool_odds":
        if not isinstance(result.data, dict) or result.data.get("status") != "exact":
            return []
        pob_citations = [
            citation
            for citation in result.citations
            if str(citation.get("chunk_id") or "").startswith("affix_pool_pob_base:")
            and citation.get("channel") == "structured"
            and citation.get("claim_scope") == "affix_pool_odds"
            and citation.get("source") == "Path of Building"
            and citation.get("source_priority") == 2
            and isinstance(citation.get("claim_data"), dict)
            and citation.get("claim_data", {}).get("kind") == "pob_base"
            and all(
                citation.get("claim_data", {}).get(key)
                for key in (
                    "base_id", "name_en", "armour_tag", "source_commit", "pob_url",
                )
            )
            and citation.get("url") == citation.get("claim_data", {}).get("pob_url")
            and str(citation.get("claim_data", {}).get("source_commit"))
            in str(citation.get("url") or "")
        ]
        mapping_citations = [
            citation
            for citation in result.citations
            if str(citation.get("chunk_id") or "").startswith("affix_pool_base_mapping:")
            and citation.get("channel") == "derived"
            and citation.get("claim_scope") == "affix_pool_odds"
            and citation.get("source") == "POEDB"
            and citation.get("source_priority") == 1
            and isinstance(citation.get("claim_data"), dict)
            and citation.get("claim_data", {}).get("kind") == "base_mapping"
            and all(
                citation.get("claim_data", {}).get(key)
                for key in (
                    "mapping_version", "base_id", "name_en", "name_zh", "poedb_slug",
                    "armour_tag", "modifier_page", "pob_url", "poedb_url",
                )
            )
            and citation.get("url") == citation.get("claim_data", {}).get("poedb_url")
        ]
        pool_citations = [
            citation
            for citation in result.citations
            if str(citation.get("chunk_id") or "").startswith("affix_pool:")
            and citation.get("channel") == "derived"
            and citation.get("claim_scope") == "affix_pool_odds"
            and citation.get("source") == "POEDB"
            and citation.get("source_priority") == 1
            and isinstance(citation.get("claim_data"), dict)
            and citation.get("claim_data", {}).get("kind") == "pool_odds"
            and all(
                citation.get("claim_data", {}).get(key) is not None
                for key in (
                    "base_id", "base_name_en", "base_name_zh", "armour_tag",
                    "modifier_page", "poedb_base_url", "pob_url", "item_level",
                    "generation_side", "generation_side_zh", "pool_size",
                    "total_weight", "target_query", "target_group", "target_weight",
                    "probability_numerator", "probability_denominator",
                    "probability_decimal", "expected_independent_selections",
                    "pool_digest", "page_row_count", "page_row_digest",
                    "source_content_hash", "source_fetched_at", "source_total_rows",
                    "source_deduped_rows",
                )
            )
            and citation.get("claim_data", {}).get("source_category") == "normal"
            and citation.get("claim_data", {}).get(
                "existing_family_exclusions_applied"
            ) is False
            and citation.get("url")
        ]
        if not (
            len(pob_citations) == len(mapping_citations) == len(pool_citations) == 1
        ):
            return []
        pob = pob_citations[0]
        mapping = mapping_citations[0]
        pool = pool_citations[0]
        p2 = pob["claim_data"]
        bridge = mapping["claim_data"]
        odds = pool["claim_data"]
        if not (
            p2["base_id"] == bridge["base_id"] == odds["base_id"]
            and p2["name_en"] == bridge["name_en"] == odds["base_name_en"]
            and bridge["name_zh"] == odds["base_name_zh"]
            and p2["armour_tag"] == bridge["armour_tag"] == odds["armour_tag"]
            and bridge["modifier_page"] == odds["modifier_page"]
            and p2["pob_url"] == bridge["pob_url"] == odds["pob_url"]
            and bridge["poedb_url"] == odds["poedb_base_url"]
            and pool["url"] == f"https://poedb.tw/cn/{odds['modifier_page']}"
        ):
            return []
        return [pob, mapping, pool]
    return []


def _call_tool(
    question: str,
    precision_tool: str,
    name: str,
    raw_arguments: str,
) -> tuple[ToolResult, bool]:
    """执行工具，并返回它是否是参数归属正确且证据完整的精确调用。"""
    if (
        name == "metamod_interaction_matrix"
        and _metamod_source_policy(question) == "excluded"
    ):
        return ToolResult(
            ok=False,
            error="当前问题明确排除了 Metamod 矩阵来源，拒绝执行该工具",
        ), False
    if name == "affix_pool_odds" and precision_tool != "affix_pool_odds":
        return ToolResult(
            ok=False,
            error=(
                "当前问题未形成可逐字冻结的普通词缀条件池查询，"
                "拒绝执行 affix_pool_odds"
            ),
        ), False
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
    elif precision_tool == "metamod_interaction_matrix":
        instruction = (
            "工具参数中的 operation 与 guard 必须逐字对应用户问题和来源矩阵键。"
            "回答只能使用固定句式复述原始结果与原始注释；不得解释 YES/NO 的外延，"
            "不得声称当前版本适用，也不得扩写可执行性、状态转移、步骤、概率、成本或路线。"
        )
    elif precision_tool == "affix_pool_odds":
        instruction = (
            "工具参数必须逐字使用问题中引号内的底材与目标，并使用问题明确给出的"
            "物品等级和唯一前缀/后缀侧。回答只能按固定句式陈述：PoB 英文底材/标签"
            "证据与 POEDB 身份桥接共同支撑的底材到词缀页映射；normal 来源、该物品"
            "等级、该侧且未应用已有 family 排除时的完整池总权重、目标组权重与条件"
            "概率；以及独立同分布选择数学期望。映射句必须同时引用 PoB 与桥接证据，"
            "权重和期望句必须同时引用 PoB、桥接与 POEDB 池证据。必须声明该条件概率"
            "不是任何通货或具体工艺的一次使用成功率；不得扩写当前装备、容量、特殊"
            "来源、步骤、实际期望通货、成本或路线。"
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


def _metamod_source_blank_answer(result: ToolResult) -> str:
    """空白源格的确定性拒答；不让 LLM 把空白解释为 N/A。"""
    data = result.data or {}
    operation = str(data.get("operation") or "")
    guard = str(data.get("guard") or "")
    return (
        f"来源页面中“{operation}”与“{guard}”对应的单元格为空，"
        "没有可引用的明确结果；因此不能推断为 N/A、YES、NO 或其它交互结论。"
    )


def _affix_pool_unknown_answer(result: ToolResult) -> str:
    """条件池 unknown 的确定性缺据；禁止把未命中或歧义改写成概率。"""
    data = result.data or {}
    gaps = data.get("gaps") if isinstance(data.get("gaps"), list) else []
    messages = [
        str(gap.get("message") or "").strip()
        for gap in gaps
        if isinstance(gap, dict) and str(gap.get("message") or "").strip()
    ]
    detail = "；".join(messages) or "工具没有形成完整、唯一的条件池证据"
    candidates = data.get("candidate_groups")
    candidate_names = [
        str(candidate.get("correct_group") or "").strip()
        for candidate in candidates if isinstance(candidate, dict)
    ] if isinstance(candidates, list) else []
    disambiguation = ""
    if candidate_names:
        disambiguation = (
            " 候选 correct_group 为：" + "、".join(candidate_names)
            + "；请从中选一个作为逐字目标后重试。"
        )
    return (
        f"在当前窄化条件下无法输出下一次同侧普通加权选择的条件概率：{detail}。"
        f"{disambiguation}未形成概率时不能解释为 0%，也不能外推为任何通货或具体工艺的成功率。"
    )


def _precision_failure_answer(tool_name: str) -> str:
    if tool_name == "check_mod_combination":
        action = "因此不作组合判断"
        evidence_kind = "确定性求解证据"
    elif tool_name == "metamod_interaction_matrix":
        action = "因此不陈述该矩阵单元格的结果"
        evidence_kind = "逐字键匹配的结构化矩阵证据"
    elif tool_name == "affix_pool_odds":
        action = "因此不输出条件权重概率"
        evidence_kind = "完整的底材映射与同侧普通词缀池证据"
    else:
        action = "因此不陈述工艺台目录事实"
        evidence_kind = "对应的结构化目录证据"
    return (
        f"已识别为需要精确工具 `{tool_name}` 的问题，但本轮没有形成唯一、"
        f"参数与当前问题一致且带{evidence_kind}的调用；{action}。"
    )


# ── 两条路径共享的流水线 ──────────────────────────────────
#
# `run_turn` 与 `stream_turn` 曾各写一遍这条流水线（各约 300 行）。代价是实测出来的：
# 表格继承那次改动让流式与批量判定从 32/32 掉到 18/32；更糟的是**结构性漏改**——
# 「偏好工具零证据兜底」只写在批量路径里，流式下同样的问题会带着零条证据进闸门、
# 每个事实句被拦，而批量那段注释本身就写着这种结果「比一句查不到更糟」。
#
# 所以准备阶段、单次工具调用、确定性收尾三段都提到这里，**两条路径只允许在
# 「怎么把文本交给用户」上不同**：批量一次性 `verify()`，流式逐句 `StreamingGate`。


@dataclass
class _TurnSetup:
    """准备阶段的产物。`gap` 或 `final` 非空表示这一轮在进生成前就定了。

    两者的区别不是措辞而是**语义**：`gap` 是「无据不答」，`final` 是已经答完了
    （覆盖声明这类代码生成的确定性答案）。流式路径要据此决定发 `gap` 事件还是
    `sentence` 事件，所以不能合成一个字段。
    """

    evidence: list[Evidence] = field(default_factory=list)
    precision_tool: str = ""
    preferred_tool: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    needs_model_call: bool = False
    #: 检索侧的 `missing_terms`，偏好工具兜底时要原样带进缺据声明
    missing_terms: list[str] = field(default_factory=list)
    gap: str = ""
    final: str = ""

    @property
    def done(self) -> bool:
        return bool(self.gap or self.final)


def _prepare_turn(question: str, result: TurnResult) -> _TurnSetup:
    """POE2 边界 → 强制检索 → 路由 → 维度闸门 → 编号证据 → 代码调偏好工具。

    只写 `result`，不落库、不发事件——收尾动作由调用方按自己的形态做。
    """
    marker = poe2_marker(question)
    if marker:
        # 放在检索**之前**：越界问题不该消耗检索，更不该产生任何可能被当作依据的证据
        result.evidence_gap = True
        result.retrieval_meta["out_of_scope"] = marker
        _record_gaps(
            question, [f"越界：《流放之路 2》问题（标记「{marker}」），本库只覆盖 POE1"],
            category="out_of_scope/poe2", kind="retrieval_gap",
        )
        return _TurnSetup(gap=_out_of_scope_answer(question, marker))

    # 强制检索。不让模型决定「要不要查」
    first = REGISTRY.call("kb_search", json.dumps({"query": question}, ensure_ascii=False))
    result.tool_calls.append(_tool_record("kb_search", {"query": question}, first))
    result.retrieval_meta = dict(first.meta)
    if not first.ok:
        result.evidence_gap = True
        return _TurnSetup(
            gap=f"检索不可用（{first.error}），无法确认依据，本轮不作答。"
        )

    data = first.data or {}
    precision_tool = _precision_tool_route(question)
    # 代码决定的偏好工具（见 `_preferred_tool_route`）。它和精确路由一样要**豁免
    # 检索不足**：这几类问题的证据本来就不在 chunk 里——「你的库覆盖哪些领域」
    # 由 kb_coverage 回答、「某属性从哪来」由 stat_sources 逐渠道枚举，
    # BM25 命不中是正常的。不豁免的话整轮在进生成前就被判缺据，工具根本没机会调。
    # 兜底在 `_terminal_gap`：跑完若一条证据都没拿到，仍然走缺据声明。
    preferred_tool = "" if precision_tool else _preferred_tool_route(question)
    if preferred_tool:
        result.retrieval_meta["preferred_tool"] = preferred_tool
    gate_refused = False
    if precision_tool:
        result.retrieval_meta["precision_route"] = precision_tool
    else:
        # 精确路由不过这道闸：那类问题的维度由工具自己的缺据字段负责声明。
        # 其余情况**无论检索是否充分都先问覆盖层**——「这一维有没有数据」与
        # 「这次 BM25 有没有命中」是两个问题，先前只在充分时问，
        # 于是偏好工具那条路上它从没被咨询过。
        verdict = _dimension_verdict(question, first.citations)
        gate_refused = bool(verdict is not None and verdict.is_gap)
        if gate_refused:
            result.retrieval_meta["dimension_gate"] = True
            data = dict(data)
            data["sufficient"] = False
            data["reason"] = verdict.reason()
            data["missing_terms"] = verdict.missing_points()
            data["dimension_gate"] = True
            logger.info("维度覆盖闸门改判为依据不足：%s", verdict.reason())

    # 偏好工具**只豁免「BM25 命不中」，不豁免维度闸门**。
    # 两者是不同的判断：前者说「证据不在 chunk 里」（那正是工具要去查的），
    # 后者说「这一维根本没有数据」——工具也造不出没有的数据。
    #
    # 实测（2026-08-19 第十八轮）：「大星团珠宝的斧剑伤害能开出哪些核心天赋」这个映射
    # 本库没有，维度闸门判了缺据，但「星团」命中偏好工具后整轮被豁免、进了模型，
    # 结果模型写了 12 句、被出口闸门拦掉 11 句——用户看到一篇拦截说明。
    # 该拦的地方是入口，不是出口。
    if not data.get("sufficient") and not precision_tool and (
        not preferred_tool or gate_refused
    ):
        # 依据不足且没有确定性精确工具路由 → 由代码生成缺据声明，模型完全不参与
        result.evidence_gap = True
        answer, gap_category = _gap_answer(question, data)
        _record_gaps(
            question, [str(data.get("reason") or "依据不足")], category=gap_category
        )
        return _TurnSetup(gap=answer)

    # BM25 不充分时不能把「最不差」的命中当证据；精确路由只给模型调用工具的机会。
    evidence = number_evidence(
        [] if precision_tool else (first.citations if data.get("sufficient") else [])
    )
    result.evidence = evidence

    # 参数可在代码里推导的偏好工具直接由代码调用（见 `_CODE_INVOKED_TOOLS`）。
    # 网关忽略 tool_choice，交给模型「请调用」是不可靠的。
    preferred_result = _invoke_preferred_tool(question, preferred_tool, result)
    if preferred_result is not None and preferred_result.ok and preferred_result.citations:
        evidence = _merge_evidence(evidence, preferred_result.citations)
        result.evidence = evidence
    if first.meta.get("degraded"):
        result.degraded_note = (
            f"检索以降级通道完成（{', '.join(first.meta.get('channels') or [])}）："
            f"{first.meta.get('note') or '向量与重排未启用'}"
        )

    setup = _TurnSetup(
        evidence=evidence,
        precision_tool=precision_tool,
        preferred_tool=preferred_tool,
        tools=_tools_for_question(question),
        needs_model_call=bool(preferred_tool) and preferred_result is None,
        missing_terms=list(data.get("missing_terms") or []),
    )
    # 覆盖声明走代码生成路径，直接答完（理由见 `_coverage_answer`）
    if (
        preferred_tool == "kb_coverage"
        and preferred_result is not None
        and preferred_result.ok
    ):
        setup.final = _coverage_answer(preferred_result)
        return setup

    # 偏好工具已由代码调过、却一条证据都没给出，且检索侧也没有 → **不进生成**。
    # 这一步以前在生成之后（`_terminal_gap` 里那段兜底），等于明知无据还先烧一次
    # 模型调用、再把整篇答案拦掉。判据在进生成前就成立，就该在这里判：
    # 缺据声明由代码生成、完全可预测，也不花 token。
    #
    # 只在**代码已调过**（`preferred_result is not None`）时才提前判。模型自己去调
    # 那个工具的情形（`needs_model_call`）必须给它机会，兜底仍留在 `_terminal_gap`。
    if (
        preferred_tool
        and preferred_result is not None
        and not evidence
        and preferred_tool not in _TOOLS_WITHOUT_CITATIONS
    ):
        result.evidence_gap = True
        setup.gap = _preferred_tool_gap(question, preferred_tool, setup.missing_terms)
        return setup

    setup.messages = [
        {"role": "system", "content": build_system_prompt()},
        {
            "role": "user",
            "content": _initial_user_content(question, evidence, precision_tool),
        },
    ]
    return setup


@dataclass
class _ToolLoopState:
    """工具循环里跨轮累积的状态。

    单独成类是因为这些字段的更新规则（尤其精确工具的三种失败形态）必须两条路径
    完全一致——它们决定的是「要不要拒答」，分叉会直接变成漏出。
    """

    question: str
    precision_tool: str
    evidence: list[Evidence]
    succeeded: bool
    invalidated: bool = False
    source_blank: ToolResult | None = None
    affix_unknown: ToolResult | None = None
    final_results: dict[str, ToolResult] = field(default_factory=dict)

    @classmethod
    def start(cls, setup: _TurnSetup, question: str) -> _ToolLoopState:
        return cls(
            question=question,
            precision_tool=setup.precision_tool,
            evidence=setup.evidence,
            succeeded=not bool(setup.precision_tool),
        )


def _handle_tool_call(state: _ToolLoopState, result: TurnResult, tc: Any) -> dict[str, Any]:
    """执行一次模型请求的工具调用，更新状态与证据，返回要回灌给模型的 tool 消息。

    返回值里带 `evidence_changed`，让流式路径知道要不要 `gate.add_evidence()`；
    批量路径忽略它即可。
    """
    precision = state.precision_tool
    if precision and tc.name == precision and state.succeeded:
        state.invalidated = True
        tr = ToolResult(
            ok=False, error="精确工具已成功完成；本轮再次改参会使证据归属不唯一"
        )
        precision_match = False
    else:
        tr, precision_match = _call_tool(state.question, precision, tc.name, tc.arguments)

    data = tr.data or {}
    if precision == "metamod_interaction_matrix" and tc.name == precision and tr.ok \
            and bool(data.get("source_blank")):
        state.source_blank = tr
    if precision == "affix_pool_odds" and tc.name == precision and tr.ok \
            and data.get("status") == "unknown":
        state.affix_unknown = tr

    result.tool_calls.append(_tool_record(tc.name, _safe_json(tc.arguments), tr))
    state.final_results[tc.name] = tr

    evidence_changed = False
    if precision_match:
        state.affix_unknown = None
        state.succeeded = True
        # 精确回答只允许使用本次、参数归属正确且类型匹配的证据。
        state.evidence = number_evidence(_precision_citations(precision, tr))
        result.evidence = state.evidence
        evidence_changed = True
    elif not precision and tr.citations:
        state.evidence = _merge_evidence(state.evidence, tr.citations)
        result.evidence = state.evidence
        evidence_changed = True

    return {
        "message": {"role": "tool", "tool_call_id": tc.id, "content": tr.for_model()},
        "evidence_changed": evidence_changed,
    }


def _assistant_tool_message(tool_calls: list[Any], content: str | None = None) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.name, "arguments": tc.arguments}}
            for tc in tool_calls
        ],
    }


def _evidence_refresh_message(evidence: list[Evidence]) -> dict[str, Any]:
    """证据集变了就把最新编号重新给模型，否则它会引用旧编号。"""
    return {
        "role": "user",
        "content": "当前可用证据（编号已更新，请据此标注）：\n\n"
                   + render_evidence_block(evidence),
    }


def _terminal_gap(state: _ToolLoopState, setup: _TurnSetup) -> str:
    """生成之后、闸门之前的四种确定性拒答。返回空串表示可以正常出答案。

    顺序有意义：空白源格与 unknown 是**有明确来源依据**的拒答，必须优先于
    「没调成功」这条泛化判据，否则用户拿到的理由不是真实原因。
    """
    question = state.question
    if state.source_blank is not None:
        _record_gaps(
            question,
            [str((state.source_blank.data or {}).get("missing_reason") or "来源矩阵单元格为空")],
            category="制作系统 / metamod_matrix",
        )
        return _metamod_source_blank_answer(state.source_blank)

    if state.affix_unknown is not None and not state.succeeded and not state.invalidated:
        _record_final_tool_result_gaps(question, state.final_results)
        return _affix_pool_unknown_answer(state.affix_unknown)

    if not state.succeeded or state.invalidated:
        return _precision_failure_answer(state.precision_tool)

    # 偏好工具豁免了「检索不足」，模型自己去调那个工具却什么也没拿到时兜在这里。
    # 代码已调过工具的情形在 `_prepare_turn` 里就判掉了，不会走到这。
    # 不兜的话答案会带着零条证据进闸门，每个事实句都被拦，用户看到一篇拦截说明——
    # 那比一句「查不到」更糟，而且掩盖了「本轮其实无据」这个事实。
    preferred = setup.preferred_tool
    if preferred and not state.evidence and preferred not in _TOOLS_WITHOUT_CITATIONS:
        return _preferred_tool_gap(question, preferred, setup.missing_terms)

    return ""


def _preferred_tool_gap(question: str, preferred: str, missing_terms: list[str]) -> str:
    """偏好工具零证据的缺据声明。进生成前后各有一个判点，措辞必须同一处产生。"""
    answer, gap_category = _gap_answer(question, {
        "reason": (
            f"已按问题类型改由精确工具 `{preferred}` 回答，"
            "但该工具本轮未返回任何可引用证据"
        ),
        "missing_terms": list(missing_terms),
    })
    _record_gaps(
        question, [f"[{preferred}] 精确工具未返回可引用证据"],
        category=gap_category, kind="tool_gap",
    )
    return answer


def _apply_notes(result: TurnResult, evidence: list[Evidence]) -> list[str]:
    """把代码生成的附注追加到答案，并返回它们（流式还要逐条发事件）。

    附注放在闸门**之后**：它们不是模型说的，不需要也不该过绑定校验（见 `_answer_notes`）。
    """
    notes = _answer_notes(result.tool_calls, evidence)
    for note in notes:
        result.answer += f"\n\n> {note}"
    return notes


def _end(result: TurnResult, conversation_id: str | None, started: float) -> TurnResult:
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    _persist(result, conversation_id)
    return result


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

    setup = _prepare_turn(question, result)
    if setup.done:
        result.answer = setup.gap or setup.final
        return _end(result, conversation_id, started)

    budget = TurnBudget(limit=int(cfg.get("llm.max_tokens_per_turn", 60000)))
    max_calls = int(cfg.get("llm.max_tool_calls", 12))
    messages = setup.messages
    state = _ToolLoopState.start(setup, question)
    answer_text = ""

    try:
        for round_no in range(max_calls + 1):
            # 第一轮裁剪 schema 代替 tool_choice（网关忽略后者，实测见 _first_round_tools）
            round_tools = (
                _first_round_tools(setup.preferred_tool, setup.tools)
                if round_no == 0 and setup.needs_model_call else setup.tools
            )
            resp = client.chat(
                messages,
                tools=round_tools,
                budget=budget,
                allow_empty_content=True,  # 纯 tool_call 回复是合法的
            )
            result.model = resp.model
            if not resp.tool_calls:
                answer_text = resp.content
                break

            messages.append(_assistant_tool_message(resp.tool_calls, resp.content))
            for tc in resp.tool_calls:
                messages.append(_handle_tool_call(state, result, tc)["message"])
            messages.append(_evidence_refresh_message(state.evidence))
        else:
            logger.warning("工具调用达到上限 %d，用现有内容作答", max_calls)
    except LLMError as exc:
        # 必须记下具体原因：只报异常类型的话，线上出问题时无从下手。
        # 消息已由 core/llm.py 脱敏，可安全写日志。
        logger.error("生成失败（trace=%s）：%s", trace_id, exc)
        result.answer = f"生成失败（{type(exc).__name__}），本轮不作答以免输出无据内容。"
        return _end(result, conversation_id, started)

    gap = _terminal_gap(state, setup)
    if gap:
        result.evidence_gap = True
        result.answer = gap
        return _end(result, conversation_id, started)

    evidence = state.evidence
    _record_final_tool_result_gaps(question, state.final_results)
    result.tokens = {
        "prompt": budget.used.prompt_tokens,
        "output": budget.used.completion_tokens,
        "reasoning": budget.used.reasoning_tokens,
        "total": budget.used.total_tokens,
    }

    # ── 出口闸门 ──
    result.raw_answer = answer_text
    binding = verify(
        answer_text,
        evidence,
        factual_terms=list(cfg.get("retrieval.factual_terms", []) or []),
        domain_terms=_domain_terms(),
    )
    result.binding = binding
    result.answer = binding.text
    result.citations = cit.collect(evidence, binding.used_labels)
    _apply_notes(result, evidence)
    if result.degraded_note:
        result.answer += f"\n\n> {result.degraded_note}"

    if binding.blocked:
        logger.warning(
            "绑定校验拦截 %d 句（trace=%s）：%s",
            binding.blocked, trace_id,
            "; ".join(v.reason for v in binding.verdicts if not v.ok)[:300],
        )
        _record_binding_gaps(question, binding)

    return _end(result, conversation_id, started)


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

    # 准备阶段与非流式**同一个函数**（`_prepare_turn`）：POE2 边界、强制检索、路由、
    # 维度闸门、代码调偏好工具。这里只负责把它的结论翻译成事件。
    setup = _prepare_turn(question, result)
    if setup.gap:
        result.answer = setup.gap
        yield {"type": "gap", "text": result.answer}
        yield from _finish(result, conversation_id, started)
        return
    if setup.final:
        result.answer = setup.final
        yield {"type": "sentence", "text": result.answer,
               "kind": "narrative", "labels": []}
        yield from _finish(result, conversation_id, started)
        return

    budget = TurnBudget(limit=int(cfg.get("llm.max_tokens_per_turn", 60000)))
    max_calls = int(cfg.get("llm.max_tool_calls", 12))
    messages = setup.messages
    state = _ToolLoopState.start(setup, question)

    gate = StreamingGate(
        setup.evidence,
        factual_terms=list(cfg.get("retrieval.factual_terms", []) or []),
        domain_terms=_domain_terms(),
    )
    announced_thinking = False

    try:
        for _round in range(max_calls + 1):
            pending: list[Any] = []
            round_content: list[str] = []
            round_tools = (
                _first_round_tools(setup.preferred_tool, setup.tools)
                if _round == 0 and setup.needs_model_call else setup.tools
            )
            for event in client.stream(messages, tools=round_tools, budget=budget):
                if event.kind == "thinking":
                    if not announced_thinking:
                        announced_thinking = True
                        yield {"type": "thinking"}
                elif event.kind == "content":
                    if setup.precision_tool:
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
                if state.succeeded and not state.invalidated:
                    for piece in round_content:
                        for verdict in gate.feed(piece):
                            yield _sentence_event(verdict)
                break

            # 模型要调工具：执行后开新的一轮流
            messages.append(_assistant_tool_message(pending))
            for tc in pending:
                yield {"type": "tool", "name": tc.name}
                handled = _handle_tool_call(state, result, tc)
                if handled["evidence_changed"]:
                    # 编号是追加式的，旧编号不变，所以只需把新证据补进闸门的映射
                    gate.add_evidence(state.evidence)
                messages.append(handled["message"])
            messages.append(_evidence_refresh_message(state.evidence))
        else:
            logger.warning("流式工具调用达到上限 %d", max_calls)
    except LLMError as exc:
        logger.error("流式生成失败（trace=%s）：%s", trace_id, exc)
        yield {"type": "error", "message": f"生成失败（{type(exc).__name__}）"}

    # 四种确定性拒答与非流式**同一个函数**（`_terminal_gap`），含偏好工具零证据兜底
    gap = _terminal_gap(state, setup)
    if gap:
        result.evidence_gap = True
        result.answer = gap
        yield {"type": "gap", "text": result.answer}
        yield from _finish(result, conversation_id, started)
        return

    evidence = state.evidence
    _record_final_tool_result_gaps(question, state.final_results)
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
    for note in _apply_notes(result, evidence):
        yield {"type": "note", "text": note}
    if result.degraded_note:
        yield {"type": "note", "text": result.degraded_note}
    if binding.blocked:
        logger.warning(
            "流式绑定校验拦截 %d 句（trace=%s）", binding.blocked, trace_id
        )
        _record_binding_gaps(question, binding)

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
    "target_ambiguous": "input_clarification/mod_affix_group",
    "base_not_resolved": "input_clarification/base_item",
    "pool_incomplete": "not_collected/mod_affix",
    "pool_snapshot_unverified": "not_collected/mod_affix_snapshot",
    "weight_incomplete": "not_collected/mod_affix_weight",
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
        _record_gaps(question, points, category=category, kind="tool_gap")


def _record_gaps(
    question: str,
    gaps: list[str],
    *,
    category: str = "",
    kind: str = "retrieval_gap",
) -> None:
    """写缺口台账。按频次排序即为采集优先级（需求 2.12）。

    `category` 来自覆盖归因（`not_collected/<域>` 或 `not_recalled/<域>`），
    它让台账能区分「该去采集」与「该改检索」——这是两类完全不同的待办。
    `kind` 见 `core.db.GAP_KINDS`：只有 `retrieval_gap` 与 `tool_gap` 构成采集优先级。

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
                                          kind, hit_count, first_seen, last_seen)
                VALUES (?,?,?,?,1,datetime('now'),datetime('now'))
                ON CONFLICT(question, missing_point) DO UPDATE SET
                    hit_count = hit_count + 1,
                    last_seen = datetime('now'),
                    kind = COALESCE(excluded.kind, knowledge_gap.kind),
                    guessed_category = COALESCE(excluded.guessed_category,
                                                knowledge_gap.guessed_category)
                """,
                (question, point[:500], category or None, kind or None),
            )
        conn.commit()
    except Exception as exc:  # noqa: BLE001  台账失败不该让整轮失败
        logger.warning("写缺口台账失败：%s", exc)


#: 归一化时丢掉的成分：绑定标记、Markdown 记号、纯数值。
#: 数值必须丢——同一个缺口每次回答取的具体值不同（T2 vs T3、47 vs 52），
#: 留着会让一个缺口散成 N 行。
_GAP_KEY_STRIP_RE = re.compile(r"\[\[[^\]]*\]\]|[*#`>|~_\-]+|\d+(?:\.\d+)?")


def _record_binding_gaps(question: str, binding: BindingResult) -> None:
    """把出口闸门的拦截写入台账 —— 但**只写真正是知识缺口的那些**。

    先前这里是 `_record_gaps(question, binding.gaps)`，直接把被拦句子的原文当作
    `missing_point`。两个后果：

      1. 排版元素与缺据声明句也被记成「缺口」。库里实测有
         `缺少可引用依据：---`、`缺少可引用依据：）`、`缺少可引用依据：**隐式词缀**`
         这类条目 —— 它们是闸门的分类误判，不是知识缺口。
      2. 同一个缺口每次措辞不同就多一行（句中数值一变就是新行），
         「按频次排序即采集优先级」因此失真。

    所以：排版元素直接不入账（它们该进的是「确定误拦」这个观测量，见
    `scripts/eval_run.py`）；其余按**实义词归一化**后入账，并带上覆盖归因，
    让台账仍然能回答「该去采集什么」。
    """
    from orchestrator.binding import is_structural  # noqa: PLC0415  同包内延迟导入

    category = ""
    try:
        from kb.coverage import attribute_gap  # noqa: PLC0415  延迟导入避免循环

        category = attribute_gap(dbmod.get_conn(), question).category_label()
    except Exception as exc:  # noqa: BLE001
        logger.debug("拦截归因失败，台账不带分类：%s", exc)

    points: list[str] = []
    for verdict in binding.verdicts:
        if verdict.ok:
            continue
        if is_structural(verdict.text):
            # 排版元素被拦是分类误判，不是知识缺口。记日志便于追，但不进台账。
            logger.info("闸门拦下排版元素（不入台账）：%r", verdict.text.strip()[:60])
            continue
        key = " ".join(_GAP_KEY_STRIP_RE.sub(" ", verdict.text).split())[:120]
        if not key:
            continue
        point = f"无据断言：{key}"
        if point not in points:
            points.append(point)
    _record_gaps(question, points, category=category, kind="binding_block")


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
