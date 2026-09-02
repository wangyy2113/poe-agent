"""通货 / 圣甲虫 → 知识卡片。

两类卡片结构相同（名字 + 堆叠上限 + 效果原文），圣甲虫多一条地图装置放置上限。
共用一处渲染而不是各写一份——它们的措辞必须一致，否则出口的数值回填会在两种说法之间对不上。

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
from entities.taxonomy import Category, EntityType, SubCategory
from retrieval.tokenize import content_words, tokens_for_fts

MAX_CHUNK_CHARS = 1200


#: 实体类型 → 卡片上的类型名与二级分类。
_KIND = {
    EntityType.CURRENCY: ("通货", SubCategory.CURRENCY.value, Category.ECONOMY),
    EntityType.SCARAB: ("圣甲虫", SubCategory.SCARAB.value, Category.ATLAS),
    EntityType.ESSENCE: ("精华", SubCategory.ESSENCE.value, Category.CRAFTING),
}


def _kind_of(entity: Entity) -> tuple[str, str, Category]:
    return _KIND.get(entity.entity_type, ("物品", SubCategory.CURRENCY.value, Category.ECONOMY))


def render_card(entity: Entity) -> str:
    attrs = entity.attributes
    label, _sub, _cat = _kind_of(entity)
    head = f"{entity.name_zh}（{entity.name_en}）" if entity.name_en else entity.name_zh
    parts: list[str] = [f"# {head}"]
    meta = [f"类型：{label}", f"堆叠上限 {attrs.get('stack_max')}"]
    if attrs.get("device_limit") is not None:
        # 与堆叠上限分开写：混一起会让「最多能放几个」答成 20
        meta.append(f"地图装置放置上限 {attrs['device_limit']}")
    if attrs.get("tier") is not None:
        meta.append(f"精华等阶 T{attrs['tier']}")
    parts.append(" ｜ ".join(meta))
    parts.append("")
    effect = attrs.get("effect_zh")
    if effect:
        parts.append(f"- 效果：{effect}")
    else:
        parts.append(f"- POEDB 页面上没有给这条{label}的效果说明（碎片与命能类多为此形态）")
    parts.extend(_essence_block(attrs))
    parts.append(f"\n来源：{entity.ref.source} → {label}")
    parts.append(entity.ref.url)
    return "\n".join(parts)


def _essence_block(attrs: dict) -> list[str]:
    """精华专有的两段：等级上限与逐装备类别的保底词缀。

    **没有这两段，105 张卡里会有 62 张一模一样**——tier 5–8 的基础效果原文逐字相同，
    只有名字和保底词缀不同。那正是收获目录当初决定不做卡片的理由（近重复文本
    在 BM25 里互相挤掉），这里靠保底词缀把每张卡区分开。

    `level_cap` 为 None 写的是「无等级上限」这个**确定的事实**，不是「未知」——
    tier 5 起本来就没有上限，写成未知会把一条已知信息说成缺口。
    """
    if attrs.get("tier") is None:
        return []
    cap = attrs.get("level_cap")
    out = [
        f"- 适用范围：{f'物品等级需求 {cap} 及以下' if cap is not None else '无等级上限'}"
    ]
    mods = attrs.get("essence_mods") or []
    if mods:
        out.append("")
        out.append("保底词缀（按装备类别）：")
        out += [f"- {m.get('item_classes')}：{m.get('text_zh')}" for m in mods]
    return out


def build_metadata(entity: Entity) -> DocumentMetadata:
    _label, subcategory, category = _kind_of(entity)
    return DocumentMetadata(
        title=f"{entity.name_zh}（{entity.name_en}）" if entity.name_en else entity.name_zh,
        category=category,
        subcategory=subcategory,
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
