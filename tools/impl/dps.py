"""显式输入的单技能命中 DPS 工具。"""

from __future__ import annotations

from typing import Any

from calc import constants
from calc import dps as dps_calc
from calc.dps_analysis import compare_hit_dps, target_action_rate
from calc.derive import fmt, pob_url
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

BOUNDARY = (
    "只计算单技能确定性主链与一次爆发：基础/附加伤害、增伤/更多、PoB 顺序的伤害转化与额外获得、"
    "敌方抗性/穿透/忽略/反转或物理减伤/压倒、幸运伤害、双倍/三倍伤害、暴击、命中、"
    "触发/非触发冷却与服务器 tick、无情一击、战争之拳/战吼覆盖、稳态穿刺、终结等效输出、"
    "频率、重复爆发及技能/数量倍率；可选联算显式异常源伤的点燃、中毒和流血稳态 DPS。"
    "不含护甲对穿刺的动态减伤、起手期异常层数、非异常持续伤害或完整 Build 自动汇总；"
    "缺少这些机制时不能称为完整 Build DPS"
)


def _url(lines: tuple[int, int]) -> str:
    return pob_url(constants.provenance(constants.BASE_CRIT_MULTIPLIER).url,
                   dps_calc.OFFENCE_FILE, lines=lines)


def _citation(answer: str, *, slug: str, title: str) -> dict[str, Any]:
    provenance = constants.provenance(constants.BASE_CRIT_MULTIPLIER)
    return {
        "chunk_id": f"hit_dps:{slug}", "entity_id": None,
        "title": title, "url": _url(dps_calc.FINAL_DPS_LINES),
        "source": provenance.source, "source_priority": int(provenance.source_priority),
        "can_support_facts": int(provenance.source_priority) <= MAX_FACTUAL_PRIORITY,
        "text": answer, "channel": "derived",
    }


