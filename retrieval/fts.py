"""FTS5 全文检索（BM25 通道）。

这是**唯一在所有平台、所有 Python 发行版上都确定可用**的检索通道：
FTS5 编译在 SQLite 内，不需要扩展加载。因此它是主通道，不是补充。

注意 `bm25()` 返回值**越小越相关**（是负数），排序与归一化都要按此处理。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from dataclasses import field

from entities.models import Chunk
from retrieval.tokenize import query_content_words, query_tokens

logger = logging.getLogger(__name__)


@dataclass
class Hit:
    """一条检索命中。"""

    chunk_id: str
    entity_id: str | None
    text: str
    metadata: dict
    #: 归一化到 0..1 的**相对**相关度，仅用于排序。
    #: 不可用作充分依据判据——相对归一化下最优项恒为 1.0。
    score: float
    #: 原始 bm25 值（负数，越小越相关），保留以便排查。
    #: **融合阶段不得覆写它**——曾经把 RRF 分写回这里，导致排查时看到的
    #: 「bm25」其实是融合分，症状与病因对不上。融合分写 `rrf` 字段。
    raw_score: float
    channel: str  # fts | alias | mod_intent
    source: str = ""
    source_priority: int = 99
    #: **查询词覆盖率**：查询的实义词有多少比例真的出现在本文档里。
    #: 这是绝对量，也是充分依据判定的依据。
    #: 「流放之路2 的升华机制是什么」只在文档里命中「升华」→ 覆盖率 1/5，
    #: 据此可判定证据不足，而 bm25 绝对值做不到这一点。
    coverage: float = 0.0
    matched_terms: list[str] = field(default_factory=list)
    missing_terms: list[str] = field(default_factory=list)
    #: RRF 融合分（多通道排名倒数之和，已施加来源优先级与意图偏好）。
    #: 只在融合后有值，是最终排序依据。
    rrf: float = 0.0

    @property
    def can_support_facts(self) -> bool:
        return self.source_priority <= 5


def term_coverage(query_terms: list[str], haystack: str) -> tuple[float, list[str], list[str]]:
    """计算查询实义词在文档中的出现比例。

    用子串包含判断：中文分词后的词直接在原文里找，比再分一次词更稳。
    """
    if not query_terms:
        return 0.0, [], []
    matched = [t for t in query_terms if t in haystack]
    missing = [t for t in query_terms if t not in haystack]
    return len(matched) / len(query_terms), matched, missing


def absent_from_corpus(conn: sqlite3.Connection, terms: list[str]) -> list[str]:
    """筛出**整个索引里一条切片都不包含**的词。

    ## 为什么需要它

    `term_coverage` 给每个查询词等权，于是一个查询可以只靠通用词过线，而**没被
    覆盖的恰好是主体词**。实测两例（都是必须拒答却漏出的）：

      - 「玄虚之靴有什么属性」：`之靴`、`属性` 命中，未覆盖的是 `玄虚` ——
        而 `玄虚之靴` 这个名字 POEDB 上根本不存在（真名神行靴）。
      - 「流放之路2 的升华机制是什么」：`升华`、`机制` 命中，未覆盖的是
        `流放之路2` ——那正是「这问的是另一款游戏」的唯一标志。

    两例的共同点：漏掉的词在**全库里都不存在**。一个词全库都没有，答案就不可能
    在库里，此时无论其余通用词覆盖到多少都不构成依据。这是「无据不答」的直接推论。

    ## 判据只取「全库为零」这一档

    刻意不做 IDF 或稀有度加权：那会改变整条判据的语义，可能把「已采集但这次没
    召回」也判成拒答，而那两类的正确处理方式完全不同（见 `kb.coverage.attribute_gap`）。
    「全库为零」是无歧义的——它只可能意味着没采集或问的不是这个领域。

    与 `term_coverage` 保持同一套匹配语义（子串包含），不用 FTS 的 MATCH：
    两套判据若分叉，会出现「覆盖率说命中了、存在性说不存在」的自相矛盾。
    """
    if not terms:
        return []
    absent: list[str] = []
    for t in terms:
        if not t.strip():
            continue
        row = conn.execute(
            "SELECT 1 FROM chunk WHERE instr(text, ?) > 0 LIMIT 1", (t,)
        ).fetchone()
        if row is not None:
            continue
        # keywords 列也算语料的一部分（`search_fts` 的 haystack 含它），
        # 漏查会把「只在 keywords 里出现的词」误判为不存在 → 错误拒答。
        # 这一步只在词已经不在任何正文里时才执行，代价可忽略。
        row = conn.execute(
            "SELECT 1 FROM chunk_fts WHERE instr(keywords, ?) > 0 LIMIT 1", (t,)
        ).fetchone()
        if row is None:
            absent.append(t)
    return absent


def index_chunks(conn: sqlite3.Connection, chunks: list[Chunk]) -> int:
    """写入 chunk 与 FTS 索引。同 chunk_id 覆盖，可重复执行。"""
    written = 0
    for c in chunks:
        if not c.metadata.indexable:
            logger.warning("跳过不可引用的切片（缺 url/source）：%s", c.chunk_id)
            continue
        conn.execute(
            "INSERT OR REPLACE INTO chunk(chunk_id, entity_id, text, metadata, created_at) "
            "VALUES (?,?,?,?,datetime('now'))",
            (c.chunk_id, c.entity_id, c.text, c.metadata.model_dump_json()),
        )
        # FTS5 无 UPSERT，先删后插保证幂等
        conn.execute("DELETE FROM chunk_fts WHERE chunk_id = ?", (c.chunk_id,))
        conn.execute(
            "INSERT INTO chunk_fts(tokens, keywords, chunk_id, entity_id) VALUES (?,?,?,?)",
            (c.tokens, c.keywords, c.chunk_id, c.entity_id or ""),
        )
        written += 1
    return written


def clear_index(conn: sqlite3.Connection, entity_prefix: str | None = None) -> None:
    """清空索引。

    `entity_prefix` 必须按类别传入，否则会连带删掉其他类别的切片——
    实测隐患：`--category unique_item --rebuild` 曾把 24,418 张词缀梯度卡一起清空，
    而日志只说「清空 chunk / FTS」，事后完全看不出词缀索引没了。
    """
    if entity_prefix:
        like = entity_prefix + "%"
        conn.execute("DELETE FROM chunk_fts WHERE entity_id LIKE ?", (like,))
        conn.execute("DELETE FROM chunk WHERE entity_id LIKE ?", (like,))
    else:
        conn.execute("DELETE FROM chunk_fts")
        conn.execute("DELETE FROM chunk")


def _normalize(raw_scores: list[float]) -> list[float]:
    """bm25 是负数且量纲随查询变化，用相对排名归一到 0..1。

    这里不用 min-max（单结果时会退化成 0 或 1），而是用
    `1 / (1 + 相对差距)`：最相关项恒为 1，其余按与最优的差距衰减。
    """
    if not raw_scores:
        return []
    best = min(raw_scores)  # 越小越相关
    out: list[float] = []
    for s in raw_scores:
        gap = s - best  # >= 0
        out.append(1.0 / (1.0 + gap))
    return out


def search_fts(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    category: str | None = None,
    league: str | None = None,
    entity_prefix: str | None = None,
    channel: str = "fts",
) -> list[Hit]:
    """BM25 检索。查询串走与写入相同的分词，保证一致。

    `entity_prefix` 可把召回限定在某类实体上（如 `mod_ladder:`），
    供意图通道使用——这样长文档不必与短文档在同一池子里竞争长度归一。
    """
    match = query_tokens(query)
    if not match:
        return []

    sql = (
        "SELECT f.chunk_id, f.entity_id, f.keywords, c.text, c.metadata, "
        "bm25(chunk_fts) AS score "
        "FROM chunk_fts f JOIN chunk c ON c.chunk_id = f.chunk_id "
        "WHERE chunk_fts MATCH ?"
    )
    params: list = [match]
    if entity_prefix:
        sql += " AND f.entity_id LIKE ?"
        params.append(entity_prefix + "%")
    sql += " ORDER BY score LIMIT ?"
    params.append(limit * 3)

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("FTS 查询失败（match=%r）：%s", match, exc)
        return []

    # 查询的实义词，用于覆盖率计算
    q_terms = query_content_words(query)

    hits: list[Hit] = []
    raw: list[float] = []
    for r in rows:
        meta = json.loads(r["metadata"])
        if category and meta.get("category") != category:
            continue
        if league and meta.get("league") != league:
            continue
        raw.append(float(r["score"]))
        haystack = f"{r['text']}\n{r['keywords']}"
        cov, matched, missing = term_coverage(q_terms, haystack)
        hits.append(
            Hit(
                chunk_id=r["chunk_id"],
                entity_id=r["entity_id"] or None,
                text=r["text"],
                metadata=meta,
                score=0.0,
                raw_score=float(r["score"]),
                channel=channel,
                source=meta.get("source", ""),
                source_priority=int(meta.get("source_priority", 99)),
                coverage=cov,
                matched_terms=matched,
                missing_terms=missing,
            )
        )

    for hit, norm in zip(hits, _normalize(raw), strict=True):
        hit.score = norm
    return hits[:limit]
