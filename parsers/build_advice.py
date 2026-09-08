"""Build 短板与双档建议（需求 8.7）。

建议只由明确的 PlayerStat 短板触发，预期收益用同一份快照可复核的差额表达。
项目尚未接入赛季经济价格，因此成本只给改造规模（单一词缀/单件装备/多部位重构），
并明确说明没有通货报价；绝不编一个 chaos/divine 区间冒充市场数据。
"""

from __future__ import annotations

from dataclasses import dataclass

from parsers.pob_build import PobBuild


@dataclass(frozen=True)
class BuildSuggestion:
    weakness: str
    tier: str
    action: str
    expected_gain: str
    estimated_cost: str
    assumptions: str


def _fmt(value: float) -> str:
    return f"{value:g}"


def _resist_gain(current: float, target: float) -> str:
    before = 1 - current / 100
    after = 1 - target / 100
    relative = (1 - after / before) * 100 if before > 0 else 0
    return (
        f"有效抗性 {_fmt(current)}% → {_fmt(target)}%（+{_fmt(target - current)} 个百分点）；"
        f"在无穿透、无额外减抗假设下，该类型承受倍率降低约 {_fmt(relative)}%"
    )


def _elemental_resistance(build: PobBuild) -> list[BuildSuggestion]:
    labels = (("火焰", "FireResist"), ("冰霜", "ColdResist"), ("闪电", "LightningResist"))
    gaps = [(label, build.player_stats[key]) for label, key in labels
            if key in build.player_stats and build.player_stats[key] < 75]
    if not gaps:
        return []
    weakness = "元素抗性未满：" + "、".join(f"{label} {_fmt(value)}%" for label, value in gaps)
    gain = "；".join(_resist_gain(value, 75) for _label, value in gaps)
    needed = "、".join(f"{label}至少 +{_fmt(75 - value)}%" for label, value in gaps)
    return [
        BuildSuggestion(
            weakness=weakness, tier="低预算",
            action=f"寻找一处可调整的装备后缀或工艺位，合计补足 {needed}",
            expected_gain=gain,
            estimated_cost="低：一次工艺位或单件稀有装备调整；未接赛季经济数据，无法给通货价",
            assumptions="假设调整后其它关键属性不下降，目标为常规 75% 上限",
        ),
        BuildSuggestion(
            weakness=weakness, tier="高预算",
            action=f"联动重构多个装备后缀，在补足 {needed} 后再预留约 20% 超抗缓冲",
            expected_gain=gain + "；额外超抗用于抵消减抗/诅咒，不抵御穿透",
            estimated_cost="高：多件装备联动更换；未接赛季经济数据，无法给通货价",
            assumptions="20% 超抗是本建议的规划目标，不是游戏硬门槛；其它关键属性保持不变",
        ),
    ]


def _chaos_resistance(build: PobBuild) -> list[BuildSuggestion]:
    if "ChaosResist" not in build.player_stats:
        return []
    current = build.player_stats["ChaosResist"]
    if current >= 75:
        return []
    low_target = min(75.0, max(0.0, current + 30))
    return [
        BuildSuggestion(
            weakness=f"混沌抗性 {_fmt(current)}%，低于常规上限 75%",
            tier="低预算",
            action=f"先把混沌抗性补到 {_fmt(low_target)}%",
            expected_gain=_resist_gain(current, low_target),
            estimated_cost="低：优先单条混沌抗性词缀或工艺位；未接赛季经济数据，无法给通货价",
            assumptions="阶段目标最多提升 30 个百分点；无穿透、无额外减抗，且其它属性不下降",
        ),
        BuildSuggestion(
            weakness=f"混沌抗性 {_fmt(current)}%，低于常规上限 75%",
            tier="高预算",
            action="通过多个装备后缀与天赋联动，把混沌抗性补到 75%",
            expected_gain=_resist_gain(current, 75),
            estimated_cost="高：通常需要多部位联动重构；未接赛季经济数据，无法给通货价",
            assumptions="目标为常规 75% 上限；无穿透、无额外减抗，且其它属性不下降",
        ),
    ]


def _suppression(build: PobBuild) -> list[BuildSuggestion]:
    key = next((name for name in (
        "EffectiveSpellSuppressionChance", "SpellSuppressionChance"
    ) if name in build.player_stats), None)
    if key is None or build.player_stats[key] >= 100:
        return []
    current = build.player_stats[key]
    gap = 100 - current
    weakness = f"法术压制 {_fmt(current)}%，距 100% 上限差 {_fmt(gap)} 个百分点"
    common = dict(
        weakness=weakness,
        expected_gain=f"压制触发率 {_fmt(current)}% → 100%（+{_fmt(gap)} 个百分点）",
        assumptions="只比较触发率；压制效果取决于 build 的其它词缀，本建议不假定固定减伤比例",
    )
    return [
        BuildSuggestion(
            tier="低预算", action="优先用现有天赋重配或一处可用装备词缀补足缺口",
            estimated_cost="低：天赋重配或单一词缀调整；未接赛季经济数据，无法给通货价",
            **common,
        ),
        BuildSuggestion(
            tier="高预算", action="重构带法术压制的装备组合，在保持其它关键属性下补到 100%",
            estimated_cost="高：多件装备联动更换；未接赛季经济数据，无法给通货价",
            **common,
        ),
    ]


def build_suggestions(build: PobBuild) -> list[BuildSuggestion]:
    """只对已知且低于明确上限的指标给建议；未知项不猜短板。"""
    return [
        *_elemental_resistance(build),
        *_chaos_resistance(build),
        *_suppression(build),
    ]
