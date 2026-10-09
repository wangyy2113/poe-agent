"""Boss 显式时间轴上的概率防御与生存分布。

伤害区间、抗性、护甲与恢复时间窗由现有 Boss 时间轴负责。本模块只把调用方明确给出的
攻击/法术分类和概率防御叠加到已经结算的承伤上。低伤与高伤分别计算，不假设区间内的
伤害掷骰分布；因此 ``high_roll`` 是保守风险轨，而不是声称游戏每击都掷到最大值。

状态按防御池桶做有界离散动态规划。桶宽是响应契约的一部分，所有概率均报告量化误差
上界；它们是独立事件模型下的估计，不是确定性保证。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal, Sequence

from calc.boss_timeline import TimedDamageEvent


HitType = Literal["attack", "spell"]
DamageBasis = Literal["low_roll", "high_roll"]
MAX_EVENTS = 100
MAX_BUCKETS = 5000
MAX_REPORTED_BINS = 100


class BossProbabilityError(ValueError):
    """概率防御输入不完整或超出有界计算范围。"""


@dataclass(frozen=True)
class ProbabilisticEvent:
    damage: TimedDamageEvent
    hit_type: HitType


@dataclass(frozen=True)
class DefenceProfile:
    attack_hit_chance_pct: float = 100.0
    attack_block_chance_pct: float = 0.0
    spell_block_chance_pct: float = 0.0
    spell_suppression_chance_pct: float = 0.0
    blocked_damage_taken_pct: float = 0.0
    suppressed_damage_taken_pct: float = 60.0


@dataclass(frozen=True)
class DamageOutcome:
    label: str
    probability_pct: float
    damage_multiplier: float


@dataclass(frozen=True)
class SurvivalDistributionBin:
    remaining_pool: float
    probability_pct: float
    depleted: bool = False


@dataclass(frozen=True)
class ProbabilityStep:
    hit_index: int
    at_seconds: float
    skill: str
    hit_type: HitType
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


@dataclass(frozen=True)
class RollDistribution:
    damage_basis: DamageBasis
    survival_probability_pct: float
    depletion_probability_pct: float
    expected_attempted_damage: float
    expected_applied_damage_before_depletion: float
    expected_remaining_pool: float
    expected_remaining_pool_if_alive: float | None
    remaining_pool_p05: float
    remaining_pool_p10: float
    remaining_pool_p50: float
    loss_from_full_pool_p50: float
    loss_from_full_pool_p90: float
    loss_from_full_pool_p95: float
    most_likely_depletion_hit: int | None
    most_likely_depletion_hit_probability_pct: float
    reported_distribution_probability_pct: float
    distribution_truncated: bool
    final_distribution: tuple[SurvivalDistributionBin, ...]
    steps: tuple[ProbabilityStep, ...]


@dataclass(frozen=True)
class ProbabilisticSequenceResult:
    pool: float
    recovery_per_second: float
    recovery_delay_seconds: float
    bucket_size: float
    bucket_count: int
    quantization_error_bound: float
    hit_count: int
    duration_seconds: float
    profile: DefenceProfile
    low_roll: RollDistribution
    high_roll: RollDistribution
    deterministic_worst_case_survives: bool
    deterministic_worst_case_ending_pool: float
    probability_model: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProbabilisticScenarioResult:
    name: str
    high_roll_survival_probability_pct: float
    high_roll_remaining_pool_p05: float
    high_roll_expected_remaining_pool: float
    deterministic_worst_case_survives: bool
    result: ProbabilisticSequenceResult


@dataclass(frozen=True)
class ProbabilisticSequenceComparison:
    scenarios: tuple[ProbabilisticScenarioResult, ...]
    best_high_roll_survival_scenario: str
    best_high_roll_p05_margin_scenario: str
    best_high_roll_expected_pool_scenario: str
    deterministic_survivors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(
    value: object, label: str, *, minimum: float = 0.0, maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BossProbabilityError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise BossProbabilityError(f"{label} 必须是大于等于 {minimum:g} 的有限数字")
    if maximum is not None and result > maximum:
        raise BossProbabilityError(f"{label} 不能超过 {maximum:g}")
    return result


def _validate_profile(profile: DefenceProfile) -> DefenceProfile:
    if not isinstance(profile, DefenceProfile):
        raise BossProbabilityError("profile 必须是 DefenceProfile")
    values = {
        field: _number(getattr(profile, field), field, maximum=100)
        for field in profile.__dataclass_fields__
    }
    return DefenceProfile(**values)


def _validate_events(events: Sequence[ProbabilisticEvent]) -> tuple[ProbabilisticEvent, ...]:
    if not isinstance(events, (list, tuple)) or not 1 <= len(events) <= MAX_EVENTS:
        raise BossProbabilityError(f"events 必须包含 1..{MAX_EVENTS} 次命中")
    normalized: list[ProbabilisticEvent] = []
    previous = -1.0
    for index, event in enumerate(events):
        if not isinstance(event, ProbabilisticEvent):
            raise BossProbabilityError(f"events[{index}] 必须是 ProbabilisticEvent")
        if event.hit_type not in {"attack", "spell"}:
            raise BossProbabilityError(f"events[{index}].hit_type 只支持 attack 或 spell")
        damage = event.damage
        if not isinstance(damage, TimedDamageEvent):
            raise BossProbabilityError(f"events[{index}].damage 必须是 TimedDamageEvent")
        at = _number(damage.at_seconds, f"events[{index}].at_seconds")
        if at < previous:
            raise BossProbabilityError("events 必须按 at_seconds 非递减排列")
        _number(damage.taken_low, f"events[{index}].taken_low")
        _number(damage.taken_high, f"events[{index}].taken_high")
        if damage.taken_low > damage.taken_high:
            raise BossProbabilityError(f"events[{index}] 的低承伤不能大于高承伤")
        normalized.append(event)
        previous = at
    return tuple(normalized)


def damage_outcomes(hit_type: HitType, profile: DefenceProfile) -> tuple[DamageOutcome, ...]:
    """返回一次事件的互斥伤害倍率分支。"""
    checked = _validate_profile(profile)
    if hit_type == "attack":
        connects = checked.attack_hit_chance_pct / 100
        block = checked.attack_block_chance_pct / 100
        rows = (
            DamageOutcome("miss", (1 - connects) * 100, 0.0),
            DamageOutcome(
                "blocked", connects * block * 100,
                checked.blocked_damage_taken_pct / 100,
            ),
            DamageOutcome("full", connects * (1 - block) * 100, 1.0),
        )
    elif hit_type == "spell":
        block = checked.spell_block_chance_pct / 100
        suppress = checked.spell_suppression_chance_pct / 100
        rows = (
            DamageOutcome(
                "blocked", block * 100, checked.blocked_damage_taken_pct / 100,
            ),
            DamageOutcome(
                "suppressed", (1 - block) * suppress * 100,
                checked.suppressed_damage_taken_pct / 100,
            ),
            DamageOutcome("full", (1 - block) * (1 - suppress) * 100, 1.0),
        )
    else:
        raise BossProbabilityError("hit_type 只支持 attack 或 spell")
    merged: dict[float, tuple[str, float]] = {}
    for row in rows:
        if row.probability_pct <= 1e-12:
            continue
        multiplier = round(row.damage_multiplier, 12)
        label, probability = merged.get(multiplier, (row.label, 0.0))
        merged[multiplier] = (label, probability + row.probability_pct)
    return tuple(
        DamageOutcome(label, probability, multiplier)
        for multiplier, (label, probability) in sorted(merged.items())
    )


def _quantize(value: float, *, pool: float, bucket_size: float) -> float:
    if value <= 0:
        return 0.0
    bucket = round(value / bucket_size)
    representative = bucket * bucket_size
    # 存活态不能量化到代表“耗尽”的 0；最低正桶用半桶中心，仍保持 ±半桶误差界。
    if representative <= 0:
        representative = bucket_size / 2
    return round(min(pool, representative), 9)


def _quantile(
    alive: dict[float, float], depleted_probability: float, quantile: float,
) -> float:
    target = quantile
    cumulative = depleted_probability
    if cumulative + 1e-15 >= target:
        return 0.0
    for remaining, probability in sorted(alive.items()):
        cumulative += probability
        if cumulative + 1e-15 >= target:
            return remaining
    return max(alive, default=0.0)


def _outcome_summary(
    outcomes: tuple[DamageOutcome, ...],
) -> tuple[float, float, float]:
    avoided = sum(row.probability_pct for row in outcomes if row.damage_multiplier == 0)
    partial = sum(
        row.probability_pct for row in outcomes if 0 < row.damage_multiplier < 1
    )
    full = sum(row.probability_pct for row in outcomes if row.damage_multiplier >= 1)
    return avoided, partial, full


def _distribution(
    events: tuple[ProbabilisticEvent, ...], *, pool: float, rate: float, delay: float,
    profile: DefenceProfile, bucket_size: float, high_roll: bool,
) -> RollDistribution:
    alive: dict[float, float] = {pool: 1.0}
    depleted = 0.0
    previous_time: float | None = None
    expected_attempted = 0.0
    expected_applied = 0.0
    depletion_by_hit: list[float] = []
    steps: list[ProbabilityStep] = []
    for index, event in enumerate(events, start=1):
        damage = event.damage.taken_high if high_roll else event.damage.taken_low
        elapsed = 0.0 if previous_time is None else event.damage.at_seconds - previous_time
        eligible = max(0.0, elapsed - delay)
        outcomes = damage_outcomes(event.hit_type, profile)
        expected_multiplier = sum(
            row.probability_pct / 100 * row.damage_multiplier for row in outcomes
        )
        expected_attempted += damage * expected_multiplier
        next_alive: dict[float, float] = {}
        depleted_on_hit = 0.0
        for remaining, state_probability in alive.items():
            before = min(pool, remaining + rate * eligible)
            for outcome in outcomes:
                branch_probability = state_probability * outcome.probability_pct / 100
                if branch_probability <= 1e-18:
                    continue
                applied = damage * outcome.damage_multiplier
                after = before - applied
                expected_applied += branch_probability * applied
                if after <= 0:
                    depleted_on_hit += branch_probability
                    continue
                bucket = _quantize(after, pool=pool, bucket_size=bucket_size)
                next_alive[bucket] = next_alive.get(bucket, 0.0) + branch_probability
        depleted += depleted_on_hit
        alive = next_alive
        if len(alive) > MAX_BUCKETS:
            raise BossProbabilityError(
                f"离散状态超过 {MAX_BUCKETS}；请增大 bucket_size"
            )
        survival = sum(alive.values())
        expected_remaining = sum(value * probability for value, probability in alive.items())
        avoided, partial, full = _outcome_summary(outcomes)
        steps.append(ProbabilityStep(
            hit_index=index, at_seconds=event.damage.at_seconds,
            skill=event.damage.skill, hit_type=event.hit_type,
            damage_type=event.damage.damage_type, damage=round(damage, 6),
            recovery_eligible_seconds=eligible,
            avoid_probability_pct=round(avoided, 9),
            partial_damage_probability_pct=round(partial, 9),
            full_damage_probability_pct=round(full, 9),
            survival_probability_pct=round(survival * 100, 9),
            depletion_probability_on_hit_pct=round(depleted_on_hit * 100, 9),
            cumulative_depletion_probability_pct=round(depleted * 100, 9),
            expected_remaining_pool=round(expected_remaining, 6),
            remaining_pool_p10=round(_quantile(alive, depleted, 0.10), 6),
            alive_state_count=len(alive),
        ))
        depletion_by_hit.append(depleted_on_hit)
        previous_time = event.damage.at_seconds

    survival = sum(alive.values())
    expected_remaining = sum(value * probability for value, probability in alive.items())
    conditional = expected_remaining / survival if survival > 0 else None
    ordered_bins = [
        SurvivalDistributionBin(
            remaining_pool=value, probability_pct=round(probability * 100, 9)
        )
        for value, probability in sorted(
            alive.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    distribution_truncated = len(ordered_bins) > MAX_REPORTED_BINS
    reported = ordered_bins[:MAX_REPORTED_BINS]
    if depleted > 0:
        reported.insert(0, SurvivalDistributionBin(
            remaining_pool=0.0, probability_pct=round(depleted * 100, 9),
            depleted=True,
        ))
    reported_probability = sum(row.probability_pct for row in reported)
    p05 = _quantile(alive, depleted, 0.05)
    p10 = _quantile(alive, depleted, 0.10)
    p50 = _quantile(alive, depleted, 0.50)
    most_likely_hit = None
    most_likely_probability = 0.0
    if depletion_by_hit and max(depletion_by_hit) > 0:
        most_likely_probability = max(depletion_by_hit)
        most_likely_hit = depletion_by_hit.index(most_likely_probability) + 1
    return RollDistribution(
        damage_basis="high_roll" if high_roll else "low_roll",
        survival_probability_pct=round(survival * 100, 9),
        depletion_probability_pct=round(depleted * 100, 9),
        expected_attempted_damage=round(expected_attempted, 6),
        expected_applied_damage_before_depletion=round(expected_applied, 6),
        expected_remaining_pool=round(expected_remaining, 6),
        expected_remaining_pool_if_alive=(
            round(conditional, 6) if conditional is not None else None
        ),
        remaining_pool_p05=round(p05, 6),
        remaining_pool_p10=round(p10, 6),
        remaining_pool_p50=round(p50, 6),
        loss_from_full_pool_p50=round(pool - p50, 6),
        loss_from_full_pool_p90=round(pool - p10, 6),
        loss_from_full_pool_p95=round(pool - p05, 6),
        most_likely_depletion_hit=most_likely_hit,
        most_likely_depletion_hit_probability_pct=round(
            most_likely_probability * 100, 9
        ),
        reported_distribution_probability_pct=round(reported_probability, 9),
        distribution_truncated=distribution_truncated,
        final_distribution=tuple(reported), steps=tuple(steps),
    )


def assess_probabilistic_sequence(
    *, events: Sequence[ProbabilisticEvent], pool: float,
    profile: DefenceProfile = DefenceProfile(), recovery_per_second: float = 0.0,
    recovery_delay_seconds: float = 0.0, bucket_size: float | None = None,
) -> ProbabilisticSequenceResult:
    """计算低/高伤害轨上的存活分布与确定性最坏结果。"""
    normalized = _validate_events(events)
    maximum_pool = _number(pool, "pool")
    if maximum_pool <= 0:
        raise BossProbabilityError("pool 必须大于 0")
    rate = _number(recovery_per_second, "recovery_per_second")
    delay = _number(recovery_delay_seconds, "recovery_delay_seconds")
    checked_profile = _validate_profile(profile)
    default_bucket = max(maximum_pool / 2000, 0.01)
    bucket = default_bucket if bucket_size is None else _number(bucket_size, "bucket_size")
    if bucket <= 0:
        raise BossProbabilityError("bucket_size 必须大于 0")
    bucket_count = math.ceil(maximum_pool / bucket)
    if bucket_count > MAX_BUCKETS:
        raise BossProbabilityError(
            f"bucket_size 过小，会产生 {bucket_count} 个桶；上限为 {MAX_BUCKETS}"
        )
    low = _distribution(
        normalized, pool=maximum_pool, rate=rate, delay=delay,
        profile=checked_profile, bucket_size=bucket, high_roll=False,
    )
    high = _distribution(
        normalized, pool=maximum_pool, rate=rate, delay=delay,
        profile=checked_profile, bucket_size=bucket, high_roll=True,
    )
    remaining = maximum_pool
    previous_time: float | None = None
    deterministic_survives = True
    for event in normalized:
        elapsed = 0.0 if previous_time is None else event.damage.at_seconds - previous_time
        remaining = min(maximum_pool, remaining + rate * max(0.0, elapsed - delay))
        remaining -= event.damage.taken_high
        if remaining <= 0:
            deterministic_survives = False
            break
        previous_time = event.damage.at_seconds
    return ProbabilisticSequenceResult(
        pool=maximum_pool, recovery_per_second=rate,
        recovery_delay_seconds=delay, bucket_size=round(bucket, 9),
        bucket_count=bucket_count,
        quantization_error_bound=round(len(normalized) * bucket / 2, 9),
        hit_count=len(normalized), duration_seconds=normalized[-1].damage.at_seconds,
        profile=checked_profile, low_roll=low, high_roll=high,
        deterministic_worst_case_survives=deterministic_survives,
        deterministic_worst_case_ending_pool=round(remaining, 6),
        probability_model=(
            "各事件独立；攻击按未命中→格挡→完整承伤，法术按格挡→压制→完整承伤；"
            "低/高伤害轨分开，不假设伤害区间内的掷骰分布"
        ),
    )


def compare_probabilistic_sequences(
    scenarios: Sequence[tuple[str, ProbabilisticSequenceResult]],
) -> ProbabilisticSequenceComparison:
    if not isinstance(scenarios, (list, tuple)) or not 2 <= len(scenarios) <= 10:
        raise BossProbabilityError("scenarios 必须包含 2..10 个方案")
    names = [str(name).strip() for name, _ in scenarios]
    if any(not name or len(name) > 80 for name in names) or len(set(names)) != len(names):
        raise BossProbabilityError("方案名称必须为不重复的 1..80 个字符")
    rows = tuple(ProbabilisticScenarioResult(
        name=name,
        high_roll_survival_probability_pct=result.high_roll.survival_probability_pct,
        high_roll_remaining_pool_p05=result.high_roll.remaining_pool_p05,
        high_roll_expected_remaining_pool=result.high_roll.expected_remaining_pool,
        deterministic_worst_case_survives=result.deterministic_worst_case_survives,
        result=result,
    ) for name, result in scenarios)
    return ProbabilisticSequenceComparison(
        scenarios=rows,
        best_high_roll_survival_scenario=max(
            rows, key=lambda row: (
                row.high_roll_survival_probability_pct,
                row.high_roll_remaining_pool_p05,
            )
        ).name,
        best_high_roll_p05_margin_scenario=max(
            rows, key=lambda row: row.high_roll_remaining_pool_p05
        ).name,
        best_high_roll_expected_pool_scenario=max(
            rows, key=lambda row: row.high_roll_expected_remaining_pool
        ).name,
        deterministic_survivors=tuple(
            row.name for row in rows if row.deterministic_worst_case_survives
        ),
    )
