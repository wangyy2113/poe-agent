"""Boss 显式临时减伤窗口与 Guard 吸收池预处理器。

临时防御发生在生命/护盾/魔力路由之前：匹配窗口先按 ``priority`` 顺序应用承伤乘数，
再由同序 Guard 容量吸收。连续 DoT 在 Guard 耗尽时精确切段，之后交给
:mod:`calc.boss_dot` 的分层连续流状态机。窗口、类型过滤和容量全部来自调用方；本模块
不从守护技能、Ward、装备、阶段或技能名推断临时防御。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal, Sequence

from calc import boss_dot
from calc.boss_dot import DotTimelineConfig, DotTimelineResult, DotWindow
from calc.boss_layered import LayeredDamageEvent
from calc.boss_recovery import ActiveRecoveryEvent
from calc.boss_timeline import TimedDamageEvent


DamageType = Literal["Physical", "Fire", "Cold", "Lightning", "Chaos"]
MitigationScope = Literal["hit", "dot", "both"]
Outcome = Literal["survives_full_range", "roll_dependent", "fails_full_range"]
MAX_WINDOWS = 100
MAX_TIME_SECONDS = 3600.0
MAX_VALUE = 1_000_000_000.0
EPSILON = 1e-9


class BossMitigationError(ValueError):
    """临时减伤窗口输入不完整、歧义或超出有界范围。"""


@dataclass(frozen=True)
class MitigationWindow:
    name: str
    start_seconds: float
    duration_seconds: float
    scope: MitigationScope = "both"
    damage_types: tuple[DamageType, ...] = ()
    taken_multiplier: float = 1.0
    guard_capacity: float = 0.0
    priority: int = 100

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds

    def active_at(self, at: float) -> bool:
        return self.start_seconds <= at < self.end_seconds

    def matches(self, *, at: float, scope: Literal["hit", "dot"], damage_type: str) -> bool:
        return (
            self.active_at(at)
            and self.scope in {scope, "both"}
            and (not self.damage_types or damage_type in self.damage_types)
        )


@dataclass(frozen=True)
class GuardAbsorption:
    window: str
    requested: float
    absorbed: float
    remaining_after: float
    depleted: bool


@dataclass(frozen=True)
class MitigatedHit:
    hit_index: int
    at_seconds: float
    skill: str
    damage_type: str
    damage_before_mitigation: float
    damage_after_multipliers: float
    damage_after_guards: float
    multiplier_prevented: float
    guard_absorbed: float
    matching_windows: tuple[str, ...]
    guard_absorptions: tuple[GuardAbsorption, ...]


@dataclass(frozen=True)
class MitigatedDotSegment:
    start_seconds: float
    end_seconds: float
    active_sources: tuple[str, ...]
    active_windows: tuple[str, ...]
    damage_before_mitigation: float
    damage_after_multipliers: float
    damage_after_guards: float
    multiplier_prevented: float
    guard_absorbed: float
    depleted_windows: tuple[str, ...]


@dataclass(frozen=True)
class MitigationWindowSummary:
    name: str
    priority: int
    initial_guard_capacity: float
    multiplier_prevented: float
    guard_absorbed: float
    unused_guard_capacity: float
    guard_depletion_time_seconds: float | None


@dataclass(frozen=True)
class MitigationRollResult:
    damage_basis: Literal["low_roll", "high_roll"]
    damage_before_mitigation: float
    damage_after_mitigation: float
    multiplier_prevented: float
    guard_absorbed: float
    total_prevented: float
    prevented_pct: float
    hit_steps: tuple[MitigatedHit, ...]
    dot_segments: tuple[MitigatedDotSegment, ...]
    windows: tuple[MitigationWindowSummary, ...]
    timeline: boss_dot.DotRollResult


@dataclass(frozen=True)
class MitigationImpact:
    high_roll_survived_without_mitigation: bool
    high_roll_ending_life_without_mitigation: float
    high_roll_ending_life_gain: float
    changed_high_roll_survival: bool
    high_roll_damage_prevented: float


@dataclass(frozen=True)
class MitigationTimelineResult:
    config: DotTimelineConfig
    mitigation_windows: tuple[MitigationWindow, ...]
    dot_windows: tuple[DotWindow, ...]
    recovery_events: tuple[ActiveRecoveryEvent, ...]
    hit_count: int
    dot_window_count: int
    mitigation_window_count: int
    duration_seconds: float
    outcome: Outcome
    low_roll: MitigationRollResult
    high_roll: MitigationRollResult
    mitigation_impact: MitigationImpact
    minimum_life_for_full_range: boss_dot.DotLayerThreshold
    minimum_energy_shield_for_full_range: boss_dot.DotLayerThreshold
    minimum_mana_for_full_range: boss_dot.DotLayerThreshold
    ordering_model: str
    window_boundary_model: str
    attribution_model: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MitigationScenarioResult:
    name: str
    outcome: Outcome
    ending_life_high_roll: float
    total_prevented_high_roll: float
    depletion_time_high_roll: float | None
    minimum_life_for_full_range: int | None
    result: MitigationTimelineResult


@dataclass(frozen=True)
class MitigationTimelineComparison:
    scenarios: tuple[MitigationScenarioResult, ...]
    best_survival_scenario: str
    best_high_roll_life_margin_scenario: str
    highest_prevented_damage_scenario: str
    longest_time_to_depletion_scenario: str
    lowest_required_life_scenario: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Track:
    remaining: dict[str, float]
    multiplier_prevented: dict[str, float]
    guard_absorbed: dict[str, float]
    depletion_times: dict[str, float | None]
    hit_steps: list[MitigatedHit]
    dot_segments: list[MitigatedDotSegment]
    fragments: list[tuple[str, float, float, float]]


def _number(
    value: object, label: str, *, minimum: float = 0.0,
    maximum: float = MAX_VALUE,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BossMitigationError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise BossMitigationError(f"{label} 必须在 {minimum:g}..{maximum:g} 之间")
    return result


def _validate_windows(
    windows: Sequence[MitigationWindow], *, duration: float,
) -> tuple[MitigationWindow, ...]:
    if not isinstance(windows, (list, tuple)) or not 1 <= len(windows) <= MAX_WINDOWS:
        raise BossMitigationError(f"mitigation_windows 必须包含 1..{MAX_WINDOWS} 个窗口")
    normalized: list[MitigationWindow] = []
    names: set[str] = set()
    for index, window in enumerate(windows):
        if not isinstance(window, MitigationWindow):
            raise BossMitigationError(f"mitigation_windows[{index}] 类型无效")
        name = str(window.name).strip()
        if not 1 <= len(name) <= 80 or name in names:
            raise BossMitigationError("临时减伤窗口名称必须为不重复的 1..80 个字符")
        names.add(name)
        start = _number(
            window.start_seconds, f"mitigation_windows[{index}].start_seconds",
            maximum=MAX_TIME_SECONDS,
        )
        length = _number(
            window.duration_seconds, f"mitigation_windows[{index}].duration_seconds",
            minimum=EPSILON, maximum=MAX_TIME_SECONDS,
        )
        if start + length > duration + EPSILON:
            raise BossMitigationError(
                f"mitigation_windows[{index}] 不能超出伤害时间轴 {duration:g}s"
            )
        if window.scope not in {"hit", "dot", "both"}:
            raise BossMitigationError(f"mitigation_windows[{index}].scope 无效")
        types = tuple(dict.fromkeys(window.damage_types))
        if any(item not in {"Physical", "Fire", "Cold", "Lightning", "Chaos"} for item in types):
            raise BossMitigationError(f"mitigation_windows[{index}].damage_types 无效")
        multiplier = _number(
            window.taken_multiplier, f"mitigation_windows[{index}].taken_multiplier",
            maximum=1,
        )
        capacity = _number(
            window.guard_capacity, f"mitigation_windows[{index}].guard_capacity",
        )
        if multiplier == 1 and capacity == 0:
            raise BossMitigationError(
                f"mitigation_windows[{index}] 必须降低承伤或提供正 Guard 容量"
            )
        if isinstance(window.priority, bool) or not isinstance(window.priority, int):
            raise BossMitigationError(f"mitigation_windows[{index}].priority 必须是整数")
        if not 0 <= window.priority <= 1000:
            raise BossMitigationError(f"mitigation_windows[{index}].priority 必须在 0..1000")
        normalized.append(MitigationWindow(
            name=name, start_seconds=start, duration_seconds=length,
            scope=window.scope, damage_types=types, taken_multiplier=multiplier,
            guard_capacity=capacity, priority=window.priority,
        ))
    return tuple(sorted(normalized, key=lambda row: (row.priority, row.start_seconds, row.name)))


def _duration(hits: Sequence[LayeredDamageEvent], dots: Sequence[DotWindow]) -> float:
    return max(
        max((event.damage.at_seconds for event in hits), default=0.0),
        max((window.end_seconds for window in dots), default=0.0),
    )


def _new_track(windows: tuple[MitigationWindow, ...]) -> _Track:
    return _Track(
        remaining={window.name: window.guard_capacity for window in windows},
        multiplier_prevented={window.name: 0.0 for window in windows},
        guard_absorbed={window.name: 0.0 for window in windows},
        depletion_times={window.name: None for window in windows},
        hit_steps=[], dot_segments=[], fragments=[],
    )


def _matching(
    windows: tuple[MitigationWindow, ...], *, at: float,
    scope: Literal["hit", "dot"], damage_type: str,
) -> tuple[MitigationWindow, ...]:
    return tuple(
        window for window in windows
        if window.matches(at=at, scope=scope, damage_type=damage_type)
    )


def _multiply(
    amount: float, matching: tuple[MitigationWindow, ...], track: _Track,
    *, scale: float = 1.0,
) -> float:
    value = amount
    for window in matching:
        after = value * window.taken_multiplier
        track.multiplier_prevented[window.name] += (value - after) * scale
        value = after
    return value


def _absorb_hit(
    amount: float, matching: tuple[MitigationWindow, ...], track: _Track,
    *, at: float,
) -> tuple[float, tuple[GuardAbsorption, ...]]:
    value = amount
    rows: list[GuardAbsorption] = []
    for window in matching:
        before = track.remaining[window.name]
        requested = value
        absorbed = min(before, requested)
        value -= absorbed
        after = before - absorbed
        track.remaining[window.name] = after
        track.guard_absorbed[window.name] += absorbed
        depleted = before > EPSILON and after <= EPSILON
        if depleted and track.depletion_times[window.name] is None:
            track.depletion_times[window.name] = at
        if window.guard_capacity > 0:
            rows.append(GuardAbsorption(
                window=window.name, requested=round(requested, 6),
                absorbed=round(absorbed, 6), remaining_after=round(after, 6),
                depleted=depleted,
            ))
    return value, tuple(rows)


def _active_sources(
    dots: tuple[DotWindow, ...], at: float,
) -> tuple[DotWindow, ...]:
    return tuple(window for window in dots if window.start_seconds <= at < window.end_seconds)


def _dot_rates(
    sources: tuple[DotWindow, ...], windows: tuple[MitigationWindow, ...],
    track: _Track, *, at: float, high_roll: bool,
) -> tuple[dict[str, float], dict[str, float], dict[str, tuple[MitigationWindow, ...]]]:
    before: dict[str, float] = {}
    after: dict[str, float] = {}
    matches: dict[str, tuple[MitigationWindow, ...]] = {}
    for source in sources:
        rate = source.taken_dps_high if high_roll else source.taken_dps_low
        matching = _matching(
            windows, at=at, scope="dot", damage_type=source.damage_type,
        )
        before[source.name] = rate
        # Attribution is integrated after the exact subsegment duration is known.
        value = rate
        for window in matching:
            value *= window.taken_multiplier
        after[source.name] = value
        matches[source.name] = matching
    return before, after, matches


def _guard_rates(
    residual: dict[str, float], sources: tuple[DotWindow, ...],
    windows: tuple[MitigationWindow, ...], track: _Track, *, at: float,
) -> tuple[dict[str, float], dict[str, float]]:
    values = dict(residual)
    absorbed_rates = {window.name: 0.0 for window in windows}
    source_types = {source.name: source.damage_type for source in sources}
    for window in windows:
        if track.remaining[window.name] <= EPSILON or window.guard_capacity <= 0:
            continue
        eligible = [
            name for name, rate in values.items()
            if rate > 0 and window.matches(
                at=at, scope="dot", damage_type=source_types[name],
            )
        ]
        rate = sum(values[name] for name in eligible)
        if rate <= 0:
            continue
        absorbed_rates[window.name] = rate
        for name in eligible:
            values[name] = 0.0
    return values, absorbed_rates


def _record_multiplier_attribution(
    sources: tuple[DotWindow, ...], matches: dict[str, tuple[MitigationWindow, ...]],
    track: _Track, *, high_roll: bool, duration: float,
) -> None:
    for source in sources:
        value = source.taken_dps_high if high_roll else source.taken_dps_low
        for window in matches[source.name]:
            after = value * window.taken_multiplier
            track.multiplier_prevented[window.name] += (value - after) * duration
            value = after


def _process_dot_interval(
    track: _Track, *, start: float, end: float,
    sources: tuple[DotWindow, ...], windows: tuple[MitigationWindow, ...],
    high_roll: bool,
) -> None:
    cursor = start
    while cursor < end - EPSILON:
        before, multiplied, matches = _dot_rates(
            sources, windows, track, at=cursor, high_roll=high_roll,
        )
        residual, absorbed_rates = _guard_rates(
            multiplied, sources, windows, track, at=cursor,
        )
        dt = end - cursor
        for window in windows:
            rate = absorbed_rates[window.name]
            if rate > EPSILON:
                dt = min(dt, track.remaining[window.name] / rate)
        if dt <= EPSILON:
            # Normalize exact-zero guards before recomputing rates.
            for window in windows:
                if track.remaining[window.name] <= EPSILON:
                    track.remaining[window.name] = 0.0
            dt = end - cursor
            if dt <= EPSILON:
                break
        _record_multiplier_attribution(
            sources, matches, track, high_roll=high_roll, duration=dt,
        )
        depleted: list[str] = []
        for window in windows:
            rate = absorbed_rates[window.name]
            if rate <= 0:
                continue
            absorbed = min(track.remaining[window.name], rate * dt)
            track.remaining[window.name] -= absorbed
            track.guard_absorbed[window.name] += absorbed
            if track.remaining[window.name] <= EPSILON:
                track.remaining[window.name] = 0.0
                if track.depletion_times[window.name] is None:
                    track.depletion_times[window.name] = cursor + dt
                    depleted.append(window.name)
        for source in sources:
            track.fragments.append((source.name, cursor, cursor + dt, residual[source.name]))
        before_total = sum(before.values()) * dt
        multiplied_total = sum(multiplied.values()) * dt
        residual_total = sum(residual.values()) * dt
        track.dot_segments.append(MitigatedDotSegment(
            start_seconds=round(cursor, 6), end_seconds=round(cursor + dt, 6),
            active_sources=tuple(source.name for source in sources),
            active_windows=tuple(dict.fromkeys(
                window.name for source in sources for window in matches[source.name]
            )),
            damage_before_mitigation=round(before_total, 6),
            damage_after_multipliers=round(multiplied_total, 6),
            damage_after_guards=round(residual_total, 6),
            multiplier_prevented=round(before_total - multiplied_total, 6),
            guard_absorbed=round(multiplied_total - residual_total, 6),
            depleted_windows=tuple(depleted),
        ))
        cursor += dt


def _process_hit(
    track: _Track, *, event: LayeredDamageEvent, index: int,
    windows: tuple[MitigationWindow, ...], high_roll: bool,
) -> float:
    before = event.damage.taken_high if high_roll else event.damage.taken_low
    matching = _matching(
        windows, at=event.damage.at_seconds, scope="hit",
        damage_type=event.damage.damage_type,
    )
    multiplied = _multiply(before, matching, track)
    residual, guard_rows = _absorb_hit(
        multiplied, matching, track, at=event.damage.at_seconds,
    )
    track.hit_steps.append(MitigatedHit(
        hit_index=index, at_seconds=event.damage.at_seconds,
        skill=event.damage.skill, damage_type=event.damage.damage_type,
        damage_before_mitigation=round(before, 6),
        damage_after_multipliers=round(multiplied, 6),
        damage_after_guards=round(residual, 6),
        multiplier_prevented=round(before - multiplied, 6),
        guard_absorbed=round(multiplied - residual, 6),
        matching_windows=tuple(window.name for window in matching),
        guard_absorptions=guard_rows,
    ))
    return residual


def _prepare_track(
    hits: tuple[LayeredDamageEvent, ...], dots: tuple[DotWindow, ...],
    windows: tuple[MitigationWindow, ...], *, high_roll: bool, duration: float,
) -> tuple[_Track, tuple[float, ...]]:
    track = _new_track(windows)
    adjusted_hits: list[float] = []
    breakpoints = {0.0, duration}
    breakpoints.update(event.damage.at_seconds for event in hits)
    for source in dots:
        breakpoints.update((source.start_seconds, source.end_seconds))
    for window in windows:
        breakpoints.update((window.start_seconds, window.end_seconds))
    ordered = sorted(breakpoints)
    hit_index = 0
    for position, at in enumerate(ordered):
        if position > 0:
            left = ordered[position - 1]
            sources = _active_sources(dots, left)
            if sources:
                _process_dot_interval(
                    track, start=left, end=at, sources=sources,
                    windows=windows, high_roll=high_roll,
                )
        for event in hits:
            if event.damage.at_seconds == at:
                hit_index += 1
                adjusted_hits.append(_process_hit(
                    track, event=event, index=hit_index, windows=windows,
                    high_roll=high_roll,
                ))
    return track, tuple(adjusted_hits)


def _fragment_rate(
    fragments: list[tuple[str, float, float, float]], *, source: str,
    left: float, right: float,
) -> float:
    middle = (left + right) / 2
    for name, start, end, rate in fragments:
        if name == source and start <= middle < end:
            return rate
    return 0.0


def _merge_dots(
    dots: tuple[DotWindow, ...], low: _Track, high: _Track,
) -> tuple[DotWindow, ...]:
    merged: list[DotWindow] = []
    for source in dots:
        points = {source.start_seconds, source.end_seconds}
        for name, start, end, _ in (*low.fragments, *high.fragments):
            if name == source.name:
                points.update((start, end))
        ordered = sorted(points)
        for index, (left, right) in enumerate(zip(ordered, ordered[1:]), start=1):
            low_rate = _fragment_rate(
                low.fragments, source=source.name, left=left, right=right,
            )
            high_rate = _fragment_rate(
                high.fragments, source=source.name, left=left, right=right,
            )
            if high_rate + EPSILON < low_rate:
                raise BossMitigationError(
                    f"窗口组合使 {source.name} 的高伤残余低于低伤残余，无法形成有序伤害区间"
                )
            merged.append(DotWindow(
                name=f"{source.name}#{index}", start_seconds=left,
                duration_seconds=right - left, damage_type=source.damage_type,
                taken_dps_low=max(0.0, low_rate),
                taken_dps_high=max(0.0, high_rate),
                energy_shield_bypass_pct=source.energy_shield_bypass_pct,
            ))
    return tuple(merged)


def _merge_hits(
    hits: tuple[LayeredDamageEvent, ...], low_values: tuple[float, ...],
    high_values: tuple[float, ...],
) -> tuple[LayeredDamageEvent, ...]:
    merged: list[LayeredDamageEvent] = []
    for event, low, high in zip(hits, low_values, high_values, strict=True):
        if high + EPSILON < low:
            raise BossMitigationError(
                f"窗口组合使 {event.damage.skill} 的高伤残余低于低伤残余"
            )
        merged.append(replace(event, damage=replace(
            event.damage, taken_low=max(0.0, low), taken_high=max(0.0, high),
        )))
    return tuple(merged)


def _window_summaries(
    track: _Track, windows: tuple[MitigationWindow, ...],
) -> tuple[MitigationWindowSummary, ...]:
    return tuple(MitigationWindowSummary(
        name=window.name, priority=window.priority,
        initial_guard_capacity=window.guard_capacity,
        multiplier_prevented=round(track.multiplier_prevented[window.name], 6),
        guard_absorbed=round(track.guard_absorbed[window.name], 6),
        unused_guard_capacity=round(track.remaining[window.name], 6),
        guard_depletion_time_seconds=(
            round(track.depletion_times[window.name], 6)
            if track.depletion_times[window.name] is not None else None
        ),
    ) for window in windows)


def _roll_result(
    track: _Track, windows: tuple[MitigationWindow, ...],
    timeline: boss_dot.DotRollResult, *, high_roll: bool,
) -> MitigationRollResult:
    before = sum(step.damage_before_mitigation for step in track.hit_steps) + sum(
        segment.damage_before_mitigation for segment in track.dot_segments
    )
    after = sum(step.damage_after_guards for step in track.hit_steps) + sum(
        segment.damage_after_guards for segment in track.dot_segments
    )
    multiplier = sum(track.multiplier_prevented.values())
    guard = sum(track.guard_absorbed.values())
    prevented = multiplier + guard
    return MitigationRollResult(
        damage_basis="high_roll" if high_roll else "low_roll",
        damage_before_mitigation=round(before, 6),
        damage_after_mitigation=round(after, 6),
        multiplier_prevented=round(multiplier, 6),
        guard_absorbed=round(guard, 6), total_prevented=round(prevented, 6),
        prevented_pct=round(prevented / before * 100, 6) if before > 0 else 0.0,
        hit_steps=tuple(track.hit_steps), dot_segments=tuple(track.dot_segments),
        windows=_window_summaries(track, windows), timeline=timeline,
    )


def assess_mitigation_timeline(
    *, hit_events: Sequence[LayeredDamageEvent], dot_windows: Sequence[DotWindow],
    recovery_events: Sequence[ActiveRecoveryEvent], config: DotTimelineConfig,
    mitigation_windows: Sequence[MitigationWindow],
) -> MitigationTimelineResult:
    """先解析临时防御，再复用 DoT 分层时间轴推进最终承伤。"""
    hits = tuple(hit_events)
    dots = tuple(dot_windows)
    if not dots:
        raise BossMitigationError("dot_windows 至少需要一个窗口；纯命中临时防御不走本工具")
    duration = _duration(hits, dots)
    windows = _validate_windows(mitigation_windows, duration=duration)
    low_track, low_hits = _prepare_track(
        hits, dots, windows, high_roll=False, duration=duration,
    )
    high_track, high_hits = _prepare_track(
        hits, dots, windows, high_roll=True, duration=duration,
    )
    adjusted_hits = _merge_hits(hits, low_hits, high_hits)
    adjusted_dots = _merge_dots(dots, low_track, high_track)
    assessed = boss_dot.assess_dot_timeline(
        hit_events=adjusted_hits, dot_windows=adjusted_dots,
        recovery_events=recovery_events, config=config,
    )
    baseline = boss_dot.assess_dot_timeline(
        hit_events=hits, dot_windows=dots, recovery_events=recovery_events,
        config=config,
    )
    low = _roll_result(low_track, windows, assessed.low_roll, high_roll=False)
    high = _roll_result(high_track, windows, assessed.high_roll, high_roll=True)
    return MitigationTimelineResult(
        config=assessed.config, mitigation_windows=windows, dot_windows=dots,
        recovery_events=assessed.recovery_events, hit_count=len(hits),
        dot_window_count=len(dots), mitigation_window_count=len(windows),
        duration_seconds=duration, outcome=assessed.outcome,
        low_roll=low, high_roll=high,
        mitigation_impact=MitigationImpact(
            high_roll_survived_without_mitigation=baseline.high_roll.survived,
            high_roll_ending_life_without_mitigation=baseline.high_roll.ending_life,
            high_roll_ending_life_gain=round(
                assessed.high_roll.ending_life - baseline.high_roll.ending_life, 6,
            ),
            changed_high_roll_survival=(
                assessed.high_roll.survived != baseline.high_roll.survived
            ),
            high_roll_damage_prevented=high.total_prevented,
        ),
        minimum_life_for_full_range=assessed.minimum_life_for_full_range,
        minimum_energy_shield_for_full_range=assessed.minimum_energy_shield_for_full_range,
        minimum_mana_for_full_range=assessed.minimum_mana_for_full_range,
        ordering_model=(
            "同一伤害先按 priority→开始时间→名称应用匹配乘数，再由同序 Guard 容量吸收，"
            "残余才进入护盾→魔力→生命时间轴"
        ),
        window_boundary_model=(
            "窗口采用 [start, end)；DoT 在 Guard 精确耗尽时切段，命中恰好位于 end 时不受保护"
        ),
        attribution_model=(
            "重叠乘数的减伤按应用顺序归属；Guard 按优先级独占吸收残余，后序窗口只接收前序未吸收量"
        ),
    )


def compare_mitigation_timelines(
    scenarios: Sequence[tuple[str, MitigationTimelineResult]],
) -> MitigationTimelineComparison:
    if not isinstance(scenarios, (list, tuple)) or not 2 <= len(scenarios) <= 10:
        raise BossMitigationError("scenarios 必须包含 2..10 个方案")
    names = [str(name).strip() for name, _ in scenarios]
    if any(not name or len(name) > 80 for name in names) or len(set(names)) != len(names):
        raise BossMitigationError("方案名称必须为不重复的 1..80 个字符")
    rows = tuple(MitigationScenarioResult(
        name=name, outcome=result.outcome,
        ending_life_high_roll=result.high_roll.timeline.ending_life,
        total_prevented_high_roll=result.high_roll.total_prevented,
        depletion_time_high_roll=result.high_roll.timeline.depletion_time_seconds,
        minimum_life_for_full_range=result.minimum_life_for_full_range.value,
        result=result,
    ) for name, result in scenarios)
    rank = {"fails_full_range": 0, "roll_dependent": 1, "survives_full_range": 2}
    reachable = [row for row in rows if row.minimum_life_for_full_range is not None]
    return MitigationTimelineComparison(
        scenarios=rows,
        best_survival_scenario=max(
            rows, key=lambda row: (rank[row.outcome], row.ending_life_high_roll)
        ).name,
        best_high_roll_life_margin_scenario=max(
            rows, key=lambda row: row.ending_life_high_roll
        ).name,
        highest_prevented_damage_scenario=max(
            rows, key=lambda row: row.total_prevented_high_roll
        ).name,
        longest_time_to_depletion_scenario=max(
            rows, key=lambda row: (
                row.depletion_time_high_roll is None,
                row.depletion_time_high_roll or 0.0,
            ),
        ).name,
        lowest_required_life_scenario=(
            min(reachable, key=lambda row: row.minimum_life_for_full_range or 0).name
            if reachable else None
        ),
    )
