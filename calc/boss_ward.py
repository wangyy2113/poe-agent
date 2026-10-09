"""Boss 显式 Ward 破裂、恢复与命中吸收状态机。

Ward 位于临时承伤乘数/Guard 之后、护盾/魔力/生命之前，只吸收命中残余，不处理 DoT。
容量、初始状态、恢复时间和破裂规则全部由调用方冻结；本模块不从装备或 PoB 静态面板
推断当前 Ward 是否就绪，也不把上一层一次性 Guard 当成 Ward。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal, Sequence

from calc import boss_dot, boss_mitigation
from calc.boss_dot import DotTimelineConfig, DotWindow
from calc.boss_layered import LayeredDamageEvent
from calc.boss_mitigation import MitigationWindow
from calc.boss_recovery import ActiveRecoveryEvent


WardInitialState = Literal["ready", "broken"]
WardBreakMode = Literal["break", "unbreakable"]
BrokenHitTimerMode = Literal["preserve", "reset"]
Outcome = Literal["survives_full_range", "roll_dependent", "fails_full_range"]
EPSILON = 1e-9
MAX_TIME_SECONDS = 3600.0
MAX_VALUE = 1_000_000_000.0


class BossWardError(ValueError):
    """Ward 配置不完整、相互矛盾或超出有界范围。"""


@dataclass(frozen=True)
class WardConfig:
    capacity: float
    restore_delay_seconds: float
    initial_state: WardInitialState = "ready"
    initial_restore_at_seconds: float | None = None
    break_mode: WardBreakMode = "break"
    broken_hit_timer_mode: BrokenHitTimerMode = "preserve"


@dataclass(frozen=True)
class WardHitStep:
    hit_index: int
    at_seconds: float
    skill: str
    damage_type: str
    damage_before_ward: float
    ward_ready_before: bool
    restored_before_hit: bool
    restore_at_before: float | None
    ward_absorbed: float
    damage_after_ward: float
    ward_broke: bool
    broken_timer_reset: bool
    ward_ready_after: bool
    restore_at_after: float | None


@dataclass(frozen=True)
class WardStateTransition:
    at_seconds: float
    hit_index: int | None
    kind: Literal["restore", "break", "timer_reset"]
    state_before: WardInitialState
    state_after: WardInitialState
    restore_at_after: float | None
    reason: str


@dataclass(frozen=True)
class WardStateSummary:
    initial_state: WardInitialState
    ending_state: WardInitialState
    capacity: float
    restore_delay_seconds: float
    break_mode: WardBreakMode
    broken_hit_timer_mode: BrokenHitTimerMode
    protected_hit_count: int
    zero_residual_hit_count: int
    break_count: int
    restoration_count: int
    broken_timer_reset_count: int
    total_absorbed: float
    ready_uptime_seconds: float
    broken_downtime_seconds: float
    ready_uptime_pct: float
    next_restore_at_seconds: float | None


@dataclass(frozen=True)
class WardRollResult:
    damage_basis: Literal["low_roll", "high_roll"]
    damage_before_mitigation: float
    damage_after_temporary_mitigation: float
    temporary_mitigation_prevented: float
    ward_absorbed: float
    damage_after_ward: float
    total_prevented: float
    prevented_pct: float
    hit_steps: tuple[WardHitStep, ...]
    state_transitions: tuple[WardStateTransition, ...]
    ward_state: WardStateSummary
    temporary_mitigation: boss_mitigation.MitigationRollResult
    timeline: boss_dot.DotRollResult


@dataclass(frozen=True)
class WardImpact:
    high_roll_survived_without_ward: bool
    high_roll_ending_life_without_ward: float
    high_roll_ending_life_gain: float
    changed_high_roll_survival: bool
    high_roll_ward_absorbed: float


@dataclass(frozen=True)
class WardTimelineResult:
    config: DotTimelineConfig
    ward: WardConfig
    mitigation_windows: tuple[MitigationWindow, ...]
    dot_windows: tuple[DotWindow, ...]
    recovery_events: tuple[ActiveRecoveryEvent, ...]
    hit_count: int
    dot_window_count: int
    mitigation_window_count: int
    duration_seconds: float
    outcome: Outcome
    low_roll: WardRollResult
    high_roll: WardRollResult
    ward_impact: WardImpact
    minimum_life_for_full_range: boss_dot.DotLayerThreshold
    minimum_energy_shield_for_full_range: boss_dot.DotLayerThreshold
    minimum_mana_for_full_range: boss_dot.DotLayerThreshold
    ordering_model: str
    simultaneous_hit_model: str
    restoration_model: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WardScenarioResult:
    name: str
    outcome: Outcome
    ending_life_high_roll: float
    ward_absorbed_high_roll: float
    protected_hits_high_roll: int
    depletion_time_high_roll: float | None
    minimum_life_for_full_range: int | None
    result: WardTimelineResult


@dataclass(frozen=True)
class WardTimelineComparison:
    scenarios: tuple[WardScenarioResult, ...]
    best_survival_scenario: str
    best_high_roll_life_margin_scenario: str
    highest_ward_absorption_scenario: str
    highest_ward_uptime_scenario: str
    most_protected_hits_scenario: str
    longest_time_to_depletion_scenario: str
    lowest_required_life_scenario: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _WardTrack:
    ready: bool
    restore_at: float | None
    protected_hits: int
    zero_residual_hits: int
    breaks: int
    restorations: int
    resets: int
    absorbed: float
    steps: list[WardHitStep]
    transitions: list[WardStateTransition]


def _number(
    value: object, label: str, *, minimum: float = 0.0,
    maximum: float = MAX_VALUE,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BossWardError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise BossWardError(f"{label} 必须在 {minimum:g}..{maximum:g} 之间")
    return result


def validate_ward_config(config: WardConfig, *, duration: float) -> WardConfig:
    if not isinstance(config, WardConfig):
        raise BossWardError("ward 类型无效")
    capacity = _number(config.capacity, "ward.capacity", minimum=EPSILON)
    delay = _number(
        config.restore_delay_seconds, "ward.restore_delay_seconds",
        maximum=MAX_TIME_SECONDS,
    )
    if config.initial_state not in {"ready", "broken"}:
        raise BossWardError("ward.initial_state 无效")
    if config.break_mode not in {"break", "unbreakable"}:
        raise BossWardError("ward.break_mode 无效")
    if config.broken_hit_timer_mode not in {"preserve", "reset"}:
        raise BossWardError("ward.broken_hit_timer_mode 无效")
    initial_restore = config.initial_restore_at_seconds
    if config.initial_state == "broken":
        if initial_restore is None:
            raise BossWardError("初始 broken 时必须提供 initial_restore_at_seconds")
        initial_restore = _number(
            initial_restore, "ward.initial_restore_at_seconds",
            maximum=MAX_TIME_SECONDS,
        )
    elif initial_restore is not None:
        raise BossWardError("初始 ready 时不能提供 initial_restore_at_seconds")
    if config.break_mode == "unbreakable":
        if config.initial_state != "ready":
            raise BossWardError("unbreakable Ward 必须从 ready 状态开始")
        if config.broken_hit_timer_mode != "preserve":
            raise BossWardError("unbreakable Ward 不接受 broken timer reset")
    if initial_restore is not None and initial_restore > duration + EPSILON:
        # 允许超出计划范围没有计算价值，且经常表示把相对延迟误填成绝对时间。
        raise BossWardError("ward.initial_restore_at_seconds 不能超出伤害时间轴")
    return WardConfig(
        capacity=capacity, restore_delay_seconds=delay,
        initial_state=config.initial_state,
        initial_restore_at_seconds=initial_restore,
        break_mode=config.break_mode,
        broken_hit_timer_mode=config.broken_hit_timer_mode,
    )


def _new_track(config: WardConfig) -> _WardTrack:
    return _WardTrack(
        ready=config.initial_state == "ready",
        restore_at=config.initial_restore_at_seconds,
        protected_hits=0, zero_residual_hits=0, breaks=0,
        restorations=0, resets=0, absorbed=0.0, steps=[],
        transitions=[],
    )


def _restore_if_due(
    track: _WardTrack, *, at: float, hit_index: int | None,
) -> bool:
    if not track.ready and track.restore_at is not None and track.restore_at <= at + EPSILON:
        restored_at = track.restore_at
        track.ready = True
        track.restore_at = None
        track.restorations += 1
        track.transitions.append(WardStateTransition(
            at_seconds=round(restored_at, 6), hit_index=hit_index,
            kind="restore", state_before="broken", state_after="ready",
            restore_at_after=None,
            reason=(
                "命中结算前到达恢复时点" if hit_index is not None
                else "计划结束前到达恢复时点"
            ),
        ))
        return True
    return False


def _apply_ward(
    events: tuple[LayeredDamageEvent, ...], config: WardConfig, *,
    high_roll: bool, duration: float,
) -> tuple[_WardTrack, tuple[float, ...]]:
    track = _new_track(config)
    residuals: list[float] = []
    for index, event in enumerate(events, start=1):
        at = event.damage.at_seconds
        restored = _restore_if_due(track, at=at, hit_index=index)
        before = event.damage.taken_high if high_roll else event.damage.taken_low
        ready_before = track.ready
        restore_before = track.restore_at
        absorbed = 0.0
        broke = False
        reset = False
        if before <= EPSILON:
            track.zero_residual_hits += 1
            residual = 0.0
        elif track.ready:
            absorbed = min(config.capacity, before)
            residual = before - absorbed
            track.absorbed += absorbed
            track.protected_hits += 1
            if config.break_mode == "break":
                track.ready = False
                track.restore_at = at + config.restore_delay_seconds
                track.breaks += 1
                broke = True
                track.transitions.append(WardStateTransition(
                    at_seconds=round(at, 6), hit_index=index, kind="break",
                    state_before="ready", state_after="broken",
                    restore_at_after=round(track.restore_at, 6),
                    reason="正命中残余触发 Ward 吸收并破裂",
                ))
        else:
            residual = before
            if config.broken_hit_timer_mode == "reset":
                track.restore_at = at + config.restore_delay_seconds
                track.resets += 1
                reset = True
                track.transitions.append(WardStateTransition(
                    at_seconds=round(at, 6), hit_index=index,
                    kind="timer_reset", state_before="broken", state_after="broken",
                    restore_at_after=round(track.restore_at, 6),
                    reason="broken 期间正命中残余按 reset 口径重置恢复时点",
                ))
        track.steps.append(WardHitStep(
            hit_index=index, at_seconds=at, skill=event.damage.skill,
            damage_type=event.damage.damage_type,
            damage_before_ward=round(before, 6),
            ward_ready_before=ready_before, restored_before_hit=restored,
            restore_at_before=(
                round(restore_before, 6) if restore_before is not None else None
            ),
            ward_absorbed=round(absorbed, 6),
            damage_after_ward=round(residual, 6), ward_broke=broke,
            broken_timer_reset=reset, ward_ready_after=track.ready,
            restore_at_after=(
                round(track.restore_at, 6) if track.restore_at is not None else None
            ),
        ))
        residuals.append(max(0.0, residual))
    _restore_if_due(track, at=duration, hit_index=None)
    return track, tuple(residuals)


def _state_durations(
    track: _WardTrack, config: WardConfig, *, duration: float,
) -> tuple[float, float]:
    ready = config.initial_state == "ready"
    cursor = 0.0
    ready_time = 0.0
    broken_time = 0.0
    for transition in track.transitions:
        at = min(duration, max(cursor, transition.at_seconds))
        if ready:
            ready_time += at - cursor
        else:
            broken_time += at - cursor
        ready = transition.state_after == "ready"
        cursor = at
    if ready:
        ready_time += max(0.0, duration - cursor)
    else:
        broken_time += max(0.0, duration - cursor)
    return ready_time, broken_time


def _summary(
    track: _WardTrack, config: WardConfig, *, duration: float,
) -> WardStateSummary:
    ready_time, broken_time = _state_durations(track, config, duration=duration)
    return WardStateSummary(
        initial_state=config.initial_state,
        ending_state="ready" if track.ready else "broken",
        capacity=config.capacity,
        restore_delay_seconds=config.restore_delay_seconds,
        break_mode=config.break_mode,
        broken_hit_timer_mode=config.broken_hit_timer_mode,
        protected_hit_count=track.protected_hits,
        zero_residual_hit_count=track.zero_residual_hits,
        break_count=track.breaks, restoration_count=track.restorations,
        broken_timer_reset_count=track.resets,
        total_absorbed=round(track.absorbed, 6),
        ready_uptime_seconds=round(ready_time, 6),
        broken_downtime_seconds=round(broken_time, 6),
        ready_uptime_pct=(
            round(ready_time / duration * 100, 6) if duration > 0 else 100.0
        ),
        next_restore_at_seconds=(
            round(track.restore_at, 6) if track.restore_at is not None else None
        ),
    )


def _ward_roll(
    track: _WardTrack, config: WardConfig,
    mitigation: boss_mitigation.MitigationRollResult,
    timeline: boss_dot.DotRollResult, *, high_roll: bool, duration: float,
) -> WardRollResult:
    before = mitigation.damage_before_mitigation
    after_mitigation = mitigation.damage_after_mitigation
    total_prevented = mitigation.total_prevented + track.absorbed
    after_ward = max(0.0, after_mitigation - track.absorbed)
    return WardRollResult(
        damage_basis="high_roll" if high_roll else "low_roll",
        damage_before_mitigation=before,
        damage_after_temporary_mitigation=after_mitigation,
        temporary_mitigation_prevented=mitigation.total_prevented,
        ward_absorbed=round(track.absorbed, 6),
        damage_after_ward=round(after_ward, 6),
        total_prevented=round(total_prevented, 6),
        prevented_pct=(
            round(total_prevented / before * 100, 6) if before > 0 else 0.0
        ),
        hit_steps=tuple(track.steps), state_transitions=tuple(track.transitions),
        ward_state=_summary(track, config, duration=duration),
        temporary_mitigation=mitigation, timeline=timeline,
    )


def assess_ward_timeline(
    *, hit_events: Sequence[LayeredDamageEvent], dot_windows: Sequence[DotWindow],
    recovery_events: Sequence[ActiveRecoveryEvent], config: DotTimelineConfig,
    ward: WardConfig, mitigation_windows: Sequence[MitigationWindow] = (),
) -> WardTimelineResult:
    """依次结算临时减伤/Guard、Ward，再推进分层混合时间轴。"""
    hits = tuple(hit_events)
    dots = tuple(dot_windows)
    if not dots:
        raise BossWardError("dot_windows 至少需要一个窗口；纯命中 Ward 不走本工具")
    if not hits:
        raise BossWardError("Ward 只处理命中，events 至少需要一次显式命中")
    duration = boss_mitigation._duration(hits, dots)
    windows = (
        boss_mitigation._validate_windows(mitigation_windows, duration=duration)
        if mitigation_windows else tuple()
    )
    ward_config = validate_ward_config(ward, duration=duration)
    low_mitigation_track, low_hits = boss_mitigation._prepare_track(
        hits, dots, windows, high_roll=False, duration=duration,
    )
    high_mitigation_track, high_hits = boss_mitigation._prepare_track(
        hits, dots, windows, high_roll=True, duration=duration,
    )
    mitigated_hits = boss_mitigation._merge_hits(hits, low_hits, high_hits)
    mitigated_dots = boss_mitigation._merge_dots(
        dots, low_mitigation_track, high_mitigation_track,
    )
    without_ward = boss_dot.assess_dot_timeline(
        hit_events=mitigated_hits, dot_windows=mitigated_dots,
        recovery_events=recovery_events, config=config,
    )
    low_track, low_ward_hits = _apply_ward(
        mitigated_hits, ward_config, high_roll=False, duration=duration,
    )
    high_track, high_ward_hits = _apply_ward(
        mitigated_hits, ward_config, high_roll=True, duration=duration,
    )
    ward_hits = boss_mitigation._merge_hits(
        mitigated_hits, low_ward_hits, high_ward_hits,
    )
    assessed = boss_dot.assess_dot_timeline(
        hit_events=ward_hits, dot_windows=mitigated_dots,
        recovery_events=recovery_events, config=config,
    )
    low_mitigation = boss_mitigation._roll_result(
        low_mitigation_track, windows, without_ward.low_roll, high_roll=False,
    )
    high_mitigation = boss_mitigation._roll_result(
        high_mitigation_track, windows, without_ward.high_roll, high_roll=True,
    )
    low = _ward_roll(
        low_track, ward_config, low_mitigation, assessed.low_roll,
        high_roll=False, duration=duration,
    )
    high = _ward_roll(
        high_track, ward_config, high_mitigation, assessed.high_roll,
        high_roll=True, duration=duration,
    )
    return WardTimelineResult(
        config=assessed.config, ward=ward_config, mitigation_windows=windows,
        dot_windows=dots, recovery_events=assessed.recovery_events,
        hit_count=len(hits), dot_window_count=len(dots),
        mitigation_window_count=len(windows), duration_seconds=duration,
        outcome=assessed.outcome, low_roll=low, high_roll=high,
        ward_impact=WardImpact(
            high_roll_survived_without_ward=without_ward.high_roll.survived,
            high_roll_ending_life_without_ward=without_ward.high_roll.ending_life,
            high_roll_ending_life_gain=round(
                assessed.high_roll.ending_life - without_ward.high_roll.ending_life, 6,
            ),
            changed_high_roll_survival=(
                assessed.high_roll.survived != without_ward.high_roll.survived
            ),
            high_roll_ward_absorbed=high.ward_absorbed,
        ),
        minimum_life_for_full_range=assessed.minimum_life_for_full_range,
        minimum_energy_shield_for_full_range=assessed.minimum_energy_shield_for_full_range,
        minimum_mana_for_full_range=assessed.minimum_mana_for_full_range,
        ordering_model=(
            "临时承伤乘数→Guard→Ward（仅命中）→护盾→魔力→生命；DoT 跳过 Ward"
        ),
        simultaneous_hit_model=(
            "同刻命中按请求顺序处理；break 模式下首个正残余命中破裂 Ward，后续同刻命中"
            "只有在 restore_delay_seconds=0 时才能再次使用"
        ),
        restoration_model=(
            "broken 状态按显式绝对恢复时点或 hit_time+restore_delay 恢复；broken 期间命中"
            f"采用 {ward_config.broken_hit_timer_mode} 计时口径"
        ),
    )


def compare_ward_timelines(
    scenarios: Sequence[tuple[str, WardTimelineResult]],
) -> WardTimelineComparison:
    if not isinstance(scenarios, (list, tuple)) or not 2 <= len(scenarios) <= 10:
        raise BossWardError("scenarios 必须包含 2..10 个方案")
    names = [str(name).strip() for name, _ in scenarios]
    if any(not name or len(name) > 80 for name in names) or len(set(names)) != len(names):
        raise BossWardError("方案名称必须为不重复的 1..80 个字符")
    rows = tuple(WardScenarioResult(
        name=name, outcome=result.outcome,
        ending_life_high_roll=result.high_roll.timeline.ending_life,
        ward_absorbed_high_roll=result.high_roll.ward_absorbed,
        protected_hits_high_roll=result.high_roll.ward_state.protected_hit_count,
        depletion_time_high_roll=result.high_roll.timeline.depletion_time_seconds,
        minimum_life_for_full_range=result.minimum_life_for_full_range.value,
        result=result,
    ) for name, result in scenarios)
    rank = {"fails_full_range": 0, "roll_dependent": 1, "survives_full_range": 2}
    reachable = [row for row in rows if row.minimum_life_for_full_range is not None]
    return WardTimelineComparison(
        scenarios=rows,
        best_survival_scenario=max(
            rows, key=lambda row: (rank[row.outcome], row.ending_life_high_roll)
        ).name,
        best_high_roll_life_margin_scenario=max(
            rows, key=lambda row: row.ending_life_high_roll
        ).name,
        highest_ward_absorption_scenario=max(
            rows, key=lambda row: row.ward_absorbed_high_roll
        ).name,
        highest_ward_uptime_scenario=max(
            rows, key=lambda row: row.result.high_roll.ward_state.ready_uptime_pct
        ).name,
        most_protected_hits_scenario=max(
            rows, key=lambda row: row.protected_hits_high_roll
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
