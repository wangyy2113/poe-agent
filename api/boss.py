"""Boss 伤害与防御阈值 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from tools.impl.boss import boss_survival_check

router = APIRouter(tags=["Boss"])


class BossSurvivalRequest(BaseModel):
    skill: str = Field(min_length=2, max_length=120)
    level: int = Field(ge=1, le=100)
    pool: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
    hit_count: int = Field(default=1, ge=1, le=100)
    uber: bool = False
    roll_range: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    fire_resist_pct: float | None = Field(default=None, ge=-200, le=90, allow_inf_nan=False)
    cold_resist_pct: float | None = Field(default=None, ge=-200, le=90, allow_inf_nan=False)
    lightning_resist_pct: float | None = Field(default=None, ge=-200, le=90, allow_inf_nan=False)
    chaos_resist_pct: float | None = Field(default=None, ge=-200, le=90, allow_inf_nan=False)
    armour: float = Field(default=0, ge=0, le=10_000_000_000, allow_inf_nan=False)
    flat_physical_reduction_pct: float = Field(
        default=0, ge=0, le=90, allow_inf_nan=False
    )
    taken_mult: float = Field(default=1, gt=0, allow_inf_nan=False)
    overwhelm_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)


class BossSurvivalResponse(BaseModel):
    found: bool
    assessable: bool
    verdict: str | None = None
    skill: str | None = None
    level: int | None = None
    uber: bool | None = None
    roll_range: float | None = None
    damage_type: str | None = None
    pool: float | None = None
    raw_low: float | None = None
    raw_high: float | None = None
    hit_count: int = 1
    total_raw_low: float | None = None
    total_raw_high: float | None = None
    resist_before_penetration: float | None = None
    penetration_pct: float | None = None
    effective_resist_pct: float | None = None
    taken_low: float | None = None
    taken_high: float | None = None
    total_taken_low: float | None = None
    total_taken_high: float | None = None
    remaining_low: float | None = None
    remaining_high: float | None = None
    max_raw_hit: float | None = None
    margin_low: float | None = None
    margin_high: float | None = None
    outcome: str | None = None
    reason: str | None = None
    candidates: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    boundary: str
    answer: str


@router.post("/boss/survival", response_model=BossSurvivalResponse)
def survival(
    req: BossSurvivalRequest, _caller: str = Depends(require_user)
) -> BossSurvivalResponse:
    result = boss_survival_check(**req.model_dump())
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_survival_query", "reason": result.error,
        })
    return BossSurvivalResponse(**(result.data or {}))
