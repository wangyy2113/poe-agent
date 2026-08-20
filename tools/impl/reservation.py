"""保留计算：开这几个光环之后还剩多少魔力。

## 为什么它必须查库而不是让用户报数

光环的基础保留百分比在库里（`pob_skill_level`）：坚定 50%、纪律 35%、寒冰之捷 25%……
让用户自己报，等于把「查得到的事实」也变成输入。所以这个工具**按名字查**，
并把查到的等级写进推导——「20 级坚定保留 50%」里的「20 级」是查出来的前提，
不是替用户假定的。

第二十四轮同时补上了**定值保留**（`reservation_flat`）：先前库里只有百分比那一列，
于是清晰（279 点）、精准（186 点）、活力（233 点）看起来「不保留任何魔力」。

## 三个取整陷阱都在 `calc/reservation.py` 里

百分比各自 round 到 2 位 → 求和 → **只 ceil 一次**；保留效率是**除法**；
`更多 <= 0` 时保留为 0。判据与行号引用都在那个模块里。

## 认不出名字就说出来

同名前缀（「净化」对应四个技能）不猜，列出候选让用户挑——拿其中一个去算，
得到的是一个带着推导表的错数字。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from calc import reservation as res
from calc.derive import pob_url, render_steps
from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 3200
#: 默认按 20 级算：光环的常见等级，也是 POEDB 与社区讨论的默认口径。
DEFAULT_GEM_LEVEL = 20
#: 候选最多列几个。列不完要写出还剩多少——静默截断等同于漏答。
MAX_CANDIDATES = 8

_BOUNDARY = (
    "保留百分比与定值取自库里的宝石每级数值，池大小与各项加成逐字取自问题；"
    "**不含血魔法（光环改为保留生命）**——库里没有生命保留的每级数值；"
    "也不判断这几个光环搭不搭（那是取向不是计算），不读取角色档案"
)


def _lookup(conn: sqlite3.Connection, name: str, gem_level: int) -> dict[str, Any]:
    """按名字查一个技能的保留。返回 `{'ok': bool, ...}`。

    先精确匹配中英文名，再退到唯一前缀匹配；多解就列候选并拒绝——
    「净化」对应四个技能，挑一个去算是在编数字。
    """
    text = (name or "").strip()
    if not text:
        return {"ok": False, "reason": "空名字"}
    rows = conn.execute(
        "SELECT gem_id, name_zh, name_en FROM pob_skill_gem "
        "WHERE name_zh = ? OR name_en = ? COLLATE NOCASE",
        (text, text),
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
        more = f"，另有 {len(rows) - MAX_CANDIDATES} 个" if len(rows) > MAX_CANDIDATES else ""
        return {
            "ok": False,
            "reason": f"「{text}」对应多个技能：{names}{more}。请写完整名字",
            "candidates": [r["name_zh"] for r in rows[:MAX_CANDIDATES]],
        }

    gem = rows[0]
    level = conn.execute(
        "SELECT gem_level, reservation_percent, reservation_flat FROM pob_skill_level "
        "WHERE gem_id = ? AND gem_level = ?",
        (gem["gem_id"], gem_level),
    ).fetchone()
    used_level = gem_level
    if level is None:
        # 没有这一级就退到**有保留数据的最高一级**，并写明用了哪一级
        level = conn.execute(
            "SELECT gem_level, reservation_percent, reservation_flat FROM pob_skill_level "
            "WHERE gem_id = ? AND (reservation_percent IS NOT NULL "
            "OR reservation_flat IS NOT NULL) ORDER BY gem_level DESC LIMIT 1",
            (gem["gem_id"],),
        ).fetchone()
        if level is None:
            return {
                "ok": False,
                "reason": f"{gem['name_zh']}（{gem['name_en']}）在库里没有保留数据"
                          "——它可能本来就不保留任何资源",
            }
        used_level = int(level["gem_level"])
    if level["reservation_percent"] is None and level["reservation_flat"] is None:
        return {
            "ok": False,
            "reason": f"{gem['name_zh']}（{gem['name_en']}）{used_level} 级没有保留数据"
                      "——它可能本来就不保留任何资源",
        }
    return {
        "ok": True,
        "name": f"{gem['name_zh']}（{gem['name_en']}，{used_level} 级）",
        "name_zh": gem["name_zh"],
        "gem_level": used_level,
        "requested_level": gem_level,
        "base_percent": float(level["reservation_percent"] or 0.0),
        "base_flat": float(level["reservation_flat"] or 0.0),
    }


def _pob_urls(conn: sqlite3.Connection) -> tuple[str, list[str]]:
    """从库里任意一颗宝石取钉住 commit 的 URL，再推出公式引用。

    为什么从数据里取：URL 必须与**宝石数据入库那一刻的 commit** 一致。
    """
    row = conn.execute(
        "SELECT pob_url FROM pob_skill_gem WHERE pob_url IS NOT NULL LIMIT 1"
    ).fetchone()
    base = str(row["pob_url"]) if row else ""
    formula = pob_url(base, res.PERFORM_FILE, lines=res.RESERVE_LINES)
    total = pob_url(base, res.PERFORM_FILE, lines=res.TOTAL_LINES)
    return formula, [
        f"- 每个技能的保留（取整 / 保留效率是除法）：{formula}",
        f"- 合计与剩余（Σ定值 + **一次**向上取整）：{total}",
        f"- 各技能的基础保留：`pob_skill_level`（PoB `Data/Skills/*.lua`）{base}",
    ]


def _render(result: res.ReservationResult, sources: list[str], notes: list[str]) -> str:
    out = [f"### 保留结算（{len(result.auras)} 个技能）", ""]
    if notes:
        out.extend(notes)
        out.append("")
    out.extend(render_steps(result.steps))
    out.append("")
    out.append(
        f"合计保留 {result.reserved:g} 点{result.pool_name}"
        f"（{result.total_percent:g}%"
        + (f" + 定值 {result.total_flat:g}" if result.total_flat else "")
        + f"），**剩余 {result.unreserved:g} 点**（{result.unreserved_pct:.1f}%）。"
    )
    if result.over_reserved:
        out.append(
            f"**开不起**：需要保留的量已经超过 {result.pool:g} 点"
            f"{result.pool_name}，PoB 会把它夹到池上限。"
        )
    out.append(
        "取整口径：每个技能的百分比各自 round 到 2 位小数后**求和**，"
        "最后对总量**只向上取整一次**——不是每个光环各自取整再相加（那会多算几点）。"
        "保留效率是**除法**：效率提高 25% 让 50% 变 40%，不是 25%。"
    )
    out.append("")
    out.append(_BOUNDARY + "。")
    out.append("")
    out.append("来源：")
    out.extend(sources)
    text = "\n".join(out)
    if len(text) <= MAX_EVIDENCE_CHARS:
        return text
    return (
        f"{text[:MAX_EVIDENCE_CHARS]}\n\n（证据超过 {MAX_EVIDENCE_CHARS} 字已截断，"
        f"剩余 {len(text) - MAX_EVIDENCE_CHARS} 字未包含在内）"
    )


@REGISTRY.register(
    name="reservation_calc",
    description=(
        "算「开这几个光环之后还剩多少魔力」：按名字从库里取每个技能的基础保留"
        "（百分比或定值），套 PoB 的取整规则（各自 round 2 位 → 求和 → 只向上取整一次）"
        "与保留效率（除法），给出逐步推导、合计保留与剩余。\n"
        "**技能名与池大小必须逐字取自用户问题**；名字不完整（如「净化」）会列出候选而不猜。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pool": {"type": "number", "description": "最大魔力（或血魔法下的最大生命）"},
            "auras": {
                "type": "array",
                "items": {"type": "string"},
                "description": "技能名列表，中文或英文，如 [\"坚定\", \"优雅\"]",
            },
            "gem_level": {
                "type": "integer",
                "description": f"宝石等级，默认 {DEFAULT_GEM_LEVEL}。库里没有这一级时会退到"
                "有保留数据的最高级并写明",
            },
            "increased_pct": {
                "type": "number", "description": "「增加/减少的保留」百分比之和（减少写负数）",
            },
            "more_mult": {"type": "number", "description": "「更多/更少保留」连乘，默认 1"},
            "efficiency_pct": {
                "type": "number",
                "description": "「保留效率提高」百分比之和。注意它是**除法**，提高 25% 让 50% 变 40%",
            },
            "efficiency_more": {
                "type": "number", "description": "保留效率的连乘项，默认 1",
            },
        },
        "required": ["auras"],
    },
)
def reservation_calc(
    auras: list[str] | None = None,
    pool: float = 0.0,
    gem_level: int = DEFAULT_GEM_LEVEL,
    increased_pct: float = 0.0,
    more_mult: float = 1.0,
    efficiency_pct: float = 0.0,
    efficiency_more: float = 1.0,
) -> ToolResult:
    names = [n for n in (auras or []) if str(n).strip()]
    if not names:
        return ToolResult(ok=False, error="auras 不能为空：至少给一个技能名")

    conn = dbmod.get_conn()
    formula_url, sources = _pob_urls(conn)

    entries: list[tuple[str, float, float]] = []
    notes: list[str] = []
    resolved: list[dict[str, Any]] = []
    for name in names:
        found = _lookup(conn, str(name), gem_level)
        if not found["ok"]:
            notes.append(f"- {found['reason']}")
            continue
        resolved.append(found)
        entries.append((found["name"], found["base_percent"], found["base_flat"]))
        if found["gem_level"] != found["requested_level"]:
            notes.append(
                f"- {found['name_zh']} 库里没有 {found['requested_level']} 级的保留数据，"
                f"按 {found['gem_level']} 级算"
            )

    if not entries:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": "没有一个技能名能对上库里的保留数据",
                "notes": notes,
                "boundary": _BOUNDARY,
            },
            citations=[{
                "chunk_id": "reservation_calc:未命中",
                "entity_id": None,
                "title": "保留结算（未命中）",
                "url": formula_url,
                "source": "Path of Building",
                "source_priority": 2,
                "can_support_facts": True,
                "text": "\n".join([
                    "### 保留结算：技能名没对上", "", *notes, "",
                    "库里的保留数据按技能名索引；名字不完整时会列出候选，"
                    "**不挑一个去算**——那会得到一个带着推导表的错数字。", "",
                    _BOUNDARY + "。",
                ]),
                "channel": "derived",
            }],
            meta={"channel": "derived", "table": "pob_skill_level"},
        )

    if not pool:
        # 没有池大小：百分比之和仍然算得出来，但**点数算不出来**。说清缺什么。
        percent_only = res.reserve(
            100.0, entries, increased_pct=increased_pct, more_mult=more_mult,
            efficiency_pct=efficiency_pct, efficiency_more=efficiency_more,
        )
        text = "\n".join([
            "### 保留结算：缺最大魔力", "",
            f"这几个技能合计保留 **{percent_only.total_percent:g}%** 最大魔力"
            + (f"，另有定值 {percent_only.total_flat:g} 点" if percent_only.total_flat else "")
            + "。",
            "",
            "**剩多少点算不出来**：那要乘上你的最大魔力（而且总量要向上取整一次）。"
            "把最大魔力告诉我就能算。",
            "",
            *(notes + [""] if notes else []),
            *render_steps(percent_only.steps[: len(entries) + 1]),
            "",
            _BOUNDARY + "。",
            "",
            "来源：",
            *sources,
        ])
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "kind": "percent_only",
                "total_percent": percent_only.total_percent,
                "total_flat": percent_only.total_flat,
                "missing_inputs": ["最大魔力"],
                "boundary": _BOUNDARY,
            },
            citations=[{
                "chunk_id": "reservation_calc:百分比合计",
                "entity_id": None,
                "title": f"保留合计 {percent_only.total_percent:g}%（缺最大魔力）",
                "url": formula_url,
                "source": "Path of Building",
                "source_priority": 2,
                "can_support_facts": True,
                "text": text,
                "channel": "derived",
            }],
            meta={"channel": "derived", "table": "pob_skill_level"},
        )

    result = res.reserve(
        pool, entries, increased_pct=increased_pct, more_mult=more_mult,
        efficiency_pct=efficiency_pct, efficiency_more=efficiency_more,
    )
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "kind": "result",
            "auras": [a.name for a in result.auras],
            "total_percent": result.total_percent,
            "total_flat": result.total_flat,
            "reserved": result.reserved,
            "unreserved": result.unreserved,
            "unreserved_pct": round(result.unreserved_pct, 2),
            "over_reserved": result.over_reserved,
            "missing_inputs": [n for n in notes],
            "boundary": _BOUNDARY,
        },
        citations=[{
            "chunk_id": "reservation_calc:" + "+".join(a.name for a in result.auras),
            "entity_id": None,
            "title": f"保留结算（剩余 {result.unreserved:g} 点{result.pool_name}）",
            "url": formula_url,
            "source": "Path of Building",
            "source_priority": 2,
            "can_support_facts": True,
            "text": _render(result, sources, notes),
            "channel": "derived",
        }],
        meta={"channel": "derived", "table": "pob_skill_level"},
    )
