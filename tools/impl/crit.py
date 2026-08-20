"""暴击计算：暴击率、暴击加成，与它们合起来的平均伤害倍率。

## 为什么它能算，而 DPS 还不能

平均伤害倍率 =(1 − 暴击率)+ 暴击率 × 暴击加成 —— **这个数不需要知道这一击有多大**。
所以在「伤害数值刻意未采集」的前提下（`pob_skill_level` 没有伤害列），
暴击是攻击侧唯一能如实算出来的一块：它给的是倍率，不是 DPS。

## 基础值查库，不让用户报数

技能的每级基础暴击在 `pob_skill_level.crit_chance`（7,600 行）、
武器底材的 `CritChanceBase` 在 `pob_base_item.stats` 里。按名字查，并写明用了哪一级。

## 三条容易记错的都写进证据

1. **暴击率上限 100%**，不是 95%（PoB 给 `CritChanceCap` 播的基础值就是 100）。
2. **基础暴击加成 150%**（PoB 内部存 `+50`，减过 100）。
3. **「增加的暴击率」乘在基础暴击上**——基础越低，百分比加成越不值。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from calc import constants
from calc import crit as crit_calc_lib
from calc.derive import pob_url, render_steps
from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 3200
DEFAULT_GEM_LEVEL = 20
MAX_CANDIDATES = 8

_BOUNDARY = (
    "只算暴击率与**平均伤害倍率**（不需要伤害数值），不算 DPS——"
    "这一击有多大要 PoB 的 effectiveness 公式与整条攻击链，本库刻意没有伤害列；"
    "也不含幸运暴击、命中判定与暴击对异常的加成"
)


def _sources() -> list[str]:
    multi = constants.provenance(constants.BASE_CRIT_MULTIPLIER)
    url = multi.url
    return [
        f"- 暴击率公式（基础 + 定值）×(1 + 增加)× 更多、以及上限："
        f"{pob_url(url, crit_calc_lib.OFFENCE_FILE, lines=crit_calc_lib.CHANCE_LINES)}",
        f"- 平均伤害倍率 `CritEffect`："
        f"{pob_url(url, crit_calc_lib.OFFENCE_FILE, line=crit_calc_lib.EFFECT_LINE)}",
        f"- 暴击率上限 {crit_calc_lib.CRIT_CHANCE_CAP:g}%（硬编码，不在常量表）："
        f"{pob_url(url, crit_calc_lib.SETUP_FILE, line=crit_calc_lib.CAP_LINE)}",
        f"- 基础暴击加成 {constants.get(constants.BASE_CRIT_MULTIPLIER):g}%："
        f"{multi.key}（GGG 原始表 {multi.dat_source or '未标注'}）{multi.url}"
        f"；PoB 内部存成 +50 的那一行："
        f"{pob_url(url, crit_calc_lib.SETUP_FILE, line=crit_calc_lib.BASE_MULTI_LINE)}",
        "- 技能每级基础暴击：`pob_skill_level.crit_chance`；"
        "武器底材：`pob_base_item.stats.CritChanceBase`",
    ]


def _lookup_gem(conn: sqlite3.Connection, name: str, level: int) -> dict[str, Any]:
    text = (name or "").strip()
    rows = conn.execute(
        "SELECT gem_id, name_zh, name_en FROM pob_skill_gem "
        "WHERE name_zh = ? OR name_en = ? COLLATE NOCASE", (text, text),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT gem_id, name_zh, name_en FROM pob_skill_gem "
            "WHERE name_zh LIKE ? OR name_en LIKE ? COLLATE NOCASE "
            "ORDER BY length(name_zh) LIMIT ?",
            (f"%{text}%", f"%{text}%", MAX_CANDIDATES + 1),
        ).fetchall()
    if not rows:
        return {"ok": False, "reason": f"库里没有名为「{text}」的技能"}
    if len(rows) > 1:
        names = "、".join(f"{r['name_zh']}（{r['name_en']}）" for r in rows[:MAX_CANDIDATES])
        return {"ok": False, "reason": f"「{text}」对应多个技能：{names}。请写完整名字"}
    gem = rows[0]
    row = conn.execute(
        "SELECT gem_level, crit_chance FROM pob_skill_level "
        "WHERE gem_id = ? AND gem_level = ?", (gem["gem_id"], level),
    ).fetchone()
    used = level
    if row is None or row["crit_chance"] is None:
        row = conn.execute(
            "SELECT gem_level, crit_chance FROM pob_skill_level WHERE gem_id = ? "
            "AND crit_chance IS NOT NULL ORDER BY gem_level DESC LIMIT 1", (gem["gem_id"],),
        ).fetchone()
        if row is None:
            return {
                "ok": False,
                "reason": f"{gem['name_zh']}（{gem['name_en']}）在库里没有基础暴击率"
                          "——它可能本来就不造成击中",
            }
        used = int(row["gem_level"])
    return {
        "ok": True,
        "label": f"{gem['name_zh']}（{gem['name_en']}，{used} 级）",
        "base_chance": float(row["crit_chance"]),
        "gem_level": used, "requested_level": level,
    }


def _lookup_weapon(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    text = (name or "").strip()
    rows = conn.execute(
        "SELECT name_zh, name_en, stats FROM pob_base_item "
        "WHERE name_zh = ? OR name_en = ? COLLATE NOCASE", (text, text),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT name_zh, name_en, stats FROM pob_base_item "
            "WHERE name_zh LIKE ? OR name_en LIKE ? COLLATE NOCASE "
            "ORDER BY length(name_zh) LIMIT ?",
            (f"%{text}%", f"%{text}%", MAX_CANDIDATES + 1),
        ).fetchall()
    if not rows:
        return {"ok": False, "reason": f"库里没有名为「{text}」的底材"}
    if len(rows) > 1:
        names = "、".join(f"{r['name_zh']}" for r in rows[:MAX_CANDIDATES])
        return {"ok": False, "reason": f"「{text}」对应多个底材：{names}。请写完整名字"}
    stats = json.loads(rows[0]["stats"] or "{}")
    base = stats.get("CritChanceBase")
    if base is None:
        return {
            "ok": False,
            "reason": f"{rows[0]['name_zh']} 不是武器（没有基础暴击率）",
        }
    return {
        "ok": True,
        "label": f"{rows[0]['name_zh']}（{rows[0]['name_en']}）",
        "base_chance": float(base),
    }


def _render_rules() -> str:
    cap = crit_calc_lib.CRIT_CHANCE_CAP
    multi = constants.get(constants.BASE_CRIT_MULTIPLIER)
    return "\n".join([
        "### 暴击怎么算", "",
        "> 暴击率 =（基础暴击 + 定值加值）×(1 + 增加的暴击率/100)× 更多的暴击率",
        "",
        f"再夹在 **{cap:g}%** 上限之内——**不是社区常说的 95%**"
        f"（PoB 给 `CritChanceCap` 播的基础值就是 {cap:g}）。",
        "",
        f"暴击加成基础 **{multi:g}%**（PoB 内部存成 +50，减过 100）。两者合起来：",
        "",
        "> 平均伤害倍率 =(1 − 暴击率)+ 暴击率 × 暴击加成",
        "",
        "所以 40% 暴击 + 350% 加成 ⇒ 0.6 + 0.4 × 3.5 = **2.0**，平均伤害翻倍。",
        "",
        "**「增加的暴击率」是乘在基础暴击上的**，所以基础越低、百分比加成越不值："
        "6% 基础配 +300% 只有 24%，而 7.5% 基础配同样的加成有 30%。"
        "这就是「哪种武器适合暴击流」的算术来源。",
        "",
        "给出技能名或武器底材名，可以直接查它的基础暴击率再算。",
        "",
        _BOUNDARY + "。",
        "",
        "来源：",
        *_sources(),
    ])


def _citation(title: str, text: str, url: str) -> dict[str, Any]:
    provenance = constants.provenance(constants.BASE_CRIT_MULTIPLIER)
    priority = int(provenance.source_priority)
    return {
        "chunk_id": f"crit_calc:{title}",
        "entity_id": None,
        "title": title,
        "url": url,
        "source": provenance.source,
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "derived",
    }


@REGISTRY.register(
    name="crit_calc",
    description=(
        "算暴击：暴击率（基础 + 定值）×(1 + 增加)× 更多、夹在 100% 上限内，"
        "以及**平均伤害倍率** =(1 − 暴击率)+ 暴击率 × 暴击加成。"
        "基础暴击可以按**技能名或武器底材名**从库里查，不用用户报数。\n"
        "**只给倍率不给 DPS**：这一击有多大要 effectiveness 公式与整条攻击链，本库没有伤害列。\n"
        "问题没给任何数值也没给名字时只讲规则。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {"type": "string", "description": "技能名（查它的每级基础暴击率）"},
            "weapon": {"type": "string", "description": "武器底材名（查它的基础暴击率）"},
            "gem_level": {
                "type": "integer", "description": f"宝石等级，默认 {DEFAULT_GEM_LEVEL}",
            },
            "base_chance": {
                "type": "number", "description": "直接给基础暴击率（给了就不查库）",
            },
            "flat_chance": {"type": "number", "description": "「+X% 暴击率」定值加值"},
            "increased_pct": {"type": "number", "description": "「暴击率提高」百分比之和"},
            "more_mult": {"type": "number", "description": "「更多暴击率」连乘，默认 1"},
            "multi_pct": {
                "type": "number",
                "description": "暴击加成总值（如 350 表示 350%）。不给就用基础 150%",
            },
            "extra_multi_pct": {
                "type": "number", "description": "在基础 150% 之上增加的暴击加成",
            },
        },
        "required": [],
    },
)
def crit_calc(
    skill: str = "",
    weapon: str = "",
    gem_level: int = DEFAULT_GEM_LEVEL,
    base_chance: float = 0.0,
    flat_chance: float = 0.0,
    increased_pct: float = 0.0,
    more_mult: float = 1.0,
    multi_pct: float | None = None,
    extra_multi_pct: float = 0.0,
) -> ToolResult:
    try:
        _sources()
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"暴击常量未入库：{exc}")

    rules_url = pob_url(
        constants.provenance(constants.BASE_CRIT_MULTIPLIER).url,
        crit_calc_lib.OFFENCE_FILE, lines=crit_calc_lib.CHANCE_LINES,
    )
    conn = dbmod.get_conn()
    source_label = ""
    notes: list[str] = []
    base = base_chance
    if not base and (skill.strip() or weapon.strip()):
        found = (
            _lookup_gem(conn, skill, gem_level) if skill.strip()
            else _lookup_weapon(conn, weapon)
        )
        if not found["ok"]:
            return ToolResult(
                ok=True,
                data={"found": False, "reason": found["reason"], "boundary": _BOUNDARY},
                citations=[_citation(
                    "暴击：名字没对上",
                    "\n".join([
                        "### 暴击：名字没对上", "", f"- {found['reason']}", "",
                        "基础暴击率按名字查库；名字不完整时列出候选，**不挑一个去算**"
                        "——那会得到一个带着推导表的错数字。", "",
                        _BOUNDARY + "。",
                    ]),
                    rules_url,
                )],
                meta={"channel": "derived", "table": "pob_skill_level"},
            )
        base = found["base_chance"]
        source_label = found["label"]
        if found.get("gem_level") and found["gem_level"] != found.get("requested_level"):
            notes.append(
                f"库里没有 {found['requested_level']} 级的基础暴击，按 {found['gem_level']} 级算"
            )

    if not base:
        return ToolResult(
            ok=True,
            data={
                "found": True, "kind": "rules",
                "crit_chance_cap": crit_calc_lib.CRIT_CHANCE_CAP,
                "base_multiplier": constants.get(constants.BASE_CRIT_MULTIPLIER),
                "boundary": _BOUNDARY,
            },
            citations=[_citation("暴击的算法与两个基数", _render_rules(), rules_url)],
            meta={"channel": "derived", "table": "game_constant"},
        )

    result = crit_calc_lib.crit(
        base, flat_chance=flat_chance, increased_pct=increased_pct,
        more_mult=more_mult, multi_pct=multi_pct, extra_multi_pct=extra_multi_pct,
    )
    out = [f"### 暴击结算{('（' + source_label + '）') if source_label else ''}", ""]
    if notes:
        out.extend(f"- {n}" for n in notes)
        out.append("")
    out.extend(render_steps(result.steps))
    out.append("")
    out.append(
        f"**暴击率 {result.chance:g}%、暴击加成 {result.multi_pct:g}% ⇒ "
        f"平均伤害倍率 {result.average_multiplier:.3f}**"
        f"（相当于比完全不暴击高 {result.damage_gain_pct:.1f}%）。"
    )
    if result.capped:
        out.append(
            f"暴击率已经顶到 {result.cap:g}% 上限（算出来是 {result.raw_chance:.2f}%），"
            "再堆暴击率不会更高——**上限是 100% 而不是 95%**。"
        )
    if multi_pct is None and not extra_multi_pct:
        out.append(
            f"暴击加成用的是基础值 {result.multi_pct:g}%——装备与天赋上的"
            "「暴击伤害加成」要加进来才是你的实际值。"
        )
    out.append("")
    out.append(_BOUNDARY + "。")
    out.append("")
    out.append("来源：")
    out.extend(_sources())
    return ToolResult(
        ok=True,
        data={
            "found": True, "kind": "result",
            "source": source_label,
            "base_chance": result.base_chance,
            "chance": round(result.chance, 2),
            "capped": result.capped,
            "multi_pct": result.multi_pct,
            "average_multiplier": round(result.average_multiplier, 4),
            "damage_gain_pct": round(result.damage_gain_pct, 2),
            "boundary": _BOUNDARY,
        },
        citations=[_citation(
            f"暴击 {result.chance:g}% × 加成 {result.multi_pct:g}% ⇒ "
            f"平均 {result.average_multiplier:.3f} 倍",
            "\n".join(out), rules_url,
        )],
        meta={"channel": "derived", "table": "pob_skill_level"},
    )
