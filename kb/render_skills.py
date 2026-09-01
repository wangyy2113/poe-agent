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


#: 属性需求列 → 中文。第三十二轮从 POEDB 详情页的逐级表入库
#: （PoB 侧的 `reqStr/reqDex/reqInt` 是换算系数，不能当显示值用）。
_ATTR_LABEL = {"req_str": "力量", "req_dex": "敏捷", "req_int": "智慧"}


def requirement_text(level: dict) -> str:
    """属性需求。**页面给了才写**：白色宝石三项都是 0，而超过自然最高级的那些
    等级页面根本没给这一列——写成 0 会让「不需要」和「没有数据」变成同一句话。"""
    parts = [
        f"{label} {int(level[key])}"
        for key, label in _ATTR_LABEL.items()
        if level.get(key) is not None
    ]
    return "、".join(parts)


def level_line(level: dict) -> str:
    """一个等级渲染成一行。缺的字段不写——补 0 会把「没有」说成「是 0」。"""
    bits: list[str] = []
    if level.get("level_requirement") is not None:
        bits.append(f"需角色 {level['level_requirement']} 级")
    requirement = requirement_text(level)
    if requirement:
        bits.append(f"属性需求 {requirement}")
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


#: 卡片上最多列几条效果文本。实测 820 颗里最多 6 条、平均 1.7 条，
#: 所以这个上限几乎不触发；留着是因为**静默截断等同于漏答**，超了必须写出还剩多少
#: （与 `tools/impl/mods.py` 同一条约定）。
MAX_CARD_STAT_LINES = 6


def stat_lines_block(attrs: dict) -> list[str]:
    """效果文本区块：每条一行，带 1 级与自然最高级的取值。

    文本原文来自 POEDB 详情页的列头（「造成 25 - 38 冰霜伤害」——里面本来就带着
    1 级的数值），每级的值来自同一张表。**不改写原文**：把它规整成 `#` 占位
    等于自己重写上游文本，而这一段正是要给用户看的那句话。
    """
    lines = [dict(line) for line in (attrs.get("stat_lines") or [])]
    if not lines:
        return []
    levels = {int(lv["gem_level"]): lv for lv in (attrs.get("levels") or [])}
    natural_max = int(attrs.get("natural_max_level") or 0)
    low = levels.get(min(levels), {}) if levels else {}
    high = levels.get(natural_max, {})
    out = ["**效果文本（POEDB 逐级表）**"]
    for line in lines[:MAX_CARD_STAT_LINES]:
        key = str(line.get("index"))
        marks: list[str] = []
        for label, level in ((f"{min(levels)} 级" if levels else "", low),
                             (f"{natural_max} 级", high)):
            value = (level.get("stat_values") or {}).get(key)
            if label and value:
                marks.append(f"{label} {value}")
        text = str(line.get("text_zh") or "")
        out.append(f"- {text}（{'｜'.join(marks)}）" if marks else f"- {text}")
    if len(lines) > MAX_CARD_STAT_LINES:
        out.append(f"（另有 {len(lines) - MAX_CARD_STAT_LINES} 条效果文本，"
                   "用 skill_gem_levels 查完整逐级数据）")
    return out


#: 卡片上最多列几条固定效果。实测 695 颗里**单颗最多 16 条**，所以这个上限
#: 会真的触发——超了必须写出还剩多少（静默截断等同于漏答）。
MAX_CARD_FIXED_LINES = 10


def fixed_stats_block(attrs: dict) -> list[str]:
    """固定效果区块（第四十四轮）：与等级无关的技能机制。

    与「效果文本」的分界是**有没有等级维度**：那一段是「造成 25 - 38 冰霜伤害
    （1 级 …｜20 级 …）」，这一段是「有 25% 的几率对敌人造成冻结状态」。

    **没有就不出区块，也不写「未采集」**：实测 21 颗页面上没有这张表，
    那是「这个技能没有与等级无关的机制」，不是采集缺口——写「未采集」就是一句假话。
    这与获取途径那 152 颗不同：那些是**页面确实没给**这一维，所以那边要写。
    """
    lines = [str(t) for t in (attrs.get("fixed_stats") or []) if str(t).strip()]
    if not lines:
        return []
    out = ["**固定效果（与等级无关）**"]
    out.extend(f"- {text}" for text in lines[:MAX_CARD_FIXED_LINES])
    if len(lines) > MAX_CARD_FIXED_LINES:
        out.append(f"（另有 {len(lines) - MAX_CARD_FIXED_LINES} 条固定效果，"
                   "用 skill_gem_levels 查完整数据）")
    return out


#: 卡片上最多列几条获取途径。实测 668 颗里最多 6 条、平均 1.9 条。
#: 与效果文本同一条约定：**静默截断等同于漏答**，超了必须写出还剩多少。
MAX_CARD_QUEST_LINES = 8

#: 获取途径 → 卡上的说法。`box` 那 21 行是七个职业在绝望岩滩捡到的开局技能。
_QUEST_KIND_LABEL = {
    "quest_reward": "任务奖励",
    "vendor": "向{npc}购买",
    "box": "开局箱子",
}


def acquisition_block(attrs: dict) -> list[str]:
    """获取途径区块（第四十三轮）。

    **只写页面给了的三条途径**（任务奖励 / 任务商人 / 开局箱子）。
    页面没给使命表的那 152 颗（瓦尔、觉醒辅助、契约类）写一句中性事实——
    只说「页面没有给」+「本库未采集」，**不推断「所以靠掉落」**：
    那是经验推断，正是第一原则要拦的东西（与通货那 15 条没有效果文本时
    「留空而不自己补一句」是同一条规矩）。

    整块只有在这一维**导入过**时才出现（`quests_loaded`）。表整张是空的说明
    `import_gem_quests.py` 还没跑，这时写「POEDB 详情页没给」是一句假话。
    """
    if not attrs.get("quests_loaded"):
        return []
    rows = [dict(q) for q in (attrs.get("quests") or [])]
    if not rows:
        return [
            "**获取途径**",
            "- POEDB 详情页未给出任务/商人表，本库未采集该宝石的获取途径",
        ]
    out = ["**获取途径**"]
    for row in rows[:MAX_CARD_QUEST_LINES]:
        kind = str(row.get("source_kind") or "")
        label = _QUEST_KIND_LABEL.get(kind, kind)
        if kind == "vendor":
            label = label.format(npc=row.get("npc_zh") or "商人")
        classes = "、".join(str(c) for c in (row.get("classes") or []))
        quest = row.get("quest_zh") or ""
        bits = f"- 第 {row.get('act')} 章「{quest}」{label}"
        out.append(f"{bits}（{classes}）" if classes else bits)
    if len(rows) > MAX_CARD_QUEST_LINES:
        out.append(f"（另有 {len(rows) - MAX_CARD_QUEST_LINES} 条获取途径，"
                   "用 skill_gem_levels 查完整数据）")
    return out


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

    stat_block = stat_lines_block(attrs)
    if stat_block:
        parts.append("")
        parts.extend(stat_block)

    fixed_block = fixed_stats_block(attrs)
    if fixed_block:
        parts.append("")
        parts.extend(fixed_block)

    quest_block = acquisition_block(attrs)
    if quest_block:
        parts.append("")
        parts.extend(quest_block)

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
