"""Craft of Exile 元词缀交互矩阵精确查询工具。

只按页面逐字 operation + guard 返回单元格事实；不做同义词匹配，不把 YES/NO
解释成给定装备可执行性，也不生成状态转移、步骤、概率、成本或路线。
"""

from __future__ import annotations

import json
from typing import Any

from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY, Source
from tools.registry import REGISTRY, ToolResult

MAX_OPERATION_CHARS = 80
MAX_GUARD_CHARS = 40
MAX_EVIDENCE_CHARS = 1800


def _render_interaction(row) -> str:  # noqa: ANN001
    out = [
        "### Metamod Interaction Matrix 页面记录",
        "",
        f"- 分区：{row['section']}",
        f"- 操作：{row['operation_text']}",
        f"- Guard：{row['guard_text']}",
        f"- 原始结果：{row['raw_value']}",
    ]
    if row["note_text"]:
        out.append(f"- 原始注释：{row['note_text']}")
    out.extend(
        [
            f"- 版本范围：{row['version_scope']}",
            "",
            "> 边界：仅复述来源矩阵单元格；页面未给出可验证的版本范围。",
            "> 不证明给定装备可执行，不提供完整状态转移、步骤、概率、成本或路线。",
        ]
    )
    return "\n".join(out)


def _citation(row) -> dict[str, Any]:  # noqa: ANN001
    return {
        "chunk_id": f"metamod_interaction:{row['interaction_id']}",
        "entity_id": None,
        "title": (
            f"Metamod Matrix：{row['operation_text']} × {row['guard_text']}"
            "（POEDB 镜像）"
        ),
        # 实际抽取的是该镜像页面；source 字段仍严格标记原始内容来源 P4。
        "url": row["mirror_url"],
        "source": Source.CRAFT_OF_EXILE.value,
        "source_priority": int(row["source_priority"]),
        "can_support_facts": int(row["source_priority"]) <= MAX_FACTUAL_PRIORITY,
        "claim_scope": "metamod_matrix",
        "claim_data": {
            "section": row["section"],
            "operation_text": row["operation_text"],
            "guard_text": row["guard_text"],
            "outcome": row["outcome"],
            "raw_value": row["raw_value"],
            "note_text": row["note_text"],
            "version_scope": row["version_scope"],
            "source_url": row["source_url"],
            "mirror_url": row["mirror_url"],
        },
        "text": _render_interaction(row)[:MAX_EVIDENCE_CHARS],
        "channel": "structured",
    }


def _latest_blank_cells(conn) -> list[dict[str, object]]:  # noqa: ANN001
    """读取最近成功快照的空白格审计；损坏时安全退化为空。"""
    row = conn.execute(
        "SELECT note FROM ingest_run WHERE category='metamod_matrix' "
        "AND succeeded>0 ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if not row or not row[0]:
        return []
    try:
        note = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    cells = note.get("blank_cells") if isinstance(note, dict) else None
    return [cell for cell in (cells or []) if isinstance(cell, dict)]


@REGISTRY.register(
    name="metamod_interaction_matrix",
    description=(
        "按 Craft of Exile（经 POEDB 镜像）元词缀交互矩阵的逐字 operation + guard "
        "查询一个页面单元格，返回 YES / NO / N/A / YES & NO 及原始注释。"
        "不做同义词猜测，不证明给定装备可执行；不得据此生成完整状态转移、制作步骤、"
        "成功率、成本或最快/最经济路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_OPERATION_CHARS,
                "description": "来源矩阵中的逐字操作名，如「混沌石」。",
            },
            "guard": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_GUARD_CHARS,
                "description": "来源矩阵中的逐字列名，如「无法骰出攻击词缀」。",
            },
        },
        "required": ["operation", "guard"],
        "additionalProperties": False,
    },
)
def metamod_interaction_matrix(operation: str, guard: str) -> ToolResult:
    operation_text = (operation or "").strip()
    guard_text = (guard or "").strip()
    if not operation_text or not guard_text:
        return ToolResult(ok=False, error="operation 与 guard 均不能为空")
    if len(operation_text) > MAX_OPERATION_CHARS:
        return ToolResult(
            ok=False, error=f"operation 最多允许 {MAX_OPERATION_CHARS} 个字符"
        )
    if len(guard_text) > MAX_GUARD_CHARS:
        return ToolResult(ok=False, error=f"guard 最多允许 {MAX_GUARD_CHARS} 个字符")

    conn = dbmod.get_conn()
    rows = conn.execute(
        "SELECT * FROM craft_metamod_interaction "
        "WHERE operation_text=? AND guard_text=? "
        "ORDER BY section, row_order, column_order",
        (operation_text, guard_text),
    ).fetchall()
    if len(rows) > 1:
        return ToolResult(
            ok=False,
            error=(
                "同一 operation + guard 命中多条矩阵记录，无法唯一绑定来源；"
                "请先修复快照完整性"
            ),
        )
    if not rows:
        source_blank = any(
            cell.get("operation_text") == operation_text
            and cell.get("guard_text") == guard_text
            for cell in _latest_blank_cells(conn)
        )
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "operation": operation_text,
                "guard": guard_text,
                "source_blank": source_blank,
                "missing_reason": (
                    "来源页面该单元格为空，不能推断为 N/A 或其它结果"
                    if source_blank
                    else "没有逐字匹配的来源矩阵记录"
                ),
                "boundary": (
                    "没有明确页面值时拒答；不做同义词匹配或游戏机制推断"
                ),
            },
            citations=[],
            meta={"channel": "structured", "table": "craft_metamod_interaction"},
        )

    row = rows[0]
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "operation": operation_text,
            "guard": guard_text,
            "boundary": (
                "仅复述来源矩阵单元格；不证明可执行，不提供状态转移、步骤、概率、"
                "成本或路线"
            ),
        },
        citations=[_citation(row)],
        meta={"channel": "structured", "table": "craft_metamod_interaction"},
    )