def _with_default_crit(inputs: dict[str, Any]) -> dict[str, Any]:
    values = dict(inputs)
    if values.get("crit_multiplier_pct") is None:
        values["crit_multiplier_pct"] = constants.get(constants.BASE_CRIT_MULTIPLIER)
    return values


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
        f"- 伤害区间 roll：非暴击 {result.damage_roll_chance_pct:g}% / "
        f"暴击 {result.crit_damage_roll_chance_pct:g}%"
        f"（{'特别幸运' if result.extreme_luck else '普通幸运/不幸'}口径）",
        f"- 双倍/三倍伤害期望倍率：{result.scaled_damage_effect:.4f}"
        f"（有效几率 {result.double_damage_chance_pct:g}% / {result.triple_damage_chance_pct:g}%）",
        f"- 战斗机制期望倍率：{result.combat_damage_effect:.4f}"
        f"（无情 {result.ruthless_blow_effect:.4f}、战争之拳 {result.fist_of_war_effect:.4f}、"
        f"战吼覆盖 {result.exerted_attack_effect:.4f}）",
        f"- 平均每次击中：{fmt(result.average_hit)}",
        f"- 计入 {result.hit_chance_pct:g}% 命中率：每次使用期望 {fmt(result.average_damage_per_use)}",
        f"- 原始每秒 {result.raw_action_rate:g} 次 → 有效 {result.action_rate:g} 次"
        f"（输入冷却 {result.cooldown_seconds:g} 秒，有效冷却 {result.effective_cooldown_seconds:g} 秒，"
        f"{'触发' if result.triggered else '非触发'}口径）",
        f"- 服务器 tick：{'已夹值' if result.action_rate_tick_capped else '未夹值'}"
        f"（重复 {result.action_repeats} 次，{'引导' if result.channelled else '非引导'}技能）",
        f"- 技能倍率 {result.skill_dps_multiplier:g} × 数量倍率 {result.quantity_multiplier:g}",
        f"- 一轮爆发：{result.burst_hit_count:g} 击、重复伤害倍率 {result.repeat_damage_multiplier:g}"
        f" → {fmt(result.average_burst_damage)}",
        f"- 稳态穿刺 DPS：{fmt(result.impale_dps)}；命中与穿刺合计：{fmt(result.combined_dps)}",
    ])
    if result.damaging_ailments:
        lines.extend([
            "", "| 伤害性异常 | 有效施加率 | 持续时间 | 稳态层数 | 强实例 roll | 单实例 DPS | 稳态 DPS |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        for item in result.damaging_ailments:
            limit = "∞" if item.stack_limit is None else fmt(item.stack_limit)
            lines.append(
                f"| {item.name} | {item.effective_chance_pct:.2f}% | {item.duration_seconds:.2f}s | "
                f"{fmt(item.active_stacks)} / {limit} | {item.average_roll_pct:.2f}% | "
                f"{fmt(item.single_instance_dps)} | {fmt(item.dps)}{'（封顶）' if item.capped else ''} |"
            )
        lines.extend([
            "", f"- 伤害性异常合计：{fmt(result.ailment_dps)}",
            f"- 命中、穿刺与异常总 DPS：{fmt(result.total_dps)}",
        ])
    lines.extend([
        f"- 有效终结：{result.cull_percent:.2f}%（×{result.cull_multiplier:.4f}）"
        f" → 含终结等效 DPS {fmt(result.effective_dps_with_cull)}",
        f"- 含异常与终结的总等效 DPS：{fmt(result.effective_total_dps_with_cull)}",
        "", f"**命中 DPS：{fmt(result.dps)}**", "", BOUNDARY + "。", "", "公式来源：",
        f"- 基础与附加伤害：{_url(dps_calc.BASE_DAMAGE_LINES)}",
        f"- 伤害类型加成与转化递归：{_url(dps_calc.DAMAGE_LINES)}",
        f"- 转化 100% 缩放及技能转化优先：{_url(dps_calc.CONVERSION_LINES)}",
        f"- 触发/非触发冷却频率：{_url(dps_calc.RATE_LINES)}",
        f"- 冷却服务器 tick 对齐：{_url(dps_calc.COOLDOWN_TICK_LINES)}",
        f"- 无情一击与战争之拳期望：{_url(dps_calc.COMBAT_EFFECT_LINES)}",
        f"- 双倍/三倍伤害重叠：{_url(dps_calc.SCALED_DAMAGE_LINES)}",
        f"- 终结等效倍率：{_url(dps_calc.CULL_LINES)}",
        f"- 幸运/不幸伤害期望：{_url(dps_calc.ROLL_LINES)}",
        f"- 抗性、穿透与承伤倍率：{_url(dps_calc.ENEMY_EFFECT_LINES)}",
        f"- 平均击中、命中率、爆发与最终 DPS：{_url(dps_calc.BURST_LINES)}",
        f"- 穿刺稳态倍率：{_url(dps_calc.IMPALE_LINES)}",
        f"- 穿刺 DPS：{_url(dps_calc.IMPALE_DPS_LINES)}",
        f"- 异常施加率与暴击加权：{_url(dps_calc.AILMENT_CHANCE_LINES)}",
        f"- 异常源伤加权：{_url(dps_calc.AILMENT_DAMAGE_LINES)}",
        f"- 流血稳态：{_url(dps_calc.BLEED_DPS_LINES)}",
        f"- 中毒稳态：{_url(dps_calc.POISON_DPS_LINES)}",
        f"- 点燃稳态：{_url(dps_calc.IGNITE_DPS_LINES)}",
    ])
    return "\n".join(lines)


@REGISTRY.register(
    name="hit_dps_calc",
    description=(
        "用明确输入复算单技能命中 DPS：基础/附加伤害、伤害有效度、增伤/更多、转化/额外获得、"
        "抗性/物理减伤、暴击、倍伤、战斗周期、冷却 tick、稳态穿刺、伤害性异常、终结、"
        "频率和数量倍率。异常必须单独提供不含命中专属修正的源伤区间。"
        "不要从装备描述猜遗漏参数，"
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
            "overwhelm_physical_reduction_pct": {"type": "number", "default": 0},
            "physical_reduction_ignore_chance_pct": {"type": "number", "default": 0},
            "resistance_ignore_chance_pct": {"type": "object"},
            "resistance_inversion_chance_pct": {"type": "object"},
            "unmodifiable_resistance_types": {"type": "array", "items": {"type": "string"}},
            "enemy_damage_taken_increased_pct": {"type": "number", "default": 0},
            "enemy_damage_taken_increased_by_type": {"type": "object"},
            "enemy_damage_taken_more_multiplier": {"type": "number", "default": 1},
            "enemy_damage_taken_more_by_type": {"type": "object"},
            "hit_chance_pct": {"type": "number", "default": 100},
            "crit_chance_pct": {"type": "number", "default": 0},
            "crit_multiplier_pct": {"type": "number", "default": 150},
            "damage_roll_chance_pct": {"type": "number", "minimum": -100, "maximum": 100},
            "crit_damage_roll_chance_pct": {"type": "number", "minimum": -100, "maximum": 100},
            "extreme_luck": {"type": "boolean", "default": False},
            "double_damage_chance_pct": {"type": "number", "minimum": 0, "maximum": 100},
            "triple_damage_chance_pct": {"type": "number", "minimum": 0, "maximum": 100},
            "double_damage_chance_on_crit_pct": {"type": "number", "minimum": 0, "maximum": 100},
            "triple_damage_chance_on_crit_pct": {"type": "number", "minimum": 0, "maximum": 100},
            "ruthless_blow_every_hits": {"type": "integer", "minimum": 0, "default": 0},
            "ruthless_blow_hit_multiplier": {"type": "number", "minimum": 0, "default": 1},
            "fist_of_war_more_damage_pct": {"type": "number", "minimum": 0, "default": 0},
            "fist_of_war_cooldown_seconds": {"type": "number", "minimum": 0, "default": 0},
            "exerted_attack_more_damage_pct": {"type": "number", "minimum": 0, "default": 0},
            "exerted_attack_uptime_pct": {"type": "number", "minimum": 0, "maximum": 100},
            "action_rate": {"type": "number"},
            "cooldown_seconds": {"type": "number", "default": 0},
            "server_tick_seconds": {"type": "number", "exclusiveMinimum": 0},
            "cooldown_uses": {"type": "integer", "minimum": 1, "default": 1},
            "action_repeats": {"type": "integer", "minimum": 1, "default": 1},
            "channelled": {"type": "boolean", "default": False},
            "triggered": {"type": "boolean", "default": False},
            "skill_dps_multiplier": {"type": "number", "default": 1},
            "quantity_multiplier": {"type": "number", "default": 1},
            "burst_hit_count": {"type": "number", "default": 1},
            "repeat_damage_multiplier": {"type": "number", "default": 1},
            "impale_chance_pct": {"type": "number", "minimum": 0, "maximum": 100},
            "impale_chance_on_crit_pct": {"type": "number", "minimum": 0, "maximum": 100},
            "impale_stacks": {"type": "number", "minimum": 0},
            "impale_base_stored_damage_pct": {"type": "number", "minimum": 0, "default": 10},
            "impale_effect_increased_pct": {"type": "number", "minimum": -100},
            "impale_effect_more_multiplier": {"type": "number", "minimum": 0, "default": 1},
            "impale_duration_seconds": {"type": "number", "minimum": 0, "default": 4},
            "enemy_impale_physical_reduction_pct": {"type": "number", "minimum": 0, "maximum": 90},
            "ignore_enemy_impale_physical_reduction": {"type": "boolean", "default": False},
            "enemy_reflected_damage_taken_increased_pct": {"type": "number", "minimum": -100},
            "enemy_reflected_damage_taken_more_multiplier": {"type": "number", "minimum": 0},
            "cull_percent": {"type": "number", "minimum": 0, "exclusiveMaximum": 100},
            "critical_cull_percent": {"type": "number", "minimum": 0, "exclusiveMaximum": 100},
            "damaging_ailments": {"type": "array", "maxItems": 3, "items": {
                "type": "object", "properties": {
                    "name": {"type": "string", "enum": ["ignite", "poison", "bleed", "点燃", "中毒", "流血"]},
                    "source_damage": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}},
                    "crit_source_damage": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}},
                    "chance_on_hit_pct": {"type": "number", "minimum": 0, "maximum": 100},
                    "chance_on_crit_pct": {"type": "number", "minimum": 0, "maximum": 100},
                    "dot_multiplier_pct": {"type": "number", "minimum": -100},
                    "crit_dot_multiplier_pct": {"type": "number", "minimum": -100},
                    "base_duration_seconds": {"type": "number", "minimum": 0},
                    "duration_increased_pct": {"type": "number", "minimum": -100},
                    "faster_damage_pct": {"type": "number", "exclusiveMinimum": -100},
                    "stack_limit": {"type": "number", "exclusiveMinimum": 0},
                    "stacks_per_application": {"type": "number", "exclusiveMinimum": 0},
                    "ailment_effect_increased_pct": {"type": "number", "minimum": -100},
                    "ruthless_blow_ailment_multiplier": {"type": "number", "minimum": 0},
                    "exerted_ailment_more_damage_pct": {"type": "number", "minimum": 0},
                    "exerted_ailment_uptime_pct": {"type": "number", "minimum": 0, "maximum": 100},
                    "enemy_mitigation_pct": {"type": "number", "minimum": -200, "maximum": 90},
                    "enemy_damage_taken_increased_pct": {"type": "number", "minimum": -100},
                    "enemy_damage_taken_more_multiplier": {"type": "number", "minimum": 0}
                }, "required": ["name", "source_damage"]
            }},
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
    overwhelm_physical_reduction_pct: float = 0.0,
    physical_reduction_ignore_chance_pct: float = 0.0,
    resistance_ignore_chance_pct: dict[str, float] | None = None,
    resistance_inversion_chance_pct: dict[str, float] | None = None,
    unmodifiable_resistance_types: list[str] | None = None,
    enemy_damage_taken_increased_by_type: dict[str, float] | None = None,
    enemy_damage_taken_more_multiplier: float = 1.0,
    enemy_damage_taken_more_by_type: dict[str, float] | None = None,
    hit_chance_pct: float = 100.0,
    crit_chance_pct: float = 0.0, crit_multiplier_pct: float | None = None,
    damage_roll_chance_pct: float = 0.0, crit_damage_roll_chance_pct: float | None = None,
    extreme_luck: bool = False, double_damage_chance_pct: float = 0.0,
    triple_damage_chance_pct: float = 0.0, double_damage_chance_on_crit_pct: float = 0.0,
    triple_damage_chance_on_crit_pct: float = 0.0,
    ruthless_blow_every_hits: int = 0, ruthless_blow_hit_multiplier: float = 1.0,
    fist_of_war_more_damage_pct: float = 0.0, fist_of_war_cooldown_seconds: float = 0.0,
    exerted_attack_more_damage_pct: float = 0.0, exerted_attack_uptime_pct: float = 0.0,
    cooldown_seconds: float = 0.0, server_tick_seconds: float | None = None,
    cooldown_uses: int = 1, action_repeats: int = 1, channelled: bool = False,
    triggered: bool = False, skill_dps_multiplier: float = 1.0,
    quantity_multiplier: float = 1.0, burst_hit_count: float = 1.0,
    repeat_damage_multiplier: float = 1.0,
    impale_chance_pct: float = 0.0, impale_chance_on_crit_pct: float | None = None,
    impale_stacks: float = 0.0, impale_base_stored_damage_pct: float = 10.0,
    impale_effect_increased_pct: float = 0.0, impale_effect_more_multiplier: float = 1.0,
    impale_duration_seconds: float = 4.0,
    enemy_impale_physical_reduction_pct: float | None = None,
    ignore_enemy_impale_physical_reduction: bool = False,
    enemy_reflected_damage_taken_increased_pct: float = 0.0,
    enemy_reflected_damage_taken_more_multiplier: float = 1.0,
    cull_percent: float = 0.0, critical_cull_percent: float = 0.0,
    damaging_ailments: list[dict[str, Any]] | None = None,
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
            overwhelm_physical_reduction_pct=overwhelm_physical_reduction_pct,
            physical_reduction_ignore_chance_pct=physical_reduction_ignore_chance_pct,
            resistance_ignore_chance_pct=resistance_ignore_chance_pct,
            resistance_inversion_chance_pct=resistance_inversion_chance_pct,
            unmodifiable_resistance_types=unmodifiable_resistance_types or (),
            enemy_damage_taken_increased_pct=enemy_damage_taken_increased_pct,
            enemy_damage_taken_increased_by_type=enemy_damage_taken_increased_by_type,
            enemy_damage_taken_more_multiplier=enemy_damage_taken_more_multiplier,
            enemy_damage_taken_more_by_type=enemy_damage_taken_more_by_type,
            hit_chance_pct=hit_chance_pct, crit_chance_pct=crit_chance_pct,
            crit_multiplier_pct=crit_multiplier_pct if crit_multiplier_pct is not None else base_crit_multi,
            damage_roll_chance_pct=damage_roll_chance_pct,
            crit_damage_roll_chance_pct=crit_damage_roll_chance_pct,
            extreme_luck=extreme_luck, double_damage_chance_pct=double_damage_chance_pct,
            triple_damage_chance_pct=triple_damage_chance_pct,
            double_damage_chance_on_crit_pct=double_damage_chance_on_crit_pct,
            triple_damage_chance_on_crit_pct=triple_damage_chance_on_crit_pct,
            ruthless_blow_every_hits=ruthless_blow_every_hits,
            ruthless_blow_hit_multiplier=ruthless_blow_hit_multiplier,
            fist_of_war_more_damage_pct=fist_of_war_more_damage_pct,
            fist_of_war_cooldown_seconds=fist_of_war_cooldown_seconds,
            exerted_attack_more_damage_pct=exerted_attack_more_damage_pct,
            exerted_attack_uptime_pct=exerted_attack_uptime_pct,
            action_rate=action_rate, cooldown_seconds=cooldown_seconds,
            server_tick_seconds=server_tick_seconds, cooldown_uses=cooldown_uses,
            action_repeats=action_repeats, channelled=channelled, triggered=triggered,
            skill_dps_multiplier=skill_dps_multiplier, quantity_multiplier=quantity_multiplier,
            burst_hit_count=burst_hit_count, repeat_damage_multiplier=repeat_damage_multiplier,
            impale_chance_pct=impale_chance_pct,
            impale_chance_on_crit_pct=impale_chance_on_crit_pct,
            impale_stacks=impale_stacks,
            impale_base_stored_damage_pct=impale_base_stored_damage_pct,
            impale_effect_increased_pct=impale_effect_increased_pct,
            impale_effect_more_multiplier=impale_effect_more_multiplier,
            impale_duration_seconds=impale_duration_seconds,
            enemy_impale_physical_reduction_pct=enemy_impale_physical_reduction_pct,
            ignore_enemy_impale_physical_reduction=ignore_enemy_impale_physical_reduction,
            enemy_reflected_damage_taken_increased_pct=enemy_reflected_damage_taken_increased_pct,
            enemy_reflected_damage_taken_more_multiplier=enemy_reflected_damage_taken_more_multiplier,
            cull_percent=cull_percent, critical_cull_percent=critical_cull_percent,
            damaging_ailments=damaging_ailments or (),
        )
    except (ValueError, TypeError, constants.MissingConstant) as exc:
        return ToolResult(ok=False, error=str(exc))
    answer = _render(result)
    data = result.as_dict()
    data.update({"found": True, "kind": "result", "boundary": BOUNDARY, "answer": answer})
    citation = _citation(answer, slug="explicit-input-pipeline", title="单技能命中 DPS 逐步推导")
    return ToolResult(ok=True, data=data, citations=[citation], meta={"channel": "derived"})


