"""Boss 技能一击伤害查询工具。

**只给伤害建模，不给打法。** 这个工具回答「谢勒的球在 84 级打多少冰霜伤害」，
不回答「谢勒怎么打」——技能机制、阶段与应对本地没有来源。

## 为什么默认给区间

`rollRange` 是 PoB 的伤害掷骰配置（0..100）。用户不指定时**两端都给**，
因为「打多少伤害」的诚实答案是一个区间——替用户挑一个点，
就是把一个范围偷偷说成了一个确定值。

## 它是给 max_hit_calc 用的

`max_hit_calc` 一直要求用户自报「这一击多少伤害」。有了这一维，
那个数第一次能从库里取到。证据里明说这条衔接，但**不替用户算**——
两个工具各做一件事，串起来的是模型（且每一步都有自己的引用）。

## 数字是 PoB 的建模值

这一维**只有 PoB 一个来源**（P2）：GGG 没有公布 Boss 技能的伤害系数。
证据里要写明，否则它读起来像官方数值。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from calc import boss_damage, boss_survival, constants, maxhit
from calc.derive import pob_url, render_steps
from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY, Source
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 2400
MAX_MATCHES = 8

_BOUNDARY = (
    "只给 Boss 技能的**伤害建模**（伤害 / 穿透 / Uber 倍率），"
    "**不含技能机制、阶段与应对打法**——那一维本地没有来源。"
    "数字是 **Path of Building 的建模值**，GGG 未公布这些系数。"
    "要判断「扛不扛得住」，把这里的伤害交给 `max_hit_calc`。"
)

_SURVIVAL_BOUNDARY = (
    "只比较 PoB 预设的一次单伤害类型命中与用户明确提供的防御池；"
    "不含持续伤害、多击序列、伤害转化、暴击、格挡、闪避、法术压制、守护技能、"
    "Ward、Mind over Matter、恢复、阶段机制或打法。结论不是整场 Boss 战的生存保证"
)

_DAMAGE_TYPE_ZH = {
    "Physical": "物理",
    "Fire": "火焰",
    "Cold": "冰霜",
    "Lightning": "闪电",
    "Chaos": "混沌",
}


def _rows(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    like = f"%{query.replace('%', '').replace('_', ' ')}%"
    return conn.execute(
        "SELECT * FROM pob_boss_skill WHERE name LIKE ? ORDER BY name LIMIT ?",
        (like, MAX_MATCHES),
    ).fetchall()


def _render(row: sqlite3.Row, level: int, uber: bool, roll: float | None) -> str:
    mults = json.loads(row["damage_mults"])
    uber_mult = row["uber_damage_mult"] if uber else None
    out = [
        f"### {row['name']}（{row['damage_type']}）"
        + ("　**Uber 变体**" if uber else ""),
        "",
        f"敌人等级 {level}。伤害按 PoB 原式复算（`{boss_damage.FORMULA_SOURCE}`）：",
    ]
    for dtype, pair in sorted(mults.items()):
        base, per_roll = float(pair[0]), float(pair[1])
        if roll is None:
            low, high = boss_damage.hit_range(
                level=level, base_mult=base, roll_mult=per_roll,
                damage_type=dtype, uber_mult=uber_mult,
            )
            out += [
                "",
                f"**{dtype}**：{low.damage:,.0f} — {high.damage:,.0f}"
                "（掷骰区间 0%..100%，未指定时给两端）",
                "",
                *render_steps(high.steps),
            ]
        else:
            hit = boss_damage.hit_damage(
                level=level, base_mult=base, roll_mult=per_roll,
                damage_type=dtype, roll_range=roll, uber_mult=uber_mult,
            )
            out += ["", f"**{dtype}**：{hit.damage:,.0f}（掷骰区间 {roll:g}%）", "",
                    *render_steps(hit.steps)]

    pens = json.loads(row["uber_penetrations"] if uber else row["penetrations"])
    out.append("")
    if pens:
        out.append("穿透：" + "、".join(f"{k} {v:g}%" for k, v in sorted(pens.items())))
    else:
        out.append("穿透：无（该变体在 PoB 里没有穿透项）")
    if row["tooltip"]:
        out.append(f"PoB 说明原文：{row['tooltip']}")
    out += [
        "",
        "> 这是**伤害建模**，不是打法：技能机制、阶段与应对未采集。",
        "> 数字来自 Path of Building 的预设系数，GGG 未公布。",
    ]
    return "\n".join(out)


def _citation(row: sqlite3.Row, text: str) -> dict[str, Any]:
    priority = int(row["source_priority"])
    return {
        "chunk_id": f"boss_skill:{row['name']}",
        "entity_id": None,
        "title": f"Boss 技能伤害：{row['name']}",
        "url": row["pob_url"],
        "source": Source.POB.value,
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "derived",
    }


def _maxhit_citation(text: str) -> dict[str, Any]:
    provenance = constants.provenance(constants.MAX_PHYS_DAMAGE_REDUCTION)
    url = pob_url(
        provenance.url, maxhit.DEFENCE_FILE, lines=maxhit.QUADRATIC_LINES
    )
    return {
        "chunk_id": "boss_survival:max_hit_formula",
        "entity_id": None,
        "title": "Boss 单次命中的防御结算与 Max Hit",
        "url": url,
        "source": provenance.source,
        "source_priority": int(provenance.source_priority),
        "can_support_facts": int(provenance.source_priority) <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "derived",
    }


def _unknown_survival_answer(
    skill: str, reason: str, *, level: int, uber: bool, pool: float
) -> str:
    return "\n".join([
        f"### {skill} 单次命中生存校验" + ("（Uber）" if uber else ""),
        "",
        "**结论：无法安全计算**",
        f"- 原因：{reason}",
        f"- 已提供：敌人等级 {level}；防御池 {pool:g}",
        "",
        f"> 边界：{_SURVIVAL_BOUNDARY}。",
    ])


def _render_survival(
    row: sqlite3.Row,
    result: boss_survival.BossSurvivalResult,
    *,
    level: int,
    uber: bool,
    roll_range: float | None,
    armour: float,
    flat_physical_reduction_pct: float,
    taken_mult: float,
) -> str:
    labels = {
        "survives_full_range": "整个已建模掷骰区间都不会耗尽该池子",
        "roll_dependent": "是否耗尽池子取决于伤害掷骰",
        "fails_full_range": "整个已建模掷骰区间都会耗尽该池子",
    }
    dtype = _DAMAGE_TYPE_ZH[result.damage_type]
    roll_note = (
        f"固定 {roll_range:g}% 掷骰点" if roll_range is not None else "0%..100% 掷骰区间"
    )
    lines = [
        f"### {row['name']} 单次命中生存校验" + ("（Uber）" if uber else ""),
        "",
        f"**结论：{labels[result.outcome]}**",
        f"- 口径：敌人等级 {level}；{roll_note}；防御池 {result.pool:g}",
        f"- 原始{dtype}伤害：{result.raw_low:,.0f} — {result.raw_high:,.0f}",
    ]
    if result.damage_type == "Physical":
        lines.append(
            f"- 防御输入：护甲 {armour:g}；额外物理减伤 {flat_physical_reduction_pct:g}%"
            f"；承受倍率 {taken_mult:g}"
        )
    else:
        lines.append(
            f"- 抗性：穿透前 {result.resist_before_penetration:g}%"
            f" − 技能穿透 {result.penetration_pct:g}%"
            f" = 实际结算 {result.effective_resist_pct:g}%"
            f"；承受倍率 {taken_mult:g}"
        )
        if armour or flat_physical_reduction_pct:
            lines.append("- 忽略的输入：护甲与物理减伤不作用于这次非物理命中")
    lines += [
        f"- 实际扣除池子：{result.taken_low:,.2f} — {result.taken_high:,.2f}",
        f"- 低 / 高掷骰后的池子余量：{result.margin_low:,.2f} / {result.margin_high:,.2f}",
        f"- 同一防御口径下的原始 Max Hit：{result.max_raw_hit:,.0f}",
    ]
    if row["tooltip"]:
        lines.append(f"- PoB 技能说明原文：{row['tooltip']}")
    lines += [
        "",
        "> “不会耗尽池子”只描述这一记已建模命中；等于池子按耗尽处理。",
        f"> 边界：{_SURVIVAL_BOUNDARY}。",
        f"> Boss 技能来源：[Path of Building]({row['pob_url']})。",
    ]
    return "\n".join(lines)


@REGISTRY.register(
    name="boss_skill_damage",
    description=(
        "查 Boss 技能在指定敌人等级下打多少伤害、穿多少抗性，以及 Uber 变体的差异。"
        "问「谢勒的球打多少伤害」「Uber 阿兹里的火焰爆破多疼」时用。"
        "未指定掷骰区间时给**区间两端**，不挑一个点。"
        "**只给伤害建模，不给技能机制与应对打法**（那一维未采集）；"
        "数字是 Path of Building 的建模值，GGG 未公布这些系数。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "minLength": 2,
                "description": "技能名（英文），如 Shaper Ball、Atziri Flameblast、Maven Fireball。",
            },
            "level": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "敌人等级。终局 Boss 常见 83..85；**必填，不替用户假定**。",
            },
            "uber": {"type": "boolean", "description": "是否 Uber 变体，默认否。"},
            "roll_range": {
                "type": "number",
                "minimum": 0,
                "maximum": 100,
                "description": "伤害掷骰区间 0..100。不给就返回两端。",
            },
        },
        "required": ["skill", "level"],
        "additionalProperties": False,
    },
)
def boss_skill_damage(
    skill: str,
    level: int,
    uber: bool = False,
    roll_range: float | None = None,
) -> ToolResult:
    name = (skill or "").strip()
    if len(name) < 2:
        return ToolResult(ok=False, error="skill 至少需要两个字符")
    conn = dbmod.get_conn()
    rows = _rows(conn, name)
    if not rows:
        available = [
            str(r[0]) for r in conn.execute("SELECT name FROM pob_boss_skill ORDER BY name")
        ]
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": f"PoB 的 Boss 技能预设里没有「{name}」",
                # 只有 10 条，全列出来比让模型猜好
                "available": available,
                "boundary": _BOUNDARY,
            },
        )
    if len(rows) > 1:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": f"「{name}」匹配到 {len(rows)} 条，请指明是哪一条",
                "candidates": [str(r["name"]) for r in rows],
                "boundary": _BOUNDARY,
            },
        )

    row = rows[0]
    try:
        text = _render(row, int(level), bool(uber), roll_range)
    except boss_damage.BossDamageError as exc:
        return ToolResult(ok=False, error=str(exc))

    return ToolResult(
        ok=True,
        data={
            "found": True,
            "skill": str(row["name"]),
            "damage_type": str(row["damage_type"]),
            "level": int(level),
            "uber": bool(uber),
            "has_uber_variant": row["uber_damage_mult"] is not None,
            "boundary": _BOUNDARY,
        },
        citations=[_citation(row, text)],
        meta={"channel": "derived", "table": "pob_boss_skill"},
    )


@REGISTRY.register(
    name="boss_survival_check",
    description=(
        "把 PoB 的 Boss 技能单次原始伤害与角色明确提供的防御池、抗性、护甲和承受倍率"
        "串起来，判断整个伤害掷骰区间都不耗尽池子、取决于掷骰，还是整个区间都会耗尽。"
        "元素穿透取自 Boss 技能预设。只支持一次单伤害类型命中；持续伤害、多击序列、"
        "伤害转化、暴击、格挡/闪避/压制、守护技能、Ward、MoM、恢复与打法均不在结论内。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string", "minLength": 2,
                "description": "PoB Boss 技能英文名，如 Shaper Ball、Maven Fireball。",
            },
            "level": {"type": "integer", "minimum": 1, "maximum": 100},
            "pool": {
                "type": "number", "exclusiveMinimum": 0,
                "description": "这一次命中可消耗的生命 + 能量护盾池。",
            },
            "uber": {"type": "boolean", "default": False},
            "roll_range": {
                "type": "number", "minimum": 0, "maximum": 100,
                "description": "可选固定掷骰点；省略时比较 0%..100% 整个区间。",
            },
            "fire_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "cold_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "lightning_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "chaos_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "armour": {"type": "number", "minimum": 0, "default": 0},
            "flat_physical_reduction_pct": {
                "type": "number", "minimum": 0, "maximum": 90, "default": 0,
            },
            "taken_mult": {
                "type": "number", "exclusiveMinimum": 0, "default": 1,
                "description": "受到伤害乘算修正；0.8 表示少受 20%。",
            },
            "overwhelm_pct": {
                "type": "number", "minimum": 0, "maximum": 100, "default": 0,
            },
        },
        "required": ["skill", "level", "pool"],
        "additionalProperties": False,
    },
)
def boss_survival_check(
    skill: str,
    level: int,
    pool: float,
    uber: bool = False,
    roll_range: float | None = None,
    fire_resist_pct: float | None = None,
    cold_resist_pct: float | None = None,
    lightning_resist_pct: float | None = None,
    chaos_resist_pct: float | None = None,
    armour: float = 0.0,
    flat_physical_reduction_pct: float = 0.0,
    taken_mult: float = 1.0,
    overwhelm_pct: float = 0.0,
) -> ToolResult:
    name = (skill or "").strip()
    if len(name) < 2:
        return ToolResult(ok=False, error="skill 至少需要两个字符")
    if pool <= 0:
        return ToolResult(ok=False, error="pool 必须大于 0")

    rows = _rows(dbmod.get_conn(), name)
    if not rows:
        return ToolResult(ok=True, data={
            "found": False,
            "assessable": False,
            "reason": f"PoB 的 Boss 技能预设里没有「{name}」",
            "boundary": _SURVIVAL_BOUNDARY,
            "answer": _unknown_survival_answer(
                name, "没有匹配的 PoB Boss 技能预设", level=level, uber=uber, pool=pool
            ),
        })
    if len(rows) > 1:
        candidates = [str(row["name"]) for row in rows]
        return ToolResult(ok=True, data={
            "found": False,
            "assessable": False,
            "reason": f"「{name}」匹配到 {len(rows)} 条，请指明技能",
            "candidates": candidates,
            "boundary": _SURVIVAL_BOUNDARY,
            "answer": _unknown_survival_answer(
                name, f"匹配到多条：{'、'.join(candidates)}", level=level, uber=uber, pool=pool
            ),
        })

    row = rows[0]
    mults = json.loads(row["damage_mults"])
    unknown_reason = ""
    if str(row["damage_type"]) == "DamageOverTime":
        unknown_reason = "该预设是持续伤害，不能用单次命中 Max Hit 判断"
    elif len(mults) != 1:
        unknown_reason = "该预设含多个伤害类型，当前闭式只支持单一伤害类型且不做转化"
    elif float(row["crit_chance"] or 0) > 0:
        unknown_reason = "该预设有非零暴击率，当前没有足够字段计算暴击命中区间"

    if unknown_reason:
        answer = _unknown_survival_answer(
            str(row["name"]), unknown_reason, level=level, uber=uber, pool=pool
        )
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "assessable": False,
                "verdict": "unknown",
                "skill": str(row["name"]),
                "reason": unknown_reason,
                "boundary": _SURVIVAL_BOUNDARY,
                "answer": answer,
            },
            citations=[_citation(row, answer)],
            meta={"channel": "derived", "table": "pob_boss_skill", "verdict": "unknown"},
        )

    dtype, pair = next(iter(mults.items()))
    resist_by_type = {
        "Fire": fire_resist_pct,
        "Cold": cold_resist_pct,
        "Lightning": lightning_resist_pct,
        "Chaos": chaos_resist_pct,
        "Physical": 0.0,
    }
    if dtype not in resist_by_type:
        unknown_reason = f"当前不支持伤害类型 {dtype}"
    elif resist_by_type[dtype] is None:
        unknown_reason = f"缺少{_DAMAGE_TYPE_ZH.get(dtype, dtype)}抗性，不能替用户假定"
    if unknown_reason:
        answer = _unknown_survival_answer(
            str(row["name"]), unknown_reason, level=level, uber=uber, pool=pool
        )
        return ToolResult(
            ok=True,
            data={
                "found": True, "assessable": False, "verdict": "unknown",
                "skill": str(row["name"]), "reason": unknown_reason,
                "missing_inputs": [f"{_DAMAGE_TYPE_ZH.get(dtype, dtype)}抗性"],
                "boundary": _SURVIVAL_BOUNDARY, "answer": answer,
            },
            citations=[_citation(row, answer)],
            meta={"channel": "derived", "table": "pob_boss_skill", "verdict": "unknown"},
        )

    try:
        uber_mult = row["uber_damage_mult"] if uber else None
        if roll_range is None:
            low, high = boss_damage.hit_range(
                level=int(level), base_mult=float(pair[0]), roll_mult=float(pair[1]),
                damage_type=dtype, uber_mult=uber_mult,
            )
        else:
            hit = boss_damage.hit_damage(
                level=int(level), base_mult=float(pair[0]), roll_mult=float(pair[1]),
                damage_type=dtype, roll_range=float(roll_range), uber_mult=uber_mult,
            )
            low = high = hit
        penetrations = json.loads(
            row["uber_penetrations"] if uber else row["penetrations"]
        )
        penetration = float(penetrations.get(f"{dtype}Pen") or 0)
        result = boss_survival.assess_single_hit(
            damage_type=dtype,
            raw_low=low.damage,
            raw_high=high.damage,
            pool=float(pool),
            resist_pct=float(resist_by_type[dtype] or 0),
            penetration_pct=penetration,
            armour=float(armour),
            flat_physical_reduction_pct=float(flat_physical_reduction_pct),
            taken_mult=float(taken_mult),
            overwhelm_pct=float(overwhelm_pct),
        )
    except (
        boss_damage.BossDamageError,
        boss_survival.BossSurvivalError,
        constants.MissingConstant,
        constants.ConstantTypeError,
    ) as exc:
        return ToolResult(ok=False, error=str(exc))

    answer = _render_survival(
        row, result, level=int(level), uber=bool(uber), roll_range=roll_range,
        armour=float(armour),
        flat_physical_reduction_pct=float(flat_physical_reduction_pct),
        taken_mult=float(taken_mult),
    )
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "assessable": True,
            "verdict": result.outcome,
            "skill": str(row["name"]),
            "level": int(level),
            "uber": bool(uber),
            "roll_range": roll_range,
            **result.as_dict(),
            "boundary": _SURVIVAL_BOUNDARY,
            "answer": answer,
        },
        citations=[_citation(row, answer), _maxhit_citation(answer)],
        meta={
            "channel": "derived",
            "tables": ["pob_boss_skill", "game_constant"],
            "verdict": result.outcome,
        },
    )
