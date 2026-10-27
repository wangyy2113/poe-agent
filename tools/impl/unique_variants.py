"""PoB 唯一装备命名变体的显式历史查询工具。"""

from __future__ import annotations

import json

from core import db as dbmod
from kb.unique_variants import (
    VariantQueryError, compare_variants, list_variants, variant_detail,
)
from tools.registry import REGISTRY, ToolResult


@REGISTRY.register(
    name="unique_variant_history",
    description=(
        "按唯一装备准确名称查询 PoB 历史/当前命名变体。不给编号只列可用变体；"
        "给 left_index 查看一个变体的完整词缀；再给 right_index 显式比较两个变体。"
        "多个当前变体必须让用户指定编号，不能任意选一个。"
        "Pre X 只表示 X 之前，不是 X 的精确快照。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "唯一装备准确中文名、英文名或 PoB UID"},
            "left_index": {"type": "integer", "minimum": 1},
            "right_index": {"type": "integer", "minimum": 1},
        },
        "required": ["name"],
        "additionalProperties": False,
    },
)
def unique_variant_history(
    name: str, left_index: int | None = None, right_index: int | None = None,
) -> ToolResult:
    if right_index is not None and left_index is None:
        return ToolResult(ok=False, error="比较必须同时指定 left_index 和 right_index")
    try:
        conn = dbmod.get_conn()
        if left_index is None:
            data = list_variants(conn, name)
            body = data["variants"]
            source_url = data["source_url"]
            source_record = data
        elif right_index is None:
            data = variant_detail(conn, name, left_index)
            body = data["variant"]
            source_url = body["url"]
            source_record = body
        else:
            data = compare_variants(conn, name, left_index, right_index)
            body = {k: data[k] for k in (
                "left", "right", "shared_mods", "removed_mods", "added_mods", "base_changed"
            )}
            source_url = data["left"]["url"]
            source_record = data["left"]
    except VariantQueryError as exc:
        return ToolResult(ok=True, data={"found": False, "reason": str(exc)})
    # 这些行只来自隔离表，作为工具调用的临时引用；不写入全局 FTS/chunk。
    citation = {
        "chunk_id": f"pob-variant:{data['uid']}:{left_index or 'list'}:{right_index or ''}",
        "entity_id": data["uid"], "title": data["name_en"],
        "url": source_url, "source": source_record["source"],
        "source_priority": source_record["source_priority"],
        "can_support_facts": bool(source_record.get("source_commit")),
        "text": json.dumps(body, ensure_ascii=False), "channel": "unique_variant_history",
    }
    return ToolResult(ok=True, data={"found": True, **data}, citations=[citation])
