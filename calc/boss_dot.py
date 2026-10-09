"""Boss 命中、显式 DoT 暴露窗口与主动恢复的统一分层时间轴。

本模块只消费已经结算好的命中承伤，以及调用方显式给出的最终 DoT 每秒承伤区间。
DoT 的来源、开始、结束、伤害类型与护盾绕过都不是从技能名、异常、装备或阶段推断的。
连续区间使用守恒流模型同时推进伤害与恢复，并在护盾、魔力、生命触及边界时重新求解；
因此耗尽时间来自事件边界和池边界，而不是固定采样步长。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Sequence

from calc.boss_layered import LayeredDamageEvent
from calc.boss_recovery import ActiveRecoveryEvent, RecoveryPoolConfig


PoolName = Literal["life", "energy_shield", "mana"]
Outcome = Literal["survives_full_range", "roll_dependent", "fails_full_range"]
ThresholdStatus = Literal["not_applicable", "reachable", "unreachable"]
DotRechargeInterruptMode = Literal[
    "energy_shield_damage", "any_damage", "does_not_interrupt"
]
MAX_EVENTS = 100
MAX_TIME_SECONDS = 3600.0
MAX_VALUE = 1_000_000_000.0
EPSILON = 1e-9


class BossDotError(ValueError):
    """DoT 混合时间轴输入不完整、歧义或超出安全范围。"""


@dataclass(frozen=True)
class DotWindow:
    name: str
    start_seconds: float
    duration_seconds: float
    damage_type: Literal["Physical", "Fire", "Cold", "Lightning", "Chaos"]
    taken_dps_low: float
    taken_dps_high: float
    energy_shield_bypass_pct: float = 0.0

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


@dataclass(frozen=True)
class DotTimelineConfig:
    pools: RecoveryPoolConfig
    dot_recharge_interrupt_mode: DotRechargeInterruptMode = "energy_shield_damage"


@dataclass(frozen=True)
class DotSourceSummary:
    name: str
    damage_type: str
    start_seconds: float
    end_seconds: float
    taken_dps: float
    scheduled_damage: float
    processed_damage: float
    unprocessed_after_depletion: float


@dataclass(frozen=True)
class DotSegment:
    start_seconds: float
    end_seconds: float
    active_dot_sources: tuple[str, ...]
    incoming_dot_damage: float
    energy_shield_damage: float
    mana_damage: float
    life_damage: float
    life_recovery_applied: float
    energy_shield_recovery_applied: float
    mana_recovery_applied: float
    life_at_end: float
    energy_shield_at_end: float
    mana_at_end: float
    recharge_interrupted_by_dot: bool
    depleted_at_seconds: float | None


@dataclass(frozen=True)
class DotHitRecord:
    hit_index: int
    at_seconds: float
    skill: str
    damage_type: str
    raw_damage: float
    taken_damage: float
    energy_shield_bypass_pct: float
    energy_shield_absorbed: float
    mana_absorbed: float
    life_damage: float
    life_after: float
    energy_shield_after: float
    mana_after: float
    depleted: bool


@dataclass(frozen=True)
class DotInstantRecoveryRecord:
    name: str
    at_seconds: float
    pool: PoolName
    phase: Literal["before_hit", "after_hit"]
    requested: float
    applied: float
    overflow: float
    skipped_because_depleted: bool


@dataclass(frozen=True)
class DotRollResult:
    damage_basis: Literal["low_roll", "high_roll"]
    survived: bool
    depletion_time_seconds: float | None
    depletion_cause: Literal["hit", "dot"] | None
    depletion_hit: int | None
    ending_life: float
    ending_energy_shield: float
    ending_mana: float
    lowest_life: float
    total_hit_damage: float
    total_dot_damage: float
    total_energy_shield_damage: float
    total_mana_damage: float
    total_life_damage: float
    total_recovery_applied: float
    total_recovery_overflow: float
    total_leech_suppressed_by_rate_cap: float
    recharge_interruption_count: int
    dot_sources: tuple[DotSourceSummary, ...]
    segments: tuple[DotSegment, ...]
    hits: tuple[DotHitRecord, ...]
    instant_recoveries: tuple[DotInstantRecoveryRecord, ...]


@dataclass(frozen=True)
class DotLayerThreshold:
    status: ThresholdStatus
    value: int | None
    current: float
    shortfall: float | None
    keeps_other_layers_fixed: bool = True


@dataclass(frozen=True)
class DotImpact:
    high_roll_survived_without_dot: bool
    high_roll_ending_life_without_dot: float
    high_roll_life_loss_from_dot: float
    changed_high_roll_survival: bool


@dataclass(frozen=True)
class DotTimelineResult:
    config: DotTimelineConfig
    dot_windows: tuple[DotWindow, ...]
    recovery_events: tuple[ActiveRecoveryEvent, ...]
    hit_count: int
    dot_window_count: int
    duration_seconds: float
    outcome: Outcome
    low_roll: DotRollResult
    high_roll: DotRollResult
    dot_impact: DotImpact
    minimum_life_for_full_range: DotLayerThreshold
    minimum_energy_shield_for_full_range: DotLayerThreshold
    minimum_mana_for_full_range: DotLayerThreshold
    event_ordering: str
    continuous_flow_model: str
    routing_model: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DotScenarioResult:
    name: str
    outcome: Outcome
    ending_life_high_roll: float
    depletion_time_high_roll: float | None
    total_dot_damage_high_roll: float
    minimum_life_for_full_range: int | None
    result: DotTimelineResult


@dataclass(frozen=True)
class DotTimelineComparison:
    scenarios: tuple[DotScenarioResult, ...]
    best_survival_scenario: str
    best_high_roll_life_margin_scenario: str
    longest_time_to_depletion_scenario: str
    lowest_dot_damage_scenario: str
    lowest_required_life_scenario: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _State:
    life: float
    energy_shield: float
    mana: float
    alive: bool = True
    depletion_time: float | None = None
    depletion_cause: Literal["hit", "dot"] | None = None
    depletion_hit: int | None = None
    lowest_life: float = math.inf
    last_recharge_interrupt_at: float | None = None
    total_hit_damage: float = 0.0
    total_dot_damage: float = 0.0
    total_es_damage: float = 0.0
    total_mana_damage: float = 0.0
    total_life_damage: float = 0.0
    total_recovery_applied: float = 0.0
    total_recovery_overflow: float = 0.0
    total_leech_suppressed: float = 0.0
    recharge_interruptions: int = 0


@dataclass(frozen=True)
class _Rates:
    incoming: float
    es_damage: float
    mana_damage: float
    life_damage: float
    life_recovery: float
    es_recovery: float
    mana_recovery: float
    leech_suppressed: float


_POOLS: tuple[PoolName, ...] = ("life", "energy_shield", "mana")


def _number(
    value: object, label: str, *, minimum: float = 0.0,
    maximum: float = MAX_VALUE,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BossDotError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise BossDotError(f"{label} 必须在 {minimum:g}..{maximum:g} 之间")
    return result


def _validate_config(config: DotTimelineConfig) -> DotTimelineConfig:
    if not isinstance(config, DotTimelineConfig):
        raise BossDotError("config 必须是 DotTimelineConfig")
    pools = config.pools
    if not isinstance(pools, RecoveryPoolConfig):
        raise BossDotError("config.pools 必须是 RecoveryPoolConfig")
    values: dict[str, Any] = {}
    for field in (
        "life", "energy_shield", "mana", "life_recovery_per_second",
        "energy_shield_recovery_per_second", "mana_recovery_per_second",
        "energy_shield_recharge_per_second", "energy_shield_recharge_delay_seconds",
    ):
        values[field] = _number(getattr(pools, field), f"pools.{field}")
    if values["life"] <= 0:
        raise BossDotError("pools.life 必须大于 0")
    values["mana_before_life_pct"] = _number(
        pools.mana_before_life_pct, "pools.mana_before_life_pct", maximum=100,
    )
    for field in (
        "life_leech_rate_cap_pct", "energy_shield_leech_rate_cap_pct",
        "mana_leech_rate_cap_pct",
    ):
        values[field] = _number(getattr(pools, field), f"pools.{field}", maximum=1000)
    if pools.energy_shield_recharge_interrupt_mode not in {
        "energy_shield_damage", "any_hit",
    }:
        raise BossDotError("命中回充中断模式无效")
    values["energy_shield_recharge_interrupt_mode"] = (
        pools.energy_shield_recharge_interrupt_mode
    )
    if config.dot_recharge_interrupt_mode not in {
        "energy_shield_damage", "any_damage", "does_not_interrupt",
    }:
        raise BossDotError("dot_recharge_interrupt_mode 无效")
    return DotTimelineConfig(
        pools=RecoveryPoolConfig(**values),
        dot_recharge_interrupt_mode=config.dot_recharge_interrupt_mode,
    )


def _validate_hits(events: Sequence[LayeredDamageEvent]) -> tuple[LayeredDamageEvent, ...]:
    if not isinstance(events, (list, tuple)) or len(events) > MAX_EVENTS:
        raise BossDotError(f"hit_events 必须包含 0..{MAX_EVENTS} 次命中")
    previous = -1.0
    normalized: list[LayeredDamageEvent] = []
    for index, event in enumerate(events):
        if not isinstance(event, LayeredDamageEvent):
            raise BossDotError(f"hit_events[{index}] 类型无效")
        damage = event.damage
        at = _number(damage.at_seconds, f"hit_events[{index}].at_seconds", maximum=3600)
        if at < previous:
            raise BossDotError("hit_events 必须按 at_seconds 非递减排列")
        low = _number(damage.taken_low, f"hit_events[{index}].taken_low")
        high = _number(damage.taken_high, f"hit_events[{index}].taken_high")
        raw_low = _number(damage.raw_low, f"hit_events[{index}].raw_low")
        raw_high = _number(damage.raw_high, f"hit_events[{index}].raw_high")
        if low > high or raw_low > raw_high:
            raise BossDotError(f"hit_events[{index}] 的低值不能大于高值")
        bypass = _number(
            event.energy_shield_bypass_pct,
            f"hit_events[{index}].energy_shield_bypass_pct", maximum=100,
        )
        normalized.append(replace(event, energy_shield_bypass_pct=bypass))
        previous = at
    return tuple(normalized)


def _validate_dots(windows: Sequence[DotWindow]) -> tuple[DotWindow, ...]:
    if not isinstance(windows, (list, tuple)) or not 1 <= len(windows) <= MAX_EVENTS:
        raise BossDotError(f"dot_windows 必须包含 1..{MAX_EVENTS} 个窗口")
    normalized: list[DotWindow] = []
    names: set[str] = set()
    for index, window in enumerate(windows):
        if not isinstance(window, DotWindow):
            raise BossDotError(f"dot_windows[{index}] 类型无效")
        name = str(window.name).strip()
        if not 1 <= len(name) <= 80:
            raise BossDotError(f"dot_windows[{index}].name 必须为 1..80 个字符")
        if name in names:
            raise BossDotError("dot_windows.name 必须唯一，才能审计来源归属")
        names.add(name)
        if window.damage_type not in {"Physical", "Fire", "Cold", "Lightning", "Chaos"}:
            raise BossDotError(f"dot_windows[{index}].damage_type 无效")
        start = _number(window.start_seconds, f"dot_windows[{index}].start_seconds", maximum=3600)
        duration = _number(
            window.duration_seconds, f"dot_windows[{index}].duration_seconds",
            minimum=EPSILON, maximum=3600,
        )
        if start + duration > MAX_TIME_SECONDS:
            raise BossDotError(f"dot_windows[{index}] 的结束时间不能超过 3600")
        low = _number(window.taken_dps_low, f"dot_windows[{index}].taken_dps_low")
        high = _number(window.taken_dps_high, f"dot_windows[{index}].taken_dps_high")
        if low > high:
            raise BossDotError(f"dot_windows[{index}] 的低 DPS 不能大于高 DPS")
        bypass = _number(
            window.energy_shield_bypass_pct,
            f"dot_windows[{index}].energy_shield_bypass_pct", maximum=100,
        )
        normalized.append(DotWindow(
            name=name, start_seconds=start, duration_seconds=duration,
            damage_type=window.damage_type, taken_dps_low=low,
            taken_dps_high=high, energy_shield_bypass_pct=bypass,
        ))
    return tuple(sorted(normalized, key=lambda row: (row.start_seconds, row.name)))


def _validate_recoveries(
    events: Sequence[ActiveRecoveryEvent], *, duration: float,
) -> tuple[ActiveRecoveryEvent, ...]:
    if not isinstance(events, (list, tuple)) or len(events) > MAX_EVENTS:
        raise BossDotError(f"recovery_events 必须包含 0..{MAX_EVENTS} 个事件")
    normalized: list[ActiveRecoveryEvent] = []
    for index, event in enumerate(events):
        if not isinstance(event, ActiveRecoveryEvent):
            raise BossDotError(f"recovery_events[{index}] 类型无效")
        name = str(event.name).strip()
        if not 1 <= len(name) <= 80 or event.pool not in _POOLS:
            raise BossDotError(f"recovery_events[{index}] 名称或池无效")
        if event.kind not in {"instant", "timed_rate", "leech"}:
            raise BossDotError(f"recovery_events[{index}].kind 无效")
        at = _number(event.at_seconds, f"recovery_events[{index}].at_seconds", maximum=3600)
        amount = _number(event.amount, f"recovery_events[{index}].amount")
        rate = _number(event.rate_per_second, f"recovery_events[{index}].rate_per_second")
        event_duration = _number(
            event.duration_seconds, f"recovery_events[{index}].duration_seconds",
            maximum=3600,
        )
        if event.phase not in {"before_hit", "after_hit"}:
            raise BossDotError(f"recovery_events[{index}].phase 无效")
        if event.kind == "instant":
            if amount <= 0 or rate != 0 or event_duration != 0:
                raise BossDotError(f"recovery_events[{index}] 瞬回形状无效")
        elif rate <= 0 or event_duration <= 0 or amount != 0 or event.phase != "before_hit":
            raise BossDotError(f"recovery_events[{index}] 持续恢复形状无效")
        if at > duration or at + event_duration > duration + EPSILON:
            raise BossDotError(f"recovery_events[{index}] 不能超出伤害时间轴")
        normalized.append(ActiveRecoveryEvent(
            name=name, at_seconds=at, pool=event.pool, kind=event.kind,
            amount=amount, rate_per_second=rate, duration_seconds=event_duration,
            phase=event.phase,
        ))
    return tuple(sorted(normalized, key=lambda row: (row.at_seconds, row.name)))


def _pool_max(config: RecoveryPoolConfig, pool: PoolName) -> float:
    return float(getattr(config, pool))


def _pool_value(state: _State, pool: PoolName) -> float:
    return float(getattr(state, pool))


def _active_dots(windows: tuple[DotWindow, ...], at: float) -> tuple[DotWindow, ...]:
    return tuple(window for window in windows if window.start_seconds <= at < window.end_seconds)


def _recovery_rates(
    state: _State, *, at: float, config: RecoveryPoolConfig,
    recoveries: tuple[ActiveRecoveryEvent, ...], recharge_allowed: bool,
) -> tuple[dict[PoolName, float], float]:
    rates: dict[PoolName, float] = {
        "life": config.life_recovery_per_second,
        "energy_shield": config.energy_shield_recovery_per_second,
        "mana": config.mana_recovery_per_second,
    }
    suppressed = 0.0
    caps = {
        "life": config.life * config.life_leech_rate_cap_pct / 100,
        "energy_shield": config.energy_shield * config.energy_shield_leech_rate_cap_pct / 100,
        "mana": config.mana * config.mana_leech_rate_cap_pct / 100,
    }
    for pool in _POOLS:
        timed = sum(
            event.rate_per_second for event in recoveries
            if event.pool == pool and event.kind == "timed_rate"
            and event.at_seconds <= at < event.end_seconds
        )
        leech = sum(
            event.rate_per_second for event in recoveries
            if event.pool == pool and event.kind == "leech"
            and event.at_seconds <= at < event.end_seconds
        )
        capped = min(leech, caps[pool])
        suppressed += leech - capped
        rates[pool] += timed + capped
    if recharge_allowed:
        rates["energy_shield"] += config.energy_shield_recharge_per_second
    return rates, suppressed


def _dot_interrupts(
    active: tuple[DotWindow, ...], mode: DotRechargeInterruptMode, *, high_roll: bool,
) -> bool:
    if not active or mode == "does_not_interrupt":
        return False
    if mode == "any_damage":
        return any(
            (window.taken_dps_high if high_roll else window.taken_dps_low) > 0
            for window in active
        )
    return any(
        window.energy_shield_bypass_pct < 100
        and (window.taken_dps_high if high_roll else window.taken_dps_low) > 0
        for window in active
    )


def _rates(
    state: _State, *, active: tuple[DotWindow, ...], high_roll: bool,
    recovery: dict[PoolName, float], config: RecoveryPoolConfig,
) -> _Rates:
    incoming = sum(
        window.taken_dps_high if high_roll else window.taken_dps_low
        for window in active
    )
    bypass = sum(
        (window.taken_dps_high if high_roll else window.taken_dps_low)
        * window.energy_shield_bypass_pct / 100
        for window in active
    )
    non_bypass = max(0.0, incoming - bypass)
    if state.energy_shield > EPSILON:
        es_damage = non_bypass
        lower = bypass
    else:
        es_damage = min(non_bypass, recovery["energy_shield"])
        lower = bypass + non_bypass - es_damage
    mana_target = lower * config.mana_before_life_pct / 100
    if state.mana > EPSILON:
        mana_damage = mana_target
        life_damage = lower - mana_target
    else:
        mana_damage = min(mana_target, recovery["mana"])
        life_damage = lower - mana_damage
    return _Rates(
        incoming=incoming, es_damage=es_damage, mana_damage=mana_damage,
        life_damage=life_damage, life_recovery=recovery["life"],
        es_recovery=recovery["energy_shield"], mana_recovery=recovery["mana"],
        leech_suppressed=0.0,
    )


def _derivative(value: float, maximum: float, recovery: float, damage: float) -> float:
    net = recovery - damage
    if value <= EPSILON and net < 0:
        return 0.0
    if value >= maximum - EPSILON and net > 0:
        return 0.0
    return net


def _time_to_boundary(value: float, maximum: float, derivative: float) -> float:
    if derivative < -EPSILON and value > EPSILON:
        return value / -derivative
    if derivative > EPSILON and value < maximum - EPSILON:
        return (maximum - value) / derivative
    return math.inf


def _flow(
    state: _State, *, start: float, end: float, active: tuple[DotWindow, ...],
    windows: tuple[DotWindow, ...], recoveries: tuple[ActiveRecoveryEvent, ...],
    config: DotTimelineConfig, high_roll: bool, source_processed: dict[str, float],
) -> DotSegment:
    segment_start = (state.life, state.energy_shield, state.mana)
    totals = {"incoming": 0.0, "es": 0.0, "mana": 0.0, "life": 0.0}
    recovery_totals: dict[PoolName, float] = {pool: 0.0 for pool in _POOLS}
    cursor = start
    interrupted = _dot_interrupts(
        active, config.dot_recharge_interrupt_mode, high_roll=high_roll,
    )
    if interrupted:
        if state.last_recharge_interrupt_at is None or start > state.last_recharge_interrupt_at + EPSILON:
            state.recharge_interruptions += 1
        state.last_recharge_interrupt_at = start
    while cursor < end - EPSILON and state.alive:
        recharge_allowed = (
            not interrupted
            and state.last_recharge_interrupt_at is not None
            and cursor >= state.last_recharge_interrupt_at
            + config.pools.energy_shield_recharge_delay_seconds - EPSILON
        )
        recovery, suppressed_rate = _recovery_rates(
            state, at=cursor, config=config.pools, recoveries=recoveries,
            recharge_allowed=recharge_allowed,
        )
        rates = _rates(
            state, active=active, high_roll=high_roll, recovery=recovery,
            config=config.pools,
        )
        derivatives = {
            "life": _derivative(
                state.life, config.pools.life, recovery["life"], rates.life_damage,
            ),
            "energy_shield": _derivative(
                state.energy_shield, config.pools.energy_shield,
                recovery["energy_shield"], rates.es_damage,
            ),
            "mana": _derivative(
                state.mana, config.pools.mana, recovery["mana"], rates.mana_damage,
            ),
        }
        candidates = [end - cursor]
        for pool in _POOLS:
            if pool == "life":
                if derivatives[pool] < -EPSILON:
                    candidates.append(state.life / -derivatives[pool])
            else:
                candidates.append(_time_to_boundary(
                    _pool_value(state, pool), _pool_max(config.pools, pool),
                    derivatives[pool],
                ))
        if not recharge_allowed and not interrupted and state.last_recharge_interrupt_at is not None:
            recharge_at = (
                state.last_recharge_interrupt_at
                + config.pools.energy_shield_recharge_delay_seconds
            )
            if cursor < recharge_at < end:
                candidates.append(recharge_at - cursor)
        dt = min(value for value in candidates if value > EPSILON)
        for window in active:
            dps = window.taken_dps_high if high_roll else window.taken_dps_low
            source_processed[window.name] += dps * dt
        totals["incoming"] += rates.incoming * dt
        totals["es"] += rates.es_damage * dt
        totals["mana"] += rates.mana_damage * dt
        totals["life"] += rates.life_damage * dt
        before = {pool: _pool_value(state, pool) for pool in _POOLS}
        state.life += derivatives["life"] * dt
        state.energy_shield += derivatives["energy_shield"] * dt
        state.mana += derivatives["mana"] * dt
        state.energy_shield = min(config.pools.energy_shield, max(0.0, state.energy_shield))
        state.mana = min(config.pools.mana, max(0.0, state.mana))
        for pool, damage_rate, recovery_rate in (
            ("life", rates.life_damage, recovery["life"]),
            ("energy_shield", rates.es_damage, recovery["energy_shield"]),
            ("mana", rates.mana_damage, recovery["mana"]),
        ):
            after = _pool_value(state, pool)
            applied = max(0.0, after - before[pool] + damage_rate * dt)
            requested = recovery_rate * dt
            recovery_totals[pool] += min(requested, applied)
            state.total_recovery_overflow += max(0.0, requested - applied)
        state.total_leech_suppressed += suppressed_rate * dt
        cursor += dt
        if state.life <= EPSILON and derivatives["life"] < -EPSILON:
            state.life = 0.0
            state.alive = False
            state.depletion_time = cursor
            state.depletion_cause = "dot"
        state.lowest_life = min(state.lowest_life, state.life)
    if interrupted:
        state.last_recharge_interrupt_at = min(end, cursor)
    state.total_dot_damage += totals["incoming"]
    state.total_es_damage += totals["es"]
    state.total_mana_damage += totals["mana"]
    state.total_life_damage += totals["life"]
    state.total_recovery_applied += sum(recovery_totals.values())
    return DotSegment(
        start_seconds=round(start, 6), end_seconds=round(min(end, cursor), 6),
        active_dot_sources=tuple(window.name for window in active),
        incoming_dot_damage=round(totals["incoming"], 6),
        energy_shield_damage=round(totals["es"], 6),
        mana_damage=round(totals["mana"], 6), life_damage=round(totals["life"], 6),
        life_recovery_applied=round(recovery_totals["life"], 6),
        energy_shield_recovery_applied=round(recovery_totals["energy_shield"], 6),
        mana_recovery_applied=round(recovery_totals["mana"], 6),
        life_at_end=round(state.life, 6),
        energy_shield_at_end=round(state.energy_shield, 6),
        mana_at_end=round(state.mana, 6), recharge_interrupted_by_dot=interrupted,
        depleted_at_seconds=(round(state.depletion_time, 6) if not state.alive else None),
    )


def _instant(
    state: _State, event: ActiveRecoveryEvent, *, config: RecoveryPoolConfig,
) -> DotInstantRecoveryRecord:
    before = _pool_value(state, event.pool)
    applied = 0.0 if not state.alive else min(event.amount, _pool_max(config, event.pool) - before)
    if state.alive:
        setattr(state, event.pool, before + applied)
    overflow = event.amount - applied
    state.total_recovery_applied += applied
    state.total_recovery_overflow += overflow
    return DotInstantRecoveryRecord(
        name=event.name, at_seconds=event.at_seconds, pool=event.pool,
        phase=event.phase, requested=round(event.amount, 6), applied=round(applied, 6),
        overflow=round(overflow, 6), skipped_because_depleted=not state.alive,
    )


def _hit(
    state: _State, event: LayeredDamageEvent, *, index: int, high_roll: bool,
    config: RecoveryPoolConfig,
) -> DotHitRecord:
    damage = event.damage.taken_high if high_roll else event.damage.taken_low
    raw = event.damage.raw_high if high_roll else event.damage.raw_low
    es_absorbed = mana_absorbed = life_damage = 0.0
    if state.alive:
        bypass = damage * event.energy_shield_bypass_pct / 100
        reaches_es = damage - bypass
        es_absorbed = min(state.energy_shield, reaches_es)
        state.energy_shield -= es_absorbed
        lower = bypass + reaches_es - es_absorbed
        mana_target = lower * config.mana_before_life_pct / 100
        mana_absorbed = min(state.mana, mana_target)
        state.mana -= mana_absorbed
        life_damage = lower - mana_absorbed
        state.life -= life_damage
        state.total_hit_damage += damage
        state.total_es_damage += es_absorbed
        state.total_mana_damage += mana_absorbed
        state.total_life_damage += life_damage
        interrupted = (
            config.energy_shield_recharge_interrupt_mode == "any_hit"
            or es_absorbed > 0
        )
        if interrupted:
            state.last_recharge_interrupt_at = event.damage.at_seconds
            state.recharge_interruptions += 1
        state.lowest_life = min(state.lowest_life, state.life)
        if state.life <= 0:
            state.alive = False
            state.depletion_time = event.damage.at_seconds
            state.depletion_cause = "hit"
            state.depletion_hit = index
    return DotHitRecord(
        hit_index=index, at_seconds=event.damage.at_seconds, skill=event.damage.skill,
        damage_type=event.damage.damage_type, raw_damage=round(raw, 6),
        taken_damage=round(damage, 6),
        energy_shield_bypass_pct=event.energy_shield_bypass_pct,
        energy_shield_absorbed=round(es_absorbed, 6),
        mana_absorbed=round(mana_absorbed, 6), life_damage=round(life_damage, 6),
        life_after=round(state.life, 6), energy_shield_after=round(state.energy_shield, 6),
        mana_after=round(state.mana, 6), depleted=not state.alive,
    )


def _simulate(
    hits: tuple[LayeredDamageEvent, ...], dots: tuple[DotWindow, ...],
    recoveries: tuple[ActiveRecoveryEvent, ...], config: DotTimelineConfig,
    *, high_roll: bool, duration: float,
) -> DotRollResult:
    state = _State(
        life=config.pools.life, energy_shield=config.pools.energy_shield,
        mana=config.pools.mana, lowest_life=config.pools.life,
    )
    source_processed = {window.name: 0.0 for window in dots}
    segments: list[DotSegment] = []
    hit_records: list[DotHitRecord] = []
    instant_records: list[DotInstantRecoveryRecord] = []
    breakpoints = {0.0, duration}
    breakpoints.update(event.damage.at_seconds for event in hits)
    for window in dots:
        breakpoints.update((window.start_seconds, window.end_seconds))
    for event in recoveries:
        breakpoints.add(event.at_seconds)
        if event.kind != "instant":
            breakpoints.add(event.end_seconds)
    ordered = sorted(breakpoints)
    hit_number = 0
    for position, at in enumerate(ordered):
        if position > 0 and state.alive:
            start = ordered[position - 1]
            active = _active_dots(dots, start)
            segment = _flow(
                state, start=start, end=at, active=active, windows=dots,
                recoveries=recoveries, config=config, high_roll=high_roll,
                source_processed=source_processed,
            )
            if active or any((
                segment.life_recovery_applied, segment.energy_shield_recovery_applied,
                segment.mana_recovery_applied,
            )):
                segments.append(segment)
        before = [
            event for event in recoveries
            if event.kind == "instant" and event.at_seconds == at
            and event.phase == "before_hit"
        ]
        after = [
            event for event in recoveries
            if event.kind == "instant" and event.at_seconds == at
            and event.phase == "after_hit"
        ]
        for event in before:
            instant_records.append(_instant(state, event, config=config.pools))
        for event in hits:
            if event.damage.at_seconds == at:
                hit_number += 1
                hit_records.append(_hit(
                    state, event, index=hit_number, high_roll=high_roll,
                    config=config.pools,
                ))
        for event in after:
            instant_records.append(_instant(state, event, config=config.pools))
        if not state.alive:
            break
    source_rows = tuple(DotSourceSummary(
        name=window.name, damage_type=window.damage_type,
        start_seconds=window.start_seconds, end_seconds=window.end_seconds,
        taken_dps=(window.taken_dps_high if high_roll else window.taken_dps_low),
        scheduled_damage=round(
            (window.taken_dps_high if high_roll else window.taken_dps_low)
            * window.duration_seconds, 6,
        ),
        processed_damage=round(source_processed[window.name], 6),
        unprocessed_after_depletion=round(max(
            0.0,
            (window.taken_dps_high if high_roll else window.taken_dps_low)
            * window.duration_seconds - source_processed[window.name],
        ), 6),
    ) for window in dots)
    return DotRollResult(
        damage_basis="high_roll" if high_roll else "low_roll", survived=state.alive,
        depletion_time_seconds=(round(state.depletion_time, 6) if state.depletion_time is not None else None),
        depletion_cause=state.depletion_cause, depletion_hit=state.depletion_hit,
        ending_life=round(state.life, 6), ending_energy_shield=round(state.energy_shield, 6),
        ending_mana=round(state.mana, 6), lowest_life=round(state.lowest_life, 6),
        total_hit_damage=round(state.total_hit_damage, 6),
        total_dot_damage=round(state.total_dot_damage, 6),
        total_energy_shield_damage=round(state.total_es_damage, 6),
        total_mana_damage=round(state.total_mana_damage, 6),
        total_life_damage=round(state.total_life_damage, 6),
        total_recovery_applied=round(state.total_recovery_applied, 6),
        total_recovery_overflow=round(state.total_recovery_overflow, 6),
        total_leech_suppressed_by_rate_cap=round(state.total_leech_suppressed, 6),
        recharge_interruption_count=state.recharge_interruptions,
        dot_sources=source_rows, segments=tuple(segments), hits=tuple(hit_records),
        instant_recoveries=tuple(instant_records),
    )


def _threshold(
    hits: tuple[LayeredDamageEvent, ...], dots: tuple[DotWindow, ...],
    recoveries: tuple[ActiveRecoveryEvent, ...], config: DotTimelineConfig,
    *, pool: PoolName, duration: float,
) -> DotLayerThreshold:
    current = _pool_max(config.pools, pool)
    if pool != "life" and current <= 0:
        return DotLayerThreshold("not_applicable", None, current, None)
    upper = max(1, math.ceil(current))
    trial = replace(config, pools=replace(config.pools, **{pool: float(upper)}))
    reachable = _simulate(
        hits, dots, recoveries, trial, high_roll=True, duration=duration,
    ).survived
    for _ in range(50):
        if reachable:
            break
        upper *= 2
        if upper > MAX_VALUE:
            break
        trial = replace(config, pools=replace(config.pools, **{pool: float(upper)}))
        reachable = _simulate(
            hits, dots, recoveries, trial, high_roll=True, duration=duration,
        ).survived
    if not reachable:
        return DotLayerThreshold("unreachable", None, current, None)
    lower = 1 if pool == "life" else 0
    while lower < upper:
        middle = (lower + upper) // 2
        trial = replace(config, pools=replace(config.pools, **{pool: float(middle)}))
        if _simulate(hits, dots, recoveries, trial, high_roll=True, duration=duration).survived:
            upper = middle
        else:
            lower = middle + 1
    return DotLayerThreshold(
        "reachable", lower, current, round(max(0.0, lower - current), 6),
    )


def assess_dot_timeline(
    *, hit_events: Sequence[LayeredDamageEvent], dot_windows: Sequence[DotWindow],
    recovery_events: Sequence[ActiveRecoveryEvent], config: DotTimelineConfig,
) -> DotTimelineResult:
    """按精确边界推进低/高命中、DoT 连续流、分层池与显式恢复。"""
    normalized_config = _validate_config(config)
    hits = _validate_hits(hit_events)
    dots = _validate_dots(dot_windows)
    duration = max(
        max((event.damage.at_seconds for event in hits), default=0.0),
        max(window.end_seconds for window in dots),
    )
    recoveries = _validate_recoveries(recovery_events, duration=duration)
    low = _simulate(hits, dots, recoveries, normalized_config, high_roll=False, duration=duration)
    high = _simulate(hits, dots, recoveries, normalized_config, high_roll=True, duration=duration)
    without_dot = _simulate(
        hits, tuple(), recoveries, normalized_config, high_roll=True, duration=duration,
    )
    outcome: Outcome = (
        "survives_full_range" if high.survived
        else "fails_full_range" if not low.survived else "roll_dependent"
    )
    return DotTimelineResult(
        config=normalized_config, dot_windows=dots, recovery_events=recoveries,
        hit_count=len(hits), dot_window_count=len(dots), duration_seconds=duration,
        outcome=outcome, low_roll=low, high_roll=high,
        dot_impact=DotImpact(
            high_roll_survived_without_dot=without_dot.survived,
            high_roll_ending_life_without_dot=without_dot.ending_life,
            high_roll_life_loss_from_dot=round(without_dot.ending_life - high.ending_life, 6),
            changed_high_roll_survival=without_dot.survived != high.survived,
        ),
        minimum_life_for_full_range=_threshold(
            hits, dots, recoveries, normalized_config, pool="life", duration=duration,
        ),
        minimum_energy_shield_for_full_range=_threshold(
            hits, dots, recoveries, normalized_config, pool="energy_shield", duration=duration,
        ),
        minimum_mana_for_full_range=_threshold(
            hits, dots, recoveries, normalized_config, pool="mana", duration=duration,
        ),
        event_ordering=(
            "区间连续流推进到时点；同刻先命中前瞬回，再按输入顺序结算全部命中，最后命中后瞬回"
        ),
        continuous_flow_model=(
            "每个恒定速率区间同时推进 DoT 与恢复，并在护盾、魔力或生命触及边界时解析重分流；"
            "不是固定步长采样"
        ),
        routing_model=(
            "每个 DoT 窗口按显式护盾绕过比例路由；其余先由护盾承担，再按显式 MoM 比例由魔力"
            "承担，短缺进入生命；DoT 回充中断使用独立显式模式"
        ),
    )


def compare_dot_timelines(
    scenarios: Sequence[tuple[str, DotTimelineResult]],
) -> DotTimelineComparison:
    if not isinstance(scenarios, (list, tuple)) or not 2 <= len(scenarios) <= 10:
        raise BossDotError("scenarios 必须包含 2..10 个方案")
    names = [str(name).strip() for name, _ in scenarios]
    if any(not name or len(name) > 80 for name in names) or len(set(names)) != len(names):
        raise BossDotError("方案名称必须为不重复的 1..80 个字符")
    rows = tuple(DotScenarioResult(
        name=name, outcome=result.outcome,
        ending_life_high_roll=result.high_roll.ending_life,
        depletion_time_high_roll=result.high_roll.depletion_time_seconds,
        total_dot_damage_high_roll=result.high_roll.total_dot_damage,
        minimum_life_for_full_range=result.minimum_life_for_full_range.value,
        result=result,
    ) for name, result in scenarios)
    rank = {"fails_full_range": 0, "roll_dependent": 1, "survives_full_range": 2}
    reachable = [row for row in rows if row.minimum_life_for_full_range is not None]
    return DotTimelineComparison(
        scenarios=rows,
        best_survival_scenario=max(
            rows, key=lambda row: (rank[row.outcome], row.ending_life_high_roll)
        ).name,
        best_high_roll_life_margin_scenario=max(
            rows, key=lambda row: row.ending_life_high_roll
        ).name,
        longest_time_to_depletion_scenario=max(
            rows, key=lambda row: (
                row.depletion_time_high_roll is None,
                row.depletion_time_high_roll or 0.0,
            ),
        ).name,
        lowest_dot_damage_scenario=min(rows, key=lambda row: row.total_dot_damage_high_roll).name,
        lowest_required_life_scenario=(
            min(reachable, key=lambda row: row.minimum_life_for_full_range or 0).name
            if reachable else None
        ),
    )
