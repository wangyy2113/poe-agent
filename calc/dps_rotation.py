"""显式输入的多技能输出组合、互斥 DoT 与固定战斗窗口。

本模块不重复实现单技能公式；每个技能先交给 :func:`calc.dps.hit_dps`，这里只负责
动作时间分配、跨技能持续伤害互斥、一次全局 DoT 封顶和组合级比较/反解。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from calc import constants
from calc.dps import Conversion, HitDpsResult, hit_dps


ROTATION_MODES = {"rotation", "parallel"}
DOT_COMPONENTS = {"ignite", "poison", "bleed", "skill_dot"}
TARGET_METRICS = {
    "steady_total_dps": "steady_total_dps",
    "steady_total_dot_dps": "steady_total_dot_dps",
    "effective_steady_total_dps": "effective_steady_total_dps",
    "combat_average_total_dps": "combat_average_total_dps",
    "combat_average_total_dot_dps": "combat_average_total_dot_dps",
    "combat_average_effective_total_dps": "combat_average_effective_total_dps",
}
MAX_SKILLS = 10
TIMELINE_STEPS = 512


@dataclass(frozen=True)
class RotationDotComponent:
    component_id: str
    skill_name: str
    component: str
    display_name: str
    stack_limit: float | None
    steady_dps: float
    combat_average_dps: float | None
    group_name: str | None


@dataclass(frozen=True)
class RotationSkillResult:
    name: str
    allocation_pct: float
    configured_action_rate: float
    allocated_action_rate: float
    effective_action_rate: float
    hit_dps: float
    impale_dps: float
    combined_dps: float
    independent_dot_dps: float
    total_before_shared_rules: float
    combat_average_combined_dps: float | None
    combat_average_independent_dot_dps: float | None
    combat_average_total_before_shared_rules: float | None
    dot_component_ids: tuple[str, ...]


@dataclass(frozen=True)
class RotationDotGroupResult:
    name: str
    policy: str
    member_ids: tuple[str, ...]
    steady_contribution_dps: float
    steady_selected_component_id: str
    combat_average_contribution_dps: float | None
    dominant_switches: int | None


@dataclass(frozen=True)
class RotationSkillTimelineContribution:
    name: str
    combined_dps: float
    independent_dot_dps: float


@dataclass(frozen=True)
class RotationTimelinePoint:
    time_seconds: float
    skills: tuple[RotationSkillTimelineContribution, ...]
    combined_dps: float
    total_dot_dps_uncapped: float
    total_dot_dps: float
    total_dot_dps_capped: bool
    total_dps: float
    effective_total_dps: float


@dataclass(frozen=True)
class RotationTimeline:
    combat_duration_seconds: float
    calculation_mode: str
    integration_steps: int
    average_combined_dps: float
    average_total_dot_dps: float
    average_total_dps: float
    average_effective_total_dps: float
    global_dot_cap_reached: bool
    global_dot_cap_first_reached_seconds: float | None
    global_dot_cap_uptime_pct: float
    points: tuple[RotationTimelinePoint, ...]


@dataclass(frozen=True)
class RotationDpsResult:
    mode: str
    rate_multiplier: float
    action_budget_pct: float
    unallocated_action_budget_pct: float | None
    cull_percent: float
    cull_multiplier: float
    skills: tuple[RotationSkillResult, ...]
    dot_components: tuple[RotationDotComponent, ...]
    dot_groups: tuple[RotationDotGroupResult, ...]
    steady_combined_dps: float
    steady_total_dot_dps_uncapped: float
    steady_total_dot_dps: float
    steady_total_dot_dps_cap: float
    steady_total_dot_dps_capped: bool
    steady_total_dps: float
    effective_steady_total_dps: float
    combat_timeline: RotationTimeline | None
    combat_average_combined_dps: float | None
    combat_average_total_dot_dps: float | None
    combat_average_total_dps: float | None
    combat_average_effective_total_dps: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RotationScenarioDelta:
    name: str
    steady_total_dps: float
    steady_delta: float
    steady_delta_pct: float | None
    steady_total_dot_dps: float
    effective_steady_total_dps: float
    combat_average_total_dps: float | None
    combat_average_total_dot_dps: float | None
    combat_delta: float | None
    result: RotationDpsResult


@dataclass(frozen=True)
class RotationComparison:
    baseline: str
    scenarios: tuple[RotationScenarioDelta, ...]
    best_steady_scenario: str
    best_steady_dot_scenario: str
    best_effective_steady_scenario: str
    best_combat_scenario: str | None
    best_combat_dot_scenario: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RotationTargetResult:
    reachable: bool
    metric: str
    target_dps: float
    required_rate_multiplier: float | None
    achieved_dps: float | None
    checked_upper_multiplier: float
    maximum_checked_dps: float
    cap_sources: tuple[str, ...]
    reason: str
    result: RotationDpsResult | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: object, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{label} 必须是大于等于 {minimum:g} 的有限数字")
    return result


def _skill_kwargs(raw: Mapping[str, Any]) -> dict[str, Any]:
    values = dict(raw)
    conversions = values.get("conversions", ())
    if not isinstance(conversions, (list, tuple)):
        raise ValueError("技能 inputs.conversions 必须是数组")
    values["conversions"] = [
        item if isinstance(item, Conversion) else Conversion(**item)
        for item in conversions
    ]
    return values


def _component_id(skill_name: str, component: str) -> str:
    return f"{skill_name}:{component}"


def _component_averages(result: HitDpsResult) -> dict[str, float]:
    averages: dict[str, float] = {}
    timeline = result.combat_timeline
    if timeline is None:
        return averages
    for item in timeline.ailment_summaries:
        averages[item.name_en.lower()] = item.average_dps
    if timeline.skill_dot_summary is not None:
        averages["skill_dot"] = timeline.skill_dot_summary.average_dps
    return averages


def _build_components(
    names: Sequence[str], results: Sequence[HitDpsResult],
) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for name, result in zip(names, results, strict=True):
        averages = _component_averages(result)
        for ailment in result.damaging_ailments:
            component = ailment.name_en.lower()
            components.append({
                "component_id": _component_id(name, component),
                "skill_name": name,
                "component": component,
                "display_name": ailment.name,
                "stack_limit": ailment.stack_limit,
                "steady_dps": ailment.dps,
                "combat_average_dps": averages.get(component),
            })
        if result.skill_dot is not None:
            components.append({
                "component_id": _component_id(name, "skill_dot"),
                "skill_name": name,
                "component": "skill_dot",
                "display_name": "技能 DoT",
                "stack_limit": result.skill_dot.stack_limit,
                "steady_dps": result.skill_dot.dps,
                "combat_average_dps": averages.get("skill_dot"),
            })
    return components


def _normalize_groups(
    raw_groups: Sequence[Mapping[str, Any]], components: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    by_id = {str(item["component_id"]): item for item in components}
    assignment: dict[str, str] = {}
    groups: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_groups):
        if not isinstance(raw, Mapping):
            raise ValueError(f"dot_groups[{index}] 必须是对象")
        name = str(raw.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValueError("DoT 互斥组名称必须为 1..80 个字符")
        if name in names:
            raise ValueError("DoT 互斥组名称不能重复")
        names.add(name)
        policy = str(raw.get("policy", "strongest")).strip().lower()
        if policy != "strongest":
            raise ValueError("DoT 互斥组 policy 目前只支持 strongest")
        raw_members = raw.get("members")
        if not isinstance(raw_members, (list, tuple)) or len(raw_members) < 2:
            raise ValueError("DoT 互斥组至少需要两个 members")
        member_ids: list[str] = []
        for member_index, member in enumerate(raw_members):
            if not isinstance(member, Mapping):
                raise ValueError(f"dot_groups[{index}].members[{member_index}] 必须是对象")
            skill = str(member.get("skill", "")).strip()
            component = str(member.get("component", "")).strip().lower()
            if component not in DOT_COMPONENTS:
                raise ValueError("DoT 互斥成员 component 只支持 ignite/poison/bleed/skill_dot")
            component_id = _component_id(skill, component)
            if component_id not in by_id:
                raise ValueError(f"DoT 互斥成员 {component_id} 不存在")
            if component_id in assignment:
                raise ValueError(f"DoT 分量 {component_id} 不能同时属于多个互斥组")
            assignment[component_id] = name
            member_ids.append(component_id)
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("DoT 互斥组 members 不能重复")
        groups.append({"name": name, "policy": policy, "member_ids": tuple(member_ids)})

    # 默认游戏口径：同一目标上单层点燃/流血跨技能只取最强实例。显式多层上限不自动互斥。
    for component in ("ignite", "bleed"):
        members = [
            str(item["component_id"])
            for item in components
            if item["component"] == component
            and item["stack_limit"] == 1
            and item["component_id"] not in assignment
        ]
        if len(members) < 2:
            continue
        base_name = f"auto:{component}:strongest"
        name = base_name
        suffix = 2
        while name in names:
            name = f"{base_name}:{suffix}"
            suffix += 1
        names.add(name)
        for member_id in members:
            assignment[member_id] = name
        groups.append({"name": name, "policy": "strongest", "member_ids": tuple(members)})
    return assignment, groups


def _compose_dot(
    values: Mapping[str, float], groups: Sequence[Mapping[str, Any]],
    assignment: Mapping[str, str],
) -> tuple[float, dict[str, tuple[float, str]]]:
    total = sum(value for key, value in values.items() if key not in assignment)
    contributions: dict[str, tuple[float, str]] = {}
    for group in groups:
        members = group["member_ids"]
        selected = max(members, key=lambda key: values.get(key, 0.0))
        contribution = values.get(selected, 0.0)
        contributions[str(group["name"])] = (contribution, selected)
        total += contribution
    return total, contributions


def rotation_dps(
    *, skills: Sequence[Mapping[str, Any]], mode: str = "rotation",
    dot_groups: Sequence[Mapping[str, Any]] = (),
    combat_duration_seconds: float | None = None,
    cull_percent: float = 0.0, rate_multiplier: float = 1.0,
) -> RotationDpsResult:
    """组合 2–10 个显式单技能伤害链。

    ``rotation`` 模式要求各技能给 ``allocation_pct``，并把动作频率乘以该分配比例；
    ``parallel`` 模式把同一字段解释为独立来源的有效覆盖率，默认 100%。
    """
    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in ROTATION_MODES:
        raise ValueError("mode 只支持 rotation 或 parallel")
    if not isinstance(skills, (list, tuple)) or not 2 <= len(skills) <= MAX_SKILLS:
        raise ValueError(f"skills 必须包含 2..{MAX_SKILLS} 个技能")
    multiplier = _number(rate_multiplier, "rate_multiplier")
    if multiplier <= 0:
        raise ValueError("rate_multiplier 必须大于 0")
    cull = _number(cull_percent, "cull_percent")
    if cull >= 100:
        raise ValueError("cull_percent 必须小于 100")
    cull_multiplier = 100 / (100 - cull)
    duration = None
    grid: tuple[float, ...] = ()
    if combat_duration_seconds is not None:
        duration = _number(combat_duration_seconds, "combat_duration_seconds")
        if duration <= 0 or duration > 3600:
            raise ValueError("combat_duration_seconds 必须在 0..3600 秒")
        grid = tuple(index * duration / TIMELINE_STEPS for index in range(TIMELINE_STEPS + 1))

    names: list[str] = []
    allocations: list[float] = []
    configured_rates: list[float] = []
    prepared_inputs: list[dict[str, Any]] = []
    for index, raw_skill in enumerate(skills):
        if not isinstance(raw_skill, Mapping):
            raise ValueError(f"skills[{index}] 必须是对象")
        name = str(raw_skill.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValueError("技能名称必须为 1..80 个字符")
        if name in names:
            raise ValueError("技能名称不能重复")
        names.append(name)
        raw_allocation = raw_skill.get("allocation_pct")
        if raw_allocation is None and normalized_mode == "rotation":
            raise ValueError("rotation 模式下每个技能都必须提供 allocation_pct")
        allocation = _number(
            100.0 if raw_allocation is None else raw_allocation,
            f"{name}.allocation_pct",
        )
        if allocation <= 0 or allocation > 100:
            raise ValueError(f"{name}.allocation_pct 必须在 0..100")
        allocations.append(allocation)
        raw_inputs = raw_skill.get("inputs")
        if not isinstance(raw_inputs, Mapping):
            raise ValueError(f"{name}.inputs 必须是对象")
        inputs = _skill_kwargs(raw_inputs)
        raw_rate = inputs.get("action_rate")
        configured_rate = _number(raw_rate, f"{name}.inputs.action_rate")
        if configured_rate <= 0:
            raise ValueError(f"{name}.inputs.action_rate 必须大于 0")
        configured_rates.append(configured_rate)
        if float(inputs.get("cull_percent", 0.0)) or float(
            inputs.get("critical_cull_percent", 0.0)
        ):
            raise ValueError("多技能组合的终结必须使用顶层 cull_percent，不能写在单技能 inputs")
        if "combat_duration_seconds" in inputs or "combat_sample_times" in inputs:
            raise ValueError("战斗时长与采样点必须由多技能组合顶层统一提供")
        inputs["action_rate"] = configured_rate * allocation / 100 * multiplier
        inputs["combat_duration_seconds"] = duration
        inputs["combat_sample_times"] = grid
        prepared_inputs.append(inputs)
    action_budget = sum(allocations)
    if normalized_mode == "rotation" and action_budget > 100 + 1e-9:
        raise ValueError("rotation 模式的 allocation_pct 合计不能超过 100")
    calculated = [hit_dps(**inputs) for inputs in prepared_inputs]

    raw_components = _build_components(names, calculated)
    assignment, normalized_groups = _normalize_groups(dot_groups, raw_components)
    steady_values = {
        str(item["component_id"]): float(item["steady_dps"])
        for item in raw_components
    }
    steady_dot_uncapped, steady_group_values = _compose_dot(
        steady_values, normalized_groups, assignment
    )
    dot_cap = constants.get(constants.DOT_DPS_CAP) if raw_components else 0.0
    steady_dot = min(steady_dot_uncapped, dot_cap) if dot_cap > 0 else 0.0
    steady_combined = sum(result.combined_dps for result in calculated)
    steady_total = steady_combined + steady_dot

    group_average_values: dict[str, float] = {}
    group_switches: dict[str, int] = {}
    timeline = None
    if duration is not None:
        point_maps = []
        for result in calculated:
            if result.combat_timeline is None:
                raise ValueError("固定战斗窗口缺少单技能时间线")
            point_maps.append({
                round(point.time_seconds, 12): point
                for point in result.combat_timeline.points
            })
        combined_values: list[float] = []
        dot_values: list[float] = []
        total_values: list[float] = []
        effective_values: list[float] = []
        capped_values: list[bool] = []
        group_series: dict[str, list[float]] = {
            str(group["name"]): [] for group in normalized_groups
        }
        group_selected: dict[str, list[str]] = {
            str(group["name"]): [] for group in normalized_groups
        }
        all_points: list[RotationTimelinePoint] = []
        for seconds in grid:
            component_values: dict[str, float] = {}
            skill_rows: list[RotationSkillTimelineContribution] = []
            combined = 0.0
            for skill_index, (name, result) in enumerate(
                zip(names, calculated, strict=True)
            ):
                point = point_maps[skill_index].get(round(seconds, 12))
                if point is None:
                    raise ValueError(f"单技能时间线缺少组合采样点 {seconds:g}s")
                combined += point.hit_dps + point.impale_dps
                independent_dot = point.skill_dot_dps + sum(
                    item.dps for item in point.ailments
                )
                skill_rows.append(RotationSkillTimelineContribution(
                    name=name, combined_dps=point.hit_dps + point.impale_dps,
                    independent_dot_dps=independent_dot,
                ))
                for ailment in point.ailments:
                    component_values[_component_id(name, ailment.name_en.lower())] = ailment.dps
                if result.skill_dot is not None:
                    component_values[_component_id(name, "skill_dot")] = point.skill_dot_dps
            uncapped_dot, group_contributions = _compose_dot(
                component_values, normalized_groups, assignment
            )
            for group_name, (contribution, selected) in group_contributions.items():
                group_series[group_name].append(contribution)
                group_selected[group_name].append(selected)
            capped_dot = min(uncapped_dot, dot_cap) if dot_cap > 0 else 0.0
            total = combined + capped_dot
            combined_values.append(combined)
            dot_values.append(capped_dot)
            total_values.append(total)
            effective_values.append(total * cull_multiplier)
            capped_values.append(dot_cap > 0 and uncapped_dot > dot_cap)
            all_points.append(RotationTimelinePoint(
                time_seconds=seconds, skills=tuple(skill_rows),
                combined_dps=combined, total_dot_dps_uncapped=uncapped_dot,
                total_dot_dps=capped_dot,
                total_dot_dps_capped=dot_cap > 0 and uncapped_dot > dot_cap,
                total_dps=total, effective_total_dps=total * cull_multiplier,
            ))

        width = duration / TIMELINE_STEPS

        def simpson(values: Sequence[float]) -> float:
            return width / 3 * (
                values[0] + values[-1]
                + 4 * sum(values[1:-1:2]) + 2 * sum(values[2:-1:2])
            ) / duration

        average_combined = simpson(combined_values)
        average_dot = simpson(dot_values)
        average_total = simpson(total_values)
        average_effective = simpson(effective_values)
        for group_name, values in group_series.items():
            group_average_values[group_name] = simpson(values)
            selections = group_selected[group_name]
            group_switches[group_name] = sum(
                1 for before, after in zip(selections, selections[1:]) if before != after
            )
        first_cap = next(
            (index * width for index, capped in enumerate(capped_values) if capped), None
        )
        cap_uptime = (
            sum(1 for capped in capped_values if capped) / len(capped_values) * 100
        )
        display_indices = sorted({
            round(index * TIMELINE_STEPS / 20) for index in range(21)
        })
        timeline = RotationTimeline(
            combat_duration_seconds=duration,
            calculation_mode="aligned_component_simpson",
            integration_steps=TIMELINE_STEPS,
            average_combined_dps=average_combined,
            average_total_dot_dps=average_dot,
            average_total_dps=average_total,
            average_effective_total_dps=average_effective,
            global_dot_cap_reached=first_cap is not None,
            global_dot_cap_first_reached_seconds=first_cap,
            global_dot_cap_uptime_pct=cap_uptime,
            points=tuple(all_points[index] for index in display_indices),
        )

    components = tuple(RotationDotComponent(
        component_id=str(item["component_id"]), skill_name=str(item["skill_name"]),
        component=str(item["component"]), display_name=str(item["display_name"]),
        stack_limit=item["stack_limit"], steady_dps=float(item["steady_dps"]),
        combat_average_dps=item["combat_average_dps"],
        group_name=assignment.get(str(item["component_id"])),
    ) for item in raw_components)
    component_by_id = {item.component_id: item for item in components}
    group_results = tuple(RotationDotGroupResult(
        name=str(group["name"]), policy=str(group["policy"]),
        member_ids=tuple(group["member_ids"]),
        steady_contribution_dps=steady_group_values[str(group["name"])][0],
        steady_selected_component_id=steady_group_values[str(group["name"])][1],
        combat_average_contribution_dps=group_average_values.get(str(group["name"])),
        dominant_switches=group_switches.get(str(group["name"])),
    ) for group in normalized_groups)
    skill_rows = []
    for name, allocation, configured_rate, result in zip(
        names, allocations, configured_rates, calculated, strict=True
    ):
        component_ids = tuple(
            item.component_id for item in components if item.skill_name == name
        )
        independent_dot = sum(component_by_id[key].steady_dps for key in component_ids)
        average_independent = (
            sum(component_by_id[key].combat_average_dps or 0.0 for key in component_ids)
            if duration is not None else None
        )
        average_combined = result.combat_average_combined_dps
        skill_rows.append(RotationSkillResult(
            name=name, allocation_pct=allocation,
            configured_action_rate=configured_rate,
            allocated_action_rate=configured_rate * allocation / 100 * multiplier,
            effective_action_rate=result.action_rate,
            hit_dps=result.dps, impale_dps=result.impale_dps,
            combined_dps=result.combined_dps, independent_dot_dps=independent_dot,
            total_before_shared_rules=result.combined_dps + independent_dot,
            combat_average_combined_dps=average_combined,
            combat_average_independent_dot_dps=average_independent,
            combat_average_total_before_shared_rules=(
                (average_combined or 0.0) + (average_independent or 0.0)
                if duration is not None else None
            ),
            dot_component_ids=component_ids,
        ))
    return RotationDpsResult(
        mode=normalized_mode, rate_multiplier=multiplier,
        action_budget_pct=action_budget,
        unallocated_action_budget_pct=(
            max(0.0, 100 - action_budget) if normalized_mode == "rotation" else None
        ),
        cull_percent=cull, cull_multiplier=cull_multiplier,
        skills=tuple(skill_rows), dot_components=components, dot_groups=group_results,
        steady_combined_dps=steady_combined,
        steady_total_dot_dps_uncapped=steady_dot_uncapped,
        steady_total_dot_dps=steady_dot, steady_total_dot_dps_cap=dot_cap,
        steady_total_dot_dps_capped=dot_cap > 0 and steady_dot_uncapped > dot_cap,
        steady_total_dps=steady_total,
        effective_steady_total_dps=steady_total * cull_multiplier,
        combat_timeline=timeline,
        combat_average_combined_dps=(timeline.average_combined_dps if timeline else None),
        combat_average_total_dot_dps=(timeline.average_total_dot_dps if timeline else None),
        combat_average_total_dps=(timeline.average_total_dps if timeline else None),
        combat_average_effective_total_dps=(
            timeline.average_effective_total_dps if timeline else None
        ),
    )


def compare_rotations(
    scenarios: Sequence[tuple[str, Mapping[str, Any]]],
) -> RotationComparison:
    if not 2 <= len(scenarios) <= 10:
        raise ValueError("scenarios 必须包含 2..10 个方案")
    names = [str(name).strip() for name, _ in scenarios]
    if any(not name or len(name) > 80 for name in names) or len(set(names)) != len(names):
        raise ValueError("方案名称必须为不重复的 1..80 个字符")
    calculated = [(name, rotation_dps(**dict(raw))) for name, raw in scenarios]
    baseline = calculated[0][1]
    rows = []
    for name, result in calculated:
        delta = result.steady_total_dps - baseline.steady_total_dps
        combat_delta = (
            result.combat_average_total_dps - baseline.combat_average_total_dps
            if result.combat_average_total_dps is not None
            and baseline.combat_average_total_dps is not None else None
        )
        rows.append(RotationScenarioDelta(
            name=name, steady_total_dps=result.steady_total_dps,
            steady_delta=delta,
            steady_delta_pct=(
                delta / baseline.steady_total_dps * 100
                if baseline.steady_total_dps else None
            ),
            steady_total_dot_dps=result.steady_total_dot_dps,
            effective_steady_total_dps=result.effective_steady_total_dps,
            combat_average_total_dps=result.combat_average_total_dps,
            combat_average_total_dot_dps=result.combat_average_total_dot_dps,
            combat_delta=combat_delta, result=result,
        ))
    combat_rows = [row for row in rows if row.combat_average_total_dps is not None]
    return RotationComparison(
        baseline=names[0], scenarios=tuple(rows),
        best_steady_scenario=max(rows, key=lambda row: row.steady_total_dps).name,
        best_steady_dot_scenario=max(rows, key=lambda row: row.steady_total_dot_dps).name,
        best_effective_steady_scenario=max(
            rows, key=lambda row: row.effective_steady_total_dps
        ).name,
        best_combat_scenario=(
            max(combat_rows, key=lambda row: row.combat_average_total_dps or 0).name
            if combat_rows else None
        ),
        best_combat_dot_scenario=(
            max(combat_rows, key=lambda row: row.combat_average_total_dot_dps or 0).name
            if combat_rows else None
        ),
    )


def target_rate_multiplier(
    target_dps: float, inputs: Mapping[str, Any], *, metric: str = "steady_total_dps",
) -> RotationTargetResult:
    target = _number(target_dps, "target_dps")
    if target <= 0:
        raise ValueError("target_dps 必须大于 0")
    if metric not in TARGET_METRICS:
        raise ValueError("不支持的多技能目标指标")
    values = dict(inputs)
    values.pop("rate_multiplier", None)
    if metric.startswith("combat_average_") and values.get("combat_duration_seconds") is None:
        raise ValueError(f"{metric} 反解必须提供 combat_duration_seconds")
    for raw_skill in values.get("skills", ()):
        skill_inputs = raw_skill.get("inputs", {}) if isinstance(raw_skill, Mapping) else {}
        if float(skill_inputs.get("fist_of_war_more_damage_pct", 0.0)) > 0:
            raise ValueError("战争之拳覆盖会随组合频率变化，当前不保证单调，不能反解")
    field = TARGET_METRICS[metric]

    def calculate(multiplier: float) -> tuple[RotationDpsResult, float]:
        result = rotation_dps(**values, rate_multiplier=multiplier)
        raw_value = getattr(result, field)
        if raw_value is None:
            raise ValueError(f"{metric} 当前没有可计算值")
        return result, float(raw_value)

    lower = 1e-6
    upper = 1.0
    upper_result, upper_value = calculate(upper)
    while upper_value < target and upper < 1_000_000:
        upper *= 2
        upper_result, upper_value = calculate(upper)
    if upper_value < target and not math.isclose(upper_value, target, rel_tol=1e-12):
        cap_sources = []
        if upper_result.steady_total_dot_dps_capped:
            cap_sources.append("dot_cap")
        return RotationTargetResult(
            reachable=False, metric=metric, target_dps=target,
            required_rate_multiplier=None, achieved_dps=None,
            checked_upper_multiplier=upper, maximum_checked_dps=upper_value,
            cap_sources=tuple(cap_sources),
            reason="在允许的统一频率倍率范围内无法达到目标；冷却、刷新或 DoT 上限已形成平台",
            result=upper_result,
        )
    for _ in range(80):
        middle = (lower + upper) / 2
        _result, value = calculate(middle)
        if value >= target:
            upper = middle
        else:
            lower = middle
    result, achieved = calculate(upper)
    cap_sources = ("dot_cap",) if result.steady_total_dot_dps_capped else ()
    return RotationTargetResult(
        reachable=True, metric=metric, target_dps=target,
        required_rate_multiplier=upper, achieved_dps=achieved,
        checked_upper_multiplier=upper, maximum_checked_dps=achieved,
        cap_sources=cap_sources,
        reason="已统一缩放所有技能的配置动作频率，并用完整组合链二分回代验证",
        result=result,
    )
