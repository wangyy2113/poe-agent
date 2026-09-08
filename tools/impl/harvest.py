"""收获（古灵庄园）定向工艺目录查询工具。

与工艺台同一条边界：目录只证明 POEDB 页面列出的**效果原文与原始命能费用**；
不把目录项包装成可执行步骤，也不推导成功率、市场成本或路线优劣。

## 为什么这个域只有工具、没有卡片

74 条描述里有 16 条只差一个词（「重铸……其中包括一个 **X** 词缀」）。
做成 74 张小卡，BM25 就要在一堆近重复文本里挑，真正该命中的那张会被挤下去。
所以这个域走**工具 + 意图路由**，与工艺台一致（那边 783 条同理）。

## 不设 `claim_scope`

工艺台的证据带 `claim_scope="bench_catalog"`，`orchestrator/binding.py` 里有一套
约 100 行的**正向允许表**限制它能支撑哪些句式。这里**暂不跟进**，理由是先量再改：

* 除工艺台外，全部工具（星团、宝石、幽魂、各计算器）都用普通证据，
  通用闸门的「事实句必须绑定证据 + 数值逐个回填」已经覆盖了数值编造；
* 一套写错的允许表比没有更糟——它会把有据的句子拦掉，而那是**错误拒答**，
  与无据漏出必须成对压制；
* 本轮没有收获类问题的录制，`--gate` 量不出这两个方向的代价。

所以这一轮把边界放在**工具描述 + 系统提示词 + `data["boundary"]`** 三处
（后者由 `loop._answer_notes()` 逐字附在答案末尾，不经模型转述）。
若日后录制显示模型据此写出制作步骤或成功率，再按工艺台那套加 scope——
判据是**实测到漏出**，不是「觉得可能会漏」。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY, Source
from tools.registry import REGISTRY, ToolResult

MAX_QUERY_CHARS = 120
MAX_RESULTS = 20
DEFAULT_RESULTS = 8
MAX_EVIDENCE_CHARS = 2400

_BOUNDARY = (
    "只复述 POEDB 收获页面的目录记录（效果原文与原始命能费用）；"
    "**不提供成功率、权重、制作步骤、市场成本或最快/最经济路线**，"
    "目录项也不证明它对某件具体装备可执行。"
    # 第五十八轮之后这句不能再写「获取途径未采集」——种子怪物已入库，
    # 由 `harvest_seed_lookup` 回答。仍未采集的是**掉落数量与概率**。
    "命能由哪些怪物种出见 `harvest_seed_lookup`；**掉落数量与概率未采集**"
)


def _escape_like(value: str) -> str:
    """把用户输入当 LIKE 字面量；参数绑定本身不会转义 % 与 _。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _render_craft(row: sqlite3.Row, costs: list[sqlite3.Row]) -> str:
    out = [
        f"### 收获定向工艺 · {row['effect_text']}",
        "",
        f"- 效果原文：{row['effect_text']}",
        "- 原始费用：" + " + ".join(
            f"{c['amount']}x {c['currency_name_zh']}" for c in costs
        ),
        "",
        "> 边界：这是一条页面目录记录，不证明它对某件具体装备可执行；",
        "> 不包含成功率、权重、制作步骤与市场成本。",
    ]
    return "\n".join(out)


def _citation(row: sqlite3.Row, costs: list[sqlite3.Row]) -> dict[str, Any]:
    priority = int(row["source_priority"])
    return {
        "chunk_id": f"harvest_craft:{row['craft_id']}",
        "entity_id": None,
        "title": f"收获定向工艺：{row['effect_text']}",
        "url": row["url"],
        "source": Source.POEDB.value,
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": _render_craft(row, costs)[:MAX_EVIDENCE_CHARS],
        "channel": "structured",
    }


