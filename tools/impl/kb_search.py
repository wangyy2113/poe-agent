"""知识库检索与实体查询工具。

`kb_search` 的关键设计：**把 `sufficient=False` 如实告诉模型**，而不是把
「最不差的几条」当证据递过去。BM25 永远能返回结果，但那不等于有依据；
掩盖这一点，模型就会拿不相关的证据编出一个像样的答案。
"""

from __future__ import annotations

import logging
from typing import Any

from core import db as dbmod
from core.config import get_config
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from retrieval.hybrid import retrieve
from retrieval.rerank import configured_rerank_client
from retrieval.runtime import configured_vector_runtime as vector_runtime_status
from tools.registry import REGISTRY, ToolResult

logger = logging.getLogger(__name__)

#: 单次检索返回给模型的证据条数上限。给多了会挤占上下文，
#: 也会让模型倾向于把弱相关证据也绑上去。
MAX_EVIDENCE = 8
#: 每条证据的截断长度。词缀梯度卡片可以很长，整段塞进去会爆上下文。
MAX_EVIDENCE_CHARS = 1200


@REGISTRY.register(
    name="kb_search",
    description=(
        "检索 POE1 知识库。任何关于游戏机制、数值、词缀、装备、制作的事实性问题，"
        "都必须先调用本工具，禁止凭记忆回答。\n"
        "返回的 sufficient 字段是关键：为 false 时说明知识库没有足够依据，"
        "此时**不得**基于返回内容作答，应输出缺据声明。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索词。专名（如「猎首」「Headhunter」）直接给，"
                "描述性问题给关键词组合（如「偷取 词缀 腰带」）",
            },
            # 枚举值**在运行时按实际有数据的分类填充**（见本模块末尾）。
            # 先前这里固定写着六个一级分类，而其中四类一条数据都没有——
            # 工具描述在对模型撒谎，模型按它过滤只会得到空结果。
            "category": {
                "type": "string",
                "description": "可选，按一级分类过滤。可用值见 enum",
                "enum": [],
            },
            "league": {"type": "string", "description": "可选，精确匹配资料的赛季标记"},
            "version": {"type": "string", "description": "可选，精确匹配资料的游戏版本"},
            "top_k": {"type": "integer", "description": "可选，返回条数，1-20"},
        },
        "required": ["query"],
    },
)
def kb_search(
    query: str, category: str | None = None, top_k: int | None = None,
    league: str | None = None, version: str | None = None,
) -> ToolResult:
    cfg = get_config()
    k = max(1, min(int(top_k or cfg.get("retrieval.top_k", 20)), 20))
    vector_requested = bool(cfg.get("retrieval.vector.enabled", False))
    conn = dbmod.get_conn()
    vector_runtime, embedding_client = vector_runtime_status(
        conn, vector_requested,
        timeout_seconds=float(cfg.get("retrieval.timeout_seconds", 8)),
    )
    rerank_requested = bool(cfg.get("retrieval.rerank.enabled", False))
    rerank_client, rerank_reason = configured_rerank_client(
        rerank_requested,
        timeout_seconds=float(cfg.get("retrieval.timeout_seconds", 8)),
    )
    result = retrieve(
        conn,
        query,
        top_k=k,
        min_score=float(cfg.get("retrieval.min_score", 0.35)),
        min_coverage=float(cfg.get("retrieval.min_coverage", 0.5)),
        rrf_k=int(cfg.get("retrieval.rrf_k", 60)),
        priority_decay=float(cfg.get("retrieval.priority_decay", 0.08)),
        vector_enabled=vector_runtime.available,
        vector_disabled_reason=(
            vector_runtime.reason
            if vector_requested and not vector_runtime.available
            else ""
        ),
        embedding_client=embedding_client,
        rerank_enabled=rerank_requested,
        rerank_client=rerank_client,
        rerank_disabled_reason=rerank_reason if rerank_requested else "",
        category=category or None,
        league=league or None,
        version=version or None,
        timeout_seconds=float(cfg.get("retrieval.timeout_seconds", 8)),
    )

    citations: list[dict[str, Any]] = []
    for h in result.hits[:MAX_EVIDENCE]:
        priority = int(h.source_priority)
        citations.append({
            "chunk_id": h.chunk_id,
            "entity_id": h.entity_id,
            "title": h.metadata.get("title", ""),
            "url": h.metadata.get("url", ""),
            "source": h.source,
            "source_priority": priority,
            # P7 社区来源不得支撑事实断言（需求 2.2）。这里就打上标记，
            # 出口校验据此拒绝用它绑定事实句。
            "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
            "text": h.text[:MAX_EVIDENCE_CHARS],
            "channel": h.channel,
        })

    return ToolResult(
        ok=True,
        data={
            "sufficient": result.sufficient,
            "reason": result.reason,
            "missing_terms": result.missing_terms,
            "evidence_count": len(citations),
        },
        citations=citations,
        meta={
            "channels": result.channels,
            "degraded": result.degraded,
            "top_coverage": round(result.top_coverage, 4),
            "note": result.note,
        },
    )


