"""知识库自描述层：这个库**有什么、能精确答什么、每条有多可信**。

## 为什么需要这一层

先前五个看起来无关的毛病，其实是同一个缺失的五种症状：

1. `kb_search` 的 `category` 参数向模型声明六个一级分类可过滤，而库里只有两类
   切片——工具描述在对模型撒谎。
2. `render_gap()` 只能说「检索未命中」，说不出「天赋树本库未采集，去 POEDB
   的 passive-skill-tree 查」。
3. 于是缺口台账里「**没采集**」与「**没召回**」混成一堆，按频次排序驱动采集
   优先级的设计意图落空。
4. 置信度（36.9% 词缀缺生成权重、93.6% 的 T 级是推导来的）已经测出来了，
   却不出现在检索出口，回答无法诚实地说「这条算不出成功率」。
5. 33 个 POEDB 索引页里 31 个只抓进缓存从未入库，这个状态只存在于抓取器的
   常量里，运行时读不到。

共同的根因：**知识库不知道自己是什么样子。** 补的就是这一层。

## 核心设计：inventory 与 status 分离

- **inventory（应该有什么）**：`DOMAINS` 手写常量。来源是 POEDB 导航栏、PoB 的
  数据面清单、需求里承诺的能力范围。它描述「玩家会问的领域」，变化很慢。
- **status（实际有什么）**：运行时探测——直接 count 库里的行、看缓存文件在不在。

**两者的差集就是覆盖缺口。** 这样清单永远不会和实际状态漂移：手写的部分只声明
「这个领域存在」，一条都不声明「我们有它」。文档里 knowledge 用 commit hash 检测
过期，对本项目更简单——数据就在同一个库里，直接数。

## 一条边界

`hints` 是**意图识别**词，不是事实断言。用它判断「用户在问天赋树」，判错的后果
是指路指错，不会输出错误的游戏事实，因此不受「无据不答」的引用约束。这与
`text_zh` 那类事实字段性质不同，不要混用。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from entities.taxonomy import Category

logger = logging.getLogger(__name__)

#: 采集状态。
#:   ingested      已入库且覆盖该领域主体
#:   partial       有部分数据，`partial_note` 必须写清缺什么
#:   harvested     页面已抓到本地缓存，但没有抽取器，一条都没入库
#:   not_collected 完全没有
Status = Literal["ingested", "partial", "harvested", "not_collected"]

STATUS_ZH: dict[str, str] = {
    "ingested": "已采集",
    "partial": "部分采集",
    "harvested": "已抓取未入库",
    "not_collected": "未采集",
}

POEDB_BASE = "https://poedb.tw/cn"


@dataclass(frozen=True)
class DomainSpec:
    """一个知识领域的**声明**。只说「这个领域存在」，不说「我们有它」。"""

    key: str
    display_zh: str
    category: Category
    #: 权威来源。**未采集的领域也必须填**——缺据声明要靠它指路。
    authority_url: str
    #: 条目数探测 SQL。返回单个整数。None 表示连表都还没有。
    count_sql: str | None = None
    #: 子渠道探测 SQL。返回一列文本。
    channel_sql: str | None = None
    #: 该领域的切片 entity_id 前缀。**不能假定等于 key**——词缀领域的 key 是
    #: `mod_affix`（表名），而切片前缀是 `mod_ladder:`（卡片粒度）。
    #: 混同会让 chunk_count 恒为 0，覆盖声明里就少了「有多少可检索卡片」这一维。
    chunk_prefix: str = ""
    #: 能精确查询它的工具名。空表示只能走全文检索。
    tools: tuple[str, ...] = ()
    #: **意图识别**词（不是事实断言）：命中即认为用户在问这个领域。
    hints: tuple[str, ...] = ()
    #: 这个领域能回答哪类问题。相当于文档里 index.md 的路由表。
    question_hints: tuple[str, ...] = ()
    #: 相关的 POEDB 索引页 slug，用于判断「是否已抓到本地」。
    poedb_pages: tuple[str, ...] = ()
    #: 相关的 PoB 数据文件，用于说明缺口的具体位置。
    pob_files: tuple[str, ...] = ()
    #: status=partial 时必须说明缺什么。
    partial_note: str = ""
    #: `partial_note` 里那些未覆盖维度的**意图词**，供 `dimension_gap()` 判定。
    #:
    #: 为什么要有机器可读的一份：`partial_note` 是给人看的散文，判定不能去匹配散文。
    #: 而缺了这一层，`partial` 领域会被当成整体可用——实测「从起点点到神威之盾要花
    #: 几个天赋点」命中天赋节点即判依据充分，而 partial_note 明写着「天赋点消耗与
    #: 连接关系未入库」。两者说的是同一件事，**改一处必须同步另一处**。
    #:
    #: 与 `hints` 的分工：`hints` 判「在问哪个领域」，这里判「问的是该领域里没有的
    #: 那一面」。两者都是意图识别词，判错只会指路指错，不会输出错误的游戏事实。
    missing_dimension_hints: tuple[str, ...] = ()
    note: str = ""
    #: 这个领域是否是**属性的独立来源**——即「某属性能从哪获得」的完整枚举必须
    #: 覆盖它。
    #:
    #: 用途：`kb/stat_channels.py` 据此派生「哪些渠道尚未采集」的声明。以前那句
    #: 声明是手写在工具描述里的（「天赋树、星团珠宝天赋、技能石、升华等」），
    #: 天赋树入库当天它就变成了假话——这正是覆盖层要消灭的那类漂移。
    #:
    #: 只标**独立**来源：精华 / 化石 / 工艺台 / 隐匿 / 纠缠都是 `mod_affix` 的
    #: 子渠道，标了会重复声明；市场价格、联盟机制、游戏常量不产出装备属性，不标。
    provides_stats: bool = False


#: ─────────────────────────────────────────────────────────────────
#: inventory：应该有哪些领域
#:
#: 粒度取「采集与否是个干净的是/否」那一层。太粗（比如整个「装备」）会把
#: 已采集的词缀和未采集的底材混在一起，缺据声明就又说不清了。
#: ─────────────────────────────────────────────────────────────────
DOMAINS: tuple[DomainSpec, ...] = (
    # ── 装备 ──
    DomainSpec(
        key="unique_item",
        display_zh="唯一装备",
        category=Category.ITEM,
        authority_url=f"{POEDB_BASE}/Unique_item",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='unique_item'",
        chunk_prefix="unique_item:",
        channel_sql=(
            "SELECT DISTINCT COALESCE("
            "  json_extract(attributes,'$.pane'),"
            "  CASE WHEN json_extract(attributes,'$.via')='detail'"
            "       THEN '详情页补全' ELSE '类别页补集' END"
            ") FROM entity WHERE entity_type='unique_item'"
        ),
        tools=("entity_lookup",),
        hints=("唯一装备", "传奇装备", "暗金", "传奇物品"),
        question_hints=(
            "某件唯一装备有哪些词缀 / 需求等级 / 底材",
            "按效果描述反查是哪件唯一装备",
        ),
        poedb_pages=("Unique_item",),
        provides_stats=True,
    ),
    DomainSpec(
        key="mod_affix",
        display_zh="装备词缀（T 级 / 物品等级 / 生成权重）",
        category=Category.ITEM,
        authority_url=f"{POEDB_BASE}/Modifiers",
        count_sql="SELECT count(*) FROM mod_affix",
        channel_sql="SELECT DISTINCT category_label FROM mod_affix WHERE category_label<>''",
        chunk_prefix="mod_ladder:",
        tools=("check_mod_combination", "mod_tiers", "stat_sources"),
        hints=("词缀", "前缀", "后缀", "T1", "T级", "词缀组", "互斥", "物品等级", "权重"),
        question_hints=(
            "某底材类别上某条词缀有几档、各档物品等级与权重",
            "一条词缀能从哪些来源获得（掉落池 / 精华 / 工艺台 / 影响力 …）",
            "两条词缀是否互斥",
        ),
        poedb_pages=("Modifiers",),
        provides_stats=True,
    ),
    DomainSpec(
        key="pob_mod",
        display_zh="词缀英文对照与按底材的权重向量",
        category=Category.ITEM,
        authority_url="https://github.com/PathOfBuildingCommunity/PathOfBuilding",
        count_sql="SELECT count(*) FROM pob_mod",
        channel_sql="SELECT DISTINCT mod_kind FROM pob_mod",
        tools=(),
        hints=(),
        question_hints=("词缀的英文原文", "同一词缀在不同底材标签上的权重差异"),
        # 用通配而不是列举：`Mod*.lua` 实测 26 个，逐个写既冗长又会随上游增删而过期。
        # 先前这里第三项写的是「…共 26 个」——**一句人话混在路径元组里**，
        # 而这个字段当时没有任何消费者，所以没人发现（第三十九轮由单测抓出来）。
        pob_files=("Data/Mod*.lua",),
        note="P2 来源，与 POEDB 交叉校验用；冲突落 source_conflict",
    ),
    DomainSpec(
        key="base_item",
        display_zh="底材（基础物品）",
        category=Category.ITEM,
        authority_url=f"{POEDB_BASE}/Items",
        count_sql="SELECT count(*) FROM pob_base_item",
        # 渠道按类别看：18 个装备部位各算一个渠道，比原先按护甲属性标签分更有信息量
        channel_sql="SELECT DISTINCT category_key FROM pob_base_item",
        tools=("affix_pool_odds",),
        hints=("底材", "基础物品", "白装底子", "胸甲底材"),
        question_hints=(
            "某底材的中英文名、PoB 标签、隐式词缀原文与 POEDB 页面",
            "某底材的基础数值（护甲/武器伤害/药剂）与属性需求",
            "具体胸甲底材在明确物等和生成侧的普通词缀有效池",
        ),
        poedb_pages=("Items", "Body_Armours"),
        pob_files=("Data/Bases/*.lua",),
        partial_note=(
            "已入库 **18 个装备类别共 1,022 条**底材（PoB × POEDB 中英文清单逐条精确桥接，"
            "覆盖 PoB 侧 96.1%）：中英文名、ItemClass、标签、影响力标签、隐式词缀原文、"
            "基础数值与属性需求都有；"
            "**尚缺**：41 条未桥接（实验底材 19 条、PoB 变体展开 13 条、伪条目 9 条，"
            "已按类别登记在缺口台账）、可获得性与稀有度、影响力适用规则、词缀容量；"
            "**按属性分的词缀页映射只有胸甲有**，所以条件权重概率仍只能算胸甲"
        ),
        missing_dimension_hints=(
            "可获得性", "稀有度", "掉落等级", "词缀容量", "影响力要求",
        ),
        note=(
            "英文名与生成标签来自钉 commit 的 PoB P2；中文名与 slug 来自 POEDB P1；"
            "两侧不唯一时整条拒绝"
        ),
        provides_stats=True,
    ),
    # ── 游戏基础 ──
    DomainSpec(
        key="game_constant",
        display_zh="游戏常量（抗性上限 / 减伤上限 / 怪物等级表）",
        category=Category.BASICS,
        authority_url="https://github.com/PathOfBuildingCommunity/PathOfBuilding",
        count_sql="SELECT count(*) FROM game_constant",
        channel_sql="SELECT DISTINCT group_name FROM game_constant",
        tools=(
            "resistance_calc", "mitigation_calc", "recovery_calc",
            "reservation_calc", "ailment_dot_calc", "ailment_threshold_calc",
            "max_hit_calc", "crit_calc", "accuracy_calc", "attack_speed_calc",
        ),
        hints=("抗性上限", "减伤上限", "格挡上限", "怪物生命", "常量"),
        question_hints=(
            "抗性 / 物理减伤 / 格挡的上限是多少",
            "某等级怪物的基础生命",
            "抗性怎么算、超抗有什么用、地图减抗之后还剩多少",
            "护甲能减多少伤害、多少闪避躲得开某等级怪物、格挡上限与法术压制",
            "护盾多久回满、偷取的速率与单次上限、魔力再生每秒多少",
            "开几个光环还剩多少魔力、点燃/中毒/流血每秒打多少",
            "打某等级怪物/头目能感电多少、要感电满需要多大伤害",
            "这套池子一发最多能挨多大的伤害（抗性 + 护甲 + 减伤合起来反解）",
            "暴击率上限与平均伤害倍率（某技能/武器的基础暴击查库）",
            "我这点命中值打某等级怪物命中率多少、要多少命中才不 miss",
            "这把武器每秒攻击几次、加了攻速之后攻击间隔多少",
        ),
        # `Modules/Data.lua` 是 PoB 自己的 magic numbers 表，按白名单取（当前只取
        # 抗性上下限两项）——判据见 `kb/sources/pob_misc.MODULE_MISC_WHITELIST`
        pob_files=("Data/Misc.lua", "Modules/Data.lua"),
        note=(
            "不含前后缀数量上限与影响力规则——Misc.lua 里没有，需另找来源；"
            "抗性、受击结算（闪避/命中、格挡、法术压制、护甲）与回复"
            "（护盾回充、偷取、魔力再生）已有计算器（复算 PoB 原式）；"
            "命中 / DPS（要先有 effectiveness 公式）、伤害转化下的 Max Hit、"
            "冰冻时长曲线等维度还没有计算器"
        ),
    ),
    DomainSpec(
        key="passive_tree",
        display_zh="天赋树节点（基石 / 核心 / 大师 / 普通）",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/passive-skill-tree",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='passive'",
        channel_sql=(
            "SELECT DISTINCT json_extract(attributes,'$.node_kind_zh') "
            "FROM entity WHERE entity_type='passive'"
        ),
        chunk_prefix="passive:",
        tools=("stat_sources", "passive_path"),
        hints=("天赋", "天赋树", "天赋点", "被动", "基石", "大师天赋", "点数"),
        question_hints=(
            "某个天赋节点给什么属性",
            "某属性能从哪些天赋节点获得",
            "某个大师天赋有哪些可选效果",
            "点到某节点要花几个天赋点、路径怎么走",
        ),
        poedb_pages=("passive-skill-tree",),
        pob_files=("TreeData/{ver}/tree.lua",),
        partial_note=(
            "节点属性、**连接关系（3,382 条边）与职业起点**都已入库，"
            "「点到某节点要花几点、怎么走」可由 `passive_path` 按职业算出并给出逐节点路径；"
            "**仍缺**：星团珠宝上的天赋（不在主树上）、天赋点的获取来源明细"
            "（任务奖励逐条），以及「这条路径值不值得点」这类取舍建议（那不是事实问题）"
        ),
        # 2026-08-19 第十五轮：点数与连接关系已入库，从这份清单里**移除**——
        # 留着就等于让闸门把能答的问题声明成缺口。上一轮在幽魂的抗性/排名上刚做过同样的事。
        # 剩下的两维确实还没有：星团珠宝天赋是独立领域（cluster_jewel，未采集），
        # 而「值不值得点」是取舍建议，本项目不做。
        missing_dimension_hints=("星团", "值不值得", "该不该点", "推荐加点"),
        note=(
            "数据取自 POEDB 的天赋树 JSON 端点（页面本身是 JS 空壳，抓页面拿不到数据）；"
            "升华节点单列 ascendancy 域"
        ),
        provides_stats=True,
    ),
    DomainSpec(
        key="cluster_jewel",
        display_zh="星团珠宝天赋（可选核心与大点）",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Cobalt_Jewel",
        count_sql="SELECT count(*) FROM pob_cluster_notable",
        tools=("cluster_jewel_info",),
        hints=("星团", "星团珠宝", "大点", "小点"),
        question_hints=(
            "各尺寸星团珠宝能加几个小天赋",
            "哪些基石只能从星团珠宝获得",
            "某个核心天赋是不是星团珠宝上的",
        ),
        pob_files=("Data/ClusterJewels.lua",),
        partial_note=(
            "已入库 **3 个尺寸**（小 2–3 / 中 4–6 / 大 8–12 个小天赋，含核心位与插槽位数）、"
            "**55 条小天赋类型**（仅 PoB 英文原文——中文附魔文本不在已缓存的任何一页上）、"
            "以及 **308 条星团核心天赋与基石清单**（中文名由已入库的天赋卡桥接，100% 命中）；"
            "**未采集**：哪些核心天赋出现在哪种小天赋类型上（`ClusterJewels.lua` 只给"
            "「尺寸 → 类型」与一份扁平清单，树 JSON 的节点字段里也没有 affliction 标签），"
            "以及星团珠宝自身的掉落与价格"
        ),
        # 「能开出哪些」正是那条未采集的映射。问到就拒答，不拿 308 条里的一部分去凑。
        missing_dimension_hints=(
            "能开出", "会开出", "开出哪些", "哪种类型", "哪个类型", "词缀池",
            "掉落", "价格", "多少钱", "值多少",
        ),
        note=(
            "易误判：ModJewelCluster.lua 早就入库了，但那是星团珠宝**自身的词缀**，"
            "不是它能开出的天赋定义——后者在 ClusterJewels.lua，2026-08-19 第十八轮接上。"
            " 另一处易误判：那 300 个核心天赋的**属性**一直都在库里（随天赋树入库成天赋卡），"
            "这一轮补的是**分类**（它们只能从星团珠宝拿、不在主树上）。"
        ),
        provides_stats=True,
    ),
    DomainSpec(
        key="skill_gem",
        display_zh="技能石",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Skill_Gems",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='skill'",
        chunk_prefix="skill:",  # 实体前缀是 entity_type，与 key 不同
        tools=("skill_gem_levels",),
        # 判据必须**专指技能石**，这是踩过三次的坑（曾把「怪物」当 boss 意图、
        # 「知识库」当路由意图、差点把「降临」当 POE2 标记）：装备说「物品等级」
        # 「需求等级」，角色说「角色等级」，所以这里只收明确指向宝石的说法。
        hints=("技能石", "主动技能", "技能宝石", "宝石等级", "技能等级"),
        question_hints=("某技能的等级需求、消耗、冷却与标签", "某宝石是什么颜色"),
        poedb_pages=("Skill_Gems", "Transfigured_Gems", "Gem"),
        pob_files=("Data/Gems.lua", "Data/Skills/*.lua"),
        partial_note=(
            "宝石清单、中英文名、颜色、标签、施放时间、**每级字面值**（等级需求、"
            "消耗、冷却、可存次数、暴击率、消耗与保留倍率）、**每级基础伤害**"
            "（按 PoB 的 effectiveness 公式算出的显示值，175 颗技能 7000 行，"
            "已与 POEDB 技能页对账）与**每级效果文本、属性需求（力/敏/智）**"
            "（取自 POEDB 详情页的逐级表，812 颗，逐级与 PoB 数值对过账）"
            "与**获取途径**（同一张详情页的「使命」表，668 颗共 1261 行："
            "第几章、哪个使命、任务奖励还是向哪个商人买、哪些职业）已入库；"
            "**DPS 与掉落来源未采集**（另外 152 颗瓦尔/觉醒辅助/契约类页面上没有使命表，"
            "它们靠掉落）"
        ),
        # 这份清单删过三轮：第二十九轮删掉伤害问法（基础伤害入库并对过账），
        # 第三十二轮删掉**属性需求**（POEDB 详情页的逐级表给了力/敏/智，819 颗逐级对过账），
        # 第四十三轮删掉**获取途径**那四条。
        # 留着就等于让闸门把答得出来的问题声明成缺口——与第十四轮幽魂抗性、
        # 第二十四轮定值保留是同一件事，而且这一条**必须与 `partial_note` 同改**
        # （规约第 5 条的成对点：一句话散文 + 一份机器判据说的是同一件事）。
        #
        # DPS 仍然留着：它要攻速与整条加成链，基础伤害只是其中一个乘数。
        #
        # 「怎么获得」这一族第四十三轮**删掉了**。先前的注释写着「任务奖励与掉落
        # 不在任何已缓存页上」——**那句是错的**：第三十二轮为取效果文本抓的 820 张
        # 详情页里就有「使命」表（章节｜使命｜NPC｜角色），668 颗有、共 1261 行。
        # 与万神殿、Boss 那两次是同一个错误：停在一页上就下「缺来源」的结论。
        # 另外 152 颗（瓦尔/觉醒/契约类）页面上确实没有这张表，它们的诚实回答由
        # 卡片与工具里那句**逐颗**的中性事实承担（「详情页未给出使命表，本库未采集」），
        # 而不是由这份**整域**的判据承担——整域说「未采集」会在 668 颗上变成假话。
        #
        # **「掉落」留着**，因为那 152 颗的掉落来源确实没有。一开始担心它
        # 「在唯一装备、通货文本里到处都是」而没敢加——**那个担心是错的**：
        # 这份判据匹配的是**问句**，而且只在本域**已被识别**（宝石意图词或
        # 别名精确命中一颗宝石）之后才生效。实测「冈姆的壮志哪里掉落」命中的是
        # 唯一装备，压根不会走到这里（`test_唯一装备的伤害问题不被宝石维度误拦`
        # 钉的就是这条）。判据描述的是**问句形式 + 已识别的域**，不是语料里的词频。
        #
        # 不放宽成「等级」或「伤害」：那会误拦「冈姆的壮志的需求等级是多少」
        # （黄金集里的反例哨兵 num_kaoms_req_level，答案 68 就在库里）。
        missing_dimension_hints=("dps", "每秒伤害", "掉落"),
        provides_stats=True,
    ),
    DomainSpec(
        key="support_gem",
        display_zh="辅助宝石",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Support_Gems",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='support_gem'",
        tools=("skill_gem_levels",),
        hints=("辅助", "辅助宝石", "辅助石"),
        question_hints=("某辅助宝石的等级需求、消耗倍率与冷却", "某辅助宝石是什么颜色"),
        poedb_pages=("Support_Gems", "Exceptional"),
        pob_files=("Data/Gems.lua",),
        partial_note=(
            "辅助宝石清单、中英文名、颜色、标签、**每级字面值**（等级需求、"
            "消耗与保留倍率、冷却、可存次数）与**每级效果文本、属性需求**"
            "（取自 POEDB 详情页的逐级表，逐级与 PoB 数值对过账）"
            "与**获取途径**（同一张详情页的「使命」表：第几章、哪个使命、"
            "任务奖励还是向哪个商人买、哪些职业）已入库；"
            "**掉落来源未采集**（觉醒辅助、契约类页面上没有使命表，它们靠掉落）"
        ),
        # 「加成多少」这类问法**第三十二轮从这份清单里移除**：辅助宝石给什么加成、
        # 每级多少，POEDB 详情页的逐级表逐条写着（「被辅助技能附加 4 - 6 基础冰霜伤害」
        # 1 级 4,6 → 20 级 152,228）。这一维先前是这个域最常被问、又答不了的一块。
        # 与 `skill_gem` 成对：第四十三轮同样删掉「怎么获得 / 哪里获得」
        # （详情页的使命表覆盖了 173 颗辅助宝石）、同样留下「掉落」
        # （觉醒辅助与契约类靠掉落，本地无来源），理由与判据见上一个域的注释。
        missing_dimension_hints=("dps", "掉落"),
        provides_stats=True,
    ),
    DomainSpec(
        key="ascendancy",
        display_zh="升华节点",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Ascendancy_class",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='ascendancy'",
        channel_sql=(
            "SELECT DISTINCT json_extract(attributes,'$.ascendancy_zh') "
            "FROM entity WHERE entity_type='ascendancy'"
        ),
        chunk_prefix="ascendancy:",
        tools=("stat_sources",),
        hints=("升华", "升华节点", "职业", "守望者", "刺客"),
        question_hints=("某升华节点的效果", "某效果由哪个升华提供"),
        poedb_pages=("Ascendancy", "Ascendancy_class", "Bloodline_Ascendancy_class"),
        partial_note=(
            "升华**节点效果**已入库（随天赋树 JSON 一起）；"
            "升华试炼流程、升华点数获取、职业总览未采集"
        ),
        missing_dimension_hints=(
            "试炼", "迷宫", "怎么解锁", "如何解锁", "升华点", "职业总览", "有哪些职业",
        ),
        provides_stats=True,
    ),
    DomainSpec(
        key="pantheon",
        display_zh="万神殿",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Pantheon",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='pantheon'",
        chunk_prefix="pantheon:",
        tools=("stat_sources",),
        hints=("万神殿", "神殿加成", "神力", "之魂"),
        question_hints=(
            "某神祇之魂的各层效果",
            "某层神力要抓获哪个头目、在哪张地图",
            "某效果由哪位神祇提供",
        ),
        poedb_pages=("Pantheon",),
        pob_files=("Data/Pantheons.lua",),
        provides_stats=True,
        partial_note=(
            "已入库 **12 位神祇 / 32 层神力 / 41 条词缀**（PoB `Data/Pantheons.lua` 出结构、"
            "英文名、数值与 GGG stat id；POEDB 逐神页 `/cn/Soul_of_<英文名>` 出中文名、"
            "中文词缀、**抓获的头目与地图**、以及基础神力来自哪一章的哪个任务；"
            "两侧 `(stat_id, value)` 逐条对账 41/41 全对）；"
            "**未采集**：启用规则与神之容器的机制说明（「同时只能开一主一次」"
            "「怎么强化」在 `/cn/Pantheon` 的散文里，不在逐神页上）"
        ),
        missing_dimension_hints=(
            "神之容器", "怎么启用", "如何启用", "怎么切换", "怎么更换", "同时能开",
            "强化", "怎么解锁", "如何解锁",
        ),
        note=(
            "2026-08-21 第三十五轮：这个域此前挂着一条**错的**结论——"
            "「缺的是来源而不是抽取器」。`/cn/Pantheon` 确实没有各神祇的神力文本"
            "（那半句是对的），但逐神页 `/cn/Soul_of_<英文名>` 一直都在，"
            "而且这条 spec **自己就写着** `pob_files=(\"Data/Pantheons.lua\",)`——"
            "本机 PoB 检出里那个文件也一直在。`pob_files` 没有任何运行时消费者，"
            "所以没人回头看它。**停在一页上就下「缺来源」的结论，与第三十二轮"
            "宝石那次是同一个错。**"
        ),
    ),
    # ── 制作系统 ──
    DomainSpec(
        key="essence",
        display_zh="精华",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Essence",
        count_sql="SELECT count(*) FROM mod_affix WHERE category='essence'",
        tools=("stat_sources",),
        hints=("精华", "精华词缀"),
        question_hints=("某精华赋予什么词缀", "某词缀由哪个精华赋予"),
        poedb_pages=("Essence",),
        partial_note="精华**赋予的词缀**已入库；精华物品本身的等级、掉落与兑换成本未采集",
        missing_dimension_hints=("掉落", "兑换", "成本", "怎么获得", "如何获得", "哪里刷"),
    ),
    DomainSpec(
        key="fossil",
        display_zh="化石与共鸣器",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Modifiers",
        count_sql="SELECT count(*) FROM mod_affix WHERE category='delve'",
        tools=("stat_sources",),
        hints=("化石", "共鸣器", "地心探索"),
        question_hints=("化石能出什么词缀",),
        partial_note="化石**词缀池**已入库；化石对权重的乘数与共鸣器组合规则未采集",
        missing_dimension_hints=("乘数", "共鸣器组合", "组合规则", "怎么搭配", "成功率"),
    ),
    DomainSpec(
        key="crafting_bench",
        display_zh="工艺工作台目录（选项 / 原始费用 / 适用类别 / 解锁）",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Crafting_Bench",
        count_sql="SELECT count(*) FROM craft_bench_recipe",
        channel_sql=(
            "SELECT DISTINCT c.currency_name_zh FROM craft_bench_recipe_cost c "
            "WHERE c.currency_name_zh<>''"
        ),
        tools=("bench_recipe_catalog", "stat_sources"),
        hints=("工艺台", "工艺工作台", "改造台", "手工"),
        question_hints=(
            "工艺工作台页面列有哪些效果、原始通货费用、适用 ItemClasses 与解锁信息",
        ),
        poedb_pages=("Crafting_Bench",),
        partial_note=(
            "目录、原始费用、适用 ItemClasses 与解锁信息已入库；"
            "**装备状态转移、crafted-mod 上限、替换/移除语义未采集**，"
            "因此目录项不代表在给定装备状态下可执行，也不能作为制作步骤。"
            "元词缀**页面矩阵**已单独采集（见 metamod_interaction_matrix），"
            "但它只复述单元格，不给出元词缀对本目录项的约束规则"
        ),
        missing_dimension_hints=(
            "状态转移", "替换", "移除", "可执行", "制作步骤", "步骤", "成功率", "怎么做",
        ),
        note="工艺台词缀本体仍可由 stat_sources 查询；目录查询不提供概率或市场成本",
    ),
    DomainSpec(
        key="harvest",
        display_zh="收获（古灵庄园）工艺",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Horticrafting",
        count_sql="SELECT count(*) FROM harvest_craft",
        channel_sql=(
            "SELECT DISTINCT currency_name_zh FROM harvest_craft_cost "
            "WHERE currency_name_zh<>''"
        ),
        tools=("harvest_craft_catalog",),
        hints=("收获", "园艺", "古灵庄园", "生命力", "命能"),
        question_hints=(
            "收获有哪些定向工艺选项",
            "某条收获工艺要多少命能",
            "某种命能能用来做什么",
        ),
        poedb_pages=("Horticrafting", "Harvest"),
        partial_note=(
            "已入库 **74 条定向工艺**与 **98 条命能费用明细**（效果原文 + 原始费用，"
            "费用通货按 GGG 元数据路径与 `poedb_currency` 逐条对账，5/5 命中）；"
            "**未采集**：命能的**获取途径**（同页 51 行作物与掉落来源那张表）、"
            "成功率与权重、制作步骤、市场成本，以及同页 41 行"
            "催化/化石/工艺台/花园工艺站混排表"
        ),
        missing_dimension_hints=(
            "怎么获得", "如何获得", "哪里获得", "获取途径", "作物", "种植", "掉落",
            "成功率", "概率", "几率", "权重", "制作步骤", "步骤", "怎么做",
        ),
        note=(
            "**没有卡片，只有工具**（与工艺台同理）：74 条描述里 16 条只差一个词"
            "（「重铸……其中包括一个 X 词缀」），做成卡片会让 BM25 在近重复文本里"
            "互相挤掉。所以 `probe()` 报的 chunk 数是 0，这不是缺陷。"
        ),
    ),
    # 注意：辛迪加给的是**隐匿**词缀（POEDB category=veiled，中文标签「隐匿」），
    # 与下面「纠缠词缀」（category=haunted，亡者之城机制）是两回事。
    # 两者都是后缀来源、都要解锁，极易混淆——混了会把制作路径指向完全不同的机制。
    DomainSpec(
        key="betrayal",
        display_zh="辛迪加（隐匿词缀解锁）",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Betrayal",
        count_sql="SELECT count(*) FROM mod_affix WHERE category='veiled'",
        tools=("stat_sources",),
        hints=("辛迪加", "隐匿", "隐匿词缀", "情报网", "尤金"),
        question_hints=("隐匿词缀有哪些", "某条隐匿词缀怎么解锁"),
        poedb_pages=("Betrayal",),
        partial_note="隐匿**词缀**已入库；情报网的成员、部门与解锁流程未采集",
        missing_dimension_hints=(
            "成员", "部门", "怎么解锁", "如何解锁", "解锁流程", "情报网", "怎么运作",
        ),
    ),
    DomainSpec(
        key="necropolis",
        display_zh="亡者之城（纠缠词缀）",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Modifiers",
        count_sql="SELECT count(*) FROM mod_affix WHERE category='haunted'",
        tools=("stat_sources",),
        hints=("纠缠", "纠缠词缀", "亡者之城", "墓地", "尸体"),
        question_hints=("纠缠词缀有哪些", "某条纠缠词缀在哪个部位可用"),
        partial_note="纠缠**词缀**已入库；尸体种类、墓地布局与调度规则未采集",
        missing_dimension_hints=("尸体种类", "墓地布局", "调度", "怎么摆", "布局"),
        note="易与辛迪加的「隐匿」词缀混淆，两者是不同机制",
    ),
    DomainSpec(
        key="craft_feasibility",
        display_zh="目标词缀组合局部可行性（候选 / family 互斥 / 占位）",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Modifiers",
        count_sql=(
            "SELECT count(*) FROM mod_affix "
            "WHERE json_valid(family_list) AND json_array_length(family_list)>0"
        ),
        tools=("check_mod_combination",),
        hints=("词缀组合", "能否共存", "是否共存", "同时存在", "前后缀占位"),
        question_hints=(
            "指定底材上的多个目标词缀是否存在 family_list 冲突",
            "目标词缀分别占前缀还是后缀",
        ),
        partial_note=(
            "已支持候选解析、完整 family_list 互斥、占位见证与**前后缀名额判定**"
            "（第三十七轮入库，因此 `possible` / 超名额的 `impossible` 都已可达）；"
            "**来源适用条件与影响力规则仍未采集**，候选含非常规来源或底材带影响力时"
            "仍只能返回 unknown。元词缀**页面矩阵**已采集，"
            "但矩阵值不能把 unknown 变成 possible"
        ),
        missing_dimension_hints=(
            "成功率", "成本", "步骤", "影响力", "怎么做", "最快", "最经济",
        ),
        note=(
            "`possible` 的含义是**这些词缀能共存**，不是「可以制作」；"
            "工具不提供制作步骤、概率或成本。"
            "不给稀有度时判不了名额，结论停在 unknown——不替用户假定「稀有」"
        ),
    ),
    DomainSpec(
        key="affix_pool_odds",
        display_zh="胸甲普通词缀有效池与条件权重概率",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Modifiers",
        count_sql=(
            "SELECT count(*) FROM mod_affix WHERE category='normal' AND weight>0 "
            "AND item_class_page LIKE 'Body_Armours_%'"
        ),
        channel_sql=(
            "SELECT DISTINCT item_class_page FROM mod_affix WHERE category='normal' "
            "AND weight>0 AND item_class_page LIKE 'Body_Armours_%'"
        ),
        tools=("affix_pool_odds",),
        hints=("有效池", "权重池", "原始权重概率", "条件概率", "词缀池"),
        question_hints=(
            "具体胸甲底材在明确物品等级、前缀或后缀侧的 normal 有效档位与总权重",
            "唯一目标 correct_group 在完整同侧 normal 池中的条件权重概率",
        ),
        poedb_pages=(
            "Body_Armours_str", "Body_Armours_dex", "Body_Armours_int",
            "Body_Armours_str_dex", "Body_Armours_str_int", "Body_Armours_dex_int",
            "Body_Armours_str_dex_int",
        ),
        pob_files=("Data/Bases/body.lua", "Data/ModExplicit.lua"),
        partial_note=(
            "只支持逐字具体胸甲底材、明确物等、prefix/suffix、normal 来源且未应用"
            "已有 family 排除的下一次同侧加权选择；**不是通货/工艺成功率**，"
            "不含当前装备状态、容量、影响力、特殊来源、步骤、实际期望通货、成本或路线"
        ),
        note="权重池以 POEDB P1 mod_affix 为真源；PoB P2 只支撑底材标签与交叉审计",
    ),
    DomainSpec(
        key="metamod_matrix",
        display_zh="元词缀交互页面矩阵（operation × guard）",
        category=Category.CRAFTING,
        authority_url="https://www.craftofexile.com/metamods",
        count_sql="SELECT count(*) FROM craft_metamod_interaction",
        channel_sql=(
            "SELECT DISTINCT section FROM craft_metamod_interaction WHERE section<>''"
        ),
        tools=("metamod_interaction_matrix",),
        hints=("元词缀矩阵", "工艺互动矩阵", "metamod matrix", "metamods矩阵"),
        question_hints=(
            "来源矩阵中某个逐字 operation 与 guard 的结果和原始注释",
        ),
        poedb_pages=("metamods",),
        partial_note=(
            "只收录来源页面有明确值的单元格；**空白格、当前版本适用性、给定装备"
            "可执行性、完整状态转移、步骤、概率、成本和路线均不提供**"
        ),
        note=(
            "原始内容来源为 Craft of Exile P4，经 POEDB 镜像采集；"
            "不得因镜像宿主误标为 POEDB P1"
        ),
    ),
    DomainSpec(
        key="craft_rule",
        display_zh="制作规则（前后缀名额 / 影响力 / 元词缀）",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Modifiers",
        count_sql="SELECT count(*) FROM craft_affix_capacity",
        tools=("check_mod_combination",),
        hints=("几个前缀", "几个后缀", "名额", "影响力", "元词缀", "觉醒者之石"),
        question_hints=(
            "稀有装备最多几前缀几后缀",
            "影响力词缀需要什么底材",
            "元词缀如何约束制作操作",
        ),
        poedb_pages=("metamods", "Modifiers"),
        pob_files=("Classes/Item.lua",),
        partial_note=(
            "**前后缀名额规则已入库**（7 条：魔法 1+1、稀有 3+3、传奇不受限制、"
            "药剂随魔法，另有 PoB 独有的珠宝与 Graft 2+2 与"
            "「已腐化的深渊珠宝回到 3+3」这条例外；魔法与稀有两侧对账一致）；"
            "**影响力底材条件与元词缀操作状态机仍未结构化**——"
            "元词缀只有页面矩阵，矩阵值不能把 unknown 变成 possible"
        ),
        missing_dimension_hints=(
            "影响力", "元词缀", "觉醒者之石", "状态转移", "制作步骤", "步骤", "怎么做",
        ),
        note=(
            "这个域是三件事的合称，本轮只补上了第一件（名额）。"
            "别因为它从 harvested 变成 partial 就以为影响力与元词缀规则也有了"
        ),
    ),
    # ── 地图系统 ──
    DomainSpec(
        key="map",
        display_zh="地图与地图词缀",
        category=Category.ATLAS,
        authority_url=f"{POEDB_BASE}/Maps",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='map'",
        chunk_prefix="map:",
        hints=("地图", "图鉴", "地图词缀"),
        question_hints=(
            "某地图的等阶、头目与额外目标",
            "某地图在图鉴上连着哪些图",
            "某地图会掉落哪些命运卡",
        ),
        partial_note=(
            "已入库 **159 张地图**：等阶（其中 110 张有，梦魇/传奇等特殊地图页面未给）、"
            "头目、额外目标、掉落的命运卡、打完头目解锁的工艺台配方，以及"
            "**异界图鉴上相邻的地图**（159 张全有）；"
            "**未采集**：地图词缀（`Maps` 页那 12 条只有未翻译的内部 stat id）、"
            "地图内部布局、完整掉落表、以及地图物品本身的隐式词缀"
        ),
        missing_dimension_hints=(
            "地图词缀", "词缀有哪些", "布局", "怎么走", "掉落表", "隐式",
        ),
        poedb_pages=("Maps", "Atlas", "Atlas_of_Worlds"),
        note="PoB 的 ModMap.lua 已明确排除（手写、无权重无区间），地图词缀须走 POEDB",
    ),
    DomainSpec(
        key="atlas_passive",
        display_zh="Atlas 天赋（异界图鉴天赋树）",
        category=Category.ATLAS,
        authority_url=f"{POEDB_BASE}/atlas-skill-tree",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='atlas_passive'",
        channel_sql=(
            "SELECT DISTINCT json_extract(attributes,'$.node_kind_zh') "
            "FROM entity WHERE entity_type='atlas_passive'"
        ),
        chunk_prefix="atlas_passive:",
        tools=("stat_sources",),
        hints=("异界天赋", "atlas天赋", "图鉴天赋", "异界图鉴天赋"),
        question_hints=("某异界天赋的效果", "某地图产出效果由哪些异界天赋提供"),
        poedb_pages=("atlas-skill-tree",),
        partial_note="节点属性已入库；天赋点消耗与连接关系未入库",
        missing_dimension_hints=(
            "要花", "花几", "几点", "点数", "消耗", "连接", "怎么走", "路径", "路线",
        ),
        note="作用于**地图产出**而非角色属性，回答时不要与角色天赋树混为一谈",
        provides_stats=True,
    ),
    DomainSpec(
        key="boss",
        display_zh="Boss 技能与应对",
        category=Category.ATLAS,
        authority_url=f"{POEDB_BASE}/Monsters",
        count_sql="SELECT count(*) FROM pob_boss_skill",
        tools=("boss_skill_damage",),
        hints=("boss", "首领", "怪物技能", "怪物机制", "打法", "怎么打", "应对"),
        question_hints=(
            "某 Boss 技能在某等级打多少伤害、穿多少抗性",
            "Uber 变体比普通变体多打多少",
        ),
        poedb_pages=("Monsters",),
        pob_files=("Data/BossSkills.lua", "Data/Bosses.lua"),
        partial_note=(
            "已入库 **10 条 Boss 技能的伤害建模**（伤害系数 / 穿透 / Uber 倍率 / 速度 / 暴击）"
            "与 **22 个 Boss 的护甲与闪避倍率**，真实伤害按 PoB 原式配 `monsterDamageTable` "
            "算出（`boss_skill_damage` 工具）；"
            "**未采集**：技能的**机制说明与应对打法**（PoB 只给建模，POEDB 的 Monsters 页"
            "也没有打法）、Boss 的掉落、以及这些终局 Boss 的**中文名**"
            "（本地没有译名来源，只有英文）"
        ),
        missing_dimension_hints=(
            "打法", "怎么打", "应对", "机制", "阶段", "站位", "怎么躲", "掉落",
        ),
        note=(
            "hints 里刻意**不放单独的「怪物」与「机制」**。这两个词在词缀与唯一装备"
            "文本里到处都是（「当你击败稀有怪物时」），拿它们做领域意图识别会把有据的"
            "装备问题误判到本领域：实测「击败稀有怪物获得其属性修正的腰带」与"
            "「猎首获得稀有怪物属性修正持续多久」都因此被维度闸门拦成缺据，"
            "属错误拒答。领域意图词必须足够专指。"
            " 2026-08-19 从「Boss 与怪物」改名为「Boss 技能与应对」：那张缓存页里"
            "**没有** Boss 技能与打法，只有怪物词缀、幽魂倍率与按等级的基础数值表。"
            "前两者已各自单列成域（monster_mod / spectre），第三类 PoB 侧早在"
            " game_constant 里。原先把三件事挂在一个域名下，"
            "让「已抓取未入库」读起来像是差一个抽取器。"
            " 2026-08-27 第三十八轮：当时那句「缺的不是抽取器而是**来源**」"
            "**又一次只对了一半**。POEDB 确实没有 Boss 技能，但这条 spec 的"
            " `pob_files` 当时是**空元组**，而 PoB 检出里一直有 `Data/BossSkills.lua`"
            "与 `Data/Bosses.lua`。`pob_files` 至今没有任何运行时消费者（纯文档字段），"
            "所以又一次没人回头看它——**同一个空元组骗了第二次**（第一次是万神殿）。"
            "补进来的是**伤害建模**而不是打法：这条分界写在 partial_note 里，"
            "别因为域从 harvested 变成 partial 就以为打法也有了。"
        ),
    ),
    DomainSpec(
        key="monster_mod",
        display_zh="怪物词缀（传奇 / 魔符 / 惊悸迷雾 / 猎魔）",
        category=Category.ATLAS,
        authority_url=f"{POEDB_BASE}/Monsters",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='monster_mod'",
        chunk_prefix="monster_mod:",
        # 「传奇」单独用不得：POE1 里装备稀有度也叫传奇，会把唯一装备问题拉进本域。
        # 同理不用裸的「精华」（那是制作系统的域）与裸的「怪物」（词缀文本里到处都是）。
        hints=("怪物词缀", "传奇怪物", "怪物身上的词缀", "魔符", "惊悸迷雾", "猎魔"),
        question_hints=("某个怪物词缀是什么效果", "传奇怪物会带哪些词缀"),
        poedb_pages=("Monsters",),
        partial_note=(
            "已入库**页面上有中文文本的 183 条**（来源、怪物等级、效果原文）；"
            "**63 条只有未翻译的内部 stat id、248 条页面只给了词缀名没有效果文本，"
            "这两类未入库**；怪物词缀的生成权重与出现规则也未采集"
        ),
        missing_dimension_hints=("生成权重", "出现几率", "多大概率", "权重"),
    ),
    DomainSpec(
        key="spectre",
        display_zh="可作幽魂的怪物与倍率",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Monsters",
        count_sql="SELECT count(*) FROM poedb_spectre",
        tools=("spectre_stats",),
        # 这个域**没有卡片**（见 kb/render_monsters.py），所以 chunk_prefix 留空，
        # 覆盖声明里 chunk_count 为 0 是正确状态而不是漏建索引。
        hints=("幽魂", "亡灵召唤"),
        question_hints=("某怪物作为幽魂的伤害与生命倍率", "某怪物能不能当幽魂"),
        poedb_pages=("Monsters",),
        partial_note=(
            "已入库 1,948 个可作幽魂的怪物与它们的**伤害% / 生命%**（POEDB），"
            "其中 268 个另有 PoB 建模的**四抗性、护甲/闪避/能量护盾系数与攻击间隔**；"
            # 这里**不写具体数值**（原先写着「测试之灵 10000%」）：partial_note 会注入
            # 系统提示词，模型照着复述那个数字就是无绑定断言，会被自己的出口闸门拦掉
            # ——实测录制里就拦到了这一句。数字留在 BASELINE 与代码注释里。
            "**倍率排名只在这 268 个里做**（POEDB 清单里混着开发用怪物，不参与排名）；"
            "**掉落与怪物技能的中文说明未采集**——技能在 PoB 侧只有内部 id"
            "（实测 1,235 个 id 与本库 810 颗宝石零命中，它们不是玩家宝石）"
        ),
        # 2026-08-19 第十四轮：抗性与排名已入库，从这份清单里**移除**——
        # 留着就是「项目要求模型说的话被自己的闸门拦掉」的镜像版本：
        # 能答的问题被自己声明成缺口。剩下的两维（掉落、技能说明）确实还没有。
        #
        # 「技能」是本域已被识别后才参与判定（判定顺序见 `dimension_gap`），
        # 所以不会误伤「技能石」那类问题——那句话里没有「幽魂」。
        missing_dimension_hints=("掉落", "技能", "会放什么技能", "打法"),
    ),
    DomainSpec(
        key="scarab",
        display_zh="圣甲虫",
        category=Category.ATLAS,
        authority_url=f"{POEDB_BASE}/Scarab",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='scarab'",
        chunk_prefix="scarab:",
        # 「圣甲虫」这个词足够专指，不像「混沌石」那样会被当计价单位提起。
        # 具体圣甲虫名同样由别名实体通道识别（那条通道只能收紧）。
        hints=("圣甲虫",),
        question_hints=("某个圣甲虫的效果是什么", "某个圣甲虫在地图装置里最多放几个"),
        poedb_pages=("Scarab",),
        partial_note=(
            "已入库 **118 个圣甲虫**：中英文名、效果原文、堆叠上限与"
            "**地图装置放置上限**（页面上的「限制」，与堆叠上限是两回事）；"
            "**未采集**：掉落来源与获取途径、价格（属「市场价格」域）、"
            "以及页面上那 7 张「提到圣甲虫的词缀表」（地图/箱子词缀、工艺台解锁、密教成员阶级）"
        ),
        missing_dimension_hints=(
            "怎么获得", "哪里获得", "哪里刷", "掉落", "价格", "多少钱", "值多少", "兑换",
        ),
    ),
    # ── 经济系统 ──
    DomainSpec(
        key="currency",
        display_zh="通货",
        category=Category.ECONOMY,
        authority_url=f"{POEDB_BASE}/Currency",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='currency'",
        chunk_prefix="currency:",
        # **只留「通货」这一个泛化词**，具体通货名交给别名实体通道识别
        # （`dimension_gap` 的 exact_entity_ids，实体命中只能收紧不能放宽）。
        #
        # 为什么不把「混沌石」这类名字放进来：它们经常是**计价单位或操作名**而不是主体。
        # 实测两条漏出：「猎首现在值多少混沌石」与「元词缀矩阵里异能混沌石对…是什么结果」——
        # 通货域因命中「混沌石」被记为 covered，于是价格缺口与矩阵空白格缺口都被取消了
        # （「有一个领域覆盖到就不拦」）。走实体通道就没有这个问题：那条通道只能加 uncovered。
        hints=("通货",),
        question_hints=("某通货的作用是什么", "某通货的堆叠上限"),
        poedb_pages=("Currency", "Scarab"),
        partial_note=(
            "已入库 **113 条通货**：中英文名、堆叠上限、效果原文（其中 **15 条**"
            "在 POEDB 上没有效果文本——碎片与命能/结晶类，留空而不是自己补一句）；"
            "**未采集**：获取途径与掉落来源、兑换比例、碎片合成数量，以及价格"
            "（价格属「市场价格」域，经济源未接入）。"
            "圣甲虫已单列成域（2026-08-19 第十七轮入库）"
        ),
        # 「多少钞」是社区对混沌石价格的说法。价格问题会同时命中 economy_price 域，
        # 这里也列上是为了在只问通货时也拦住——两处都拦不冲突，漏一处才是问题。
        missing_dimension_hints=(
            "怎么获得", "哪里获得", "哪里刷", "掉落", "兑换", "多少钞", "价格", "多少钱",
            "值多少", "合成", "几个碎片",
        ),
    ),
    DomainSpec(
        key="economy_price",
        display_zh="市场价格",
        category=Category.ECONOMY,
        authority_url="https://api.poe.watch",
        count_sql=None,
        hints=("价格", "多少钱", "多少C", "值多少", "成本", "行情"),
        question_hints=("某物品当前价格", "某制作方案的通货成本"),
        note="经济源未接入。缺它，任何「多少钱 / 期望消耗」的问题都无法回答",
    ),
    # ── 联盟机制 ──
    DomainSpec(
        key="league_mechanic",
        display_zh="联盟机制",
        category=Category.LEAGUE,
        authority_url="https://www.pathofexile.com/forum/view-forum/patch-notes",
        count_sql=None,
        hints=("赛季", "联盟", "新机制"),
        question_hints=("当前赛季有什么机制",),
        note="赛季名须从 GGG api/leagues 动态解析，不得硬编码",
    ),
)


@dataclass
class DomainStatus:
    """一个领域的**实测**状态。"""

    spec: DomainSpec
    status: Status
    item_count: int = 0
    chunk_count: int = 0
    channels: list[str] = field(default_factory=list)
    #: 已知的置信度限制，逐条实测得出（见 `_confidence_notes`）
    confidence_notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def usable(self) -> bool:
        """能否用于回答问题。"""
        return self.status in ("ingested", "partial")

    def one_line(self) -> str:
        """注入 prompt 的一行摘要。"""
        bits = [f"{self.spec.display_zh}（{STATUS_ZH[self.status]}"]
        if self.item_count:
            bits.append(f"，{self.item_count:,} 条")
        if self.channels:
            bits.append(f"，{len(self.channels)} 个来源渠道")
        bits.append("）")
        line = "".join(bits)
        if self.status == "partial" and self.spec.partial_note:
            line += f"　缺：{self.spec.partial_note}"
        elif not self.usable:
            line += f"　权威来源：{self.spec.authority_url}"
        return line


# ── 探测 ──────────────────────────────────────────────────────────

def _scalar(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int | None:
    """执行计数 SQL。表不存在等错误返回 None，不抛——探测不该让启动失败。"""
    try:
        row = conn.execute(sql, args).fetchone()
    except sqlite3.Error as exc:
        logger.debug("覆盖探测失败（%s）：%s", sql, exc)
        return None
    return int(row[0]) if row and row[0] is not None else 0


def _column(conn: sqlite3.Connection, sql: str) -> list[str]:
    try:
        return [str(r[0]) for r in conn.execute(sql) if r[0]]
    except sqlite3.Error as exc:
        logger.debug("渠道探测失败（%s）：%s", sql, exc)
        return []


def _confidence_notes(conn: sqlite3.Connection, key: str) -> list[str]:
    """实测该领域的置信度限制。

    **数字必须现算，不能写死。** 写死的比例会随数据更新变成假话，
    而它出现在回答里时看起来同样可信——那正是「错的出处比没有出处更糟」。
    """
    notes: list[str] = []
    if key != "mod_affix":
        return notes
    total = _scalar(conn, "SELECT count(*) FROM mod_affix") or 0
    if not total:
        return notes

    def pct(sql: str) -> tuple[int, float]:
        n = _scalar(conn, sql) or 0
        return n, 100.0 * n / total

    n, p = pct("SELECT count(*) FROM mod_affix WHERE weight IS NULL OR weight=0")
    if n:
        notes.append(
            f"{p:.1f}%（{n:,} 条）没有生成权重 → 这些词缀的**成功率无法计算**，"
            "不得给出概率"
        )
    n, p = pct("SELECT count(*) FROM mod_affix WHERE tier_source='derived'")
    if n:
        notes.append(
            f"{p:.1f}% 的 T 级是按 POEDB 前端算法推导的，非页面原文直接给出"
            "（算法已复刻并通过不变量校验）"
        )
    n, p = pct("SELECT count(*) FROM mod_affix WHERE ranges='[]'")
    if n:
        notes.append(f"{p:.1f}% 是固定值词缀（无数值区间），反查时文本须完全一致")
    n = _scalar(
        conn,
        "SELECT count(*) FROM (SELECT loose_template, item_class_page "
        "FROM mod_affix GROUP BY loose_template, item_class_page "
        "HAVING count(DISTINCT correct_group || '|' || category) > 1)",
    ) or 0
    if n:
        notes.append(
            f"{n:,} 个（宽松模板 × 底材类别）组合横跨多个词缀组或来源 → "
            "仅凭数值无法唯一定档，回答时必须并列可能来源"
        )
    return notes


def pob_data_root() -> Path | None:
    """本机 PoB 检出的 `src/` 目录；没有就返回 None（不报错——PoB 是可选依赖）。"""
    try:
        from kb.sources.pob import resolve_pob_root  # noqa: PLC0415  避免导入环

        root, _origin = resolve_pob_root()
    except Exception:  # noqa: BLE001  没有 PoB 检出是正常情况，不该让覆盖探测失败
        return None
    return root / "src"


def _pob_file_present(spec: DomainSpec) -> bool:
    """该域声明的 PoB 文件是否真的在本机检出里。

    路径里可能有通配（`Data/Bases/*.lua`）与占位（`TreeData/{ver}/tree.lua`）：
    含 `{` 的一律跳过（那是写给人看的模板，不是可解析的路径），
    含 `*` 的走 glob。**匹配不上不报错**——这里只用来分辨
    harvested 与 not_collected，判错的代价是一个状态字，不是一条事实断言。
    """
    root = pob_data_root()
    if root is None or not spec.pob_files:
        return False
    for raw in spec.pob_files:
        if "{" in raw:
            continue
        if "*" in raw:
            if any(root.glob(raw)):
                return True
        elif (root / raw).is_file():
            return True
    return False


def probe(
    conn: sqlite3.Connection, *, cache_dir: Path | None = None
) -> list[DomainStatus]:
    """探测全部领域的实际状态。

    判定顺序：有数据 → ingested / partial；没数据但**页面在本地缓存里、
    或声明的 PoB 文件在本机检出里** → harvested；否则 not_collected。

    `pob_files` 那一支是第三十九轮补的。在那之前它是**纯文档字段、零运行时消费者**，
    于是连着两轮被同一种方式坑：万神殿（第三十五轮）与 Boss（第三十八轮）的 spec 上
    都写着「缺的是来源」，而 PoB 检出里一直有对应文件——一个没有消费者的字段，
    写了也等于没写，写错更没人发现。现在它至少决定 `harvested` 与 `not_collected`
    的分野：**PoB 侧有文件而库里没数据，就该报「已抓取未入库」而不是「未采集」**，
    因为前者的含义正是「拿数据这步已经做完了，缺的是抽取器」。
    """
    from kb.fetcher import cache_path_for  # noqa: PLC0415  避免循环导入

    out: list[DomainStatus] = []
    for spec in DOMAINS:
        count = _scalar(conn, spec.count_sql) if spec.count_sql else None
        prefix = spec.chunk_prefix or f"{spec.key}:"
        chunk_n = (
            _scalar(conn, "SELECT count(*) FROM chunk WHERE entity_id LIKE ?", (f"{prefix}%",))
            or 0
        )

        if count:
            status: Status = "partial" if spec.partial_note else "ingested"
        else:
            harvested = False
            if cache_dir is not None and spec.poedb_pages:
                harvested = any(
                    cache_path_for(cache_dir, f"{POEDB_BASE}/{slug}").is_file()
                    for slug in spec.poedb_pages
                )
            harvested = harvested or _pob_file_present(spec)
            status = "harvested" if harvested else "not_collected"

        out.append(
            DomainStatus(
                spec=spec,
                status=status,
                item_count=count or 0,
                chunk_count=chunk_n,
                channels=_column(conn, spec.channel_sql) if spec.channel_sql else [],
                confidence_notes=_confidence_notes(conn, spec.key),
            )
        )
    return out


def refresh(conn: sqlite3.Connection, *, cache_dir: Path | None = None) -> int:
    """探测并写入 `kb_coverage`。返回领域数。"""
    rows = probe(conn, cache_dir=cache_dir)
    conn.execute("DELETE FROM kb_coverage")
    conn.executemany(
        """
        INSERT INTO kb_coverage(
            domain_key, display_zh, category, status, item_count, chunk_count,
            channels, authority_url, tools, hints, question_hints,
            confidence_notes, partial_note, note, probed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        [
            (
                s.spec.key, s.spec.display_zh, s.spec.category.value, s.status,
                s.item_count, s.chunk_count,
                json.dumps(s.channels, ensure_ascii=False),
                s.spec.authority_url,
                json.dumps(list(s.spec.tools), ensure_ascii=False),
                json.dumps(list(s.spec.hints), ensure_ascii=False),
                json.dumps(list(s.spec.question_hints), ensure_ascii=False),
                json.dumps(s.confidence_notes, ensure_ascii=False),
                s.spec.partial_note, s.spec.note,
            )
            for s in rows
        ],
    )
    by_status: dict[str, int] = {}
    for s in rows:
        by_status[s.status] = by_status.get(s.status, 0) + 1
    logger.info("覆盖声明已刷新：%d 个领域 %s", len(rows), by_status)
    return len(rows)


