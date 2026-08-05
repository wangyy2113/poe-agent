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
            "category": {
                "type": "string",
                "description": "可选，按一级分类过滤：游戏基础 / 装备 / 制作系统 / "
                "地图系统 / 联盟机制 / 经济系统",
            },
            "top_k": {"type": "integer", "description": "可选，返回条数，1-20"},
        },
        "required": ["query"],
    },
)
def kb_search(query: str, category: str | None = None, top_k: int | None = None) -> ToolResult:
    cfg = get_config()
    k = max(1, min(int(top_k or cfg.get("retrieval.top_k", 20)), 20))
    result = retrieve(
        dbmod.get_conn(),
        query,
        top_k=k,
        min_score=float(cfg.get("retrieval.min_score", 0.35)),
        min_coverage=float(cfg.get("retrieval.min_coverage", 0.5)),
        rrf_k=int(cfg.get("retrieval.rrf_k", 60)),
        priority_decay=float(cfg.get("retrieval.priority_decay", 0.08)),
        vector_enabled=bool(cfg.get("retrieval.vector.enabled", False)),
        rerank_enabled=bool(cfg.get("retrieval.rerank.enabled", False)),
        category=category or None,
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
