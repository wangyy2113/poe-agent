"""万神殿神祇 → 知识卡片。

一位神祇一张卡：主/次神力标记、逐层（层名 · 中文词缀 · 抓获头目 · 抓获地图）、
以及基础神力来自哪一章的哪个任务。**一神一张而不是一层一张**——用户问「阿拉卡力
之魂有什么效果」时想看到的是四层一起，拆开会让每一层都缺少上下文
（第 3 层的卡片上只有一个头目名和一条词缀，看不出它属于谁）。

## 卡片上只写这一页说得出的话

写「主要神力 / 次要神力」是因为逐神页的 Attr 表上就有 `IsMajor`。
但**不写「同时只能启用一主一次、在城镇免费更换」**——那句话在 `/cn/Pantheon`
的机制说明散文里，不在本卡引用的这一页上。卡片末尾的 `来源` 指向逐神页，
在上面写一句该页没有的话，就是把断言挂到了不含它的引用下。
启用规则与神之容器因此明确划进 `partial_note` 的未采集维度。

## 英文原文附在中文后面

中文词缀取自 POEDB（P1），英文取自 PoB（P2）。两者**不逐项对应**（理由见
`kb/sources/poedb_pantheon.py` 的模块 docstring 与 `pantheon_soul` 的表注释），
所以英文**整层一起列**在中文之后，不做逐条对照——排成两列会让人以为同一行是同一条。

## 不写「本库没有 X」这类边界

那属于覆盖层与 `_answer_notes()`（规约第 2 条）。
"""

from __future__ import annotations

import hashlib

from entities.models import Chunk, DocumentMetadata, Entity
from entities.taxonomy import Category, SubCategory
from retrieval.tokenize import content_words, tokens_for_fts


def _tiers(entity: Entity) -> list[dict]:
    return list(entity.attributes.get("tiers") or [])


def render_card(entity: Entity) -> str:
    attrs = entity.attributes
    is_major = bool(attrs.get("is_major"))
    head = f"{entity.name_zh}（{entity.name_en}）" if entity.name_en else entity.name_zh
    tiers = _tiers(entity)

    parts: list[str] = [f"# {head}"]
    parts.append(
        " ｜ ".join(
            [
                # 「神祇」必须出现在卡片上：这些实体就是神祇，而「万神殿有哪几位神祇」
                # 是最自然的问法之一。第三十五轮实测，卡片只写「神力」时那个词
                # 在全库为零，于是有据的问题被判成「知识库里完全不存在这个词」。
                "类型：万神殿神祇",
                "主要神力" if is_major else "次要神力",
                f"共 {len(tiers)} 层神力",
            ]
        )
    )
    parts.append("")

    for tier in tiers:
        index = tier.get("tier")
        name = tier.get("name_zh") or ""
        if index == 1:
            lead = f"**第 {index} 层（基础神力）** {name}"
            where = []
            if tier.get("act_zh"):
                where.append(str(tier["act_zh"]))
            if tier.get("quest_zh"):
                where.append(f"任务「{tier['quest_zh']}」")
            if where:
                lead += f"　由{'·'.join(where)}给予"
        else:
            lead = f"**第 {index} 层** 抓获 {name}"
            if tier.get("map_zh"):
                lead += f"（地图：{tier['map_zh']}）"
        parts.append(lead)
        for mod in tier.get("mods_zh") or []:
            parts.append(f"- {mod}")
        parts.append("")

    english = [
        line
        for tier in tiers
        for line in (tier.get("mods_en") or [])
    ]
    if english:
        parts.append("英文原文（Path of Building）：")
        for line in english:
            parts.append(f"- {line}")
        parts.append("")

    parts.append(f"来源：{entity.ref.source} → 万神殿")
    parts.append(entity.ref.url)
    return "\n".join(parts)


def build_metadata(entity: Entity) -> DocumentMetadata:
    return DocumentMetadata(
        title=f"{entity.name_zh}（{entity.name_en}）" if entity.name_en else entity.name_zh,
        category=Category.BASICS,
        subcategory=SubCategory.PANTHEON.value,
        source=entity.ref.source,
        source_priority=entity.ref.priority,
        url=entity.ref.url,
        language="zh-CN",
        facets={"is_major": "1" if entity.attributes.get("is_major") else "0"},
        fetched_at=entity.ref.fetched_at,
    )


def to_chunks(entity: Entity) -> list[Chunk]:
    """实体 → 切片。缺 url/source 的实体不产出切片（需求 2.5）。

    卡片实测 326..725 字（最长是四层主神日耀女神），一神一张**不切分**——
    切开会让某一层与「这是谁的神力」分到两张卡上。也因此不需要 `MAX_CHUNK_CHARS`：
    留一个从不生效的阈值，下一个人会以为这里做了长度控制。
    """
    if not entity.indexable:
        return []
    card = render_card(entity)
    digest = hashlib.sha256(f"{entity.entity_id}#0".encode()).hexdigest()[:16]
    keywords = " ".join(
        entity.search_keywords
        or content_words(
            [m.text for m in entity.mods],
            extra=[entity.name_zh, entity.name_en or ""],
        )
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
