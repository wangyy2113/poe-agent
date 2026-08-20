"""单文件 SQLite 存储：实体库 + FTS5 索引 + 向量 + 会话 + 轨迹 + 缺口台账。

全部状态集中在一个文件，备份即复制该文件。

跨平台注意：
  - FTS5 是编译进 SQLite 的，所有平台可用 → BM25 是唯一确定可用的检索通道。
  - sqlite-vec 是可加载扩展，依赖 Connection.enable_load_extension，
    而该方法在部分 Python 发行版被编译关闭（如 macOS 自带 3.9 带 OMIT_LOAD_EXTENSION），
    且 sqlite-vec 只有 win_amd64 没有 win_arm64。
    故做三级探测，任一失败即禁用向量通道并记录原因，绝不让启动失败。
  - data/ 必须在本地磁盘：SQLite WAL 在网络共享盘上锁不可靠。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_vector_status: VectorStatus | None = None


@dataclass(frozen=True)
class VectorStatus:
    """向量通道可用性探测结果。"""

    available: bool
    reason: str

    def __str__(self) -> str:
        return f"{'可用' if self.available else '不可用'}（{self.reason}）"


SCHEMA = """
-- ── Tier A：结构化实体库（精确层）──
CREATE TABLE IF NOT EXISTS entity (
    entity_id       TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    name_zh         TEXT NOT NULL,
    name_en         TEXT,              -- 权威英文名；未取到则为 NULL，不得由 slug 反推
    slug            TEXT,              -- URL 标识符
    base_type       TEXT,              -- 底材
    item_class      TEXT,
    attributes      TEXT NOT NULL,     -- JSON
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL,     -- 缺 url 的文档不得进入可引用索引
    league          TEXT,
    version         TEXT,
    content_hash    TEXT NOT NULL,     -- 增量更新判断依据
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_type ON entity(entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_name_zh ON entity(name_zh);

-- 别名 → 实体。别名是检索匹配辅助，不是事实断言，
-- 因此允许收录 slug 去下划线等派生形式。
CREATE TABLE IF NOT EXISTS alias (
    alias      TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    kind       TEXT NOT NULL,          -- name_zh | name_en | slug | manual
    PRIMARY KEY (alias_norm, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_alias_norm ON alias(alias_norm);
CREATE INDEX IF NOT EXISTS idx_alias_entity ON alias(entity_id);

-- ── 词缀库（装备分析的地基）──
-- 同一条词缀在不同底材类别上的 ilvl/权重/T 级可能不同，因此按
-- 「实质字段全集」的哈希（uid）作主键，不做跨类别去重。
-- 这样才能正确回答「这条词缀在弓上是 T5，在法杖上是 T4」。
CREATE TABLE IF NOT EXISTS mod_affix (
    uid             TEXT PRIMARY KEY,
    mod_code        TEXT NOT NULL,     -- basename(hover)，词缀稳定标识符
    item_class_page TEXT NOT NULL,     -- POEDB 页面 slug，如 Bows
    item_class_code TEXT,              -- ItemClassesCode，如 Bow
    item_class_zh   TEXT,              -- 中文类别名，如 弓
    category        TEXT NOT NULL,     -- normal/essence/master/delve/synthesis/...
    category_label  TEXT,              -- 该来源组中文标题
    name_zh         TEXT,              -- 词缀名，如「焦化的」
    gen_type        INTEGER,           -- 1=前缀 2=后缀 0=其他
    gen_label       TEXT,
    family_list     TEXT,              -- JSON 数组；**互斥判断用整个列表求交集**
    correct_group   TEXT,              -- family_list[0]；**T 级分组只用它**
    ilvl            INTEGER,           -- 该词缀要求的物品等级
    tier            INTEGER,
    tier_count      INTEGER,           -- 该组总级数，用于展示 T3/10
    tier_source     TEXT,              -- poedb_payload=数据自带 / derived=按算法推导
    weight          INTEGER,           -- 生成权重 DropChance
    weight_pct      REAL,              -- 组内占比
    text_zh         TEXT,              -- 词缀文本，数值原样保留
    text_template   TEXT,              -- 严格模板：只占位区间，固定数字保留（校验用）
    loose_template  TEXT,              -- 宽松模板：全部数字占位（召回用）
    ranges          TEXT,              -- JSON [[low,high],...]，逐位对应严格模板的 #
    tags            TEXT,              -- JSON 标签（属性/生命/抗性…）
    spawn_tags      TEXT,              -- JSON 可生成底材标签
    fossil_tags     TEXT,              -- JSON 化石标签
    variant_id      TEXT,              -- 来源实例（如精华的 BaseItemTypesID）
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
-- 装备分析的主查询路径：用宽松模板召回，再用严格模板 + 区间校验
CREATE INDEX IF NOT EXISTS idx_mod_loose ON mod_affix(loose_template, item_class_page);
CREATE INDEX IF NOT EXISTS idx_mod_template ON mod_affix(text_template, item_class_page);
CREATE INDEX IF NOT EXISTS idx_mod_class ON mod_affix(item_class_page, category);
CREATE INDEX IF NOT EXISTS idx_mod_group ON mod_affix(correct_group, gen_type);
CREATE INDEX IF NOT EXISTS idx_mod_code ON mod_affix(mod_code);

-- ── PoB 词缀（P2 来源，英文）──
-- 为什么单独建表而不是塞进 mod_affix：
--   1. 两个源的粒度不同。mod_affix 按「词缀 × 底材类别」一行（同一词缀在弓上
--      T5、法杖上 T4 是两行）；PoB 一条词缀一行，底材差异体现在 weights 向量里。
--      硬合并会逼着二者之一失真。
--   2. 交叉校验要求两边**各自独立保存**。混进同一张表就只能靠 source 列区分，
--      而 UPSERT 一旦按 uid 撞上就会互相覆盖，冲突还没被发现就消失了。
-- 对账结果单独落 source_conflict，驱动需求 15.5 的跨来源对照。
CREATE TABLE IF NOT EXISTS pob_mod (
    uid             TEXT PRIMARY KEY,  -- {mod_kind}:{mod_id}
    mod_id          TEXT NOT NULL,     -- PoB 键名，如 IncreasedLife7
    mod_kind        TEXT NOT NULL,     -- explicit/corrupted/eldritch/master/...
    source_file     TEXT NOT NULL,     -- Data/ModExplicit.lua
    affix_type      TEXT,              -- Prefix / Suffix / 空
    affix_name      TEXT,              -- "Hale"、"of the Brute"
    mod_group       TEXT,              -- 与 mod_affix.correct_group 同为 GGG 内部 ID
    ilvl            INTEGER,           -- PoB 的 level：该档位要求的物品等级
    text_en         TEXT NOT NULL,     -- 词缀英文原文，数值原样保留
    text_template   TEXT,              -- 严格模板（与 mod_affix 同一套归一化规则）
    loose_template  TEXT,
    ranges          TEXT,              -- JSON [[low,high],...]
    weights         TEXT,              -- JSON {底材标签: 权重}；权重 0 是显式禁止
    weight_mults    TEXT,              -- JSON 权重乘数（化石等条件性加成）
    spawn_tags      TEXT,              -- JSON 权重>0 的底材标签
    tags            TEXT,              -- JSON modTags：分类标签，给检索与展示
    behaviour_tags  TEXT,              -- JSON tags：机制标记(has_attack_mod)，
                                       -- 决定权重乘数条件；与分类标签语义不同不可合并
    item_types      TEXT,              -- JSON types：适用物品类型（工艺台词缀专有）
    variant_id      TEXT,              -- 来源实例/档位（头盔附魔的实验室难度等）
    trade_hashes    TEXT,              -- JSON 交易站 stat id，打通 Trade API
    stat_order      TEXT,              -- JSON
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL,     -- 钉住 commit 的 GitHub blob 地址
    pob_version     TEXT,              -- 如 "PoB 2.66.2 / tree 3.29"
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pobmod_group ON pob_mod(mod_group, ilvl);
CREATE INDEX IF NOT EXISTS idx_pobmod_kind ON pob_mod(mod_kind);
CREATE INDEX IF NOT EXISTS idx_pobmod_loose ON pob_mod(loose_template);

-- ── PoB 胸甲底材注册表（P2 结构 + POEDB P1 中英文名称桥接）──
-- 不写入 entity：本表的 name_en/tags 来自 PoB，name_zh/slug 来自 POEDB，
-- 两侧必须一一对应后才入库。它只负责把具体胸甲底材唯一映射到词缀页；
-- 不承载稀有度、词缀容量、影响力适用性、制作状态转移或可获得性结论。
CREATE TABLE IF NOT EXISTS pob_base_item (
    base_id         TEXT PRIMARY KEY,
    name_en         TEXT NOT NULL CHECK(name_en <> ''),
    name_zh         TEXT NOT NULL CHECK(name_zh <> ''),
    poedb_slug      TEXT NOT NULL CHECK(poedb_slug <> ''),
    -- 类别键与 POEDB 页面：`kb.sources.pob_bases.CATEGORIES` 的 key 与页面 slug。
    -- 有了它们才能回答「这条底材属于哪个装备部位、词缀查哪一页」。
    category_key    TEXT NOT NULL CHECK(category_key <> ''),
    poedb_page      TEXT NOT NULL CHECK(poedb_page <> ''),
    item_class      TEXT NOT NULL CHECK(item_class <> ''),
    -- 腰带、戒指、箭袋等 PoB 不给 subType；首饰与药剂没有插槽。空串 / 0 是真实形态，
    -- 原先的 `subtype <> ''` 与 `socket_limit > 0` 是照胸甲写的，泛化时必须放开。
    subtype         TEXT NOT NULL,
    socket_limit    INTEGER NOT NULL CHECK(socket_limit >= 0),
    tags            TEXT NOT NULL CHECK(json_valid(tags) AND json_array_length(tags) > 0),
    influence_tags  TEXT NOT NULL CHECK(json_valid(influence_tags)),
    -- 只有护甲类有「按属性分的词缀页」。非护甲类两列都是空串，
    -- 非空时仍必须落在原枚举里——`craft/pool.py` 的条件权重模型依赖这个约束。
    armour_tag      TEXT NOT NULL CHECK(armour_tag = '' OR armour_tag IN (
        'str_armour', 'dex_armour', 'int_armour', 'str_dex_armour',
        'str_int_armour', 'dex_int_armour', 'str_dex_int_armour'
    )),
    modifier_page   TEXT NOT NULL CHECK(modifier_page = '' OR modifier_page IN (
        'Body_Armours_str', 'Body_Armours_dex', 'Body_Armours_int',
        'Body_Armours_str_dex', 'Body_Armours_str_int', 'Body_Armours_dex_int',
        'Body_Armours_str_dex_int'
    )),
    implicit_text   TEXT,
    implicit_mod_types TEXT NOT NULL CHECK(json_valid(implicit_mod_types)),
    implicit_ids    TEXT NOT NULL CHECK(json_valid(implicit_ids)),
    -- 数值表 + 它取自 PoB 的哪个键。原名 `defences` 只对护甲成立，
    -- 拿它装武器伤害会让列名说谎。
    stats           TEXT NOT NULL CHECK(json_valid(stats)),
    stats_kind      TEXT NOT NULL CHECK(stats_kind IN ('armour','weapon','flask','none')),
    requirements    TEXT NOT NULL CHECK(json_valid(requirements)),
    source_file     TEXT NOT NULL CHECK(source_file LIKE 'Data/Bases/%.lua'),
    source          TEXT NOT NULL CHECK(source = 'Path of Building'),
    source_priority INTEGER NOT NULL CHECK(source_priority = 2),
    pob_url         TEXT NOT NULL CHECK(pob_url <> ''),
    poedb_url       TEXT NOT NULL CHECK(poedb_url <> ''),
    source_commit   TEXT NOT NULL CHECK(length(source_commit) = 40),
    pob_version     TEXT,
    game_version    TEXT,
    content_hash    TEXT NOT NULL CHECK(content_hash <> ''),
    updated_at      TEXT NOT NULL,
    UNIQUE(name_en),
    UNIQUE(poedb_slug)
    -- **没有 UNIQUE(name_zh)**：POEDB 会把两个不同底材译成同一个中文名，实测 4 例
    -- （`Silk Gloves` 与 `Fingerless Silk Gloves` 都叫「丝绸手套」，
    -- `Convening Wand` 与 `Convoking Wand` 都叫「召集法杖」）。
    -- 原先有这条约束是因为表里只有胸甲，那个范围内它恰好成立。
    -- 中文名因此**不能当唯一键用**：按中文名查底材必须处理多命中，
    -- `craft/pool.py._resolve_base` 已经是命中多条即拒绝，那是正确行为。
);
CREATE INDEX IF NOT EXISTS idx_pobbase_armour_tag ON pob_base_item(armour_tag);
CREATE INDEX IF NOT EXISTS idx_pobbase_name_zh ON pob_base_item(name_zh);
CREATE INDEX IF NOT EXISTS idx_pobbase_category ON pob_base_item(category_key);
CREATE INDEX IF NOT EXISTS idx_pobbase_modifier_page ON pob_base_item(modifier_page);

-- ── 工艺工作台目录（选项 / 原始费用 / 适用类别 / 解锁信息）──
-- 这里只保存 POEDB 页面明确列出的目录事实，不承载「该动作对当前装备可执行」的
-- 状态转移语义。后者必须等独立 craft_rule 有可靠来源后再开放。
CREATE TABLE IF NOT EXISTS craft_bench_recipe (
    recipe_id       TEXT PRIMARY KEY,  -- 完整记录 canonical JSON 的 SHA-256
    effect_text     TEXT NOT NULL CHECK(effect_text <> ''),
    item_classes    TEXT NOT NULL CHECK(
        json_valid(item_classes) AND json_array_length(item_classes) > 0
    ),                              -- JSON 非空数组，保持页面原始 ItemClasses 名称
    unlock_text     TEXT NOT NULL CHECK(unlock_text <> ''),
    unlock_url      TEXT,           -- Default / 等级条件在页面上没有链接，合法为空
    unlock_hint     TEXT,           -- tooltip，如「生命和魔力 - 等级 2」
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''), -- 工艺台主表来源页，始终必填
    content_hash    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bench_effect ON craft_bench_recipe(effect_text);

-- 一条工艺可能要求多种通货（真实页面 783 行中有 15 行是双费用），不能把费用
-- 压成 recipe 上的单个 currency 字段，也不能塞进 mod_affix。
CREATE TABLE IF NOT EXISTS craft_bench_recipe_cost (
    recipe_id       TEXT NOT NULL,
    position        INTEGER NOT NULL CHECK(position >= 0),
    amount          INTEGER NOT NULL CHECK(amount > 0),
    currency_slug   TEXT NOT NULL CHECK(currency_slug <> ''),
    currency_name_zh TEXT NOT NULL CHECK(currency_name_zh <> ''),
    currency_url    TEXT NOT NULL CHECK(currency_url <> ''),
    PRIMARY KEY (recipe_id, position),
    FOREIGN KEY (recipe_id) REFERENCES craft_bench_recipe(recipe_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_bench_cost_currency
    ON craft_bench_recipe_cost(currency_slug);

-- ── 元词缀交互矩阵（Craft of Exile P4，经 POEDB 镜像采集）──
-- 一行只表示来源页面中一个 operation × guard 单元格的明确值。源页面空白格不入表，
-- 也绝不推断为 N/A；完整装备状态转移、可执行性、步骤、概率和成本均不在此表范围内。
CREATE TABLE IF NOT EXISTS craft_metamod_interaction (
    interaction_id TEXT PRIMARY KEY,
    section         TEXT NOT NULL CHECK(section <> ''),
    operation_text  TEXT NOT NULL CHECK(operation_text <> ''),
    guard_text      TEXT NOT NULL CHECK(guard_text <> ''),
    outcome         TEXT NOT NULL CHECK(
        outcome IN ('yes', 'no', 'not_applicable', 'conditional')
    ),
    raw_value       TEXT NOT NULL CHECK(
        raw_value IN ('YES', 'NO', 'N/A', 'YES & NO')
    ),
    note_text       TEXT,
    row_order       INTEGER NOT NULL CHECK(row_order >= 0),
    column_order    INTEGER NOT NULL CHECK(column_order BETWEEN 0 AND 3),
    source          TEXT NOT NULL CHECK(source = 'Craft of Exile'),
    source_priority INTEGER NOT NULL CHECK(source_priority = 4),
    source_url      TEXT NOT NULL CHECK(
        source_url = 'https://www.craftofexile.com/metamods'
    ),
    mirror_url      TEXT NOT NULL CHECK(
        mirror_url = 'https://poedb.tw/cn/metamods'
    ),
    content_hash    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    version_scope   TEXT NOT NULL CHECK(version_scope = 'unknown'),
    updated_at      TEXT NOT NULL,
    CHECK(
        (raw_value = 'YES' AND outcome = 'yes') OR
        (raw_value = 'NO' AND outcome = 'no') OR
        (raw_value = 'N/A' AND outcome = 'not_applicable') OR
        (raw_value = 'YES & NO' AND outcome = 'conditional')
    ),
    CHECK(outcome <> 'conditional' OR (note_text IS NOT NULL AND note_text <> '')),
    UNIQUE(operation_text, guard_text)
);
CREATE INDEX IF NOT EXISTS idx_metamod_operation_guard
    ON craft_metamod_interaction(operation_text, guard_text);

-- ── 游戏常量（calc/ 的真源）──
-- 标量逐个成行，这样每个常量都能被单独引用（需求 12：常量附来源与版本）。
-- dat_source 记 GGG 原始表名（GameConstants.dat / Character.ot），
-- 比「来自 PoB」精确一级——出问题时能直接定位到上游哪张表。
-- 注意玩家侧上限（抗性 75%、物理减伤 90%、格挡 75%）在 characterConstants，
-- 不在 gameConstants。
CREATE TABLE IF NOT EXISTS game_constant (
    key             TEXT PRIMARY KEY,  -- characterConstants.base_maximum_all_resistances_%
    group_name      TEXT NOT NULL,     -- characterConstants
    dat_source      TEXT,              -- Metadata/Characters/Character.ot
    -- 0 = 出处注释紧贴该赋值（确定）；1 = 从上文延续而来（推断，需人工确认）。
    -- 区分是因为「错的出处比没有出处更糟」：它会让错误的溯源看起来可信。
    dat_source_inherited INTEGER DEFAULT 0,
    kind            TEXT NOT NULL,     -- scalar / level_table / array / other
    value_num       REAL,              -- 标量值
    value_json      TEXT,              -- 数组或嵌套值
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL,     -- 钉住 commit 的 blob 地址（带行号锚点）
    pob_version     TEXT,
    game_version    TEXT,              -- 如 3.29
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_const_group ON game_constant(group_name);

-- ── PoB 唯一装备（P2，英文；用于与 POEDB 交叉校验）──
-- variant_scope 是关键：PoB 的文本块里**历史版本与当前版本混在一起**
-- （1293 件里 752 件带 Variant 标记）。冈姆的壮志同时写着 +500（3.25 前）
-- 与 +1000（当前），不区分就会把过期数值当现值输出。
-- 只有 variant_scope='current' 的行可用于回答；'obsolete' 仅供版本 diff 分析。
CREATE TABLE IF NOT EXISTS pob_unique (
    uid             TEXT PRIMARY KEY,  -- unique:{name}
    name_en         TEXT NOT NULL,
    base_type_en    TEXT,
    source_file     TEXT NOT NULL,     -- Data/Uniques/amulet.lua
    league          TEXT,              -- 首发赛季
    drop_source     TEXT,              -- Source: 行（掉落来源）
    level_req       INTEGER,
    implicit_count  INTEGER,
    limited_to      INTEGER,           -- 珠宝的携带上限
    radius          TEXT,
    variant_names   TEXT,              -- JSON 全部 variant 名
    current_variants TEXT,             -- JSON 保留的 variant 下标（1-based）
    obsolete_variants TEXT,            -- JSON 被排除的历史版本下标
    mods            TEXT NOT NULL,     -- JSON [{kind,text,ranges,variants,tags,...}]
    ranges_fp       TEXT,              -- JSON 区间指纹序列，跨语言比对用
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL,
    pob_version     TEXT,
    game_version    TEXT,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pobuniq_name ON pob_unique(name_en);
CREATE INDEX IF NOT EXISTS idx_pobuniq_base ON pob_unique(base_type_en);

-- ── PoB 技能石与辅助宝石（P2 出结构，POEDB 出中文名）──
-- 桥接键是 PoB 的 `grantedEffectId`，它同时出现在 POEDB 清单页的
-- `data-hover="?s=Data\\GemEffects/<id>"` 里，两侧只差大小写（实测 810/861 = 94.1%
-- 命中、零多解）。**不按名字配对**：中文名要从 POEDB 取，用名字配就成了循环依赖。
CREATE TABLE IF NOT EXISTS pob_skill_gem (
    gem_id          TEXT PRIMARY KEY,  -- grantedEffectId，保留 PoB 侧原大小写
    name_en         TEXT NOT NULL CHECK(name_en <> ''),
    name_zh         TEXT NOT NULL CHECK(name_zh <> ''),
    -- 同一颗宝石的其它译名（JSON 数组）。POEDB 的 /cn 页对 7 颗宝石同时给出国服与
    -- 繁中译名（魔卫复苏 / 僵尸复苏、召唤魔侍 / 召唤骷髅），两个都是真名字：
    -- 正名取页面文档顺序第一个，其余进这里并一起入别名表，两种问法都能命中。
    name_zh_alt     TEXT NOT NULL CHECK(json_valid(name_zh_alt)),
    poedb_slug      TEXT NOT NULL CHECK(poedb_slug <> ''),
    gem_kind        TEXT NOT NULL CHECK(gem_kind IN ('active','support')),
    -- 宝石颜色即属性归属。POEDB 用 gem_red/green/blue 三个 class 表达，
    -- PoB 用 color=1/2/3；两侧都可能给出白色（无属性需求）的第四态。
    attr_color      TEXT NOT NULL CHECK(attr_color IN ('str','dex','int','none')),
    tags            TEXT NOT NULL CHECK(json_valid(tags)),
    tag_string      TEXT NOT NULL,     -- PoB 的英文标签串（Spell, AoE, Cold）
    -- POEDB 的中文标签（法术 / 效果区域 / 冰霜）。单列一份而不是靠英文翻：
    -- 中文问法「有哪些冰霜法术」只能靠这一份命中，而翻译表一旦手写就会过期。
    tags_zh         TEXT NOT NULL CHECK(json_valid(tags_zh)),
    -- **没有属性需求列（力/敏/智）**：Gems.lua 的 reqStr/reqDex/reqInt 不是显示值，
    -- 而是换算系数。PoB 算的是
    --   round((20 + (等级需求 - 3) × 3) × (系数 / 100) ^ 0.9 × statType)
    -- （主动 0.7 / 辅助 0.5，小于 14 归零，见 CalcTools.getGemStatRequirement）。
    -- 把系数当需求写出去就是个带引用的错数字，所以这一维明确划进未采集
    -- （attr_color 是字面的，「这是红宝石还是蓝宝石」照样答得出）。
    -- 自然最大等级：20（少数辅助是 3–6）。腐化与加等级能超过它，
    -- 所以它是「正常获取的上限」而不是 pob_skill_level 里的最大 gem_level。
    natural_max_level INTEGER NOT NULL CHECK(natural_max_level > 0),
    cast_time       REAL,              -- 辅助宝石没有施放时间，为 NULL
    skill_types     TEXT NOT NULL CHECK(json_valid(skill_types)),
    is_vaal         INTEGER NOT NULL CHECK(is_vaal IN (0,1)),
    is_transfigured INTEGER NOT NULL CHECK(is_transfigured IN (0,1)),
    description_en  TEXT,              -- PoB 的英文一句话说明，缓存页里没有中文版
    source_file     TEXT NOT NULL CHECK(source_file LIKE 'Data/%'),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    pob_url         TEXT NOT NULL CHECK(pob_url <> ''),
    poedb_url       TEXT NOT NULL CHECK(poedb_url <> ''),
    source_commit   TEXT NOT NULL CHECK(length(source_commit) = 40),
    pob_version     TEXT,
    game_version    TEXT,
    content_hash    TEXT NOT NULL CHECK(content_hash <> ''),
    updated_at      TEXT NOT NULL,
    UNIQUE(name_en),
    UNIQUE(poedb_slug)
    -- **没有 UNIQUE(name_zh)**：同一个中文名对应多颗宝石是真实形态
    -- （瓦尔版与常规版、蜕变宝石与本体在个别条目上重名）。
    -- 这条约束在 pob_base_item 上就是错的，别在这里重犯。
);
CREATE INDEX IF NOT EXISTS idx_gem_zh ON pob_skill_gem(name_zh);
CREATE INDEX IF NOT EXISTS idx_gem_kind ON pob_skill_gem(gem_kind, attr_color);

-- ── 宝石每级数值：字面值 + **算出来的基础伤害** ──
-- 伤害那三列是第二十九轮才加的，而它等了很久：PoB 存的每级伤害是**乘数**
-- （冰霜新星 1 级存 0.8 / 1.2），要按 effectiveness 公式算出来才是游戏里显示的数字，
-- 而「算错就是带着引用的错数字」。所以先补对账口径再入库——
-- POEDB 技能页把区间写成「造成 (25 — 1122) - (38 — 1683) 冰霜伤害」，
-- 括号里正好是宝石 1 级 → 自然最高级，20 个技能逐个对账 20/20 一致
-- （`scripts/verify_gem_damage.py`，公式在 `calc/skill_damage.py`）。
-- 存**算好的显示值**而不是乘数：乘数放进来就会被下一个人当伤害读走。
-- 没有伤害数据的技能（光环、辅助、纯效果类）这三列留 NULL，那是「本来就没有」。
CREATE TABLE IF NOT EXISTS pob_skill_level (
    gem_id          TEXT NOT NULL REFERENCES pob_skill_gem(gem_id) ON DELETE CASCADE,
    gem_level       INTEGER NOT NULL CHECK(gem_level >= 1),
    level_requirement INTEGER,         -- 角色等级需求
    cost            TEXT NOT NULL CHECK(json_valid(cost)),  -- {"Mana":23} / {} 表示无消耗
    cooldown        REAL,
    stored_uses     INTEGER,
    crit_chance     REAL,
    mana_multiplier REAL,              -- 辅助宝石的魔力消耗乘数
    attack_speed_multiplier REAL,
    -- 保留：百分比与定值都是**字面值**，两列都收。
    -- 第二十四轮补上 `reservation_flat`：先前只有 `reservation_percent`，于是
    -- 清晰 / 精准 / 活力这类**定值保留**的技能在库里看起来「不保留任何魔力」——
    -- 实测 PoB 侧有 400 条 `manaReservationFlat`，漏收它等于把「保留 279 点魔力」
    -- 答成「不保留」。
    -- **没有生命保留列**：上游 `lifeReservationPercent` 只出现在 3 个**未桥接**的
    -- 技能上（BoneArmour / TentacleSmash / ThirstForBlood，POEDB 清单页没有它们），
    -- `sup_str.lua` 里那两处是辅助宝石的 mod 声明、不是每级数值。
    -- 加一列当前管线永远填不上的空列，正是本表注释开头警告的那种坑。
    reservation_percent REAL,          -- 保留最大魔力的百分比（光环大多数）
    reservation_flat REAL,             -- 保留定值魔力（清晰 279 / 活力 233 / 精准 186）
    -- 基础伤害：按 PoB 的 effectiveness 公式算出来的**显示值**（不是乘数）
    base_damage_min REAL,
    base_damage_max REAL,
    damage_type     TEXT,              -- cold / fire / lightning / chaos / physical
    -- 伤害有效度。PoB 存 2.3，游戏里显示「伤害效果 230%」，
    -- 换算只在渲染层做一次（工具与卡片共用同一个函数），DB 存原值。
    damage_effectiveness REAL,
    PRIMARY KEY(gem_id, gem_level)
);

-- ── 怪物词缀（POEDB `/cn/Monsters` 的「怪物」表）──
-- 只收**有中文文本**的行。实测 501 行里：190 行有中文（127 只有中文 + 63 中文与未翻译
-- 原始 stat id 并存），63 行只有未翻译的内部 stat id（`maven map boss buffed [50]`），
-- 248 行连效果文本都没有（页面只列了词缀名，如「法瑞尔的狂野之灵」）。
-- 后两类跳过并登记台账：把内部 stat id 当中文答案输出，等于拿调试字段冒充游戏文本。
CREATE TABLE IF NOT EXISTS poedb_monster_mod (
    mod_id          TEXT PRIMARY KEY,  -- hash(kind, name_zh, text_zh)
    -- 词缀来源分类，页面「类型」列原文：传奇 / 精华 / Talisman / 惊悸迷雾 / 猎魔 / 菌潮 …
    kind            TEXT NOT NULL CHECK(kind <> ''),
    monster_level   INTEGER,           -- 页面「等级」列，多为 1
    name_zh         TEXT NOT NULL CHECK(name_zh <> ''),
    text_zh         TEXT NOT NULL CHECK(text_zh <> ''),
    -- POEDB 未翻译的原始 stat id（`monster dropped item rarity +% [0]`）。
    -- 单列一份而不是拼进 text_zh：它是调试信息，**不可作为面向用户的文本**，
    -- 但留着能说明「这条词缀还有一部分效果没有中文」。
    raw_stats       TEXT NOT NULL CHECK(json_valid(raw_stats)),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    content_hash    TEXT NOT NULL CHECK(content_hash <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monstermod_kind ON poedb_monster_mod(kind);
CREATE INDEX IF NOT EXISTS idx_monstermod_name ON poedb_monster_mod(name_zh);

-- ── 幽魂可用怪物与倍率（同一页的最后一张表）──
-- 那张表的「幽魂」列 1948 行**全是勾**，所以它本身就是「可作幽魂的怪物」清单，
-- 这一列不携带信息（别把它存成布尔值，那会让人以为库里还有不可作幽魂的怪物）。
--
-- 刻意**不建 entity/卡片**，只有表 + `spectre_stats` 工具 + 意图词路由：
-- 1948 张「名字 + 两个倍率」的卡片彼此高度雷同，而怪物名里有大量通用词
-- （「恶魔」「守卫」），别名精确命中会把它们注入任何含这些词的问题的候选集——
-- 词缀梯度卡踩过同一个坑（粒度不对就靠排序层补偿，补偿层最后都被删了）。
CREATE TABLE IF NOT EXISTS poedb_spectre (
    -- POEDB 侧的稳定键：`data-hover` 给 `Data\\MonsterVarieties/<id>` 时取 id
    -- （2026-08-19 实测 1921 行），只给 tooltip 缓存哈希时取哈希 + 倍率（27 行；
    -- 哈希是 tooltip 内容的键、会撞，判据见 poedb_monsters._variety_key）。
    variety_key     TEXT PRIMARY KEY,
    name_zh         TEXT NOT NULL CHECK(name_zh <> ''),
    -- 英文 slug 来自链接。**不是权威英文名**（那要详情页），只用于对照与检索
    poedb_slug      TEXT NOT NULL CHECK(poedb_slug <> ''),
    damage_pct      INTEGER NOT NULL,  -- 页面「伤害%」列原值，100 = 基础
    life_pct        INTEGER NOT NULL,  -- 页面「生命%」列原值
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    content_hash    TEXT NOT NULL CHECK(content_hash <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spectre_name ON poedb_spectre(name_zh);
CREATE INDEX IF NOT EXISTS idx_spectre_life ON poedb_spectre(life_pct DESC);

-- ── PoB 幽魂建模数据（P2）──
-- `poedb_spectre`（P1）给的是**清单与倍率**：1948 个可作幽魂的怪物、伤害% / 生命%。
-- PoB `Data/Spectres.lua` 只建模了 **268** 个（玩家实际会用的那些），但多给了
-- 四抗性、护甲/闪避/能量护盾系数、攻击间隔与技能 id ——那几维 POEDB 那页没有。
--
-- 两侧**各自独立保存**（与 mod_affix / pob_mod 同一个理由）：粒度不同，
-- 且交叉校验要求两份原值都在。实测两侧的生命/伤害是同一个量的不同单位
-- （POEDB 120 ↔ PoB 1.2，逐条相等的占 91% / 94.8%），差异分两类落 source_conflict：
--   kind='spectre'         两侧都只有一条却对不上 → **真分歧**（实测 8 条）
--   kind='spectre_review'  POEDB 有多个同名种类而 PoB 只建模一个 → 粒度差异（43 条）
-- 混成一类就会把「谁记错了」和「谁记得更细」当成同一件事——词缀对账上踩过。
CREATE TABLE IF NOT EXISTS pob_spectre (
    metadata_id     TEXT PRIMARY KEY,  -- Metadata/Monsters/Axis/AxisCaster
    name_en         TEXT NOT NULL CHECK(name_en <> ''),
    -- 系数，1.0 = 该等级的基础值。PoB 用小数、POEDB 用百分数
    life_mult       REAL NOT NULL,
    damage_mult     REAL NOT NULL,
    -- 129 / 127 / 95 条才有护甲 / 闪避 / 能量护盾。NULL 是「PoB 没给」，
    -- **不要补 0**：那会把「没建模」说成「这个怪物没有护甲」
    armour_mult     REAL,
    evasion_mult    REAL,
    es_mult         REAL,
    -- 四抗性，百分数原值（0 / 75 ...）。这一维只有 PoB 有
    fire_resist     INTEGER NOT NULL,
    cold_resist     INTEGER NOT NULL,
    lightning_resist INTEGER NOT NULL,
    chaos_resist    INTEGER NOT NULL,
    attack_time     REAL,
    attack_range    REAL,
    accuracy        REAL,
    damage_spread   REAL,
    -- 怪物技能 id（`MonsterLightningThorns` 这类）。**只存不呈现**：
    -- 实测 1235 个 id 与本库 810 颗宝石的 grantedEffectId **零命中**——
    -- 它们是怪物内部技能，不是玩家宝石，没有中文名可给。
    skill_ids       TEXT NOT NULL CHECK(json_valid(skill_ids)),
    monster_tags    TEXT NOT NULL CHECK(json_valid(monster_tags)),
    source_file     TEXT NOT NULL CHECK(source_file LIKE 'Data/%'),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    pob_url         TEXT NOT NULL CHECK(pob_url <> ''),
    source_commit   TEXT NOT NULL CHECK(length(source_commit) = 40),
    pob_version     TEXT,
    game_version    TEXT,
    content_hash    TEXT NOT NULL CHECK(content_hash <> ''),
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pobspectre_name ON pob_spectre(name_en);
CREATE INDEX IF NOT EXISTS idx_pobspectre_life ON pob_spectre(life_mult DESC);

-- ── 天赋树的图结构（连接关系与职业起点）──
-- 节点**属性**早就入库了（`entity` 里 1631 条天赋卡），缺的一直是「怎么连的」。
-- 树 JSON 本来就在缓存里、也一直被解析，只是 `in` / `out` 与 `classes` 没被取出来——
-- 与元词缀矩阵、技能石同一个形态：数据在手上，缺的是抽取那一步。
--
-- 节点 id 就是 GGG 的 skill id（实测 3389/3389 与 JSON 键相等），
-- 所以 `entity.attributes.skill_ids` 能直接连到这里。
CREATE TABLE IF NOT EXISTS passive_graph_node (
    node_id         TEXT PRIMARY KEY,  -- = GGG skill id
    name_zh         TEXT NOT NULL,
    name_en         TEXT,
    -- keystone / notable / mastery / jewel_socket / ascendancy / normal
    kind            TEXT NOT NULL CHECK(kind <> ''),
    ascendancy_id   TEXT NOT NULL,     -- 非空 = 升华子树上的节点
    -- 职业起点节点的职业名（中文），其余为空串
    class_start     TEXT NOT NULL,
    -- 路径能否**经过**它。三类不可经过，每一类都是游戏规则而不是数据问题：
    --   root       7 个职业起点都连在它上面，但游戏里不能从一个职业穿到另一个职业
    --              （不排除它，「野蛮人→女巫」会算成 2 点，实测就是这么错的）
    --   升华节点   只能从升华起点进入，不在主树连通域里
    --   大师天赋   点了之后选一个效果，它不作为通路
    -- 珠宝插槽**可经过**：游戏里它就是一个能点的普通节点。
    passable        INTEGER NOT NULL CHECK(passable IN (0,1)),
    tree_version    TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pgnode_name ON passive_graph_node(name_zh);
CREATE INDEX IF NOT EXISTS idx_pgnode_kind ON passive_graph_node(kind, passable);

-- 无向边，规范化存一次（node_a < node_b）。JSON 里 `in` / `out` 两侧都列了同一条边，
-- 不规范化会存成两条，最短路径本身不受影响，但「树上有多少条连线」会翻倍。
CREATE TABLE IF NOT EXISTS passive_graph_edge (
    node_a          TEXT NOT NULL,
    node_b          TEXT NOT NULL,
    tree_version    TEXT NOT NULL,
    PRIMARY KEY (node_a, node_b),
    CHECK (node_a < node_b)
);

-- 树的总量常量。**不塞进 `game_constant`**：那张表的列注释与来源约定都是围绕
-- PoB 的 dat 导出写的（`dat_source`、钉 commit 的 blob 行号锚点），
-- 混进 POEDB 的树数据会让那些列对一半的行说不通。
CREATE TABLE IF NOT EXISTS passive_tree_meta (
    key             TEXT PRIMARY KEY,  -- totalPoints / ascendancyPoints
    value_num       REAL NOT NULL,
    tree_version    TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    updated_at      TEXT NOT NULL
);

-- ── 通货（POEDB `/cn/Currency`）──
-- 那页**一张表都没有**（卡片布局），内容在 `a.item_currency` + `div.property`
-- + `div.explicitMod` 三个 class 里。先前把它记成「已抓取未入库」时，
-- 「0 张表」曾被当成「抽不出来」——实测能抽出 113 条，那个判断是错的。
--
-- 稳定键取 `data-hover` 里的 GGG 元数据路径（`Metadata/Items/Currency/CurrencyRerollRare`），
-- 不用中文名也不用 slug：中文名会随译名调整，slug 只是 URL 标识。
CREATE TABLE IF NOT EXISTS poedb_currency (
    metadata_id     TEXT PRIMARY KEY,
    name_zh         TEXT NOT NULL CHECK(name_zh <> ''),
    -- 权威英文名来自 `/us/Currency` 同一页。取不到时为 NULL，
    -- **禁止由 slug 反推**（与 entity.name_en 同一条规则）。
    name_en         TEXT,
    poedb_slug      TEXT NOT NULL CHECK(poedb_slug <> ''),
    item_class      TEXT NOT NULL,     -- 页面 class，实测 113 条全是 StackableCurrency
    stack_max       INTEGER NOT NULL CHECK(stack_max > 0),
    -- 效果原文。**15 条是 NULL**：碎片（卷轴碎片、混沌石碎片…）与命能/结晶在页面上
    -- 没有效果文本。补一句自己写的说明就是编造，所以留空并在覆盖声明里写清有多少条。
    effect_zh       TEXT,
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    content_hash    TEXT NOT NULL CHECK(content_hash <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(poedb_slug)
    -- **没有 UNIQUE(name_zh)**：同名不同条目在 POEDB 上是真实形态（底材那边实测过 4 例），
    -- 通货目前没撞上，但不预先加一条将来要删的约束。
);
CREATE INDEX IF NOT EXISTS idx_currency_name ON poedb_currency(name_zh);

-- ── 圣甲虫（POEDB `/cn/Scarab`）──
-- 与 `poedb_currency` 分表而不是合表：那张表的语义是「通货」，把圣甲虫塞进去会让表名说谎。
-- 卡片解析两页共用 `kb/sources/poedb_cards.py`（结构逐字相同），分开的只是表与域。
--
-- 页面上有三个物品类，只收 `MapFragment`（118 条真圣甲虫）；
-- 4 条亡者之城包与 1 条孵化器按类别登记进台账，不入这张表。
CREATE TABLE IF NOT EXISTS poedb_scarab (
    metadata_id     TEXT PRIMARY KEY,
    name_zh         TEXT NOT NULL CHECK(name_zh <> ''),
    name_en         TEXT,              -- 取不到时为 NULL，**禁止由 slug 反推**
    poedb_slug      TEXT NOT NULL CHECK(poedb_slug <> ''),
    stack_max       INTEGER NOT NULL CHECK(stack_max > 0),
    -- 地图装置里的放置上限（页面上的「限制」）。与 stack_max 是两回事：
    -- 混一起会把「最多能放几个」答成堆叠上限 20。实测分布 1..5。
    device_limit    INTEGER,
    effect_zh       TEXT,              -- 实测 118 条全有
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    content_hash    TEXT NOT NULL CHECK(content_hash <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(poedb_slug)
);
CREATE INDEX IF NOT EXISTS idx_scarab_name ON poedb_scarab(name_zh);

-- ── 星团珠宝（PoB `Data/ClusterJewels.lua`）──
-- 300 个星团核心天赋**早就作为天赋实体在库里了**（它们在树 JSON 里，随天赋树一起入库），
-- 缺的一直是**分类**：不知道这 300 个只能从星团珠宝拿、不在主树上。
-- 上一轮的 `passive_path` 因此只能说「可能只存在于星团珠宝或升华子树」——
-- 这三张表把那句「可能」变成事实。
CREATE TABLE IF NOT EXISTS pob_cluster_jewel (
    size            TEXT PRIMARY KEY,  -- Small / Medium / Large
    min_nodes       INTEGER NOT NULL CHECK(min_nodes > 0),
    max_nodes       INTEGER NOT NULL CHECK(max_nodes >= min_nodes),
    notable_slots   INTEGER NOT NULL,
    socket_slots    INTEGER NOT NULL,
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    pob_url         TEXT NOT NULL CHECK(pob_url <> ''),
    source_commit   TEXT NOT NULL CHECK(length(source_commit) = 40),
    updated_at      TEXT NOT NULL
);

-- 每个尺寸可选的「增加的小天赋给予…」类型。实测 Small 17 / Medium 21 / Large 17。
-- **只有英文**：POEDB 侧的中文附魔文本不在已缓存的任何一页上（`Cobalt_Jewel` 页
-- 601KB 里一句「小天赋」都没有），所以按 PoB 原文存并在卡片上标明是英文——
-- 与宝石的 `description_en` 同一处理，不自己翻译。
CREATE TABLE IF NOT EXISTS pob_cluster_skill (
    size            TEXT NOT NULL,
    tag             TEXT NOT NULL,     -- affliction_maximum_life
    name_en         TEXT NOT NULL CHECK(name_en <> ''),
    stats_en        TEXT NOT NULL CHECK(json_valid(stats_en)),
    enchant_en      TEXT NOT NULL CHECK(json_valid(enchant_en)),
    PRIMARY KEY (size, tag)
);

-- 星团珠宝上的核心天赋与基石清单。中文名**从已入库的天赋实体桥接**
-- （按 PoB 的英文名对 `entity.name_en`），不另抓一遍树数据。
CREATE TABLE IF NOT EXISTS pob_cluster_notable (
    name_en         TEXT PRIMARY KEY,
    name_zh         TEXT,              -- 桥接不上时为 NULL，不由英文名硬翻
    entity_id       TEXT,              -- 对应的天赋卡实体，便于回查属性
    is_keystone     INTEGER NOT NULL CHECK(is_keystone IN (0,1)),
    sort_order      INTEGER,
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    pob_url         TEXT NOT NULL CHECK(pob_url <> ''),
    source_commit   TEXT NOT NULL CHECK(length(source_commit) = 40),
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clusternotable_zh ON pob_cluster_notable(name_zh);

-- ── 地图（POEDB `/cn/Atlas_of_Worlds`）──
-- 三页里只有这一页有实质内容：`Maps` 页的主表是「地图碎片 → 区域」且区域名是未翻译的
-- 英文 id（`AnyCorruptedSideArea`），`Atlas` 页只有 1 张中文卡片。**结构要一页一页看**。
--
-- 这一页是卡片布局（`div.d-flex` × 164），每张卡给：名字、等阶、额外目标、头目、
-- 掉落的命运卡、解锁的工艺台配方、以及 **Link（异界图鉴上连着哪些图）**。
CREATE TABLE IF NOT EXISTS poedb_map (
    name_zh         TEXT PRIMARY KEY,  -- 实测 163 个名字全部唯一
    -- 等阶。**实测只有 110/159 张有**（梦魇/传奇等特殊地图没给），所以可空——
    -- 补 0 会让「这张图几阶」答成 0 阶。
    tier            INTEGER,
    bonus_objective TEXT,              -- 「在 魔法 或更稀有版本的该地图内击败头目」
    bosses          TEXT NOT NULL CHECK(json_valid(bosses)),        -- 头目中文名
    divination_cards TEXT NOT NULL CHECK(json_valid(divination_cards)),
    -- 异界图鉴上相邻的地图（中文名）。这就是图鉴的连接关系，159 张卡全有。
    links           TEXT NOT NULL CHECK(json_valid(links)),
    bench_recipe    TEXT,              -- 打完头目解锁的工艺台配方，如「阻灵术 - 等级 1」
    atlas_points    TEXT,              -- 传奇/梦魇地图提供的异界天赋点
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    content_hash    TEXT NOT NULL CHECK(content_hash <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_map_tier ON poedb_map(tier);

-- ── 跨来源冲突台账 ──
-- 需求 15.5/15.6：冲突时并列展示双方并推荐 POEDB，不得自行判定谁对而隐藏一方。
-- 因此冲突必须**落库**，不能只在导入日志里一闪而过。
CREATE TABLE IF NOT EXISTS source_conflict (
    conflict_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    subject      TEXT NOT NULL,        -- 冲突对象，如 mod_group=IncreasedLife ilvl=44
    field        TEXT NOT NULL,        -- 冲突字段，如 ranges / weight / tier_count
    kind         TEXT NOT NULL,        -- mod / unique / base / constant
    source_a     TEXT NOT NULL,
    value_a      TEXT NOT NULL,
    ref_a        TEXT,                 -- a 侧的 uid 或 entity_id
    url_a        TEXT,
    source_b     TEXT NOT NULL,
    value_b      TEXT NOT NULL,
    ref_b        TEXT,
    url_b        TEXT,
    note         TEXT,
    -- 差异的**成因**（不是结论）。人工核对必须按成因分批看，逐行看不动：
    -- ladder_partial   两源阶梯大部分重合、个别档位对不上 → 真正值得先看的一类
    -- ladder_identical 阶梯逐档全等却仍有单行对不上 → 同一词缀内部的抽取问题
    -- 见 scripts/verify_pob_mods.py 的配对说明。
    cause        TEXT,
    resolved     INTEGER DEFAULT 0,    -- 人工确认后置 1
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    UNIQUE(subject, field, source_a, source_b)
);
CREATE INDEX IF NOT EXISTS idx_conflict_kind ON source_conflict(kind, resolved);

-- ── 知识库自描述：覆盖声明 ──
-- 为什么要落库而不是每次现算：它要注入 system prompt、要供缺据声明归因、
-- 要让 kb_search 的 category 枚举说真话，三处都在请求路径上，每次扫全库不合适。
--
-- 关键约定：这张表**全部由 kb/coverage.py 探测生成**，任何一行都不是手写的。
-- 手写的只有 DOMAINS 里「这个领域应该存在」的声明；「我们实际有多少」一律现测。
-- 两者的差集就是覆盖缺口，这样清单永远不会和实际状态漂移。
CREATE TABLE IF NOT EXISTS kb_coverage (
    domain_key       TEXT PRIMARY KEY,
    display_zh       TEXT NOT NULL,
    category         TEXT NOT NULL,     -- 六个一级分类之一
    -- ingested 已采集 / partial 部分采集 / harvested 已抓取未入库 / not_collected 未采集
    status           TEXT NOT NULL,
    item_count       INTEGER DEFAULT 0, -- 实测条目数
    chunk_count      INTEGER DEFAULT 0,
    channels         TEXT,              -- JSON：子渠道（词缀的 24 个来源等）
    authority_url    TEXT NOT NULL,     -- 未采集时缺据声明靠它指路，故必填
    tools            TEXT,              -- JSON：能精确查询它的工具名
    hints            TEXT,              -- JSON：**意图识别**词，非事实断言
    question_hints   TEXT,              -- JSON：能回答哪类问题（路由表）
    confidence_notes TEXT,              -- JSON：实测的置信度限制，数字现算不写死
    partial_note     TEXT,              -- status=partial 时说明缺什么
    note             TEXT,
    probed_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coverage_status ON kb_coverage(status);

-- ── Tier B：知识卡片切片（语义层）──
CREATE TABLE IF NOT EXISTS chunk (
    chunk_id   TEXT PRIMARY KEY,
    entity_id  TEXT,
    text       TEXT NOT NULL,
    metadata   TEXT NOT NULL,          -- JSON: DocumentMetadata
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunk_entity ON chunk(entity_id);

-- ── 采集任务记录 ──
CREATE TABLE IF NOT EXISTS ingest_run (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    category    TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    total       INTEGER DEFAULT 0,
    succeeded   INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0,
    elapsed_ms  INTEGER DEFAULT 0,
    note        TEXT
);

-- ── 缺口台账：每条缺据声明都记下来，按频次排序即为采集优先级 ──
CREATE TABLE IF NOT EXISTS knowledge_gap (
    gap_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    question     TEXT NOT NULL,
    missing_point TEXT NOT NULL,
    guessed_category TEXT,
    -- 条目来源，见 GAP_KINDS。**只有 retrieval_gap 与 tool_gap 构成采集优先级**：
    -- binding_block 是闸门行为的观测量，混进来会让频次排序失真。
    kind         TEXT,
    hit_count    INTEGER DEFAULT 1,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gap_hits ON knowledge_gap(hit_count DESC);

-- ── 会话与执行轨迹 ──
CREATE TABLE IF NOT EXISTS conversation (
    conversation_id TEXT PRIMARY KEY,
    title           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message (
    message_id      TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    citations       TEXT,              -- JSON
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_message_conv ON message(conversation_id);

CREATE TABLE IF NOT EXISTS trace (
    trace_id        TEXT PRIMARY KEY,
    conversation_id TEXT,
    intent          TEXT,
    plan            TEXT,
    tool_calls      TEXT,              -- JSON
    retrieval_hits  TEXT,              -- JSON
    binding_result  TEXT,              -- JSON：断言绑定校验结果与拦截原因
    tokens_prompt   INTEGER DEFAULT 0,
    tokens_output   INTEGER DEFAULT 0,
    tokens_reasoning INTEGER DEFAULT 0, -- 推理 token 单列：实测占比可达 98%
    elapsed_ms      INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL
);
"""

# FTS5 外部内容表：tokens 存 jieba 预分词结果，keywords 存 search_keywords
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    tokens,
    keywords,
    chunk_id UNINDEXED,
    entity_id UNINDEXED,
    tokenize = 'unicode61'
);
"""


def _probe_vector(conn: sqlite3.Connection) -> VectorStatus:
    """三级探测 sqlite-vec：属性存在 → 能启用 → 能加载。任一失败即禁用。"""
    if not hasattr(conn, "enable_load_extension"):
        return VectorStatus(
            False,
            "当前 Python 的 sqlite3 未编译扩展加载支持（OMIT_LOAD_EXTENSION）；"
            "请改用 python.org 或 Homebrew 发行版",
        )
    try:
        conn.enable_load_extension(True)
    except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError) as exc:
        return VectorStatus(False, f"无法启用扩展加载: {exc}")

    try:
        import sqlite_vec  # noqa: PLC0415  延迟导入：未安装时不应影响启动
    except ImportError:
        return VectorStatus(False, "未安装 sqlite-vec（Windows on ARM 无对应 wheel）")

    try:
        sqlite_vec.load(conn)
        version = conn.execute("select vec_version()").fetchone()[0]
        return VectorStatus(True, f"sqlite-vec {version}")
    except Exception as exc:  # 扩展二进制与平台不匹配等
        return VectorStatus(False, f"加载 sqlite-vec 失败: {type(exc).__name__}: {exc}")
    finally:
        try:
            conn.enable_load_extension(False)
        except Exception:  # noqa: BLE001  关闭失败不影响后续
            pass


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移。只做「补上本该存在的约束」这类修正，不做破坏性变更。

    knowledge_gap 的去重与唯一索引：
      台账原先没有 UNIQUE(question, missing_point)，而写入代码用了
      `ON CONFLICT DO NOTHING` —— 冲突目标不存在，于是每次调用都插新行，
      紧随其后的 UPDATE 又会把**所有**同题行的 hit_count 一起加一。
      实测 51 行里只有 22 个不同条目，一条问题重复 18 行、hit_count 累到 189。
      后果是「按频次排序即采集优先级」这个核心信号完全失真。
      这里合并重复行（hit_count 取该组行数，即真实出现次数），再补上唯一索引，
      让同类 bug 以后直接撞约束而不是静默积累。
    """
    try:
        dup = conn.execute(
            "SELECT count(*) FROM (SELECT 1 FROM knowledge_gap "
            "GROUP BY question, missing_point HAVING count(*) > 1)"
        ).fetchone()[0]
        if dup:
            conn.executescript(
                """
                CREATE TEMP TABLE _gap_merged AS
                SELECT question, missing_point,
                       MAX(COALESCE(guessed_category,'')) AS guessed_category,
                       COUNT(*) AS hit_count,
                       MIN(first_seen) AS first_seen,
                       MAX(last_seen)  AS last_seen
                FROM knowledge_gap GROUP BY question, missing_point;
                DELETE FROM knowledge_gap;
                INSERT INTO knowledge_gap(question, missing_point, guessed_category,
                                          hit_count, first_seen, last_seen)
                SELECT question, missing_point,
                       NULLIF(guessed_category,''), hit_count, first_seen, last_seen
                FROM _gap_merged;
                DROP TABLE _gap_merged;
                """
            )
            logger.info("缺口台账去重：合并了 %d 组重复条目", dup)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_gap_unique "
            "ON knowledge_gap(question, missing_point)"
        )
        _migrate_gap_kind(conn)
        _migrate_base_item_all_categories(conn)
        _migrate_conflict_cause(conn)
        _migrate_reservation_columns(conn)
        conn.commit()
    except sqlite3.Error as exc:
        # 迁移失败不能让启动失败——但必须告警，否则台账会继续悄悄积累重复
        logger.warning("缺口台账迁移未完成：%s", exc)


#: 台账条目的来源。三者要分开，因为对应的待办完全不同：
#:   retrieval_gap  检索判定依据不足        → 该去采集，或该改检索
#:   tool_gap       结构化工具自报缺据      → 该去补那份结构化数据
#:   binding_block  出口闸门拦下一句无据断言 → 该看是漏出还是错误拒答
#: 只有前两类构成「采集优先级」，第三类是闸门行为的观测量。
GAP_KINDS = ("retrieval_gap", "tool_gap", "binding_block", "binding_block_legacy")


def _migrate_reservation_columns(conn: sqlite3.Connection) -> None:
    """给 `pob_skill_level` 补 `reservation_flat`（第二十四轮）与基础伤害三列（第二十九轮）。

    为什么需要：先前只收了 `manaReservationPercent`，于是**定值保留**的技能
    （清晰 / 精准 / 活力）在库里看起来「不保留任何魔力」——而 PoB 侧有 402 条
    `manaReservationFlat`。把「保留 28 点魔力」答成「不保留」，比查不到更糟。

    补列之后要**重跑 `scripts/import_pob_skills.py`** 才有值：这里只改结构，
    不伪造数据。列留空的那一段时间里，工具读到 NULL 就是「未采集」。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pob_skill_level)")}
    added = []
    for column in ("reservation_flat", "base_damage_min", "base_damage_max"):
        if column not in cols:
            conn.execute(f"ALTER TABLE pob_skill_level ADD COLUMN {column} REAL")
            added.append(column)
    if "damage_type" not in cols:
        conn.execute("ALTER TABLE pob_skill_level ADD COLUMN damage_type TEXT")
        added.append("damage_type")
    if added:
        conn.commit()
        logger.info("pob_skill_level 补列完成：%s（需重跑技能石导入才有值）", added)


def _migrate_conflict_cause(conn: sqlite3.Connection) -> None:
    """给 `source_conflict` 补 `cause` 列。

    为什么需要：`kind='mod_review'` 一度有 2729 行，人工核对无从下手——而其中大部分
    并不是数值分歧，是**配对配错了**（按组名配对，而两源组名系统性不同）。改成按阶梯
    指纹配对后剩 1081 行，且成因单一。成因必须落列而不是塞进 `note`：
    要能 `GROUP BY cause` 才谈得上「按成因分批核对」。

    历史行的 cause 留空——它们是旧判据产出的，不能事后编一个成因给它们。
    重跑 `scripts/verify_pob_mods.py --write-conflicts` 会按新判据重写。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(source_conflict)")}
    if "cause" in cols:
        return
    conn.execute("ALTER TABLE source_conflict ADD COLUMN cause TEXT")
    conn.commit()
    logger.info("source_conflict 补 cause 列完成（历史行留空）")


def _migrate_gap_kind(conn: sqlite3.Connection) -> None:
    """给 `knowledge_gap` 补 `kind` 列，并把历史行按可辨识特征回填。

    为什么必须分类：`_record_gaps` 曾把**被拦句子的原文**当作 missing_point 写进台账
    且不带分类，于是台账里混进了 `缺少可引用依据：---`、`缺少可引用依据：）`、
    `缺少可引用依据：**隐式词缀**` 这类条目——它们不是知识缺口，是闸门的分类误判。
    需求 2.12 说「按频次排序即采集优先级」，混进这些噪声后那个排序就没有意义了。

    历史行只**标记**不删除：台账是审计记录，删掉就无法回溯当初为什么积累了噪声。
    标为 `binding_block_legacy` 后从采集优先级列表里过滤掉即可。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_gap)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE knowledge_gap ADD COLUMN kind TEXT")
        logger.info("缺口台账迁移：已补 kind 列")
    # 回填：历史上只有出口闸门会写「缺少可引用依据：」前缀且不带分类
    updated = conn.execute(
        "UPDATE knowledge_gap SET kind='binding_block_legacy' "
        "WHERE kind IS NULL AND missing_point LIKE '缺少可引用依据：%'"
    ).rowcount
    conn.execute(
        "UPDATE knowledge_gap SET kind='tool_gap' "
        "WHERE kind IS NULL AND missing_point LIKE '[%]%'"
    )
    conn.execute("UPDATE knowledge_gap SET kind='retrieval_gap' WHERE kind IS NULL")
    if updated:
        logger.info(
            "缺口台账迁移：%d 条历史闸门噪声已标为 binding_block_legacy"
            "（不再计入采集优先级）", updated,
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gap_kind ON knowledge_gap(kind)")


def _migrate_base_item_all_categories(conn: sqlite3.Connection) -> None:
    """把 `pob_base_item` 从「只装胸甲」重建为「全装备类别」。

    原表的 CHECK 把 `item_class` 钉死在 `'Body Armour'`、`source_file` 钉死在
    `body.lua`，`subtype <> ''` 与 `socket_limit > 0` 也是照胸甲写的（腰带没有
    subType、戒指没有插槽）。SQLite 改不了 CHECK，只能重建表。

    旧行不丢：按 `category_key='body'` / `poedb_page='Body_Armours'` /
    `stats_kind='armour'` 回填，`defences` 原样搬进 `stats`。
    这三个值对旧数据是**事实**而不是猜测——旧表的 CHECK 保证了里面只有胸甲。
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pob_base_item'"
    ).fetchone()
    if row is None:
        return  # 表还没建，由 SCHEMA 负责
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pob_base_item)")}
    needs_columns = "stats" not in cols
    # 第二处形态变更：去掉中文名上的唯一约束。POEDB 会把两个底材译成同一个中文名，
    # 那条约束在只有胸甲时恰好成立，加入手套与法杖后立刻撞。
    #
    # **按真实索引判，不要匹配 SQL 文本**：SQLite 把建表语句连注释一起存进
    # sqlite_master，而本表的注释里就写着那个约束名——按文本匹配会永远命中，
    # 于是每次启动都重建一次表（实测踩到，日志里连着两条迁移记录）。
    needs_unique_drop = _has_single_column_unique(conn, "pob_base_item", "name_zh")
    if not (needs_columns or needs_unique_drop):
        return

    what = "补齐全类别列" if needs_columns else "去掉 UNIQUE(name_zh)"
    logger.info("底材表迁移：重建 pob_base_item（%s）", what)
    conn.execute("ALTER TABLE pob_base_item RENAME TO pob_base_item_old")
    conn.executescript(_base_item_schema())
    if needs_columns:
        # body-only 旧结构：category_key / poedb_page / stats_kind 按事实回填
        # （旧表的 CHECK 保证里面只有胸甲），`defences` 原样搬进 `stats`
        select = (
            "SELECT base_id,name_en,name_zh,poedb_slug,'body','Body_Armours',item_class,"
            "subtype,socket_limit,tags,influence_tags,armour_tag,modifier_page,"
            "implicit_text,implicit_mod_types,implicit_ids,defences,'armour',"
            "requirements,source_file,source,source_priority,pob_url,poedb_url,"
            "source_commit,pob_version,game_version,content_hash,updated_at"
        )
    else:
        select = (
            "SELECT base_id,name_en,name_zh,poedb_slug,category_key,poedb_page,item_class,"
            "subtype,socket_limit,tags,influence_tags,armour_tag,modifier_page,"
            "implicit_text,implicit_mod_types,implicit_ids,stats,stats_kind,"
            "requirements,source_file,source,source_priority,pob_url,poedb_url,"
            "source_commit,pob_version,game_version,content_hash,updated_at"
        )
    conn.execute(
        """
        INSERT INTO pob_base_item(
            base_id,name_en,name_zh,poedb_slug,category_key,poedb_page,item_class,
            subtype,socket_limit,tags,influence_tags,armour_tag,modifier_page,
            implicit_text,implicit_mod_types,implicit_ids,stats,stats_kind,
            requirements,source_file,source,source_priority,pob_url,poedb_url,
            source_commit,pob_version,game_version,content_hash,updated_at
        )
        """
        + select
        + " FROM pob_base_item_old"
    )
    moved = conn.execute("SELECT count(*) FROM pob_base_item").fetchone()[0]
    conn.execute("DROP TABLE pob_base_item_old")
    logger.info("底材表迁移完成：搬入 %d 条既有记录", moved)


def _has_single_column_unique(
    conn: sqlite3.Connection, table: str, column: str
) -> bool:
    """表上是否有「只含该列」的唯一约束（`origin='u'` 即建表里的 UNIQUE(...)）。"""
    for index in conn.execute(f"PRAGMA index_list({table})"):
        if not index["unique"] or index["origin"] != "u":
            continue
        cols = [r["name"] for r in conn.execute(f"PRAGMA index_info({index['name']})")]
        if cols == [column]:
            return True
    return False


def _base_item_schema() -> str:
    """从 SCHEMA 里切出 `pob_base_item` 的建表语句。

    切而不是复制一份：复制的那份迟早与 SCHEMA 分叉，届时迁移建出来的表
    与全新库建出来的表结构不同——那种 bug 只会在「升级过的库」上出现。
    """
    marker = "CREATE TABLE IF NOT EXISTS pob_base_item ("
    start = SCHEMA.index(marker)
    end = SCHEMA.index(");", start) + 2
    stmt = SCHEMA[start:end]
    tail = SCHEMA[end:]
    # 连带把紧随其后的索引一起建出来
    for line in tail.splitlines():
        if line.strip().startswith("CREATE INDEX") and "pob_base_item" in line:
            stmt += "\n" + line.strip()
        elif line.strip().startswith("CREATE TABLE"):
            break
    return stmt


def init_db(db_path: Path, vector_dim: int = 0) -> sqlite3.Connection:
    """建立单连接、开 WAL、建表、探测向量扩展。幂等。"""
    global _conn, _vector_status

    with _lock:
        if _conn is not None:
            return _conn

        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row

        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")

        conn.executescript(SCHEMA)
        conn.executescript(FTS_SCHEMA)
        _migrate(conn)

        _vector_status = _probe_vector(conn)
        if _vector_status.available and vector_dim > 0:
            # 维度不写死：由 embedding 模型实际输出维度决定
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0("
                f"chunk_id TEXT PRIMARY KEY, embedding FLOAT[{int(vector_dim)}])"
            )
            logger.info("向量通道就绪，维度 %d", vector_dim)
        elif _vector_status.available:
            logger.info("向量扩展可用但未配置维度，向量表未建（vector.enabled=false）")
        else:
            logger.info("向量通道禁用：%s", _vector_status.reason)

        conn.commit()
        _conn = conn
        return conn


def get_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("数据库未初始化，请先调用 init_db()")
    return _conn


def vector_status() -> VectorStatus:
    return _vector_status or VectorStatus(False, "未探测")


def close_db() -> None:
    """关闭前做 WAL checkpoint，避免残留 -wal / -shm 文件。"""
    global _conn, _vector_status
    with _lock:
        if _conn is None:
            return
        try:
            _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _conn.commit()
        except sqlite3.Error as exc:
            logger.warning("关闭前 checkpoint 失败: %s", exc)
        finally:
            _conn.close()
            _conn = None
            _vector_status = None
