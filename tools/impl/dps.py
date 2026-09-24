"""显式输入的单技能命中 DPS 工具。"""

from __future__ import annotations

from typing import Any

from calc import constants
from calc import dps as dps_calc
from calc.derive import fmt, pob_url
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

BOUNDARY = (
    "只计算一次击中的确定性主链：基础/附加伤害、增伤/更多、PoB 顺序的伤害转化与额外获得、"
    "敌方抗性/穿透或物理减伤、暴击、命中、频率及技能/数量倍率。不含幸运伤害、双倍/三倍伤害、"
    "压倒物理减伤、抗性反转/忽略、重复技、触发冷却、穿刺、异常与持续伤害；"
    "缺少这些机制时不能称为完整 Build DPS"
)


def _url(lines: tuple[int, int]) -> str:
    return pob_url(constants.provenance(constants.BASE_CRIT_MULTIPLIER).url,
                   dps_calc.OFFENCE_FILE, lines=lines)


def _render(result: dps_calc.HitDpsResult) -> str:
    lines = ["### 单技能命中 DPS", "", "| 最终类型 | 非暴击区间 | 有效抗性/减伤 | 结算后区间 |", "| --- | ---: | ---: | ---: |"]
    for item in result.by_type:
        lines.append(
            f"| {dps_calc.TYPE_ZH[item.damage_type]} | {fmt(item.before_mitigation.minimum)}–"
            f"{fmt(item.before_mitigation.maximum)} | {item.effective_resistance:g}% | "
            f"{fmt(item.after_mitigation.minimum)}–{fmt(item.after_mitigation.maximum)} |"
        )
    lines.extend([
        "", f"- 结算后非暴击总区间：{fmt(result.non_crit_hit.minimum)}–{fmt(result.non_crit_hit.maximum)}",
        f"- 暴击期望倍率：{result.crit_effect:.4f}（{result.crit_chance_pct:g}% 暴击率，{result.crit_multiplier_pct:g}% 暴击加成）",
        f"- 平均每次击中：{fmt(result.average_hit)}",
        f"- 计入 {result.hit_chance_pct:g}% 命中率：每次使用期望 {fmt(result.average_damage_per_use)}",
        f"- 每秒 {result.action_rate:g} 次 × 技能倍率 {result.skill_dps_multiplier:g} × 数量倍率 {result.quantity_multiplier:g}",
        "", f"**命中 DPS：{fmt(result.dps)}**", "", BOUNDARY + "。", "", "公式来源：",
        f"- 基础与附加伤害：{_url(dps_calc.BASE_DAMAGE_LINES)}",
        f"- 伤害类型加成与转化递归：{_url(dps_calc.DAMAGE_LINES)}",
        f"- 转化 100% 缩放及技能转化优先：{_url(dps_calc.CONVERSION_LINES)}",
        f"- 抗性、穿透与承伤倍率：{_url(dps_calc.ENEMY_EFFECT_LINES)}",
        f"- 平均击中、命中率与最终 DPS：{_url(dps_calc.FINAL_DPS_LINES)}",
    ])
    return "\n".join(lines)


