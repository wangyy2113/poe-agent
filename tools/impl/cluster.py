"""星团珠宝：尺寸与小点数、星团基石、以及「这个核心天赋是不是星团珠宝上的」。

## 这个工具补的是分类，不是属性

300 个星团核心天赋的**属性**早就在库里（它们随天赋树入库成了天赋卡，用别名就能查到）。
这里回答的是另一件事：**它只能从星团珠宝拿、不在主天赋树上**。
上一轮的 `passive_path` 对这些节点只能说「可能只存在于星团珠宝或升华子树」，
有了这份清单才能把「可能」去掉。

## 拿不到的那一维，明确说不知道

**哪些核心天赋出现在哪种小点类型上**——`ClusterJewels.lua` 只给「尺寸 → 类型」与一份
扁平的 308 条清单，树 JSON 的节点字段里也没有 affliction 标签。所以问「大星团珠宝的
斧剑伤害能开出哪些核心天赋」时，这个工具说不知道，而不是拿 308 条里的一部分去凑。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 1800
#: 小天赋类型最多列几条。超出写出还剩多少——静默截断等同于漏答。
MAX_SKILL_ROWS = 12

_SIZE_ZH = {"Small": "小型", "Medium": "中型", "Large": "大型"}
_SIZE_ALIAS = {"小": "Small", "中": "Medium", "大": "Large"}

_BOUNDARY = (
    "只给尺寸、小天赋数量与清单；**哪些核心天赋出现在哪种小天赋类型上未采集**"
    "（本地来源没有这个映射），也不提供掉落与价格"
)


def _size_of(query: str) -> str | None:
    for zh, size in _SIZE_ALIAS.items():
        if zh in query:
            return size
    for size in _SIZE_ZH:
        if size.lower() in query.lower():
            return size
    return None


def cluster_notable_row(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """按中文名或英文名查星团核心天赋。`passive_path` 也用它来给出精确理由。"""
    return conn.execute(
        "SELECT * FROM pob_cluster_notable WHERE name_zh = ? OR name_en = ? COLLATE NOCASE",
        (name, name),
    ).fetchone()


def _render_sizes(rows: list[sqlite3.Row], skills: dict[str, list[sqlite3.Row]]) -> str:
    out = ["### 星团珠宝的尺寸与可加小天赋数", ""]
    out.append("| 尺寸 | 可加小天赋 | 核心天赋位 | 珠宝插槽位 | 小点类型数 |")
    out.append("| --- | --- | --- | --- | --- |")
    for row in rows:
        size = str(row["size"])
        out.append(
            f"| {_SIZE_ZH.get(size, size)}（{size}） | {row['min_nodes']}–{row['max_nodes']} | "
            f"{row['notable_slots']} | {row['socket_slots']} | {len(skills.get(size, []))} |"
        )
    out.append("")
    # 「星团珠宝上的天赋要花几个天赋点」问的就是这件事：这些天赋**不在主天赋树上**，
    # 所以没有「从起点走过去要几点」这个数字。不写这句，答案就答不到问题上
    # （实测录制里模型只复述了尺寸表）。
    out.append(
        "星团珠宝上的核心天赋与基石**不在主天赋树上**，因此没有「从起点走过去要几个"
        "天赋点」这个数字：它们靠星团珠宝提供，而珠宝要插在天赋树的珠宝插槽里"
        "（那个插槽本身要几点，可以用 passive_path 算）。"
    )
    out.append("")
    out.append(_BOUNDARY + "。")
    out.append("")
    out.append(f"来源：Path of Building {rows[0]['pob_url']}")
    return "\n".join(out)


def _render_skills(size: str, rows: list[sqlite3.Row], size_row: sqlite3.Row | None) -> str:
    label = _SIZE_ZH.get(size, size)
    out = [f"### {label}星团珠宝可选的小天赋类型（{len(rows)} 种）", ""]
    if size_row is not None:
        out.append(
            f"{label}星团珠宝可加 {size_row['min_nodes']}–{size_row['max_nodes']} 个小天赋，"
            f"有 {size_row['notable_slots']} 个核心天赋位、{size_row['socket_slots']} 个珠宝插槽位。"
        )
        out.append("")
    out.append("| 类型（PoB 英文原文） | 小天赋给予（英文原文） |")
    out.append("| --- | --- |")
    for row in rows[:MAX_SKILL_ROWS]:
        stats = "；".join(json.loads(row["stats_en"] or "[]")) or "—"
        out.append(f"| {row['name_en']} | {stats} |")
    if len(rows) > MAX_SKILL_ROWS:
        out.append("")
        out.append(f"（共 {len(rows)} 种，以上列出前 {MAX_SKILL_ROWS} 种）")
    out.append("")
    out.append(
        "类型名与小天赋文本**只有英文**：POEDB 侧的中文附魔文本不在本库已缓存的任何一页上，"
        "所以按 PoB 原文给出，不自行翻译。"
    )
    out.append("")
    out.append(_BOUNDARY + "。")
    return "\n".join(out)


def _render_keystones(rows: list[sqlite3.Row]) -> str:
    out = [f"### 只能从星团珠宝获得的基石（{len(rows)} 个）", ""]
    out.append("| 中文名 | 英文名 |")
    out.append("| --- | --- |")
    for row in rows:
        out.append(f"| {row['name_zh'] or '—'} | {row['name_en']} |")
    out.append("")
    out.append("这些基石**不在主天赋树上**，用 `passive_path` 算不出「从起点走过去要几点」。")
    out.append("")
    out.append(_BOUNDARY + "。")
    return "\n".join(out)


def _citation(row: sqlite3.Row, title: str, text: str) -> dict[str, Any]:
    priority = int(row["source_priority"]) if "source_priority" in row.keys() else 2
    return {
        "chunk_id": f"cluster_jewel:{title}",
        "entity_id": None,
        "title": title,
        "url": str(row["pob_url"]) if "pob_url" in row.keys() else "",
        "source": str(row["source"]) if "source" in row.keys() else "Path of Building",
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "structured",
    }


@REGISTRY.register(
    name="cluster_jewel_info",
    description=(
        "查星团珠宝：各尺寸能加几个小天赋、有几个核心天赋位与珠宝插槽位、"
        "可选的小天赋类型，以及**只能从星团珠宝获得的基石**清单；"
        "也能判断某个核心天赋是不是星团珠宝上的（那些不在主天赋树上）。\n"
        "**哪些核心天赋出现在哪种小天赋类型上没有采集**，不得据此回答那类问题。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "问尺寸写「小型/中型/大型」；问某个核心天赋写它的名字，"
                "如「空明之掌」；问基石清单写「基石」",
            },
        },
        "required": ["query"],
    },
)
def cluster_jewel_info(query: str) -> ToolResult:
    conn = dbmod.get_conn()
    text_query = (query or "").strip()
    if not text_query:
        return ToolResult(ok=False, error="query 不能为空")

    sizes = conn.execute("SELECT * FROM pob_cluster_jewel ORDER BY min_nodes").fetchall()
    if not sizes:
        return ToolResult(
            ok=True, data={"found": False, "reason": "星团珠宝数据尚未导入"}
        )
    skills_by_size: dict[str, list[sqlite3.Row]] = {}
    for row in conn.execute("SELECT * FROM pob_cluster_skill ORDER BY size, name_en"):
        skills_by_size.setdefault(str(row["size"]), []).append(row)

    # 1) 具体核心天赋：这是把 passive_path 那句「可能」变成事实的那条路
    notable = cluster_notable_row(conn, text_query)
    if notable is not None:
        kind = "基石" if notable["is_keystone"] else "核心天赋"
        text = "\n".join([
            f"### {notable['name_zh'] or notable['name_en']}（星团珠宝{kind}）",
            "",
            f"这是**星团珠宝上的{kind}**（英文名 {notable['name_en']}），"
            "只能通过星团珠宝获得，**不在主天赋树上**——所以用 `passive_path` "
            "算不出「从起点走过去要几个天赋点」。",
            "",
            "它的属性可以按名字查天赋卡。星团珠宝本身要插在天赋树的珠宝插槽里，"
            "而**哪种小天赋类型能开出它，本库未采集**。",
            "",
            _BOUNDARY + "。",
            "",
            f"来源：Path of Building {notable['pob_url']}",
        ])
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "kind": "notable",
                "name_zh": notable["name_zh"],
                "name_en": notable["name_en"],
                "is_keystone": bool(notable["is_keystone"]),
                "on_main_tree": False,
                "boundary": _BOUNDARY,
            },
            citations=[_citation(notable, f"{notable['name_zh'] or notable['name_en']}（星团珠宝）", text)],
            meta={"channel": "structured", "table": "pob_cluster_notable"},
        )

    # 2) 基石清单
    if "基石" in text_query or "keystone" in text_query.lower():
        rows = conn.execute(
            "SELECT * FROM pob_cluster_notable WHERE is_keystone=1 ORDER BY name_zh"
        ).fetchall()
        return ToolResult(
            ok=True,
            data={"found": True, "kind": "keystones", "count": len(rows),
                  "boundary": _BOUNDARY},
            citations=[_citation(rows[0], "星团珠宝基石清单", _render_keystones(rows))],
            meta={"channel": "structured", "table": "pob_cluster_notable"},
        )

    # 3) 某个尺寸的小点类型
    size = _size_of(text_query)
    if size:
        rows = skills_by_size.get(size, [])
        size_row = next((r for r in sizes if str(r["size"]) == size), None)
        if rows and size_row is not None:
            return ToolResult(
                ok=True,
                data={"found": True, "kind": "size", "size": size,
                      "min_nodes": size_row["min_nodes"], "max_nodes": size_row["max_nodes"],
                      "skill_types": len(rows), "boundary": _BOUNDARY},
                citations=[_citation(size_row, f"{_SIZE_ZH[size]}星团珠宝",
                                     _render_skills(size, rows, size_row))],
                meta={"channel": "structured", "table": "pob_cluster_skill"},
            )

    # 4) 兜底：三个尺寸的总览
    return ToolResult(
        ok=True,
        data={"found": True, "kind": "overview", "sizes": len(sizes),
              "boundary": _BOUNDARY},
        citations=[_citation(sizes[0], "星团珠宝尺寸总览", _render_sizes(sizes, skills_by_size))],
        meta={"channel": "structured", "table": "pob_cluster_jewel"},
    )
