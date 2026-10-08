"""确定性计算器 HTTP API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from tools.impl.dps import hit_dps_calc, hit_dps_compare, hit_dps_target_rate
from tools.impl.dps_rotation import (
    rotation_dps_calc,
    rotation_dps_compare,
    rotation_dps_target_multiplier,
)

router = APIRouter(tags=["计算器"])


class ConversionRequest(BaseModel):
    source: str = Field(min_length=1, max_length=20)
    target: str = Field(min_length=1, max_length=20)
    percent: float = Field(ge=0, le=1000, allow_inf_nan=False)
    kind: str = Field(default="global", pattern="^(global|skill|gain)$")


class DamagingAilmentRequest(BaseModel):
    name: str = Field(pattern="^(ignite|poison|bleed|点燃|中毒|流血)$")
    source_damage: tuple[float, float]
    crit_source_damage: tuple[float, float] | None = None
    chance_on_hit_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    chance_on_crit_pct: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    dot_multiplier_pct: float = Field(default=0, ge=-100, allow_inf_nan=False)
    crit_dot_multiplier_pct: float | None = Field(default=None, ge=-100, allow_inf_nan=False)
    base_duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    duration_increased_pct: float = Field(default=0, ge=-100, allow_inf_nan=False)
    faster_damage_pct: float = Field(default=0, gt=-100, allow_inf_nan=False)
    stack_limit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    stacks_per_application: float = Field(default=1, gt=0, allow_inf_nan=False)
    ailment_effect_increased_pct: float = Field(default=0, ge=-100, allow_inf_nan=False)
    ruthless_blow_ailment_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    exerted_ailment_more_damage_pct: float = Field(default=0, ge=0, allow_inf_nan=False)
    exerted_ailment_uptime_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    enemy_mitigation_pct: float | None = Field(default=None, ge=-200, le=90, allow_inf_nan=False)
    enemy_damage_taken_increased_pct: float | None = Field(
        default=None, ge=-100, allow_inf_nan=False
    )
    enemy_damage_taken_more_multiplier: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )


class SkillDotRequest(BaseModel):
    base_damage_per_second: dict[str, float]
    increased_damage_pct: float = Field(default=0, ge=-100, allow_inf_nan=False)
    increased_by_type: dict[str, float] = Field(default_factory=dict)
    more_damage_multipliers: list[float] = Field(default_factory=list, max_length=50)
    more_by_type: dict[str, float] = Field(default_factory=dict)
    dot_multiplier_pct: float = Field(default=0, ge=-100, allow_inf_nan=False)
    dot_multiplier_by_type: dict[str, float] = Field(default_factory=dict)
    aura_effect_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    enemy_mitigation_pct_by_type: dict[str, float] = Field(default_factory=dict)
    enemy_damage_taken_increased_pct: float = Field(default=0, ge=-100, allow_inf_nan=False)
    enemy_damage_taken_increased_by_type: dict[str, float] = Field(default_factory=dict)
    enemy_damage_taken_more_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    enemy_damage_taken_more_by_type: dict[str, float] = Field(default_factory=dict)
    can_stack: bool = False
    duration_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    application_rate_multiplier: float = Field(default=1, gt=0, allow_inf_nan=False)
    instance_multiplier: float = Field(default=1, gt=0, allow_inf_nan=False)
    stack_limit: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class HitDpsRequest(BaseModel):
    base_damage: dict[str, tuple[float, float]] = Field(default_factory=dict)
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
    overwhelm_physical_reduction_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    physical_reduction_ignore_chance_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    resistance_ignore_chance_pct: dict[str, float] = Field(default_factory=dict)
    resistance_inversion_chance_pct: dict[str, float] = Field(default_factory=dict)
    unmodifiable_resistance_types: list[str] = Field(default_factory=list, max_length=4)
    enemy_damage_taken_increased_pct: float = Field(default=0, ge=-100, allow_inf_nan=False)
    enemy_damage_taken_increased_by_type: dict[str, float] = Field(default_factory=dict)
    enemy_damage_taken_more_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    enemy_damage_taken_more_by_type: dict[str, float] = Field(default_factory=dict)
    hit_chance_pct: float = Field(default=100, ge=0, le=100, allow_inf_nan=False)
    crit_chance_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    crit_multiplier_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    damage_roll_chance_pct: float = Field(default=0, ge=-100, le=100, allow_inf_nan=False)
    crit_damage_roll_chance_pct: float | None = Field(default=None, ge=-100, le=100, allow_inf_nan=False)
    extreme_luck: bool = False
    double_damage_chance_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    triple_damage_chance_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    double_damage_chance_on_crit_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    triple_damage_chance_on_crit_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    ruthless_blow_every_hits: int = Field(default=0, ge=0, le=1000)
    ruthless_blow_hit_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    fist_of_war_more_damage_pct: float = Field(default=0, ge=0, allow_inf_nan=False)
    fist_of_war_cooldown_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    exerted_attack_more_damage_pct: float = Field(default=0, ge=0, allow_inf_nan=False)
    exerted_attack_uptime_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    action_rate: float = Field(gt=0, allow_inf_nan=False)
    cooldown_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    server_tick_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    cooldown_uses: int = Field(default=1, ge=1, le=1000)
    action_repeats: int = Field(default=1, ge=1, le=1000)
    channelled: bool = False
    triggered: bool = False
    skill_dps_multiplier: float = Field(default=1, gt=0, allow_inf_nan=False)
    quantity_multiplier: float = Field(default=1, gt=0, allow_inf_nan=False)
    burst_hit_count: float = Field(default=1, ge=1, allow_inf_nan=False)
    repeat_damage_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    impale_chance_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    impale_chance_on_crit_pct: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    impale_stacks: float = Field(default=0, ge=0, allow_inf_nan=False)
    impale_base_stored_damage_pct: float = Field(default=10, ge=0, allow_inf_nan=False)
    impale_effect_increased_pct: float = Field(default=0, ge=-100, allow_inf_nan=False)
    impale_effect_more_multiplier: float = Field(default=1, ge=0, allow_inf_nan=False)
    impale_duration_seconds: float = Field(default=4, ge=0, allow_inf_nan=False)
    enemy_armour: float = Field(default=0, ge=0, allow_inf_nan=False)
    enemy_impale_physical_reduction_pct: float | None = Field(
        default=None, ge=0, le=90, allow_inf_nan=False
    )
    ignore_enemy_impale_physical_reduction: bool = False
    enemy_reflected_damage_taken_increased_pct: float = Field(
        default=0, ge=-100, allow_inf_nan=False
    )
    enemy_reflected_damage_taken_more_multiplier: float = Field(
        default=1, ge=0, allow_inf_nan=False
    )
    cull_percent: float = Field(default=0, ge=0, lt=100, allow_inf_nan=False)
    critical_cull_percent: float = Field(default=0, ge=0, lt=100, allow_inf_nan=False)
    damaging_ailments: list[DamagingAilmentRequest] = Field(default_factory=list, max_length=3)
    skill_dot: SkillDotRequest | None = None
    combat_duration_seconds: float | None = Field(
        default=None, gt=0, le=3600, allow_inf_nan=False
    )


class HitDpsScenarioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    inputs: HitDpsRequest


class HitDpsCompareRequest(BaseModel):
    scenarios: list[HitDpsScenarioRequest] = Field(min_length=2, max_length=10)


class HitDpsTargetRateInputs(HitDpsRequest):
    action_rate: float = Field(default=1, gt=0, exclude=True, allow_inf_nan=False)


class HitDpsTargetRateRequest(BaseModel):
    target_dps: float = Field(gt=0, allow_inf_nan=False)
    metric: str = Field(
        default="hit_dps",
        pattern=(
            "^(hit_dps|combined_dps|effective_dps_with_cull|ailment_dps|skill_dot_dps|"
            "total_dot_dps|total_dps|"
            "effective_total_dps_with_cull|combat_average_combined_dps|"
            "combat_average_ailment_dps|combat_average_skill_dot_dps|"
            "combat_average_total_dot_dps|combat_average_total_dps|"
            "combat_average_effective_total_dps_with_cull)$"
        ),
    )
    inputs: HitDpsTargetRateInputs


class RotationSkillRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    allocation_pct: float | None = Field(
        default=None, gt=0, le=100, allow_inf_nan=False
    )
    inputs: HitDpsRequest


class RotationDotGroupMemberRequest(BaseModel):
    skill: str = Field(min_length=1, max_length=80)
    component: str = Field(pattern="^(ignite|poison|bleed|skill_dot)$")


class RotationDotGroupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    policy: str = Field(default="strongest", pattern="^strongest$")
    members: list[RotationDotGroupMemberRequest] = Field(min_length=2, max_length=40)


class RotationDpsRequest(BaseModel):
    skills: list[RotationSkillRequest] = Field(min_length=2, max_length=10)
    mode: str = Field(default="rotation", pattern="^(rotation|parallel)$")
    dot_groups: list[RotationDotGroupRequest] = Field(default_factory=list, max_length=20)
    combat_duration_seconds: float | None = Field(
        default=None, gt=0, le=3600, allow_inf_nan=False
    )
    cull_percent: float = Field(default=0, ge=0, lt=100, allow_inf_nan=False)


class RotationScenarioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    inputs: RotationDpsRequest


class RotationCompareRequest(BaseModel):
    scenarios: list[RotationScenarioRequest] = Field(min_length=2, max_length=10)


class RotationTargetRequest(BaseModel):
    target_dps: float = Field(gt=0, allow_inf_nan=False)
    metric: str = Field(
        default="steady_total_dps",
        pattern=(
            "^(steady_total_dps|steady_total_dot_dps|effective_steady_total_dps|"
            "combat_average_total_dps|combat_average_total_dot_dps|"
            "combat_average_effective_total_dps)$"
        ),
    )
    inputs: RotationDpsRequest


@router.post("/calc/hit-dps", response_model=None)
def calculate_hit_dps(req: HitDpsRequest, _caller: str = Depends(require_user)) -> dict[str, Any]:
    result = hit_dps_calc(
        **req.model_dump(exclude={"conversions"}, exclude_none=True),
        conversions=[item.model_dump() for item in req.conversions],
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return dict(result.data or {})


@router.post("/calc/hit-dps/compare", response_model=None)
def compare_hit_dps_scenarios(
    req: HitDpsCompareRequest, _caller: str = Depends(require_user)
) -> dict[str, Any]:
    result = hit_dps_compare(scenarios=[
        {"name": scenario.name, "inputs": scenario.inputs.model_dump(exclude_none=True)}
        for scenario in req.scenarios
    ])
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return dict(result.data or {})


@router.post("/calc/hit-dps/target-rate", response_model=None)
def calculate_hit_dps_target_rate(
    req: HitDpsTargetRateRequest, _caller: str = Depends(require_user)
) -> dict[str, Any]:
    result = hit_dps_target_rate(
        target_dps=req.target_dps,
        metric=req.metric,
        inputs=req.inputs.model_dump(exclude={"action_rate"}, exclude_none=True),
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return dict(result.data or {})


@router.post("/calc/rotation-dps", response_model=None)
def calculate_rotation_dps(
    req: RotationDpsRequest, _caller: str = Depends(require_user)
) -> dict[str, Any]:
    result = rotation_dps_calc(**req.model_dump(exclude_none=True))
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return dict(result.data or {})


@router.post("/calc/rotation-dps/compare", response_model=None)
def compare_rotation_dps_scenarios(
    req: RotationCompareRequest, _caller: str = Depends(require_user)
) -> dict[str, Any]:
    result = rotation_dps_compare(scenarios=[
        {"name": scenario.name, "inputs": scenario.inputs.model_dump(exclude_none=True)}
        for scenario in req.scenarios
    ])
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return dict(result.data or {})


@router.post("/calc/rotation-dps/target-multiplier", response_model=None)
def calculate_rotation_target_multiplier(
    req: RotationTargetRequest, _caller: str = Depends(require_user)
) -> dict[str, Any]:
    result = rotation_dps_target_multiplier(
        target_dps=req.target_dps, metric=req.metric,
        inputs=req.inputs.model_dump(exclude_none=True),
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail=result.error)
    return dict(result.data or {})
