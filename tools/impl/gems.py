"""宝石逐级数值查询。

与 `tools/impl/mods.py` 的 `mod_tiers` 同一个分工：**卡片只做语义检索入口，精确的
逐级数值直查表**（见 `kb/render_skills.py` 的模块说明）。一颗宝石有 20–40 个等级，
放进卡片会被出口的 1200 字截断切掉尾部，而被截掉的等级看起来就像不存在。

本工具输出三类数据，出处不同、界限要写清楚：

* **每级字面值**（等级需求、消耗、冷却、暴击率…）与**基础伤害**——PoB 侧；
* **效果文本与属性需求**——POEDB 详情页的逐级表（第三十二轮入库，逐级与 PoB
  对过账才入库；对不上的那几颗**这里就是没有**，会逐颗写明而不是笼统说未采集）；
* 仍然没有的：**DPS**（要攻速与整条加成链）与**获取途径**。

问 DPS 与获取途径的问法在进模型之前就被维度闸门改判为缺据
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
        "base_damage_min": row["base_damage_min"],
        "base_damage_max": row["base_damage_max"],
        "damage_type": row["damage_type"],
    }


def _poedb_levels(conn, gem_id: str) -> dict[int, dict[str, Any]]:  # noqa: ANN001
    """POEDB 侧的每级属性需求与效果取值。没有就是没有（那一颗没入库）。"""
    return {
        int(r["gem_level"]): {
            "req_str": r["req_str"], "req_dex": r["req_dex"], "req_int": r["req_int"],
            "stat_values": json.loads(r["values_json"] or "{}"),
        }
        for r in conn.execute(
            "SELECT gem_level, req_str, req_dex, req_int, values_json "
            "FROM poedb_gem_stat_level WHERE gem_id = ?", (gem_id,)
        )
    }


def _poedb_stat_lines(conn, gem_id: str) -> list[dict[str, Any]]:  # noqa: ANN001
    return [
        {"index": int(r["stat_index"]), "text_zh": r["text_zh"]}
        for r in conn.execute(
            "SELECT stat_index, text_zh FROM poedb_gem_stat WHERE gem_id = ? "
            "ORDER BY stat_index", (gem_id,)
        )
    ]


def _poedb_quests(conn, gem_id: str) -> list[dict[str, Any]]:  # noqa: ANN001
    """获取途径（第四十三轮）。没有行有两种含义，由调用方按整表是否为空区分。"""
    return [
        {
            "act": int(r["act"]), "quest_zh": r["quest_zh"],
            "source_kind": r["source_kind"], "npc_zh": r["npc_zh"] or None,
            "classes": json.loads(r["classes_json"] or "[]"),
        }
        for r in conn.execute(
            "SELECT act, quest_zh, source_kind, npc_zh, classes_json "
            "FROM poedb_gem_quest WHERE gem_id = ? ORDER BY act, quest_zh, source_kind",
            (gem_id,),
        )
    ]


def _poedb_fixed(conn, gem_id: str) -> list[str]:  # noqa: ANN001
    """固定效果（第四十四轮）：与等级无关的技能机制。空列表就是这颗技能没有。"""
    return [
        str(r["text_zh"])
        for r in conn.execute(
            "SELECT text_zh FROM poedb_gem_fixed_stat WHERE gem_id = ? "
            "ORDER BY stat_index", (gem_id,)
        )
    ]


def _quest_line(row: dict[str, Any]) -> str:
    kind = str(row.get("source_kind") or "")
    if kind == "vendor":
        label = f"向{row.get('npc_zh') or '商人'}购买"
    elif kind == "box":
        label = "开局箱子"
    else:
        label = "任务奖励"
    classes = "、".join(str(c) for c in (row.get("classes") or []))
    line = f"- 第 {row.get('act')} 章「{row.get('quest_zh')}」{label}"
    return f"{line}（{classes}）" if classes else line


def _render_gem(
    gem: sqlite3.Row, levels: list[sqlite3.Row], total: int, *,
    stat_lines: list[dict[str, Any]], poedb_levels: dict[int, dict[str, Any]],
    quests: list[dict[str, Any]], fixed_stats: list[str],
) -> str:
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
        merged = _level_dict(row)
        extra = poedb_levels.get(int(row["gem_level"])) or {}
        # 属性需求并进同一行：卡片与工具用同一个 `level_line`，措辞逐字一致
        merged.update({k: v for k, v in extra.items() if k != "stat_values"})
        line = level_line(merged)
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
    if stat_lines:
        out.append("")
        out.append("效果文本（POEDB 逐级表，含各列在上面这些等级的取值）：")
        for line_row in stat_lines:
            key = str(line_row["index"])
            marks = [
                f"{int(row['gem_level'])} 级 {(poedb_levels.get(int(row['gem_level'])) or {}).get('stat_values', {})[key]}"
                for row in levels
                if key in ((poedb_levels.get(int(row["gem_level"])) or {}).get("stat_values") or {})
            ]
            suffix = f"（{'｜'.join(marks)}）" if marks else ""
            out.append(f"- {line_row['text_zh']}{suffix}")
    # 固定效果（第四十四轮）：与等级无关，所以单列一段而不是塞进逐级数值里。
    if fixed_stats:
        out.append("")
        out.append("固定效果（与等级无关）：")
        out.extend(f"- {text}" for text in fixed_stats)

    # 获取途径（第四十三轮）。逐颗说真话：668 颗有使命表，152 颗没有。
    out.append("")
    if quests:
        out.append("获取途径（POEDB 详情页的使命表）：")
        out.extend(_quest_line(row) for row in quests)
        acquisition_note = "掉落来源未采集（上表只有任务奖励、商人与开局箱子）"
    else:
        acquisition_note = (
            "POEDB 详情页未给出这颗宝石的使命表，本库未采集它的获取途径"
        )
    out.append("")
    if stat_lines:
        out.append(f"本库未采集该技能的 DPS；{acquisition_note}——不要从上表推断。")
    else:
        # 逐颗说真话：819 颗有效果文本，这一颗没有。笼统说「本库未采集效果文本」
        # 会在另外 819 颗上变成假话，而那正是覆盖声明要避免的那种句子。
        out.append(
            "本库没有这颗宝石的效果文本与属性需求（POEDB 详情页未取到或与 PoB "
            f"逐级数值对不上），也未采集 DPS；{acquisition_note}"
            "——不要从上表推断这些。"
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
        "返回里还有**每级效果文本与属性需求（力/敏/智）**（出自 POEDB 详情页的逐级表）"
        "以及**基础伤害**。本库**没有 DPS 与获取途径**，返回里也不会有，"
        "不得从返回内容推断这两项。"
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
        stat_lines = _poedb_stat_lines(conn, gem["gem_id"])
        poedb_levels = _poedb_levels(conn, gem["gem_id"])
        quests = _poedb_quests(conn, gem["gem_id"])
        fixed_stats = _poedb_fixed(conn, gem["gem_id"])
        citations.append(_citation(gem, _render_gem(
            gem, shown, total, stat_lines=stat_lines, poedb_levels=poedb_levels,
            quests=quests, fixed_stats=fixed_stats)))
        picked.append({
            "name_zh": gem["name_zh"],
            "name_en": gem["name_en"],
            "gem_kind": gem["gem_kind"],
            "levels_total": total,
            "levels_shown": len(shown),
            "stat_lines": [line["text_zh"] for line in stat_lines],
            "requirements_by_level": {
                str(lv): {k: v for k, v in row.items()
                          if k.startswith("req_") and v is not None}
                for lv, row in sorted(poedb_levels.items())
                if any(v is not None for k, v in row.items() if k.startswith("req_"))
            },
            "acquisition": quests,
            "fixed_stats": fixed_stats,
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
                "每级字面值与基础伤害出自 PoB，效果文本、属性需求、固定效果与获取途径"
                "出自 POEDB 详情页（逐级两维与固定效果的冷却/魔力都对过账）；"
                "获取途径只含任务奖励、商人与开局箱子，**掉落来源与 DPS 不提供**"
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
