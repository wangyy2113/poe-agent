"""单技能 DPS 的多方案比较与目标频率反解。

这里不重新实现伤害公式，只编排 :func:`calc.dps.hit_dps`，保证单方案、比较和反解
使用同一条计算链。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from calc.dps import Conversion, HitDpsResult, effective_cooldown, hit_dps


TARGET_METRICS = {
    "hit_dps": "dps",
    "combined_dps": "combined_dps",
    "effective_dps_with_cull": "effective_dps_with_cull",
    "ailment_dps": "ailment_dps",
    "skill_dot_dps": "skill_dot_dps",
    "total_dot_dps": "total_dot_dps",
    "total_dps": "total_dps",
    "effective_total_dps_with_cull": "effective_total_dps_with_cull",
    "combat_average_combined_dps": "combat_average_combined_dps",
    "combat_average_ailment_dps": "combat_average_ailment_dps",
    "combat_average_skill_dot_dps": "combat_average_skill_dot_dps",
    "combat_average_total_dot_dps": "combat_average_total_dot_dps",
    "combat_average_total_dps": "combat_average_total_dps",
    "combat_average_effective_total_dps_with_cull": (
        "combat_average_effective_total_dps_with_cull"
    ),
}


@dataclass(frozen=True)
class ScenarioDelta:
    name: str
    dps: float
    dps_delta: float
    dps_delta_pct: float | None
    combined_dps: float
    effective_dps_with_cull: float
    ailment_dps: float
    skill_dot_dps: float
    total_dot_dps: float
    total_dps: float
    effective_total_dps_with_cull: float
    combat_average_combined_dps: float | None
    combat_average_ailment_dps: float | None
    combat_average_skill_dot_dps: float | None
    combat_average_total_dot_dps: float | None
    combat_average_total_dps: float | None
    combat_average_effective_total_dps_with_cull: float | None
    average_hit: float
    average_hit_delta: float
    burst_damage: float
    burst_damage_delta: float
    result: HitDpsResult

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DpsComparison:
    baseline: str
    scenarios: tuple[ScenarioDelta, ...]
    best_dps_scenario: str
    best_combined_dps_scenario: str
    best_effective_dps_scenario: str
    best_ailment_dps_scenario: str
    best_skill_dot_dps_scenario: str
    best_total_dot_dps_scenario: str
    best_total_dps_scenario: str
    best_effective_total_dps_scenario: str
    best_combat_average_scenario: str | None
    best_combat_average_dot_scenario: str | None
    best_combat_average_total_scenario: str | None
    best_combat_average_effective_total_scenario: str | None
    best_burst_scenario: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TargetRateResult:
    reachable: bool
    metric: str
    target_dps: float
    dps_per_effective_action: float
    required_effective_action_rate: float | None
    required_raw_action_rate: float | None
    cooldown_cap: float | None
    action_rate_cap: float | None
    cap_sources: tuple[str, ...]
    maximum_achievable_dps: float | None
    achieved_dps: float | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _kwargs(raw: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(raw)
    values["conversions"] = [
        item if isinstance(item, Conversion) else Conversion(**item)
        for item in values.get("conversions", ())
    ]
    return values


def compare_hit_dps(
    scenarios: Sequence[tuple[str, Mapping[str, Any]]],
) -> DpsComparison:
    """在同一公式口径下比较 2–10 个显式场景，首项为基线。"""
    if not 2 <= len(scenarios) <= 10:
        raise ValueError("scenarios 必须包含 2..10 个方案")
    names = [str(name).strip() for name, _ in scenarios]
    if any(not name or len(name) > 80 for name in names):
        raise ValueError("方案名称必须为 1..80 个字符")
    if len(set(names)) != len(names):
        raise ValueError("方案名称不能重复")

    calculated = [(name, hit_dps(**_kwargs(raw))) for name, raw in scenarios]
    baseline_name, baseline = calculated[0]
    rows: list[ScenarioDelta] = []
    for name, result in calculated:
        delta = result.dps - baseline.dps
        delta_pct = delta / baseline.dps * 100 if baseline.dps else None
        rows.append(ScenarioDelta(
            name=name,
            dps=result.dps,
            dps_delta=delta,
            dps_delta_pct=delta_pct,
            combined_dps=result.combined_dps,
            effective_dps_with_cull=result.effective_dps_with_cull,
            ailment_dps=result.ailment_dps,
            skill_dot_dps=result.skill_dot_dps,
            total_dot_dps=result.total_dot_dps,
            total_dps=result.total_dps,
            effective_total_dps_with_cull=result.effective_total_dps_with_cull,
            combat_average_combined_dps=result.combat_average_combined_dps,
            combat_average_ailment_dps=result.combat_average_ailment_dps,
            combat_average_skill_dot_dps=result.combat_average_skill_dot_dps,
            combat_average_total_dot_dps=result.combat_average_total_dot_dps,
            combat_average_total_dps=result.combat_average_total_dps,
            combat_average_effective_total_dps_with_cull=(
                result.combat_average_effective_total_dps_with_cull
            ),
            average_hit=result.average_hit,
            average_hit_delta=result.average_hit - baseline.average_hit,
            burst_damage=result.average_burst_damage,
            burst_damage_delta=result.average_burst_damage - baseline.average_burst_damage,
            result=result,
        ))
    combat_rows = [row for row in rows if row.combat_average_combined_dps is not None]
    combat_total_rows = [row for row in rows if row.combat_average_total_dps is not None]
    return DpsComparison(
        baseline=baseline_name,
        scenarios=tuple(rows),
        best_dps_scenario=max(rows, key=lambda row: row.dps).name,
        best_combined_dps_scenario=max(rows, key=lambda row: row.combined_dps).name,
        best_effective_dps_scenario=max(
            rows, key=lambda row: row.effective_dps_with_cull
        ).name,
        best_ailment_dps_scenario=max(rows, key=lambda row: row.ailment_dps).name,
        best_skill_dot_dps_scenario=max(rows, key=lambda row: row.skill_dot_dps).name,
        best_total_dot_dps_scenario=max(rows, key=lambda row: row.total_dot_dps).name,
        best_total_dps_scenario=max(rows, key=lambda row: row.total_dps).name,
        best_effective_total_dps_scenario=max(
            rows, key=lambda row: row.effective_total_dps_with_cull
        ).name,
        best_combat_average_scenario=(
            max(combat_rows, key=lambda row: row.combat_average_combined_dps or 0).name
            if combat_rows else None
        ),
        best_combat_average_dot_scenario=(
            max(combat_total_rows, key=lambda row: row.combat_average_total_dot_dps or 0).name
            if combat_total_rows else None
        ),
        best_combat_average_total_scenario=(
            max(combat_total_rows, key=lambda row: row.combat_average_total_dps or 0).name
            if combat_total_rows else None
        ),
        best_combat_average_effective_total_scenario=(
            max(
                combat_total_rows,
                key=lambda row: row.combat_average_effective_total_dps_with_cull or 0,
            ).name
            if combat_total_rows else None
        ),
        best_burst_scenario=max(rows, key=lambda row: row.burst_damage).name,
    )


def target_action_rate(
    target_dps: float,
    inputs: Mapping[str, Any],
    *,
    metric: str = "hit_dps",
) -> TargetRateResult:
    """数值反解目标频率，支持冷却平台及随频率变化的战斗机制。"""
    if isinstance(target_dps, bool) or not isinstance(target_dps, (int, float)):
        raise ValueError("target_dps 必须是数字")
    target = float(target_dps)
    if not math.isfinite(target) or target <= 0:
        raise ValueError("target_dps 必须是有限正数")
    if metric not in TARGET_METRICS:
        raise ValueError(
            "metric 只支持 hit_dps、combined_dps、effective_dps_with_cull、"
            "ailment_dps、skill_dot_dps、total_dot_dps、total_dps、"
            "effective_total_dps_with_cull、combat_average_combined_dps、"
            "combat_average_ailment_dps、combat_average_skill_dot_dps、"
            "combat_average_total_dot_dps、combat_average_total_dps、"
            "combat_average_effective_total_dps_with_cull"
        )

    values = _kwargs(inputs)
    values.pop("action_rate", None)
    if metric.startswith("combat_average_") and values.get("combat_duration_seconds") is None:
        raise ValueError(f"{metric} 反解必须提供 combat_duration_seconds")
    if metric in {
        "ailment_dps", "total_dot_dps", "total_dps",
        "effective_total_dps_with_cull", "combat_average_ailment_dps",
        "combat_average_total_dot_dps", "combat_average_total_dps",
        "combat_average_effective_total_dps_with_cull",
    }:
        if float(values.get("fist_of_war_more_damage_pct", 0.0)) > 0:
            raise ValueError(
                "含异常指标在战争之拳覆盖随频率变化时可能非单调，不能可靠反解目标频率"
            )
        for raw_ailment in values.get("damaging_ailments", ()):
            if not isinstance(raw_ailment, Mapping):
                continue
            source = raw_ailment.get("source_damage")
            crit_source = raw_ailment.get("crit_source_damage")
            normal_dot = raw_ailment.get("dot_multiplier_pct", 0.0)
            crit_dot = raw_ailment.get("crit_dot_multiplier_pct", normal_dot)
            if (
                isinstance(source, (list, tuple))
                and isinstance(crit_source, (list, tuple))
                and len(source) == 2 and len(crit_source) == 2
                and (float(crit_source[0]) < float(source[0])
                     or float(crit_source[1]) < float(source[1]))
            ) or (
                crit_dot is not None and normal_dot is not None
                and float(crit_dot) < float(normal_dot)
            ):
                raise ValueError(
                    "含异常指标的暴击源伤或暴击 DoT 加成低于非暴击时可能非单调，"
                    "不能可靠反解目标频率"
                )
    if metric in {"skill_dot_dps", "combat_average_skill_dot_dps"}:
        raw_skill_dot = values.get("skill_dot")
        if not isinstance(raw_skill_dot, Mapping):
            raise ValueError(f"{metric} 反解必须提供 skill_dot")
        if metric == "skill_dot_dps" and not raw_skill_dot.get("can_stack", False):
            raise ValueError("skill_dot_dps 目标频率只支持 can_stack=true 的技能 DoT")
    raw_cooldown = values.pop("cooldown_seconds", 0.0)
    if isinstance(raw_cooldown, bool) or not isinstance(raw_cooldown, (int, float)):
        raise ValueError("cooldown_seconds 必须是数字")
    cooldown = float(raw_cooldown)
    if not math.isfinite(cooldown) or cooldown < 0:
        raise ValueError("cooldown_seconds 必须是有限非负数")
    triggered = bool(values.pop("triggered", False))
    tick = values.get("server_tick_seconds")
    cooldown_uses = values.get("cooldown_uses", 1)
    effective_cd, _rounded = effective_cooldown(
        cooldown,
        server_tick_seconds=tick,
        cooldown_uses=cooldown_uses,
    )
    metric_field = TARGET_METRICS[metric]

    def calculate(raw_rate: float) -> tuple[HitDpsResult, float]:
        result = hit_dps(
            **values,
            action_rate=raw_rate,
            cooldown_seconds=cooldown,
            triggered=triggered,
        )
        metric_value = getattr(result, metric_field)
        if metric_value is None:
            raise ValueError(f"{metric} 当前没有可计算值")
        return result, float(metric_value)

    unit, unit_value = calculate(1.0)
    per_rate = unit_value / unit.action_rate if unit.action_rate > 0 else 0.0
    cooldown_cap = 1 / effective_cd if effective_cd > 0 else None
    action_repeats = values.get("action_repeats", 1)
    channelled = bool(values.get("channelled", False))
    tick_cap = (
        action_repeats / float(tick)
        if tick is not None and not channelled
        else None
    )
    cap_sources: list[str] = []
    if cooldown_cap is not None:
        cap_sources.append("cooldown")
    if tick_cap is not None:
        cap_sources.append("server_tick")
    if per_rate <= 0:
        return TargetRateResult(
            reachable=False, metric=metric, target_dps=target,
            dps_per_effective_action=per_rate,
            required_effective_action_rate=None, required_raw_action_rate=None,
            cooldown_cap=cooldown_cap, action_rate_cap=None,
            cap_sources=tuple(cap_sources), maximum_achievable_dps=0.0,
            achieved_dps=None,
            reason="当前参数每次使用的期望伤害为 0，无法通过提高频率达到目标",
        )

    lower = 1e-12
    if tick_cap is not None:
        upper = tick_cap
    elif effective_cd > 0 and triggered:
        upper = cooldown_cap or 1.0
    else:
        upper = 1.0
        while upper < 1e12:
            _probe, value = calculate(upper)
            if value >= target:
                break
            upper *= 2
        upper = min(upper, 1e12)

    maximum: float | None = None
    action_rate_cap: float | None = None
    if effective_cd > 0 or tick_cap is not None:
        if tick_cap is not None:
            max_rate = tick_cap
        elif triggered:
            max_rate = cooldown_cap or 1.0
        else:
            max_rate = 1e12
        maximum_result, maximum = calculate(max_rate)
        action_rate_cap = maximum_result.action_rate
        if (
            metric in {"ailment_dps", "combat_average_ailment_dps"}
            and any(item.capped for item in maximum_result.damaging_ailments)
        ) or (
            metric in {"skill_dot_dps", "combat_average_skill_dot_dps"}
            and maximum_result.skill_dot is not None
            and maximum_result.skill_dot.capped
        ) or (
            metric in {
                "total_dot_dps", "total_dps", "effective_total_dps_with_cull",
                "combat_average_total_dot_dps", "combat_average_total_dps",
                "combat_average_effective_total_dps_with_cull",
            }
            and maximum_result.total_dot_dps_capped
        ):
            cap_sources.append("dot_cap")
    _upper_result, upper_value = calculate(upper)
    if upper_value < target and not math.isclose(upper_value, target, rel_tol=1e-12):
        dot_capped = (
            metric in {"ailment_dps", "combat_average_ailment_dps"}
            and any(item.capped for item in _upper_result.damaging_ailments)
        ) or (
            metric in {"skill_dot_dps", "combat_average_skill_dot_dps"}
            and _upper_result.skill_dot is not None
            and _upper_result.skill_dot.capped
        ) or (
            metric in {
                "total_dot_dps", "total_dps", "effective_total_dps_with_cull",
                "combat_average_total_dot_dps", "combat_average_total_dps",
                "combat_average_effective_total_dps_with_cull",
            }
            and _upper_result.total_dot_dps_capped
        )
        if dot_capped:
            if "dot_cap" not in cap_sources:
                cap_sources.append("dot_cap")
            maximum = upper_value
            reason = "持续伤害的 DoT 上限使目标不可达"
        elif tick_cap is not None and effective_cd > 0:
            reason = "服务器 tick 与冷却共同限制了最高有效频率"
        elif tick_cap is not None:
            reason = "服务器 tick 限制了最高有效频率"
        elif triggered:
            reason = "触发冷却限制了最高有效频率"
        elif effective_cd > 0:
            reason = "非触发技能的动作时间与冷却相加，目标超过冷却渐近上限"
        else:
            reason = "在允许的原始频率范围内无法达到目标"
        return TargetRateResult(
            reachable=False, metric=metric, target_dps=target,
            dps_per_effective_action=per_rate,
            required_effective_action_rate=None, required_raw_action_rate=None,
            cooldown_cap=cooldown_cap, action_rate_cap=action_rate_cap,
            cap_sources=tuple(cap_sources), maximum_achievable_dps=maximum,
            achieved_dps=None, reason=reason,
        )

    for _ in range(120):
        middle = (lower + upper) / 2
        _middle_result, middle_value = calculate(middle)
        if middle_value >= target:
            upper = middle
        else:
            lower = middle
    required_raw = upper
    verification, achieved = calculate(required_raw)
    return TargetRateResult(
        reachable=True, metric=metric, target_dps=target,
        dps_per_effective_action=achieved / verification.action_rate,
        required_effective_action_rate=verification.action_rate,
        required_raw_action_rate=required_raw, cooldown_cap=cooldown_cap,
        action_rate_cap=action_rate_cap, cap_sources=tuple(cap_sources),
        maximum_achievable_dps=maximum, achieved_dps=achieved,
        reason="已用同一伤害链数值反解并回代验证",
    )
