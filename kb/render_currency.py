"""通货 → 知识卡片。

卡片很短（名字 + 堆叠上限 + 效果原文），一条一张，不切分。

## 没有效果文本的那 15 条也出卡片

碎片与命能/结晶在 POEDB 上没有效果文本。卡片照出，但**明写「页面上没有效果说明」**
而不是留一片空白——空白会让人以为渲染坏了，而自己补一句「用于合成」就是编造。

## 不写「本库没有价格」这类边界

那属于覆盖层与 `_answer_notes()`（规约第 2 条）。把「价格」两个字写进 113 张卡片，
等于给每个含「价格」的问题都送一份覆盖率——技能石那轮已经踩过这个坑。
"""

from __future__ import annotations

import hashlib

from entities.models import Chunk, DocumentMetadata, Entity
from entities.taxonomy import Category, SubCategory
from retrieval.tokenize import content_words, tokens_for_fts

MAX_CHUNK_CHARS = 1200


def render_card(entity: Entity) -> str:
    attrs = entity.attributes
    head = f"{entity.name_zh}（{entity.name_en}）" if entity.name_en else entity.name_zh
    parts: list[str] = [f"# {head}"]
    parts.append(f"类型：通货 ｜ 堆叠上限 {attrs.get('stack_max')}")
    parts.append("")
    effect = attrs.get("effect_zh")
    if effect:
        parts.append(f"- 效果：{effect}")
    else:
        parts.append("- POEDB 页面上没有给这条通货的效果说明（碎片与命能类多为此形态）")
    parts.append(f"\n来源：{entity.ref.source} → 通货")
    parts.append(entity.ref.url)
    return "\n".join(parts)


def build_metadata(entity: Entity) -> DocumentMetadata:
    return DocumentMetadata(
        title=f"{entity.name_zh}（{entity.name_en}）" if entity.name_en else entity.name_zh,
        category=Category.ECONOMY,
        subcategory=SubCategory.CURRENCY.value,
        source=entity.ref.source,
        source_priority=entity.ref.priority,
        url=entity.ref.url,
        language="zh-CN",
        facets={"item_class": str(entity.attributes.get("item_class") or "")},
        fetched_at=entity.ref.fetched_at,
    )


def to_chunks(entity: Entity) -> list[Chunk]:
    """实体 → 切片。缺 url/source 的实体不产出切片（需求 2.5）。"""
    if not entity.indexable:
        return []
    card = render_card(entity)
    digest = hashlib.sha256(f"{entity.entity_id}#0".encode()).hexdigest()[:16]
    keywords = " ".join(
        entity.search_keywords
        or content_words([str(entity.attributes.get("effect_zh") or "")],
                         extra=[entity.name_zh, entity.name_en or ""])
    )
    return [
        Chunk(
            chunk_id=f"{entity.entity_id}#0#{digest}",
            entity_id=entity.entity_id,
            text=card,
            tokens=tokens_for_fts(card),
            keywords=keywords,
            metadata=build_metadata(entity),
        )
    ]
