"""sqlite-vec indexing and query adapter for remote multilingual embeddings.

The index is deliberately disabled until a complete backfill for the exact
configured model finishes.  Chunk writes invalidate it, so stale vectors never
silently participate in retrieval.
"""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import struct
from dataclasses import dataclass

from retrieval.embedding import EmbeddingClient, EmbeddingError, MAX_BATCH
from retrieval.fts import Hit, term_coverage
from retrieval.tokenize import query_content_words


_DIM_RE = re.compile(r"embedding\s+FLOAT\[(\d+)\]", re.IGNORECASE)


@dataclass(frozen=True)
class VectorIndexStatus:
    available: bool
    reason: str
    dimension: int = 0
    indexed_count: int = 0


@dataclass(frozen=True)
class VectorBackfillResult:
    model: str
    dimension: int
    indexed_count: int
    status: str


def _table_dimension(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunk_vec'"
    ).fetchone()
    if row is None:
        return 0
    match = _DIM_RE.search(str(row[0] or ""))
    return int(match.group(1)) if match else 0


def vector_index_status(conn: sqlite3.Connection, model: str) -> VectorIndexStatus:
    if not model.strip():
        return VectorIndexStatus(False, "Embedding 模型未配置")
    try:
        dimension = _table_dimension(conn)
        if not dimension:
            return VectorIndexStatus(False, "向量表未建立或维度未知")
        row = conn.execute(
            "SELECT model, dim, indexed_count, dirty FROM vector_index_state WHERE id=1"
        ).fetchone()
        if row is None:
            return VectorIndexStatus(False, "向量索引尚未回填", dimension)
        if int(row[3]):
            return VectorIndexStatus(False, "知识切片更新后向量索引尚未回填", dimension)
        if str(row[0]) != model or int(row[1]) != dimension:
            return VectorIndexStatus(False, "向量索引模型或维度与当前配置不一致", dimension)
        indexed = int(row[2])
        corpus_count = int(conn.execute("SELECT count(*) FROM chunk").fetchone()[0])
        vector_count = int(conn.execute("SELECT count(*) FROM chunk_vec").fetchone()[0])
        if indexed <= 0 or indexed != corpus_count or vector_count != corpus_count:
            return VectorIndexStatus(False, "向量索引覆盖不完整", dimension, indexed)
        return VectorIndexStatus(True, "向量索引已完整回填", dimension, indexed)
    except sqlite3.Error as exc:
        return VectorIndexStatus(False, f"检查向量索引失败：{type(exc).__name__}")


def _serialize(vector: list[float]) -> bytes:
    try:
        import sqlite_vec  # noqa: PLC0415  可选扩展，仅在向量通道启用时导入
    except ImportError as exc:
        raise EmbeddingError("未安装 sqlite-vec") from exc
    return sqlite_vec.serialize_float32(vector)


def _next_batch(conn: sqlite3.Connection, after_id: str, size: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT chunk_id, text FROM chunk WHERE chunk_id > ? ORDER BY chunk_id LIMIT ?",
        (after_id, size),
    ).fetchall()


def _batch_vectors(
    conn: sqlite3.Connection, client: EmbeddingClient, batch: list[sqlite3.Row]
) -> list[list[float]]:
    hashes = [hashlib.sha256(str(row["text"]).encode("utf-8")).hexdigest() for row in batch]
    by_hash: dict[str, list[float]] = {}
    missing: dict[str, str] = {}
    for row, digest in zip(batch, hashes, strict=True):
        if digest in by_hash or digest in missing:
            continue
        cached = conn.execute(
            "SELECT dim, embedding FROM vector_embedding_cache "
            "WHERE model=? AND content_sha256=?",
            (client.model, digest),
        ).fetchone()
        if cached is None:
            missing[digest] = str(row["text"])
            continue
        dim, raw = int(cached[0]), bytes(cached[1])
        if dim <= 0 or len(raw) != dim * 4:
            raise EmbeddingError("缓存向量维度或长度无效")
        by_hash[digest] = list(struct.unpack(f"<{dim}f", raw))
    if missing:
        fresh = client.embed(list(missing.values()))
        if len(fresh) != len(missing):
            raise EmbeddingError("Embedding 新结果数量与缺失文本不一致")
        with conn:
            for digest, vector in zip(missing, fresh, strict=True):
                by_hash[digest] = vector
                conn.execute(
                    "INSERT OR IGNORE INTO vector_embedding_cache("
                    "model,content_sha256,dim,embedding) VALUES (?,?,?,?)",
                    (client.model, digest, len(vector), _serialize(vector)),
                )
    return [by_hash[digest] for digest in hashes]


