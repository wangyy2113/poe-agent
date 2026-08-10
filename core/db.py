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
        conn.commit()
    except sqlite3.Error as exc:
        # 迁移失败不能让启动失败——但必须告警，否则台账会继续悄悄积累重复
        logger.warning("缺口台账迁移未完成：%s", exc)


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