# ── 消费侧 ────────────────────────────────────────────────────────

def load(conn: sqlite3.Connection) -> list[DomainStatus]:
    """从 `kb_coverage` 读回。表为空时**回退到实时探测**，避免忘了刷新就变瞎。"""
    try:
        rows = conn.execute("SELECT * FROM kb_coverage ORDER BY domain_key").fetchall()
    except sqlite3.Error:
        rows = []
    if not rows:
        return probe(conn)

    expected_keys = {domain.key for domain in DOMAINS}
    stored_keys = {str(row["domain_key"]) for row in rows}
    if stored_keys != expected_keys:
        logger.warning(
            "kb_coverage 快照与当前 inventory 不一致（缺少=%s，多出=%s），改用实时探测",
            sorted(expected_keys - stored_keys),
            sorted(stored_keys - expected_keys),
        )
        return probe(conn)

    by_key = {d.key: d for d in DOMAINS}
    out: list[DomainStatus] = []
    for r in rows:
        spec = by_key.get(r["domain_key"])
        if spec is None:
            # 库里有、inventory 里没有：说明 DOMAINS 被删过。跳过并告警，
            # 不要静默当它不存在。
            logger.warning("kb_coverage 里有未声明的领域 %s，已忽略", r["domain_key"])
            continue
        out.append(
            DomainStatus(
                spec=spec,
                status=r["status"],
                item_count=r["item_count"] or 0,
                chunk_count=r["chunk_count"] or 0,
                channels=json.loads(r["channels"] or "[]"),
                confidence_notes=json.loads(r["confidence_notes"] or "[]"),
            )
        )
    return out