def backfill_vectors(
    conn: sqlite3.Connection,
    client: EmbeddingClient,
    *,
    batch_size: int = 16,
) -> VectorBackfillResult:
    """Rebuild all vectors; partial failure leaves a dirty, unusable index."""
    if not 1 <= batch_size <= MAX_BATCH:
        raise ValueError(f"batch_size 必须在 1..{MAX_BATCH} 之间")
    first = _next_batch(conn, "", batch_size)
    if not first:
        raise ValueError("知识库没有可回填的 chunk")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vector_embedding_cache("
        "model TEXT NOT NULL, content_sha256 TEXT NOT NULL, dim INTEGER NOT NULL, "
        "embedding BLOB NOT NULL, PRIMARY KEY(model,content_sha256))"
    )
    first_vectors = _batch_vectors(conn, client, first)
    dimension = len(first_vectors[0])
    old_dimension = _table_dimension(conn)
    if old_dimension and old_dimension != dimension:
        raise ValueError(
            f"已有向量表维度 {old_dimension}，当前模型返回 {dimension}；"
            "请先在维护窗口按新维度重建向量表"
        )
    if not old_dimension:
        conn.execute(
            "CREATE VIRTUAL TABLE chunk_vec USING vec0("
            f"chunk_id TEXT PRIMARY KEY, embedding FLOAT[{dimension}])"
        )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vector_index_state("
        "id INTEGER PRIMARY KEY CHECK(id=1), model TEXT NOT NULL, dim INTEGER NOT NULL, "
        "indexed_count INTEGER NOT NULL, dirty INTEGER NOT NULL, "
        "corpus_revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)"
    )
    for operation in ("INSERT", "UPDATE", "DELETE"):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS chunk_vector_dirty_{operation.lower()} "
            f"AFTER {operation} ON chunk BEGIN "
            "UPDATE vector_index_state SET dirty=1, "
            "corpus_revision=corpus_revision+1 WHERE id=1; END"
        )
    previous = conn.execute(
        "SELECT corpus_revision FROM vector_index_state WHERE id=1"
    ).fetchone()
    revision = int(previous[0]) if previous else 0
    with conn:
        conn.execute("DELETE FROM chunk_vec")
        conn.execute(
            "INSERT OR REPLACE INTO vector_index_state("
            "id,model,dim,indexed_count,dirty,corpus_revision,updated_at) "
            "VALUES (1,?,?,0,1,?,datetime('now'))",
            (client.model, dimension, revision),
        )

    batch = first
    vectors = first_vectors
    last_id = ""
    indexed = 0
    while batch:
        if len(vectors) != len(batch) or any(len(vector) != dimension for vector in vectors):
            raise EmbeddingError("Embedding 批次维度或数量发生变化")
        with conn:
            for row, vector in zip(batch, vectors, strict=True):
                conn.execute(
                    "INSERT INTO chunk_vec(chunk_id, embedding) VALUES (?, ?)",
                    (row["chunk_id"], _serialize(vector)),
                )
            indexed += len(batch)
            conn.execute(
                "UPDATE vector_index_state SET indexed_count=?, updated_at=datetime('now') "
                "WHERE id=1",
                (indexed,),
            )
        last_id = str(batch[-1]["chunk_id"])
        batch = _next_batch(conn, last_id, batch_size)
        if batch:
            vectors = _batch_vectors(conn, client, batch)

    with conn:
        state = conn.execute(
            "SELECT corpus_revision FROM vector_index_state WHERE id=1"
        ).fetchone()
        corpus_count = int(conn.execute("SELECT count(*) FROM chunk").fetchone()[0])
        if state is None or int(state[0]) != revision or corpus_count != indexed:
            raise RuntimeError("回填期间知识切片发生变化；索引保持禁用，请重新回填")
        conn.execute(
            "UPDATE vector_index_state SET dirty=0, updated_at=datetime('now') WHERE id=1"
        )
    return VectorBackfillResult(client.model, dimension, indexed, "ready")


def search_vector(
    conn: sqlite3.Connection,
    query: str,
    client: EmbeddingClient,
    *,
    limit: int = 20,
    category: str | None = None,
    league: str | None = None,
    version: str | None = None,
) -> list[Hit]:
    status = vector_index_status(conn, client.model)
    if not status.available:
        raise EmbeddingError(status.reason)
    vector = client.embed([query])[0]
    if len(vector) != status.dimension:
        raise EmbeddingError("查询向量维度与已回填索引不一致")
    rows = conn.execute(
        "SELECT c.chunk_id, c.entity_id, c.text, c.metadata, "
        "COALESCE(f.keywords, '') AS keywords, v.distance "
        "FROM chunk_vec v JOIN chunk c ON c.chunk_id=v.chunk_id "
        "LEFT JOIN chunk_fts f ON f.chunk_id=c.chunk_id "
        "WHERE v.embedding MATCH ? AND v.k=? ORDER BY v.distance",
        (
            _serialize(vector),
            status.indexed_count if category or league or version
            else min(max(limit * 3, limit), status.indexed_count),
        ),
    ).fetchall()
    query_terms = query_content_words(query)
    hits: list[Hit] = []
    for row in rows:
        meta = json.loads(row["metadata"])
        if category and meta.get("category") != category:
            continue
        if league and meta.get("league") != league:
            continue
        if version and meta.get("version") != version:
            continue
        distance = float(row["distance"])
        coverage, matched, missing = term_coverage(
            query_terms, f"{row['text']}\n{row['keywords']}"
        )
        hits.append(Hit(
            chunk_id=row["chunk_id"], entity_id=row["entity_id"],
            text=row["text"], metadata=meta,
            score=1.0 / (1.0 + distance), raw_score=distance,
            channel="vector", source=meta.get("source", ""),
            source_priority=int(meta.get("source_priority", 99)),
            coverage=coverage, matched_terms=matched, missing_terms=missing,
        ))
        if len(hits) >= limit:
            break
    return hits
