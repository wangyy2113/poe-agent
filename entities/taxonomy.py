"""知识分类体系。分类值集中在此，避免字符串散落各处。

一级分类固定为需求 2.6 的六类。
"""

from __future__ import annotations

from enum import StrEnum


class Category(StrEnum):
    """一级分类。"""

    BASICS = "游戏基础"
    ITEM = "装备"
    CRAFTING = "制作系统"
    ATLAS = "地图系统"
    LEAGUE = "联盟机制"
    ECONOMY = "经济系统"


class SubCategory(StrEnum):
    """二级分类（按需扩充；V1 只用到装备类的一部分）。"""

    # 游戏基础
    SKILL = "技能"
    SUPPORT_GEM = "辅助技能"
    PASSIVE = "天赋"
    ASCENDANCY = "升华"
    PANTHEON = "万神殿"

    # 装备
    UNIQUE_ITEM = "唯一装备"
    RARE_ITEM = "稀有装备"
    NORMAL_ITEM = "白装"
    BASE_ITEM = "底材"
    IMPLICIT_MOD = "隐式词缀"
    EXPLICIT_MOD = "显式词缀"
    CORRUPTED_IMPLICIT = "腐化隐式"
    FRACTURED_MOD = "裂隙词缀"
    SYNTHESISED_IMPLICIT = "合成隐式"
    ENCHANT = "附魔"
    QUALITY = "品质"

    # 制作系统
    ESSENCE = "精华"
    FOSSIL = "化石"
    HARVEST = "收获"
    BETRAYAL = "辛迪加"
    BESTIARY = "野兽"
    INCURSION = "神庙"
    CRAFTING_BENCH = "工艺台"

    # 地图系统
    ATLAS = "Atlas"
    MAP = "地图"
    BOSS = "Boss"
    ATLAS_PASSIVE = "Atlas 天赋"

    # 经济系统
    CURRENCY = "通货"
    SCARAB = "圣甲虫"
    DIVINATION_CARD = "命运卡"
    FRAGMENT = "碎片"


class EntityType(StrEnum):
    """实体类型，构成 entity_id 前缀。"""

    UNIQUE_ITEM = "unique_item"
    BASE_ITEM = "base_item"
    MOD = "mod"
    SKILL = "skill"
    SUPPORT_GEM = "support_gem"
    PASSIVE = "passive"
    ASCENDANCY = "ascendancy"
    BOSS = "boss"
    MAP = "map"
    CURRENCY = "currency"


class ModKind(StrEnum):
    """词缀类型。"""

    IMPLICIT = "implicit"
    EXPLICIT = "explicit"
    CORRUPTED_IMPLICIT = "corrupted_implicit"
    FRACTURED = "fractured"
    SYNTHESISED = "synthesised"
    ENCHANT = "enchant"
    CRAFTED = "crafted"
    # POEDB 的 explicitMod 里可能只含 span.item_description，
    # 那是括号说明文本而非真实词条，单独归类以免被当作事实词缀。
    DESCRIPTION = "description"


# 来源名常量，须与 config.yaml 的 sources.priority 键一致
class Source(StrEnum):
    POEDB = "POEDB"
    #: Path of Building 社区版的 src/Data/，由 GGG 客户端 .dat 机器导出。
    #: 数值与结构上更接近一手，但无中文名、无页面说明，故列 P2 而非 P1。
    POB = "Path of Building"
    WIKI = "PoE Wiki"
    CRAFT_OF_EXILE = "Craft of Exile"
    POE_WATCH = "poe.watch"
    PATCH_NOTES = "GGG Patch Notes"
    COMMUNITY = "Community"


#: 可支撑事实断言的来源优先级上限。P7 社区来源不得用于事实断言。
MAX_FACTUAL_PRIORITY = 6


def make_entity_id(entity_type: EntityType | str, slug: str) -> str:
    """全局稳定主键，形如 `unique_item:Headhunter`。"""
    et = entity_type.value if isinstance(entity_type, EntityType) else str(entity_type)
    return f"{et}:{slug}"