def usable_categories(conn: sqlite3.Connection) -> list[str]:
    """当前**确实有数据**的一级分类。

    `kb_search` 的 `category` 参数枚举由此动态生成——先前它固定声明六类，
    而其中四类一条数据都没有，等于在对模型撒谎。
    """
    cats: list[str] = []
    for s in load(conn):
        if s.usable and s.spec.category.value not in cats:
            cats.append(s.spec.category.value)
    return cats


@dataclass
class GapAttribution:
    """一次查不到的归因结果。"""

    #: 命中的未采集领域
    missing_domains: list[DomainStatus] = field(default_factory=list)
    #: 命中的已采集领域（说明是召回问题，不是采集问题）
    covered_domains: list[DomainStatus] = field(default_factory=list)

    @property
    def kind(self) -> Literal["not_collected", "not_recalled", "unknown"]:
        if self.missing_domains:
            return "not_collected"
        if self.covered_domains:
            return "not_recalled"
        return "unknown"

    def explain(self) -> str:
        """给缺据声明用的一段说明。"""
        if self.kind == "not_collected":
            lines = ["问题涉及的领域**本知识库尚未采集**，不是检索没找到："]
            for d in self.missing_domains:
                lines.append(
                    f"- {d.spec.display_zh}（{STATUS_ZH[d.status]}）"
                    f"　权威来源：{d.spec.authority_url}"
                )
            return "\n".join(lines)
        if self.kind == "not_recalled":
            names = "、".join(d.spec.display_zh for d in self.covered_domains)
            return (
                f"问题涉及的领域（{names}）本知识库**已采集**，"
                "但这次没有召回到足够依据——可能是用词与库中表述不一致。"
                "换用更具体的专名或词缀原文再试可能有效。"
            )
        return "无法判断问题属于哪个已声明领域，可能超出本助手的服务范围。"

    def category_label(self) -> str:
        """写进缺口台账的 `guessed_category`，让台账能按渠道归因。"""
        if self.missing_domains:
            return f"not_collected/{self.missing_domains[0].spec.key}"
        if self.covered_domains:
            return f"not_recalled/{self.covered_domains[0].spec.key}"
        return "unknown"


