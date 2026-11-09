"""Max Hit：一发最多能挨多大的伤害（前六块计算器的合流）。

## 为什么这一块要单独有工具

「我这套装备能不能挨住那一发」是防御问题的终点，而它不是任何单一维度：
抗性、护甲、物理减伤、承受倍率、池子大小要按**顺序**合起来反解。
分开问四遍，用户得自己乘一遍——那正是最容易算错的地方。

## 边界写在最前面

只算**单一伤害类型、100% 不转化**的那一支（PoB 在这一支上是闭式解）。
伤害转化、守护技能、Ward、灵体、**Mind over Matter**（伤害分流到魔力）
都不算——PoB 对多类型转化用的是多轮平滑逼近，它自己的注释说完整解
「可能要解一个六次多项式」。拿单类型的结果去冒充那些情形，是一个带着推导表的错数字。
"""

from __future__ import annotations

import math
from typing import Any

from calc import constants
from calc import maxhit as mh
from calc import resistance as res
from calc.derive import fmt, pob_url, render_steps
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 3200

_BOUNDARY = (
    "只算**单一伤害类型、100% 不转化**的一发：池子由问题给出（生命 + 能量护盾），"
    "先区分总抗性与生效抗性；**不含伤害转化、守护技能、Ward、灵体与 Mind over Matter**"
    "（PoB 对那些情形用的是多轮平滑逼近，不是闭式解）；护甲只对物理伤害生效"
)


def _sources() -> list[str]:
    cap = constants.provenance(constants.MAX_PHYS_DAMAGE_REDUCTION)
    url = cap.url
    return [
        f"- 抗性基础上限 {constants.get(constants.MAX_ALL_RESISTANCES):g}%、"
        f"最大抗性硬上限 {constants.get(constants.MAX_RESIST_CAP):g}%："
        f"{pob_url(constants.provenance(constants.MAX_RESIST_CAP).url, res.FORMULA_FILE, lines=res.FORMULA_LINES)}",
        f"- 无护甲时的闭式：{pob_url(url, mh.DEFENCE_FILE, lines=mh.SIMPLE_LINES)}",
        f"- 有护甲时的二次方程（含两个边界）："
        f"{pob_url(url, mh.DEFENCE_FILE, lines=mh.QUADRATIC_LINES)}",
        f"- PoB 自己的注释：完整解要解六次多项式，所以多类型转化用逼近："
        f"{pob_url(url, mh.DEFENCE_FILE, line=mh.COMMENT_LINE)}",
        f"- 物理减伤上限 {constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION):g}%："
        f"{cap.key}（GGG 原始表 {cap.dat_source or '未标注'}）{cap.url}",
    ]


def _render_rules() -> str:
    cap = constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION)
    return "\n".join([
        "### 一发最多能挨多少：怎么算", "",
        "一发打到身上依次经过 **抗性 → 护甲与物理减伤 → 池子（生命 + 能量护盾）**，"
        "Max Hit 就是把这条链反解。",
        "",
        "**没有护甲**时是一步除法：",
        "",
        "> 最大承受伤害 = 池 ÷（抗性倍率 × 承受倍率）",
        "",
        "所以 75% 抗性、5000 池 ⇒ 能挨 20,000：抗性把一发砍到四分之一。",
        "",
        f"**有护甲**时不是——护甲减伤本身依赖伤害大小（`护甲 /(护甲 + 5 × 伤害)`），"
        f"所以反解要解一个二次方程，再夹在「完全不算护甲」与「护甲吃满 {cap:g}% "
        f"减伤上限」两个边界之间。护甲对**大打击**的收益远低于小打击，"
        f"这就是「护甲挡不住大招」的算术来源。",
        "",
        "要算具体数字，请给出：**池子**（生命 + 能量护盾）、该类型的**实际生效抗性**，"
        "以及护甲（物理伤害才用得上）。",
        "",
        _BOUNDARY + "。",
        "",
        "来源：",
        *_sources(),
    ])


