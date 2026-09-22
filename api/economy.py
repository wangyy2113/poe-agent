"""实时经济价格 API。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from economy.models import MaterialRequest as DomainMaterialRequest
from economy.service import EconomyUnavailable, InvalidEconomyQuery
from tools.impl.economy import (
    render_material_cost,
    render_price,
    render_trend,
    service_from_config,
)

router = APIRouter(tags=["经济"])


class PriceRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    category: Literal["auto", "unique", "currency", "scarab", "card", "fragment"] = "auto"
    league: str | None = Field(default=None, max_length=100)


class PriceResponse(BaseModel):
    found: bool
    query: str
    resolved_name: str
    requested_category: str
    categories_searched: list[str]
    league: str
    source: str
    source_url: str
    fetched_at: str
    from_cache: bool
    stale: bool
    degraded: bool
    fallback: bool
    fallback_reason: str
    warning: str
    prices: list[dict[str, Any]]
    answer: str


class TrendRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    category: Literal["auto", "unique", "currency", "scarab", "card", "fragment"] = "auto"
    league: str | None = Field(default=None, max_length=100)
    window_hours: int = Field(default=168, ge=1, le=2160)


class TrendResponse(BaseModel):
    found: bool
    analyzable: bool
    query: str
    resolved_name: str
    requested_category: str
    categories_searched: list[str]
    league: str
    source: str
    source_url: str
    fetched_at: str
    from_cache: bool
    stale: bool
    degraded: bool
    fallback: bool
    fallback_reason: str
    warning: str
    window_hours: int
    minimum_observations: int
    trends: list[dict[str, Any]]
    answer: str


class MaterialCostItemRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
    category: Literal["auto", "unique", "currency", "scarab", "card", "fragment"] = "auto"


class MaterialCostRequest(BaseModel):
    materials: list[MaterialCostItemRequest] = Field(min_length=1, max_length=50)
    league: str | None = Field(default=None, max_length=100)


class MaterialCostResponse(BaseModel):
    league: str
    source: str
    source_url: str
    fetched_at: str
    from_cache: bool
    stale: bool
    degraded: bool
    fallback: bool
    warning: str
    calculable: bool
    unresolved_count: int
    total_representative_chaos: float | None
    total_mean_chaos: float | None
    total_min_chaos: float | None
    total_max_chaos: float | None
    materials: list[dict[str, Any]]
    answer: str


@router.post("/economy/price", response_model=PriceResponse)
def price(req: PriceRequest, _caller: str = Depends(require_user)) -> PriceResponse:
    try:
        result = service_from_config().price(
            req.query, category=req.category, league=req.league
        )
    except InvalidEconomyQuery as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_economy_query", "reason": str(exc),
        }) from exc
    except EconomyUnavailable as exc:
        raise HTTPException(status_code=503, detail={
            "error": "economy_unavailable", "reason": str(exc),
        }) from exc
    return PriceResponse(**result.as_dict(), answer=render_price(result))


@router.post("/economy/trend", response_model=TrendResponse)
def trend(req: TrendRequest, _caller: str = Depends(require_user)) -> TrendResponse:
    try:
        result = service_from_config().trend(
            req.query,
            category=req.category,
            league=req.league,
            window_hours=req.window_hours,
        )
    except InvalidEconomyQuery as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_economy_trend_query", "reason": str(exc),
        }) from exc
    except EconomyUnavailable as exc:
        raise HTTPException(status_code=503, detail={
            "error": "economy_unavailable", "reason": str(exc),
        }) from exc
    return TrendResponse(**result.as_dict(), answer=render_trend(result))


@router.post("/economy/material-cost", response_model=MaterialCostResponse)
def material_cost(
    req: MaterialCostRequest, _caller: str = Depends(require_user)
) -> MaterialCostResponse:
    try:
        result = service_from_config().material_cost(
            [
                DomainMaterialRequest(
                    query=item.query,
                    quantity=item.quantity,
                    category=item.category,
                )
                for item in req.materials
            ],
            league=req.league,
        )
    except InvalidEconomyQuery as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_material_cost_query", "reason": str(exc),
        }) from exc
    except EconomyUnavailable as exc:
        raise HTTPException(status_code=503, detail={
            "error": "economy_unavailable", "reason": str(exc),
        }) from exc
    return MaterialCostResponse(**result.as_dict(), answer=render_material_cost(result))