def attribute_gap(conn: sqlite3.Connection, question: str) -> GapAttribution:
    """判断一个查不到的问题是「未采集」还是「未召回」。

    靠 `hints` 做**意图识别**——判错只会指路指错，不会输出错误的游戏事实，
    因此不受引用约束。命中多个领域时，未采集的优先报告：那是更根本的原因。
    """
    result = GapAttribution()
    for s in load(conn):
        if not any(h and h in question for h in s.spec.hints):
            continue
        (result.covered_domains if s.usable else result.missing_domains).append(s)
    return result


@dataclass
class DimensionVerdict:
    """问题**所问的那个维度**上，本库到底有没有数据。

    为什么需要它：充分依据判定只回答「查询词或实体是否出现在库里」。而
    「实体在库里」不等于「被问的那一面有数据」——实测两条漏出都出在这个缝隙上：

      「猎首现在值多少混沌石」  别名精确命中猎首 → 判依据充分，可价格维度
                                （economy_price）一个字都没有
      「点到神威之盾要花几个天赋点」 命中天赋节点 → 判依据充分，可 passive_tree 的
                                partial_note 明写「天赋点消耗与连接关系未入库」

    这一层就是把 (inventory × status) 那份自描述数据接到充分性判定上。
    它**只能把「充分」改判为「不足」**，反过来不行——放宽依据要有新证据，
    而这里没有引入任何新证据。
    """

    #: 命中且该维度确实有数据的领域
    covered: list[DomainStatus] = field(default_factory=list)
    #: 命中但该维度没有数据的领域（未采集 / 已抓未入库 / partial 且问的正是缺的那面）
    uncovered: list[DomainStatus] = field(default_factory=list)
    #: uncovered 中每个领域被判定的具体原因，与 `uncovered` 等长
    why: list[str] = field(default_factory=list)

    @property
    def is_gap(self) -> bool:
        """全部命中的领域都没有该维度的数据时才判缺据。

        **有一个领域覆盖到就不拦**：复合问题（「猎首有哪些词缀，值多少钱」）里
        有据的部分照答、无据的部分单独声明，这是需求里写明的拒答粒度——
        整轮拦掉就把有据的那半也丢了。此时缺口由提示词的「覆盖边界」章节负责声明。
        """
        return bool(self.uncovered) and not self.covered

    def reason(self) -> str:
        """写进缺据声明的「判定依据」。逐条说清是未采集还是维度缺失。"""
        parts = [
            f"{s.spec.display_zh}（{STATUS_ZH[s.status]}）：{w}"
            for s, w in zip(self.uncovered, self.why)
        ]
        return "问题所问的维度在知识库中没有数据 —— " + "；".join(parts)

    def missing_points(self) -> list[str]:
        """缺据声明里「缺失的信息点」。"""
        return [f"{s.spec.display_zh}的数据" for s in self.uncovered]

    def authority_urls(self) -> list[str]:
        return [s.spec.authority_url for s in self.uncovered]

    def category_label(self) -> str:
        if self.uncovered:
            return f"not_collected/{self.uncovered[0].spec.key}"
        return "unknown"


