"""混合检索：实体精确通道 + BM25，RRF 融合 → 来源优先级加权 → 充分依据判定。

检索只使用本地词法与实体通道，不依赖模型或远程服务。

**充分依据判定由代码执行，不交给模型自觉**（需求 1.3）：
查询词覆盖率不足且无实体精确命中时返回 evidence_gap，不进入生成。

## 曾经有过、现已删除的两个补偿层

这里原先有 `_diversify()`（按 (底材类别, 词缀组) 限流，每组最多留 3 张卡）与
`_prefer_mod_source()`（在词缀卡占据的名次内按来源偏好重排）。两者都是在补偿
同一个根因：词缀卡的粒度是 (底材类别, **来源**, 前后缀, 词缀组)，于是同一个
词缀组在多个来源下有多张高度雷同的卡，把候选集挤满。

`kb/render_mods.py` 把粒度并成 (底材类别, 词缀组)、来源作为卡内分节之后，
同组已不存在近似重复，这两层补偿就没有对象了，一并删除。

记在这里是为了说明：**排序层反复加权、限流、调系数，往往说明索引的切分粒度
不对。** 先怀疑粒度，再改排序。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field, replace

from retrieval import alias as alias_mod
from retrieval.fts import Hit, absent_from_corpus, search_fts, term_coverage
from retrieval.tokenize import query_content_words
from retrieval.terminology import search_query

#: 构成充分依据所需的最少命中词数（在无实体精确命中的情况下）。
#: 单个词的共现不构成依据——一篇文档与查询只共用一个词，
#: 不意味着它回答了这个查询。见 retrieve() 里的判定说明。
MIN_MATCHED_TERMS = 2

#: 词缀梯度卡的 entity_id 前缀
MOD_LADDER_PREFIX = "mod_ladder:"

#: 词缀类意图的判别词。命中任一即额外跑一路只查词缀卡的召回。
#: 放在代码里而非索引里：写进索引会让全部卡片都含这些词导致 IDF 归零。
_MOD_INTENT_TERMS = (
    "词缀", "前缀", "后缀", "词缀组", "互斥",
    "T1", "T2", "T3", "T级", "阶级", "梯度", "档位",
    "物品等级", "掉落等级", "ilvl", "权重", "比重", "几率", "概率",
    # **「精华」必须写成「精华词缀」**（第五十九轮）：精华物品本身入库之后，
    # 这两个字成了 105 个实体的域名，裸词会让「哀恸精华是什么」也去跑一路
    # 只查词缀卡的召回，把该命中的精华卡挤到后面（实测 top1 是毫不相干的
    # `箭袋 · CloseRangeChain`）。与「命中率」从受击结算让给命中计算器、
    # 「降临」不能单独当 POE2 标记是同一条：**意图词必须专指**。
    # 化石 / 工艺台 / 裂界仍是裸词——它们至今只作为词缀来源存在，没有同名实体域。
    # **「精华」拆成了「精华词缀」+ 三个问句形式**（第五十九轮）：精华物品本身入库后，
    # 这两个字成了 105 个实体的域名，裸词会让「哀恸精华是什么」也去跑一路只查词缀卡的
    # 召回，把该命中的精华卡挤到后面（实测 top1 是毫不相干的 `箭袋 · CloseRangeChain`）。
    #
    # 但只删裸词会**丢掉另一半**：实测「用什么精华能打出冰霜抗性」随即从正确的
    # `腰带 · ColdResistance` 阶梯卡掉到一件唯一装备上——那是一道**枚举词缀来源**的题，
    # 本来就该走词缀通道。所以补回的是**问句形式**而不是话题词，
    # 与「枚举问句永远不是计算题」同一条判据。
    "精华词缀", "什么精华", "哪个精华", "哪些精华",
    "化石", "工艺台", "工艺工作台", "裂界", "腐化", "附魔",
)


#: 召回倍率：每路通道取 top_k 的几倍作为候选，再截断到 top_k。
#: 合并卡片粒度之后同组不再有近似重复，但保留余量仍有价值——
#: 覆盖率判定要在尽量完整的候选集上做。
RECALL_MULTIPLIER = 4


def _looks_like_mod_query(query: str) -> bool:
    """判断查询是否表现出词缀类意图。"""
    return any(term in query for term in _MOD_INTENT_TERMS)


#: 通用量词与疑问词。它们**永远不可能是专名**，因此不参与「全库为零即无依据」判定。
#:
#: 与 `_MOD_INTENT_TERMS` 分开写：那个是词缀领域的意图词（「档位」「权重」），
#: 这个是与领域无关的问法成分。只靠前者是打地鼠——修好「档位」之后，
#: 「腰带上的力量词缀**一共**有几档，**每档**要求什么物品等级」又因为
#: 「一共、每档」全库为零而被判无依据（实测）。
_GENERIC_ASK_RE = re.compile(
    r"^(?:一共|总共|合计|每[档级条个件种]?|各[档级条个件种]?|多少|几[档级条个件只种]?"
    r"|哪些|哪个|哪种|什么|怎么|如何|是否|能否|可否|分别|所有|全部|以及|还有"
    r"|区别|差异|不同|对比|比较)$"
)


def _drop_intent_terms(terms: list[str]) -> list[str]:
    """剔除意图词与通用问法成分，只留可能是问题**主体**的词。

    只给「未覆盖词是否全库为零」那道判据用。那道判据要抓的是**专名不存在**
    （「玄虚之靴」「流放之路2」），而意图词表达的是「想问哪类信息」，不是主体：

        「腰带上力量词缀的最高档位是多少」
          覆盖率 80% 达标、正确的梯度卡也召回了，却因为「档位」全库为零而拒答。
          库里写的是「T 级 / 阶级 / 梯度」，「档位」是用户口语——而它本来就已经
          登记在 `_MOD_INTENT_TERMS` 里。

    这类误伤属于错误拒答，与漏出同样要压（需求 16.7）。
    """
    return [
        t for t in terms
        if t not in _MOD_INTENT_TERMS and not _GENERIC_ASK_RE.match(t)
    ]


@dataclass
class RetrievalResult:
    """检索结果。`sufficient` 为 False 时上层必须走缺据声明路径。"""

    query: str
    search_query: str = ""
    hits: list[Hit] = field(default_factory=list)
    sufficient: bool = False
    #: 相对相关度最高分，**仅供排序参考，不作判据**
    top_score: float = 0.0
    #: 最佳查询词覆盖率，这才是充分依据的判据
    top_coverage: float = 0.0
    #: 未被任何候选覆盖的查询词——缺据声明里要如实列出「缺什么」
    missing_terms: list[str] = field(default_factory=list)
    #: 本轮实际生效的检索通道，供回答中说明降级状态（需求 3.11）
    channels: list[str] = field(default_factory=list)
    degraded: bool = False
    note: str = ""
    reason: str = ""


def _rrf_fuse(
    channels: dict[str, list[Hit]], k: int, priority_decay: float
) -> list[Hit]:
    """RRF 融合：只用排名不用分值，避开 bm25 负值与向量距离的量纲差异。

    融合后按 source_priority 施加衰减，使 POEDB 在同等相关度下排前——
    这是把「来源优先级」落在算法里，而不是写在提示词里。
    """
    scores: dict[str, float] = {}
    best: dict[str, Hit] = {}

    for channel_hits in channels.values():
        for rank, hit in enumerate(channel_hits, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
            # 同一 chunk 被多通道命中时，保留 score 更高的那份元数据
            if hit.chunk_id not in best or hit.score > best[hit.chunk_id].score:
                best[hit.chunk_id] = hit

    fused: list[Hit] = []
    for chunk_id, rrf in scores.items():
        hit = replace(best[chunk_id])
        # 优先级衰减：priority 1 不衰减，每级递减
        penalty = priority_decay * max(0, hit.source_priority - 1)
        hit.score = max(0.0, min(1.0, hit.score)) * (1.0 - min(penalty, 0.9))
        # 写入 rrf 而非覆盖 raw_score：raw_score 要保持是 bm25 原值，
        # 否则排查时看到的「bm25」其实是融合分，会把病因指向错的地方。
        hit.rrf = rrf
        fused.append(hit)

    # 先按 RRF 排，同分再按优先级与归一化分
    fused.sort(key=lambda h: (-h.rrf, h.source_priority, -h.score))
    return fused


def retrieve(
    conn: sqlite3.Connection,
    query: str,
    *,
    top_k: int = 20,
    min_coverage: float = 0.5,
    rrf_k: int = 60,
    priority_decay: float = 0.08,
    category: str | None = None,
    league: str | None = None,
    version: str | None = None,
    timeout_seconds: float | None = None,
) -> RetrievalResult:
    result = RetrievalResult(query=query, search_query=search_query(query))
    query = result.search_query
    channels: dict[str, list[Hit]] = {}
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    timed_out = False

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic()) if deadline is not None else float("inf")

    def mark_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        result.degraded = True
        if "检索超时" not in result.note:
            result.note += ("；" if result.note else "") + "检索超时，已保留完成的通道结果"

    # 查询实义词：所有通道的覆盖率都按同一套词计算，输出才可比
    q_terms = query_content_words(query)

    # ── 通道 1：实体精确匹配（最快最确定，放最前）──
    entity_ids = alias_mod.lookup(conn, result.query)
    if entity_ids:
        alias_hits = [
            hit for hit in alias_mod.hits_for_entities(conn, entity_ids)
            if (not category or hit.metadata.get("category") == category)
            and (not league or hit.metadata.get("league") == league)
            and (not version or hit.metadata.get("version") == version)
        ]
        if alias_hits:
            # alias 通道由查表命中，本身不经 FTS，覆盖率需在此补算，
            # 否则输出里正确答案显示 0% 而错误候选显示 100%，误导排查。
            for h in alias_hits:
                cov, matched, missing = term_coverage(q_terms, h.text)
                h.coverage, h.matched_terms, h.missing_terms = cov, matched, missing
            channels["alias"] = alias_hits
            result.channels.append("alias")

    # 每路多召回候选，再统一融合并截断。
    recall_k = top_k * RECALL_MULTIPLIER

    # ── 通道 2：BM25（唯一在所有平台确定可用的通道）──
    fts_hits = search_fts(
        conn, query, limit=recall_k, category=category, league=league,
        version=version,
    )
    if fts_hits:
        channels["fts"] = fts_hits
    result.channels.append("fts")

    # ── 通道 3：词缀意图通道 ──
    # 词缀梯度卡是长文档（平均 669 字，最长 14625），在同一池子里与短小的
    # 唯一装备卡竞争时会被 BM25 的长度归一压制。因此当查询表现出词缀类意图
    # 时，单独跑一路**只查词缀卡**的召回，让它们在自己的池子里排序，
    # 再交给 RRF 融合。
    #
    # 为什么不用「把领域词写进每张卡的 keywords」来解决：实测那样做会让
    # 全部 24418 张卡都含这些词，IDF 归零，反而失去区分度。
    if _looks_like_mod_query(query) and remaining() > 0:
        mod_hits = search_fts(
            conn, query, limit=recall_k,
            category=category, league=league, version=version,
            entity_prefix=MOD_LADDER_PREFIX, channel="mod_intent",
        )
        if mod_hits:
            channels["mod_intent"] = mod_hits
            result.channels.append("mod_intent")
    elif _looks_like_mod_query(query) and deadline is not None:
        mark_timeout()

    if not channels:
        if deadline is not None and remaining() <= 0:
            mark_timeout()
        result.sufficient = False
        result.reason = "检索超时，未获得可用结果" if timed_out else "检索无任何命中"
        return result

    fused = _rrf_fuse(channels, rrf_k, priority_decay)
    result.hits = fused[:top_k]
    result.top_score = result.hits[0].score if result.hits else 0.0
    # P7 community cards can help a user navigate, but cannot establish a
    # factual claim.  The gate must therefore evaluate only factual sources.
    factual_hits = [hit for hit in result.hits if hit.can_support_facts]
    result.top_coverage = max((h.coverage for h in factual_hits), default=0.0)

    # 未被任何候选覆盖的查询词：缺据声明要如实说明缺什么
    covered: set[str] = set()
    all_terms: list[str] = []
    for h in factual_hits:
        covered.update(h.matched_terms)
        for t in h.missing_terms + h.matched_terms:
            if t not in all_terms:
                all_terms.append(t)
    result.missing_terms = [t for t in all_terms if t not in covered]
    if not factual_hits:
        result.missing_terms = q_terms

    # ── 充分依据判定：由代码执行，不交给模型（需求 1.3）──
    #
    # 判据是**查询词覆盖率**而非相对相关度。原因：相对归一化下最优项恒为 1.0，
    # 用它做阈值等于闸门永不触发；而 bm25 绝对值随查询长度与语料变化，
    # 越界查询与正常查询可以落在同一量级（实测 -8.97 vs -9.99），也无法区分。
    # 覆盖率是绝对量：查询的实义词有多少真的出现在文档里，可解释且可阈值化。
    exact_hit = any(h.channel == "alias" for h in factual_hits)
    best = max(factual_hits, key=lambda h: h.coverage) if factual_hits else None
    matched_count = len(best.matched_terms) if best else 0

    if not factual_hits:
        result.sufficient = False
        result.reason = "仅命中不能支撑事实断言的社区资料，缺少可引用的事实来源"
    elif exact_hit:
        # 别名表命中意味着 Tier A 确实存在该实体条目，是最强证据
        result.sufficient = True
        result.reason = "实体精确命中"
    elif matched_count < MIN_MATCHED_TERMS:
        # **单个词的共现不构成充分依据。**
        # 实测：「无尽之衣」被切成 [无尽, 之衣]，「无尽呢喃」只命中「无尽」
        # 就拿到 50% 覆盖率踩线通过——但那是完全不同的物品。
        # 短查询（多为专名）分母小，比例判据不可靠，必须叠加绝对下限。
        # 专名查询的正确依据来自别名表（Tier A 精确层）；查不到就是库里没有。
        result.sufficient = False
        result.reason = (
            f"仅命中 {matched_count} 个查询词（下限 {MIN_MATCHED_TERMS}），"
            "且无实体精确命中；专名查询应由别名表命中，未命中即知识库无此条目"
        )
    elif result.top_coverage >= min_coverage:
        # 覆盖率达标还不够：**没被覆盖的词若全库都不存在**，命中的就只是通用词。
        #
        # 覆盖率给每个查询词等权，于是问题可以只靠通用词过线，漏掉的偏偏是主体词。
        # 实测「玄虚之靴有什么属性」靠 `之靴`+`属性` 拿到 67%，未覆盖的 `玄虚`
        # 恰是那个 POEDB 上不存在的名字；「流放之路2 的升华机制是什么」靠
        # `升华`+`机制` 过线，未覆盖的 `流放之路2` 恰是「问的是另一款游戏」的标志。
        # 一个词全库为零，答案就不可能在库里——此时其余词覆盖多少都不算依据。
        absent = (
            absent_from_corpus(conn, _drop_intent_terms(result.missing_terms))
            if not timed_out and remaining() > 0 else []
        )
        if absent:
            result.sufficient = False
            result.reason = (
                f"查询词覆盖率 {result.top_coverage:.0%} 虽达标，但"
                f"「{'、'.join(absent[:4])}」在知识库中完全不存在——"
                "命中的只是与问题主体无关的通用词，不构成依据"
            )
        else:
            result.sufficient = True
            result.reason = (
                f"查询词覆盖率 {result.top_coverage:.0%} ≥ {min_coverage:.0%}"
                f"（命中 {matched_count} 个词）"
            )
    else:
        result.sufficient = False
        result.reason = (
            f"查询词覆盖率仅 {result.top_coverage:.0%}（低于 {min_coverage:.0%}）"
            + (f"，未覆盖：{'、'.join(result.missing_terms[:6])}" if result.missing_terms else "")
        )

    if deadline is not None and remaining() <= 0:
        mark_timeout()
    if timed_out and not exact_hit:
        result.sufficient = False
        result.reason = "检索超时，候选结果可能不完整；请缩小查询或稍后重试"
    return result