def sync_category_enum(conn: Any = None) -> list[str]:
    """把 `kb_search` 的 category 枚举同步为**实际有数据**的分类。

    为什么要在运行时改 schema 而不是写死：库里有什么分类取决于采集进度。写死六类
    的后果是模型拿一个空分类去过滤，得到 0 条证据，然后要么错误拒答、要么绕过证据
    自己编——两种都是我们要防的。

    找不到覆盖声明时**留空枚举**（等于不限制），而不是退回写死的六类：
    宁可少一个过滤维度，也不要给模型一份假的可选值。
    """
    try:
        from kb.coverage import usable_categories  # noqa: PLC0415  延迟导入避免循环

        cats = usable_categories(conn or dbmod.get_conn())
    except Exception as exc:  # noqa: BLE001
        logger.warning("同步 category 枚举失败，保持为空（不限制）：%s", exc)
        return []

    spec = REGISTRY.spec("kb_search")
    if spec is not None:
        prop = spec.parameters["properties"]["category"]
        prop["enum"] = cats
        prop["description"] = (
            "可选，按一级分类过滤。当前**确实有数据**的分类："
            + "、".join(cats)
            + "。其余分类尚未采集，填了只会得到空结果"
        )
    return cats


@REGISTRY.register(
    name="entity_lookup",
    description=(
        "按名称精确查一个实体（唯一装备等），返回其规范卡片。"
        "已知确切名称时用它，比 kb_search 更准。支持中文名、英文名与社区俗称。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "实体名，如「猎首」或「Headhunter」"},
        },
        "required": ["name"],
    },
)
def entity_lookup(name: str) -> ToolResult:
    from retrieval.alias import lookup as alias_lookup  # 延迟导入，避免循环依赖

    conn = dbmod.get_conn()
    entity_ids = alias_lookup(conn, name)
    if not entity_ids:
        return ToolResult(
            ok=True,
            data={"found": False, "reason": f"别名表里没有「{name}」，知识库无此条目"},
            citations=[],
        )

    citations: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT c.chunk_id, c.entity_id, c.text, e.name_zh, e.name_en, e.url, "
        "e.source, e.source_priority FROM chunk c JOIN entity e ON c.entity_id = e.entity_id "
        f"WHERE c.entity_id IN ({','.join('?' * len(entity_ids))}) LIMIT ?",
        (*entity_ids, MAX_EVIDENCE),
    )
    for r in rows:
        priority = int(r["source_priority"])
        title = r["name_zh"]
        if r["name_en"]:
            title = f"{r['name_zh']}（{r['name_en']}）"
        citations.append({
            "chunk_id": r["chunk_id"],
            "entity_id": r["entity_id"],
            "title": title,
            "url": r["url"],
            "source": r["source"],
            "source_priority": priority,
            "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
            "text": r["text"][:MAX_EVIDENCE_CHARS],
            "channel": "alias",
        })

    return ToolResult(
        ok=True,
        data={"found": bool(citations), "entity_ids": entity_ids,
              "evidence_count": len(citations)},
        citations=citations,
    )