@REGISTRY.register(
    name="harvest_craft_catalog",
    description=(
        "查询 POEDB 收获（古灵庄园）**定向工艺目录**：效果原文与原始命能费用。"
        "问「收获能不能改某类词缀 / 某条收获工艺要多少命能 / 某种命能能做什么」时用。"
        "也能按费用通货筛（如「狂野紫晶命能」）。"
        "结果只是页面目录，不代表对用户当前装备可执行；"
        "不得据此生成制作步骤、成功率、权重、市场成本或最快/最经济路线。"
        "命能由哪些怪物种出请改用 `harvest_seed_lookup`；掉落数量与概率未采集。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_CHARS,
                "description": (
                    "效果原文关键词，如「火焰」「抗性」「重铸」「辅助宝石」；"
                    "也可以写费用通货名，如「狂野紫晶命能」。"
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULTS,
                "description": f"最多返回多少条，默认 {DEFAULT_RESULTS}，上限 {MAX_RESULTS}。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
def harvest_craft_catalog(query: str, limit: int = DEFAULT_RESULTS) -> ToolResult:
    q = (query or "").strip()
    if not q:
        return ToolResult(ok=False, error="query 不能为空")
    if len(q) > MAX_QUERY_CHARS:
        return ToolResult(ok=False, error=f"query 最多允许 {MAX_QUERY_CHARS} 个字符")
    if not any(char.isalnum() for char in q):
        return ToolResult(ok=False, error="query 必须包含文字或数字，不能只给通配符或标点")
    try:
        requested = int(limit)
    except (TypeError, ValueError):
        return ToolResult(ok=False, error="limit 必须是整数")
    if not 1 <= requested <= MAX_RESULTS:
        return ToolResult(ok=False, error=f"limit 必须在 1 到 {MAX_RESULTS} 之间")

    conn = dbmod.get_conn()
    like = f"%{_escape_like(q)}%"
    # 描述或**费用通货名**任一命中：「狂野紫晶命能能做什么」问的是后者
    where = (
        "h.effect_text LIKE ? ESCAPE '\\' OR EXISTS ("
        "  SELECT 1 FROM harvest_craft_cost c WHERE c.craft_id=h.craft_id"
        "    AND c.currency_name_zh LIKE ? ESCAPE '\\')"
    )
    params: list[Any] = [like, like]
    total = conn.execute(
        f"SELECT count(*) FROM harvest_craft h WHERE {where}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT h.* FROM harvest_craft h WHERE {where} "
        "ORDER BY h.effect_text, h.craft_id LIMIT ?",
        [*params, requested],
    ).fetchall()

    citations: list[dict[str, Any]] = []
    for row in rows:
        costs = conn.execute(
            "SELECT * FROM harvest_craft_cost WHERE craft_id=? ORDER BY position",
            (row["craft_id"],),
        ).fetchall()
        if not costs:
            # 目录里没有免费工艺；缺费用说明入库时漏了行，不能当成「这条不要钱」
            return ToolResult(
                ok=False,
                error=f"工艺 {row['craft_id']} 缺少费用子项，目录完整性校验失败",
            )
        citations.append(_citation(row, costs))

    data: dict[str, Any] = {
        "found": bool(rows),
        "query": q,
        "total": int(total),
        "returned": len(rows),
        "truncated": total > len(rows),
        "boundary": _BOUNDARY,
    }
    if total > len(rows):
        # 静默截断等同于漏答：还剩多少必须写出来
        data["truncation_note"] = (
            f"匹配到 {total} 条，本次只返回 {len(rows)} 条，还有 {total - len(rows)} 条未列出"
        )
    return ToolResult(
        ok=True,
        data=data,
        citations=citations,
        meta={"channel": "structured", "table": "harvest_craft"},
    )


#: 单次最多列几个怪物。51 个里同色 17 个，全列也不长，但静默截断等同于漏答。
MAX_SEED_ROWS = 20

#: 这一维的边界。由代码附在 `data` 里，不让模型转述。
_SEED_BOUNDARY = (
    "种子怪物按**颜色**掉对应命能（Wild→狂野紫晶、Vivid→活性黄晶、Primal→原始蓝晶）——"
    "颜色与命能英文名前缀逐字对应，是查表不是推断。"
    "**第四种「神圣白晶命能」不在这张表里**（它没有对应的种子怪物）。"
    "页面没有给掉落数量与概率，本工具也不提供。"
)

#: 颜色 → 命能中文名。**桥接键是命能通货的英文名前缀**，不是猜的
#: （`Wild Crystallised Lifeforce`）。
_COLOUR_TO_LIFEFORCE = {
    "Wild": "狂野紫晶命能",
    "Vivid": "活性黄晶命能",
    "Primal": "原始蓝晶命能",
}


@REGISTRY.register(
    name="harvest_seed_lookup",
    description=(
        "查**收获（古灵庄园）的种子怪物**：某种命能由哪些怪物掉、某个怪物属于哪种颜色。"
        "命能问「怎么获得 / 要种什么」时用它。"
        "只回答颜色与阶级，**不提供掉落数量与概率**（页面没有给）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "命能名（如「狂野紫晶命能」）、颜色（Wild/Vivid/Primal）或怪物名。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
def harvest_seed_lookup(query: str) -> ToolResult:
    conn = dbmod.get_conn()
    q = (query or "").strip()
    if not q:
        return ToolResult(ok=False, error="query 不能为空")

    colour = next(
        (c for c, name in _COLOUR_TO_LIFEFORCE.items() if name in q or q in name), "")
    if not colour and q.title() in _COLOUR_TO_LIFEFORCE:
        colour = q.title()
    if colour:
        rows = [dict(r) for r in conn.execute(
            "SELECT monster_zh, tier, colour, url, source, source_priority "
            "FROM poedb_harvest_seed WHERE colour=? ORDER BY tier, monster_zh", (colour,))]
    else:
        rows = [dict(r) for r in conn.execute(
            "SELECT monster_zh, tier, colour, url, source, source_priority "
            "FROM poedb_harvest_seed WHERE monster_zh LIKE ? ORDER BY tier, monster_zh",
            (f"%{q}%",))]

    if not rows:
        return ToolResult(
            ok=True,
            data={"found": False,
                  "reason": (
                      f"没有匹配「{q}」的收获种子怪物。"
                      "本表只收录 51 个种子怪物（Wild/Vivid/Primal 各 17）；"
                      "神圣白晶命能没有对应的种子怪物，不在表内。"),
                  "boundary": _SEED_BOUNDARY},
        )

    got_colour = rows[0]["colour"]
    lifeforce = _COLOUR_TO_LIFEFORCE.get(got_colour, "")
    shown = rows[:MAX_SEED_ROWS]
    title = f"{got_colour} 色种子怪物" + (f"（掉 {lifeforce}）" if lifeforce else "")
    lines = [f"### {title}（共 {len(rows)} 个）", "", "| 怪物 | 阶级 | 颜色 |",
             "| --- | --- | --- |"]
    lines += [f"| {r['monster_zh']} | {r['tier']} | {r['colour']} |" for r in shown]
    if len(rows) > len(shown):
        lines.append("")
        lines.append(f"（以上仅列 {len(shown)} 个，还有 {len(rows) - len(shown)} 个未列出）")
    lines.append("")
    lines.append(_SEED_BOUNDARY)
    return ToolResult(
        ok=True,
        data={"found": True, "colour": got_colour, "lifeforce": lifeforce,
              "total": len(rows), "shown": len(shown),
              "truncated": len(rows) > len(shown),
              "monsters": [{"name": r["monster_zh"], "tier": r["tier"]} for r in shown],
              "boundary": _SEED_BOUNDARY},
        citations=[{
            "chunk_id": f"harvest_seed:{got_colour}",
            "entity_id": None,
            "title": title,
            "url": rows[0]["url"],
            "source": rows[0]["source"],
            "source_priority": int(rows[0]["source_priority"]),
            "can_support_facts": True,
            "text": "\n".join(lines),
            "channel": "structured",
        }],
        meta={"channel": "structured", "table": "poedb_harvest_seed"},
    )


# ── 按标签改词缀权重（化石 / 催化剂 / 花园工艺站 / 两条元词缀，第六十五轮）──

#: 一次最多列几行。静默截断等同于漏答。
MAX_TAG_WEIGHT_ROWS = 40

#: 边界。由代码放进 `data`，不让模型转述。
_TAG_WEIGHT_BOUNDARY = (
    "权重乘数取自 POEDB 收获页的 `Type ｜ Name ｜ Tags` 表。"
    "**只有化石与那两条工艺台元词缀给了数字**；催化剂与花园工艺站页面只列标签、"
    "没给乘数，本库因此留空——不要把留空读成「乘数是 1」。"
    "`x0` 是「这一类词缀不会出现」这个确定事实。"
    "**这张表不含成功率、也不含共鸣器的组合规则**（那一维未采集），"
    "而标签名中英混排（`冰霜` 与 `bleed`）是页面原值，本库不自行翻译。"
)


@REGISTRY.register(
    name="tag_weight_lookup",
    description=(
        "查**按词缀标签改权重**的东西：某个化石屏蔽/放大哪些标签的词缀、"
        "想放大某个标签该用哪个化石、某个催化剂或花园工艺站对应什么标签。"
        "问「炽炎化石有什么用 / 哪个化石能提高火焰词缀 / 化石的权重乘数」时用它。\n"
        "**不回答成功率与共鸣器组合规则**——那一维本库未采集。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "化石/催化剂/工艺站名（如「炽炎化石」），或标签（如「火焰」「施法」）。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
def tag_weight_lookup(query: str) -> ToolResult:
    q = (query or "").strip()
    # 校验与 `harvest_craft_catalog` 逐条相同——**同一个概念不写第二份判据**。
    if not q:
        return ToolResult(ok=False, error="query 不能为空")
    if len(q) > MAX_QUERY_CHARS:
        return ToolResult(ok=False, error=f"query 最多允许 {MAX_QUERY_CHARS} 个字符")
    if not any(char.isalnum() for char in q):
        return ToolResult(ok=False, error="query 必须包含文字或数字，不能只给通配符或标点")

    conn = dbmod.get_conn()

    # **必须走 `_escape_like`**，而这张表是最需要它的一张：标签名本身带下划线
    # （`physical_damage` / `chaos_damage`），不转义的话用户输入里的 `_` 会变成
    # 「任意一个字符」的通配符，`%` 更是直接匹配全部 61 行。
    like = f"%{_escape_like(q)}%"
    rows = [dict(r) for r in conn.execute(
        "SELECT kind, name_zh, tag, multiplier, url, source, source_priority "
        "FROM poedb_tag_weight "
        "WHERE name_zh LIKE ? ESCAPE '\\' OR tag LIKE ? ESCAPE '\\' "
        "ORDER BY kind, name_zh, row_index",
        (like, like),
    )]
    if not rows:
        known = conn.execute("SELECT count(*) FROM poedb_tag_weight").fetchone()[0]
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": (
                    f"按标签改权重的表里没有「{q}」（共 {known} 行：化石 / 催化 / "
                    "花园工艺站 / 两条工艺台元词缀）。**这不等于它没有效果**——"
                    "这张表只收 POEDB 收获页列出的那些。"
                ),
                "boundary": _TAG_WEIGHT_BOUNDARY,
            },
        )

    shown = rows[:MAX_TAG_WEIGHT_ROWS]
    lines = [f"### 「{q}」相关的标签权重乘数（共 {len(rows)} 行）", "",
             "| 类型 | 名称 | 词缀标签 | 权重乘数 |", "| --- | --- | --- | --- |"]
    for r in shown:
        mult = "（页面未给）" if r["multiplier"] is None else _fmt_multiplier(r["multiplier"])
        lines.append(f"| {r['kind']} | {r['name_zh']} | {r['tag']} | {mult} |")
    if len(rows) > len(shown):
        lines.append("")
        lines.append(f"（以上仅列 {len(shown)} 行，还有 {len(rows) - len(shown)} 行未列出）")
    lines += ["", _TAG_WEIGHT_BOUNDARY]
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "total": len(rows),
            "shown": len(shown),
            "truncated": len(rows) > len(shown),
            "rows": [
                {"kind": r["kind"], "name": r["name_zh"], "tag": r["tag"],
                 "multiplier": r["multiplier"],
                 "blocks": r["multiplier"] == 0.0}
                for r in shown
            ],
            "boundary": _TAG_WEIGHT_BOUNDARY,
        },
        citations=[{
            "chunk_id": f"tag_weight:{q}",
            "entity_id": None,
            "title": f"{q} 的标签权重乘数（POEDB 收获页）",
            "url": rows[0]["url"],
            "source": rows[0]["source"],
            "source_priority": int(rows[0]["source_priority"]),
            "can_support_facts": True,
            "text": "\n".join(lines),
            "channel": "structured",
        }],
        meta={"channel": "structured", "table": "poedb_tag_weight"},
    )


def _fmt_multiplier(value: float) -> str:
    """`10.0 → x10`、`0.15 → x0.15`、`0.0 → x0（屏蔽）`。

    整数不留 `.0`：证据里出现 `x10.0` 而页面写的是 `x10`，
    数值回填校验会因为这一位小数点找不到而拦掉一句正确的话。
    """
    text = f"{value:g}"
    return f"x{text}（屏蔽这一类词缀）" if value == 0 else f"x{text}"
