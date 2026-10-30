"""按固定 Boss key 和章节读取已核验 Wiki 原文；引用随段落绑定。"""

from __future__ import annotations

from core import db as dbmod
from kb import boss_guides
from tools.registry import REGISTRY, ToolResult


@REGISTRY.register(
    name="boss_guide_lookup",
    description=(
        "查八个终局 Boss 的 PoE Wiki 攻略原文章节。先只给 boss_key 列章节，再用精确"
        "section 和可选 offset 读取一段原文。Wiki 是 P3 可变页面；不把攻略建议、"
        "推荐数值或 Uber 差异当成当前版本的官方保证。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "boss_key": {"type": "string", "enum": list(boss_guides.PAGES)},
            "section": {"type": "string", "description": "列表返回的精确章节名；省略只列章节"},
            "offset": {"type": "integer", "minimum": 0, "description": "章节内 0-based 行偏移"},
        },
        "required": ["boss_key"],
        "additionalProperties": False,
    },
)
def boss_guide_lookup(
    boss_key: str, section: str = "", offset: int = 0,
) -> ToolResult:
    if boss_key not in boss_guides.PAGES or offset < 0:
        return ToolResult(ok=False, error="Boss key 或偏移无效")
    guide = boss_guides.detail(dbmod.get_conn(), boss_key)
    if guide is None:
        return ToolResult(ok=True, data={
            "found": False, "reason": "该 Boss 的 Wiki 攻略尚未入库",
            "source_url": boss_guides.BASE + boss_guides.PAGES[boss_key][0],
        })
    headings = [item["heading"] for item in guide["sections"]]
    if not section:
        return ToolResult(ok=True, data={
            "found": True, "boss_key": boss_key, "page_title": guide["page_title"],
            "sections": headings, "source_url": guide["source_url"],
            "fetched_at": guide["fetched_at"],
            "boundary": "先选择精确章节；本次仅列目录，不支撑机制或打法事实",
        })
    target = next((item for item in guide["sections"] if item["heading"] == section), None)
    if target is None:
        return ToolResult(ok=True, data={
            "found": False, "reason": "章节未入库", "available_sections": headings,
        })
    lines = target["lines"]
    window = lines[offset:offset + 12]
    if not window:
        return ToolResult(ok=True, data={
            "found": False, "reason": "章节偏移超出范围", "line_count": len(lines),
        })
    evidence = f"{guide['page_title']} / {section}\n" + "\n".join(window)
    citation = {
        "chunk_id": f"boss_guide:{boss_key}:{section}:{offset}",
        "entity_id": f"boss_guide:{boss_key}",
        "title": f"{guide['page_title']} / {section}",
        "url": guide["source_url"], "source": "PoE Wiki",
        "source_priority": 3, "can_support_facts": True,
        "text": evidence, "channel": "boss_guide_lookup",
    }
    return ToolResult(ok=True, data={
        "found": True, "boss_key": boss_key, "page_title": guide["page_title"],
        "section": section, "lines": window, "offset": offset,
        "next_offset": offset + len(window) if offset + len(window) < len(lines) else None,
        "source_url": guide["source_url"], "fetched_at": guide["fetched_at"],
        "boundary": (
            "这是抓取时的 PoE Wiki 原文。与当前版本或玩家 Build 的适用性需另核对；"
            "数值门槛不能从攻略句子外推。"
        ),
    }, citations=[citation])
