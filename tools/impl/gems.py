"""宝石逐级数值查询。

与 `tools/impl/mods.py` 的 `mod_tiers` 同一个分工：**卡片只做语义检索入口，精确的
逐级数值直查表**（见 `kb/render_skills.py` 的模块说明）。一颗宝石有 20–40 个等级，
放进卡片会被出口的 1200 字截断切掉尾部，而被截掉的等级看起来就像不存在。

本工具只输出**字面值**。技能的伤害数值不在库里（PoB 存的是乘数，要按它的公式算），
所以这里也不会有——问伤害的问题在进模型之前就已经被维度闸门改判为缺据
（`kb/coverage.py` 的 `missing_dimension_hints`）。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY, Source
from kb.render_skills import COLOR_LABEL, KIND_LABEL, level_line
from tools.registry import REGISTRY, ToolResult

#: 一次最多列几个等级。超出时**必须写出还剩多少**——证据在出口按字数截断，
#: 不声明的话模型会把截断处当成表的结尾，然后说「这颗宝石只有 12 级」。
MAX_LEVEL_ROWS = 12
#: 名字匹配最多返回几颗宝石。宝石名重合度不像词缀那么高，超过这个数说明查询词太泛。
MAX_GEMS = 3
MAX_EVIDENCE_CHARS = 1200


def _level_dict(row: sqlite3.Row) -> dict[str, Any]:
    """等级行 → `render_skills.level_line` 认识的字典。

    复用同一个渲染函数而不是在这里另写一份：卡片上的「消耗 23 魔力」与工具里的
    必须逐字一致，否则出口的数值回填校验会在两种措辞之间对不上。
    """
    return {
        "gem_level": row["gem_level"],
        "level_requirement": row["level_requirement"],
        "cost": json.loads(row["cost"] or "{}"),
        "cooldown": row["cooldown"],
        "stored_uses": row["stored_uses"],
        "crit_chance": row["crit_chance"],
        "mana_multiplier": row["mana_multiplier"],
        "attack_speed_multiplier": row["attack_speed_multiplier"],
        "reservation_percent": row["reservation_percent"],
        "reservation_flat": row["reservation_flat"],
    }


def _render_gem(gem: sqlite3.Row, levels: list[sqlite3.Row], total: int) -> str:
    name = f"{gem['name_zh']}（{gem['name_en']}）"
    kind = KIND_LABEL.get(gem["gem_kind"], "宝石")
    out = [f"### {name}", ""]
    head = [kind, COLOR_LABEL.get(gem["attr_color"], "颜色未知")]
    if gem["is_vaal"]:
        head.append("瓦尔宝石")
    if gem["is_transfigured"]:
        head.append("蜕变宝石")
    out.append(" ｜ ".join(head))
    tags_zh = json.loads(gem["tags_zh"] or "[]")
    if tags_zh:
        out.append(f"标签：{'、'.join(tags_zh)}")
    spec = [f"自然最高 {gem['natural_max_level']} 级"]
    if gem["cast_time"] is not None:
        spec.insert(0, f"施放时间 {float(gem['cast_time']):g} 秒")
    out.append(" ｜ ".join(spec))
    out.append("")
    out.append(f"逐级数值（本库共 {total} 级）：")
    for row in levels:
        line = level_line(_level_dict(row))
        suffix = ""
        if row["damage_effectiveness"] is not None:
            # 这是**附加伤害的加成系数**，不是技能伤害。PoB 存 2.3、游戏显示 230%
            # （口径见 GemTooltip.lua）。标签写全名，免得被当成「伤害数值」。
            suffix = f"，附加伤害的效果 {float(row['damage_effectiveness']) * 100:g}%"
        out.append(f"- {row['gem_level']} 级：{line}{suffix}" if line else f"- {row['gem_level']} 级{suffix}")
    if total > len(levels):
        out.append("")
        out.append(
            f"（本库共 {total} 级，以上仅列 {len(levels)} 级；"
            "指定 level 参数可直接取某一级）"
        )
    out.append("")
    out.append(
        "本库未采集该技能的伤害数值、效果描述与属性需求（力/敏/智）——"
        "这些不在上表里，不要从上表推断。"
    )
    out.append("")
    out.append(f"来源：{gem['source']} {gem['source_file']}")
    return "\n".join(out)


def _citation(gem: sqlite3.Row, text: str) -> dict[str, Any]:
    priority = int(gem["source_priority"])
    return {
        "chunk_id": f"skill_gem_levels:{gem['gem_id']}",
        "entity_id": None,
        "title": f"{gem['name_zh']}（{gem['name_en']}）逐级数值",
        # 数值出自 PoB 的那个文件，引用就指向它的钉 commit 地址；
        # 中文名对照页另放在 data 里，不混进这条引用的 url
        "url": gem["pob_url"] or "",
        "source": gem["source"] or Source.POB.value,
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "structured",
    }


@REGISTRY.register(
    name="skill_gem_levels",
    description=(
        "查技能石 / 辅助宝石的**逐级字面数值**：每级的角色等级需求、消耗、"
        "消耗与保留倍率、冷却时间与可存次数、暴击率。问「某宝石 20 级需要多少魔力 / "
        "几级能用 / 冷却多久 / 满级要多少等级」时用它，**不要用 kb_search**——"
        "这个工具直查结构化宝石表，逐级精确；kb_search 只能命中卡片，且卡片只列三个"
        "关键等级。\n"
        "本库**没有技能的伤害数值、效果描述与属性需求**，返回里也不会有，不得从"
        "返回内容推断这些。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "gem_query": {
                "type": "string",
                "description": "宝石名，中文或英文，如「冰霜新星」「Ice Nova」「受伤时施放」",
            },
            "level": {
                "type": "integer",
                "description": "可选，只看某一级（1-40）。不填则按 1 级、自然最高级"
                "与数据上限附近取样",
            },
        },
        "required": ["gem_query"],
    },
)
def skill_gem_levels(gem_query: str, level: int | None = None) -> ToolResult:
    conn = dbmod.get_conn()
    q = (gem_query or "").strip()
    if not q:
        return ToolResult(ok=False, error="gem_query 不能为空")

    gems = conn.execute(
        """
        SELECT * FROM pob_skill_gem
        WHERE name_zh = ? OR name_en = ? COLLATE NOCASE
           OR name_zh LIKE ? OR name_en LIKE ? COLLATE NOCASE
           OR name_zh_alt LIKE ?
        ORDER BY (name_zh = ?) DESC, (name_en = ? COLLATE NOCASE) DESC,
                 length(name_zh), name_zh
        """,
        (q, q, f"%{q}%", f"%{q}%", f'%"{q}"%', q, q),
    ).fetchall()
    if not gems:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": f"宝石表里没有匹配「{q}」的技能石或辅助宝石",
                "hint": "换用宝石的中文全名或英文名；蜕变宝石要写全名（如「怒火之劈砍」）",
            },
        )

    citations: list[dict[str, Any]] = []
    picked: list[dict[str, Any]] = []
    for gem in gems[:MAX_GEMS]:
        rows = conn.execute(
            "SELECT * FROM pob_skill_level WHERE gem_id = ? ORDER BY gem_level",
            (gem["gem_id"],),
        ).fetchall()
        if not rows:
            continue
        total = len(rows)
        if level is not None:
            wanted = [r for r in rows if int(r["gem_level"]) == int(level)]
            if not wanted:
                available = f"{rows[0]['gem_level']}-{rows[-1]['gem_level']}"
                return ToolResult(
                    ok=True,
                    data={
                        "found": False,
                        "reason": f"{gem['name_zh']} 在本库里没有 {level} 级的数据",
                        "available_levels": available,
                    },
                )
            shown = wanted
        else:
            shown = _sample_levels(rows, int(gem["natural_max_level"] or 0))
        citations.append(_citation(gem, _render_gem(gem, shown, total)))
        picked.append({
            "name_zh": gem["name_zh"],
            "name_en": gem["name_en"],
            "gem_kind": gem["gem_kind"],
            "levels_total": total,
            "levels_shown": len(shown),
            "poedb_url": gem["poedb_url"],
        })

    if not citations:
        return ToolResult(
            ok=True,
            data={"found": False, "reason": f"「{q}」匹配到宝石但没有任何等级数据"},
        )

    return ToolResult(
        ok=True,
        data={
            "found": True,
            "gems": picked,
            "gems_matched": len(gems),
            "gems_returned": len(citations),
            "truncated": len(gems) > len(citations),
            # 字段名用 `boundary` 而不是自造一个：`orchestrator/loop._NOTE_FIELDS`
            # 已经在做「这类边界声明由代码逐字附在答案末尾」这件事
            #（metamods / bench / craft_pool 三个工具都走它）。
            # 让模型自己说这句话就会被出口闸门拦掉——那是同一个模式的第五次。
            "boundary": (
                "只给每级字面值；不提供技能伤害数值、效果描述文本、"
                "属性需求（力/敏/智）与获取途径"
            ),
        },
        citations=citations,
        meta={"channel": "structured", "table": "pob_skill_level"},
    )


def _sample_levels(rows: list[sqlite3.Row], natural_max: int) -> list[sqlite3.Row]:
    """未指定等级时取样：1 级、自然最高级、数据上限，再按需补中间档到上限条数。

    取样而不是全给：40 级全列会被出口截断，而截断处会被当成「宝石只有这么多级」。
    真正要逐级看的问法会带 level 参数。
    """
    by_level = {int(r["gem_level"]): r for r in rows}
    keys = sorted(by_level)
    picks = [keys[0]]
    for candidate in (natural_max, keys[-1]):
        if candidate in by_level and candidate not in picks:
            picks.append(candidate)
    # 中间档按等距补，让「升级收益」类问题也有几个可比的点
    for key in keys:
        if len(picks) >= MAX_LEVEL_ROWS:
            break
        if key % 5 == 0 and key not in picks:
            picks.append(key)
    return [by_level[k] for k in sorted(picks)]
