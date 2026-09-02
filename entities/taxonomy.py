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
    CRAFT_RULE = "制作规则"

    # 地图系统
    ATLAS = "Atlas"
    MAP = "地图"
    BOSS = "Boss"
    MONSTER_MOD = "怪物词缀"
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
    #: Atlas（异界图鉴）天赋树节点。与 PASSIVE 分开是因为两者是**不同的树**，
    #: 作用域也不同（角色属性 vs 地图产出），混在一起会让「哪些天赋给某属性」
    #: 把地图产出词条也算进角色属性里。
    ATLAS_PASSIVE = "atlas_passive"
    BOSS = "boss"
    #: 怪物身上的词缀（传奇 / 精华 / 魔符 / 惊悸迷雾 …）。与 MOD 分开：
    #: 那个是**装备**词缀，混在一起会让「哪些词缀给抗性」把怪物词缀也算进装备池。
    MONSTER_MOD = "monster_mod"
    MAP = "map"
    CURRENCY = "currency"
    #: 圣甲虫。与 CURRENCY 分开：它是地图机制物品（MapFragment），
    #: 归到通货里会让「通货有哪些」把圣甲虫也算进去。
    SCARAB = "scarab"
    #: 精华**物品本身**（tier / 堆叠 / 等级上限 / 基础效果 + 逐类别保底词缀）。
    #: 与 CURRENCY 分开：精华在 POEDB 上是独立页与独立物品类，归进通货会让
    #: 「通货有哪些」多出 105 条。**与 MOD 也分开**——`mod_affix` 里那 5985 条
    #: `category='essence'` 是精华**赋予的词缀**，是另一个粒度；两者混在一起
    #: 会让「精华的堆叠上限」在词缀行上找答案（实测它原本命中的是精华圣甲虫，
    #: 堆叠 20，而精华是 10——一个准备好的错答案）。
    ESSENCE = "essence"
    #: 制作规则（目前只有前后缀名额）。做成实体是为了**给规则一个出口**：
    #: 第三十七轮把名额规则入了库，但它只被 `check_mod_combination` 内部消费，
    #: 于是「稀有装备最多几个前缀」——这个域自己 question_hints 里就写着的问法——
    #: 检索一条都命中不了，被判成缺据。**入库不等于取得出来。**
    CRAFT_RULE = "craft_rule"
    #: 万神殿神祇。与 PASSIVE / ASCENDANCY 分开：神力既不在天赋树上、也不花天赋点，
    #: 混进去会让「哪些天赋给混沌抗性」把神力算成天赋——那是答非所问且无法验证的。
    PANTHEON = "pantheon"


class ModKind(StrEnum):
    """词缀类型。"""

    IMPLICIT = "implicit"
    EXPLICIT = "explicit"
    CORRUPTED_IMPLICIT = "corrupted_implicit"
    FRACTURED = "fractured"
    SYNTHESISED = "synthesised"
    ENCHANT = "enchant"
    CRAFTED = "crafted"
    #: 天赋节点提供的属性。点了就一直生效。
    #: 不复用 EXPLICIT：那会让「装备上的显式词缀」查询把天赋条目也算进来。
    PASSIVE = "passive"
    #: 万神殿神力提供的属性。启用了就一直生效，但**同时只能开一主一次**——
    #: 这条限制正是不复用 PASSIVE 的理由：天赋点了就都在，神力是互斥选择，
    #: 把 12 位神祇的 41 条词缀当成可同时生效的属性会算出一个不存在的角色。
    PANTHEON = "pantheon"
    #: 大师天赋（Mastery）的**可选**效果。一个大师节点列出多个效果，
    #: 点它时**只能选其中一个**。与 PASSIVE 分开是为了把「选一」这件事
    #: 固化在类型上而不是只写在卡片文案里——一旦只靠文案，
    #: 任何遍历 mods 的代码都会把六个互斥效果当成同时生效的六条属性。
    MASTERY = "mastery"
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
