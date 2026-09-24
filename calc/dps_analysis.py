"""命中 DPS 的多方案比较与目标频率反解。

这里不重新实现伤害公式，只编排 :func:`calc.dps.hit_dps`，保证单方案、比较和反解
使用同一条计算链。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from calc.dps import Conversion, HitDpsResult, hit_dps


@dataclass(frozen=True)
class ScenarioDelta:
    name: str
    dps: float
    dps_delta: float
    dps_delta_pct: float | None
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
    best_burst_scenario: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TargetRateResult:
    reachable: bool
    target_dps: float
    dps_per_effective_action: float
    required_effective_action_rate: float | None
    required_raw_action_rate: float | None
    cooldown_cap: float | None
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
            average_hit=result.average_hit,
            average_hit_delta=result.average_hit - baseline.average_hit,
            burst_damage=result.average_burst_damage,
            burst_damage_delta=result.average_burst_damage - baseline.average_burst_damage,
            result=result,
        ))
    return DpsComparison(
        baseline=baseline_name,
        scenarios=tuple(rows),
        best_dps_scenario=max(rows, key=lambda row: row.dps).name,
        best_burst_scenario=max(rows, key=lambda row: row.burst_damage).name,
    )


def target_action_rate(target_dps: float, inputs: Mapping[str, Any]) -> TargetRateResult:
    """反解达到目标 DPS 所需的原始频率，并正确处理触发/非触发冷却。"""
    if isinstance(target_dps, bool) or not isinstance(target_dps, (int, float)):
        raise ValueError("target_dps 必须是数字")
    target = float(target_dps)
    if not math.isfinite(target) or target <= 0:
        raise ValueError("target_dps 必须是有限正数")

    values = _kwargs(inputs)
    values.pop("action_rate", None)
    raw_cooldown = values.pop("cooldown_seconds", 0.0)
    if isinstance(raw_cooldown, bool) or not isinstance(raw_cooldown, (int, float)):
        raise ValueError("cooldown_seconds 必须是数字")
    cooldown = float(raw_cooldown)
    if not math.isfinite(cooldown) or cooldown < 0:
        raise ValueError("cooldown_seconds 必须是有限非负数")
    triggered = bool(values.pop("triggered", False))
    unit = hit_dps(**values, action_rate=1.0, cooldown_seconds=0.0, triggered=False)
    per_rate = unit.dps
    if per_rate <= 0:
        return TargetRateResult(
            False, target, per_rate, None, None,
            1 / cooldown if cooldown > 0 else None, None,
            "当前参数每次使用的期望伤害为 0，无法通过提高频率达到目标",
        )

    required_effective = target / per_rate
    cap = 1 / cooldown if cooldown > 0 else None
    if cooldown <= 0:
        required_raw = required_effective
    elif triggered:
        if required_effective > cap:
            return TargetRateResult(
                False, target, per_rate, required_effective, None, cap, None,
                "触发冷却限制了最高有效频率",
            )
        required_raw = required_effective
    else:
        # effective = 1 / (1/raw + cooldown)，其上限 1/cooldown 只能无限逼近。
        if required_effective >= cap:
            return TargetRateResult(
                False, target, per_rate, required_effective, None, cap, None,
                "非触发技能的动作时间与冷却相加，目标频率达到或超过冷却渐近上限",
            )
        required_raw = 1 / (1 / required_effective - cooldown)

    verification = hit_dps(
        **values,
        action_rate=required_raw,
        cooldown_seconds=cooldown,
        triggered=triggered,
    )
    return TargetRateResult(
        True, target, per_rate, required_effective, required_raw, cap,
        verification.dps, "已用同一伤害链回代验证",
    )
