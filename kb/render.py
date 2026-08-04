"""实体 → 知识卡片 Markdown → 切片。

两条硬约束：
  1. **数值原文保留**：卡片里的词缀文本直接来自 POEDB，不做任何改写或换算。
     检索命中后若需精确数值，还可经 entity_id 回 Tier A 取结构化 ranges。
  2. **切片不切断词缀**：数值被切到两个 chunk 会让该事实点无法被引用，
     进而被判定为缺据。因此以「整条词缀」为最小不可分单元。
"""

from __future__ import annotations

import hashlib

from entities.models import Chunk, DocumentMetadata, Entity, ModLine
from entities.taxonomy import Category, ModKind, SubCategory
from retrieval.tokenize import content_words, tokens_for_fts

#: 单个 chunk 的目标字符数上限。唯一装备平均 5.4 条词缀，
#: 绝大多数能放进一个 chunk；超长的按词缀边界切。
MAX_CHUNK_CHARS = 1200

_KIND_LABEL = {
    ModKind.IMPLICIT: "隐式词缀",
    ModKind.EXPLICIT: "显式词缀",
    ModKind.CORRUPTED_IMPLICIT: "腐化隐式",
    ModKind.FRACTURED: "裂隙词缀",
    ModKind.SYNTHESISED: "合成隐式",
    ModKind.ENCHANT: "附魔",
    ModKind.CRAFTED: "工艺台词缀",
    ModKind.DESCRIPTION: "说明",
}


def _mod_lines(mods: list[ModLine]) -> list[str]:
    """每条词缀渲染为一行，行内不再拆分。"""
    lines: list[str] = []
    for m in mods:
        label = _KIND_LABEL.get(m.kind, m.kind.value)
        lines.append(f"- [{label}] {m.text}")
    return lines


def render_card(entity: Entity) -> str:
    """渲染知识卡片。标题含中英文名便于对照社区资料。"""
    head = entity.name_zh
    if entity.name_en:
        head = f"{entity.name_zh}（{entity.name_en}）"

    parts: list[str] = [f"# {head}"]

    meta_bits: list[str] = ["类型：唯一装备"]
    if entity.base_type:
        meta_bits.append(f"底材：{entity.base_type}")
    if entity.item_class:
        meta_bits.append(f"分类：{entity.item_class}")
    parts.append(" ｜ ".join(meta_bits))

    if entity.requirement and entity.requirement.raw:
        parts.append(f"\n**需求**：{entity.requirement.raw}")

    implicit = entity.mods_of(ModKind.IMPLICIT)
    if implicit:
        parts.append("\n**隐式词缀**")
        parts.extend(_mod_lines(implicit))

    explicit = [m for m in entity.mods if m.kind is ModKind.EXPLICIT]
    if explicit:
        parts.append("\n**显式词缀**")
        parts.extend(_mod_lines(explicit))

    desc = entity.mods_of(ModKind.DESCRIPTION)
    if desc:
        parts.append("\n**附加说明**")
        parts.extend(_mod_lines(desc))

    if not entity.factual_mods:
        parts.append("\n> 该物品在 POEDB 上未列出属性词缀（碎片 / 契约 / 奇珍类物品通常如此）。")

    if entity.flavour_text:
        parts.append(f"\n**风味文本**\n> {entity.flavour_text}")

    parts.append(f"\n来源：{entity.ref.source} → {head}")
    parts.append(entity.ref.url)

    return "\n".join(parts)


def build_metadata(entity: Entity) -> DocumentMetadata:
    title = entity.name_zh
    if entity.name_en:
        title = f"{entity.name_zh}（{entity.name_en}）"

    tags = [t for t in [entity.base_type, entity.item_class] if t]
    return DocumentMetadata(
        title=title,
        category=Category.ITEM,
        subcategory=SubCategory.UNIQUE_ITEM.value,
        source=entity.ref.source,
        source_priority=entity.ref.priority,
        url=entity.ref.url,
        league=entity.league,
        version=entity.version,
        language="zh-CN",
        tags=tags,
        fetched_at=entity.ref.fetched_at,
    )


def _split_by_mod_boundary(card: str, limit: int) -> list[str]:
    """超长卡片按行切分，保证不切断任何一行（即不切断任何一条词缀）。"""
    lines = card.split("\n")
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in lines:
        if buf and size + len(line) + 1 > limit:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def to_chunks(entity: Entity) -> list[Chunk]:
    """实体 → 切片。缺 url/source 的实体不产出切片（需求 2.5）。"""
    if not entity.indexable:
        return []

    card = render_card(entity)
    metadata = build_metadata(entity)
    pieces = (
        [card] if len(card) <= MAX_CHUNK_CHARS else _split_by_mod_boundary(card, MAX_CHUNK_CHARS)
    )

    # keywords 用实体已算好的（抽取阶段生成），并补上标题词
    keyword_text = " ".join(
        entity.search_keywords
        or content_words([m.text for m in entity.factual_mods], extra=[entity.name_zh])
    )

    chunks: list[Chunk] = []
    for idx, text in enumerate(pieces):
        digest = hashlib.sha256(f"{entity.entity_id}#{idx}".encode()).hexdigest()[:16]
        chunks.append(
            Chunk(
                chunk_id=f"{entity.entity_id}#{idx}#{digest}",
                entity_id=entity.entity_id,
                text=text,
                tokens=tokens_for_fts(text),
                keywords=keyword_text,
                metadata=metadata,
            )
        )
    return chunks