def _render_result(
    result: mh.MaxHitResult, damage_type: str,
    resistance: res.ResistanceResult | None, assumptions: list[str],
) -> str:
    threshold = fmt(round(result.max_hit, 8))
    out = [f"### 单次{damage_type}击中的承伤临界值：{threshold}", ""]
    out.extend(assumptions)
    if resistance:
        out.append(
            f"总抗性 {resistance.total}% → 最大抗性 {resistance.max_resist}% → "
            f"生效抗性 {resistance.final}%"
            + (f" → 敌方穿透 {resistance.enemy_penetration:g}% 后用于承伤计算的抗性 {result.resist_pct:g}%" if resistance.enemy_penetration else "")
            + "。超出的抗性不会继续降低本次击中伤害。"
        )
        out.extend(render_steps(resistance.steps))
        out.append("")
    out.extend(render_steps(result.steps))
    out.append("")
    out.append(
        f"池子 {fmt(result.pool)}、{damage_type}抗性 {result.resist_pct:g}%"
        + (f"、护甲 {fmt(result.armour)}" if result.armour else "")
        + f"：**单次原始伤害临界值为 {threshold}**"
        f"（相当于裸池的 {result.effective_multiplier:.2f} 倍）。"
    )
    out.append("这是 Max Hit 模型阈值，不是保证存活的伤害量；减伤后的伤害达到可用生命/护盾池时会耗尽池子。")
    if result.armour:
        out.append(
            f"这一发会被护甲与减伤挡掉约 {result.armour_reduction_pct:.1f}%——"
            f"护甲减伤依赖伤害大小，所以**同一堆护甲对更大的打击挡得更少**。"
        )
        if result.armour_capped:
            out.append(
                f"这一发已经把减伤顶到上限（{constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION):g}%），"
                "再加护甲不会更高。"
            )
    out.append("")
    out.append(_BOUNDARY + "。")
    out.append("")
    out.append("来源：")
    out.extend(_sources())
    text = "\n".join(out)
    if len(text) <= MAX_EVIDENCE_CHARS:
        return text
    return (
        f"{text[:MAX_EVIDENCE_CHARS]}\n\n（证据超过 {MAX_EVIDENCE_CHARS} 字已截断，"
        f"剩余 {len(text) - MAX_EVIDENCE_CHARS} 字未包含在内）"
    )


def _citation(title: str, text: str, url: str) -> dict[str, Any]:
    provenance = constants.provenance(constants.MAX_PHYS_DAMAGE_REDUCTION)
    priority = int(provenance.source_priority)
    return {
        "chunk_id": f"max_hit_calc:{title}",
        "entity_id": None,
        "title": title,
        "url": url,
        "source": provenance.source,
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text,
        "channel": "derived",
    }


def _clarification(message: str) -> ToolResult:
    text = (
        "### 需要核对计算前提\n\n" + message + "。请核对后再算，本轮不提供承伤数值。\n\n"
        f"基础最大抗性为 {constants.get(constants.MAX_ALL_RESISTANCES):g}%，"
        f"最大抗性硬上限为 {constants.get(constants.MAX_RESIST_CAP):g}%。"
        "总抗性需要先按最大抗性封顶，再扣除敌方穿透；不能擅自把生效抗性改成总抗性。\n\n"
        + _BOUNDARY + "。\n\n来源：\n" + "\n".join(_sources())
    )
    return ToolResult(
        ok=True, data={"found": True, "kind": "clarification", "reason": message},
        citations=[_citation("承伤计算的输入约束", text, pob_url(
            constants.provenance(constants.MAX_RESIST_CAP).url,
            res.FORMULA_FILE, lines=res.FORMULA_LINES,
        ))],
        meta={"channel": "derived", "table": "game_constant"},
    )


