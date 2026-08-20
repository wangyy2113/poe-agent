"""宝石 → 知识卡片。

## 卡片只承担语义检索入口，精确数值走工具

与 `kb/render_mods.py` 同一个分工（那里写了完整理由）：证据在出口按 1200 字截断，
把 20–40 行等级表放进卡片，被截掉的部分会让模型把截断处当成表的结尾。所以卡片只放
**一颗宝石的身份信息 + 三个关键等级**，逐级查询交给 `skill_gem_levels` 工具直查
`pob_skill_level`。

## 卡片里不写「本库未采集伤害数值」

这句话看起来正是「无据不答」要说的，但它必须由**覆盖层**说，不能写进卡片：
充分依据的判据是查询实义词在 `chunk.text` 里的覆盖率，把「伤害」「数值」这些词写进
810 张卡片，等于给每一个含「伤害」的问题都送一份覆盖率——上一轮刚把检索侧漏出压到
0%，靠的就是不让通用词凭空获得覆盖。边界声明属于 `kb/coverage.py` 与
`orchestrator/loop._answer_notes()` 那一层（规约第 2 条：能在上层解决的绝不放到下层）。

同理**卡片不放 `damage_effectiveness`**（「附加伤害的效果 230%」）：它是加成系数、
不是技能伤害，放进检索证据里，模型很容易把它当成「伤害」答出去，而那个数字确实在
证据里、出口闸门拦不住它。工具里可以给——用户问的是那一级的完整数据，标签写全就不会
被误读。
"""

from __future__ import annotations

import hashlib

from entities.models import Chunk, DocumentMetadata, Entity
from entities.taxonomy import Category, EntityType, SubCategory
from retrieval.tokenize import content_words, tokens_for_fts

#: 宝石卡都很短（实测最长 600 字上下），留个上限做保险。
MAX_CHUNK_CHARS = 1200

#: 宝石颜色与类型的中文说法。**公开**给 `tools/impl/gems.py` 复用——
#: 卡片与工具必须用同一套措辞，否则出口的数值回填会在两种说法之间对不上。
COLOR_LABEL = {
    "str": "红色（力量）",
    "dex": "绿色（敏捷）",
    "int": "蓝色（智慧）",
    "none": "白色（无属性需求）",
}

KIND_LABEL = {"active": "技能石", "support": "辅助宝石"}

#: 消耗资源 → 中文。键是 PoB `Data/Costs.lua` 的 `Resource`；
#: 抽取器遇到不在 Costs.lua 里的资源会直接报错，所以这里只需覆盖实际出现的那些。
_COST_LABEL = {
    "Mana": "魔力",
    "Life": "生命",
    "ES": "能量护盾",
    "Rage": "怒火",
    "Soul": "魂",
    "ManaPercent": "% 魔力",
    "LifePercent": "% 生命",
    "ManaPerMinute": "魔力/秒",
    "LifePerMinute": "生命/秒",
    "ESPerMinute": "能量护盾/秒",
    "ManaPercentPerMinute": "% 魔力/秒",
    "LifePercentPerMinute": "% 生命/秒",
    "ESPercentPerMinute": "% 能量护盾/秒",
}


def cost_text(cost: dict[str, float]) -> str:
    """消耗渲染。值已是显示值（抽取器按 Costs.lua 的 Divisor 换算过）。"""
    parts: list[str] = []
    for resource, amount in sorted(cost.items()):
        label = _COST_LABEL.get(resource, resource)
        number = f"{amount:g}"
        parts.append(f"{number}{label}" if label.startswith("%") else f"{number} {label}")
    return "、".join(parts)


#: 伤害类型 → 中文。与 `calc/skill_damage.DAMAGE_TYPE_ZH` 同一份含义，
#: 这里单列一份是为了让卡片渲染不依赖 calc 层（卡片只做排版）。
_DAMAGE_TYPE_ZH = {
    "cold": "冰霜", "fire": "火焰", "lightning": "闪电",
    "chaos": "混沌", "physical": "物理",
}


def level_line(level: dict) -> str:
    """一个等级渲染成一行。缺的字段不写——补 0 会把「没有」说成「是 0」。"""
    bits: list[str] = []
    if level.get("level_requirement") is not None:
        bits.append(f"需角色 {level['level_requirement']} 级")
    cost = cost_text(level.get("cost") or {})
    if cost:
        bits.append(f"消耗 {cost}")
    if level.get("mana_multiplier") is not None:
        # PoB 存的是增量，游戏里显示 100 + 增量（见 GemTooltip.lua）
        bits.append(f"消耗与保留倍率 {level['mana_multiplier'] + 100:g}%")
    # 基础伤害（第二十九轮入库，已与 POEDB 对账）。放在最前面：它是问得最多的一项
    if level.get("base_damage_min") is not None:
        type_zh = _DAMAGE_TYPE_ZH.get(str(level.get("damage_type") or ""), "")
        bits.append(
            f"基础{type_zh}伤害 {level['base_damage_min']:g}–{level['base_damage_max']:g}"
        )
    if level.get("reservation_percent") is not None:
        bits.append(f"保留 {level['reservation_percent']:g}% 魔力")
    # 定值保留（清晰 / 精准 / 活力）。漏了它，这些技能在卡片上看起来「不保留」
    if level.get("reservation_flat") is not None:
        bits.append(f"保留 {level['reservation_flat']:g} 点魔力")
    if level.get("cooldown") is not None:
        text = f"冷却 {level['cooldown']:g} 秒"
        stored = level.get("stored_uses")
        if stored and stored > 1:
            text += f"（可存 {stored} 次）"
        bits.append(text)
    if level.get("crit_chance") is not None:
        bits.append(f"暴击率 {level['crit_chance']:g}%")
    if level.get("attack_speed_multiplier") is not None:
        bits.append(f"攻击速度为基础的 {level['attack_speed_multiplier'] + 100:g}%")
    return "，".join(bits)


