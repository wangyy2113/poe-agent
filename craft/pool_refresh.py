"""胸甲普通词缀池的七页刷新、预检与事务化发布。

刷新先抓取并解析全部页面，再碰生产库。任一页来源、行数账、正权重同侧池或
页面身份不闭合，整批拒绝。当前 POEDB 已把 hover 内部 ID 改为不透明哈希；
因此仅发布概率模型需要的 normal 行，保留其它来源类别和可唯一对账的旧内部 ID。
发布时重查旧页摘要，在一个 SQLite 事务内替换 normal 行、重建索引并写入审计。
CLI 在发布前创建一致性备份。

这只更新 POEDB 词缀页，不改变 PoB 底材版本，也不把普通池权重解释成通货概率。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from core.db import SCHEMA
from craft.pool import (
    POOL_SNAPSHOT_MAX_AGE,
    NORMAL_POOL_SNAPSHOT_MODE,
    affix_page_digest,
    verify_affix_page_snapshot,
)
from kb.fetcher import FetchResult, Fetcher
from kb.pipeline import MOD_LADDER_PREFIX, _upsert_mods
from kb.render_mods import build_mod_chunks
from kb.sources import poedb_mods
from kb.sources.pob_bases import ARMOUR_TAG_TO_PAGE
from retrieval import fts as fts_mod
from retrieval import vocab as vocab_mod

POOL_PAGES = tuple(ARMOUR_TAG_TO_PAGE.values())
MAX_CLOCK_SKEW = timedelta(minutes=5)


class PoolRefreshError(RuntimeError):
    """整批预检或发布未闭合；生产词缀页未被部分发布。"""


@dataclass(frozen=True)
class PreparedPage:
    slug: str
    result: FetchResult
    records: tuple[poedb_mods.ModRecord, ...]
    stats: poedb_mods.ModExtractStats
    positive_prefixes: int
    positive_suffixes: int


@dataclass(frozen=True)
class PlannedPage:
    slug: str
    records: tuple[poedb_mods.ModRecord, ...]
    row_count: int
    row_digest: str
    old_full_count: int
    old_full_digest: str
    audit: dict[str, object]


@dataclass(frozen=True)
class PageChange:
    slug: str
    old_rows: int
    new_rows: int
    old_digest: str
    new_digest: str
    added: int
    removed: int
    fetched_at: str
    source_hash: str
    positive_prefixes: int
    positive_suffixes: int
    preserved_legacy_ids: int
    new_opaque_ids: int

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()


@dataclass(frozen=True)
class PoolRefreshPlan:
    pages: tuple[PlannedPage, ...]
    changes: tuple[PageChange, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": True,
            "page_count": len(self.pages),
            "old_rows": sum(page.old_rows for page in self.changes),
            "new_rows": sum(page.new_rows for page in self.changes),
            "added": sum(page.added for page in self.changes),
            "removed": sum(page.removed for page in self.changes),
            "preserved_legacy_ids": sum(page.preserved_legacy_ids for page in self.changes),
            "new_opaque_ids": sum(page.new_opaque_ids for page in self.changes),
            "changes": [page.to_dict() for page in self.changes],
        }


def _snapshot_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PoolRefreshError("抓取时间格式无效") from exc
    if parsed.tzinfo is None:
        raise PoolRefreshError("抓取时间缺少时区")
    return parsed.astimezone(timezone.utc)


def _validate_page(slug: str, result: FetchResult, records: list[poedb_mods.ModRecord],
                   stats: poedb_mods.ModExtractStats, now: datetime) -> None:
    url = f"https://poedb.tw/cn/{slug}"
    if not result.ok or result.url != url or result.final_url != url:
        raise PoolRefreshError(f"{slug} HTTP 响应或最终 URL 与所请求页面不符")
    actual_hash = hashlib.sha256(result.text.encode("utf-8")).hexdigest()
    if result.content_hash != actual_hash:
        raise PoolRefreshError(f"{slug} 响应正文与抓取哈希不符")
    age = now - _snapshot_time(result.fetched_at)
    if age < -MAX_CLOCK_SKEW or age > POOL_SNAPSHOT_MAX_AGE:
        raise PoolRefreshError(f"{slug} 来源快照已过期或时间异常")
    if (
        stats.failed or stats.dedup_conflicts or stats.textless
        or stats.succeeded != len(records)
        or stats.total != stats.succeeded + stats.deduped
    ):
        raise PoolRefreshError(f"{slug} 来源行账不闭合，不能证明概率分母完整")
    if len({record.uid for record in records}) != len(records):
        raise PoolRefreshError(f"{slug} 解析后存在重复 UID")
    for side in (1, 2):
        if not any(
            record.category == "normal" and record.gen_type == side
            and isinstance(record.weight, int) and record.weight > 0
            and record.correct_group and record.family_list
            for record in records
        ):
            raise PoolRefreshError(f"{slug} 缺少正权重 normal {'前缀' if side == 1 else '后缀'}池")


def prepare_refresh(fetcher: Fetcher, *, now: datetime | None = None) -> tuple[PreparedPage, ...]:
    """强制重新抓取七页并验证完整来源行账；不修改生产数据库。"""
    clock = now or datetime.now(timezone.utc)
    prepared: list[PreparedPage] = []
    for slug in POOL_PAGES:
        url = f"https://poedb.tw/cn/{slug}"
        try:
            result = fetcher.fetch(url, force=True)
            records, stats = poedb_mods.extract_mods(result.text, slug, url)
            _validate_page(slug, result, records, stats, clock)
        except PoolRefreshError:
            raise
        except Exception as exc:
            raise PoolRefreshError(f"{slug} 抓取、解析或来源预检失败：{exc}") from exc
        prepared.append(PreparedPage(
            slug=slug, result=result, records=tuple(records), stats=stats,
            positive_prefixes=sum(
                record.category == "normal" and record.gen_type == 1
                and record.weight > 0 for record in records
            ),
            positive_suffixes=sum(
                record.category == "normal" and record.gen_type == 2
                and record.weight > 0 for record in records
            ),
        ))
    return tuple(prepared)


def _legacy_key(record: poedb_mods.ModRecord) -> tuple[Any, ...]:
    """不依赖已变成哈希的 hover；这些字段必须共同唯一才沿用旧 ID。"""
    return (
        record.gen_type, record.correct_group, record.ilvl, record.tier,
        record.weight, record.name_zh, record.variant_id,
        tuple(record.family_list), tuple(record.tags),
        tuple(record.spawn_tags), tuple(record.fossil_tags),
    )


def _old_legacy_key(row: sqlite3.Row) -> tuple[Any, ...]:
    try:
        arrays = [json.loads(row[key]) for key in (
            "family_list", "tags", "spawn_tags", "fossil_tags"
        )]
        if any(
            not isinstance(items, list)
            or any(not isinstance(item, str) for item in items)
            for items in arrays
        ):
            raise ValueError("标签列不是字符串数组")
        return (
            *(row[key] for key in (
                "gen_type", "correct_group", "ilvl", "tier", "weight", "name_zh",
                "variant_id",
            )),
            *(tuple(items) for items in arrays),
        )
    except (TypeError, ValueError) as exc:
        raise PoolRefreshError("旧 normal 行的标签 JSON 无效，不能沿用内部 ID") from exc


def _reconcile_normal(
    old_rows: list[sqlite3.Row], new_rows: list[poedb_mods.ModRecord]
) -> tuple[list[poedb_mods.ModRecord], int]:
    """只在旧行与新行一对一时沿用旧内部 ID；新词缀保留来源哈希。"""
    old_by_key: dict[tuple[Any, ...], list[sqlite3.Row]] = {}
    new_by_key: dict[tuple[Any, ...], int] = {}
    for row in old_rows:
        old_by_key.setdefault(_old_legacy_key(row), []).append(row)
    for record in new_rows:
        key = _legacy_key(record)
        new_by_key[key] = new_by_key.get(key, 0) + 1

    reconciled: list[poedb_mods.ModRecord] = []
    preserved = 0
    for record in new_rows:
        key = _legacy_key(record)
        candidates = old_by_key.get(key, [])
        if len(candidates) == 1 and new_by_key[key] == 1:
            legacy_code = str(candidates[0]["mod_code"] or "")
            if legacy_code:
                record = replace(record, mod_code=legacy_code)
                preserved += 1
        elif candidates:
            raise PoolRefreshError(
                f"normal 词缀 {record.correct_group}/{record.ilvl} 的旧 ID 无法唯一对账"
            )
        reconciled.append(record)
    if len({record.uid for record in reconciled}) != len(reconciled):
        raise PoolRefreshError("normal 词缀沿用旧 ID 后发生 UID 冲突")
    return reconciled, preserved


def _replace_normal_page(
    conn: sqlite3.Connection, slug: str, records: list[poedb_mods.ModRecord]
) -> tuple[int, str]:
    """只替换 normal；同页的精华、影响、地渊等来源记录不受影响。"""
    conn.execute("SAVEPOINT normal_affix_page")
    try:
        conn.execute(
            "DELETE FROM mod_affix WHERE item_class_page=? AND category='normal'", (slug,)
        )
        written = _upsert_mods(conn, records)
        count, digest = affix_page_digest(conn, slug, normal_only=True)
        if written != count:
            raise PoolRefreshError(f"{slug} normal 整页替换行数不一致")
        conn.execute("RELEASE normal_affix_page")
        return count, digest
    except Exception:
        conn.execute("ROLLBACK TO normal_affix_page")
        conn.execute("RELEASE normal_affix_page")
        raise


def _normal_audit(page: PreparedPage, count: int, digest: str,
                  preserved: int) -> dict[str, object]:
    stats = page.stats
    return {
        "scope": "normal", "status": "complete", "rows": count, "digest": digest,
        "source_url": page.result.url,
        "source_final_url": page.result.final_url,
        "source_content_hash": page.result.content_hash,
        "source_fetched_at": page.result.fetched_at,
        "source_total_rows": stats.total,
        "source_succeeded_rows": stats.succeeded,
        "source_failed_rows": stats.failed,
        "source_deduped_rows": stats.deduped,
        "source_textless_rows": stats.textless,
        "source_dedup_conflicts": stats.dedup_conflicts,
        "source_normal_rows": stats.categories.get("normal", 0),
        "preserved_legacy_ids": preserved,
        "new_opaque_ids": count - preserved,
        "parse_complete": True,
        "explicit_no_mods": False,
    }


def plan_refresh(conn: sqlite3.Connection, pages: tuple[PreparedPage, ...]) -> PoolRefreshPlan:
    """对比 normal 行集并对账旧内部 ID；不触碰其它来源类别。"""
    if tuple(page.slug for page in pages) != POOL_PAGES:
        raise PoolRefreshError("必须按固定顺序提供全部七个胸甲词缀页")
    changes: list[PageChange] = []
    planned: list[PlannedPage] = []
    staging = sqlite3.connect(":memory:")
    staging.row_factory = sqlite3.Row
    try:
        staging.executescript(SCHEMA)
        for page in pages:
            old_full_count, old_full_digest = affix_page_digest(conn, page.slug)
            old_count, old_digest = affix_page_digest(
                conn, page.slug, normal_only=True
            )
            old_rows = conn.execute(
                "SELECT uid,mod_code,gen_type,correct_group,ilvl,tier,weight,name_zh,"
                "variant_id,family_list,tags,spawn_tags,fossil_tags "
                "FROM mod_affix WHERE item_class_page=? AND category='normal'",
                (page.slug,),
            ).fetchall()
            fresh_normal = [record for record in page.records if record.category == "normal"]
            reconciled, preserved = _reconcile_normal(old_rows, fresh_normal)
            new_count, new_digest = _replace_normal_page(staging, page.slug, reconciled)
            audit = _normal_audit(page, new_count, new_digest, preserved)
            planned.append(PlannedPage(
                slug=page.slug, records=tuple(reconciled), row_count=new_count,
                row_digest=new_digest, old_full_count=old_full_count,
                old_full_digest=old_full_digest, audit=audit,
            ))
            old_uids = {str(row["uid"]) for row in old_rows}
            new_uids = {record.uid for record in reconciled}
            changes.append(PageChange(
                slug=page.slug, old_rows=old_count, new_rows=new_count,
                old_digest=old_digest, new_digest=new_digest,
                added=len(new_uids - old_uids), removed=len(old_uids - new_uids),
                fetched_at=page.result.fetched_at, source_hash=page.result.content_hash,
                positive_prefixes=page.positive_prefixes,
                positive_suffixes=page.positive_suffixes,
                preserved_legacy_ids=preserved,
                new_opaque_ids=new_count - preserved,
            ))
        return PoolRefreshPlan(pages=tuple(planned), changes=tuple(changes))
    finally:
        staging.close()


def apply_refresh(conn: sqlite3.Connection, plan: PoolRefreshPlan) -> dict[str, Any]:
    """一个事务内发布七页、索引与审计；调用方须先创建一致性备份。"""
    if tuple(page.slug for page in plan.pages) != POOL_PAGES:
        raise PoolRefreshError("发布计划缺少固定七页")
    if conn.in_transaction:
        raise PoolRefreshError("发布连接已有未完成事务")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for page in plan.pages:
            if affix_page_digest(conn, page.slug) != (
                page.old_full_count, page.old_full_digest
            ):
                raise PoolRefreshError(f"{page.slug} 自预检后已改变，请重新预检")
        for page in plan.pages:
            count, digest = _replace_normal_page(conn, page.slug, list(page.records))
            if (count, digest) != (page.row_count, page.row_digest):
                raise PoolRefreshError(f"{page.slug} 发布后摘要与预检不一致")

        note = json.dumps({
            "snapshot_mode": NORMAL_POOL_SNAPSHOT_MODE,
            "refresh_scope": "body_armour_normal_seven_pages_v1",
            "pages": {page.slug: page.audit for page in plan.pages},
        }, ensure_ascii=False, sort_keys=True)
        conn.execute(
            "INSERT INTO ingest_run(source,category,started_at,finished_at,total,succeeded,"
            "failed,skipped,note) VALUES ('POEDB','mod_affix',datetime('now'),"
            "datetime('now'),7,7,0,0,?)", (note,),
        )
        for page in plan.pages:
            proof, reason = verify_affix_page_snapshot(conn, page.slug)
            if proof is None:
                raise PoolRefreshError(f"{page.slug} 发布后来源证明未通过：{reason}")

        vocab_mod.build_vocab(conn)
        chunks = build_mod_chunks(conn)
        fts_mod.clear_index(conn, entity_prefix=MOD_LADDER_PREFIX)
        indexed = fts_mod.index_chunks(conn, chunks)
        if indexed != len(chunks):
            raise PoolRefreshError("词缀卡片存在不可索引项，拒绝部分发布")
        conn.commit()
    except Exception:
        conn.rollback()
        # 词表是磁盘派生文件；回滚后恢复其与旧库的一致性。
        vocab_mod.build_vocab(conn)
        raise
    return {"applied": True, "indexed_mod_chunks": indexed, **plan.to_dict()}
