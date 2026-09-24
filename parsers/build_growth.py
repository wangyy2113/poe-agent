"""基于用户显式数值目标的 Build 成长里程碑。

本模块只比较同一份 Build 输入里的 ``PlayerStat`` 与用户给出的目标值。它不从职业、
装备文本或历史经验推断目标，也不反推具体装备、工艺、价格或 DPS 加成链。缺少当前值时
保留 unknown；这样规划不会把“想达到什么”偷换成“怎样一定能达到”。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from parsers.pob_build import PobBuild


@dataclass(frozen=True)
class GrowthMetricSpec:
    label: str
    fields: tuple[str, ...]
    unit: str = ""
    allow_negative_target: bool = False


GROWTH_METRICS: dict[str, GrowthMetricSpec] = {
    "life": GrowthMetricSpec("可用生命", ("LifeUnreserved", "Life")),
    "energy_shield": GrowthMetricSpec("能量护盾", ("EnergyShield",)),
    "fire_resistance": GrowthMetricSpec(
        "火焰抗性", ("FireResist",), "%", allow_negative_target=True
    ),
    "cold_resistance": GrowthMetricSpec(
        "冰霜抗性", ("ColdResist",), "%", allow_negative_target=True
    ),
    "lightning_resistance": GrowthMetricSpec(
        "闪电抗性", ("LightningResist",), "%", allow_negative_target=True
    ),
    "chaos_resistance": GrowthMetricSpec(
        "混沌抗性", ("ChaosResist",), "%", allow_negative_target=True
    ),
    "spell_suppression": GrowthMetricSpec(
        "法术压制", ("EffectiveSpellSuppressionChance", "SpellSuppressionChance"), "%"
    ),
    "attack_block": GrowthMetricSpec(
        "攻击格挡", ("EffectiveBlockChance", "BlockChance"), "%"
    ),
    "spell_block": GrowthMetricSpec(
        "法术格挡", ("EffectiveSpellBlockChance", "SpellBlockChance"), "%"
    ),
    "armour": GrowthMetricSpec("护甲", ("Armour",)),
    "evasion": GrowthMetricSpec("闪避值", ("Evasion",)),
    "life_recovery": GrowthMetricSpec(
        "生命净回复", ("NetLifeRegen", "LifeRegenRecovery", "LifeRegen"), "/秒"
    ),
    "dps": GrowthMetricSpec(
        "当前技能 DPS", ("FullDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS"), " DPS"
    ),
    "physical_max_hit": GrowthMetricSpec(
        "物理 Max Hit", ("PhysicalMaximumHitTaken",)
    ),
    "fire_max_hit": GrowthMetricSpec("火焰 Max Hit", ("FireMaximumHitTaken",)),
    "cold_max_hit": GrowthMetricSpec("冰霜 Max Hit", ("ColdMaximumHitTaken",)),
    "lightning_max_hit": GrowthMetricSpec(
        "闪电 Max Hit", ("LightningMaximumHitTaken",)
    ),
    "chaos_max_hit": GrowthMetricSpec("混沌 Max Hit", ("ChaosMaximumHitTaken",)),
}

MAX_GROWTH_OBJECTIVES = 20
MAX_ABS_TARGET = 1_000_000_000_000_000.0


@dataclass(frozen=True)
class GrowthObjective:
    metric: str
    target: float
    priority: int = 50
    label: str = ""


@dataclass(frozen=True)
class GrowthMilestone:
    sequence: int
    metric: str
    label: str
    priority: int
    status: str
    current: float | None
    target: float
    gap: float | None
    unit: str
    source_field: str
    current_source: str
    target_source: str
    action: str
    expected_gain: str
    verification: str
    assumptions: str


@dataclass(frozen=True)
class BuildGrowthPlan:
    status: str
    total: int
    met: int
    remaining: int
    unknown: int
    completion_percent: float
    next_metric: str
    milestones: tuple[GrowthMilestone, ...]
    note: str


def validate_objectives(objectives: list[GrowthObjective]) -> None:
    if not 1 <= len(objectives) <= MAX_GROWTH_OBJECTIVES:
        raise ValueError(f"成长目标数量必须在 1..{MAX_GROWTH_OBJECTIVES} 之间")
    metrics = [item.metric for item in objectives]
    if len(set(metrics)) != len(metrics):
        raise ValueError("同一个成长指标不能重复")
    for item in objectives:
        spec = GROWTH_METRICS.get(item.metric)
        if spec is None:
            raise ValueError(f"不支持的成长指标：{item.metric}")
        if (
            isinstance(item.target, bool)
            or not isinstance(item.target, (int, float))
            or not math.isfinite(item.target)
        ):
            raise ValueError("成长目标必须是有限数字")
        if abs(item.target) > MAX_ABS_TARGET:
            raise ValueError("成长目标超出安全数值范围")
        if item.target < 0 and not spec.allow_negative_target:
            raise ValueError(f"{item.metric} 的目标不能为负数")
        if isinstance(item.priority, bool) or not isinstance(item.priority, int):
            raise ValueError("优先级必须是整数")
        if not 1 <= item.priority <= 100:
            raise ValueError("优先级必须在 1..100 之间，数字越小越优先")


def _fmt(value: float) -> str:
    return f"{value:g}"


def _current(build: PobBuild, spec: GrowthMetricSpec) -> tuple[str, float | None]:
    field = next((name for name in spec.fields if name in build.player_stats), "")
    return field, build.player_stats.get(field) if field else None


def _verification(spec: GrowthMetricSpec, field: str) -> str:
    fields = field or " / ".join(spec.fields)
    return (
        "完成一次调整后，用相同技能、敌人档位和配置重新计算或转录 Build，"
        f"再检查 `{fields}`；其它已达目标也必须重新核对。"
    )


def plan_build_growth(
    build: PobBuild, objectives: list[GrowthObjective]
) -> BuildGrowthPlan:
    """按用户优先级生成可复核里程碑；只做当前值与目标值的确定性差额。"""
    validate_objectives(objectives)
    ordered = sorted(enumerate(objectives), key=lambda pair: (pair[1].priority, pair[0]))
    current_source = build.player_stats_basis or "PoB `<PlayerStat>` 快照"
    milestones: list[GrowthMilestone] = []
    for sequence, (_original_index, objective) in enumerate(ordered, start=1):
        spec = GROWTH_METRICS[objective.metric]
        source_field, current = _current(build, spec)
        label = objective.label.strip() or spec.label
        if current is None:
            status = "unknown"
            gap = None
            action = (
                f"先补充能明确给出 {' / '.join(f'`{name}`' for name in spec.fields)} "
                f"的 Build 快照，再判断距用户目标 {_fmt(objective.target)}{spec.unit} 多远。"
            )
            expected_gain = ""
        elif current >= objective.target:
            status = "met"
            gap = 0.0
            action = (
                f"当前 {_fmt(current)}{spec.unit} 已达到用户目标 "
                f"{_fmt(objective.target)}{spec.unit}；后续调整时保持不低于该目标。"
            )
            expected_gain = "无需提升"
        else:
            status = "gap"
            gap = objective.target - current
            action = (
                f"把{label}从 {_fmt(current)}{spec.unit} 提升到用户目标 "
                f"{_fmt(objective.target)}{spec.unit}；本规划不猜具体装备或工艺。"
            )
            expected_gain = (
                f"{_fmt(current)}{spec.unit} → {_fmt(objective.target)}{spec.unit}"
                f"（至少 +{_fmt(gap)}{spec.unit}）"
            )
        milestones.append(GrowthMilestone(
            sequence=sequence,
            metric=objective.metric,
            label=label,
            priority=objective.priority,
            status=status,
            current=current,
            target=objective.target,
            gap=gap,
            unit=spec.unit,
            source_field=source_field,
            current_source=current_source,
            target_source="用户显式提交的成长目标",
            action=action,
            expected_gain=expected_gain,
            verification=_verification(spec, source_field),
            assumptions=(
                "只比较同一口径的快照字段；目标的游戏可达性、具体改装路径、机会成本和"
                "市场价格均未评估。"
            ),
        ))

    met = sum(item.status == "met" for item in milestones)
    remaining = sum(item.status == "gap" for item in milestones)
    unknown = sum(item.status == "unknown" for item in milestones)
    if unknown == len(milestones):
        status = "unknown"
    elif unknown:
        status = "partial"
    elif remaining:
        status = "ready"
    else:
        status = "complete"
    next_metric = next(
        (item.metric for item in milestones if item.status == "gap"), ""
    )
    return BuildGrowthPlan(
        status=status,
        total=len(milestones),
        met=met,
        remaining=remaining,
        unknown=unknown,
        completion_percent=round(met / len(milestones) * 100, 2),
        next_metric=next_metric,
        milestones=tuple(milestones),
        note=(
            "这是用户目标与当前输入快照的差额计划，不是自动配装器。顺序只来自用户优先级；"
            "没有依据时不推荐具体装备、天赋、工艺或预算，也不把 PoB 数值冒充本库重算结果。"
        ),
    )
