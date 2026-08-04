"""混合检索：实体精确通道 + BM25，RRF 融合 → 来源优先级加权 → 充分依据判定。

向量通道当前关闭（网关无 embedding 模型），接口已预留：一旦 embedding 可用，
在 `_gather` 里加一路候选即可，融合与判定逻辑无需改动。

**充分依据判定由代码执行，不交给模型自觉**（需求 1.3）：
最高分低于 min_score 即返回 evidence_gap，不进入生成。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from retrieval import alias as alias_mod
from retrieval import vocab as vocab_mod
from retrieval.fts import Hit, search_fts, term_coverage
from retrieval.tokenize import query_content_words

logger = logging.getLogger(__name__)

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
    "精华", "化石", "工艺台", "工艺工作台", "裂界", "腐化", "附魔",
)


#: 查询未点名词缀来源时默认偏向的来源（POEDB 的常规掉落池代码）。
DEFAULT_MOD_CATEGORY = "normal"

#: 来源偏好的加权系数。RRF 分在相邻名次间只差百分之几，需要一个明确的乘数
#: 才能把被点名 / 默认来源提到前面；同时保持足够小，不至于把覆盖率低的候选顶上来。
MOD_SOURCE_BOOST = 1.25

#: 同一 (底材类别, 词缀组) 在候选集里最多保留几张卡。见 `_diversify`。
MAX_PER_MOD_GROUP = 3

#: 召回倍率：每路通道取 top_k 的几倍作为候选，供限流筛选后再截断到 top_k。
RECALL_MULTIPLIER = 4


def _looks_like_mod_query(query: str) -> bool:
    """判断查询是否表现出词缀类意图。"""
    return any(term in query for term in _MOD_INTENT_TERMS)


def _prefer_mod_source(hits: list[Hit], query: str) -> None:
    """按词缀来源意图，**只在词缀卡自己占据的名次内**重排。

    问题：同一 (底材, 前后缀, 词缀组) 在多个来源下各有一张卡。查
    「弓上的火焰伤害前缀有哪些等级」时，精华 / 工艺工作台 / 异度天灾等版本
    与常规掉落池版本的文本高度重合，BM25 分只差千分之几，实测常规池那张
    排到第 10 位。生成层若照首条引用，会答成「弓的火焰伤害前缀来自精华」——
    机制上完全是另一回事，正是「一个小前提错就整条结论反向」的典型。

    规则（两条都由数据决定，不含手写来源清单）：
    1. 查询点名了来源（词表里 kind=mod_source 的词，如「精华」「工艺工作台」）
       → 偏向被点名的来源；
    2. 没点名 → 偏向常规掉落池。与 `entities/mod_lookup.py` 里
       normal > master > essence 的消歧顺序一致。

    **为什么必须限定在词缀卡的名次内**：早先直接把加权乘进全局 rrf 再整体重排，
    结果查「击败稀有怪物获得其属性修正的腰带」时，一张 43% 覆盖率的 Base 词缀卡
    把 100% 覆盖率的「猎首」挤到第二。来源偏好回答的是「同一词缀该看哪个来源」，
    它没有资格判断词缀卡与装备卡谁更相关。因此这里只做**槽位内重排**：
    词缀卡占据哪些名次不变，只调整它们彼此的先后，其他实体类型的位置分毫不动。

    也因此 `rrf` 不被改写——它保持纯融合分，最终排序由列表顺序表达。
    """
    wanted = {t for t in vocab_mod.scan_terms(query) if vocab_mod.kind_of(t) == "mod_source"}

    def preferred(h: Hit) -> bool:
        facets = h.metadata.get("facets") or {}
        if wanted:
            return facets.get("mod_source", "") in wanted
        return facets.get("mod_category") == DEFAULT_MOD_CATEGORY

    slots = [
        i for i, h in enumerate(hits)
        if (h.metadata.get("facets") or {}).get("mod_source")
    ]
    if len(slots) < 2:
        return

    mods = [hits[i] for i in slots]
    # 软偏好：仍以相关度为主，被偏好的来源乘一个系数。用硬性字典序会让一张
    # 相关度明显更低的 Base 卡压过高度相关的精华卡，那是另一种失真。
    mods.sort(
        key=lambda h: (
            -h.rrf * (MOD_SOURCE_BOOST if preferred(h) else 1.0),
            h.source_priority,
            -h.score,
        )
    )
    for slot, hit in zip(slots, mods, strict=True):
        hits[slot] = hit


def _diversify(hits: list[Hit], per_group: int = MAX_PER_MOD_GROUP) -> list[Hit]:
    """按 (底材类别, 词缀组) 限流，剔除近似重复的候选。

    为什么必须做：同一个词缀组在同一底材上，常规掉落池 / 工艺工作台 / 精华 /
    虚空忆境 / 异度天灾 有利 / 异度天灾 有害 …… 各有一张卡，文本高度雷同。
    实测「戒指的抗性词缀权重」的前 20 名被这类近重复占满，**常规掉落池的完整梯度
    根本没进候选集**——最该被引用的那张卡到不了生成层。这不是排名瑕疵，是召回失败。

    为什么用限流而不是加大来源加权：加权是在拿魔数拟合噪声（这些候选的 bm25
    只差 3%，实测 -10.42 ~ -10.12），而且治不了「候选集被同组挤满」这个根因。
    限流只**删除**低名次的近重复，不抬升任何候选，因此不会把低相关项顶上来，
    也不会撼动实体精确命中的位置。

    非词缀卡（无 facets）不受限流影响。
    """
    if per_group <= 0:
        return hits
    seen: dict[tuple[str, str], int] = {}
    out: list[Hit] = []
    for h in hits:
        facets = h.metadata.get("facets") or {}
        key = (facets.get("item_class_page", ""), facets.get("mod_group", ""))
        if not key[0] and not key[1]:
            out.append(h)
            continue
        n = seen.get(key, 0)
        if n >= per_group:
            continue
        seen[key] = n + 1
        out.append(h)
    return out


@dataclass
class RetrievalResult:
    """检索结果。`sufficient` 为 False 时上层必须走缺据声明路径。"""

    query: str
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
        hit = best[chunk_id]
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
    min_score: float = 0.35,
    min_coverage: float = 0.5,
    rrf_k: int = 60,
    priority_decay: float = 0.08,
    vector_enabled: bool = False,
    rerank_enabled: bool = False,
    category: str | None = None,
    league: str | None = None,
) -> RetrievalResult:
    result = RetrievalResult(query=query)
    channels: dict[str, list[Hit]] = {}

    # 查询实义词：所有通道的覆盖率都按同一套词计算，输出才可比
    q_terms = query_content_words(query)

    # ── 通道 1：实体精确匹配（最快最确定，放最前）──
    entity_ids = alias_mod.lookup(conn, query)
    if entity_ids:
        alias_hits = alias_mod.hits_for_entities(conn, entity_ids)
        if alias_hits:
            # alias 通道由查表命中，本身不经 FTS，覆盖率需在此补算，
            # 否则输出里正确答案显示 0% 而错误候选显示 100%，误导排查。
            for h in alias_hits:
                cov, matched, missing = term_coverage(q_terms, h.text)
                h.coverage, h.matched_terms, h.missing_terms = cov, matched, missing
            channels["alias"] = alias_hits
            result.channels.append("alias")

    # 召回阶段取比 top_k 更多的候选，留出被限流剔除的余量——否则同组近重复
    # 在进入融合前就把名额吃光，限流再怎么筛也变不出没召回到的卡。
    recall_k = top_k * RECALL_MULTIPLIER

    # ── 通道 2：BM25（唯一在所有平台确定可用的通道）──
    fts_hits = search_fts(conn, query, limit=recall_k, category=category, league=league)
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
    if _looks_like_mod_query(query):
        mod_hits = search_fts(
            conn, query, limit=recall_k,
            entity_prefix=MOD_LADDER_PREFIX, channel="mod_intent",
        )
        if mod_hits:
            channels["mod_intent"] = mod_hits
            result.channels.append("mod_intent")

    # ── 通道 4：向量（当前关闭）──
    if vector_enabled:
        result.channels.append("vector")
    else:
        result.degraded = True
        result.note = "向量检索与重排未启用（网关无 embedding 模型），当前走 BM25 + 实体精确匹配"

    if not rerank_enabled:
        result.degraded = True

    if not channels:
        result.sufficient = False
        result.reason = "检索无任何命中"
        return result

    fused = _rrf_fuse(channels, rrf_k, priority_decay)
    _prefer_mod_source(fused, query)
    # 先限流再截断：否则同组近重复会在截断前就把候选名额吃光
    result.hits = _diversify(fused)[:top_k]
    result.top_score = result.hits[0].score if result.hits else 0.0
    result.top_coverage = max((h.coverage for h in result.hits), default=0.0)

    # 未被任何候选覆盖的查询词：缺据声明要如实说明缺什么
    covered: set[str] = set()
    all_terms: list[str] = []
    for h in result.hits:
        covered.update(h.matched_terms)
        for t in h.missing_terms + h.matched_terms:
            if t not in all_terms:
                all_terms.append(t)
    result.missing_terms = [t for t in all_terms if t not in covered]

    # ── 充分依据判定：由代码执行，不交给模型（需求 1.3）──
    #
    # 判据是**查询词覆盖率**而非相对相关度。原因：相对归一化下最优项恒为 1.0，
    # 用它做阈值等于闸门永不触发；而 bm25 绝对值随查询长度与语料变化，
    # 越界查询与正常查询可以落在同一量级（实测 -8.97 vs -9.99），也无法区分。
    # 覆盖率是绝对量：查询的实义词有多少真的出现在文档里，可解释且可阈值化。
    exact_hit = any(h.channel == "alias" for h in result.hits)
    best = max(result.hits, key=lambda h: h.coverage) if result.hits else None
    matched_count = len(best.matched_terms) if best else 0

    if exact_hit:
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

    return result
