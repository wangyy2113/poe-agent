"""地图 → 知识卡片。

一张图一张卡：等阶、头目、额外目标、掉落的命运卡、解锁的工艺台配方、图鉴上相邻的地图。

## 缺的字段写「页面未给」，不留空也不补零

实测 159 张里只有 110 张有等阶（梦魇/传奇等特殊地图没给）。空着会让人以为渲染坏了，
补 0 会让「这张图几阶」答成 0 阶。

## 卡片不写「地图词缀未采集」

那属于覆盖层（规约第 2 条）。把「词缀」写进 159 张卡片，等于给每个含「词缀」的问题
都送一份覆盖率——技能石那轮踩过这个坑。
"""

from __future__ import annotations

import hashlib

from entities.models import Chunk, DocumentMetadata, Entity
from entities.taxonomy import Category, SubCategory
from retrieval.tokenize import content_words, tokens_for_fts

MAX_CHUNK_CHARS = 1200
#: 相邻地图与命运卡最多列几个，超出写出还剩多少——静默截断等同于漏答。
MAX_LIST_ITEMS = 8


def _listing(label: str, values: list[str]) -> str | None:
    if not values:
        return None
    shown = values[:MAX_LIST_ITEMS]
    text = f"- {label}：{'、'.join(shown)}"
    if len(values) > MAX_LIST_ITEMS:
        text += f"（共 {len(values)} 个，以上列出前 {len(shown)} 个）"
    return text


def render_card(entity: Entity) -> str:
    attrs = entity.attributes
    parts: list[str] = [f"# {entity.name_zh}（地图）"]
    tier = attrs.get("tier")
    parts.append(f"等阶：{tier}" if tier else "等阶：POEDB 页面未给（梦魇 / 传奇等特殊地图多为此形态）")
    parts.append("")
    for line in (
        _listing("头目", [str(b) for b in (attrs.get("bosses") or [])]),
        _listing("掉落的命运卡", [str(c) for c in (attrs.get("divination_cards") or [])]),
        _listing("异界图鉴上相邻的地图", [str(l) for l in (attrs.get("links") or [])]),
    ):
        if line:
            parts.append(line)
    if attrs.get("bonus_objective"):
        parts.append(f"- 额外目标：{attrs['bonus_objective']}")
    if attrs.get("bench_recipe"):
        parts.append(f"- 打完头目解锁的工艺台配方：{attrs['bench_recipe']}")
    if attrs.get("atlas_points"):
        parts.append(f"- {attrs['atlas_points']}")
    parts.append(f"\n来源：{entity.ref.source} → 异界图鉴")
    parts.append(entity.ref.url)
    return "\n".join(parts)


def build_metadata(entity: Entity) -> DocumentMetadata:
    return DocumentMetadata(
        title=f"{entity.name_zh}（地图）",
        category=Category.ATLAS,
        subcategory=SubCategory.MAP.value,
        source=entity.ref.source,
        source_priority=entity.ref.priority,
        url=entity.ref.url,
        language="zh-CN",
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
        or content_words([str(b) for b in (entity.attributes.get("bosses") or [])],
                         extra=[entity.name_zh])
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
