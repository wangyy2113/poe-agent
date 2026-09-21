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
from collections.abc import Sequence
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

-- ── 收获（古灵庄园）定向工艺（POEDB `/cn/Horticrafting`）──
-- 与工艺台同一条边界：本表只保存页面明确列出的**效果文本与原始命能费用**。
-- `craft/harvest_operations.py`、`craft/harvest_modifier_operations.py` 与
-- `craft/harvest_enchant_operations.py` 另以 57 条逐字原文为契约实现装备重铸、
-- 元素转换、新增/移除与品质附魔状态转移；其余目录项不承载「对当前物品可执行」
-- 的语义，也不给成功率与最经济路线。
--
-- 那一页有三张表，本表只对应第一张（74 行定向工艺）。另外两张
-- （51 行作物与掉落来源、41 行催化/化石/工艺台/花园工艺站混排）**有意不取**，
-- 由采集器按行数登记进 knowledge_gap——静默省略与「页面上就没有」事后分不清。
--
-- **刻意没有分类列。** 描述里带 `<span class="craftingfire">` 这类 class，看着像现成的
-- 分类标签，实测是**配色不是语义**：「召唤生物词缀」与「魔力词缀」都标成
-- craftingcaster，「元素词缀」则一个 class 都没有。存了它，「哪些收获工艺能改施法词缀」
-- 就会答出召唤生物与魔力、还漏掉元素。而描述原文里本来就有那个中文词，
-- 检索与 LIKE 都能匹配——留一个错的分类列，比没有分类列糟得多。
CREATE TABLE IF NOT EXISTS harvest_craft (
    craft_id        TEXT PRIMARY KEY,  -- 完整记录 canonical JSON 的 SHA-256
    effect_text     TEXT NOT NULL CHECK(effect_text <> ''),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    content_hash    TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_harvest_effect ON harvest_craft(effect_text);

-- 费用。一条工艺 1..2 个组件（实测 74 条里 50 条单组件、24 条双组件，共 98 条）。
-- `currency_metadata_id` 是与 `poedb_currency` 的桥接键（实测 5/5 命中、中文名逐字一致），
-- 采集时逐条对账；它比中文名与 slug 都稳——名字会随译名调整，slug 只是 URL 标识。
CREATE TABLE IF NOT EXISTS harvest_craft_cost (
    craft_id        TEXT NOT NULL,
    position        INTEGER NOT NULL CHECK(position >= 0),
    amount          INTEGER NOT NULL CHECK(amount > 0),
    currency_metadata_id TEXT NOT NULL CHECK(currency_metadata_id <> ''),
    currency_slug   TEXT NOT NULL CHECK(currency_slug <> ''),
    currency_name_zh TEXT NOT NULL CHECK(currency_name_zh <> ''),
    currency_url    TEXT NOT NULL CHECK(currency_url <> ''),
    PRIMARY KEY (craft_id, position),
    FOREIGN KEY (craft_id) REFERENCES harvest_craft(craft_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_harvest_cost_currency
    ON harvest_craft_cost(currency_metadata_id);

-- ── Boss 技能的伤害建模（PoB `Data/BossSkills.lua` / `Data/Bosses.lua`）──
-- **补的是伤害建模，不是打法。** PoB 这两个文件给的是「某技能在某等级打多少、
-- 穿多少、Uber 乘多少」，不含机制、阶段与应对——后者本地没有来源，
-- `boss` 域的 partial_note 要逐字写清这条分界。
--
-- **伤害不存在这张表里**：存的是系数，真实伤害要按
-- `round(monsterDamageTable[等级] × (基准 + 掷骰区间 × 增量) × Uber 倍率)` 算
-- （`Modules/ConfigOptions.lua:2258`，算在 `calc/boss_damage.py`）。
-- 存算好的值就得为每个等级存一行，而等级表本来就在 game_constant 里。
--
-- 这一维**只有 PoB 一个来源**（P2）：GGG 没有公布 Boss 技能的伤害系数。
-- 所以证据里要写明它是建模值，不能读起来像官方数值。
CREATE TABLE IF NOT EXISTS pob_boss_skill (
    name            TEXT PRIMARY KEY,  -- Shaper Ball / Atziri Flameblast
    -- **没有 name_zh 列**：本地找不到这些终局 Boss 的中文名（`poedb_spectre` 只有
    -- 中文名没有英文名，且这些 Boss 本来就不能做幽魂；entity 里的「塑界者」只出现在
    -- 物品名里）。加一列当前管线永远填不上的空列，正是 `pob_skill_level`
    -- 表注释警告过的坑——下一个人会以为它待填，然后拿别处的名字硬凑。
    damage_type     TEXT NOT NULL,     -- Spell / SpellProjectile / Melee / DamageOverTime
    -- JSON {伤害类型: [基准, 每 1% 掷骰增量]}，如 {"Cold": [11.67, 0.0583]}
    damage_mults    TEXT NOT NULL CHECK(json_valid(damage_mults)),
    -- JSON {穿透键: 值}。**上游写成空字符串的项不收**（`Maven Fireball` 的 FirePen
    -- 在 BossSkills.lua 里就是 ""，PoB 拿它当「这一栏不填」）——留空与 0 是两件事。
    penetrations    TEXT NOT NULL CHECK(json_valid(penetrations)),
    uber_penetrations TEXT NOT NULL CHECK(json_valid(uber_penetrations)),
    uber_damage_mult REAL,             -- 没有这一项就是 Uber 不额外乘
    speed           REAL,
    uber_speed      REAL,
    crit_chance     REAL,
    earlier_uber    INTEGER NOT NULL CHECK(earlier_uber IN (0,1)),
    tooltip         TEXT,              -- PoB 原文，含少数几条可抽验的穿透数字
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    pob_url         TEXT NOT NULL CHECK(pob_url <> ''),
    source_commit   TEXT NOT NULL CHECK(length(source_commit) = 40),
    updated_at      TEXT NOT NULL
);

-- Boss 的受击建模系数。与技能分表：一个 Boss 有多个技能，而护甲/闪避倍率是 Boss 的属性。
CREATE TABLE IF NOT EXISTS pob_boss (
    key             TEXT PRIMARY KEY,  -- Shaper / Sirus / Maven
    -- 同样没有 name_zh，理由见上表注释
    armour_mult     REAL NOT NULL,
    evasion_mult    REAL NOT NULL,
    is_uber         INTEGER NOT NULL CHECK(is_uber IN (0,1)),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    pob_url         TEXT NOT NULL CHECK(pob_url <> ''),
    source_commit   TEXT NOT NULL CHECK(length(source_commit) = 40),
    updated_at      TEXT NOT NULL
);

-- ── 前后缀名额规则（POEDB `/cn/Modifiers` × PoB `Classes/Item.lua`）──
-- 这张表是 `check_mod_combination` 能说出 `possible` 的**唯一前提**：在它之前，
-- `craft/feasibility.py` 末尾有一行无条件的 `verdict = "unknown"`，三态里的第三态是死的。
--
-- **不塞进 `game_constant`**：那张表是「键 → 标量」，而这里是
-- (稀有度 × 物品种类) → 两个上限，还要记「哪一侧说的」。拆成十几个散键会丢掉
-- 谓词语义（判定时要按种类挑规则），塞进 value_json 又会变成第二类
-- 「入库了却取不出来」（第二十六轮那 46 行的教训）。
-- 同一条理由已经写在 `passive_tree_meta` 的表注释里。
--
-- **`prefix_limit IS NULL` = 不受限制**（传奇物品），不是「没查到」：
-- 没查到的规则根本不会有行。判定时必须把这两件事分开，
-- 否则「传奇不受限制」会被读成「规则缺失 → unknown」。
--
-- `agreement` 记这条规则是谁说的：`both` 两侧一致（魔法 1+1、稀有 3+3）、
-- `poedb_only`（传奇不受限制、药剂只有魔法）、`pob_only`（珠宝与 Graft 2+2，
-- 以及**已腐化的深渊珠宝回到 3+3** 这条反直觉的例外）。
-- 单侧来源不是冲突，但呈现时要能看出证据只有一边。
CREATE TABLE IF NOT EXISTS craft_affix_capacity (
    rarity          TEXT NOT NULL CHECK(rarity <> ''),      -- 魔法 / 稀有 / 传奇
    -- default / flask / jewel / graft / jewel_abyss_corrupted
    item_kind       TEXT NOT NULL CHECK(item_kind <> ''),
    prefix_limit    INTEGER CHECK(prefix_limit IS NULL OR prefix_limit >= 0),
    suffix_limit    INTEGER CHECK(suffix_limit IS NULL OR suffix_limit >= 0),
    agreement       TEXT NOT NULL CHECK(agreement IN ('both','poedb_only','pob_only')),
    poedb_text      TEXT,              -- POEDB 页面原句，可引用
    pob_text        TEXT,              -- PoB 原文片段
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),   -- 推荐引用的那一侧
    pob_url         TEXT,              -- 钉住 commit 的 blob 地址（带行号锚点）
    source_commit   TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (rarity, item_kind)
);

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

-- ── 宝石的效果文本与属性需求（POEDB 详情页的逐级表，第三十二轮）──
-- 这两张表补的是**从第十二轮起就写在「未采集」里的两维**：效果描述文本与属性需求。
-- 它们为什么直到现在才敢入库，理由不是「没有数据」而是「没有口径」——
-- PoB 侧的 stat 是 id + 乘数（要按 `Modules/StatDescriber.lua` 渲染，而且渲出来是英文），
-- `reqStr/reqDex/reqInt` 是换算系数不是显示值。POEDB 详情页把两者都按等级列成了表，
-- **中文的、可引用的**，而且同一张表里的 `需求等级` 与 `魔力` 两列本库早就有 PoB 侧的值，
-- 于是每颗宝石每一级都能逐格对账（判据见 `kb/sources/poedb_gem_page.reconcile`）。
--
-- **两侧数值分开存**：这里只放 POEDB 独有的两维（效果文本、属性需求），
-- 对账用的 `需求等级`/`消耗` 不再存第二份——同一个数存两处就是两份会分叉的真源
-- （`pob_mod` 与 `mod_affix` 分表是因为两侧粒度不同，这里不是）。
--
-- 外键跟着 `pob_skill_gem` 级联：PoB 侧重跑快照时这三张表会被清空，
-- **必须紧接着重跑 `scripts/import_gem_stats.py` 与 `scripts/import_gem_quests.py`**
-- （导入脚本会提醒）。
-- 让它悄悄留着旧行更糟：宝石重命名或改版之后，效果文本会挂到另一颗宝石上。
CREATE TABLE IF NOT EXISTS poedb_gem_stat (
    gem_id          TEXT NOT NULL,
    -- 列序。页面上效果文本的**顺序是有意义的**（技能自己的效果在前、
    -- 通用加成在后），乱序展示会让用户以为是两颗宝石。
    stat_index      INTEGER NOT NULL,
    -- 列头原文，**带着 1 级的数值**（「造成 21 - 31 物理伤害」）。
    -- 不改写成 `#` 占位：那等于自己重写上游文本，而每一级的实际值在
    -- `poedb_gem_stat_level.values_json` 里。
    text_zh         TEXT NOT NULL CHECK(text_zh <> ''),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(gem_id, stat_index),
    FOREIGN KEY(gem_id) REFERENCES pob_skill_gem(gem_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS poedb_gem_stat_level (
    gem_id          TEXT NOT NULL,
    gem_level       INTEGER NOT NULL,
    -- 属性需求：**页面给了才有值**。补 0 会让「这颗宝石不需要智力」与
    -- 「这一列页面上没有」变成同一件事（`pob_skill_level` 的注释是同一条规则）。
    req_str         INTEGER,
    req_dex         INTEGER,
    req_int         INTEGER,
    -- 效果文本各列在这一级的值：`{"0": "4", "1": "16"}`（键是 stat_index 的字符串）。
    -- 存 JSON 而不是每列一行：列数随宝石变（实测 0–6 列），
    -- 而这一维**只按等级整块取用**，没有单列查询的场景。
    values_json     TEXT NOT NULL CHECK(json_valid(values_json)),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(gem_id, gem_level),
    FOREIGN KEY(gem_id) REFERENCES pob_skill_gem(gem_id) ON DELETE CASCADE
);

-- ── 收获种子怪物（POEDB `/cn/Horticrafting`，第五十八轮）──
-- 回答「命能怎么来」：种子生成的怪物按**颜色**掉对应命能。
-- 实测 51 个，Wild / Vivid / Primal 各 17。
--
-- **这张表第三十六轮起被叫错了名字**：台账里写的是「作物与掉落来源」，
-- 而它三列其实是 `怪物 ｜ 阶级 ｜ Colour`。照那个名字去想，会以为它是
-- 「哪种作物掉什么」，实际是「哪个怪物属于哪个颜色」——**注释里的名字也会骗人**。
--
-- **颜色到命能是查表不是推断**：`colour` 的三个取值与 `poedb_currency.name_en`
-- 的前缀逐字相同（`Wild Crystallised Lifeforce`）。不做外键：命能只是通货表里的
-- 四行，而这里的键是颜色；桥接在查询时做，两边都能独立重导。
--
-- **第四种命能「神圣白晶」不在这张表里**（它没有对应的种子怪物），
-- 这条边界要在出口说清，不能让用户以为四种都覆盖了。
CREATE TABLE IF NOT EXISTS poedb_harvest_seed (
    monster_zh      TEXT NOT NULL CHECK(monster_zh <> ''),
    tier            INTEGER NOT NULL CHECK(tier > 0),
    -- Wild / Vivid / Primal
    colour          TEXT NOT NULL CHECK(colour <> ''),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(monster_zh)
);
CREATE INDEX IF NOT EXISTS idx_harvest_seed_colour ON poedb_harvest_seed(colour);

-- ── 按标签改词缀权重（同一页第三张表，第六十五轮）──
-- `Type ｜ Name ｜ Tags` 四类混排：化石 / 工艺工作台 / 催化 / 花园工艺站。
-- 一张表是因为**页面就是一张表**，而四类的共同语义正是「按标签改词缀权重」。
--
-- 这张表补的是 `fossil` 域 `partial_note` 里那句「化石对权重的乘数未采集」——
-- 它**一直在这一页上**，只是台账把它记成「催化剂等参考表」、抽取器注释又写着
-- 「属于另一个域，这里仍然不取」，于是挂了近三十轮（判据见 `parse_tag_weights`）。
--
-- `multiplier` **可空**：只有化石与工艺工作台给了数字，催化与花园工艺站页面
-- 只给标签。补 1 会把「页面没说」写成「乘数是 1」——那是个看起来很合理的假值。
-- 而 `multiplier = 0` 是「这类词缀不会出现」这个**确定的事实**，与 NULL 不是一回事。
--
-- `tag` 中英混排（`冰霜` 与 `bleed` / `physical_damage`）是**页面原值**：
-- 英文那些在 `<i>` 里、上游没给中文，本库不自己翻译。
CREATE TABLE IF NOT EXISTS poedb_tag_weight (
    kind            TEXT NOT NULL CHECK(kind <> ''),   -- 化石 / 工艺工作台 / 催化 / 花园工艺站
    name_zh         TEXT NOT NULL CHECK(name_zh <> ''),
    tag             TEXT NOT NULL CHECK(tag <> ''),
    -- 页面没给数字时为 NULL；0 表示屏蔽这一类词缀
    multiplier      REAL CHECK(multiplier IS NULL OR multiplier >= 0),
    row_index       INTEGER NOT NULL,
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    -- 同一个东西可以改多个标签，所以主键要带 tag
    PRIMARY KEY(kind, name_zh, tag)
);
CREATE INDEX IF NOT EXISTS idx_tag_weight_tag ON poedb_tag_weight(tag);
CREATE INDEX IF NOT EXISTS idx_tag_weight_kind ON poedb_tag_weight(kind);

-- ── 获取途径（POEDB 详情页的 `Acquisition` 区块，第五十四轮）──
-- 「哪张命运卡能出头猎」「托沃卧怎么合」这类问题在这之前一条都答不了。
--
-- **不只是命运卡**：`ingredient` 为空是命运卡（集齐这张卡换到 `offer`），
-- 非空是**升级/合成配方**（`托沃崩 + 托沃的祝福 → 托沃卧`）。混成一类会让
-- 「托沃卧怎么来」得到错答案，所以 `kind` 由 `ingredient` 是否为空派生、不另存。
--
-- **覆盖是部分的，这一点要传到出口**：2026-09-01 实测（全部来自已缓存页）
--     唯一装备 1342 页里 264 页有 → 338 行
--     通货      113 页里  19 页有 →  36 行
--     技能石    820 页里   9 页有 →  13 行
-- 八成的唯一装备没有这一段。「库里查不到」与「没有卡能出它」在用户那边分不开，
-- 所以工具必须逐条说清是哪一种——与宝石那 152 颗「详情页未给出使命表」同一条规矩。
--
-- 不挂外键：`owner_slug` 跨三类实体（唯一装备 / 通货 / 技能石），
-- 三张表的主键各不相同；挂谁都会漏掉另外两类。
CREATE TABLE IF NOT EXISTS poedb_acquisition (
    -- 这一段属于哪个实体的详情页（该实体的 POEDB slug）
    owner_slug      TEXT NOT NULL CHECK(owner_slug <> ''),
    owner_kind      TEXT NOT NULL CHECK(owner_kind IN ('unique_item','currency','skill_gem')),
    row_index       INTEGER NOT NULL,
    -- 命运卡名，或合成配方里的主料
    source_zh       TEXT NOT NULL CHECK(source_zh <> ''),
    source_slug     TEXT NOT NULL DEFAULT '',
    -- 附加材料。**空串表示这是命运卡途径**，非空表示合成/升级配方
    ingredient      TEXT NOT NULL DEFAULT '',
    -- 换到手的东西（页面 Offer 列）
    offer_zh        TEXT NOT NULL CHECK(offer_zh <> ''),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(owner_slug, row_index)
);
CREATE INDEX IF NOT EXISTS idx_acquisition_offer ON poedb_acquisition(offer_zh);
CREATE INDEX IF NOT EXISTS idx_acquisition_source ON poedb_acquisition(source_zh);

-- ── 商店配方（POEDB `/cn/Vendor_recipe_system`，第五十一轮）──
-- 「怎么用装备换混沌石」「点金石从哪来」是最常问的获取途径，而通货/精华/圣甲虫
-- 三个域的 `partial_note` 都写着「获取途径未采集」。
--
-- **这一页一直在缓存里**（179KB、21 张表、201 行），从来没被抽取过；同一张表头
-- 也出现在宝石详情页上（第四十四轮清点时数到 32 处，当时记作「商店配方」放过了）。
-- 这次先把缓存翻了一遍，才没有去抓 113 张通货详情页——是那个「缺来源」错误的反面。
--
-- **方向不能弄反**：`result` 是**换到手的**（页面的「贡品」列），
-- `ingredients` 是**付出去的**（「你的物品」列）。写反会把「2 个重铸石换 1 个后悔石」
-- 说成反过来——一个读起来通顺的错答案。
--
-- 主键要带 `block_index`：**类别名不唯一**，`賣物品給商人 Recipe` 在页面上出现两次
-- （5 行与 2 行两张表）。只用类别名做键会丢掉其中一张。
--
-- 每块的行数由标题自证（`传奇 物品 Recipe /23`），对不上直接拒绝整页——
-- 静默少收等于漏答，而这一维的用户问题正是「有没有这个配方」。
CREATE TABLE IF NOT EXISTS poedb_vendor_recipe (
    category        TEXT NOT NULL CHECK(category <> ''),
    block_index     INTEGER NOT NULL,
    row_index       INTEGER NOT NULL,
    -- 换到手的东西（页面「贡品」列）
    result          TEXT NOT NULL CHECK(result <> ''),
    -- 付出去的东西（页面「你的物品」列）
    ingredients     TEXT NOT NULL CHECK(ingredients <> ''),
    note            TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(category, block_index, row_index)
);
CREATE INDEX IF NOT EXISTS idx_vendor_recipe_result
    ON poedb_vendor_recipe(result);

-- ── 技能 × 辅助的搭配关系（POEDB 详情页，第四十五轮）──
-- 「这个技能能配哪些辅助 / 多重投射能不能辅助冰霜新星」是最常问的一类问题，
-- 而在这之前本库完全答不了：`pob_skill_gem.skill_types` 在**辅助宝石上是空的**。
--
-- **被否掉的一条路：从 PoB 算。** PoB 有权威规则（`requireSkillTypes` /
-- `excludeSkillTypes` + `CalcTools.doesTypeExpressionMatch` 那个带 OR/AND/NOT 的
-- 栈式求值器），但 `canGrantedEffectSupportActiveSkill` **依赖角色状态**：
-- 装备的武器（Varunastra 算作所有单手）、召唤物技能、物品赋予的技能、触发类。
-- 无状态复现只能做出一个子集，而**会在武器类辅助上悄悄与 PoB 不一致的子集**
-- 正是「带着引用的错数字」。所以用 POEDB 页面自己算好的、**默认无 build 语境**
-- 的清单——那也正是用户问这句话时的语境。这条边界要写进工具的 `boundary`。
--
-- **只存两侧都确认的对。** 页面有两个方向：`被辅助`（技能→辅助，558 页）与
-- `可辅助`（辅助→技能，260 页）。2026-09-01 实测：
--     两侧一致 56,895 对（入库）
--     只在被辅助侧 1,152（其中 558 对方 slug 不在库、594 两边都在库却单侧）
--     只在可辅助侧 1,049（其中 597 不在库、452 单侧）
-- 单侧的按成因进台账、不入库——与唯一装备详情页「两侧对不上就拒绝合并」、
-- `craft_affix_capacity.agreement` 是同一条：单侧来源不是冲突，
-- 但不能当成两侧确认过的事实。
--
-- 两道自证都在页面上：区块标题里写着条数（`被辅助 /116`，判据是
-- `链接数 // 2 == 声明数`，因为每条目有图标与文字两个链接），
-- 以及双向一致性本身。
--
-- 取哪个区块看**标签页 id**，不是位置：一页上可能有好几个「被辅助」，
-- 分别属于同一页上的不同技能（见 `parse_gem_support_links` 的 docstring）。
CREATE TABLE IF NOT EXISTS poedb_gem_support_pair (
    skill_gem_id    TEXT NOT NULL,
    support_gem_id  TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(skill_gem_id, support_gem_id),
    FOREIGN KEY(skill_gem_id) REFERENCES pob_skill_gem(gem_id) ON DELETE CASCADE,
    FOREIGN KEY(support_gem_id) REFERENCES pob_skill_gem(gem_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gem_support_by_support
    ON poedb_gem_support_pair(support_gem_id);

-- ── 技能石的固定效果（POEDB 详情页的「固定」表，第四十四轮）──
-- 与 `poedb_gem_stat` 的分界就是**有没有等级维度**：那张是逐级效果文本
-- （「造成 25 - 38 冰霜伤害」，每级一个值），这张是**与等级无关**的技能机制
-- （「有 25% 的几率对敌人造成冻结状态」「最多召唤 3 个赦罪哨卫」）。
-- 所以这里没有配套的 `_level` 表，也不该有。
--
-- 2026-09-01 实测：820 颗里 704 颗有中文固定效果、共 **2187** 条；
-- 21 颗页面上没有这张表（那是「这个技能没有固定机制」，**不是未采集**，
-- 所以卡片上不写「未采集」——写了就是假话）；95 颗只有内部字段（全是光环）。
--
-- **不存 `Key: value` 行**（实测 444 条，键只有 `Mana` 与 `CooldownTime`）：
-- 把内部字段当中文答案输出就是拿调试字段冒充游戏文本（怪物词缀那一轮的禁忌），
-- 而且这两项 PoB 侧早就有，存进来就是第二份会分叉的真源。
-- 它们改作**对账口径**——这一维本来只有 POEDB 一个来源，见 `reconcile_fixed`。
--
-- 取表要看**选中的标签页**，不能取第一张：实测 280 张页有两张「固定」表，
-- 其中 98 张两张内容不一样（静滞之天雷之珠 2.8 米 vs 1.8 米）。
-- 取第一张等于在两个互相矛盾的说法里无依据地挑一个。
--
-- 文本里的换行是上游写成的**字面反斜杠加 n**（实测 126 条），入库前换成空格
-- 且**不拆行**：它有时夹在句子中间，拆开会得到两个都读不通的碎片。
--
-- 外键与另外两张 POEDB 宝石表一样级联，重导宝石快照后要重跑 `import_gem_stats.py`。
CREATE TABLE IF NOT EXISTS poedb_gem_fixed_stat (
    gem_id          TEXT NOT NULL,
    -- 页面顺序。固定效果之间有语义顺序（技能本体在前、限制条件在后），
    -- 乱序展示会让用户以为是另一颗技能。
    stat_index      INTEGER NOT NULL,
    text_zh         TEXT NOT NULL CHECK(text_zh <> ''),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(gem_id, stat_index),
    FOREIGN KEY(gem_id) REFERENCES pob_skill_gem(gem_id) ON DELETE CASCADE
);

-- ── 技能石的获取途径（POEDB 详情页的「使命」表，第四十三轮）──
-- 从第十二轮起「获取途径」一直写在宝石域的未采集里，`kb/coverage.py` 的注释还写着
-- 「任务奖励与掉落不在任何已缓存页上」。**那句是错的**：第三十二轮为取效果文本
-- 抓的 820 张详情页里就有这张表（章节 ｜ 使命 ｜ NPC ｜ 角色），
-- 而 `poedb_gem_page` 自己的 docstring 当时就写着页面上还有「任务奖励」表。
-- 与万神殿、Boss、宝石效果文本是同一个错误的第四次：**停在一页上就下「缺来源」的结论**。
--
-- 2026-09-01 实测：820 颗里 668 颗有表、共 1265 行、零解析错误。
--
-- **一行一条途径，`act` 是这一行的事实、不是「使命」的属性。** 实测有 2 行
-- （熔岩护盾 / 黑色入侵者、斩首（辅）/ 永恒梦魇）的章节与同名使命的其余行不同，
-- 两条都是商人行。所以**不做「使命 → 章节」归一**——那等于替上游改数据。
-- （最初假设它 1:1 是**建模者的前提**，页面从没这么说过。）
--
-- **没有「掉落来源」列**：另外 152 颗（瓦尔 52、觉醒辅助、契约类）页面上根本没有
-- 这张表，它们靠掉落，而掉落来源本轮明确未采集。加一列当前管线永远填不上的空列，
-- 正是 `pob_skill_level` 表注释警告过的坑。
--
-- 外键与上面两张表同样级联，重导宝石快照后必须重跑 `scripts/import_gem_quests.py`。
CREATE TABLE IF NOT EXISTS poedb_gem_quest (
    gem_id          TEXT NOT NULL,
    act             INTEGER NOT NULL CHECK(act > 0),
    quest_zh        TEXT NOT NULL CHECK(quest_zh <> ''),
    quest_slug      TEXT NOT NULL,
    -- quest_reward（任务奖励）/ vendor（任务商人）/ box（绝望岩滩的起始箱）。
    -- 取自 NPC 列图标的 title 而不是单元格文本：起始箱那 21 行单元格是**空的**，
    -- 按文本判会把七个职业的开局技能静默丢掉。
    source_kind     TEXT NOT NULL CHECK(source_kind IN ('quest_reward','vendor','box')),
    -- 商人名。**没有 NPC 时存空串而不是 NULL**：SQLite 允许非 rowid 表的复合主键里
    -- 出现重复的 NULL（实测：`PRIMARY KEY(a,b)` 下 `('x',NULL)` 能插两次），
    -- 用 NULL 会让这个主键在任务奖励那 576 行上**完全不起作用**——
    -- 一个看起来在约束、实际没约束的约束，与那个「从来没生效过」的序号列函数同一类坑。
    npc_zh          TEXT NOT NULL DEFAULT '',
    -- 展开去重后的职业 JSON 数组（`全职业` → 七个职业）。
    classes_json    TEXT NOT NULL CHECK(json_valid(classes_json)),
    -- 角色列原文，`全职业` 原样留着：用户就是拿这个词问的。
    classes_raw     TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    -- 同一颗宝石同一个使命可能既是任务奖励又能向商人买（实测很常见），
    -- 所以主键必须带上 source_kind 与 npc。
    --
    -- 页面同一个键下会出现**两行**（1265 行归并成 1261 行，实测 4 处）：
    -- 三处是同一批职业换了个顺序（元素要害 / 衰弱 / 时空锁链，正是上游漏了图标
    -- 又重列一遍的那几行），一处（元素扩散(辅)）两行职业**不相交**。
    -- 所以归并方式是**按职业取并集**而不是丢掉一行——丢掉就会少掉圣堂武僧。
    -- `act` 不进主键：实测该键下章节恒唯一（章节只在**不同的**使命行之间才会不同）。
    PRIMARY KEY(gem_id, quest_slug, source_kind, npc_zh),
    FOREIGN KEY(gem_id) REFERENCES pob_skill_gem(gem_id) ON DELETE CASCADE
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

-- ── 精华物品本身（POEDB `/cn/Essence` 的 `精华` 标签页，第五十九轮）──
-- **精华赋予的词缀早就在 `mod_affix` 里**（5985 条 `category='essence'`，带
-- `name_zh` × `item_class_zh` × `text_zh`，并且已经渲染进词缀组卡）。这两张表补的是
-- `essence` 域 `partial_note` 里写着的另一半：**精华物品本身**的 tier / 堆叠 / 等级上限 /
-- 基础效果。动手前先查了库，否则这里会是一张与 `mod_affix` 逐条重复的 2310 行表。
--
-- 实测出来的机制分界在 tier 4 与 5 之间：tier 0–4 的 **41** 件只能升级白装且限等级
-- （15/35/45/60/75），tier 5–8 的 **64** 件无等级上限、还能**重置稀有物品的属性**。
-- 这条差异写在效果原文里，`level_cap` 为 NULL 就是「没有上限」——**不填 0**，
-- 留空与 0 在事后是两件事。腐化专属的 5 件里 4 件是 tier 8，哀恸精华是那条 tier 0。
CREATE TABLE IF NOT EXISTS poedb_essence (
    -- GGG 元数据路径；97 条来自 `data-hover`，另外 8 条只给 tooltip 哈希，退化成
    -- `slug:<英文 slug>`（与通货、圣甲虫同一条退化规则）
    metadata_id     TEXT NOT NULL CHECK(metadata_id <> ''),
    name_zh         TEXT NOT NULL CHECK(name_zh <> ''),
    -- 由 `scripts/verify_essence.py` 从 PoB `Data/Essence.lua` 回填。**不由 slug 反推**：
    -- slug 只用来找到 PoB 条目，存进来的是 PoB 自己写的 name
    name_en         TEXT,
    slug            TEXT NOT NULL CHECK(slug <> ''),
    -- **不写 `BETWEEN 1 AND 8`**：那会把我的假设写进约束。实测页面上
    -- `哀恸精华` 的 `Essence Tier` 是 **0**，而它的四个同类（浮夸/错乱/极恐/谵妄）
    -- 都是 8、PoB 侧给 `Essence of Hysteria` 也是 8——**页面自己前后不一致**。
    -- 存页面的原值，分歧由 `scripts/verify_essence.py` 登记进 `source_conflict`。
    tier            INTEGER NOT NULL CHECK(tier >= 0),
    stack_max       INTEGER,
    -- tier 0–4 的 41 件有（15/35/45/60/75），tier 5 起为 NULL。NULL ≠ 0
    level_cap       INTEGER,
    base_effect     TEXT NOT NULL CHECK(base_effect <> ''),
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    content_hash    TEXT,
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY(metadata_id),
    UNIQUE(slug)
);
CREATE INDEX IF NOT EXISTS idx_essence_name ON poedb_essence(name_zh);
CREATE INDEX IF NOT EXISTS idx_essence_tier ON poedb_essence(tier);

-- 逐装备类别的保底词缀，**按页面原始分组存**（105 个精华共 632 行，不是 105×22=2310）。
-- 拆成一类一行会与 `mod_affix` 的 5985 条逐条重复；保留分组则是页面自己的表述，
-- 卡片照着渲染就是用户在 POEDB 上看到的样子。
-- `item_classes` 是逗号分隔的中文类别名，一行可能只有一个（`弓: 弓技能石等级+2`）。
CREATE TABLE IF NOT EXISTS poedb_essence_mod (
    metadata_id     TEXT NOT NULL,
    group_index     INTEGER NOT NULL,
    item_classes    TEXT NOT NULL CHECK(item_classes <> ''),
    text_zh         TEXT NOT NULL CHECK(text_zh <> ''),
    PRIMARY KEY(metadata_id, group_index),
    FOREIGN KEY(metadata_id) REFERENCES poedb_essence(metadata_id) ON DELETE CASCADE
);

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

-- ── 万神殿神力（POEDB `/cn/Soul_of_<英文名>` × PoB `Data/Pantheons.lua`）──
-- 本库**唯一一张双源合成表**，所以两侧的溯源都要留：POEDB 侧 url/content_hash/
-- fetched_at，PoB 侧 source_commit。少留一侧，就说不清某一列到底是谁说的。
--
-- **按「层」建行而不是按「神」**：12 位神祇、32 层神力，而用户问的粒度就是层
-- （「阿拉卡力之魂第三层要打谁」「格鲁丝克之魂在哪抓」）。按神建行会把 4 层塞进
-- 一个 JSON，任何按头目、按地图的查询都得先解析再过滤。
--
-- **mods_zh 与 mods_pob 不逐项对应，禁止 zip。** 同一层里 POEDB 正文的词缀顺序与
-- PoB 的顺序不一致（实测艾贝拉斯第二层两侧正好相反）；按数值配也不行——同一层可能
-- 两条值相同（月影女神第一层两条都是 1、幽格尔第一层两条都是 50），而且 PoB 的
-- value 是 **stat 原生单位**（图克哈玛第二层存 120 /分钟，页面显示 2%/秒）。
-- 所以两侧并列存放：中文用于展示与检索，PoB 侧用于英文检索与溯源。
-- 想「顺手」把它们配起来，就会得到带着引用的错文本。
--
-- 对账在 `stats` 这一维上做（`[[stat_id, value], ...]`，Attr 表与 PoB 口径一致，
-- 实测 41/41 全对）。对不上的整位神祇拒绝入库并登记 source_conflict。
CREATE TABLE IF NOT EXISTS pantheon_soul (
    god_key         TEXT NOT NULL,     -- PoB 表键 = POEDB Attr 表的 ID，如 Arakaali
    tier            INTEGER NOT NULL CHECK(tier BETWEEN 1 AND 4),  -- 1 是基础神力
    god_name_zh     TEXT NOT NULL CHECK(god_name_zh <> ''),
    god_name_en     TEXT NOT NULL CHECK(god_name_en <> ''),
    is_major        INTEGER NOT NULL CHECK(is_major IN (0,1)),
    -- 这一层的名字：第 1 层是神力名，2..4 层是要打的那个头目。
    name_zh         TEXT NOT NULL CHECK(name_zh <> ''),
    -- 第 1 层由任务奖励，没有抓获目标；2..4 层才有。空串表示「这一层没有」。
    boss_zh         TEXT NOT NULL DEFAULT '',
    map_zh          TEXT NOT NULL DEFAULT '',   -- 抓获该头目的地图
    act_zh          TEXT NOT NULL DEFAULT '',   -- 第 1 层：「第 7 章」
    quest_zh        TEXT NOT NULL DEFAULT '',   -- 第 1 层：给予神力的任务
    mods_zh         TEXT NOT NULL CHECK(json_valid(mods_zh)),   -- JSON [中文词缀原文]
    mods_pob        TEXT NOT NULL CHECK(json_valid(mods_pob)),  -- JSON [{stat_id,value,line_en}]
    stats           TEXT NOT NULL CHECK(json_valid(stats)),     -- JSON [[stat_id, value]]
    source          TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    url             TEXT NOT NULL CHECK(url <> ''),
    content_hash    TEXT NOT NULL CHECK(content_hash <> ''),
    source_commit   TEXT NOT NULL CHECK(length(source_commit) = 40),
    fetched_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (god_key, tier)
);
CREATE INDEX IF NOT EXISTS idx_pantheon_zh ON pantheon_soul(god_name_zh);
CREATE INDEX IF NOT EXISTS idx_pantheon_boss ON pantheon_soul(boss_zh);

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

-- ── 实时经济快照（poe.watch）──
-- 价格数据不进入知识卡片：它有独立 TTL，且每次回答必须携带抓取时间与陈旧标记。
-- payload 保留上游原始 JSON，避免上游增加字段时必须先迁表；content_hash 用于观测
-- 快照是否真的变化。每个联盟/类别只保留最新一份，数据库仍是可整体备份的单文件。
CREATE TABLE IF NOT EXISTS economy_snapshot (
    source       TEXT NOT NULL,
    league       TEXT NOT NULL,
    category     TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    payload      TEXT NOT NULL CHECK(json_valid(payload)),
    content_hash TEXT NOT NULL CHECK(content_hash <> ''),
    PRIMARY KEY(source, league, category)
);
CREATE INDEX IF NOT EXISTS idx_economy_snapshot_fetched
    ON economy_snapshot(fetched_at);

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
    -- 条目类型，见 GAP_KINDS。**只有 COLLECTION_GAP_KINDS 那三类构成采集优先级**：
    -- binding_block 是闸门行为的观测量、out_of_scope 永远不会被采集，
    -- 混进来都会让频次排序失真。
    kind         TEXT,
    -- **流量来源**，见 GAP_SOURCES。评测把同一批问题反复跑，不区分的话
    -- hit_count 量的是「评测跑了几次」而不是「用户问了几次」——
    -- 第三十轮实测：台账 93% 是评测流量，而采集优先级前两名都是本该拒答的黄金用例。
    source       TEXT,
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
        _migrate_gap_verify_source(conn)
        _migrate_base_item_all_categories(conn)
        _migrate_conflict_cause(conn)
        _migrate_reservation_columns(conn)
        _migrate_gap_source(conn)
        conn.commit()
    except sqlite3.Error as exc:
        # 迁移失败不能让启动失败——但必须告警，否则台账会继续悄悄积累重复
        logger.warning("缺口台账迁移未完成：%s", exc)


#: 台账条目的来源。分开是因为对应的待办完全不同：
#:   retrieval_gap  检索判定依据不足        → 该去采集，或该改检索
#:   tool_gap       结构化工具自报缺据      → 该去补那份结构化数据
#:   binding_block  出口闸门拦下一句无据断言 → 该看是漏出还是错误拒答
#:   out_of_scope   永久不在服务范围（POE2）→ **没有待办**
#: 只有前两类构成「采集优先级」。
#:
#: `out_of_scope` 是第三十轮加的：POE2 越界原先记成 `retrieval_gap`，于是
#: 「流放之路2 的升华机制是什么」以 20 次命中**坐在采集优先级第一位**——
#: 而那个问题永远不会被采集。台账是用来定优先级的，头部塞着一条永不执行的待办，
#: 整张表就失去了作用。
#: `ingest_gap` 是导入脚本写的（「这 51 条 POEDB 有而 PoB 没有」这类），
#: 第三十轮才补进这份清单——它在用、却既没被声明也没在 `/admin/stats` 里露出来，
#: 而它恰恰是**最可执行**的一类采集待办。
#: `coverage_note` 是第五十七轮加的，起因与 `out_of_scope` 一模一样，
#: 只是这次是**自己**把信号搞脏的：第 43/45/54 轮的导入脚本用 `ingest_gap` 记下了
#: 「上游那一页本来就没有这一段」这类**覆盖率度量**——
#:
#:     1078x [no_acquisition_block] unique_item 有 1078 项的详情页上没有 Acquisition
#:      597x [one_sided] 只在「可辅助」侧：对方 slug 不在库
#:      152x [no_quest_table] 152 颗宝石的详情页上没有使命表
#:
#: 它们的 `missing_point` 里就写着「不是漏抽，是页面本来就没有这一段」，
#: 却因为 `kind='ingest_gap'` 全部坐进采集优先级的头部，把真待办挤到第 9 位往后。
#: 这正是上面那句话说的：**头部塞着永不执行的条目，整张表就失去了作用。**
#: 判据：**「去补它需要新来源」的是覆盖率，不是待办**。
GAP_KINDS = (
    "retrieval_gap", "tool_gap", "ingest_gap",
    "binding_block", "binding_block_legacy", "out_of_scope", "coverage_note",
)

#: 构成「采集优先级」的三类。其余是观测量、覆盖率度量，或永久不在范围内。
COLLECTION_GAP_KINDS = ("retrieval_gap", "tool_gap", "ingest_gap")

#: 台账条目的**流量来源**。评测会把同一批问题反复跑，命中次数因此变成
#: 「评测跑了几次」而不是「用户问了几次」——第三十轮实测：台账前两名
#: （20 次、14 次）全是黄金问答集里的用例，而它们本来就该拒答。
GAP_SOURCES = ("chat", "eval", "import", "verify")


def record_ingest_gaps(
    conn: sqlite3.Connection,
    *,
    question: str,
    category: str,
    points: Sequence[tuple[str, int]],
    kind: str = "ingest_gap",
    source: str = "import",
) -> int:
    """登记一次采集/核对发现的未入库项。

    `source` 默认 `import`（采集脚本）。**核对脚本要显式传 `verify`**——
    第六十四轮实测：`scripts/verify_against_detail.py` 自己写的那份 UPSERT
    虽然 INSERT 里带了 `source='verify'`，但 `ON CONFLICT` 子句**没更新 source**，
    于是第三十一轮之前建的行永远停在 `chat` 上，每次重跑只给它们的 `hit_count` +1。
    实测后果：台账里 `source='verify'` **一行都没有**，而「真实用户缺口」
    排头的 11 条全是这个脚本的输出，且它们在第三十四轮就已经被修掉了。

    `kind` 默认 `ingest_gap`（**是采集待办**）。上游那一页本来就没有这一段时，
    调用方要显式传 `kind='coverage_note'`——那是覆盖率度量、不是待办，
    混进优先级会把真待办挤下去（理由见 `GAP_KINDS` 上那段）。
    **按 `(question, kind, source)` 整组替换**，所以同一个 question 下
    两种 kind 各写各的、互不覆盖。

    四个导入/采集脚本原先各写一份同样的 UPSERT，三个缺陷也就各犯了一遍
    （第三十一轮实测，起因是照着台账排序去干活时发现头部那条已经过期）：

    1. **旧快照不会退场**。`missing_point` 里带着条数，条数一变就是新的一行，
       上一行仍以更高的 `hit_count` 留在优先级头部。实测：技能石那条写着
       「11 颗未能桥接」而当时实际只剩 1 颗，它还是第一名。所以这里**整组替换**
       同一 question 下 `source='import'` 的行，并把 `first_seen` 承接过来
       ——「这个缺口最早什么时候记下的」是审计信息，不能因为重写而丢。
    2. **`hit_count` 量错了东西**。它原先每跑一次导入 +1，也就是量「脚本跑了几次」。
       采集类缺口的量级是**缺多少条**，所以这一类直接把条数写进去：优先级按它排序
       才是「先补缺得最多的」。用户问了几次那层含义只对 `chat`/`eval` 成立。
    3. **`source` 从来没人写**。四处都留空，于是 `COALESCE(source,'chat')`
       把脚本自己的写入算成用户流量——第三十轮修掉评测流量时漏了这一半。
    """
    previous = {
        str(row["missing_point"]): str(row["first_seen"])
        for row in conn.execute(
            "SELECT missing_point, first_seen FROM knowledge_gap "
            "WHERE question=? AND kind=? AND source=?",
            (question, kind, source),
        )
    }
    oldest = min(previous.values(), default=None)
    conn.execute(
        "DELETE FROM knowledge_gap WHERE question=? AND kind=? AND source=?",
        (question, kind, source),
    )
    for missing_point, magnitude in points:
        text = missing_point[:500]
        conn.execute(
            "INSERT INTO knowledge_gap(question, missing_point, guessed_category, kind, "
            "source, hit_count, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,COALESCE(?,datetime('now')),datetime('now'))",
            (question, text, category, kind, source,
             max(int(magnitude), 1), previous.get(text, oldest)),
        )
    return len(points)


def _migrate_gap_source(conn: sqlite3.Connection) -> None:
    """给 `knowledge_gap` 补 `source` 列，并把两类噪声条目就地重新归类（第三十轮）。

    为什么必须做：需求 2.12 说「按频次排序即采集优先级」，而实测台账前两名是

        20 次  流放之路2 的升华机制是什么          → 永久越界，没有待办
        14 次  请你猜一下下个赛季会加什么新机制    → 本来就该拒答

    两条都来自黄金问答集（评测每跑一次就加一次），而且第一条还被归到了
    `not_recalled/ascendancy`——照着它去补升华数据是纯浪费。所以：

    1. 问题里带 POE2 标记的行改判 `kind='out_of_scope'`（那不是采集待办）；
    2. 问题出现在黄金问答集里的行标 `source='eval'`，采集优先级里排除；
    3. 其余保持 `source='chat'`。

    **只重新归类、不删行**：台账是审计记录，删掉就无法回溯当初为什么排错了优先级。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_gap)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE knowledge_gap ADD COLUMN source TEXT")
        logger.info("缺口台账迁移：已补 source 列")
    conn.execute("UPDATE knowledge_gap SET source='chat' WHERE source IS NULL")

    # 1) POE2 越界改判。判据与 `orchestrator.loop.poe2_marker` 同一套标记，
    #    在这里重新实现会两处不一致，所以直接引它。
    try:
        from orchestrator.loop import poe2_marker  # noqa: PLC0415  避免模块级循环依赖

        rows = conn.execute(
            "SELECT gap_id, question FROM knowledge_gap "
            "WHERE COALESCE(kind,'retrieval_gap') <> 'out_of_scope'"
        ).fetchall()
        moved = [(r[0],) for r in rows if poe2_marker(str(r[1]))]
        if moved:
            conn.executemany(
                "UPDATE knowledge_gap SET kind='out_of_scope', "
                "guessed_category='out_of_scope/poe2' WHERE gap_id=?",
                moved,
            )
            logger.info("缺口台账迁移：%d 条 POE2 越界条目移出采集优先级", len(moved))
    except Exception as exc:  # noqa: BLE001  迁移失败不该让启动失败
        logger.warning("缺口台账 POE2 改判未完成：%s", exc)

    # 2) 黄金问答集里的问题标成评测流量
    try:
        from tests.golden.loader import load_cases  # noqa: PLC0415

        questions = [(c.query,) for c in load_cases()]
        if questions:
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS _eval_q(q TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM _eval_q")
            conn.executemany("INSERT OR IGNORE INTO _eval_q(q) VALUES (?)", questions)
            marked = conn.execute(
                "UPDATE knowledge_gap SET source='eval' "
                "WHERE question IN (SELECT q FROM _eval_q) AND source <> 'eval'"
            ).rowcount
            if marked:
                logger.info("缺口台账迁移：%d 条评测流量已标记（不计入采集优先级）", marked)
    except Exception as exc:  # noqa: BLE001  测试资产不可用时跳过即可
        logger.debug("黄金问答集不可用，跳过评测流量标记：%s", exc)

    # 3) 导入脚本写的行归到 `source='import'`（第三十一轮）。
    #    原先四处都留空，`COALESCE(source,'chat')` 于是把脚本自己的写入算成用户流量
    #    ——第三十轮修评测流量时漏掉的正是这一半。**只改归属不改 hit_count**：
    #    那些行的命中次数是「导入跑了几次」，含义得由各自的导入脚本下次重写时纠正
    #    （见 `record_ingest_gaps`），在迁移里猜条数只会造出第二个失真的数。
    #
    #    **`verify` 要排除掉**（第六十四轮）。这里原本的判据是
    #    「`ingest_gap` 只有采集/导入脚本会写，所以这个判据是充分的」——
    #    那句话在核对脚本也开始写 `ingest_gap` 之后就**不成立了**，
    #    而这条 UPDATE 每次 `init_db` 都跑，于是它会把刚改对的 `verify`
    #    一路改回 `import`（实测：迁移把 11 条改成 verify，这一句立刻又抹掉）。
    #    判据换成显式白名单：只认领「没人认领的」，不动别的脚本自己声明的来源。
    marked = conn.execute(
        "UPDATE knowledge_gap SET source='import' "
        "WHERE kind='ingest_gap' AND COALESCE(source,'') NOT IN ('import','verify')"
    ).rowcount
    if marked:
        logger.info("缺口台账迁移：%d 条导入脚本写的条目归到 source='import'", marked)

    # 4) **覆盖率度量移出采集优先级**（第五十七轮）。第 43/45/54 轮的导入脚本把
    #    「上游那一页本来就没有这一段」记成了 `ingest_gap`，于是台账头部八条全是
    #    它们（合计四千多的 hit_count），真待办被挤到第 9 位往后——正是第三十轮
    #    修 POE2 越界时那句话说的：头部塞着永不执行的条目，整张表就失去了作用。
    #
    #    判据是 `missing_point` 里的成因标记，不是靠猜：这三个标记的语义都明确是
    #    「去补它需要新来源」，而不是「我们漏抽了」。
    #    **只重新归类、不删行**，与第三十轮同一条：台账是审计记录。
    moved = conn.execute(
        "UPDATE knowledge_gap SET kind='coverage_note' "
        "WHERE kind='ingest_gap' AND ("
        "  missing_point LIKE '%[no_acquisition_block]%'"
        "  OR missing_point LIKE '%[no_quest_table]%'"
        "  OR missing_point LIKE '%[one_sided]%')"
    ).rowcount
    if moved:
        logger.info("缺口台账迁移：%d 条覆盖率度量移出采集优先级", moved)
    conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gap_source ON knowledge_gap(source)")


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


def _migrate_gap_verify_source(conn: sqlite3.Connection) -> None:
    """把核对脚本写的行从 `chat` 改判成 `verify`（第六十四轮）。

    这是「测量工具本身失真」的第四次，而且是第三十轮/第三十一轮那两次的**残留**：

    `scripts/verify_against_detail.py` 的 INSERT 里第三十一轮就带上了
    `source='verify'`，但它那份 UPSERT 的 `ON CONFLICT` 子句**没有更新 source**。
    于是第三十一轮之前建的行永远停在 `chat` 上，每次重跑只给它们 `hit_count` +1。
    实测后果两条都很具体：

      * 台账里 `source='verify'` **一行都没有**；
      * 「排除评测流量后的真实用户缺口」头部 11 条全是这个脚本的输出，
        而它们在第三十四轮详情页合并时就已经被修掉了
        （实测「不可改变」现在就在中规中矩的卡片上）。

    判据是这个脚本写下的**逐字格式**，不做猜测：
    `guessed_category='unique_item/detail_only_mod'`，或者
    missing_point 以「详情页有而列表页没有的词条：」开头。

    **只改归属，不动 `hit_count`、不删行**——台账是审计记录（与第三十一轮同一条）。
    行本身的退场交给脚本下次重跑：它现在**无条件**调 `record_ingest_gaps`，
    按 (question, kind, source) 整组替换，修好的自然清空。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_gap)")}
    if "source" not in cols:
        return
    moved = conn.execute(
        "UPDATE knowledge_gap SET source='verify', kind='ingest_gap' "
        "WHERE COALESCE(source,'chat')='chat' AND ("
        "  guessed_category='unique_item/detail_only_mod'"
        "  OR missing_point LIKE '详情页有而列表页没有的词条：%')"
    ).rowcount
    if moved:
        conn.commit()
        logger.info(
            "缺口台账迁移：%d 条核对脚本的发现已从 chat 改判为 verify/ingest_gap"
            "（先前被算成用户流量，把采集优先级带偏）", moved,
        )


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


def vector_channel_status(
    requested: bool,
    *,
    query_adapter_ready: bool = False,
) -> VectorStatus:
    """返回向量检索的**运行时有效状态**，而不是只复述配置值。

    ``retrieval.vector.enabled`` 只是运维侧的启用请求。扩展不可加载、数据库尚未
    初始化、向量表没有建立或查询适配器尚未接入时，请求值不能被检索/API 当成
    “向量通道已启用”。这里集中收口判定，避免各入口分别读取配置后产生不同结论。
    """
    if not requested:
        return VectorStatus(False, "配置关闭")

    extension = vector_status()
    if not extension.available:
        return VectorStatus(False, extension.reason)
    if _conn is None:
        return VectorStatus(False, "数据库未初始化")

    try:
        row = _conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunk_vec'"
        ).fetchone()
    except sqlite3.Error as exc:
        return VectorStatus(False, f"检查向量表失败: {type(exc).__name__}: {exc}")
    if row is None:
        return VectorStatus(
            False,
            "向量表未建立（retrieval.vector.dim 必须为正数）",
        )
    if not query_adapter_ready:
        return VectorStatus(False, "向量查询适配器尚未接入")
    return VectorStatus(True, extension.reason)


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
