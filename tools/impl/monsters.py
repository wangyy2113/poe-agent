"""幽魂查询：POEDB 的清单与倍率 + PoB 的抗性与建模系数。

这张表没有卡片（理由见 `kb/render_monsters.py`），所以它是**只能靠工具到达**的能力。
可达性由 `orchestrator/loop._PREFERRED_TOOL_INTENTS` 里的「幽魂」意图词负责：
不挂上去它就是一个死了的能力——元词缀矩阵在库里躺了很久都没人调用过，教训在那。

## 两个来源各答一半，不混

``poedb_spectre``  P1。**清单**（1948 个可作幽魂的怪物）与伤害% / 生命%
``pob_spectre``    P2。只建模 268 个，但多给**四抗性**、护甲/闪避/能量护盾系数、攻击间隔

两侧的生命/伤害是同一个量的不同单位（POEDB 120 ↔ PoB 1.2）。逐条一致的占 91%/94.8%，
对不上的已落 `source_conflict`：`kind='spectre'` 是真分歧（8 条）、
`kind='spectre_review'` 是粒度差异（43 条，POEDB 记得更细）。**真分歧并列展示**
（需求 15.5），粒度差异不当作冲突呈现。

## 排名只在 PoB 建模的那 268 个里做

POEDB 的 1948 条里混着开发用怪物（「测试之灵」10000%/10000%），拿它排名虽然有出处，
但对玩家是错答案。PoB 只建模玩家实际会用的，所以它同时是一份「确认可用」清单——
排名的依据换成它，并在答案里写明这个范围。上一轮把「不支持排名」写进覆盖声明，
就是在等这份清单。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from orchestrator.citation import conflict_block
from tools.registry import REGISTRY, ToolResult

#: 同名怪物最多列几条。实测 230 个名字对应多条（不同区域版本），最多的「恶魔」39 条。
#: 超出时**必须写出还剩多少**——静默截断等同于漏答。
MAX_ROWS = 12
#: 排名最多给几条。
MAX_RANK_ROWS = 10
MAX_EVIDENCE_CHARS = 1800

_BOUNDARY_BASE = "倍率与抗性按来源原值给出；不提供掉落、怪物技能的中文说明"
_BOUNDARY_RANK = "排名只在 PoB 建模的 268 个幽魂里做（POEDB 清单里另有未建模的，不参与排名）"


def _resist_line(row: sqlite3.Row) -> str:
    return (
        f"火 {row['fire_resist']}% / 冰 {row['cold_resist']}% / "
        f"电 {row['lightning_resist']}% / 混 {row['chaos_resist']}%"
    )


def _pob_by_name(conn: sqlite3.Connection, slugs: list[str]) -> dict[str, sqlite3.Row]:
    """按「slug 去下划线 == PoB 英文名」取 PoB 侧。同名多解的不取（配不准就不出结论）。"""
    if not slugs:
        return {}
    names = [s.replace("_", " ") for s in slugs]
    placeholders = ",".join("?" * len(names))
    out: dict[str, sqlite3.Row] = {}
    seen_twice: set[str] = set()
    for row in conn.execute(
        f"SELECT * FROM pob_spectre WHERE name_en IN ({placeholders})",  # noqa: S608 已参数化
        names,
    ):
        if row["name_en"] in out:
            seen_twice.add(row["name_en"])
            continue
        out[row["name_en"]] = row
    for name in seen_twice:
        out.pop(name, None)
    return out


def _conflicts_for(conn: sqlite3.Connection, slugs: list[str]) -> list[dict[str, Any]]:
    """取**真分歧**（`kind='spectre'`）。`spectre_review` 是粒度差异，不当冲突呈现。"""
    if not slugs:
        return []
    subjects = [f"spectre={s.replace('_', ' ')}" for s in slugs]
    placeholders = ",".join("?" * len(subjects))
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT subject,source_a,value_a,source_b,value_b FROM source_conflict "  # noqa: S608
            f"WHERE kind='spectre' AND subject IN ({placeholders})",
            subjects,
        )
    ]


def _render_lookup(
    name: str, rows: list[sqlite3.Row], pob: dict[str, sqlite3.Row], total: int, modelled: int
) -> str:
    out = [f"### {name}（可作幽魂的怪物）", ""]
    out.append(
        f"本库共收录 {total} 个可作幽魂的怪物（其中 {modelled} 个有 PoB 的抗性与建模系数）；"
        f"「{name}」命中 {len(rows)} 条。"
    )
    out.append("")
    # PoB 的生命/伤害系数也要进表：它们是冲突并列里出现的数字，
    # 不在证据里模型就只能无绑定地说，然后被出口闸门拦掉（实测拦到过 1.32）。
    out.append("| 中文名 | 英文标识 | 伤害%（POEDB） | 生命%（POEDB） | PoB 系数 | 四抗性（PoB） |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for row in rows[:MAX_ROWS]:
        meta = pob.get(str(row["poedb_slug"]).replace("_", " "))
        resist = _resist_line(meta) if meta is not None else "PoB 未建模"
        mults = (
            f"生命 {meta['life_mult']:g}×、伤害 {meta['damage_mult']:g}×"
            if meta is not None else "—"
        )
        out.append(
            f"| {row['name_zh']} | {row['poedb_slug']} | "
            f"{row['damage_pct']}% | {row['life_pct']}% | {mults} | {resist} |"
        )
    if len(rows) > MAX_ROWS:
        out.append("")
        out.append(
            f"（命中 {len(rows)} 条，以上仅列前 {MAX_ROWS} 条；"
            "同名怪物是不同区域版本，倍率各不相同，请用更具体的名字）"
        )

    extra = [pob[k] for k in {str(r["poedb_slug"]).replace("_", " ") for r in rows} if k in pob]
    if extra:
        meta = extra[0]
        bits = [f"攻击间隔 {meta['attack_time']:g} 秒"] if meta["attack_time"] else []
        for label, key in (("护甲系数", "armour_mult"), ("闪避系数", "evasion_mult"),
                           ("能量护盾系数", "es_mult")):
            if meta[key] is not None:
                bits.append(f"{label} {meta[key]:g}×")
        if bits:
            out.append("")
            out.append(f"PoB 建模的其它数值：{'、'.join(bits)}。")
    out.append("")
    out.append("倍率以 100%（PoB 侧写作 1.0×）为基础值。" + _BOUNDARY_BASE + "。")
    out.append("")
    out.append(f"来源：POEDB {rows[0]['url']}")
    if extra:
        out.append(f"抗性与系数来源：Path of Building {extra[0]['pob_url']}")
    return "\n".join(out)


def _render_ranking(rows: list[sqlite3.Row], by: str, modelled: int, total: int) -> str:
    label = {"life": "生命系数", "damage": "伤害系数"}[by]
    out = [f"### 幽魂{label}排行（PoB 建模的 {modelled} 个）", ""]
    out.append(
        f"排名依据是 PoB 建模的 {modelled} 个幽魂——POEDB 清单共 {total} 个，"
        "未被 PoB 建模的不参与排名（那里面混着开发用怪物，例如「测试之灵」）。"
    )
    out.append("")
    out.append("| 名次 | 中文名 | 英文名 | 生命系数 | 伤害系数 | 四抗性 |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for i, row in enumerate(rows, start=1):
        out.append(
            f"| {i} | {row['name_zh'] or '—'} | {row['name_en']} | "
            f"{row['life_mult']:g}× | {row['damage_mult']:g}× | {_resist_line(row)} |"
        )
    out.append("")
    out.append(f"{_BOUNDARY_RANK}。{_BOUNDARY_BASE}。")
    out.append("")
    out.append(f"来源：Path of Building {rows[0]['pob_url']}")
    return "\n".join(out)


def _citation(chunk_id: str, title: str, url: str, source: str, priority: int, text: str) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "entity_id": None,
        "title": title,
        "url": url,
        "source": source,
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "structured",
    }


@REGISTRY.register(
    name="spectre_stats",
    description=(
        "查怪物**作为幽魂**的数据：POEDB 的伤害% / 生命%（清单共 1948 个），"
        "以及 PoB 建模的四抗性、护甲/闪避/能量护盾系数与攻击间隔（268 个）。\n"
        "问「XX 能不能当幽魂 / XX 的幽魂倍率 / 幽魂抗性」用 monster_name；"
        "问「生命最高的幽魂 / 幽魂排行」用 rank_by（排名只在 PoB 建模的 268 个里做）。\n"
        "**没有掉落与怪物技能的中文说明**，不得据此回答那两类问题。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "monster_name": {
                "type": "string",
                "description": "怪物名，中文或英文标识，如「不朽精锐」「Abhorred_Elitist」",
            },
            "rank_by": {
                "type": "string",
                "description": "按系数排行：life=生命、damage=伤害。与 monster_name 二选一",
                "enum": ["life", "damage"],
            },
            "top": {"type": "integer", "description": "排行取前几名，1-10，默认 5"},
        },
    },
)
def spectre_stats(
    monster_name: str | None = None, rank_by: str | None = None, top: int | None = None
) -> ToolResult:
    conn = dbmod.get_conn()
    total = int(conn.execute("SELECT count(*) FROM poedb_spectre").fetchone()[0])
    modelled = int(conn.execute("SELECT count(*) FROM pob_spectre").fetchone()[0])

    if rank_by:
        if rank_by not in ("life", "damage"):
            return ToolResult(ok=False, error="rank_by 只能是 life 或 damage")
        limit = max(1, min(int(top or 5), MAX_RANK_ROWS))
        column = "life_mult" if rank_by == "life" else "damage_mult"
        rows = conn.execute(
            # 中文名靠 slug 反查 POEDB 侧；PoB 只有英文名
            f"""
            SELECT p.*, (
                SELECT s.name_zh FROM poedb_spectre s
                WHERE replace(s.poedb_slug,'_',' ') = p.name_en LIMIT 1
            ) AS name_zh
            FROM pob_spectre p ORDER BY p.{column} DESC, p.name_en LIMIT ?
            """,  # noqa: S608 列名来自上面的白名单
            (limit,),
        ).fetchall()
        if not rows:
            return ToolResult(
                ok=True,
                data={"found": False, "reason": "PoB 幽魂建模数据尚未导入，无法排名",
                      "boundary": f"{_BOUNDARY_RANK}。{_BOUNDARY_BASE}"},
            )
        text = _render_ranking(rows, rank_by, modelled, total)
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "rank_by": rank_by,
                "returned": len(rows),
                "ranked_within": modelled,
                "spectres_in_library": total,
                "boundary": f"{_BOUNDARY_RANK}。{_BOUNDARY_BASE}",
            },
            citations=[_citation(
                f"spectre_rank:{rank_by}:{limit}", f"幽魂{rank_by}排行",
                rows[0]["pob_url"], rows[0]["source"], int(rows[0]["source_priority"]), text,
            )],
            meta={"channel": "structured", "table": "pob_spectre"},
        )

    name = (monster_name or "").strip()
    if not name:
        return ToolResult(ok=False, error="monster_name 与 rank_by 至少要给一个")

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
                "boundary": _BOUNDARY_BASE,
            },
        )

    slugs = [str(r["poedb_slug"]) for r in rows[:MAX_ROWS]]
    pob = _pob_by_name(conn, slugs)
    conflicts = _conflicts_for(conn, slugs)
    text = _render_lookup(name, rows, pob, total, modelled)
    data: dict[str, Any] = {
        "found": True,
        "matched": len(rows),
        "returned": min(len(rows), MAX_ROWS),
        "truncated": len(rows) > MAX_ROWS,
        "spectres_in_library": total,
        "pob_modelled": len(pob),
        "boundary": _BOUNDARY_BASE,
    }
    if conflicts:
        # 需求 15.5：确认冲突并列展示、不自行判定谁对。渲染交给
        # `citation.conflict_block`（在此之前它一直是不可达代码——没有已确认冲突），
        # 走 `loop._NOTE_FIELDS` 由代码逐字附在答案末尾：让模型转述就会因无绑定被拦。
        data["conflict_block"] = conflict_block(conflicts)
        data["conflicts"] = len(conflicts)
    return ToolResult(
        ok=True,
        data=data,
        citations=[_citation(
            f"spectre_stats:{rows[0]['variety_key']}",
            f"{rows[0]['name_zh']}（幽魂数据）",
            rows[0]["url"], rows[0]["source"], int(rows[0]["source_priority"]), text,
        )],
        meta={"channel": "structured", "table": "poedb_spectre"},
    )
