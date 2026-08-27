"""前后缀名额规则 → 知识卡片。

## 为什么这条规则要有卡片

第三十七轮把名额规则入了库，本意是让 `check_mod_combination` 能判 `possible`。
但那条路径**只在工具内部消费规则**，于是「稀有装备最多几个前缀几个后缀」——
`craft_rule` 这个域自己 `question_hints` 里就写着的问法——检索一条都命中不了，
当场被判成缺据（实测 `sufficient=False`、`covered` 里却有 craft_rule）。

**入库不等于取得出来。** 这是同一句话在本项目里的第四种形态
（前三种：已入库 ≠ 那一维已入库、数据入库但没有出口、常量入库但 `get()` 取不出来）。
所以规则除了进表，还要出一张卡。

## 一张卡而不是七张

七条规则合起来才是「名额规则」这件事；拆成七张卡，问「稀有最多几个前缀」时
会命中一张只写着 `稀有 default 3 3` 的卡片，看不出它是什么、也看不出还有别的档。

## 单侧来源要在卡片上看得见

魔法 1+1 与稀有 3+3 两侧都说；珠宝 / Graft 的 2+2 与「已腐化的深渊珠宝回到 3+3」
只有 PoB 说。卡片按 `agreement` 分组标注——**单侧来源不是冲突，但读的人有权知道
这一条只有一边说过**。
"""

from __future__ import annotations

import hashlib

from entities.models import Chunk, DocumentMetadata, Entity
from entities.taxonomy import Category, SubCategory
from retrieval.tokenize import content_words, tokens_for_fts

#: 规则里 `None` 的读法。写成常量，免得卡片与判定各写一份措辞。
UNLIMITED = "不受前后缀数量限制"

_KIND_ZH = {
    "default": "一般装备",
    "flask": "药剂",
    "jewel": "珠宝",
    "graft": "移植物（Graft）",
    "jewel_abyss_corrupted": "已腐化的深渊珠宝",
}

_AGREEMENT_ZH = {
    "both": "POEDB 与 PoB 两侧一致",
    "poedb_only": "仅 POEDB 载明",
    "pob_only": "仅 PoB 载明",
}


def render_card(entity: Entity) -> str:
    rules = list(entity.attributes.get("rules") or [])
    parts: list[str] = [
        "# 前后缀名额规则（稀有度 × 物品种类）",
        "类型：制作规则 ｜ 共 " + str(len(rules)) + " 条",
        "",
        "| 稀有度 | 物品种类 | 前缀上限 | 后缀上限 | 来源 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for rule in rules:
        prefix = rule.get("prefix_limit")
        suffix = rule.get("suffix_limit")
        limits = (
            (UNLIMITED, "")
            if prefix is None
            else (str(prefix), str(suffix))
        )
        kind = _KIND_ZH.get(str(rule.get("item_kind")), str(rule.get("item_kind")))
        agreement = _AGREEMENT_ZH.get(str(rule.get("agreement")), "")
        parts.append(
            f"| {rule.get('rarity')} | {kind} | {limits[0]} | {limits[1]} | {agreement} |"
        )

    parts.append("")
    for rule in rules:
        text = rule.get("poedb_text") or ""
        if text:
            parts.append(f"- 页面原文：{text}")

    parts.append("")
    parts.append(
        "> 名额只约束**随机的**前后缀。固定词缀（部分底材自带）与腐化、附魔、"
        "灭界者等其他生成位各有各的规则，不占随机前后缀名额。"
    )
    parts.append(f"\n来源：{entity.ref.source} → 制作规则")
    parts.append(entity.ref.url)
    return "\n".join(parts)


def build_metadata(entity: Entity) -> DocumentMetadata:
    return DocumentMetadata(
        title="前后缀名额规则",
        category=Category.CRAFTING,
        subcategory=SubCategory.CRAFT_RULE.value,
        source=entity.ref.source,
        source_priority=entity.ref.priority,
        url=entity.ref.url,
        language="zh-CN",
        facets={},
        fetched_at=entity.ref.fetched_at,
    )


def to_chunks(entity: Entity) -> list[Chunk]:
    if not entity.indexable:
        return []
    card = render_card(entity)
    digest = hashlib.sha256(f"{entity.entity_id}#0".encode()).hexdigest()[:16]
    keywords = " ".join(
        entity.search_keywords or content_words([card], extra=[entity.name_zh])
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
