"""工艺工作台配方目录查询工具。

目录只证明 POEDB 页面列出的选项、费用、适用 ItemClasses 与解锁信息；不把目录项
包装成可执行步骤，也不推导概率、市场成本或路线优劣。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY, Source
from tools.registry import REGISTRY, ToolResult

MAX_QUERY_CHARS = 120
MAX_ITEM_CLASS_CHARS = 40
MAX_RESULTS = 20
DEFAULT_RESULTS = 8
MAX_EVIDENCE_CHARS = 2400


def _escape_like(value: str) -> str:
    """把用户输入当 LIKE 字面量；参数绑定本身不会转义 % 与 _。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _render_recipe(row: sqlite3.Row, costs: list[sqlite3.Row]) -> str:
    item_classes = json.loads(row["item_classes"])
    out = [
        f"### 工艺工作台目录项 · {row['effect_text']}",
        "",
        f"- 效果原文：{row['effect_text']}",
        f"- 适用 ItemClasses：{'、'.join(item_classes)}",
        "- 原始费用：" + " + ".join(
            f"{cost['amount']}x {cost['currency_name_zh']}" for cost in costs
        ),
        f"- 解锁：{row['unlock_text']}",
    ]
    if row["unlock_hint"]:
        out.append(f"- 解锁说明：{row['unlock_hint']}")
    out.extend(
        [
            "",
            "> 边界：这是一条页面目录记录，不证明它在任意给定装备状态下可执行；",
            "> 不包含装备状态转移、crafted-mod 上限、替换/移除语义或元词缀交互。",
        ]
    )
    return "\n".join(out)


def _citation(row: sqlite3.Row, costs: list[sqlite3.Row]) -> dict[str, Any]:
    return {
        "chunk_id": f"bench_recipe:{row['recipe_id']}",
        "entity_id": None,
        "title": f"工艺工作台：{row['effect_text']}",
        "url": row["url"],
        "source": Source.POEDB.value,
        "source_priority": int(row["source_priority"]),
        "can_support_facts": int(row["source_priority"]) <= MAX_FACTUAL_PRIORITY,
        "claim_scope": "bench_catalog",
        "claim_data": {
            "effect_text": row["effect_text"],
            "item_classes": json.loads(row["item_classes"]),
            "raw_costs": [
                {
                    "amount": int(cost["amount"]),
                    "currency": cost["currency_name_zh"],
                }
                for cost in costs
            ],
            "unlock_text": row["unlock_text"],
            "unlock_hint": row["unlock_hint"],
        },
        "text": _render_recipe(row, costs)[:MAX_EVIDENCE_CHARS],
        "channel": "structured",
    }


@REGISTRY.register(
    name="bench_recipe_catalog",
    description=(
        "查询 POEDB 工艺工作台**目录**：效果原文、原始通货费用、适用 ItemClasses 与"
        "解锁信息。问「工艺台有没有某效果 / 页面列出的费用是什么 / 哪些类别适用」时用。"
        "结果不代表该工艺对用户当前装备必然可执行；不得据此生成制作步骤、成功率、"
        "市场成本或最快/最经济路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_QUERY_CHARS,
                "description": "效果原文关键词，如「最大生命」「前缀无法被改变」。",
            },
            "item_class": {
                "type": "string",
                "maxLength": MAX_ITEM_CLASS_CHARS,
                "description": "可选，按页面 ItemClasses 精确限定，如「护甲」「项链」「药剂」。",
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
def bench_recipe_catalog(
    query: str, item_class: str | None = None, limit: int = DEFAULT_RESULTS
) -> ToolResult:
    q = (query or "").strip()
    if not q:
        return ToolResult(ok=False, error="query 不能为空")
    if len(q) > MAX_QUERY_CHARS:
        return ToolResult(ok=False, error=f"query 最多允许 {MAX_QUERY_CHARS} 个字符")
    if not any(char.isalnum() for char in q):
        return ToolResult(ok=False, error="query 必须包含文字或数字，不能只给通配符或标点")

    class_name = (item_class or "").strip()
    if len(class_name) > MAX_ITEM_CLASS_CHARS:
        return ToolResult(
            ok=False,
            error=f"item_class 最多允许 {MAX_ITEM_CLASS_CHARS} 个字符",
        )
    try:
        requested_limit = int(limit)
    except (TypeError, ValueError):
        return ToolResult(ok=False, error="limit 必须是整数")
    if not 1 <= requested_limit <= MAX_RESULTS:
        return ToolResult(ok=False, error=f"limit 必须在 1 到 {MAX_RESULTS} 之间")

    conn = dbmod.get_conn()
    where = ["r.effect_text LIKE ? ESCAPE '\\'"]
    params: list[Any] = [f"%{_escape_like(q)}%"]
    if class_name:
        where.append(
            "EXISTS (SELECT 1 FROM json_each(r.item_classes) WHERE value = ?)"
        )
        params.append(class_name)
    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT count(*) FROM craft_bench_recipe r WHERE {where_sql}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT r.* FROM craft_bench_recipe r WHERE {where_sql} "
        "ORDER BY r.effect_text, r.recipe_id LIMIT ?",
        [*params, requested_limit],
    ).fetchall()

    citations: list[dict[str, Any]] = []
    for row in rows:
        costs = conn.execute(
            "SELECT * FROM craft_bench_recipe_cost WHERE recipe_id=? ORDER BY position",
            (row["recipe_id"],),
        ).fetchall()
        if not costs:
            return ToolResult(
                ok=False,
                error=f"配方 {row['recipe_id']} 缺少费用子项，目录完整性校验失败",
            )
        citations.append(_citation(row, costs))

    return ToolResult(
        ok=True,
        data={
            "found": bool(rows),
            "query": q,
            "item_class": class_name or None,
            "total": int(total),
            "returned": len(rows),
            "truncated": total > len(rows),
            "boundary": (
                "目录记录不证明给定装备状态下可执行；不提供步骤、概率、市场成本或路线比较"
            ),
        },
        citations=citations,
        meta={"channel": "structured", "table": "craft_bench_recipe"},
    )
