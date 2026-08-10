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
    note: str = ""


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
    ),
    DomainSpec(
        key="mod_affix",
        display_zh="装备词缀（T 级 / 物品等级 / 生成权重）",
        category=Category.ITEM,
        authority_url=f"{POEDB_BASE}/Modifiers",
        count_sql="SELECT count(*) FROM mod_affix",
        channel_sql="SELECT DISTINCT category_label FROM mod_affix WHERE category_label<>''",
        chunk_prefix="mod_ladder:",
        tools=("mod_tiers", "mod_sources"),
        hints=("词缀", "前缀", "后缀", "T1", "T级", "词缀组", "互斥", "物品等级", "权重"),
        question_hints=(
            "某底材类别上某条词缀有几档、各档物品等级与权重",
            "一条词缀能从哪些来源获得（掉落池 / 精华 / 工艺台 / 影响力 …）",
            "两条词缀是否互斥",
        ),
        poedb_pages=("Modifiers",),
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
        pob_files=("Data/ModExplicit.lua", "Data/ModMaster.lua", "…共 26 个"),
        note="P2 来源，与 POEDB 交叉校验用；冲突落 source_conflict",
    ),
    DomainSpec(
        key="base_item",
        display_zh="底材（基础物品）",
        category=Category.ITEM,
        authority_url=f"{POEDB_BASE}/Items",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='base_item'",
        hints=("底材", "基础物品", "白装底子"),
        question_hints=("某底材的基础属性、需求、隐式词缀",),
        poedb_pages=("Items",),
    ),
    # ── 游戏基础 ──
    DomainSpec(
        key="game_constant",
        display_zh="游戏常量（抗性上限 / 减伤上限 / 怪物等级表）",
        category=Category.BASICS,
        authority_url="https://github.com/PathOfBuildingCommunity/PathOfBuilding",
        count_sql="SELECT count(*) FROM game_constant",
        channel_sql="SELECT DISTINCT group_name FROM game_constant",
        tools=(),
        hints=("抗性上限", "减伤上限", "格挡上限", "怪物生命", "常量"),
        question_hints=("抗性 / 物理减伤 / 格挡的上限是多少", "某等级怪物的基础生命"),
        pob_files=("Data/Misc.lua",),
        note="不含前后缀数量上限与影响力规则——Misc.lua 里没有，需另找来源",
    ),
    DomainSpec(
        key="passive_tree",
        display_zh="天赋树节点",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/passive-skill-tree",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='passive'",
        hints=("天赋", "天赋树", "天赋点", "被动", "点数"),
        question_hints=("某个天赋节点给什么属性", "某属性能从哪些天赋获得"),
        poedb_pages=("passive-skill-tree",),
        pob_files=("TreeData/{ver}/tree.lua",),
    ),
    DomainSpec(
        key="cluster_jewel",
        display_zh="星团珠宝天赋（可选核心与大点）",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Cobalt_Jewel",
        count_sql=None,
        hints=("星团", "星团珠宝", "大点", "小点"),
        question_hints=("某个星团珠宝能开出哪些天赋", "某天赋出现在哪种星团珠宝上"),
        pob_files=("Data/ClusterJewels.lua",),
        note=(
            "易误判：ModJewelCluster.lua 已入库，但那是星团珠宝**自身的词缀**，"
            "不是它能开出的天赋点定义（ClusterJewels.lua 未接）"
        ),
    ),
    DomainSpec(
        key="skill_gem",
        display_zh="技能石",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Skill_Gems",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='skill'",
        hints=("技能石", "主动技能", "技能宝石", "宝石等级"),
        question_hints=("某技能的数值与标签", "某效果由哪些技能提供"),
        poedb_pages=("Skill_Gems", "Transfigured_Gems", "Gem"),
        pob_files=("Data/Gems.lua", "Data/Skills/"),
    ),
    DomainSpec(
        key="support_gem",
        display_zh="辅助宝石",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Support_Gems",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='support_gem'",
        hints=("辅助", "辅助宝石", "辅助石"),
        question_hints=("某辅助宝石的加成与代价", "哪些辅助能提升某类伤害"),
        poedb_pages=("Support_Gems", "Exceptional"),
        pob_files=("Data/Gems.lua",),
    ),
    DomainSpec(
        key="ascendancy",
        display_zh="升华职业与升华节点",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Ascendancy_class",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='ascendancy'",
        hints=("升华", "升华节点", "职业", "守望者", "刺客"),
        question_hints=("某升华节点的效果", "某效果由哪个升华提供"),
        poedb_pages=("Ascendancy", "Ascendancy_class", "Bloodline_Ascendancy_class"),
    ),
    DomainSpec(
        key="pantheon",
        display_zh="万神殿",
        category=Category.BASICS,
        authority_url=f"{POEDB_BASE}/Pantheon",
        count_sql=None,
        hints=("万神殿", "神殿加成"),
        question_hints=("某万神殿加成的效果",),
        poedb_pages=("Pantheon",),
        pob_files=("Data/Pantheons.lua",),
    ),
    # ── 制作系统 ──
    DomainSpec(
        key="essence",
        display_zh="精华",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Essence",
        count_sql="SELECT count(*) FROM mod_affix WHERE category='essence'",
        tools=("mod_sources",),
        hints=("精华", "精华词缀"),
        question_hints=("某精华赋予什么词缀", "某词缀由哪个精华赋予"),
        poedb_pages=("Essence",),
        partial_note="精华**赋予的词缀**已入库；精华物品本身的等级、掉落与兑换成本未采集",
    ),
    DomainSpec(
        key="fossil",
        display_zh="化石与共鸣器",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Modifiers",
        count_sql="SELECT count(*) FROM mod_affix WHERE category='delve'",
        tools=("mod_sources",),
        hints=("化石", "共鸣器", "地心探索"),
        question_hints=("化石能出什么词缀",),
        partial_note="化石**词缀池**已入库；化石对权重的乘数与共鸣器组合规则未采集",
    ),
    DomainSpec(
        key="crafting_bench",
        display_zh="工艺工作台（选项与费用）",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Crafting_Bench",
        count_sql="SELECT count(*) FROM mod_affix WHERE category='master'",
        tools=("mod_sources",),
        hints=("工艺台", "工艺工作台", "改造台", "手工"),
        question_hints=("工艺台能做什么词缀",),
        poedb_pages=("Crafting_Bench",),
        partial_note="工艺台**词缀**已入库；**每项的通货费用未采集**，因此制作成本无法计算",
    ),
    DomainSpec(
        key="harvest",
        display_zh="收获（古灵庄园）工艺",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Horticrafting",
        count_sql=None,
        hints=("收获", "园艺", "古灵庄园", "生命力"),
        question_hints=("收获有哪些定向工艺选项",),
        poedb_pages=("Horticrafting", "Harvest"),
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
        tools=("mod_sources",),
        hints=("辛迪加", "隐匿", "隐匿词缀", "情报网", "尤金"),
        question_hints=("隐匿词缀有哪些", "某条隐匿词缀怎么解锁"),
        poedb_pages=("Betrayal",),
        partial_note="隐匿**词缀**已入库；情报网的成员、部门与解锁流程未采集",
    ),
    DomainSpec(
        key="necropolis",
        display_zh="亡者之城（纠缠词缀）",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/Modifiers",
        count_sql="SELECT count(*) FROM mod_affix WHERE category='haunted'",
        tools=("mod_sources",),
        hints=("纠缠", "纠缠词缀", "亡者之城", "墓地", "尸体"),
        question_hints=("纠缠词缀有哪些", "某条纠缠词缀在哪个部位可用"),
        partial_note="纠缠**词缀**已入库；尸体种类、墓地布局与调度规则未采集",
        note="易与辛迪加的「隐匿」词缀混淆，两者是不同机制",
    ),
    DomainSpec(
        key="craft_rule",
        display_zh="制作规则（前后缀名额 / 影响力 / 元词缀）",
        category=Category.CRAFTING,
        authority_url=f"{POEDB_BASE}/metamods",
        count_sql=None,
        hints=("几个前缀", "几个后缀", "名额", "影响力", "元词缀", "觉醒者之石"),
        question_hints=(
            "稀有装备最多几前缀几后缀",
            "这套词缀组合是否可能同时存在",
            "影响力词缀需要什么底材",
        ),
        poedb_pages=("metamods",),
        note="制作可行性判定的前置。game_constant 里没有这类规则，需另找权威来源",
    ),
    # ── 地图系统 ──
    DomainSpec(
        key="map",
        display_zh="地图与地图词缀",
        category=Category.ATLAS,
        authority_url=f"{POEDB_BASE}/Maps",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='map'",
        hints=("地图", "图鉴", "地图词缀"),
        question_hints=("某地图的布局与 Boss", "地图词缀有哪些"),
        poedb_pages=("Maps", "Atlas", "Atlas_of_Worlds"),
        note="PoB 的 ModMap.lua 已明确排除（手写、无权重无区间），地图词缀须走 POEDB",
    ),
    DomainSpec(
        key="atlas_passive",
        display_zh="Atlas 天赋",
        category=Category.ATLAS,
        authority_url=f"{POEDB_BASE}/atlas-skill-tree",
        count_sql=None,
        hints=("异界天赋", "atlas天赋", "图鉴天赋"),
        question_hints=("某异界天赋的效果",),
        poedb_pages=("atlas-skill-tree",),
    ),
    DomainSpec(
        key="boss",
        display_zh="Boss 与怪物",
        category=Category.ATLAS,
        authority_url=f"{POEDB_BASE}/Monsters",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='boss'",
        hints=("boss", "首领", "怪物", "机制", "打法"),
        question_hints=("某 Boss 的技能与应对",),
        poedb_pages=("Monsters",),
    ),
    # ── 经济系统 ──
    DomainSpec(
        key="currency",
        display_zh="通货",
        category=Category.ECONOMY,
        authority_url=f"{POEDB_BASE}/Currency",
        count_sql="SELECT count(*) FROM entity WHERE entity_type='currency'",
        hints=("通货", "混沌石", "崇高石", "神圣石", "改造石"),
        question_hints=("某通货的作用",),
        poedb_pages=("Currency", "Scarab"),
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


def probe(
    conn: sqlite3.Connection, *, cache_dir: Path | None = None
) -> list[DomainStatus]:
    """探测全部领域的实际状态。

    判定顺序：有数据 → ingested / partial；没数据但页面在本地缓存里 →
    harvested；否则 not_collected。
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
