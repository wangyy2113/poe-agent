"""实时经济价格、成本与双边价差 API。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from economy.models import MaterialRequest as DomainMaterialRequest
from economy.models import TradeFeeInput
from economy.rare_items import InvalidRareComparable
from economy.service import EconomyUnavailable, InvalidEconomyQuery
from economy.trade_api import TradeApiError
from tools.impl.economy import (
    rare_service_from_config,
    render_flip_analysis,
    render_material_cost,
    render_price,
    render_rare_comparables,
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


class RareComparableRequest(BaseModel):
    item_text: str = Field(min_length=1, max_length=20000)
    league: str | None = Field(default=None, max_length=100)


class RareComparableResponse(BaseModel):
    available: bool
    status: str
    reason: str
    league: str
    source: str
    source_url: str
    fetched_at: str
    base_type_input: str
    base_type_en: str
    item_level: int | None
    corrupted: bool
    explicit_mod_count: int
    mapped_mod_count: int
    comparison_quality: str
    mapped_stats: list[dict[str, Any]]
    unmapped_mods: list[str]
    quote: dict[str, Any] | None
    answer: str


class TradeFeeRequest(BaseModel):
    buy_fixed_chaos: float = Field(ge=0, le=1_000_000_000, allow_inf_nan=False)
    buy_percent: float = Field(ge=0, lt=100, allow_inf_nan=False)
    sell_fixed_chaos: float = Field(ge=0, le=1_000_000_000, allow_inf_nan=False)
    sell_percent: float = Field(ge=0, lt=100, allow_inf_nan=False)


class FlipAnalysisRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
    fees: TradeFeeRequest
    category: Literal["auto", "currency", "scarab", "card", "fragment"] = "auto"
    league: str | None = Field(default=None, max_length=100)
    max_listing_age_hours: int = Field(default=24, ge=1, le=168)
    minimum_net_profit_chaos: float = Field(default=0, ge=0, allow_inf_nan=False)
    minimum_margin_percent: float = Field(default=0, ge=0, allow_inf_nan=False)


class FlipAnalysisResponse(BaseModel):
    query: str
    resolved_name: str
    category: str
    league: str
    quantity: float
    source: str
    source_url: str
    fetched_at: str
    max_listing_age_hours: int
    status: str
    reason: str
    calculable: bool
    verdict: str
    minimum_net_profit_chaos: float
    minimum_margin_percent: float
    fees: dict[str, Any]
    buy_side: dict[str, Any]
    sell_side: dict[str, Any]
    gross_buy_cost_chaos: float | None
    buy_fee_chaos: float | None
    total_buy_cost_chaos: float | None
    gross_sell_revenue_chaos: float | None
    sell_fee_chaos: float | None
    net_sell_revenue_chaos: float | None
    net_profit_chaos: float | None
    margin_percent: float | None
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


@router.post("/economy/flip-analysis", response_model=FlipAnalysisResponse)
def flip_analysis(
    req: FlipAnalysisRequest, _caller: str = Depends(require_user)
) -> FlipAnalysisResponse:
    try:
        result = service_from_config().flip_analysis(
            req.query,
            quantity=req.quantity,
            fees=TradeFeeInput(**req.fees.model_dump()),
            category=req.category,
            league=req.league,
            max_listing_age_hours=req.max_listing_age_hours,
            minimum_net_profit_chaos=req.minimum_net_profit_chaos,
            minimum_margin_percent=req.minimum_margin_percent,
        )
    except InvalidEconomyQuery as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_flip_analysis", "reason": str(exc),
        }) from exc
    except (EconomyUnavailable, TradeApiError) as exc:
        raise HTTPException(status_code=503, detail={
            "error": "economy_unavailable", "reason": str(exc),
        }) from exc
    return FlipAnalysisResponse(
        **result.as_dict(), answer=render_flip_analysis(result)
    )


@router.post("/economy/rare-comparables", response_model=RareComparableResponse)
def rare_comparables(
    req: RareComparableRequest, _caller: str = Depends(require_user)
) -> RareComparableResponse:
    try:
        result = rare_service_from_config().estimate(req.item_text, league=req.league)
    except InvalidRareComparable as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_rare_comparable", "reason": str(exc),
        }) from exc
    except TradeApiError as exc:
        raise HTTPException(status_code=503, detail={
            "error": "trade_api_unavailable", "reason": str(exc),
        }) from exc
    return RareComparableResponse(
        **result.as_dict(), answer=render_rare_comparables(result)
    )


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
