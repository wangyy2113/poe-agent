"""Boss 分层防御池上的显式主动恢复事件时间轴。

本模块在 :mod:`calc.boss_layered` 的伤害路由语义上继续推进三类主动恢复：

* ``instant``：药剂瞬回、击中回复等已经换算成池数值的离散事件；
* ``timed_rate``：药剂持续恢复或其它明确持续窗口；
* ``leech``：调用方明确给出的偷取实例速率与持续时间，多个实例合计后按池速率上限裁剪。

这里不生成恢复事件，也不从攻击频率、药剂充能或伤害反推偷取。时间轴只回答“这些事件若
按给定时间发生，会怎样改变逐击生存”，不会把显式计划冒充自动循环或实战保证。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Sequence

from calc.boss_layered import LayeredDamageEvent


PoolName = Literal["life", "energy_shield", "mana"]
RecoveryKind = Literal["instant", "timed_rate", "leech"]
InstantPhase = Literal["before_hit", "after_hit"]
Outcome = Literal["survives_full_range", "roll_dependent", "fails_full_range"]
ThresholdStatus = Literal["not_applicable", "reachable", "unreachable"]
RechargeInterruptMode = Literal["energy_shield_damage", "any_hit"]
MAX_EVENTS = 100
MAX_TIME_SECONDS = 3600.0
MAX_VALUE = 1_000_000_000.0


class BossRecoveryError(ValueError):
    """主动恢复时间轴输入不完整、歧义或超出有界范围。"""


@dataclass(frozen=True)
class ActiveRecoveryEvent:
    name: str
    at_seconds: float
    pool: PoolName
    kind: RecoveryKind
    amount: float = 0.0
    rate_per_second: float = 0.0
    duration_seconds: float = 0.0
    phase: InstantPhase = "before_hit"

    @property
    def end_seconds(self) -> float:
        return self.at_seconds + self.duration_seconds


@dataclass(frozen=True)
class RecoveryPoolConfig:
    life: float
    energy_shield: float = 0.0
    mana: float = 0.0
    mana_before_life_pct: float = 0.0
    life_recovery_per_second: float = 0.0
    energy_shield_recovery_per_second: float = 0.0
    mana_recovery_per_second: float = 0.0
    energy_shield_recharge_per_second: float = 0.0
    energy_shield_recharge_delay_seconds: float = 2.0
    energy_shield_recharge_interrupt_mode: RechargeInterruptMode = (
        "energy_shield_damage"
    )
    life_leech_rate_cap_pct: float = 20.0
    energy_shield_leech_rate_cap_pct: float = 10.0
    mana_leech_rate_cap_pct: float = 20.0


@dataclass(frozen=True)
class PoolRecoveryBreakdown:
    pool: PoolName
    fixed_requested: float
    fixed_applied: float
    timed_rate_requested: float
    timed_rate_applied: float
    leech_requested: float
    leech_after_rate_cap: float
    leech_applied: float
    leech_suppressed_by_rate_cap: float
    recharge_requested: float
    recharge_applied: float
    continuous_applied: float
    continuous_overflow: float
    instant_before_requested: float
    instant_before_applied: float
    instant_before_overflow: float
    instant_after_requested: float
    instant_after_applied: float
    instant_after_overflow: float


@dataclass(frozen=True)
class InstantRecoveryRecord:
    name: str
    at_seconds: float
    pool: PoolName
    phase: InstantPhase
    requested: float
    applied: float
    overflow: float
    skipped_because_depleted: bool


@dataclass(frozen=True)
class RecoverySequenceStep:
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
    recovery: tuple[PoolRecoveryBreakdown, ...]
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
    life_after_post_hit_recovery: float
    energy_shield_after_post_hit_recovery: float
    mana_after_post_hit_recovery: float
    recharge_interrupted: bool
    depleted: bool


@dataclass(frozen=True)
class RecoveryRollResult:
    damage_basis: Literal["low_roll", "high_roll"]
    survived: bool
    depletion_hit: int | None
    ending_life: float
    ending_energy_shield: float
    ending_mana: float
    lowest_life: float
    lowest_life_hit: int
    total_taken_damage: float
    total_energy_shield_absorbed: float
    total_mana_absorbed: float
    total_life_damage: float
    total_fixed_recovery_applied: float
    total_timed_rate_recovery_applied: float
    total_leech_recovery_applied: float
    total_recharge_applied: float
    total_instant_recovery_applied: float
    total_recovery_overflow: float
    total_leech_suppressed_by_rate_cap: float
    recharge_interruption_count: int
    recovery_records: tuple[InstantRecoveryRecord, ...]
    steps: tuple[RecoverySequenceStep, ...]


@dataclass(frozen=True)
class RecoveryLayerThreshold:
    status: ThresholdStatus
    value: int | None
    current: float
    shortfall: float | None
    keeps_other_layers_fixed: bool = True


@dataclass(frozen=True)
class ActiveRecoveryImpact:
    high_roll_survived_without_active_recovery: bool
    high_roll_ending_life_without_active_recovery: float
    high_roll_life_gain: float
    changed_high_roll_survival: bool


@dataclass(frozen=True)
class RecoverySequenceResult:
    config: RecoveryPoolConfig
    recovery_events: tuple[ActiveRecoveryEvent, ...]
    hit_count: int
    duration_seconds: float
    outcome: Outcome
    low_roll: RecoveryRollResult
    high_roll: RecoveryRollResult
    active_recovery_impact: ActiveRecoveryImpact
    minimum_life_for_full_range: RecoveryLayerThreshold
    minimum_energy_shield_for_full_range: RecoveryLayerThreshold
    minimum_mana_for_full_range: RecoveryLayerThreshold
    event_ordering: str
    recovery_attribution_model: str
    routing_model: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecoveryScenarioResult:
    name: str
    outcome: Outcome
    ending_life_high_roll: float
    total_active_recovery_high_roll: float
    minimum_life_for_full_range: int | None
    result: RecoverySequenceResult


@dataclass(frozen=True)
class RecoverySequenceComparison:
    scenarios: tuple[RecoveryScenarioResult, ...]
    best_survival_scenario: str
    best_high_roll_life_margin_scenario: str
    highest_active_recovery_scenario: str
    lowest_required_life_scenario: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _State:
    life: float
    energy_shield: float
    mana: float
    alive: bool = True
    depletion_hit: int | None = None
    lowest_life: float = math.inf
    lowest_life_hit: int = 0
    last_recharge_interrupt_at: float | None = None
    total_taken_damage: float = 0.0
    total_energy_shield_absorbed: float = 0.0
    total_mana_absorbed: float = 0.0
    total_life_damage: float = 0.0
    total_fixed_recovery_applied: float = 0.0
    total_timed_rate_recovery_applied: float = 0.0
    total_leech_recovery_applied: float = 0.0
    total_recharge_applied: float = 0.0
    total_instant_recovery_applied: float = 0.0
    total_recovery_overflow: float = 0.0
    total_leech_suppressed_by_rate_cap: float = 0.0
    recharge_interruption_count: int = 0


_POOLS: tuple[PoolName, ...] = ("life", "energy_shield", "mana")


def _number(
    value: object, label: str, *, minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BossRecoveryError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise BossRecoveryError(f"{label} 必须是大于等于 {minimum:g} 的有限数字")
    if maximum is not None and result > maximum:
        raise BossRecoveryError(f"{label} 不能超过 {maximum:g}")
    return result


def _validate_config(config: RecoveryPoolConfig) -> RecoveryPoolConfig:
    if not isinstance(config, RecoveryPoolConfig):
        raise BossRecoveryError("config 必须是 RecoveryPoolConfig")
    values: dict[str, Any] = {}
    for field in (
        "life", "energy_shield", "mana", "life_recovery_per_second",
        "energy_shield_recovery_per_second", "mana_recovery_per_second",
        "energy_shield_recharge_per_second", "energy_shield_recharge_delay_seconds",
    ):
        values[field] = _number(getattr(config, field), field, maximum=MAX_VALUE)
    for field in (
        "life_leech_rate_cap_pct", "energy_shield_leech_rate_cap_pct",
        "mana_leech_rate_cap_pct",
    ):
        values[field] = _number(getattr(config, field), field, maximum=1000)
    values["mana_before_life_pct"] = _number(
        config.mana_before_life_pct, "mana_before_life_pct", maximum=100,
    )
    if values["life"] <= 0:
        raise BossRecoveryError("life 必须大于 0")
    if config.energy_shield_recharge_interrupt_mode not in {
        "energy_shield_damage", "any_hit",
    }:
        raise BossRecoveryError(
            "energy_shield_recharge_interrupt_mode 只支持 energy_shield_damage 或 any_hit"
        )
    values["energy_shield_recharge_interrupt_mode"] = (
        config.energy_shield_recharge_interrupt_mode
    )
    return RecoveryPoolConfig(**values)


def _validate_damage_events(
    events: Sequence[LayeredDamageEvent],
) -> tuple[LayeredDamageEvent, ...]:
    if not isinstance(events, (list, tuple)) or not 1 <= len(events) <= MAX_EVENTS:
        raise BossRecoveryError(f"damage_events 必须包含 1..{MAX_EVENTS} 次命中")
    previous = -1.0
    normalized: list[LayeredDamageEvent] = []
    for index, event in enumerate(events):
        if not isinstance(event, LayeredDamageEvent):
            raise BossRecoveryError(f"damage_events[{index}] 类型无效")
        damage = event.damage
        at = _number(
            damage.at_seconds, f"damage_events[{index}].at_seconds",
            maximum=MAX_TIME_SECONDS,
        )
        if at < previous:
            raise BossRecoveryError("damage_events 必须按 at_seconds 非递减排列")
        for field in ("raw_low", "raw_high", "taken_low", "taken_high"):
            _number(getattr(damage, field), f"damage_events[{index}].{field}")
        if damage.raw_low > damage.raw_high or damage.taken_low > damage.taken_high:
            raise BossRecoveryError(f"damage_events[{index}] 的低值不能大于高值")
        bypass = _number(
            event.energy_shield_bypass_pct,
            f"damage_events[{index}].energy_shield_bypass_pct", maximum=100,
        )
        normalized.append(replace(event, energy_shield_bypass_pct=bypass))
        previous = at
    return tuple(normalized)


def _validate_recovery_events(
    events: Sequence[ActiveRecoveryEvent],
) -> tuple[ActiveRecoveryEvent, ...]:
    if not isinstance(events, (list, tuple)) or len(events) > MAX_EVENTS:
        raise BossRecoveryError(f"recovery_events 必须包含 0..{MAX_EVENTS} 个事件")
    normalized: list[ActiveRecoveryEvent] = []
    for index, event in enumerate(events):
        if not isinstance(event, ActiveRecoveryEvent):
            raise BossRecoveryError(f"recovery_events[{index}] 类型无效")
        name = str(event.name).strip()
        if not 1 <= len(name) <= 80:
            raise BossRecoveryError(f"recovery_events[{index}].name 必须为 1..80 个字符")
        if event.pool not in _POOLS:
            raise BossRecoveryError(f"recovery_events[{index}].pool 无效")
        if event.kind not in {"instant", "timed_rate", "leech"}:
            raise BossRecoveryError(f"recovery_events[{index}].kind 无效")
        at = _number(
            event.at_seconds, f"recovery_events[{index}].at_seconds",
            maximum=MAX_TIME_SECONDS,
        )
        amount = _number(event.amount, f"recovery_events[{index}].amount", maximum=MAX_VALUE)
        rate = _number(
            event.rate_per_second, f"recovery_events[{index}].rate_per_second",
            maximum=MAX_VALUE,
        )
        duration = _number(
            event.duration_seconds, f"recovery_events[{index}].duration_seconds",
            maximum=MAX_TIME_SECONDS,
        )
        if event.phase not in {"before_hit", "after_hit"}:
            raise BossRecoveryError(f"recovery_events[{index}].phase 无效")
        if event.kind == "instant":
            if amount <= 0 or rate != 0 or duration != 0:
                raise BossRecoveryError(
                    f"recovery_events[{index}] 瞬回必须给正 amount，且不能给速率或持续时间"
                )
        else:
            if rate <= 0 or duration <= 0 or amount != 0:
                raise BossRecoveryError(
                    f"recovery_events[{index}] 持续事件必须给正速率和持续时间，且不能给 amount"
                )
            if event.phase != "before_hit":
                raise BossRecoveryError(
                    f"recovery_events[{index}] 只有瞬回可以使用 after_hit"
                )
            if at + duration > MAX_TIME_SECONDS:
                raise BossRecoveryError(
                    f"recovery_events[{index}] 的结束时间不能超过 {MAX_TIME_SECONDS:g}"
                )
        normalized.append(ActiveRecoveryEvent(
            name=name, at_seconds=at, pool=event.pool, kind=event.kind,
            amount=amount, rate_per_second=rate, duration_seconds=duration,
            phase=event.phase,
        ))
    return tuple(sorted(normalized, key=lambda item: (item.at_seconds, item.name)))


def _pool_max(config: RecoveryPoolConfig, pool: PoolName) -> float:
    return float(getattr(config, pool))


def _pool_value(state: _State, pool: PoolName) -> float:
    return float(getattr(state, pool))


def _set_pool_value(state: _State, pool: PoolName, value: float) -> None:
    setattr(state, pool, value)


def _leech_cap(config: RecoveryPoolConfig, pool: PoolName) -> float:
    pct = {
        "life": config.life_leech_rate_cap_pct,
        "energy_shield": config.energy_shield_leech_rate_cap_pct,
        "mana": config.mana_leech_rate_cap_pct,
    }[pool]
    return _pool_max(config, pool) * pct / 100


def _blank_breakdown(pool: PoolName) -> dict[str, float | str]:
    return {
        "pool": pool, "fixed_requested": 0.0, "fixed_applied": 0.0,
        "timed_rate_requested": 0.0, "timed_rate_applied": 0.0,
        "leech_requested": 0.0, "leech_after_rate_cap": 0.0,
        "leech_applied": 0.0, "leech_suppressed_by_rate_cap": 0.0,
        "recharge_requested": 0.0, "recharge_applied": 0.0,
        "continuous_applied": 0.0, "continuous_overflow": 0.0,
        "instant_before_requested": 0.0, "instant_before_applied": 0.0,
        "instant_before_overflow": 0.0, "instant_after_requested": 0.0,
        "instant_after_applied": 0.0, "instant_after_overflow": 0.0,
    }


def _as_breakdowns(values: dict[PoolName, dict[str, float | str]]) -> tuple[PoolRecoveryBreakdown, ...]:
    return tuple(PoolRecoveryBreakdown(**values[pool]) for pool in _POOLS)  # type: ignore[arg-type]


def _advance(
    state: _State, *, start: float, end: float, config: RecoveryPoolConfig,
    recoveries: tuple[ActiveRecoveryEvent, ...],
    totals: dict[PoolName, dict[str, float | str]],
) -> None:
    if end <= start or not state.alive:
        return
    breakpoints = {start, end}
    for event in recoveries:
        if event.kind != "instant":
            if start < event.at_seconds < end:
                breakpoints.add(event.at_seconds)
            if start < event.end_seconds < end:
                breakpoints.add(event.end_seconds)
    if state.last_recharge_interrupt_at is not None:
        recharge_start = (
            state.last_recharge_interrupt_at
            + config.energy_shield_recharge_delay_seconds
        )
        if start < recharge_start < end:
            breakpoints.add(recharge_start)
    ordered = sorted(breakpoints)
    for left, right in zip(ordered, ordered[1:]):
        duration = right - left
        if duration <= 0:
            continue
        for pool in _POOLS:
            fixed_rate = {
                "life": config.life_recovery_per_second,
                "energy_shield": config.energy_shield_recovery_per_second,
                "mana": config.mana_recovery_per_second,
            }[pool]
            timed_rate = sum(
                event.rate_per_second for event in recoveries
                if event.pool == pool and event.kind == "timed_rate"
                and event.at_seconds <= left < event.end_seconds
            )
            leech_rate = sum(
                event.rate_per_second for event in recoveries
                if event.pool == pool and event.kind == "leech"
                and event.at_seconds <= left < event.end_seconds
            )
            capped_leech_rate = min(leech_rate, _leech_cap(config, pool))
            recharge_rate = 0.0
            if (
                pool == "energy_shield"
                and state.last_recharge_interrupt_at is not None
                and left >= state.last_recharge_interrupt_at
                + config.energy_shield_recharge_delay_seconds
            ):
                recharge_rate = config.energy_shield_recharge_per_second
            requested = {
                "fixed": fixed_rate * duration,
                "timed_rate": timed_rate * duration,
                "leech": leech_rate * duration,
                "leech_capped": capped_leech_rate * duration,
                "recharge": recharge_rate * duration,
            }
            potential = (
                requested["fixed"] + requested["timed_rate"]
                + requested["leech_capped"] + requested["recharge"]
            )
            maximum = _pool_max(config, pool)
            before = _pool_value(state, pool)
            applied = min(max(0.0, maximum - before), potential)
            scale = applied / potential if potential > 0 else 0.0
            overflow = potential - applied
            _set_pool_value(state, pool, before + applied)
            row = totals[pool]
            for label, key in (
                ("fixed", "fixed_requested"),
                ("timed_rate", "timed_rate_requested"),
                ("leech", "leech_requested"),
                ("leech_capped", "leech_after_rate_cap"),
                ("recharge", "recharge_requested"),
            ):
                row[key] = float(row[key]) + requested[label]
            suppressed = requested["leech"] - requested["leech_capped"]
            row["leech_suppressed_by_rate_cap"] = (
                float(row["leech_suppressed_by_rate_cap"]) + suppressed
            )
            for requested_key, applied_key in (
                ("fixed", "fixed_applied"),
                ("timed_rate", "timed_rate_applied"),
                ("leech_capped", "leech_applied"),
                ("recharge", "recharge_applied"),
            ):
                row[applied_key] = (
                    float(row[applied_key]) + requested[requested_key] * scale
                )
            row["continuous_applied"] = float(row["continuous_applied"]) + applied
            row["continuous_overflow"] = float(row["continuous_overflow"]) + overflow
            state.total_fixed_recovery_applied += requested["fixed"] * scale
            state.total_timed_rate_recovery_applied += requested["timed_rate"] * scale
            state.total_leech_recovery_applied += requested["leech_capped"] * scale
            state.total_recharge_applied += requested["recharge"] * scale
            state.total_recovery_overflow += overflow
            state.total_leech_suppressed_by_rate_cap += suppressed


def _apply_instant(
    state: _State, event: ActiveRecoveryEvent, *, config: RecoveryPoolConfig,
    totals: dict[PoolName, dict[str, float | str]], records: list[InstantRecoveryRecord],
) -> None:
    skipped = not state.alive
    before = _pool_value(state, event.pool)
    maximum = _pool_max(config, event.pool)
    applied = 0.0 if skipped else min(max(0.0, maximum - before), event.amount)
    overflow = event.amount - applied
    if not skipped:
        _set_pool_value(state, event.pool, before + applied)
    prefix = "instant_before" if event.phase == "before_hit" else "instant_after"
    row = totals[event.pool]
    row[f"{prefix}_requested"] = float(row[f"{prefix}_requested"]) + event.amount
    row[f"{prefix}_applied"] = float(row[f"{prefix}_applied"]) + applied
    row[f"{prefix}_overflow"] = float(row[f"{prefix}_overflow"]) + overflow
    state.total_instant_recovery_applied += applied
    state.total_recovery_overflow += overflow
    records.append(InstantRecoveryRecord(
        name=event.name, at_seconds=event.at_seconds, pool=event.pool,
        phase=event.phase, requested=round(event.amount, 6), applied=round(applied, 6),
        overflow=round(overflow, 6), skipped_because_depleted=skipped,
    ))


def _round_breakdowns(
    values: dict[PoolName, dict[str, float | str]],
) -> tuple[PoolRecoveryBreakdown, ...]:
    rounded: dict[PoolName, dict[str, float | str]] = {}
    for pool, row in values.items():
        rounded[pool] = {
            key: (round(value, 6) if isinstance(value, float) else value)
            for key, value in row.items()
        }
    return _as_breakdowns(rounded)


def _simulate(
    damage_events: tuple[LayeredDamageEvent, ...], *,
    recovery_events: tuple[ActiveRecoveryEvent, ...],
    config: RecoveryPoolConfig, high_roll: bool,
) -> RecoveryRollResult:
    state = _State(
        life=config.life, energy_shield=config.energy_shield, mana=config.mana,
    )
    steps: list[RecoverySequenceStep] = []
    records: list[InstantRecoveryRecord] = []
    previous_time = 0.0
    applied_instants: set[int] = set()
    for index, event in enumerate(damage_events, start=1):
        at = event.damage.at_seconds
        elapsed = at - previous_time if index > 1 else at
        totals = {pool: _blank_breakdown(pool) for pool in _POOLS}
        life_before_recovery = state.life
        es_before_recovery = state.energy_shield
        mana_before_recovery = state.mana
        cursor = previous_time
        intermediate_times = sorted({
            recovery.at_seconds for recovery in recovery_events
            if recovery.kind == "instant" and cursor < recovery.at_seconds < at
        })
        for instant_time in intermediate_times:
            _advance(
                state, start=cursor, end=instant_time, config=config,
                recoveries=recovery_events, totals=totals,
            )
            for recovery_index, recovery in enumerate(recovery_events):
                if (
                    recovery.kind == "instant"
                    and recovery.at_seconds == instant_time
                    and recovery_index not in applied_instants
                ):
                    _apply_instant(
                        state, recovery, config=config, totals=totals, records=records,
                    )
                    applied_instants.add(recovery_index)
            cursor = instant_time
        _advance(
            state, start=cursor, end=at, config=config,
            recoveries=recovery_events, totals=totals,
        )
        first_at_time = index == 1 or damage_events[index - 2].damage.at_seconds != at
        last_at_time = (
            index == len(damage_events)
            or damage_events[index].damage.at_seconds != at
        )
        if first_at_time:
            for recovery_index, recovery in enumerate(recovery_events):
                if (
                    recovery.kind == "instant" and recovery.at_seconds == at
                    and recovery.phase == "before_hit"
                    and recovery_index not in applied_instants
                ):
                    _apply_instant(
                        state, recovery, config=config, totals=totals, records=records,
                    )
                    applied_instants.add(recovery_index)

        damage = event.damage.taken_high if high_roll else event.damage.taken_low
        raw = event.damage.raw_high if high_roll else event.damage.raw_low
        life_before_hit = state.life
        es_before_hit = state.energy_shield
        mana_before_hit = state.mana
        es_absorbed = es_bypassed = mana_target = mana_absorbed = 0.0
        mana_shortfall = life_damage = 0.0
        interrupted = False
        if state.alive:
            state.total_taken_damage += damage
            es_bypassed = damage * event.energy_shield_bypass_pct / 100
            reaches_es = damage - es_bypassed
            es_absorbed = min(state.energy_shield, reaches_es)
            state.energy_shield -= es_absorbed
            reaches_life_mana = es_bypassed + reaches_es - es_absorbed
            mana_target = reaches_life_mana * config.mana_before_life_pct / 100
            mana_absorbed = min(state.mana, mana_target)
            state.mana -= mana_absorbed
            mana_shortfall = mana_target - mana_absorbed
            life_damage = reaches_life_mana - mana_absorbed
            state.life -= life_damage
            state.total_energy_shield_absorbed += es_absorbed
            state.total_mana_absorbed += mana_absorbed
            state.total_life_damage += life_damage
            if state.life < state.lowest_life:
                state.lowest_life = state.life
                state.lowest_life_hit = index
            interrupted = (
                config.energy_shield_recharge_interrupt_mode == "any_hit"
                or es_absorbed > 0
            )
            if interrupted:
                state.last_recharge_interrupt_at = at
                state.recharge_interruption_count += 1
            if state.life <= 0:
                state.alive = False
                state.depletion_hit = index

        life_after_hit = state.life
        es_after_hit = state.energy_shield
        mana_after_hit = state.mana
        if last_at_time:
            for recovery_index, recovery in enumerate(recovery_events):
                if (
                    recovery.kind == "instant" and recovery.at_seconds == at
                    and recovery.phase == "after_hit"
                    and recovery_index not in applied_instants
                ):
                    _apply_instant(
                        state, recovery, config=config, totals=totals, records=records,
                    )
                    applied_instants.add(recovery_index)
        steps.append(RecoverySequenceStep(
            hit_index=index, at_seconds=at,
            elapsed_since_previous_hit=elapsed if first_at_time else 0.0,
            skill=event.damage.skill, damage_type=event.damage.damage_type,
            raw_damage=round(raw, 6), taken_damage=round(damage, 6),
            energy_shield_bypass_pct=event.energy_shield_bypass_pct,
            life_before_recovery=round(life_before_recovery, 6),
            energy_shield_before_recovery=round(es_before_recovery, 6),
            mana_before_recovery=round(mana_before_recovery, 6),
            recovery=_round_breakdowns(totals),
            life_before_hit=round(life_before_hit, 6),
            energy_shield_before_hit=round(es_before_hit, 6),
            mana_before_hit=round(mana_before_hit, 6),
            energy_shield_absorbed=round(es_absorbed, 6),
            energy_shield_bypassed=round(es_bypassed, 6),
            mana_target_damage=round(mana_target, 6),
            mana_absorbed=round(mana_absorbed, 6),
            mana_shortfall_to_life=round(mana_shortfall, 6),
            life_damage=round(life_damage, 6),
            life_after_hit=round(life_after_hit, 6),
            energy_shield_after_hit=round(es_after_hit, 6),
            mana_after_hit=round(mana_after_hit, 6),
            life_after_post_hit_recovery=round(state.life, 6),
            energy_shield_after_post_hit_recovery=round(state.energy_shield, 6),
            mana_after_post_hit_recovery=round(state.mana, 6),
            recharge_interrupted=interrupted, depleted=not state.alive,
        ))
        previous_time = at

    if state.lowest_life == math.inf:
        state.lowest_life = state.life
    return RecoveryRollResult(
        damage_basis="high_roll" if high_roll else "low_roll",
        survived=state.alive, depletion_hit=state.depletion_hit,
        ending_life=round(state.life, 6),
        ending_energy_shield=round(state.energy_shield, 6),
        ending_mana=round(state.mana, 6), lowest_life=round(state.lowest_life, 6),
        lowest_life_hit=state.lowest_life_hit,
        total_taken_damage=round(state.total_taken_damage, 6),
        total_energy_shield_absorbed=round(state.total_energy_shield_absorbed, 6),
        total_mana_absorbed=round(state.total_mana_absorbed, 6),
        total_life_damage=round(state.total_life_damage, 6),
        total_fixed_recovery_applied=round(state.total_fixed_recovery_applied, 6),
        total_timed_rate_recovery_applied=round(
            state.total_timed_rate_recovery_applied, 6
        ),
        total_leech_recovery_applied=round(state.total_leech_recovery_applied, 6),
        total_recharge_applied=round(state.total_recharge_applied, 6),
        total_instant_recovery_applied=round(state.total_instant_recovery_applied, 6),
        total_recovery_overflow=round(state.total_recovery_overflow, 6),
        total_leech_suppressed_by_rate_cap=round(
            state.total_leech_suppressed_by_rate_cap, 6
        ),
        recharge_interruption_count=state.recharge_interruption_count,
        recovery_records=tuple(records), steps=tuple(steps),
    )


def _survives_with(
    damage_events: tuple[LayeredDamageEvent, ...],
    recovery_events: tuple[ActiveRecoveryEvent, ...], config: RecoveryPoolConfig,
    *, field: PoolName, value: float,
) -> bool:
    return _simulate(
        damage_events, recovery_events=recovery_events,
        config=replace(config, **{field: value}), high_roll=True,
    ).survived


def _threshold(
    damage_events: tuple[LayeredDamageEvent, ...],
    recovery_events: tuple[ActiveRecoveryEvent, ...], config: RecoveryPoolConfig,
    *, field: PoolName, applicable: bool = True,
) -> RecoveryLayerThreshold:
    current = float(getattr(config, field))
    if not applicable:
        return RecoveryLayerThreshold(
            status="not_applicable", value=None, current=current, shortfall=None,
        )
    lower = 1 if field == "life" else 0
    upper = max(lower, math.ceil(current), 1)
    while upper <= MAX_VALUE and not _survives_with(
        damage_events, recovery_events, config, field=field, value=float(upper),
    ):
        upper *= 2
    if upper > MAX_VALUE:
        return RecoveryLayerThreshold(
            status="unreachable", value=None, current=current, shortfall=None,
        )
    while lower < upper:
        middle = (lower + upper) // 2
        if _survives_with(
            damage_events, recovery_events, config,
            field=field, value=float(middle),
        ):
            upper = middle
        else:
            lower = middle + 1
    return RecoveryLayerThreshold(
        status="reachable", value=lower, current=current,
        shortfall=round(max(0.0, lower - current), 6),
    )


def assess_recovery_sequence(
    *, damage_events: Sequence[LayeredDamageEvent],
    recovery_events: Sequence[ActiveRecoveryEvent], config: RecoveryPoolConfig,
) -> RecoverySequenceResult:
    """推进分层承伤、持续/瞬时主动恢复、偷取限速与耗尽状态。"""
    damages = _validate_damage_events(damage_events)
    recoveries = _validate_recovery_events(recovery_events)
    checked = _validate_config(config)
    end = damages[-1].damage.at_seconds
    for index, event in enumerate(recoveries):
        if event.at_seconds > end:
            raise BossRecoveryError(
                f"recovery_events[{index}] 开始于最后一次命中之后，超出本次时间轴"
            )
        if event.kind != "instant" and event.at_seconds >= end:
            raise BossRecoveryError(
                f"recovery_events[{index}] 的持续窗口在最后一次命中前没有生效时间"
            )
    low = _simulate(
        damages, recovery_events=recoveries, config=checked, high_roll=False,
    )
    high = _simulate(
        damages, recovery_events=recoveries, config=checked, high_roll=True,
    )
    if high.survived:
        outcome: Outcome = "survives_full_range"
    elif not low.survived:
        outcome = "fails_full_range"
    else:
        outcome = "roll_dependent"
    without = _simulate(
        damages, recovery_events=(), config=checked, high_roll=True,
    )
    es_applicable = checked.energy_shield > 0 or any(
        event.energy_shield_bypass_pct < 100 for event in damages
    )
    mana_applicable = checked.mana_before_life_pct > 0
    return RecoverySequenceResult(
        config=checked, recovery_events=recoveries, hit_count=len(damages),
        duration_seconds=damages[-1].damage.at_seconds, outcome=outcome,
        low_roll=low, high_roll=high,
        active_recovery_impact=ActiveRecoveryImpact(
            high_roll_survived_without_active_recovery=without.survived,
            high_roll_ending_life_without_active_recovery=without.ending_life,
            high_roll_life_gain=round(high.ending_life - without.ending_life, 6),
            changed_high_roll_survival=high.survived != without.survived,
        ),
        minimum_life_for_full_range=_threshold(
            damages, recoveries, checked, field="life",
        ),
        minimum_energy_shield_for_full_range=_threshold(
            damages, recoveries, checked, field="energy_shield",
            applicable=es_applicable,
        ),
        minimum_mana_for_full_range=_threshold(
            damages, recoveries, checked, field="mana", applicable=mana_applicable,
        ),
        event_ordering=(
            "同一时间点先结算 before_hit 瞬回，再按请求顺序结算全部命中，最后结算 "
            "after_hit 瞬回；生命耗尽后任何恢复都不能复活"
        ),
        recovery_attribution_model=(
            "同一积分段有多个连续恢复来源且触及池上限时，实际生效量按各来源潜在恢复量"
            "等比例归属；总生效量与总溢出精确，来源归属只用于审计，不代表游戏内优先级"
        ),
        routing_model=(
            "最终承伤先按每击护盾绕过比例分流，未绕过部分由护盾吸收；溢出与绕过部分"
            "再由魔力按比例承担，剩余进入生命"
        ),
    )


def compare_recovery_sequences(
    scenarios: Sequence[tuple[str, RecoverySequenceResult]],
) -> RecoverySequenceComparison:
    if not isinstance(scenarios, (list, tuple)) or not 2 <= len(scenarios) <= 10:
        raise BossRecoveryError("scenarios 必须包含 2..10 个方案")
    names = [str(name).strip() for name, _ in scenarios]
    if any(not name or len(name) > 80 for name in names) or len(set(names)) != len(names):
        raise BossRecoveryError("方案名称必须为不重复的 1..80 个字符")
    rank = {"fails_full_range": 0, "roll_dependent": 1, "survives_full_range": 2}
    rows = tuple(RecoveryScenarioResult(
        name=name, outcome=result.outcome,
        ending_life_high_roll=result.high_roll.ending_life,
        total_active_recovery_high_roll=round(
            result.high_roll.total_timed_rate_recovery_applied
            + result.high_roll.total_leech_recovery_applied
            + result.high_roll.total_instant_recovery_applied,
            6,
        ),
        minimum_life_for_full_range=result.minimum_life_for_full_range.value,
        result=result,
    ) for name, result in scenarios)
    reachable = [row for row in rows if row.minimum_life_for_full_range is not None]
    return RecoverySequenceComparison(
        scenarios=rows,
        best_survival_scenario=max(
            rows, key=lambda row: (rank[row.outcome], row.ending_life_high_roll)
        ).name,
        best_high_roll_life_margin_scenario=max(
            rows, key=lambda row: row.ending_life_high_roll
        ).name,
        highest_active_recovery_scenario=max(
            rows, key=lambda row: row.total_active_recovery_high_roll
        ).name,
        lowest_required_life_scenario=(
            min(reachable, key=lambda row: row.minimum_life_for_full_range or 0).name
            if reachable else None
        ),
    )
