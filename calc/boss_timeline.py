"""带显式时间与确定性恢复的 Boss 混合命中序列。

伤害解析与减伤仍由现有 Boss 工具和 :mod:`calc.boss_survival` 完成；本模块只接收已经
结算到防御后的伤害区间，负责在用户给定的命中时间之间应用持续恢复。这样不会复制 Boss
伤害公式，也不会把用户排列的事件冒充游戏中的真实连招。

恢复模型刻意保持有界：一个共享防御池、固定每秒恢复、每次受击后统一恢复延迟。它不猜
生命/护盾分池、回充中断、偷取实例、药剂、Ward、MoM、格挡或闪避。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal, Sequence


Outcome = Literal["survives_full_range", "roll_dependent", "fails_full_range"]
RecoveryStatus = Literal["not_needed", "reachable", "unreachable"]
MAX_EVENTS = 100
MAX_TIME_SECONDS = 3600.0
MAX_RECOVERY_RATE = 1_000_000_000_000.0


class BossTimelineError(ValueError):
    """时间轴输入不完整或不满足安全计算边界。"""


@dataclass(frozen=True)
class TimedDamageEvent:
    at_seconds: float
    skill: str
    damage_type: str
    raw_low: float
    raw_high: float
    taken_low: float
    taken_high: float


@dataclass(frozen=True)
class TimedSequenceStep:
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


@dataclass(frozen=True)
class TimedSequenceResult:
    pool: float
    recovery_per_second: float
    recovery_delay_seconds: float
    duration_seconds: float
    hit_count: int
    total_taken_low: float
    total_taken_high: float
    total_recovered_best_case: float
    total_recovered_worst_case: float
    ending_remaining_best_case: float
    ending_remaining_worst_case: float
    lowest_remaining_best_case: float
    lowest_remaining_worst_case: float
    lowest_remaining_best_case_hit: int
    lowest_remaining_worst_case_hit: int
    earliest_possible_depletion_hit: int | None
    guaranteed_depletion_hit: int | None
    minimum_pool_for_any_roll: int
    minimum_pool_for_full_range: int
    required_recovery_for_any_roll: float | None
    required_recovery_for_full_range: float | None
    required_recovery_for_any_roll_exclusive: bool
    required_recovery_for_full_range_exclusive: bool
    required_recovery_for_any_roll_status: RecoveryStatus
    required_recovery_for_full_range_status: RecoveryStatus
    outcome: Outcome
    steps: tuple[TimedSequenceStep, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TimedScenarioResult:
    name: str
    outcome: Outcome
    ending_remaining_worst_case: float
    lowest_remaining_worst_case: float
    minimum_pool_for_full_range: int
    required_recovery_for_full_range: float | None
    required_recovery_for_full_range_exclusive: bool
    result: TimedSequenceResult


@dataclass(frozen=True)
class TimedSequenceComparison:
    scenarios: tuple[TimedScenarioResult, ...]
    best_survival_scenario: str
    best_worst_case_margin_scenario: str
    lowest_required_pool_scenario: str
    lowest_required_recovery_scenario: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _PathState:
    remaining: float
    alive: bool = True
    total_recovered: float = 0.0
    first_depletion_hit: int | None = None
    lowest_remaining: float = math.inf
    lowest_hit: int = 0


def _number(value: object, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BossTimelineError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise BossTimelineError(f"{label} 必须是大于等于 {minimum:g} 的有限数字")
    return result


def _validated_events(events: Sequence[TimedDamageEvent]) -> tuple[TimedDamageEvent, ...]:
    if not isinstance(events, (list, tuple)) or not 1 <= len(events) <= MAX_EVENTS:
        raise BossTimelineError(f"events 必须包含 1..{MAX_EVENTS} 次命中")
    normalized: list[TimedDamageEvent] = []
    previous = -1.0
    for index, event in enumerate(events):
        if not isinstance(event, TimedDamageEvent):
            raise BossTimelineError(f"events[{index}] 必须是 TimedDamageEvent")
        at = _number(event.at_seconds, f"events[{index}].at_seconds")
        if at > MAX_TIME_SECONDS:
            raise BossTimelineError(f"events[{index}].at_seconds 不能超过 {MAX_TIME_SECONDS:g}")
        if at < previous:
            raise BossTimelineError("events 必须按 at_seconds 非递减排列")
        skill = str(event.skill).strip()
        if not skill:
            raise BossTimelineError(f"events[{index}].skill 不能为空")
        raw_low = _number(event.raw_low, f"events[{index}].raw_low")
        raw_high = _number(event.raw_high, f"events[{index}].raw_high")
        taken_low = _number(event.taken_low, f"events[{index}].taken_low")
        taken_high = _number(event.taken_high, f"events[{index}].taken_high")
        if raw_low > raw_high or taken_low > taken_high:
            raise BossTimelineError(f"events[{index}] 的低值不能大于高值")
        normalized.append(TimedDamageEvent(
            at_seconds=at, skill=skill, damage_type=str(event.damage_type),
            raw_low=raw_low, raw_high=raw_high,
            taken_low=taken_low, taken_high=taken_high,
        ))
        previous = at
    return tuple(normalized)


def _recover(
    state: _PathState, *, pool: float, rate: float, eligible_seconds: float,
) -> tuple[float, float]:
    if not state.alive:
        return state.remaining, 0.0
    before = state.remaining
    state.remaining = min(pool, state.remaining + rate * eligible_seconds)
    recovered = max(0.0, state.remaining - before)
    state.total_recovered += recovered
    return state.remaining, recovered


def _apply_damage(
    state: _PathState, *, damage: float, hit_index: int,
) -> None:
    if not state.alive:
        return
    state.remaining -= damage
    if state.remaining < state.lowest_remaining:
        state.lowest_remaining = state.remaining
        state.lowest_hit = hit_index
    if state.remaining <= 0:
        state.alive = False
        state.first_depletion_hit = hit_index


def _survives(
    events: Sequence[TimedDamageEvent], *, pool: float, recovery_per_second: float,
    recovery_delay_seconds: float, high_roll: bool,
) -> bool:
    state = _PathState(remaining=pool)
    previous_time: float | None = None
    for index, event in enumerate(events, start=1):
        elapsed = 0.0 if previous_time is None else event.at_seconds - previous_time
        eligible = max(0.0, elapsed - recovery_delay_seconds)
        _recover(state, pool=pool, rate=recovery_per_second, eligible_seconds=eligible)
        _apply_damage(
            state,
            damage=event.taken_high if high_roll else event.taken_low,
            hit_index=index,
        )
        if not state.alive:
            return False
        previous_time = event.at_seconds
    return True


def _minimum_pool(
    events: Sequence[TimedDamageEvent], *, recovery_per_second: float,
    recovery_delay_seconds: float, high_roll: bool,
) -> int:
    total = sum(event.taken_high if high_roll else event.taken_low for event in events)
    lower = 1
    upper = max(1, math.floor(total) + 1)
    while lower < upper:
        middle = (lower + upper) // 2
        if _survives(
            events, pool=float(middle), recovery_per_second=recovery_per_second,
            recovery_delay_seconds=recovery_delay_seconds, high_roll=high_roll,
        ):
            upper = middle
        else:
            lower = middle + 1
    return lower


def _required_recovery(
    events: Sequence[TimedDamageEvent], *, pool: float,
    recovery_delay_seconds: float, high_roll: bool,
) -> tuple[RecoveryStatus, float | None]:
    if _survives(
        events, pool=pool, recovery_per_second=0.0,
        recovery_delay_seconds=recovery_delay_seconds, high_roll=high_roll,
    ):
        return "not_needed", 0.0
    if not _survives(
        events, pool=pool, recovery_per_second=MAX_RECOVERY_RATE,
        recovery_delay_seconds=recovery_delay_seconds, high_roll=high_roll,
    ):
        return "unreachable", None
    lower = 0.0
    upper = 1.0
    while upper < MAX_RECOVERY_RATE and not _survives(
        events, pool=pool, recovery_per_second=upper,
        recovery_delay_seconds=recovery_delay_seconds, high_roll=high_roll,
    ):
        lower = upper
        upper *= 2
    for _ in range(80):
        middle = (lower + upper) / 2
        if _survives(
            events, pool=pool, recovery_per_second=middle,
            recovery_delay_seconds=recovery_delay_seconds, high_roll=high_roll,
        ):
            upper = middle
        else:
            lower = middle
    return "reachable", upper


def assess_timed_sequence(
    *, events: Sequence[TimedDamageEvent], pool: float,
    recovery_per_second: float = 0.0, recovery_delay_seconds: float = 0.0,
) -> TimedSequenceResult:
    """逐时结算命中区间、固定持续恢复与每次受击后恢复延迟。"""
    normalized = _validated_events(events)
    maximum_pool = _number(pool, "pool")
    if maximum_pool <= 0:
        raise BossTimelineError("pool 必须大于 0")
    rate = _number(recovery_per_second, "recovery_per_second")
    delay = _number(recovery_delay_seconds, "recovery_delay_seconds")
    if delay > MAX_TIME_SECONDS:
        raise BossTimelineError(f"recovery_delay_seconds 不能超过 {MAX_TIME_SECONDS:g}")

    best = _PathState(remaining=maximum_pool)
    worst = _PathState(remaining=maximum_pool)
    previous_time: float | None = None
    steps: list[TimedSequenceStep] = []
    for index, event in enumerate(normalized, start=1):
        elapsed = 0.0 if previous_time is None else event.at_seconds - previous_time
        eligible = max(0.0, elapsed - delay)
        best_before, best_recovered = _recover(
            best, pool=maximum_pool, rate=rate, eligible_seconds=eligible,
        )
        worst_before, worst_recovered = _recover(
            worst, pool=maximum_pool, rate=rate, eligible_seconds=eligible,
        )
        _apply_damage(best, damage=event.taken_low, hit_index=index)
        _apply_damage(worst, damage=event.taken_high, hit_index=index)
        steps.append(TimedSequenceStep(
            hit_index=index, at_seconds=event.at_seconds,
            elapsed_since_previous_hit=elapsed, recovery_eligible_seconds=eligible,
            skill=event.skill, damage_type=event.damage_type,
            raw_low=event.raw_low, raw_high=event.raw_high,
            taken_low=event.taken_low, taken_high=event.taken_high,
            best_case_recovered=round(best_recovered, 6),
            worst_case_recovered=round(worst_recovered, 6),
            best_case_pool_before_hit=round(best_before, 6),
            worst_case_pool_before_hit=round(worst_before, 6),
            best_case_remaining=round(best.remaining, 6),
            worst_case_remaining=round(worst.remaining, 6),
            best_case_depleted=not best.alive,
            worst_case_depleted=not worst.alive,
        ))
        previous_time = event.at_seconds

    if worst.alive:
        outcome: Outcome = "survives_full_range"
    elif not best.alive:
        outcome = "fails_full_range"
    else:
        outcome = "roll_dependent"
    any_status, any_rate = _required_recovery(
        normalized, pool=maximum_pool, recovery_delay_seconds=delay, high_roll=False,
    )
    full_status, full_rate = _required_recovery(
        normalized, pool=maximum_pool, recovery_delay_seconds=delay, high_roll=True,
    )
    return TimedSequenceResult(
        pool=maximum_pool, recovery_per_second=rate,
        recovery_delay_seconds=delay,
        duration_seconds=normalized[-1].at_seconds,
        hit_count=len(normalized),
        total_taken_low=round(sum(event.taken_low for event in normalized), 6),
        total_taken_high=round(sum(event.taken_high for event in normalized), 6),
        total_recovered_best_case=round(best.total_recovered, 6),
        total_recovered_worst_case=round(worst.total_recovered, 6),
        ending_remaining_best_case=round(best.remaining, 6),
        ending_remaining_worst_case=round(worst.remaining, 6),
        lowest_remaining_best_case=round(best.lowest_remaining, 6),
        lowest_remaining_worst_case=round(worst.lowest_remaining, 6),
        lowest_remaining_best_case_hit=best.lowest_hit,
        lowest_remaining_worst_case_hit=worst.lowest_hit,
        earliest_possible_depletion_hit=worst.first_depletion_hit,
        guaranteed_depletion_hit=best.first_depletion_hit,
        minimum_pool_for_any_roll=_minimum_pool(
            normalized, recovery_per_second=rate,
            recovery_delay_seconds=delay, high_roll=False,
        ),
        minimum_pool_for_full_range=_minimum_pool(
            normalized, recovery_per_second=rate,
            recovery_delay_seconds=delay, high_roll=True,
        ),
        required_recovery_for_any_roll=any_rate,
        required_recovery_for_full_range=full_rate,
        required_recovery_for_any_roll_exclusive=any_status == "reachable",
        required_recovery_for_full_range_exclusive=full_status == "reachable",
        required_recovery_for_any_roll_status=any_status,
        required_recovery_for_full_range_status=full_status,
        outcome=outcome, steps=tuple(steps),
    )


def compare_timed_sequences(
    scenarios: Sequence[tuple[str, TimedSequenceResult]],
) -> TimedSequenceComparison:
    if not isinstance(scenarios, (list, tuple)) or not 2 <= len(scenarios) <= 10:
        raise BossTimelineError("scenarios 必须包含 2..10 个方案")
    names = [str(name).strip() for name, _ in scenarios]
    if any(not name or len(name) > 80 for name in names) or len(set(names)) != len(names):
        raise BossTimelineError("方案名称必须为不重复的 1..80 个字符")
    rank = {"fails_full_range": 0, "roll_dependent": 1, "survives_full_range": 2}
    rows = tuple(TimedScenarioResult(
        name=name, outcome=result.outcome,
        ending_remaining_worst_case=result.ending_remaining_worst_case,
        lowest_remaining_worst_case=result.lowest_remaining_worst_case,
        minimum_pool_for_full_range=result.minimum_pool_for_full_range,
        required_recovery_for_full_range=result.required_recovery_for_full_range,
        required_recovery_for_full_range_exclusive=(
            result.required_recovery_for_full_range_exclusive
        ),
        result=result,
    ) for name, result in scenarios)
    reachable_recovery = [
        row for row in rows if row.required_recovery_for_full_range is not None
    ]
    return TimedSequenceComparison(
        scenarios=rows,
        best_survival_scenario=max(
            rows, key=lambda row: (rank[row.outcome], row.lowest_remaining_worst_case)
        ).name,
        best_worst_case_margin_scenario=max(
            rows, key=lambda row: row.lowest_remaining_worst_case
        ).name,
        lowest_required_pool_scenario=min(
            rows, key=lambda row: row.minimum_pool_for_full_range
        ).name,
        lowest_required_recovery_scenario=(
            min(
                reachable_recovery,
                key=lambda row: row.required_recovery_for_full_range or 0.0,
            ).name if reachable_recovery else None
        ),
    )
