"""显式输入的多技能 DPS 组合工具。"""

from __future__ import annotations

from typing import Any

from calc import constants
from calc.dps_rotation import (
    RotationComparison,
    RotationDpsResult,
    compare_rotations,
    rotation_dps,
    target_rate_multiplier,
)
from calc.derive import fmt
from tools.impl.dps import _citation, _with_default_crit
from tools.registry import REGISTRY, ToolResult


BOUNDARY = (
    "只组合 2–10 个参数完整的单技能确定性伤害链。rotation 模式把 allocation_pct 作为共享"
    "动作时间预算，parallel 模式把它作为独立来源覆盖率；点燃与单层流血自动跨技能只取"
    "最强实例，其它不可叠加技能 DoT 必须由 dot_groups 显式声明。组合层统一应用一次全局"
    "DoT 上限和顶层终结。它不从装备/天赋/PoB Build 自动汇总参数，不模拟走位、资源耗尽、"
    "阶段切换、动态 buff 快照、召唤物 AI 或未声明的地面效果互斥，不能称为完整 Build DPS"
)


def _skills_with_default_crit(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in skills:
        row = dict(item)
        row["inputs"] = _with_default_crit(dict(row.get("inputs", {})))
        normalized.append(row)
    return normalized


def _render(result: RotationDpsResult) -> str:
    mode = "动作轮转" if result.mode == "rotation" else "并行来源"
    lines = [
        "### 多技能组合 DPS", "",
        f"- 组合模式：{mode}；频率统一倍率：{result.rate_multiplier:g}",
        f"- 动作/覆盖分配合计：{result.action_budget_pct:g}%"
        + (
            f"；未分配动作预算 {result.unallocated_action_budget_pct:g}%"
            if result.unallocated_action_budget_pct is not None else ""
        ),
        "", "| 技能 | 分配 | 配置频率 | 分配后频率 | 最终有效频率 | 命中 | 穿刺 | 独立 DoT | 共享规则前合计 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for skill in result.skills:
        lines.append(
            f"| {skill.name} | {skill.allocation_pct:g}% | {fmt(skill.configured_action_rate)} | "
            f"{fmt(skill.allocated_action_rate)} | {fmt(skill.effective_action_rate)} | "
            f"{fmt(skill.hit_dps)} | {fmt(skill.impale_dps)} | "
            f"{fmt(skill.independent_dot_dps)} | {fmt(skill.total_before_shared_rules)} |"
        )
    if result.dot_components:
        lines.extend([
            "", "| DoT 分量 | 来源技能 | 稳态 DPS | 互斥组 |",
            "| --- | --- | ---: | --- |",
        ])
        for item in result.dot_components:
            lines.append(
                f"| {item.display_name} | {item.skill_name} | {fmt(item.steady_dps)} | "
                f"{item.group_name or '独立相加'} |"
            )
    if result.dot_groups:
        lines.extend(["", "互斥组结算："])
        for group in result.dot_groups:
            lines.append(
                f"- {group.name}：稳态取 {group.steady_selected_component_id} = "
                f"{fmt(group.steady_contribution_dps)} DPS"
                + (
                    f"；窗口平均 {fmt(group.combat_average_contribution_dps)}，"
                    f"主导来源切换 {group.dominant_switches} 次"
                    if group.combat_average_contribution_dps is not None else ""
                )
            )
    lines.extend([
        "", f"- 稳态命中+穿刺：{fmt(result.steady_combined_dps)}",
        f"- 共享规则后 DoT：{fmt(result.steady_total_dot_dps)}"
        f"（封顶前 {fmt(result.steady_total_dot_dps_uncapped)}）"
        f"{'，已触及全局上限' if result.steady_total_dot_dps_capped else ''}",
        f"- 顶层终结：{result.cull_percent:g}%（×{result.cull_multiplier:.4f}）",
        f"", f"**稳态组合总 DPS：{fmt(result.steady_total_dps)}；"
        f"含终结等效 {fmt(result.effective_steady_total_dps)}**",
    ])
    if result.combat_timeline is not None:
        timeline = result.combat_timeline
        lines.extend([
            "", f"### {timeline.combat_duration_seconds:g} 秒组合时间线", "",
            "| 时间 | 命中+穿刺 | 封顶前 DoT | 最终 DoT | 总 DPS | 含终结 |",
            "| ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        for point in timeline.points:
            lines.append(
                f"| {point.time_seconds:.3f}s | {fmt(point.combined_dps)} | "
                f"{fmt(point.total_dot_dps_uncapped)} | {fmt(point.total_dot_dps)}"
                f"{'*' if point.total_dot_dps_capped else ''} | {fmt(point.total_dps)} | "
                f"{fmt(point.effective_total_dps)} |"
            )
        lines.extend([
            "", f"- 时间线口径：{timeline.integration_steps} 段对齐分量 Simpson 积分；"
            "每个时点先做跨技能互斥，再做一次全局 DoT 封顶",
            f"- 窗口平均命中+穿刺 / DoT / 总 DPS：{fmt(timeline.average_combined_dps)} / "
            f"{fmt(timeline.average_total_dot_dps)} / {fmt(timeline.average_total_dps)}",
            f"- 窗口平均含终结总等效 DPS：{fmt(timeline.average_effective_total_dps)}",
        ])
        if timeline.global_dot_cap_reached:
            lines.append(
                f"- 全局 DoT 上限首次采样命中："
                f"{timeline.global_dot_cap_first_reached_seconds:.3f}s；"
                f"窗口封顶采样占比 {timeline.global_dot_cap_uptime_pct:.2f}%"
            )
    lines.extend(["", BOUNDARY + "。"])
    return "\n".join(lines)


_SKILLS_SCHEMA = {
    "type": "array", "minItems": 2, "maxItems": 10,
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "allocation_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
            "inputs": {
                "type": "object",
                "description": "hit_dps_calc 的完整单技能参数；必须包含 action_rate",
            },
        },
        "required": ["name", "inputs"],
    },
}

_DOT_GROUPS_SCHEMA = {
    "type": "array", "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "policy": {"type": "string", "enum": ["strongest"]},
            "members": {
                "type": "array", "minItems": 2,
                "items": {
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string"},
                        "component": {
                            "type": "string",
                            "enum": ["ignite", "poison", "bleed", "skill_dot"],
                        },
                    },
                    "required": ["skill", "component"],
                },
            },
        },
        "required": ["name", "members"],
    },
}


