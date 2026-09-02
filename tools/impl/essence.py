"""精华物品查询工具。

## 为什么卡片之外还要一个工具

105 张精华卡各自回答「这个精华是什么」，但**跨精华的问题一张卡都答不了**：
「哪些精华没有等级上限」「T7 精华有哪些」「破空精华和低语精华差在哪」。
没有这个出口，那类问题会落到 `kb_search` 上，在 24000 张卡里挑——
这正是这个项目反复踩的「数据入库但没有出口」。

## 边界：这里只有精华**物品本身**

精华**赋予的词缀**是另一个粒度，在 `mod_affix`（5985 条 `category='essence'`）里，
也已经渲染进词缀组卡。问「某精华在某装备上给什么」由精华卡回答（卡上有逐类别的
保底词缀），问「哪些来源能出某条词缀」由词缀组卡回答。**本工具不重复那一份。**

掉落与兑换成本仍未采集，写进 `boundary` 由代码附注，不让模型转述。
"""

from __future__ import annotations

from core import db as dbmod
from tools.registry import REGISTRY, ToolResult

MAX_ROWS = 24
MAX_QUERY_CHARS = 60

_BOUNDARY = (
    "只复述 POEDB 精华页记录的**精华物品本身**（等阶、堆叠上限、等级上限、效果原文、"
    "逐装备类别的保底词缀）；**掉落来源与兑换成本未采集**，不提供获取难度或性价比判断"
)

#: 等阶 → 页面上的名字后缀。用于「T7 精华有哪些」这类按阶枚举。
_TIER_LABEL = {
    1: "低语", 2: "呢喃", 3: "啼泣", 4: "哀嚎",
    5: "咆哮", 6: "尖啸", 7: "破空",
}


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _render(rows: list[dict], mods: dict[str, list[tuple[str, str]]], title: str,
            total: int) -> str:
    """`total` 是**截断前**的件数：拿 `len(rows)` 当标题里的数会把
    「共 64 件」写成「共 24 件」，那正是「静默截断等同于漏答」的一种。"""
    out = [f"### {title}（共 {total} 件）", ""]
    if len(rows) == 1:
        row = rows[0]
        head = f"{row['name_zh']}（{row['name_en']}）" if row["name_en"] else row["name_zh"]
        out += [
            f"- 名称：{head}",
            f"- 精华等阶：T{row['tier']}",
            f"- 堆叠上限：{row['stack_max']}",
            "- 适用范围：" + (
                f"物品等级需求 {row['level_cap']} 及以下"
                if row["level_cap"] is not None else "无等级上限"
            ),
            f"- 效果：{row['base_effect']}",
        ]
        pairs = mods.get(row["metadata_id"], [])
        if pairs:
            out += ["", "保底词缀（按装备类别）："]
            out += [f"- {classes}：{text}" for classes, text in pairs]
    else:
        out += ["| 精华 | 等阶 | 堆叠 | 适用范围 |", "| --- | --- | --- | --- |"]
        out += [
            f"| {r['name_zh']} | T{r['tier']} | {r['stack_max']} | "
            + (f"物品等级 {r['level_cap']} 及以下" if r["level_cap"] is not None else "无等级上限")
            + " |"
            for r in rows
        ]
    out += ["", f"> 边界：{_BOUNDARY}。"]
    return "\n".join(out)


@REGISTRY.register(
    name="essence_lookup",
    description=(
        "查**精华物品本身**：某个精华的等阶 / 堆叠上限 / 等级上限 / 效果原文 / "
        "逐装备类别的保底词缀，或按等阶枚举（「T7 精华有哪些」「哪些精华没有等级上限」）。"
        "**不回答**「哪些来源能出某条词缀」——那是词缀组卡的活；"
        "掉落来源与兑换成本未采集。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_CHARS,
                "description": (
                    "精华名（如「憎恨之低语精华」「哀恸精华」）、等阶名（如「破空」「尖啸」）、"
                    "或写 `无等级上限` 枚举不限等级的那些。"
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
def essence_lookup(query: str) -> ToolResult:
    conn = dbmod.get_conn()
    q = (query or "").strip()
    if not q:
        return ToolResult(ok=False, error="query 不能为空")

    cols = ("metadata_id,name_zh,name_en,tier,stack_max,level_cap,base_effect,"
            "url,source,source_priority")
    if "无等级上限" in q or "没有等级上限" in q or "不限等级" in q:
        rows = [dict(r) for r in conn.execute(
            f"SELECT {cols} FROM poedb_essence WHERE level_cap IS NULL "
            "ORDER BY tier, name_zh")]
        title = "没有等级上限的精华"
    else:
        tier = next((t for t, label in _TIER_LABEL.items() if label in q), None)
        if tier is not None and "之" not in q:
            # 「破空精华有哪些」按阶枚举；「憎恨之破空精华」含「之」，走按名查
            rows = [dict(r) for r in conn.execute(
                f"SELECT {cols} FROM poedb_essence WHERE tier=? ORDER BY name_zh", (tier,))]
            title = f"T{tier}（{_TIER_LABEL[tier]}）精华"
        else:
            rows = [dict(r) for r in conn.execute(
                f"SELECT {cols} FROM poedb_essence WHERE name_zh LIKE ? ESCAPE '\\' "
                "OR name_en LIKE ? ESCAPE '\\' ORDER BY tier, name_zh",
                (f"%{_escape_like(q)}%", f"%{_escape_like(q)}%"))]
            title = f"匹配「{q}」的精华"

    if not rows:
        return ToolResult(ok=True, data={
            "found": False,
            "reason": (
                f"本库的 105 件精华里没有匹配「{q}」的。"
                "本表只收录**精华物品本身**；若要问的是「哪些来源能出某条词缀」，"
                "那在词缀组卡里，不在这张表。"),
            "boundary": _BOUNDARY,
        })

    shown = rows[:MAX_ROWS]
    mods: dict[str, list[tuple[str, str]]] = {}
    if len(shown) == 1:
        mods[shown[0]["metadata_id"]] = [
            (c, t) for c, t in conn.execute(
                "SELECT item_classes,text_zh FROM poedb_essence_mod "
                "WHERE metadata_id=? ORDER BY group_index", (shown[0]["metadata_id"],))
        ]
    text = _render(shown, mods, title, len(rows))
    if len(rows) > len(shown):
        # 静默截断等同于漏答：超出上限必须写出还剩多少
        text += f"\n\n（以上仅列 {len(shown)} 件，还有 {len(rows) - len(shown)} 件未列出）"
    return ToolResult(
        ok=True,
        data={
            "found": True, "total": len(rows), "shown": len(shown),
            "truncated": len(rows) > len(shown),
            "essences": [
                {"name_zh": r["name_zh"], "name_en": r["name_en"], "tier": r["tier"],
                 "stack_max": r["stack_max"], "level_cap": r["level_cap"]}
                for r in shown
            ],
            "boundary": _BOUNDARY,
        },
        citations=[{
            "chunk_id": f"essence:{shown[0]['metadata_id']}",
            "entity_id": None,
            "title": title,
            "url": shown[0]["url"],
            "source": shown[0]["source"],
            "source_priority": int(shown[0]["source_priority"]),
            "can_support_facts": True,
            "text": text,
            "channel": "structured",
        }],
        meta={"channel": "structured", "table": "poedb_essence"},
    )