def dimension_gap(
    conn: sqlite3.Connection,
    question: str,
    *,
    exact_entity_ids: Sequence[str] = (),
) -> DimensionVerdict:
    """判断问题所问的维度有没有数据。

    与 `attribute_gap()` 的分工：那个在**已经判定缺据之后**归因（是没采集还是没召回），
    这个在**判定充分之后**复核（被问的维度是否真的有数据）。判错只会指路指错，
    不会输出错误的游戏事实。

    ## 领域识别有两条通道：意图词，和**实体精确命中**

    只靠 `hints` 不够，因为专名本身就是最强的领域信号，而专名不可能进词表：
    「冰霜脉冲的伤害是多少」里没有任何一个技能石意图词，可它点名了一颗库里的宝石——
    810 颗宝石的名字不能写成 `hints`。所以别名精确命中的实体，其类型所属的领域也算
    「被问到了」，随后照旧由 `missing_dimension_hints` 判断问的是不是缺的那一面。

    `exact_entity_ids` **只收别名通道（`channel="alias"`）的命中**，不收 BM25 的。
    区别是决定性的：别名命中意味着用户点名了这个实体；而全文检索的前若干条里
    夹着一张宝石卡是常事，拿它当「在问技能石」，任何含「伤害」的装备问题都会被误拦。
    """
    prefixes: list[tuple[str, DomainStatus]] = []
    verdict = DimensionVerdict()
    statuses = load(conn)
    for s in statuses:
        prefixes.append((s.spec.chunk_prefix or f"{s.spec.key}:", s))

    asked_by_entity = {
        status.key
        for prefix, status in prefixes
        if any(str(eid).startswith(prefix) for eid in exact_entity_ids)
    }

    matched_hints: dict[str, list[str]] = {}
    for s in statuses:
        hits = [h for h in s.spec.hints if h and h in question]
        hinted = bool(hits)
        matched_hints[s.key] = hits
        if not hinted and s.key not in asked_by_entity:
            continue
        # 只靠实体命中识别出来的领域**只能进 uncovered，不能进 covered**。
        #
        # 两条通道的证明力不同：意图词命中说明「在问这个领域的这一面」，而实体命中
        # 只说明「句子里点了这个领域的一个专名」。把后者也算成 covered 会放宽闸门——
        # 实测反例：「猎首现在值多少混沌石」别名命中猎首（unique_item 已全量入库），
        # 于是 covered 非空，而 `is_gap` 要求「没有任何领域覆盖到」，价格缺口就被放过了
        # （2026-08-19 评测里 gap_price_headhunter 因此漏出）。
        #
        # 这一层的不变量是**只能收紧**（见类 docstring），所以这里直接跳过。
        entity_only = not hinted
        if not s.usable:
            verdict.uncovered.append(s)
            verdict.why.append(
                "该领域一条都没有入库"
                + (f"，权威来源 {s.spec.authority_url}" if s.spec.authority_url else "")
            )
            continue
        asked_missing = [
            h for h in s.spec.missing_dimension_hints if h and h in question
        ]
        if s.status == "partial" and asked_missing:
            verdict.uncovered.append(s)
            verdict.why.append(
                f"该领域只是部分采集，而问题涉及的「{'、'.join(asked_missing[:3])}」"
                f"正落在未采集的部分（{s.spec.partial_note or '见覆盖声明'}）"
            )
            continue
        if entity_only:
            continue
        verdict.covered.append(s)

    _drop_subsumed_coverage(verdict, matched_hints)
    return verdict


