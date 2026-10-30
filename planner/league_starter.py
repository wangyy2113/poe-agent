"""Ordered league-start checkpoints from PoB snapshots supplied by the user.

This is a route through explicit target builds, not a generated optimal build.
Every numeric transition reuses the strict PoB context checks from build_compare.
"""

from __future__ import annotations

from dataclasses import dataclass

from parsers.build_compare import ScenarioStructureDiff, compare_build_scenarios
from parsers.build_growth import GROWTH_METRICS
from parsers.pob_build import PobBuild


MAX_STAGES = 5


@dataclass(frozen=True)
class StageMetric:
    metric: str
    label: str
    unit: str
    status: str
    previous_value: float | None
    previous_field: str
    target_value: float | None
    target_field: str
    delta: float | None
    delta_percent: float | None
    reason: str


@dataclass(frozen=True)
class StageEquipmentTarget:
    slot: str
    status: str
    name: str
    base_type: str
    note: str


@dataclass(frozen=True)
class LeagueStage:
    sequence: int
    label: str
    goal: str
    previous_label: str
    target_level: int | None
    target_class: str
    target_ascendancy: str
    target_skill: str
    target_tree_version: str
    structure: ScenarioStructureDiff
    equipment_targets: tuple[StageEquipmentTarget, ...]
    context_status: str
    context_reasons: tuple[str, ...]
    metrics: tuple[StageMetric, ...]
    verification: str


@dataclass(frozen=True)
class LeagueStarterPlan:
    status: str
    stage_count: int
    metric_transition_count: int
    numeric_ready: int
    numeric_unknown: int
    stages: tuple[LeagueStage, ...]
    note: str


def plan_league_starter(
    current: PobBuild,
    stages: list[tuple[str, str, PobBuild]],
    *,
    metrics: list[str] | None = None,
) -> LeagueStarterPlan:
    """Describe each adjacent PoB transition and a reproducible completion check."""
    if not 1 <= len(stages) <= MAX_STAGES:
        raise ValueError(f"开荒阶段数量必须在 1..{MAX_STAGES} 之间")
    labels = [label.strip() for label, _, _ in stages]
    if any(not label for label in labels) or len(set(labels)) != len(labels):
        raise ValueError("阶段 label 必须非空且不能重复")
    if "当前" in labels:
        raise ValueError("阶段 label 不能使用保留名称「当前」")
    if any(not goal.strip() for _, goal, _ in stages):
        raise ValueError("每个阶段必须给出用户自己的 goal")

    selected_metrics = list(GROWTH_METRICS if metrics is None else metrics)
    if not selected_metrics or len(set(selected_metrics)) != len(selected_metrics):
        raise ValueError("metrics 必须非空且不能重复")
    unknown = [metric for metric in selected_metrics if metric not in GROWTH_METRICS]
    if unknown:
        raise ValueError("不支持的比较指标：" + "、".join(unknown))

    previous_label = "当前"
    previous_build = current
    planned: list[LeagueStage] = []
    numeric_ready = 0
    numeric_unknown = 0
    for sequence, (label, goal, target) in enumerate(stages, start=1):
        label = label.strip()
        comparison = compare_build_scenarios(
            [(previous_label, previous_build), (label, target)],
            metrics=selected_metrics,
        )
        snapshot = comparison.scenarios[1]
        context = comparison.context_checks[1]
        structure = comparison.structure_diffs[1]
        target_items = {item.slot.strip(): item for item in target.items if item.slot.strip()}
        equipment_targets: list[StageEquipmentTarget] = []
        for slot in structure.changed_item_slots:
            item = target_items.get(slot)
            if item is None:
                equipment_targets.append(StageEquipmentTarget(
                    slot, "empty" if target.structure_complete else "unknown",
                    "", "",
                    "目标快照该槽为空" if target.structure_complete else
                    "目标快照结构不完整，无法确认该槽是否为空",
                ))
            elif item.parsed is None:
                equipment_targets.append(StageEquipmentTarget(
                    slot, "unknown", "", "", item.error or "目标装备文本未能解析",
                ))
            else:
                equipment_targets.append(StageEquipmentTarget(
                    slot, "present", item.parsed.name, item.parsed.base_type,
                    "名称与底材取自目标 PoB；未校验装备获得方式、词缀效果或成本",
                ))
        stage_metrics: list[StageMetric] = []
        for comparison_metric in comparison.metrics:
            previous_value, target_value = comparison_metric.values
            if target_value.status == "ready":
                numeric_ready += 1
            else:
                numeric_unknown += 1
            stage_metrics.append(StageMetric(
                metric=comparison_metric.metric,
                label=comparison_metric.label,
                unit=comparison_metric.unit,
                status=target_value.status,
                previous_value=previous_value.value,
                previous_field=previous_value.source_field,
                target_value=target_value.value,
                target_field=target_value.source_field,
                delta=target_value.delta,
                delta_percent=target_value.delta_percent,
                reason=target_value.reason,
            ))
        planned.append(LeagueStage(
            sequence=sequence,
            label=label,
            goal=goal.strip(),
            previous_label=previous_label,
            target_level=snapshot.level,
            target_class=snapshot.class_name,
            target_ascendancy=snapshot.ascend_class_name,
            target_skill=snapshot.active_skill,
            target_tree_version=snapshot.tree_version,
            structure=structure,
            equipment_targets=tuple(equipment_targets),
            context_status=context.status,
            context_reasons=context.reasons,
            metrics=tuple(stage_metrics),
            verification=(
                f"达到「{label}」后导出新的完整 PoB，逐项核对等级、当前 Calcs 技能、"
                "装备槽、启用宝石和天赋节点；数值必须在相同 Calcs 技能、完整配置、"
                "树版本及 PlayerStat 字段口径下重新计算。"
            ),
        ))
        previous_label, previous_build = label, target

    status = (
        "ready" if numeric_unknown == 0
        else "partial" if numeric_ready
        else "unknown"
    )
    return LeagueStarterPlan(
        status=status,
        stage_count=len(planned),
        metric_transition_count=numeric_ready + numeric_unknown,
        numeric_ready=numeric_ready,
        numeric_unknown=numeric_unknown,
        stages=tuple(planned),
        note=(
            "阶段顺序与目标全部来自用户提供的 PoB 和文字，不是自动最优开荒路线。"
            "结构差异不证明 PlayerStat 变化由其造成；目标快照数值是 PoB 自带的报告值，"
            "并非本库重算、游戏门槛或可保证收益。缺少可比上下文时数值差额保持 unknown。"
        ),
    )
