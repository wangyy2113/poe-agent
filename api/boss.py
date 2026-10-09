"""Boss 伤害与防御阈值 API。"""

from __future__ import annotations

from typing import Any, Literal

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
from tools.impl.boss_probability import (
    boss_probabilistic_sequence_check,
    boss_probabilistic_sequence_compare,
)
from tools.impl.boss_layered import (
    boss_layered_sequence_check,
    boss_layered_sequence_compare,
)
from tools.impl.boss_recovery import (
    boss_recovery_sequence_check,
    boss_recovery_sequence_compare,
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


class BossLayeredEvent(BossTimedEvent):
    energy_shield_bypass_pct: float = Field(
        default=0, ge=0, le=100, allow_inf_nan=False,
        description="该击最终承伤中明确绕过能量护盾的比例；不按伤害类型自动猜测。",
    )


class BossLayeredPoolConfigResponse(BaseModel):
    life: float
    energy_shield: float
    mana: float
    mana_before_life_pct: float
    life_recovery_per_second: float
    energy_shield_recovery_per_second: float
    mana_recovery_per_second: float
    energy_shield_recharge_per_second: float
    energy_shield_recharge_delay_seconds: float
    energy_shield_recharge_interrupt_mode: Literal["energy_shield_damage", "any_hit"]


class BossLayeredStepResponse(BaseModel):
    hit_index: int
    at_seconds: float
    elapsed_since_previous_hit: float
    skill: str
    damage_type: str
    raw_damage: float
    taken_damage: float
    energy_shield_bypass_pct: float
    life_before_recovery: float
    energy_shield_before_recovery: float
    mana_before_recovery: float
    life_recovered: float
    energy_shield_recovered: float
    energy_shield_recharged: float
    mana_recovered: float
    recharge_eligible_seconds: float
    life_before_hit: float
    energy_shield_before_hit: float
    mana_before_hit: float
    energy_shield_absorbed: float
    energy_shield_bypassed: float
    mana_target_damage: float
    mana_absorbed: float
    mana_shortfall_to_life: float
    life_damage: float
    life_after_hit: float
    energy_shield_after_hit: float
    mana_after_hit: float
    recharge_interrupted: bool
    depleted: bool


class BossLayeredRollResponse(BaseModel):
    damage_basis: Literal["low_roll", "high_roll"]
    survived: bool
    depletion_hit: int | None = None
    ending_life: float
    ending_energy_shield: float
    ending_mana: float
    lowest_life: float
    lowest_life_hit: int
    total_taken_damage: float
    total_energy_shield_absorbed: float
    total_energy_shield_bypassed: float
    total_mana_absorbed: float
    total_life_damage: float
    total_life_recovered: float
    total_energy_shield_recovered: float
    total_energy_shield_recharged: float
    total_mana_recovered: float
    recharge_interruption_count: int
    recharge_active_seconds: float
    steps: list[BossLayeredStepResponse]


class BossLayerThresholdResponse(BaseModel):
    status: Literal["not_applicable", "reachable", "unreachable"]
    value: int | None = None
    current: float
    shortfall: float | None = None
    keeps_other_layers_fixed: bool = True


class BossLayeredSequenceRequest(BaseModel):
    events: list[BossLayeredEvent] = Field(min_length=1, max_length=100)
    level: int = Field(ge=1, le=100)
    life: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
    energy_shield: float = Field(default=0, ge=0, le=1_000_000_000, allow_inf_nan=False)
    mana: float = Field(default=0, ge=0, le=1_000_000_000, allow_inf_nan=False)
    mana_before_life_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
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
    life_recovery_per_second: float = Field(
        default=0, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    energy_shield_recovery_per_second: float = Field(
        default=0, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    mana_recovery_per_second: float = Field(
        default=0, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    energy_shield_recharge_per_second: float = Field(
        default=0, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    energy_shield_recharge_delay_seconds: float = Field(
        default=2, ge=0, le=3600, allow_inf_nan=False
    )
    energy_shield_recharge_interrupt_mode: Literal[
        "energy_shield_damage", "any_hit"
    ] = "energy_shield_damage"


class BossLayeredSequenceResponse(BaseModel):
    found: bool
    assessable: bool
    verdict: str | None = None
    level: int
    uber: bool
    roll_range: float | None = None
    requested_events: list[BossLayeredEvent]
    config: BossLayeredPoolConfigResponse | None = None
    hit_count: int = 0
    duration_seconds: float | None = None
    outcome: str | None = None
    low_roll: BossLayeredRollResponse | None = None
    high_roll: BossLayeredRollResponse | None = None
    minimum_life_for_any_roll: BossLayerThresholdResponse | None = None
    minimum_life_for_full_range: BossLayerThresholdResponse | None = None
    minimum_energy_shield_for_full_range: BossLayerThresholdResponse | None = None
    minimum_mana_for_full_range: BossLayerThresholdResponse | None = None
    routing_model: str = ""
    reason: str | None = None
    missing_inputs: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    boundary: str
    answer: str


class BossLayeredScenarioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    inputs: BossLayeredSequenceRequest


class BossLayeredCompareRequest(BaseModel):
    scenarios: list[BossLayeredScenarioRequest] = Field(min_length=2, max_length=10)


class BossRecoveryEvent(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    at_seconds: float = Field(ge=0, le=3600, allow_inf_nan=False)
    pool: Literal["life", "energy_shield", "mana"]
    kind: Literal["instant", "timed_rate", "leech"]
    amount: float = Field(default=0, ge=0, le=1_000_000_000, allow_inf_nan=False)
    rate_per_second: float = Field(
        default=0, ge=0, le=1_000_000_000, allow_inf_nan=False
    )
    duration_seconds: float = Field(default=0, ge=0, le=3600, allow_inf_nan=False)
    phase: Literal["before_hit", "after_hit"] = "before_hit"


class BossRecoverySequenceRequest(BossLayeredSequenceRequest):
    recovery_events: list[BossRecoveryEvent] = Field(min_length=0, max_length=100)
    life_leech_rate_cap_pct: float = Field(
        default=20, ge=0, le=1000, allow_inf_nan=False
    )
    energy_shield_leech_rate_cap_pct: float = Field(
        default=10, ge=0, le=1000, allow_inf_nan=False
    )
    mana_leech_rate_cap_pct: float = Field(
        default=20, ge=0, le=1000, allow_inf_nan=False
    )


class BossRecoverySequenceResponse(BaseModel):
    found: bool
    assessable: bool
    verdict: str | None = None
    level: int
    uber: bool
    roll_range: float | None = None
    requested_events: list[BossLayeredEvent]
    requested_recovery_events: list[BossRecoveryEvent] = Field(default_factory=list)
    config: dict[str, Any] | None = None
    recovery_events: list[dict[str, Any]] = Field(default_factory=list)
    hit_count: int = 0
    duration_seconds: float | None = None
    outcome: str | None = None
    low_roll: dict[str, Any] | None = None
    high_roll: dict[str, Any] | None = None
    active_recovery_impact: dict[str, Any] | None = None
    minimum_life_for_full_range: BossLayerThresholdResponse | None = None
    minimum_energy_shield_for_full_range: BossLayerThresholdResponse | None = None
    minimum_mana_for_full_range: BossLayerThresholdResponse | None = None
    event_ordering: str = ""
    recovery_attribution_model: str = ""
    routing_model: str = ""
    reason: str | None = None
    missing_inputs: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    boundary: str
    answer: str


class BossRecoveryScenarioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    inputs: BossRecoverySequenceRequest


class BossRecoveryCompareRequest(BaseModel):
    scenarios: list[BossRecoveryScenarioRequest] = Field(min_length=2, max_length=10)


class BossProbabilisticEvent(BossTimedEvent):
    hit_type: Literal["attack", "spell"]


class BossProbabilityProfileResponse(BaseModel):
    attack_hit_chance_pct: float
    attack_block_chance_pct: float
    spell_block_chance_pct: float
    spell_suppression_chance_pct: float
    blocked_damage_taken_pct: float
    suppressed_damage_taken_pct: float


class BossProbabilityDistributionBinResponse(BaseModel):
    remaining_pool: float
    probability_pct: float
    depleted: bool = False


class BossProbabilityStepResponse(BaseModel):
    hit_index: int
    at_seconds: float
    skill: str
    hit_type: Literal["attack", "spell"]
    damage_type: str
    damage: float
    recovery_eligible_seconds: float
    avoid_probability_pct: float
    partial_damage_probability_pct: float
    full_damage_probability_pct: float
    survival_probability_pct: float
    depletion_probability_on_hit_pct: float
    cumulative_depletion_probability_pct: float
    expected_remaining_pool: float
    remaining_pool_p10: float
    alive_state_count: int


class BossRollDistributionResponse(BaseModel):
    damage_basis: Literal["low_roll", "high_roll"]
    survival_probability_pct: float
    depletion_probability_pct: float
    expected_attempted_damage: float
    expected_applied_damage_before_depletion: float
    expected_remaining_pool: float
    expected_remaining_pool_if_alive: float | None = None
    remaining_pool_p05: float
    remaining_pool_p10: float
    remaining_pool_p50: float
    loss_from_full_pool_p50: float
    loss_from_full_pool_p90: float
    loss_from_full_pool_p95: float
    most_likely_depletion_hit: int | None = None
    most_likely_depletion_hit_probability_pct: float
    reported_distribution_probability_pct: float
    distribution_truncated: bool
    final_distribution: list[BossProbabilityDistributionBinResponse]
    steps: list[BossProbabilityStepResponse]


class BossProbabilisticSequenceRequest(BaseModel):
    events: list[BossProbabilisticEvent] = Field(min_length=1, max_length=100)
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
    attack_hit_chance_pct: float = Field(default=100, ge=0, le=100, allow_inf_nan=False)
    attack_block_chance_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    spell_block_chance_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    spell_suppression_chance_pct: float = Field(
        default=0, ge=0, le=100, allow_inf_nan=False
    )
    blocked_damage_taken_pct: float = Field(default=0, ge=0, le=100, allow_inf_nan=False)
    suppressed_damage_taken_pct: float = Field(
        default=60, ge=0, le=100, allow_inf_nan=False,
        description="当前默认按阻挡 40% 伤害，即承受 60%；特殊效果应显式覆盖。",
    )
    bucket_size: float | None = Field(
        default=None, gt=0, le=1_000_000_000, allow_inf_nan=False,
        description="省略时取 max(pool/2000, 0.01)，最多 5000 个离散池桶。",
    )


class BossProbabilisticSequenceResponse(BaseModel):
    found: bool
    assessable: bool
    verdict: str | None = None
    level: int
    uber: bool
    roll_range: float | None = None
    requested_events: list[BossProbabilisticEvent]
    pool: float | None = None
    recovery_per_second: float = 0
    recovery_delay_seconds: float = 0
    bucket_size: float | None = None
    bucket_count: int | None = None
    quantization_error_bound: float | None = None
    hit_count: int = 0
    duration_seconds: float | None = None
    profile: BossProbabilityProfileResponse | None = None
    low_roll: BossRollDistributionResponse | None = None
    high_roll: BossRollDistributionResponse | None = None
    deterministic_worst_case_survives: bool | None = None
    deterministic_worst_case_ending_pool: float | None = None
    probability_model: str = ""
    reason: str | None = None
    missing_inputs: list[str] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
    boundary: str
    answer: str


class BossProbabilisticScenarioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    inputs: BossProbabilisticSequenceRequest


class BossProbabilisticCompareRequest(BaseModel):
    scenarios: list[BossProbabilisticScenarioRequest] = Field(min_length=2, max_length=10)


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


@router.post(
    "/boss/probabilistic-sequence",
    response_model=BossProbabilisticSequenceResponse,
)
def probabilistic_sequence(
    req: BossProbabilisticSequenceRequest,
    _caller: str = Depends(require_user),
) -> BossProbabilisticSequenceResponse:
    result = boss_probabilistic_sequence_check(**req.model_dump())
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_probabilistic_sequence_query",
            "reason": result.error,
        })
    return BossProbabilisticSequenceResponse(**(result.data or {}))


@router.post(
    "/boss/layered-sequence",
    response_model=BossLayeredSequenceResponse,
)
def layered_sequence(
    req: BossLayeredSequenceRequest,
    _caller: str = Depends(require_user),
) -> BossLayeredSequenceResponse:
    result = boss_layered_sequence_check(**req.model_dump())
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_layered_sequence_query",
            "reason": result.error,
        })
    return BossLayeredSequenceResponse(**(result.data or {}))


@router.post("/boss/layered-sequence/compare", response_model=None)
def compare_layered_sequences(
    req: BossLayeredCompareRequest,
    _caller: str = Depends(require_user),
) -> dict[str, Any]:
    result = boss_layered_sequence_compare(scenarios=[
        {"name": scenario.name, "inputs": scenario.inputs.model_dump()}
        for scenario in req.scenarios
    ])
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_layered_sequence_comparison",
            "reason": result.error,
        })
    return dict(result.data or {})