@REGISTRY.register(
    name="rotation_dps_calc",
    description=(
        "组合 2–10 个显式单技能伤害链。支持共享动作时间轮转或并行来源、跨技能最强 DoT "
        "互斥、一次全局 DoT 上限、顶层终结和固定战斗窗口；不自动读取 Build。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skills": _SKILLS_SCHEMA,
            "mode": {"type": "string", "enum": ["rotation", "parallel"], "default": "rotation"},
            "dot_groups": _DOT_GROUPS_SCHEMA,
            "combat_duration_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
            "cull_percent": {"type": "number", "minimum": 0, "exclusiveMaximum": 100},
        },
        "required": ["skills"],
    },
)
def rotation_dps_calc(
    skills: list[dict[str, Any]], mode: str = "rotation",
    dot_groups: list[dict[str, Any]] | None = None,
    combat_duration_seconds: float | None = None, cull_percent: float = 0.0,
) -> ToolResult:
    try:
        result = rotation_dps(
            skills=_skills_with_default_crit(skills), mode=mode,
            dot_groups=dot_groups or (), combat_duration_seconds=combat_duration_seconds,
            cull_percent=cull_percent,
        )
    except (ValueError, TypeError, constants.MissingConstant) as exc:
        return ToolResult(ok=False, error=str(exc))
    answer = _render(result)
    data = result.as_dict()
    data.update({"kind": "rotation_dps", "answer": answer, "boundary": BOUNDARY})
    return ToolResult(
        ok=True, data=data,
        citations=[_citation(answer, slug="multi-skill-rotation", title="多技能组合 DPS")],
        meta={"channel": "derived"},
    )


