"""`/search`：纯检索接口，供检索质量评测与调试（需求 3.7）。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core import db as dbmod
from retrieval.runtime import configured_retrieve

router = APIRouter(tags=["检索"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int | None = Field(default=None, ge=1, le=50)
    category: str | None = None
    league: str | None = None
    version: str | None = None


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
    result = configured_retrieve(
        dbmod.get_conn(), req.query, top_k=req.top_k,
        category=req.category, league=req.league, version=req.version,
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