@router.post(
    "/boss/recovery-sequence",
    response_model=BossRecoverySequenceResponse,
)
def recovery_sequence(
    req: BossRecoverySequenceRequest,
    _caller: str = Depends(require_user),
) -> BossRecoverySequenceResponse:
    result = boss_recovery_sequence_check(**req.model_dump())
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_recovery_sequence_query",
            "reason": result.error,
        })
    return BossRecoverySequenceResponse(**(result.data or {}))


@router.post("/boss/recovery-sequence/compare", response_model=None)
def compare_recovery_sequences(
    req: BossRecoveryCompareRequest,
    _caller: str = Depends(require_user),
) -> dict[str, Any]:
    result = boss_recovery_sequence_compare(scenarios=[
        {"name": scenario.name, "inputs": scenario.inputs.model_dump()}
        for scenario in req.scenarios
    ])
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_recovery_sequence_comparison",
            "reason": result.error,
        })
    return dict(result.data or {})


@router.post("/boss/probabilistic-sequence/compare", response_model=None)
def compare_probabilistic_sequences(
    req: BossProbabilisticCompareRequest,
    _caller: str = Depends(require_user),
) -> dict[str, Any]:
    result = boss_probabilistic_sequence_compare(scenarios=[
        {"name": scenario.name, "inputs": scenario.inputs.model_dump()}
        for scenario in req.scenarios
    ])
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_boss_probabilistic_sequence_comparison",
            "reason": result.error,
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
