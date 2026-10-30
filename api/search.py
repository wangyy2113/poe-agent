"""`/search`：纯检索接口，供检索质量评测与调试（需求 3.7）。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core import db as dbmod
from core.config import get_config
from retrieval.hybrid import retrieve
from retrieval.runtime import configured_vector_runtime

router = APIRouter(tags=["检索"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int | None = Field(default=None, ge=1, le=50)
    category: str | None = None


class HitOut(BaseModel):
    chunk_id: str
    entity_id: str | None
    title: str
    url: str
    source: str
    source_priority: int
    channel: str
    score: float
    coverage: float
    text: str


class SearchResponse(BaseModel):
    query: str
    #: False 时上层必须走缺据声明路径，**不得**基于 hits 作答。
    #: BM25 总会返回「最不差」的若干条，那不等于有依据。
    sufficient: bool
    reason: str
    top_coverage: float
    missing_terms: list[str]
    channels: list[str]
    degraded: bool
    note: str
    hits: list[HitOut]


@router.post("/search", response_model=SearchResponse)
def search(req: SearchRequest) -> SearchResponse:
    cfg = get_config()
    conn = dbmod.get_conn()
    vector_requested = bool(cfg.get("retrieval.vector.enabled", False))
    runtime, embedding_client = configured_vector_runtime(
        conn, vector_requested,
        timeout_seconds=float(cfg.get("retrieval.timeout_seconds", 8)),
    )
    result = retrieve(
        conn,
        req.query,
        top_k=req.top_k or int(cfg.get("retrieval.top_k", 20)),
        min_score=float(cfg.get("retrieval.min_score", 0.35)),
        min_coverage=float(cfg.get("retrieval.min_coverage", 0.5)),
        rrf_k=int(cfg.get("retrieval.rrf_k", 60)),
        priority_decay=float(cfg.get("retrieval.priority_decay", 0.08)),
        vector_enabled=runtime.available,
        vector_disabled_reason=(
            runtime.reason
            if vector_requested and not runtime.available
            else ""
        ),
        embedding_client=embedding_client,
        rerank_enabled=bool(cfg.get("retrieval.rerank.enabled", False)),
        category=req.category,
    )
    return SearchResponse(
        query=result.query,
        sufficient=result.sufficient,
        reason=result.reason,
        top_coverage=result.top_coverage,
        missing_terms=result.missing_terms,
        channels=result.channels,
        degraded=result.degraded,
        note=result.note,
        hits=[
            HitOut(
                chunk_id=h.chunk_id,
                entity_id=h.entity_id,
                title=h.metadata.get("title", ""),
                url=h.metadata.get("url", ""),
                source=h.source,
                source_priority=h.source_priority,
                channel=h.channel,
                score=round(h.score, 4),
                coverage=round(h.coverage, 4),
                text=h.text,
            )
            for h in result.hits
        ],
    )