@REGISTRY.register(
    name="hit_dps_compare",
    description=(
        "用同一条确定性公式比较 2 到 10 个显式参数方案，返回命中、穿刺、异常、总计及终结等效 DPS、"
        "相对首个基线的差值、平均击中与爆发伤害；不从装备或 Build 猜参数。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "scenarios": {"type": "array", "minItems": 2, "maxItems": 10, "items": {
                "type": "object", "properties": {
                    "name": {"type": "string"},
                    "inputs": {"type": "object"},
                }, "required": ["name", "inputs"],
            }},
        },
        "required": ["scenarios"],
    },
)
def hit_dps_compare(scenarios: list[dict[str, Any]]) -> ToolResult:
    try:
        comparison = compare_hit_dps([
            (str(item["name"]), _with_default_crit(dict(item["inputs"]))) for item in scenarios
        ])
    except (KeyError, ValueError, TypeError, constants.MissingConstant) as exc:
        return ToolResult(ok=False, error=str(exc))
    data = comparison.as_dict()
    rows = ["### 命中 DPS 方案比较", "", "| 方案 | 命中 DPS | 含穿刺 | 异常 DPS | 总 DPS | 含异常与终结 | 命中相对基线 | 平均击中 | 一轮爆发 |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in comparison.scenarios:
        if row.name == comparison.baseline:
            pct = "基线"
        elif row.dps_delta_pct is None:
            pct = "不可定义"
        else:
            pct = f"{row.dps_delta_pct:+.2f}%"
        rows.append(
            f"| {row.name} | {fmt(row.dps)} | {fmt(row.combined_dps)} | "
            f"{fmt(row.ailment_dps)} | {fmt(row.total_dps)} | "
            f"{fmt(row.effective_total_dps_with_cull)} | {pct} | {fmt(row.average_hit)} | "
            f"{fmt(row.burst_damage)} |"
        )
    rows.extend(["", f"- 最高持续 DPS：{comparison.best_dps_scenario}",
                 f"- 最高命中+穿刺 DPS：{comparison.best_combined_dps_scenario}",
                 f"- 最高含终结等效 DPS：{comparison.best_effective_dps_scenario}",
                 f"- 最高异常 DPS：{comparison.best_ailment_dps_scenario}",
                 f"- 最高命中+穿刺+异常总 DPS：{comparison.best_total_dps_scenario}",
                 f"- 最高含异常与终结总等效 DPS：{comparison.best_effective_total_dps_scenario}",
                 f"- 最高一轮爆发：{comparison.best_burst_scenario}", "", BOUNDARY + "。"])
    answer = "\n".join(rows)
    data.update({"kind": "comparison", "answer": answer, "boundary": BOUNDARY})
    return ToolResult(
        ok=True, data=data,
        citations=[_citation(answer, slug="scenario-comparison", title="命中 DPS 方案比较")],
        meta={"channel": "derived"},
    )


@REGISTRY.register(
    name="hit_dps_target_rate",
    description=(
        "在其它参数固定时，数值反解达到目标命中、穿刺、异常、总计或含终结等效 DPS 所需的原始"
        "每秒次数，并判断服务器 tick 及触发/非触发冷却上限是否令目标不可达。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_dps": {"type": "number"},
            "metric": {"type": "string", "enum": [
                "hit_dps", "combined_dps", "effective_dps_with_cull", "ailment_dps",
                "total_dps", "effective_total_dps_with_cull"
            ], "default": "hit_dps"},
            "inputs": {"type": "object", "description": "除 action_rate 外的 hit_dps_calc 参数"},
        },
        "required": ["target_dps", "inputs"],
    },
)
def hit_dps_target_rate(
    target_dps: float,
    inputs: dict[str, Any],
    metric: str = "hit_dps",
) -> ToolResult:
    try:
        result = target_action_rate(
            target_dps, _with_default_crit(inputs), metric=metric
        )
    except (ValueError, TypeError, constants.MissingConstant) as exc:
        return ToolResult(ok=False, error=str(exc))
    data = result.as_dict()
    metric_label = {
        "hit_dps": "命中 DPS",
        "combined_dps": "命中+穿刺 DPS",
        "effective_dps_with_cull": "含终结等效 DPS",
        "ailment_dps": "伤害性异常 DPS",
        "total_dps": "命中+穿刺+异常总 DPS",
        "effective_total_dps_with_cull": "含异常与终结总等效 DPS",
    }[result.metric]
    if result.reachable:
        answer = (
            f"达到 {fmt(result.target_dps)} {metric_label}需要原始每秒 "
            f"{fmt(result.required_raw_action_rate or 0)} 次，"
            f"对应有效每秒 {fmt(result.required_effective_action_rate or 0)} 次；"
            f"同链路回代为 {fmt(result.achieved_dps or 0)}。"
        )
    else:
        cap = (
            f"，有效频率上限为每秒 {fmt(result.action_rate_cap)} 次"
            if result.action_rate_cap else ""
        )
        maximum = (
            f"，该口径最高约 {fmt(result.maximum_achievable_dps)}"
            if result.maximum_achievable_dps is not None else ""
        )
        answer = f"目标 {fmt(result.target_dps)} {metric_label}不可达{cap}{maximum}：{result.reason}。"
    data.update({"kind": "target_rate", "answer": answer, "boundary": BOUNDARY})
    return ToolResult(
        ok=True, data=data,
        citations=[_citation(answer, slug="target-action-rate", title="命中 DPS 目标频率反解")],
        meta={"channel": "derived"},
    )