def _drop_subsumed_coverage(
    verdict: DimensionVerdict, matched_hints: dict[str, list[str]]
) -> None:
    """更专指的意图词命中时，被它**包含**的泛化命中不算覆盖。

    为什么需要：`is_gap` 的规则是「有一个领域覆盖到就不拦」，那是给**复合问题**写的
    （「猎首有哪些词缀，值多少钱」要答词缀那半）。但同一个主语被两个领域用不同精度的
    词命中时，那条规则会让泛化的一方把专指的一方的缺口取消掉。

    实测反例：「惊悸迷雾怪物词缀的生成权重是多少」——`monster_mod` 靠「怪物词缀」
    「惊悸迷雾」命中且权重正落在它的未采集维度，而 `mod_affix`（装备词缀，已全量入库）
    靠泛化的「词缀」「权重」命中并被记为 covered，于是整句判为有据，
    模型拿到的是**装备**词缀的权重（评测里 gap_monster_mod_weight 因此漏出）。

    判据只取「子串」这一档：covered 的某个命中词是 uncovered 的某个命中词的**真子串**
    （「词缀」⊂「怪物词缀」）。不做同义词或语义相似度——那会把复合问题也误判成同一主语，
    而复合问题必须保留部分作答的能力（需求里的拒答粒度是断言不是整轮）。
    """
    if not verdict.uncovered or not verdict.covered:
        return
    specific = [h for s in verdict.uncovered for h in matched_hints.get(s.key, ())]
    kept: list[DomainStatus] = []
    for status in verdict.covered:
        hits = matched_hints.get(status.key, ())
        subsumed = any(
            hit != longer and hit in longer for hit in hits for longer in specific
        )
        if not subsumed:
            kept.append(status)
    verdict.covered = kept