@REGISTRY.register(
    name="rotation_dps_compare",
    description="比较 2–10 套多技能轮转/并行组合，返回稳态与固定窗口总 DPS 差值和最佳方案。",
    parameters={
        "type": "object", "properties": {
            "scenarios": {
                "type": "array", "minItems": 2, "maxItems": 10,
                "items": {
                    "type": "object", "properties": {
                        "name": {"type": "string"}, "inputs": {"type": "object"},
                    }, "required": ["name", "inputs"],
                },
            },
        }, "required": ["scenarios"],
    },
)
def rotation_dps_compare(scenarios: list[dict[str, Any]]) -> ToolResult:
    try:
        prepared = []
        for item in scenarios:
            inputs = dict(item["inputs"])
            inputs["skills"] = _skills_with_default_crit(list(inputs.get("skills", [])))
            prepared.append((str(item["name"]), inputs))
        result = compare_rotations(prepared)
    except (KeyError, ValueError, TypeError, constants.MissingConstant) as exc:
        return ToolResult(ok=False, error=str(exc))
    lines = [
        "### 多技能组合方案比较", "",
        "| 方案 | 稳态总 DPS | 稳态 DoT | 含终结 | 固定窗口总 DPS | 相对基线 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in result.scenarios:
        delta = (
            "基线" if row.name == result.baseline
            else "不可定义" if row.steady_delta_pct is None
            else f"{row.steady_delta_pct:+.2f}%"
        )
        lines.append(
            f"| {row.name} | {fmt(row.steady_total_dps)} | {fmt(row.steady_total_dot_dps)} | "
            f"{fmt(row.effective_steady_total_dps)} | "
            f"{fmt(row.combat_average_total_dps) if row.combat_average_total_dps is not None else '—'} | "
            f"{delta} |"
        )
    lines.extend([
        "", f"- 最高稳态总 DPS：{result.best_steady_scenario}",
        f"- 最高稳态 DoT：{result.best_steady_dot_scenario}",
        f"- 最高固定窗口总 DPS：{result.best_combat_scenario or '未提供战斗时长'}",
        "", BOUNDARY + "。",
    ])
    answer = "\n".join(lines)
    data = result.as_dict()
    data.update({"kind": "rotation_comparison", "answer": answer, "boundary": BOUNDARY})
    return ToolResult(
        ok=True, data=data,
        citations=[_citation(answer, slug="multi-skill-comparison", title="多技能组合方案比较")],
        meta={"channel": "derived"},
    )


@REGISTRY.register(
    name="rotation_dps_target_multiplier",
    description=(
        "统一缩放多技能组合中所有配置动作频率，反解目标稳态或固定窗口总 DPS 所需倍率，"
        "并用完整互斥/全局封顶链回代。"
    ),
    parameters={
        "type": "object", "properties": {
            "target_dps": {"type": "number", "exclusiveMinimum": 0},
            "metric": {"type": "string", "enum": [
                "steady_total_dps", "steady_total_dot_dps", "effective_steady_total_dps",
                "combat_average_total_dps", "combat_average_total_dot_dps",
                "combat_average_effective_total_dps",
            ], "default": "steady_total_dps"},
            "inputs": {"type": "object"},
        }, "required": ["target_dps", "inputs"],
    },
)
def rotation_dps_target_multiplier(
    target_dps: float, inputs: dict[str, Any], metric: str = "steady_total_dps",
) -> ToolResult:
    try:
        prepared = dict(inputs)
        prepared["skills"] = _skills_with_default_crit(list(prepared.get("skills", [])))
        result = target_rate_multiplier(target_dps, prepared, metric=metric)
    except (ValueError, TypeError, constants.MissingConstant) as exc:
        return ToolResult(ok=False, error=str(exc))
    if result.reachable:
        answer = (
            f"达到 {fmt(result.target_dps)} 的 {result.metric} 需要把所有技能配置动作频率统一乘以 "
            f"{fmt(result.required_rate_multiplier or 0)}；完整组合链回代为 "
            f"{fmt(result.achieved_dps or 0)}。"
        )
    else:
        answer = (
            f"目标 {fmt(result.target_dps)} 的 {result.metric} 在统一频率倍率 "
            f"{fmt(result.checked_upper_multiplier)} 以内不可达；最高检查值 "
            f"{fmt(result.maximum_checked_dps)}：{result.reason}。"
        )
    data = result.as_dict()
    data.update({"kind": "rotation_target", "answer": answer, "boundary": BOUNDARY})
    return ToolResult(
        ok=True, data=data,
        citations=[_citation(answer, slug="multi-skill-target", title="多技能组合目标频率倍率")],
        meta={"channel": "derived"},
    )