# ── 获取途径（第五十四轮）─────────────────────────────────────

#: 单次最多列几条。静默截断等同于漏答，超出必须写出还剩多少。
MAX_ACQUISITION_ROWS = 30

#: 边界。由代码附在 `data` 里，不让模型转述。
_ACQ_BOUNDARY = (
    "获取途径只收录 POEDB 详情页 `Acquisition` 区块里的**命运卡与升级配方**，"
    "**不含掉落来源**（怎么刷）——那一维 POEDB 这一侧没有来源。"
    "而且覆盖是部分的：1342 件唯一装备里只有 264 件有这一段，"
    "所以**查不到不等于没有**。"
)


@REGISTRY.register(
    name="acquisition_routes",
    description=(
        "查某件唯一装备 / 通货 / 技能石的**获取途径**：哪张命运卡能换到它、"
        "或者用什么升级配方合成它。也可以反过来问某张命运卡能换到什么。"
        "**不回答掉落来源**（怎么刷）——那一维本库没有来源。"
        "覆盖是部分的（唯一装备只有两成有这一段），查不到会明说是哪一种情况。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "物品名（查它怎么来），或命运卡名（查它能换什么）。",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
)
def acquisition_routes(name: str) -> ToolResult:
    conn = dbmod.get_conn()
    q = (name or "").strip()
    if not q:
        return ToolResult(ok=False, error="name 不能为空")

    got = [dict(r) for r in conn.execute(
        "SELECT owner_kind, source_zh, ingredient, offer_zh, url, source, source_priority "
        "FROM poedb_acquisition WHERE offer_zh LIKE ? OR source_zh LIKE ? "
        "ORDER BY owner_kind, offer_zh, row_index",
        (f"%{q}%", f"%{q}%"),
    )]
    if not got:
        # **「本库没这一段」与「它没有获取途径」是两件事**，必须说清是哪一种
        known = conn.execute(
            "SELECT count(*) FROM poedb_acquisition").fetchone()[0]
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": (
                    f"本库的获取途径里没有「{q}」。**这不等于它没有获取途径**："
                    f"这一维只收录了 POEDB 详情页上有 `Acquisition` 区块的那些"
                    f"（共 {known} 行，唯一装备里只有两成有这一段），"
                    "而掉落来源本库完全没有采集。"
                ),
                "boundary": _ACQ_BOUNDARY,
            },
        )

    shown = got[:MAX_ACQUISITION_ROWS]
    lines = [f"### 「{q}」相关的获取途径（共 {len(got)} 条）", "",
             "| 换到手 | 来源 | 附加材料 | 类型 |", "| --- | --- | --- | --- |"]
    for r in shown:
        kind = "升级配方" if r["ingredient"] else "命运卡"
        lines.append(
            f"| {r['offer_zh']} | {r['source_zh']} | {r['ingredient'] or '—'} | {kind} |")
    if len(got) > len(shown):
        lines.append("")
        lines.append(f"（以上仅列 {len(shown)} 条，还有 {len(got) - len(shown)} 条未列出）")
    lines.append("")
    lines.append(_ACQ_BOUNDARY)
    return ToolResult(
        ok=True,
        data={
            "found": True, "total": len(got), "shown": len(shown),
            "truncated": len(got) > len(shown),
            "routes": [
                {"offer": r["offer_zh"], "source": r["source_zh"],
                 "ingredient": r["ingredient"],
                 "kind": "recipe" if r["ingredient"] else "divination_card"}
                for r in shown
            ],
            "boundary": _ACQ_BOUNDARY,
        },
        citations=[{
            "chunk_id": f"acquisition:{q}",
            "entity_id": None,
            "title": f"{q} 的获取途径（POEDB）",
            "url": got[0]["url"],
            "source": got[0]["source"],
            "source_priority": int(got[0]["source_priority"]),
            "can_support_facts": True,
            "text": "\n".join(lines),
            "channel": "structured",
        }],
        meta={"channel": "structured", "table": "poedb_acquisition"},
    )