@REGISTRY.register(
    name="hit_dps_calc",
    description=(
        "用明确输入复算单技能命中 DPS：基础/附加伤害、伤害有效度、增伤/更多、转化/额外获得、"
        "敌方抗性/穿透、暴击、命中、频率和数量倍率。不要从装备描述猜遗漏参数，"
        "也不要把结果称为完整 Build DPS。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "base_damage": {"type": "object", "description": "各伤害类型的 [最小,最大]，键支持中英文"},
            "added_damage": {"type": "object"},
            "base_multiplier": {"type": "number", "default": 1},
            "damage_effectiveness": {"type": "number", "default": 1},
            "added_damage_multiplier": {"type": "number", "default": 1},
            "increased_damage_pct": {"type": "number", "default": 0},
            "increased_by_type": {"type": "object"},
            "more_damage_multipliers": {"type": "array", "items": {"type": "number"}},
            "more_by_type": {"type": "object"},
            "conversions": {"type": "array", "items": {"type": "object", "properties": {
                "source": {"type": "string"}, "target": {"type": "string"},
                "percent": {"type": "number"},
                "kind": {"type": "string", "enum": ["global", "skill", "gain"]},
            }, "required": ["source", "target", "percent"]}},
            "enemy_resistance": {"type": "object"}, "penetration": {"type": "object"},
            "enemy_physical_reduction_pct": {"type": "number", "default": 0},
            "enemy_damage_taken_increased_pct": {"type": "number", "default": 0},
            "enemy_damage_taken_more_multiplier": {"type": "number", "default": 1},
            "hit_chance_pct": {"type": "number", "default": 100},
            "crit_chance_pct": {"type": "number", "default": 0},
            "crit_multiplier_pct": {"type": "number", "default": 150},
            "action_rate": {"type": "number"},
            "skill_dps_multiplier": {"type": "number", "default": 1},
            "quantity_multiplier": {"type": "number", "default": 1},
        },
        "required": ["base_damage", "action_rate"],
    },
)
def hit_dps_calc(
    base_damage: dict[str, list[float]] | None = None, action_rate: float | None = None,
    added_damage: dict[str, list[float]] | None = None, base_multiplier: float = 1.0,
    damage_effectiveness: float = 1.0, added_damage_multiplier: float = 1.0,
    increased_damage_pct: float = 0.0, increased_by_type: dict[str, float] | None = None,
    more_damage_multipliers: list[float] | None = None, more_by_type: dict[str, float] | None = None,
    conversions: list[dict[str, Any]] | None = None,
    enemy_resistance: dict[str, float] | None = None, penetration: dict[str, float] | None = None,
    enemy_physical_reduction_pct: float = 0.0, enemy_damage_taken_increased_pct: float = 0.0,
    enemy_damage_taken_more_multiplier: float = 1.0, hit_chance_pct: float = 100.0,
    crit_chance_pct: float = 0.0, crit_multiplier_pct: float | None = None,
    skill_dps_multiplier: float = 1.0, quantity_multiplier: float = 1.0,
) -> ToolResult:
    try:
        if base_damage is None or action_rate is None:
            missing = []
            if base_damage is None:
                missing.append("各伤害类型的基础区间 base_damage")
            if action_rate is None:
                missing.append("每秒攻击/施法次数 action_rate")
            answer = "无法计算命中 DPS：还缺" + "、".join(missing) + "。不会从‘这套配置’猜这些输入。\n\n" + BOUNDARY + "。"
            return ToolResult(ok=True, data={
                "found": False, "kind": "missing_inputs", "missing_inputs": missing,
                "reason": "DPS 必须由明确伤害区间与频率计算，不能从‘这套配置’猜输入",
                "boundary": BOUNDARY, "answer": answer,
            })
        base_crit_multi = constants.get(constants.BASE_CRIT_MULTIPLIER)
        result = dps_calc.hit_dps(
            base_damage=base_damage, added_damage=added_damage, base_multiplier=base_multiplier,
            damage_effectiveness=damage_effectiveness, added_damage_multiplier=added_damage_multiplier,
            increased_damage_pct=increased_damage_pct, increased_by_type=increased_by_type,
            more_damage_multipliers=more_damage_multipliers or (), more_by_type=more_by_type,
            conversions=[dps_calc.Conversion(**item) for item in (conversions or [])],
            enemy_resistance=enemy_resistance, penetration=penetration,
            enemy_physical_reduction_pct=enemy_physical_reduction_pct,
            enemy_damage_taken_increased_pct=enemy_damage_taken_increased_pct,
            enemy_damage_taken_more_multiplier=enemy_damage_taken_more_multiplier,
            hit_chance_pct=hit_chance_pct, crit_chance_pct=crit_chance_pct,
            crit_multiplier_pct=crit_multiplier_pct if crit_multiplier_pct is not None else base_crit_multi,
            action_rate=action_rate, skill_dps_multiplier=skill_dps_multiplier,
            quantity_multiplier=quantity_multiplier,
        )
    except (ValueError, TypeError, constants.MissingConstant) as exc:
        return ToolResult(ok=False, error=str(exc))
    answer = _render(result)
    data = result.as_dict()
    data.update({"found": True, "kind": "result", "boundary": BOUNDARY, "answer": answer})
    provenance = constants.provenance(constants.BASE_CRIT_MULTIPLIER)
    citation = {
        "chunk_id": "hit_dps:explicit-input-pipeline", "entity_id": None,
        "title": "单技能命中 DPS 逐步推导", "url": _url(dps_calc.FINAL_DPS_LINES),
        "source": provenance.source, "source_priority": int(provenance.source_priority),
        "can_support_facts": int(provenance.source_priority) <= MAX_FACTUAL_PRIORITY,
        "text": answer, "channel": "derived",
    }
    return ToolResult(ok=True, data=data, citations=[citation], meta={"channel": "derived"})
