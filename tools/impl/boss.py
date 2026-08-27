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

from calc import boss_damage
from calc.derive import render_steps
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
