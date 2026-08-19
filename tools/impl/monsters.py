"""幽魂可用怪物的倍率查询。

这张表没有卡片（理由见 `kb/render_monsters.py`），所以它是**只能靠工具到达**的能力。
可达性由 `orchestrator/loop._PREFERRED_TOOL_INTENTS` 里的「幽魂」意图词负责：
不挂上去它就是一个死了的能力——元词缀矩阵在库里躺了很久都没人调用过，教训在那。

## 本轮刻意不做倍率排名

「生命倍率最高的幽魂是哪个」答不了，而这是个**数据边界**不是漏做：POEDB 这张表里
混着开发用怪物（「测试之灵」「元素反弹之灵」都是 10000%/10000%），把它们排在第一位
虽然有出处，但对玩家是错答案。要排名得先能区分「实际可用」——PoB 的
`Data/Spectres.lua` 有一份 268 条的确认清单（还带四抗性与技能列表），实测按
slug→英文名 能与本表命中 297 行。接它是下一轮的事，接上之前不给排名。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

#: 同名怪物最多列几条。实测 230 个名字对应多条（不同区域版本的同名怪物），
#: 最多的「恶魔」有 27 条。超出时**必须写出还剩多少**——静默截断等同于漏答。
MAX_ROWS = 12
MAX_EVIDENCE_CHARS = 1200

_BOUNDARY = (
    "只给页面上的伤害% / 生命%；不提供抗性、技能、掉落，也不做倍率排名"
    "（本库尚未区分开发用怪物与实际可用幽魂）"
)


def _render(name: str, rows: list[sqlite3.Row], total_in_library: int) -> str:
    out = [f"### {name}（可作幽魂的怪物）", ""]
    out.append(f"本库共收录 {total_in_library} 个可作幽魂的怪物；「{name}」命中 {len(rows)} 条。")
    out.append("")
    out.append("| 中文名 | 英文标识 | 伤害% | 生命% |")
    out.append("| --- | --- | --- | --- |")
    for row in rows[:MAX_ROWS]:
        out.append(
            f"| {row['name_zh']} | {row['poedb_slug']} | "
            f"{row['damage_pct']}% | {row['life_pct']}% |"
        )
    if len(rows) > MAX_ROWS:
        out.append("")
        out.append(
            f"（命中 {len(rows)} 条，以上仅列前 {MAX_ROWS} 条；"
            "同名怪物是不同区域版本，倍率各不相同，请用更具体的名字）"
        )
    out.append("")
    out.append("倍率以 100% 为基础值。" + _BOUNDARY + "。")
    out.append("")
    out.append(f"来源：POEDB {rows[0]['url']}")
    return "\n".join(out)


@REGISTRY.register(
    name="spectre_stats",
    description=(
        "查某个怪物**作为幽魂**的伤害% 与生命%（POEDB 的可作幽魂怪物清单）。"
        "问「XX 能不能当幽魂 / XX 的幽魂倍率是多少 / 幽魂生命倍率」时用它。\n"
        "本库只有这两个倍率：**没有抗性、技能、掉落**，也**不支持倍率排名**"
        "（清单里混着开发用怪物，无法区分实际可用），不得据此回答「最强幽魂是哪个」。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "monster_name": {
                "type": "string",
                "description": "怪物名，中文或英文标识，如「不朽精锐」「Abhorred_Elitist」",
            },
        },
        "required": ["monster_name"],
    },
)
def spectre_stats(monster_name: str) -> ToolResult:
    conn = dbmod.get_conn()
    name = (monster_name or "").strip()
    if not name:
        return ToolResult(ok=False, error="monster_name 不能为空")

    total = int(conn.execute("SELECT count(*) FROM poedb_spectre").fetchone()[0])
    rows = conn.execute(
        """
        SELECT * FROM poedb_spectre
        WHERE name_zh = ? OR poedb_slug = ? COLLATE NOCASE
           OR name_zh LIKE ? OR poedb_slug LIKE ? COLLATE NOCASE
        ORDER BY (name_zh = ?) DESC, life_pct DESC, name_zh
        """,
        (name, name, f"%{name}%", f"%{name.replace(' ', '_')}%", name),
    ).fetchall()
    if not rows:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": f"可作幽魂的怪物清单里没有匹配「{name}」的条目",
                "spectres_in_library": total,
                # 「不在清单里」有两种可能，必须说清楚，不能让模型二选一地猜
                "hint": "可能是这个怪物不能作幽魂，也可能是名字写法与 POEDB 不同；"
                        "本库无法区分这两种情况",
                "boundary": _BOUNDARY,
            },
        )

    text = _render(name, rows, total)
    priority = int(rows[0]["source_priority"])
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "matched": len(rows),
            "returned": min(len(rows), MAX_ROWS),
            "truncated": len(rows) > MAX_ROWS,
            "spectres_in_library": total,
            "boundary": _BOUNDARY,
        },
        citations=[{
            "chunk_id": f"spectre_stats:{rows[0]['variety_key']}",
            "entity_id": None,
            "title": f"{rows[0]['name_zh']}（幽魂倍率）",
            "url": rows[0]["url"],
            "source": rows[0]["source"],
            "source_priority": priority,
            "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
            "text": text[:MAX_EVIDENCE_CHARS],
            "channel": "structured",
        }],
        meta={"channel": "structured", "table": "poedb_spectre"},
    )
