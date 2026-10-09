"""普通用户可读的跨来源差异/冲突查询 API。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from tools.impl.conflicts import source_conflict_lookup, source_conflict_summary


router = APIRouter(tags=["跨来源核对"])


class SourceConflictLookupRequest(BaseModel):
    query: str = Field(default="", max_length=160)
    status: Literal["confirmed", "review", "all"] = "confirmed"
    resolution: Literal["active", "resolved", "all"] = "active"
    kinds: list[str] = Field(default_factory=list, max_length=20)
    sources: list[str] = Field(default_factory=list, max_length=20)
    limit: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=500)


class SourceConflictItemOut(BaseModel):
    conflict_id: int
    subject: str
    field: str
    kind: str
    status: Literal["confirmed", "review"]
    source_a: str
    value_a: str
    url_a: str | None = None
    source_b: str
    value_b: str
    url_b: str | None = None
    note: str | None = None
    cause: str | None = None
    resolved: bool
    first_seen: str
    last_seen: str


class SourceConflictLookupResponse(BaseModel):
    found: bool
    status_filter: Literal["confirmed", "review", "all"]
    resolution_filter: Literal["active", "resolved", "all"]
    query: str
    kinds: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    total_matching: int
    returned: int
    confirmed_count: int
    review_count: int
    resolved_count: int
    has_more: bool
    next_cursor: str | None = None
    items: list[SourceConflictItemOut] = Field(default_factory=list)
    classification_rule: str
    review_notice: str | None = None
    boundary: str
    answer: str


class SourceConflictKindSummaryOut(BaseModel):
    kind: str
    status: Literal["confirmed", "review"]
    active_count: int
    resolved_count: int
    total_count: int


class SourceConflictPairSummaryOut(BaseModel):
    source_a: str
    source_b: str
    confirmed_count: int
    review_count: int
    total_count: int


class SourceConflictCauseSummaryOut(BaseModel):
    cause: str
    review_count: int


class SourceConflictSummaryResponse(BaseModel):
    total_count: int
    active_count: int
    resolved_count: int
    confirmed_count: int
    confirmed_active_count: int
    review_count: int
    review_active_count: int
    kinds: list[SourceConflictKindSummaryOut] = Field(default_factory=list)
    source_pairs: list[SourceConflictPairSummaryOut] = Field(default_factory=list)
    review_causes: list[SourceConflictCauseSummaryOut] = Field(default_factory=list)
    classification_rule: str
    user_facing_rule: str
    boundary: str
    answer: str


@router.post("/conflicts/lookup", response_model=SourceConflictLookupResponse)
def conflict_lookup(
    req: SourceConflictLookupRequest,
    _caller: str = Depends(require_user),
) -> SourceConflictLookupResponse:
    result = source_conflict_lookup(**req.model_dump())
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_source_conflict_query", "reason": result.error,
        })
    return SourceConflictLookupResponse(**(result.data or {}))


@router.get("/conflicts/summary", response_model=SourceConflictSummaryResponse)
def conflict_summary(
    _caller: str = Depends(require_user),
) -> SourceConflictSummaryResponse:
    result = source_conflict_summary()
    if not result.ok:
        raise HTTPException(status_code=500, detail={
            "error": "source_conflict_summary_failed", "reason": result.error,
        })
    return SourceConflictSummaryResponse(**(result.data or {}))
