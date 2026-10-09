"""Boss 显式时间轴上的生命、能量护盾与魔力分层状态机。

Boss 伤害减免仍由既有链路完成；本模块只接收最终承伤区间。每次事件的能量护盾绕过比例、
全局的魔力承伤比例和所有回复速率都必须由调用方明确提供。这样可以观察分池与回充中断，
又不会从伤害类型或 Build 职业猜测 CI、MoM、EB、Shavronne 等特殊规则。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Sequence

from calc.boss_timeline import TimedDamageEvent


Outcome = Literal["survives_full_range", "roll_dependent", "fails_full_range"]
ThresholdStatus = Literal["not_applicable", "reachable", "unreachable"]
RechargeInterruptMode = Literal["energy_shield_damage", "any_hit"]
MAX_EVENTS = 100
MAX_TIME_SECONDS = 3600.0


class BossLayeredError(ValueError):
    """分层防御池输入不完整或超出有界范围。"""


@dataclass(frozen=True)
class LayeredDamageEvent:
    damage: TimedDamageEvent
    energy_shield_bypass_pct: float = 0.0


@dataclass(frozen=True)
class LayeredPoolConfig:
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


@dataclass(frozen=True)
class LayeredSequenceStep:
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


@dataclass(frozen=True)
class LayeredRollResult:
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
    total_energy_shield_bypassed: float
    total_mana_absorbed: float
    total_life_damage: float
    total_life_recovered: float
    total_energy_shield_recovered: float
    total_energy_shield_recharged: float
    total_mana_recovered: float
    recharge_interruption_count: int
    recharge_active_seconds: float
    steps: tuple[LayeredSequenceStep, ...]


@dataclass(frozen=True)
class LayerThreshold:
    status: ThresholdStatus
    value: int | None
    current: float
    shortfall: float | None
    keeps_other_layers_fixed: bool = True


@dataclass(frozen=True)
class LayeredSequenceResult:
    config: LayeredPoolConfig
    hit_count: int
    duration_seconds: float
    outcome: Outcome
    low_roll: LayeredRollResult
    high_roll: LayeredRollResult
    minimum_life_for_any_roll: LayerThreshold
    minimum_life_for_full_range: LayerThreshold
    minimum_energy_shield_for_full_range: LayerThreshold
    minimum_mana_for_full_range: LayerThreshold
    routing_model: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LayeredScenarioResult:
    name: str
    outcome: Outcome
    ending_life_high_roll: float
    lowest_life_high_roll: float
    total_life_damage_high_roll: float
    minimum_life_for_full_range: int | None
    result: LayeredSequenceResult


@dataclass(frozen=True)
class LayeredSequenceComparison:
    scenarios: tuple[LayeredScenarioResult, ...]
    best_survival_scenario: str
    best_high_roll_life_margin_scenario: str
    lowest_high_roll_life_damage_scenario: str
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
    total_energy_shield_absorbed: float = 0.0
    total_energy_shield_bypassed: float = 0.0
    total_mana_absorbed: float = 0.0
    total_life_damage: float = 0.0
    total_life_recovered: float = 0.0
    total_energy_shield_recovered: float = 0.0
    total_energy_shield_recharged: float = 0.0
    total_mana_recovered: float = 0.0
    recharge_interruption_count: int = 0
    recharge_active_seconds: float = 0.0


def _number(
    value: object, label: str, *, minimum: float = 0.0, maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BossLayeredError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise BossLayeredError(f"{label} 必须是大于等于 {minimum:g} 的有限数字")
    if maximum is not None and result > maximum:
        raise BossLayeredError(f"{label} 不能超过 {maximum:g}")
    return result


def _validate_config(config: LayeredPoolConfig) -> LayeredPoolConfig:
    if not isinstance(config, LayeredPoolConfig):
        raise BossLayeredError("config 必须是 LayeredPoolConfig")
    values: dict[str, Any] = {}
    for field in (
        "life", "energy_shield", "mana", "life_recovery_per_second",
        "energy_shield_recovery_per_second", "mana_recovery_per_second",
        "energy_shield_recharge_per_second",
        "energy_shield_recharge_delay_seconds",
    ):
        values[field] = _number(getattr(config, field), field)
    values["mana_before_life_pct"] = _number(
        config.mana_before_life_pct, "mana_before_life_pct", maximum=100
    )
    if values["life"] <= 0:
        raise BossLayeredError("life 必须大于 0")
    if config.energy_shield_recharge_interrupt_mode not in {
        "energy_shield_damage", "any_hit",
    }:
        raise BossLayeredError(
            "energy_shield_recharge_interrupt_mode 只支持 energy_shield_damage 或 any_hit"
        )
    values["energy_shield_recharge_interrupt_mode"] = (
        config.energy_shield_recharge_interrupt_mode
    )
    return LayeredPoolConfig(**values)


def _validate_events(
    events: Sequence[LayeredDamageEvent],
) -> tuple[LayeredDamageEvent, ...]:
    if not isinstance(events, (list, tuple)) or not 1 <= len(events) <= MAX_EVENTS:
        raise BossLayeredError(f"events 必须包含 1..{MAX_EVENTS} 次命中")
    normalized: list[LayeredDamageEvent] = []
    previous = -1.0
    for index, event in enumerate(events):
        if not isinstance(event, LayeredDamageEvent):
            raise BossLayeredError(f"events[{index}] 必须是 LayeredDamageEvent")
        damage = event.damage
        if not isinstance(damage, TimedDamageEvent):
            raise BossLayeredError(f"events[{index}].damage 必须是 TimedDamageEvent")
        at = _number(damage.at_seconds, f"events[{index}].at_seconds")
        if at > MAX_TIME_SECONDS:
            raise BossLayeredError(f"events[{index}].at_seconds 不能超过 {MAX_TIME_SECONDS:g}")
        if at < previous:
            raise BossLayeredError("events 必须按 at_seconds 非递减排列")
        _number(damage.raw_low, f"events[{index}].raw_low")
        _number(damage.raw_high, f"events[{index}].raw_high")
        _number(damage.taken_low, f"events[{index}].taken_low")
        _number(damage.taken_high, f"events[{index}].taken_high")
        if damage.raw_low > damage.raw_high or damage.taken_low > damage.taken_high:
            raise BossLayeredError(f"events[{index}] 的低值不能大于高值")
        bypass = _number(
            event.energy_shield_bypass_pct,
            f"events[{index}].energy_shield_bypass_pct", maximum=100,
        )
        normalized.append(LayeredDamageEvent(damage=damage, energy_shield_bypass_pct=bypass))
        previous = at
    return tuple(normalized)


def _fixed_recovery(current: float, maximum: float, rate: float, elapsed: float) -> tuple[float, float]:
    after = min(maximum, current + rate * elapsed)
    return after, max(0.0, after - current)


def _recharge_seconds(
    *, previous_time: float | None, at_seconds: float,
    last_interrupt: float | None, delay: float,
) -> float:
    if previous_time is None or last_interrupt is None:
        return 0.0
    start = max(previous_time, last_interrupt + delay)
    return max(0.0, at_seconds - start)


def _simulate(
    events: tuple[LayeredDamageEvent, ...], *, config: LayeredPoolConfig,
    high_roll: bool,
) -> LayeredRollResult:
    state = _State(
        life=config.life, energy_shield=config.energy_shield, mana=config.mana,
    )
    previous_time: float | None = None
    steps: list[LayeredSequenceStep] = []
    total_taken = 0.0
    for index, event in enumerate(events, start=1):
        damage = event.damage.taken_high if high_roll else event.damage.taken_low
        raw = event.damage.raw_high if high_roll else event.damage.raw_low
        elapsed = 0.0 if previous_time is None else event.damage.at_seconds - previous_time
        life_before_recovery = state.life
        es_before_recovery = state.energy_shield
        mana_before_recovery = state.mana
        life_recovered = es_recovered = es_recharged = mana_recovered = 0.0
        recharge_eligible = 0.0
        if state.alive:
            state.life, life_recovered = _fixed_recovery(
                state.life, config.life, config.life_recovery_per_second, elapsed,
            )
            state.energy_shield, es_recovered = _fixed_recovery(
                state.energy_shield, config.energy_shield,
                config.energy_shield_recovery_per_second, elapsed,
            )
            recharge_eligible = _recharge_seconds(
                previous_time=previous_time, at_seconds=event.damage.at_seconds,
                last_interrupt=state.last_recharge_interrupt_at,
                delay=config.energy_shield_recharge_delay_seconds,
            )
            before_recharge = state.energy_shield
            state.energy_shield = min(
                config.energy_shield,
                state.energy_shield
                + config.energy_shield_recharge_per_second * recharge_eligible,
            )
            es_recharged = state.energy_shield - before_recharge
            state.mana, mana_recovered = _fixed_recovery(
                state.mana, config.mana,
                config.mana_recovery_per_second, elapsed,
            )
            state.total_life_recovered += life_recovered
            state.total_energy_shield_recovered += es_recovered
            state.total_energy_shield_recharged += es_recharged
            state.total_mana_recovered += mana_recovered
            state.recharge_active_seconds += recharge_eligible

        life_before_hit = state.life
        es_before_hit = state.energy_shield
        mana_before_hit = state.mana
        es_absorbed = es_bypassed = mana_target = mana_absorbed = 0.0
        mana_shortfall = life_damage = 0.0
        interrupted = False
        if state.alive:
            total_taken += damage
            es_bypassed = damage * event.energy_shield_bypass_pct / 100
            reaches_es = damage - es_bypassed
            es_absorbed = min(state.energy_shield, reaches_es)
            state.energy_shield -= es_absorbed
            reaches_life_mana = es_bypassed + (reaches_es - es_absorbed)
            mana_target = reaches_life_mana * config.mana_before_life_pct / 100
            mana_absorbed = min(state.mana, mana_target)
            state.mana -= mana_absorbed
            mana_shortfall = mana_target - mana_absorbed
            life_damage = reaches_life_mana - mana_absorbed
            state.life -= life_damage
            state.total_energy_shield_absorbed += es_absorbed
            state.total_energy_shield_bypassed += es_bypassed
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
                state.last_recharge_interrupt_at = event.damage.at_seconds
                state.recharge_interruption_count += 1
            if state.life <= 0:
                state.alive = False
                state.depletion_hit = index

        steps.append(LayeredSequenceStep(
            hit_index=index, at_seconds=event.damage.at_seconds,
            elapsed_since_previous_hit=elapsed, skill=event.damage.skill,
            damage_type=event.damage.damage_type, raw_damage=round(raw, 6),
            taken_damage=round(damage, 6),
            energy_shield_bypass_pct=event.energy_shield_bypass_pct,
            life_before_recovery=round(life_before_recovery, 6),
            energy_shield_before_recovery=round(es_before_recovery, 6),
            mana_before_recovery=round(mana_before_recovery, 6),
            life_recovered=round(life_recovered, 6),
            energy_shield_recovered=round(es_recovered, 6),
            energy_shield_recharged=round(es_recharged, 6),
            mana_recovered=round(mana_recovered, 6),
            recharge_eligible_seconds=round(recharge_eligible, 6),
            life_before_hit=round(life_before_hit, 6),
            energy_shield_before_hit=round(es_before_hit, 6),
            mana_before_hit=round(mana_before_hit, 6),
            energy_shield_absorbed=round(es_absorbed, 6),
            energy_shield_bypassed=round(es_bypassed, 6),
            mana_target_damage=round(mana_target, 6),
            mana_absorbed=round(mana_absorbed, 6),
            mana_shortfall_to_life=round(mana_shortfall, 6),
            life_damage=round(life_damage, 6),
            life_after_hit=round(state.life, 6),
            energy_shield_after_hit=round(state.energy_shield, 6),
            mana_after_hit=round(state.mana, 6),
            recharge_interrupted=interrupted, depleted=not state.alive,
        ))
        previous_time = event.damage.at_seconds

    return LayeredRollResult(
        damage_basis="high_roll" if high_roll else "low_roll",
        survived=state.alive, depletion_hit=state.depletion_hit,
        ending_life=round(state.life, 6),
        ending_energy_shield=round(state.energy_shield, 6),
        ending_mana=round(state.mana, 6),
        lowest_life=round(state.lowest_life, 6),
        lowest_life_hit=state.lowest_life_hit,
        total_taken_damage=round(total_taken, 6),
        total_energy_shield_absorbed=round(state.total_energy_shield_absorbed, 6),
        total_energy_shield_bypassed=round(state.total_energy_shield_bypassed, 6),
        total_mana_absorbed=round(state.total_mana_absorbed, 6),
        total_life_damage=round(state.total_life_damage, 6),
        total_life_recovered=round(state.total_life_recovered, 6),
        total_energy_shield_recovered=round(state.total_energy_shield_recovered, 6),
        total_energy_shield_recharged=round(state.total_energy_shield_recharged, 6),
        total_mana_recovered=round(state.total_mana_recovered, 6),
        recharge_interruption_count=state.recharge_interruption_count,
        recharge_active_seconds=round(state.recharge_active_seconds, 6),
        steps=tuple(steps),
    )


def _survives_with(
    events: tuple[LayeredDamageEvent, ...], config: LayeredPoolConfig,
    *, field: Literal["life", "energy_shield", "mana"], value: float,
    high_roll: bool,
) -> bool:
    return _simulate(
        events, config=replace(config, **{field: value}), high_roll=high_roll,
    ).survived


def _threshold(
    events: tuple[LayeredDamageEvent, ...], config: LayeredPoolConfig,
    *, field: Literal["life", "energy_shield", "mana"], high_roll: bool,
    applicable: bool = True,
) -> LayerThreshold:
    current = float(getattr(config, field))
    if not applicable:
        return LayerThreshold(
            status="not_applicable", value=None, current=current, shortfall=None,
        )
    total = sum(
        event.damage.taken_high if high_roll else event.damage.taken_low
        for event in events
    )
    upper = max(1, math.floor(total + config.life + config.energy_shield + config.mana) + 1)
    if not _survives_with(
        events, config, field=field, value=float(upper), high_roll=high_roll,
    ):
        return LayerThreshold(
            status="unreachable", value=None, current=current, shortfall=None,
        )
    lower = 0 if field != "life" else 1
    while lower < upper:
        middle = (lower + upper) // 2
        if _survives_with(
            events, config, field=field, value=float(middle), high_roll=high_roll,
        ):
            upper = middle
        else:
            lower = middle + 1
    return LayerThreshold(
        status="reachable", value=lower, current=current,
        shortfall=round(max(0.0, lower - current), 6),
    )


def assess_layered_sequence(
    *, events: Sequence[LayeredDamageEvent], config: LayeredPoolConfig,
) -> LayeredSequenceResult:
    """推进低/高承伤轨的三池路由、回复、回充与耗尽状态。"""
    normalized = _validate_events(events)
    checked = _validate_config(config)
    low = _simulate(normalized, config=checked, high_roll=False)
    high = _simulate(normalized, config=checked, high_roll=True)
    if high.survived:
        outcome: Outcome = "survives_full_range"
    elif not low.survived:
        outcome = "fails_full_range"
    else:
        outcome = "roll_dependent"
    es_applicable = checked.energy_shield > 0 or any(
        event.energy_shield_bypass_pct < 100 for event in normalized
    )
    mana_applicable = checked.mana_before_life_pct > 0
    return LayeredSequenceResult(
        config=checked, hit_count=len(normalized),
        duration_seconds=normalized[-1].damage.at_seconds, outcome=outcome,
        low_roll=low, high_roll=high,
        minimum_life_for_any_roll=_threshold(
            normalized, checked, field="life", high_roll=False,
        ),
        minimum_life_for_full_range=_threshold(
            normalized, checked, field="life", high_roll=True,
        ),
        minimum_energy_shield_for_full_range=_threshold(
            normalized, checked, field="energy_shield", high_roll=True,
            applicable=es_applicable,
        ),
        minimum_mana_for_full_range=_threshold(
            normalized, checked, field="mana", high_roll=True,
            applicable=mana_applicable,
        ),
        routing_model=(
            "最终承伤先按每击 energy_shield_bypass_pct 分流；未绕过部分由能量护盾吸收，"
            "溢出与绕过部分再按 mana_before_life_pct 由魔力承担，魔力不足的份额回落到生命"
        ),
    )


def compare_layered_sequences(
    scenarios: Sequence[tuple[str, LayeredSequenceResult]],
) -> LayeredSequenceComparison:
    if not isinstance(scenarios, (list, tuple)) or not 2 <= len(scenarios) <= 10:
        raise BossLayeredError("scenarios 必须包含 2..10 个方案")
    names = [str(name).strip() for name, _ in scenarios]
    if any(not name or len(name) > 80 for name in names) or len(set(names)) != len(names):
        raise BossLayeredError("方案名称必须为不重复的 1..80 个字符")
    rank = {"fails_full_range": 0, "roll_dependent": 1, "survives_full_range": 2}
    rows = tuple(LayeredScenarioResult(
        name=name, outcome=result.outcome,
        ending_life_high_roll=result.high_roll.ending_life,
        lowest_life_high_roll=result.high_roll.lowest_life,
        total_life_damage_high_roll=result.high_roll.total_life_damage,
        minimum_life_for_full_range=result.minimum_life_for_full_range.value,
        result=result,
    ) for name, result in scenarios)
    reachable = [row for row in rows if row.minimum_life_for_full_range is not None]
    return LayeredSequenceComparison(
        scenarios=rows,
        best_survival_scenario=max(
            rows, key=lambda row: (rank[row.outcome], row.lowest_life_high_roll)
        ).name,
        best_high_roll_life_margin_scenario=max(
            rows, key=lambda row: row.lowest_life_high_roll
        ).name,
        lowest_high_roll_life_damage_scenario=min(
            rows, key=lambda row: row.total_life_damage_high_roll
        ).name,
        lowest_required_life_scenario=(
            min(reachable, key=lambda row: row.minimum_life_for_full_range or 0).name
            if reachable else None
        ),
    )
