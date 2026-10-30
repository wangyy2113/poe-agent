"""Conservative comparison of user-supplied PoB PlayerStat snapshots.

The module compares values already calculated by PoB.  It never recalculates a
build and never ranks scenarios across unlike metrics.  A delta is emitted only
when the candidate and reference use the same active calculation skill, exact
configuration snapshot, tree version, and PlayerStat field.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from parsers.build_growth import GROWTH_METRICS
from parsers.pob_build import PobBuild


MAX_SCENARIOS = 5


@dataclass(frozen=True)
class ScenarioSnapshot:
    label: str
    level: int | None
    class_name: str
    ascend_class_name: str
    active_skill: str
    enemy_context: str
    tree_version: str
    player_stat_count: int


@dataclass(frozen=True)
class ScenarioContextCheck:
    label: str
    status: str
    reasons: tuple[str, ...]
    differing_config_keys: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioStructureDiff:
    label: str
    status: str
    changed_item_slots: tuple[str, ...]
    added_gems: tuple[str, ...]
    removed_gems: tuple[str, ...]
    passive_nodes_added: tuple[str, ...]
    passive_nodes_removed: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class ScenarioMetricValue:
    label: str
    status: str
    value: float | None
    source_field: str
    delta: float | None
    delta_percent: float | None
    direction: str
    reason: str


@dataclass(frozen=True)
class MetricComparison:
    metric: str
    label: str
    unit: str
    status: str
    values: tuple[ScenarioMetricValue, ...]


@dataclass(frozen=True)
class BuildScenarioComparison:
    status: str
    reference_label: str
    scenario_count: int
    context_status: str
    ready_metrics: int
    partial_metrics: int
    unknown_metrics: int
    scenarios: tuple[ScenarioSnapshot, ...]
    context_checks: tuple[ScenarioContextCheck, ...]
    structure_diffs: tuple[ScenarioStructureDiff, ...]
    metrics: tuple[MetricComparison, ...]
    overall_verdict: str
    note: str


def _active_skill(build: PobBuild) -> str:
    # mainSocketGroup is a presentation choice, not proof of the skill used to
    # calculate PlayerStat.  Without Calcs.skill_number, numeric deltas are unknown.
    if build.calcs_skill_group is None:
        return ""
    group = build.calcs_group
    gem = group.calcs_active_gem if group else None
    return gem.name_en.strip() if gem else ""


def _metric_value(build: PobBuild, fields: tuple[str, ...]) -> tuple[str, float | None]:
    for field in fields:
        value = build.player_stats.get(field)
        if value is not None and math.isfinite(value):
            return field, float(value)
    return "", None


def _item_by_slot(build: PobBuild) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in build.items:
        slot = item.slot.strip()
        if not slot:
            continue
        # Compare the complete user-supplied item text so a roll/mod change on the
        # same named item still marks the slot.  The raw text is never returned.
        out[slot] = item.raw.strip()
    return out


def _enabled_gems(build: PobBuild) -> Counter[str]:
    def descriptor(gem) -> str:  # noqa: ANN001
        details = []
        if gem.level is not None:
            details.append(f"L{gem.level}")
        if gem.quality is not None:
            details.append(f"Q{gem.quality}")
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{gem.name_en}{suffix}"

    return Counter(
        descriptor(gem)
        for group in build.skill_groups if group.enabled
        for gem in group.gems if gem.enabled and gem.name_en
    )


def _expanded_counter_difference(
    left: Counter[str], right: Counter[str]
) -> tuple[str, ...]:
    return tuple(sorted((left - right).elements(), key=str.casefold))


def _structure_diff(
    reference_label: str,
    reference: PobBuild,
    label: str,
    candidate: PobBuild,
) -> ScenarioStructureDiff:
    if label == reference_label:
        return ScenarioStructureDiff(label, "reference", (), (), (), (), (), "")
    reference_items = _item_by_slot(reference)
    candidate_items = _item_by_slot(candidate)
    changed_slots = tuple(sorted(
        slot for slot in set(reference_items) | set(candidate_items)
        if reference_items.get(slot) != candidate_items.get(slot)
    ))
    reference_gems = _enabled_gems(reference)
    candidate_gems = _enabled_gems(candidate)
    added_gems = _expanded_counter_difference(candidate_gems, reference_gems)
    removed_gems = _expanded_counter_difference(reference_gems, candidate_gems)
    reference_nodes = set(reference.passive_nodes)
    candidate_nodes = set(candidate.passive_nodes)
    status = (
        "ready" if reference.structure_complete and candidate.structure_complete
        else "partial"
    )
    note = (
        "只比较输入中实际携带的结构；至少一份方案不是完整 Build 快照，未出现的对象不能解释为移除。"
        if status == "partial" else
        "结构差异来自两份 PoB 输入；它只说明对象变化，不证明任何 PlayerStat 差值由该对象造成。"
    )
    return ScenarioStructureDiff(
        label=label,
        status=status,
        changed_item_slots=changed_slots,
        added_gems=added_gems,
        removed_gems=removed_gems,
        passive_nodes_added=tuple(sorted(candidate_nodes - reference_nodes)),
        passive_nodes_removed=tuple(sorted(reference_nodes - candidate_nodes)),
        note=note,
    )


def _context_check(
    reference_label: str,
    reference: PobBuild,
    label: str,
    candidate: PobBuild,
) -> ScenarioContextCheck:
    if label == reference_label:
        return ScenarioContextCheck(label, "reference", (), ())

    mismatches: list[str] = []
    unknowns: list[str] = []
    reference_skill = _active_skill(reference)
    candidate_skill = _active_skill(candidate)
    if not reference_skill or not candidate_skill:
        unknowns.append("至少一份方案没有可确认的 Calcs 当前技能")
    elif reference_skill.casefold() != candidate_skill.casefold():
        mismatches.append(
            f"Calcs 当前技能不同：{reference_skill} vs {candidate_skill}"
        )

    if not reference.tree_version or not candidate.tree_version:
        unknowns.append("至少一份方案缺少当前激活天赋树版本")
    elif reference.tree_version != candidate.tree_version:
        mismatches.append(
            f"天赋树版本不同：{reference.tree_version} vs {candidate.tree_version}"
        )

    if not reference.config or not candidate.config:
        unknowns.append("至少一份方案缺少可确认的 PoB 配置快照")
    config_keys = sorted(set(reference.config) | set(candidate.config))
    differing = tuple(
        key for key in config_keys
        if (
            (key in reference.config) != (key in candidate.config)
            or reference.config.get(key) != candidate.config.get(key)
        )
    )
    if differing:
        mismatches.append(
            "PoB 配置快照不同：" + "、".join(differing)
        )

    if mismatches:
        status = "mismatch"
        reasons = (*mismatches, *unknowns)
    elif unknowns:
        status = "unknown"
        reasons = tuple(unknowns)
    else:
        status = "ready"
        reasons = ()
    return ScenarioContextCheck(label, status, tuple(reasons), differing)


def compare_build_scenarios(
    scenarios: list[tuple[str, PobBuild]],
    *,
    metrics: list[str] | None = None,
    reference_label: str = "",
) -> BuildScenarioComparison:
    """Compare each scenario with one explicit reference under strict context checks."""
    if not 2 <= len(scenarios) <= MAX_SCENARIOS:
        raise ValueError(f"Build 方案数量必须在 2..{MAX_SCENARIOS} 之间")
    labels = [label.strip() for label, _ in scenarios]
    if any(not label for label in labels):
        raise ValueError("每个 Build 方案都必须有非空 label")
    if len(set(labels)) != len(labels):
        raise ValueError("Build 方案 label 不能重复")

    selected_metrics = list(GROWTH_METRICS if metrics is None else metrics)
    if not selected_metrics:
        raise ValueError("metrics 不能为空")
    if len(set(selected_metrics)) != len(selected_metrics):
        raise ValueError("同一个比较指标不能重复")
    unknown_metric_names = [
        metric for metric in selected_metrics if metric not in GROWTH_METRICS
    ]
    if unknown_metric_names:
        raise ValueError("不支持的比较指标：" + "、".join(unknown_metric_names))

    selected_reference = reference_label.strip() or labels[0]
    if selected_reference not in labels:
        raise ValueError(f"reference_label 不在方案中：{selected_reference}")
    by_label = {
        normalized_label: scenarios[index][1]
        for index, normalized_label in enumerate(labels)
    }
    reference = by_label[selected_reference]

    snapshots = tuple(
        ScenarioSnapshot(
            label=label,
            level=build.level,
            class_name=build.class_name,
            ascend_class_name=build.ascend_class_name,
            active_skill=_active_skill(build),
            enemy_context=str(build.config.get("enemyIsBoss") or ""),
            tree_version=build.tree_version,
            player_stat_count=len(build.player_stats),
        )
        for label, build in by_label.items()
    )
    context_checks = tuple(
        _context_check(selected_reference, reference, label, build)
        for label, build in by_label.items()
    )
    context_by_label = {item.label: item for item in context_checks}
    structure_diffs = tuple(
        _structure_diff(selected_reference, reference, label, build)
        for label, build in by_label.items()
    )

    comparisons: list[MetricComparison] = []
    for metric in selected_metrics:
        spec = GROWTH_METRICS[metric]
        reference_field, reference_value = _metric_value(reference, spec.fields)
        values: list[ScenarioMetricValue] = []
        candidate_ready = 0
        candidate_total = len(scenarios) - 1
        for label, build in by_label.items():
            field, value = _metric_value(build, spec.fields)
            if label == selected_reference:
                if value is None:
                    values.append(ScenarioMetricValue(
                        label, "unknown", None, "", None, None, "unknown",
                        "基线 PlayerStat 缺少该指标的受支持字段",
                    ))
                else:
                    values.append(ScenarioMetricValue(
                        label, "reference", value, field, 0.0, 0.0, "baseline", "",
                    ))
                continue

            context = context_by_label[label]
            reason = ""
            if context.status != "ready":
                reason = "；".join(context.reasons) or "方案上下文不可比较"
            elif reference_value is None:
                reason = "基线 PlayerStat 缺少该指标的受支持字段"
            elif value is None:
                reason = "候选 PlayerStat 缺少该指标的受支持字段"
            elif field != reference_field:
                reason = (
                    f"PlayerStat 字段口径不同：{reference_field or '缺失'} vs "
                    f"{field or '缺失'}"
                )

            if reason:
                values.append(ScenarioMetricValue(
                    label, "unknown", value, field, None, None, "unknown", reason,
                ))
                continue

            delta_decimal = Decimal(str(value)) - Decimal(str(reference_value))
            delta = float(delta_decimal)
            percent = (
                float(delta_decimal / abs(Decimal(str(reference_value))) * Decimal("100"))
                if reference_value != 0 else None
            )
            direction = "up" if delta > 0 else "down" if delta < 0 else "equal"
            values.append(ScenarioMetricValue(
                label, "ready", value, field, delta, percent, direction,
                "" if percent is not None else "基线值为 0，百分比变化不可计算",
            ))
            candidate_ready += 1

        status = (
            "ready" if candidate_ready == candidate_total
            else "partial" if candidate_ready
            else "unknown"
        )
        comparisons.append(MetricComparison(
            metric=metric,
            label=spec.label,
            unit=spec.unit,
            status=status,
            values=tuple(values),
        ))

    ready_count = sum(item.status == "ready" for item in comparisons)
    partial_count = sum(item.status == "partial" for item in comparisons)
    unknown_count = sum(item.status == "unknown" for item in comparisons)
    status = (
        "ready" if ready_count == len(comparisons)
        else "partial" if ready_count or partial_count
        else "unknown"
    )
    context_states = {item.status for item in context_checks if item.status != "reference"}
    context_status = (
        "ready" if context_states == {"ready"}
        else "unknown" if context_states and context_states <= {"unknown"}
        else "mismatch" if "mismatch" in context_states
        else "unknown"
    )
    return BuildScenarioComparison(
        status=status,
        reference_label=selected_reference,
        scenario_count=len(scenarios),
        context_status=context_status,
        ready_metrics=ready_count,
        partial_metrics=partial_count,
        unknown_metrics=unknown_count,
        scenarios=snapshots,
        context_checks=context_checks,
        structure_diffs=structure_diffs,
        metrics=tuple(comparisons),
        overall_verdict="not_ranked",
        note=(
            "只比较各 PoB 文件已经保存的 PlayerStat，不重算 Build。只有 Calcs 当前技能、"
            "完整配置、天赋树版本和具体 PlayerStat 字段口径一致时才计算差值；各指标单位"
            "不同且收益取舍由用户决定，因此不生成总体分数、总体最佳、装备建议或价格结论。"
        ),
    )
