"""GGG 官方公告的精确版本与逐行原文查询工具。"""

from __future__ import annotations

from core import db as dbmod
from kb import patch_notes
from tools.registry import REGISTRY, ToolResult


@REGISTRY.register(
    name="patch_note_lookup",
    description=(
        "查询已入库的 GGG 官方 PoE1 Patch Notes / Hotfix。不给 term 时按精确 version"
        "列公告；给英文 term 时逐行返回该词在官方首帖中的原文与版本时间线。"
        "字面提及不等于改动方向、生效时间或 Build 实际影响。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "version": {"type": "string", "description": "可选的精确版本，例如 3.29.3b"},
            "term": {"type": "string", "description": "可选英文机制或技能名，至少两个字符"},
        },
        "additionalProperties": False,
    },
)
def patch_note_lookup(version: str = "", term: str = "") -> ToolResult:
    version = version.strip()
    term = term.strip()
    if len(version) > 40 or len(term) > 120 or (term and len(term) < 2):
        return ToolResult(ok=False, error="版本或查询词长度无效")
    conn = dbmod.get_conn()
    if not term:
        notes = patch_notes.search(conn, version=version, limit=20)
        return ToolResult(ok=True, data={
            "found": bool(notes), "items": notes,
            "boundary": "仅列已核验并入库的官方公告；选择英文原词再查逐行变更原文",
        })
    rows = patch_notes.mentions(conn, [term], version=version, limit=30)
    citations = [{
        "chunk_id": f"patch_note:{row['thread_id']}:{row['line_number']}",
        "entity_id": f"patch_note:{row['thread_id']}",
        "title": row["title"], "url": row["source_url"],
        "source": "GGG Patch Notes", "source_priority": 6,
        "can_support_facts": True,
        "text": f"{row['title']} ({row['published_on']})\n{row['source_excerpt']}",
        "channel": "patch_note_lookup",
    } for row in rows]
    return ToolResult(ok=True, data={
        "found": bool(rows), "term": term, "version": version,
        "mentions": rows,
        "boundary": (
            "逐字命中官方首帖；不自动判断原文的改动方向、当前生效版本或玩家 Build 影响。"
            "未命中只表示已入库公告无字面提及。"
        ),
    }, citations=citations)
