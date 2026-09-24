"""确定性计算器 HTTP API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from tools.impl.dps import hit_dps_calc

router = APIRouter(tags=["计算器"])


class ConversionRequest(BaseModel):
    source: str = Field(min_length=1, max_length=20)
    target: str = Field(min_length=1, max_length=20)
    percent: float = Field(ge=0, le=1000, allow_inf_nan=False)
    kind: str = Field(default="global", pattern="^(global|skill|gain)$")


class HitDpsRequest(BaseModel):
    base_damage: dict[str, tuple[float, float]]
    added_damage: dict[str, tuple[float, float]] = Field(default_factory=dict)
    base_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    damage_effectiveness: float = Field(default=1, ge=0, allow_inf_nan=False)
    added_damage_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    increased_damage_pct: float = Field(default=0, ge=-100, allow_inf_nan=False)
    increased_by_type: dict[str, float] = Field(default_factory=dict)
    more_damage_multipliers: list[float] = Field(default_factory=list, max_length=50)
    more_by_type: dict[str, float] = Field(default_factory=dict)
    conversions: list[ConversionRequest] = Field(default_factory=list, max_length=50)
    enemy_resistance: dict[str, float] = Field(default_factory=dict)
    penetration: dict[str, float] = Field(default_factory=dict)
    enemy_physical_reduction_pct: float = Field(default=0, ge=0, le=90, allow_inf_nan=False)
    enemy_damage_taken_increased_pct: float = Field(default=0, ge=-100, allow_inf_nan=False)
    enemy_damage_taken_more_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    hit_chance_pct: float = Field(default=100, ge=0, le=100, allow_inf_nan=False)
    crit_chance_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    crit_multiplier_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    action_rate: float = Field(gt=0, allow_inf_nan=False)
    skill_dps_multiplier: float = Field(default=1, gt=0, allow_inf_nan=False)
    quantity_multiplier: float = Field(default=1, gt=0, allow_inf_nan=False)


@router.post("/calc/hit-dps", response_model=None)
def calculate_hit_dps(req: HitDpsRequest, _caller: str = Depends(require_user)) -> dict[str, Any]:
    result = hit_dps_calc(
        **req.model_dump(exclude={"conversions"}),
        conversions=[item.model_dump() for item in req.conversions],
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return dict(result.data or {})