@REGISTRY.register(
    name="max_hit_calc",
    description=(
        "算「一发最多能挨多大的伤害」：把抗性 → 护甲/物理减伤 → 生命+护盾池这条链反解。"
        "无护甲时是一步除法，有护甲时按 PoB 的二次方程闭式解并夹在两个边界之间；"
        "同时给出「相当于裸池的多少倍」。\n"
        "**只算单一伤害类型、不转化**；不含守护技能、Ward、Mind over Matter。\n"
        "先理解输入口径，再调用工具。用户仅说火抗99%或90%时用 total_resist_pct，不能当成已生效值。"
        "只有用户明确说实际/生效抗性才用 resist_pct；即使数值不合法也须原样传入，由工具指出矛盾，禁止擅自改成总抗性。"
        "不明确最大抗性时，工具按基础上限作条件计算并声明假设。"
        "上限及穿透必须逐字取自问题，不能自行预先相减。问题没给池子或抗性时只讲规则。"
        "题目存在本工具不能表达的防御条件时必须填写 unsupported_conditions，不可静默忽略。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "life": {"type": "number", "description": "当前可用生命"},
            "energy_shield": {"type": "number", "description": "能量护盾"},
            "pool": {
                "type": "number",
                "description": "直接给池子（生命 + 护盾）。不能与 life / energy_shield 同时给出",
            },
            "resist_pct": {
                "type": "number",
                "description": "用户明确给出的、穿透前实际生效抗性，保留原始数值交工具校验。与 total_resist_pct 二选一；物理填0",
            },
            "total_resist_pct": {
                "type": "number", "description": "未按最大抗性封顶的总抗性；仅说99%火抗时填99，工具负责封顶",
            },
            "maximum_resist_pct": {
                "type": "number", "description": "明确给出的最大抗性（例如上限80%），保留原始数值交工具校验；未提供则省略",
            },
            "extra_max_resist_pct": {
                "type": "number", "description": "明确给出的本伤害类型最大抗性加成，如+5%；不要自行加到75，不能与maximum_resist_pct同填",
            },
            "enemy_penetration": {
                "type": "number", "description": "敌方穿透百分比，默认0；先封顶再扣除，不能被超抗抵消",
            },
            "unsupported_conditions": {
                "type": "array", "items": {"type": "string"},
                "description": "题目中本工具无法表达的有效条件，例如伤害转化、守护、MoM、压制或格挡；没有则省略",
            },
            "armour": {"type": "number", "description": "护甲值（只对物理伤害生效）"},
            "flat_reduction_pct": {
                "type": "number", "description": "另有的物理伤害减免百分比（耐力球等）",
            },
            "taken_mult": {
                "type": "number",
                "description": "「受到的伤害减少/增加」乘算修正，0.8 表示少受 20%",
            },
            "overwhelm_pct": {
                "type": "number", "description": "敌方的压制（无视物理减伤）百分比",
            },
            "damage_type": {
                "type": "string",
                "enum": ["物理", "火焰", "冰霜", "闪电", "混沌"],
                "description": "单次击中的伤害类型，决定抗性、护甲和可用防御池的适用性",
            },
        },
        "required": [],
    },
)
def max_hit_calc(
    life: float = 0.0,
    energy_shield: float = 0.0,
    pool: float = 0.0,
    resist_pct: float | None = None,
    armour: float = 0.0,
    flat_reduction_pct: float = 0.0,
    taken_mult: float = 1.0,
    overwhelm_pct: float = 0.0,
    damage_type: str = "",
    total_resist_pct: float | None = None,
    maximum_resist_pct: float | None = None,
    extra_max_resist_pct: float = 0.0,
    enemy_penetration: float = 0.0,
    unsupported_conditions: list[str] | None = None,
) -> ToolResult:
    try:
        _sources()
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"防御常量未入库：{exc}")

    if unsupported_conditions:
        # Model-supplied condition text is not verified game evidence.
        return _clarification("本工具不能结算题目中的额外防御条件，不能忽略后给出完整结果")
    values = [life, energy_shield, pool, armour, flat_reduction_pct, taken_mult,
              overwhelm_pct, extra_max_resist_pct, enemy_penetration]
    values.extend(v for v in (resist_pct, total_resist_pct, maximum_resist_pct) if v is not None)
    if not all(math.isfinite(v) for v in values):
        return _clarification("计算输入必须是有限数值")
    if min(life, energy_shield, pool, armour, enemy_penetration) < 0 or taken_mult <= 0:
        return _clarification("生命、护盾、池子、护甲和穿透不能为负，承受倍率必须大于0")
    if not 0 <= flat_reduction_pct <= constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION) or not 0 <= overwhelm_pct <= 100:
        return _clarification("物理减伤须在0%..90%之间，无视物理减伤须在0%..100%之间")
    if pool and (life or energy_shield):
        return _clarification("pool 与 life/energy_shield 不能同时给出，请明确可用防御池")
    if resist_pct is not None and total_resist_pct is not None:
        return _clarification("总抗性与生效抗性只能提供一种，不能重复结算")
    if maximum_resist_pct is not None and extra_max_resist_pct:
        return _clarification("最大抗性与最大抗性加成不能同时给出，请明确上限口径")
    if resist_pct is not None and (maximum_resist_pct is not None or extra_max_resist_pct):
        return _clarification("需要计算抗性上限时请提供 total_resist_pct；resist_pct 只接受已结算的生效值")

    label = damage_type.strip() or ("物理" if armour else "该类型")
    if label not in ("物理", "该类型", *res.ELEMENTS_ZH):
        return _clarification("请明确单一伤害类型：物理、火焰、冰霜、闪电或混沌")
    if label == "物理" and (resist_pct or total_resist_pct is not None or maximum_resist_pct is not None or extra_max_resist_pct or enemy_penetration):
        return _clarification("物理击中不能使用元素/混沌抗性或抗性穿透减伤")
    if label != "物理" and (armour or flat_reduction_pct or overwhelm_pct):
        return _clarification("本模型的护甲与物理减伤只适用于物理击中，不能用于元素或混沌击中")
    if label == "混沌" and energy_shield:
        return _clarification("混沌伤害不能默认将能量护盾计入可用池；请明确绕过护盾规则与可用生命")

    rules_url = pob_url(
        constants.provenance(constants.MAX_PHYS_DAMAGE_REDUCTION).url,
        mh.DEFENCE_FILE, lines=mh.QUADRATIC_LINES,
    )
    total = pool or (life + energy_shield)
    missing = []
    if total <= 0:
        missing.append("生命 + 能量护盾")
    if total > 0 and label != "物理" and resist_pct is None and total_resist_pct is None:
        missing.append("该伤害类型的总抗性或明确的生效抗性")
    if total_resist_pct is not None and label == "该类型":
        missing.append("单一伤害类型")
    if missing:
        note = "请补充：" + "、".join(missing) + "。没给池子或抗性时不能默认按0计算。\n\n"
        return ToolResult(
            ok=True,
            data={
                "found": True, "kind": "rules",
                "damage_reduction_cap": constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION),
                "missing_inputs": missing,
                "boundary": _BOUNDARY,
            },
            citations=[_citation("Max Hit 的算法与边界", note + _render_rules(), rules_url)],
            meta={"channel": "derived", "table": "game_constant"},
        )

    resistance = None
    assumptions = []
    try:
        if total_resist_pct is not None:
            resistance = res.resistance(
                label, base_specific=total_resist_pct,
                max_override=maximum_resist_pct, extra_max_specific=extra_max_resist_pct,
                enemy_penetration=enemy_penetration,
            )
            effective = resistance.final - enemy_penetration
            if maximum_resist_pct is None and not extra_max_resist_pct:
                assumptions.append(
                    f"**条件计算**：未提供最大抗性加成，按基础最大抗性 {constants.get(constants.MAX_ALL_RESISTANCES):g}% "
                    f"计算；最大抗性硬上限为 {constants.get(constants.MAX_RESIST_CAP):g}%。"
                    "若角色实际最大抗性不同，请提供该值后重算。"
                )
        else:
            before_pen = float(resist_pct or 0)
            if not constants.get(constants.RESIST_FLOOR) <= before_pen <= constants.get(constants.MAX_RESIST_CAP):
                raise ValueError("实际生效抗性必须在 -200%..90% 之间，与题目给出的值矛盾；请确认实际生效值或明确这是未封顶总抗性")
            effective = before_pen - enemy_penetration
            if label != "物理":
                assumptions.append(f"按明确给出的生效抗性 {before_pen:g}% 计算，敌方穿透 {enemy_penetration:g}%。")
        result = mh.max_hit(
            total, resist_pct=effective, armour=armour,
            flat_reduction_pct=flat_reduction_pct, taken_mult=taken_mult,
            overwhelm_pct=overwhelm_pct,
        )
    except ValueError as exc:
        return _clarification(str(exc))
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"防御常量未入库：{exc}")
    return ToolResult(
        ok=True,
        data={
            "found": True, "kind": "result",
            "pool": total,
            "total_resist_pct": resistance.total if resistance else None,
            "maximum_resist_pct": resistance.max_resist if resistance else None,
            "effective_resist_pct": effective,
            "enemy_penetration": enemy_penetration,
            "assumptions": assumptions,
            "max_hit": result.max_hit,
            "without_armour": round(result.without_armour, 2),
            "effective_multiplier": round(result.effective_multiplier, 2),
            "armour_reduction_pct": round(result.armour_reduction_pct, 2),
            "armour_capped": result.armour_capped,
            "damage_type": label,
            "boundary": _BOUNDARY,
        },
        citations=[_citation(
            f"一发{label}最多能挨 {fmt(result.max_hit)}",
            _render_result(result, label, resistance, assumptions), rules_url,
        )],
        meta={"channel": "derived", "table": "game_constant"},
    )
