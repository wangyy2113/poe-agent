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