def render_manifest(conn: sqlite3.Connection, *, for_prompt: bool = True) -> str:
    """渲染覆盖声明。

    `for_prompt=True` 时是注入 system prompt 的紧凑版——相当于文档里
    「overview + index 始终注入」那一层：用最少的 token 让模型知道
    这个库有什么、该走哪个工具、哪些根本没有。
    """
    rows = load(conn)
    usable = [s for s in rows if s.usable]
    missing = [s for s in rows if not s.usable]

    out: list[str] = ["## 知识库覆盖范围", ""]
    out.append("**已采集**（可作为依据）：")
    for s in sorted(usable, key=lambda x: -x.item_count):
        line = f"- {s.one_line()}"
        if s.spec.tools:
            line += f"　精确查询工具：{'、'.join(s.spec.tools)}"
        out.append(line)

    out.append("")
    out.append("**未采集**（问到这些必须明确说未采集并指向权威来源，不得推断）：")
    for s in sorted(missing, key=lambda x: x.spec.category.value):
        out.append(f"- {s.spec.display_zh}　{s.spec.authority_url}")

    conf = [n for s in rows for n in s.confidence_notes]
    if conf:
        out.append("")
        out.append("**已知的置信度限制**：")
        out.extend(f"- {n}" for n in conf)

    if for_prompt:
        return "\n".join(out)

    out.append("")
    out.append("**问题路由**：")
    for s in usable:
        for q in s.spec.question_hints:
            out.append(f"- {q} → {s.spec.display_zh}"
                       + (f"（{'、'.join(s.spec.tools)}）" if s.spec.tools else ""))
    return "\n".join(out)
