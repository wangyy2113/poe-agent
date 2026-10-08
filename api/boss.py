"""Boss 伤害与防御阈值 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from tools.impl.boss import (
    boss_defence_threshold,
    boss_risk_audit,
    boss_sequence_check,
    boss_survival_check,
)
from tools.impl.boss_timeline import (
    boss_timed_sequence_check,
    boss_timed_sequence_compare,
)

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
    earliest_depletion_hit: int | None = None
    latest_depletion_hit: int | None = None
    guaranteed_survivable_hits: int | None = None
    maximum_survivable_hits: int | None = None
    minimum_pool_for_any_roll: int | None = None
    minimum_pool_for_full_range: int | None = None
    pool_shortfall_for_any_roll: float | None = None
    pool_shortfall_for_full_range: float | None = None
    required_resist_for_full_range_pct: int | None = None
    resist_shortfall_pct: float | None = None
    required_resist_status: str = "not_applicable"
    required_armour_for_full_range: int | None = None
    armour_shortfall: float | None = None
    required_armour_status: str = "not_applicable"
    capacity_reason: str = ""
    max_raw_hit: float | None = None
    margin_low: float | None = None
    margin_high: float | None = None
    outcome: str | None = None
    reason: str | None = None
    candidates: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    boundary: str
    answer: str


class BossDefenceThresholdRequest(BaseModel):
    skill: str = Field(min_length=2, max_length=120)
    level: int = Field(ge=1, le=100)
    target_pool: float | None = Field(
        default=None, gt=0, le=1_000_000_000, allow_inf_nan=False
    )
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


class BossDefenceThresholdResponse(BossSurvivalResponse):
    target_pool: float | None = None


class BossSequenceEntry(BaseModel):
    skill: str = Field(min_length=2, max_length=120)
    count: int = Field(default=1, ge=1, le=100)


class BossSequenceRequest(BaseModel):
    sequence: list[BossSequenceEntry] = Field(min_length=1, max_length=100)
    level: int = Field(ge=1, le=100)
    pool: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
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


class BossSequenceStepResponse(BaseModel):
    hit_index: int
    skill: str
    damage_type: str
    raw_low: float
    raw_high: float
    taken_low: float
    taken_high: float
    cumulative_taken_low: float
    cumulative_taken_high: float
    remaining_best_case: float
    remaining_worst_case: float


class BossSequenceDamageTotalResponse(BaseModel):
    damage_type: str
    hit_count: int
    raw_low: float
    raw_high: float
    taken_low: float
    taken_high: float


class BossSequenceResponse(BaseModel):
    found: bool
    assessable: bool
    verdict: str | None = None
    level: int
    uber: bool
    roll_range: float | None = None
    pool: float | None
    hit_count: int = 0
    requested_sequence: list[BossSequenceEntry]
    steps: list[BossSequenceStepResponse] = Field(default_factory=list)
    total_taken_low: float | None = None
    total_taken_high: float | None = None
    remaining_best_case: float | None = None
    remaining_worst_case: float | None = None
    earliest_possible_depletion_hit: int | None = None
    guaranteed_depletion_hit: int | None = None
    minimum_pool_for_any_roll: int | None = None
    minimum_pool_for_full_range: int | None = None
    largest_single_hit_taken_low: float | None = None
    largest_single_hit_taken_high: float | None = None
    most_dangerous_hit_index: int | None = None
    most_dangerous_skill: str | None = None
    most_dangerous_damage_type: str | None = None
    damage_type_totals: list[BossSequenceDamageTotalResponse] = Field(default_factory=list)
    failed_sequence_index: int | None = None
    reason: str | None = None
    candidates: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    boundary: str
    answer: str


class BossTimedEvent(BaseModel):
    skill: str = Field(min_length=2, max_length=120)
    at_seconds: float = Field(ge=0, le=3600, allow_inf_nan=False)


class BossTimedSequenceRequest(BaseModel):
    events: list[BossTimedEvent] = Field(min_length=1, max_length=100)
    level: int = Field(ge=1, le=100)
    pool: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
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
    recovery_per_second: float = Field(
        default=0, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    recovery_delay_seconds: float = Field(
        default=0, ge=0, le=3600, allow_inf_nan=False
    )


class BossTimedSequenceStepResponse(BaseModel):
    hit_index: int
    at_seconds: float
    elapsed_since_previous_hit: float
    recovery_eligible_seconds: float
    skill: str
    damage_type: str
    raw_low: float
    raw_high: float
    taken_low: float
    taken_high: float
    best_case_recovered: float
    worst_case_recovered: float
    best_case_pool_before_hit: float
    worst_case_pool_before_hit: float
    best_case_remaining: float
    worst_case_remaining: float
    best_case_depleted: bool
    worst_case_depleted: bool


class BossTimedSequenceResponse(BaseModel):
    found: bool
    assessable: bool
    verdict: str | None = None
    level: int
    uber: bool
    roll_range: float | None = None
    requested_events: list[BossTimedEvent]
    pool: float | None = None
    recovery_per_second: float = 0
    recovery_delay_seconds: float = 0
    duration_seconds: float | None = None
    hit_count: int = 0
    total_taken_low: float | None = None
    total_taken_high: float | None = None
    total_recovered_best_case: float | None = None
    total_recovered_worst_case: float | None = None
    ending_remaining_best_case: float | None = None
    ending_remaining_worst_case: float | None = None
    lowest_remaining_best_case: float | None = None
    lowest_remaining_worst_case: float | None = None
    lowest_remaining_best_case_hit: int | None = None
    lowest_remaining_worst_case_hit: int | None = None
    earliest_possible_depletion_hit: int | None = None
    guaranteed_depletion_hit: int | None = None
    minimum_pool_for_any_roll: int | None = None
    minimum_pool_for_full_range: int | None = None
    required_recovery_for_any_roll: float | None = None
    required_recovery_for_full_range: float | None = None
    required_recovery_for_any_roll_exclusive: bool = False
    required_recovery_for_full_range_exclusive: bool = False
    required_recovery_for_any_roll_status: str | None = None
    required_recovery_for_full_range_status: str | None = None
    outcome: str | None = None
    steps: list[BossTimedSequenceStepResponse] = Field(default_factory=list)
    failed_sequence_index: int | None = None
    reason: str | None = None
    candidates: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    boundary: str
    answer: str


class BossTimedScenarioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    inputs: BossTimedSequenceRequest


class BossTimedCompareRequest(BaseModel):
    scenarios: list[BossTimedScenarioRequest] = Field(min_length=2, max_length=10)


class BossRiskAuditRequest(BaseModel):
    level: int = Field(ge=1, le=100)
    pool: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
    hit_count: int = Field(default=1, ge=1, le=100)
    uber: bool = False
    roll_range: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    fire_resist_pct: float | None = Field(default=None, ge=-200, le=90, allow_inf_nan=False)
    cold_resist_pct: float | None = Field(default=None, ge=-200, le=90, allow_inf_nan=False)
    lightning_resist_pct: float | None = Field(default=None, ge=-200, le=90, allow_inf_nan=False)
    chaos_resist_pct: float | None = Field(default=None, ge=-200, le=90, allow_inf_nan=False)
    armour: float | None = Field(default=None, ge=0, le=10_000_000_000, allow_inf_nan=False)
    flat_physical_reduction_pct: float = Field(
        default=0, ge=0, le=90, allow_inf_nan=False
    )
    taken_mult: float = Field(default=1, gt=0, allow_inf_nan=False)
    overwhelm_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)


class BossRiskAuditSkillResponse(BaseModel):
    skill: str
    damage_type: str | None = None
    assessable: bool
    verdict: str | None = None
    outcome: str | None = None
    risk_rank: int | None = None
    raw_low: float | None = None
    raw_high: float | None = None
    taken_low: float | None = None
    taken_high: float | None = None
    total_taken_low: float | None = None
    total_taken_high: float | None = None
    margin_low: float | None = None
    margin_high: float | None = None
    worst_case_pool_ratio_pct: float | None = None
    minimum_pool_for_full_range: int | None = None
    pool_shortfall_for_full_range: float | None = None
    required_resist_for_full_range_pct: int | None = None
    resist_shortfall_pct: float | None = None
    required_resist_status: str = "not_applicable"
    required_armour_for_full_range: int | None = None
    armour_shortfall: float | None = None
    required_armour_status: str = "not_applicable"
    penetration_pct: float | None = None
    effective_resist_pct: float | None = None
    reason: str = ""
    missing_inputs: list[str] = Field(default_factory=list)


class BossRiskAuditResponse(BaseModel):
    found: bool
    assessable: bool
    verdict: str | None = None
    known_worst_outcome: str | None = None
    assessment_status: str
    level: int
    uber: bool
    roll_range: float | None = None
    pool: float | None
    hit_count: int = 1
    total_skill_count: int
    assessed_skill_count: int
    unknown_skill_count: int
    verdict_counts: dict[str, int] = Field(default_factory=dict)
    most_dangerous_skill: str | None = None
    most_dangerous_damage_type: str | None = None
    most_dangerous_taken_low: float | None = None
    most_dangerous_taken_high: float | None = None
    most_dangerous_total_taken_low: float | None = None
    most_dangerous_total_taken_high: float | None = None
    most_dangerous_pool_ratio_pct: float | None = None
    minimum_pool_for_all_assessed_skills: int | None = None
    pool_shortfall_for_all_assessed_skills: float | None = None
    pool_bottleneck_skill: str | None = None
    defence_gap_skill_count: int = 0
    single_axis_reachable_gap_count: int = 0
    single_axis_unreachable_gap_count: int = 0
    skills: list[BossRiskAuditSkillResponse] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    reason: str = ""
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


@router.post("/boss/threshold", response_model=BossDefenceThresholdResponse)
def defence_threshold(
    req: BossDefenceThresholdRequest, _caller: str = Depends(require_user)
) -> BossDefenceThresholdResponse:
    result = boss_defence_threshold(**req.model_dump())
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_defence_threshold_query", "reason": result.error,
        })
    return BossDefenceThresholdResponse(**(result.data or {}))


@router.post("/boss/sequence", response_model=BossSequenceResponse)
def sequence(
    req: BossSequenceRequest, _caller: str = Depends(require_user)
) -> BossSequenceResponse:
    payload = req.model_dump()
    result = boss_sequence_check(**payload)
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_sequence_query", "reason": result.error,
        })
    return BossSequenceResponse(**(result.data or {}))


@router.post("/boss/timed-sequence", response_model=BossTimedSequenceResponse)
def timed_sequence(
    req: BossTimedSequenceRequest, _caller: str = Depends(require_user)
) -> BossTimedSequenceResponse:
    result = boss_timed_sequence_check(**req.model_dump())
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_timed_sequence_query", "reason": result.error,
        })
    return BossTimedSequenceResponse(**(result.data or {}))


@router.post("/boss/timed-sequence/compare", response_model=None)
def compare_timed_sequences(
    req: BossTimedCompareRequest, _caller: str = Depends(require_user)
) -> dict[str, Any]:
    result = boss_timed_sequence_compare(scenarios=[
        {"name": scenario.name, "inputs": scenario.inputs.model_dump()}
        for scenario in req.scenarios
    ])
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_timed_sequence_comparison", "reason": result.error,
        })
    return dict(result.data or {})


@router.post("/boss/audit", response_model=BossRiskAuditResponse)
def risk_audit(
    req: BossRiskAuditRequest, _caller: str = Depends(require_user)
) -> BossRiskAuditResponse:
    result = boss_risk_audit(**req.model_dump())
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_risk_audit_query", "reason": result.error,
        })
    return BossRiskAuditResponse(**(result.data or {}))
