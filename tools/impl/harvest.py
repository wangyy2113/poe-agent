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
    "命能的**获取途径（作物与掉落）本轮未采集**"
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
        "命能的获取途径（作物与掉落）未采集。"
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
