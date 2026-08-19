"""怪物词缀 → 知识卡片。

## 只有词缀有卡片，幽魂没有

幽魂表 1915 行是「名字 + 两个倍率」，卡片之间高度雷同，而怪物名里有大量通用词
（「恶魔」「守卫」「弓箭手」）——别名精确命中会把它们注入任何含这些词的问题的候选集。
词缀梯度卡踩过同一个坑：粒度不对就要靠排序层补偿，而那些补偿层最后都被删了。
所以幽魂走 `tools/impl/monsters.py` 的 `spectre_stats` 直查表，本模块不给它出卡片。

## 卡片粒度 = (词缀来源, 词缀名)

一条怪物词缀就是一张卡。它们本来就短且各不相同（「镀金的 → 30% 额外物理伤害减免」），
不存在近似重复的问题。来源（传奇 / 精华 / 魔符 / 惊悸迷雾 / 猎魔 / 罪恶枷锁）写进卡片
正文，因为「哪些传奇词缀会加抗性」这类问题要靠它区分。
"""

from __future__ import annotations

import hashlib

from entities.models import Chunk, DocumentMetadata, Entity
from entities.taxonomy import Category, SubCategory
from retrieval.tokenize import content_words, tokens_for_fts

#: 怪物词缀卡都很短（实测最长不到 200 字），留个上限做保险。
MAX_CHUNK_CHARS = 1200


def render_card(entity: Entity) -> str:
    """渲染怪物词缀卡。文本原文照抄 POEDB，不做任何改写或换算。"""
    attrs = entity.attributes
    parts: list[str] = [f"# {entity.name_zh}"]
    meta_bits = ["类型：怪物词缀", f"来源：{attrs.get('kind', '')}"]
    level = attrs.get("monster_level")
    if level:
        meta_bits.append(f"怪物等级 {level}")
    parts.append(" ｜ ".join(bit for bit in meta_bits if bit))

    parts.append("")
    parts.append(f"- {attrs.get('text_zh', '')}")

    raw = [str(r) for r in (attrs.get("raw_stats") or [])]
    if raw:
        # 未翻译的内部 stat id **单列并标注**，不混进上面那行。
        # 它是调试信息，写清楚「没有中文」比假装它是词缀文本诚实。
        parts.append("")
        parts.append(f"> 该词缀还有 {len(raw)} 项效果在 POEDB 上没有中文文本，"
                     f"仅有内部标识：{'、'.join(raw)}")

    parts.append(f"\n来源：{entity.ref.source} → 怪物词缀")
    parts.append(entity.ref.url)
    return "\n".join(parts)


def build_metadata(entity: Entity) -> DocumentMetadata:
    return DocumentMetadata(
        title=f"{entity.name_zh}（怪物词缀）",
        category=Category.BASICS,
        subcategory=SubCategory.MONSTER_MOD.value,
        source=entity.ref.source,
        source_priority=entity.ref.priority,
        url=entity.ref.url,
        language="zh-CN",
        tags=[str(entity.attributes.get("kind") or "")],
        facets={"monster_mod_kind": str(entity.attributes.get("kind") or "")},
        fetched_at=entity.ref.fetched_at,
    )


def to_chunks(entity: Entity) -> list[Chunk]:
    """实体 → 切片。缺 url/source 的实体不产出切片（需求 2.5）。"""
    if not entity.indexable:
        return []
    card = render_card(entity)
    digest = hashlib.sha256(f"{entity.entity_id}#0".encode()).hexdigest()[:16]
    keyword_text = " ".join(
        entity.search_keywords
        or content_words([str(entity.attributes.get("text_zh") or "")],
                         extra=[entity.name_zh])
    )
    return [
        Chunk(
            chunk_id=f"{entity.entity_id}#0#{digest}",
            entity_id=entity.entity_id,
            text=card,
            tokens=tokens_for_fts(card),
            keywords=keyword_text,
            metadata=build_metadata(entity),
        )
    ]
