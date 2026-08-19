"""实体精确匹配通道。

为什么需要它（需求 3.5）：专名查询（「猎首」「Headhunter」）在纯 BM25 或
向量检索里可能被大量含相同词根的条目稀释。别名表是纯 SQL 查表，最快也最确定，
命中即把该实体的规范卡片**强制注入**候选集，保证专名问题不会漏掉正确条目。

别名的性质：**检索匹配辅助，不是事实断言**。因此允许收录 slug 去下划线这类
派生形式（`Abberaths_Horn` → `Abberaths Horn`）——它用于匹配，不会被当作
物品的正式英文名输出。正式英文名只能来自详情页（见 Entity.name_en）。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterable

from entities.models import Entity
from retrieval.fts import Hit

logger = logging.getLogger(__name__)


def normalize(text: str) -> str:
    """别名归一化：小写、去空白与常见标点，便于宽松匹配。"""
    t = text.strip().lower()
    t = re.sub(r"[\s_\-'’,.:：、·]+", "", t)
    return t


def aliases_of(entity: Entity) -> list[tuple[str, str]]:
    """产出 [(别名, 类型)]。

    `attributes["name_zh_alt"]` 也算别名：同一个对象存在两种官方译名时（宝石的
    国服 / 繁中并列，如「魔卫复苏」与「僵尸复苏」），只收正名会让另一种问法查不到，
    而它并不是错名字。放在这里而不是各导入器自己插表——什么算别名只该有一处说法。
    """
    out: list[tuple[str, str]] = [(entity.name_zh, "name_zh")]
    for alt in entity.attributes.get("name_zh_alt") or []:
        if isinstance(alt, str) and alt.strip():
            out.append((alt, "name_zh_alt"))
    if entity.name_en:
        out.append((entity.name_en, "name_en"))
    if entity.slug:
        out.append((entity.slug, "slug"))
        spaced = entity.slug.replace("_", " ")
        if spaced != entity.slug:
            out.append((spaced, "slug"))
    return out


def index_aliases(conn: sqlite3.Connection, entities: list[Entity]) -> int:
    rows = []
    for e in entities:
        for alias, kind in aliases_of(e):
            if not alias.strip():
                continue
            rows.append((alias, normalize(alias), e.entity_id, kind))
    conn.executemany(
        "INSERT OR REPLACE INTO alias(alias, alias_norm, entity_id, kind) VALUES (?,?,?,?)",
        rows,
    )
    return len(rows)


def clear_aliases(conn: sqlite3.Connection, entity_prefix: str | None = None) -> None:
    """清空别名。按类别重建时须传 `entity_prefix`，避免误删其他类别的别名。"""
    if entity_prefix:
        conn.execute("DELETE FROM alias WHERE entity_id LIKE ?", (entity_prefix + "%",))
    else:
        conn.execute("DELETE FROM alias")


def lookup(conn: sqlite3.Connection, query: str) -> list[str]:
    """查别名表，返回命中的 entity_id。

    两级匹配：先整串精确匹配，再退化为「查询中包含某个别名」的子串匹配，
    以应对「猎首这件装备有什么词缀」这类带上下文的提问。
    """
    norm = normalize(query)
    if not norm:
        return []

    exact = [
        r["entity_id"]
        for r in conn.execute("SELECT entity_id FROM alias WHERE alias_norm = ?", (norm,))
    ]
    if exact:
        return list(dict.fromkeys(exact))

    # 子串匹配：只考虑长度 >= 2 的别名，避免单字噪声。
    # 用 SQL 的 instr 让数据库侧过滤，避免把整张别名表拉进内存。
    rows = conn.execute(
        "SELECT entity_id, alias_norm FROM alias "
        "WHERE length(alias_norm) >= 2 AND instr(?, alias_norm) > 0 "
        "ORDER BY length(alias_norm) DESC LIMIT 20",
        (norm,),
    ).fetchall()
    return list(dict.fromkeys(r["entity_id"] for r in rows))


#: 别名精确命中的通道名。命中它意味着用户**点名**了这个实体，
#: 是维度闸门的领域识别信号之一（见 `kb.coverage.dimension_gap`）。
CHANNEL = "alias"


def exact_entity_ids(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """从 ``(通道, 实体 id)`` 序列里挑出别名精确命中的实体 id，保序去重。

    判据只写在这一处：调用方有两种形态——请求链路上是工具返回的 citation 字典
    （`orchestrator/loop`），评测链路上是 `Hit` 对象（`scripts/eval_run`）。
    两边各写一份 `channel == "alias"` 的过滤，就会出现「评测量到的路径与线上不同」，
    而 `eval_run` 自己的注释正是为这件事写的。
    """
    return list(dict.fromkeys(eid for channel, eid in pairs if channel == CHANNEL and eid))


def hits_for_entities(conn: sqlite3.Connection, entity_ids: list[str]) -> list[Hit]:
    """把实体的规范卡片取成 Hit，score 固定为 1.0（精确命中）。"""
    if not entity_ids:
        return []
    placeholders = ",".join("?" * len(entity_ids))
    rows = conn.execute(
        f"SELECT chunk_id, entity_id, text, metadata FROM chunk "  # noqa: S608 占位符已参数化
        f"WHERE entity_id IN ({placeholders}) ORDER BY chunk_id",
        entity_ids,
    ).fetchall()

    hits: list[Hit] = []
    for r in rows:
        meta = json.loads(r["metadata"])
        hits.append(
            Hit(
                chunk_id=r["chunk_id"],
                entity_id=r["entity_id"],
                text=r["text"],
                metadata=meta,
                score=1.0,
                raw_score=0.0,
                channel="alias",
                source=meta.get("source", ""),
                source_priority=int(meta.get("source_priority", 99)),
            )
        )
    return hits
