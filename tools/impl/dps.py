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
    "触发/非触发冷却、频率、重复爆发及技能/数量倍率。不含护甲动态减伤、无情一击、战吼、"
    "服务器 tick 上限、终结、穿刺、异常与持续伤害；"
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
        f"- 平均每次击中：{fmt(result.average_hit)}",
        f"- 计入 {result.hit_chance_pct:g}% 命中率：每次使用期望 {fmt(result.average_damage_per_use)}",
        f"- 原始每秒 {result.raw_action_rate:g} 次 → 有效 {result.action_rate:g} 次"
        f"（冷却 {result.cooldown_seconds:g} 秒，{'触发' if result.triggered else '非触发'}口径）",
        f"- 技能倍率 {result.skill_dps_multiplier:g} × 数量倍率 {result.quantity_multiplier:g}",
        f"- 一轮爆发：{result.burst_hit_count:g} 击、重复伤害倍率 {result.repeat_damage_multiplier:g}"
        f" → {fmt(result.average_burst_damage)}",
        "", f"**命中 DPS：{fmt(result.dps)}**", "", BOUNDARY + "。", "", "公式来源：",
        f"- 基础与附加伤害：{_url(dps_calc.BASE_DAMAGE_LINES)}",
        f"- 伤害类型加成与转化递归：{_url(dps_calc.DAMAGE_LINES)}",
        f"- 转化 100% 缩放及技能转化优先：{_url(dps_calc.CONVERSION_LINES)}",
        f"- 触发/非触发冷却频率：{_url(dps_calc.RATE_LINES)}",
        f"- 双倍/三倍伤害重叠：{_url(dps_calc.SCALED_DAMAGE_LINES)}",
        f"- 幸运/不幸伤害期望：{_url(dps_calc.ROLL_LINES)}",
        f"- 抗性、穿透与承伤倍率：{_url(dps_calc.ENEMY_EFFECT_LINES)}",
        f"- 平均击中、命中率、爆发与最终 DPS：{_url(dps_calc.BURST_LINES)}",
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
            "action_rate": {"type": "number"},
            "cooldown_seconds": {"type": "number", "default": 0},
            "triggered": {"type": "boolean", "default": False},
            "skill_dps_multiplier": {"type": "number", "default": 1},
            "quantity_multiplier": {"type": "number", "default": 1},
            "burst_hit_count": {"type": "number", "default": 1},
            "repeat_damage_multiplier": {"type": "number", "default": 1},
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
    triple_damage_chance_on_crit_pct: float = 0.0, cooldown_seconds: float = 0.0,
    triggered: bool = False, skill_dps_multiplier: float = 1.0,
    quantity_multiplier: float = 1.0, burst_hit_count: float = 1.0,
    repeat_damage_multiplier: float = 1.0,
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
            action_rate=action_rate, cooldown_seconds=cooldown_seconds, triggered=triggered,
            skill_dps_multiplier=skill_dps_multiplier, quantity_multiplier=quantity_multiplier,
            burst_hit_count=burst_hit_count, repeat_damage_multiplier=repeat_damage_multiplier,
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
        "用同一条确定性命中 DPS 公式比较 2 到 10 个显式参数方案，返回相对首个基线的 DPS、"
        "平均击中与爆发伤害差值；不从装备或 Build 猜参数。"
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
    rows = ["### 命中 DPS 方案比较", "", "| 方案 | DPS | 相对基线 | 平均击中 | 一轮爆发 |", "| --- | ---: | ---: | ---: | ---: |"]
    for row in comparison.scenarios:
        if row.name == comparison.baseline:
            pct = "基线"
        elif row.dps_delta_pct is None:
            pct = "不可定义"
        else:
            pct = f"{row.dps_delta_pct:+.2f}%"
        rows.append(
            f"| {row.name} | {fmt(row.dps)} | {pct} | {fmt(row.average_hit)} | {fmt(row.burst_damage)} |"
        )
    rows.extend(["", f"- 最高持续 DPS：{comparison.best_dps_scenario}",
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
        "在其它命中 DPS 参数固定时，反解达到目标 DPS 所需的原始每秒次数，并判断触发或"
        "非触发冷却上限是否令目标不可达。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target_dps": {"type": "number"},
            "inputs": {"type": "object", "description": "除 action_rate 外的 hit_dps_calc 参数"},
        },
        "required": ["target_dps", "inputs"],
    },
)
def hit_dps_target_rate(target_dps: float, inputs: dict[str, Any]) -> ToolResult:
    try:
        result = target_action_rate(target_dps, _with_default_crit(inputs))
    except (ValueError, TypeError, constants.MissingConstant) as exc:
        return ToolResult(ok=False, error=str(exc))
    data = result.as_dict()
    if result.reachable:
        answer = (
            f"达到 {fmt(result.target_dps)} 命中 DPS 需要原始每秒 {fmt(result.required_raw_action_rate or 0)} 次，"
            f"对应有效每秒 {fmt(result.required_effective_action_rate or 0)} 次；"
            f"同链路回代为 {fmt(result.achieved_dps or 0)}。"
        )
    else:
        cap = f"，冷却上限为每秒 {fmt(result.cooldown_cap)} 次" if result.cooldown_cap else ""
        answer = f"目标 {fmt(result.target_dps)} 命中 DPS 不可达{cap}：{result.reason}。"
    data.update({"kind": "target_rate", "answer": answer, "boundary": BOUNDARY})
    return ToolResult(
        ok=True, data=data,
        citations=[_citation(answer, slug="target-action-rate", title="命中 DPS 目标频率反解")],
        meta={"channel": "derived"},
    )