def _key_levels(levels: list[dict], natural_max: int) -> list[tuple[str, dict]]:
    """挑三个等级：1 级、自然上限、PoB 数据的最高级。

    为什么是这三个：1 级是入手态，自然上限（多数宝石 20）是常规终态，最高级覆盖
    腐化与 +等级。中间等级没有代表性，而全列会撑破卡片。
    """
    by_level = {int(lv["gem_level"]): lv for lv in levels}
    if not by_level:
        return []
    top = max(by_level)
    picks: list[tuple[str, dict]] = [("1 级", by_level[min(by_level)])]
    if natural_max in by_level and natural_max != min(by_level):
        picks.append((f"{natural_max} 级（自然最高）", by_level[natural_max]))
    if top not in {min(by_level), natural_max}:
        picks.append((f"{top} 级（PoB 数据上限，需腐化或 +等级）", by_level[top]))
    return picks


def render_card(entity: Entity) -> str:
    """渲染宝石卡。数值取自 `entity.attributes`（导入时写入的结构化字段）。"""
    attrs = entity.attributes
    head = f"{entity.name_zh}（{entity.name_en}）" if entity.name_en else entity.name_zh
    parts: list[str] = [f"# {head}"]

    kind = KIND_LABEL.get(str(attrs.get("gem_kind")), "宝石")
    meta_bits = [f"类型：{kind}", COLOR_LABEL.get(str(attrs.get("attr_color")), "颜色未知")]
    flags = []
    if attrs.get("is_vaal"):
        flags.append("瓦尔宝石")
    if attrs.get("is_transfigured"):
        flags.append("蜕变宝石")
    if flags:
        meta_bits.append("、".join(flags))
    parts.append(" ｜ ".join(meta_bits))

    alt = [str(name) for name in (attrs.get("name_zh_alt") or [])]
    if alt:
        parts.append(f"另译：{'、'.join(alt)}")

    tags_zh = [str(t) for t in (attrs.get("tags_zh") or [])]
    if tags_zh:
        parts.append(f"标签：{'、'.join(tags_zh)}")
    if attrs.get("tag_string"):
        parts.append(f"英文标签：{attrs['tag_string']}")

    spec_bits: list[str] = []
    if attrs.get("cast_time") is not None:
        spec_bits.append(f"施放时间 {float(attrs['cast_time']):g} 秒")
    if attrs.get("natural_max_level"):
        spec_bits.append(f"自然最高 {int(attrs['natural_max_level'])} 级")
    if spec_bits:
        parts.append(" ｜ ".join(spec_bits))

    levels = [dict(lv) for lv in (attrs.get("levels") or [])]
    picks = _key_levels(levels, int(attrs.get("natural_max_level") or 0))
    if picks:
        parts.append("")
        for label, level in picks:
            line = level_line(level)
            parts.append(f"- {label}：{line}" if line else f"- {label}")

    if attrs.get("description_en"):
        parts.append(f"\nPoB 英文说明：{attrs['description_en']}")

    parts.append(f"\n来源：{entity.ref.source} → {attrs.get('source_file', '')}")
    parts.append(entity.ref.url)
    if attrs.get("poedb_url"):
        parts.append(f"中文名对照：{attrs['poedb_url']}")
    return "\n".join(parts)


def build_metadata(entity: Entity) -> DocumentMetadata:
    subcategory = (
        SubCategory.SUPPORT_GEM.value
        if entity.entity_type is EntityType.SUPPORT_GEM
        else SubCategory.SKILL.value
    )
    tags = [str(t) for t in (entity.attributes.get("tags_zh") or [])]
    return DocumentMetadata(
        title=f"{entity.name_zh}（{entity.name_en}）" if entity.name_en else entity.name_zh,
        category=Category.BASICS,
        subcategory=subcategory,
        source=entity.ref.source,
        source_priority=entity.ref.priority,
        url=entity.ref.url,
        version=entity.version,
        language="zh-CN",
        tags=tags,
        facets={"gem_kind": str(entity.attributes.get("gem_kind") or "")},
        fetched_at=entity.ref.fetched_at,
    )


def to_chunks(entity: Entity) -> list[Chunk]:
    """实体 → 切片。缺 url/source 的实体不产出切片（需求 2.5）。

    一颗宝石一张卡，**不切分**：卡片已经按「只放身份信息 + 三个关键等级」控制在
    上限内，真超了说明渲染逻辑变了，那时应该改渲染而不是让切分把一颗宝石劈成两半。
    """
    if not entity.indexable:
        return []
    card = render_card(entity)
    metadata = build_metadata(entity)
    digest = hashlib.sha256(f"{entity.entity_id}#0".encode()).hexdigest()[:16]
    keyword_text = " ".join(
        entity.search_keywords
        or content_words(
            [str(t) for t in (entity.attributes.get("tags_zh") or [])],
            extra=[entity.name_zh, entity.name_en or ""],
        )
    )
    return [
        Chunk(
            chunk_id=f"{entity.entity_id}#0#{digest}",
            entity_id=entity.entity_id,
            text=card,
            tokens=tokens_for_fts(card),
            keywords=keyword_text,
            metadata=metadata,
        )
    ]
