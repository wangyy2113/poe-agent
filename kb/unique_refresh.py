"""Stage the complete POEDB unique-item chain before changing the live index.

The old list importer commits before the detail importer runs. A failed detail
pass can therefore remove the extra ``不可改变`` lines already in the database.
This module reads the list, class supplements, English names and every detail
page first. A single transaction then publishes entities, cards and reviews.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from entities.models import Entity
from kb.fetcher import FetchResult, cache_path_for
from kb.pipeline import _entity_from_row, _entity_hash, _upsert_entity
from kb.render import to_chunks
from kb.sources import poedb_index, poedb_unique
from retrieval import alias as alias_mod
from retrieval import fts as fts_mod
from retrieval import vocab as vocab_mod
from scripts.import_unique_detail import merged_entity


MIN_MAIN_ITEMS = 1000
MIN_CLASS_ONLY_ITEMS = 50
MIN_ENGLISH_ITEMS = 1000
MAX_INVENTORY_DROP = 0.05
UNIQUE_PREFIX = "unique_item:"


class UniqueRefreshError(ValueError):
    """A source or database check failed; nothing should be published."""


class PageSource(Protocol):
    def fetch(self, url: str, *, force: bool = False) -> FetchResult: ...


class CachedPageSource:
    """Read an existing crawl snapshot without network or cache TTL effects."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    def fetch(self, url: str, *, force: bool = False) -> FetchResult:
        path = cache_path_for(self.cache_dir, url)
        if not path.is_file():
            raise UniqueRefreshError(f"缺少本地缓存页：{url}")
        body = path.read_text(encoding="utf-8")
        return FetchResult(
            url=url, text=body,
            content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            from_cache=True,
            fetched_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            final_url=url,
        )


@dataclass(frozen=True)
class Review:
    slug: str
    name: str
    reason: str
    source_url: str
    detail_url: str

    def as_dict(self) -> dict[str, str]:
        return {"slug": self.slug, "name": self.name, "reason": self.reason}


@dataclass(frozen=True)
class PageEvidence:
    role: str
    url: str
    final_url: str
    content_hash: str
    fetched_at: str
    from_cache: bool

    @classmethod
    def from_result(cls, role: str, result: FetchResult) -> PageEvidence:
        return cls(role, result.url, result.final_url or result.url,
                   result.content_hash, result.fetched_at, result.from_cache)


@dataclass(frozen=True)
class UniqueRefreshPlan:
    entities: tuple[Entity, ...]
    preserved_slugs: tuple[str, ...]
    reviews: tuple[Review, ...]
    old_fingerprint: str
    old_count: int
    discovered_count: int
    list_count: int
    class_only_count: int
    detail_only_count: int
    detail_pages: int
    detail_added_lines: int
    changed_count: int
    source_digest: str
    sources: tuple[PageEvidence, ...]
    source_pages: int
    from_cache: int
    oldest_source_at: str
    newest_source_at: str
    list_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "attention" if self.reviews else "ready",
            "old_entities": self.old_count,
            "discovered_entities": self.discovered_count,
            "published_entities": len(self.entities),
            "preserved_entities": len(self.preserved_slugs),
            "list_entities": self.list_count,
            "class_only_entities": self.class_only_count,
            "detail_only_entities": self.detail_only_count,
            "detail_pages_checked": self.detail_pages,
            "detail_added_lines": self.detail_added_lines,
            "changed_entities": self.changed_count,
            "review_count": len(self.reviews),
            "reviews": [r.as_dict() for r in self.reviews[:20]],
            "source_digest": self.source_digest,
            "source_pages": self.source_pages,
            "from_cache": self.from_cache,
            "oldest_source_at": self.oldest_source_at,
            "newest_source_at": self.newest_source_at,
            "list_hash": self.list_hash,
        }


def entity_fingerprint(conn: sqlite3.Connection) -> str:
    """Detect a concurrent unique write between staging and publication."""
    rows = conn.execute(
        "SELECT entity_id,name_zh,name_en,base_type,item_class,attributes,"
        "content_hash,url,version,fetched_at,updated_at FROM entity "
        "WHERE entity_type='unique_item' "
        "ORDER BY entity_id"
    )
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def published_fingerprint(conn: sqlite3.Connection) -> str:
    """Bind entities, cards, FTS postings and aliases into one release proof."""
    digest = hashlib.sha256()
    digest.update(entity_fingerprint(conn).encode())
    for label, sql in (
        ("chunk", "SELECT chunk_id,entity_id,text,metadata FROM chunk "
         "WHERE entity_id LIKE 'unique_item:%' ORDER BY chunk_id"),
        ("fts", "SELECT chunk_id,entity_id,tokens,keywords FROM chunk_fts "
         "WHERE entity_id LIKE 'unique_item:%' ORDER BY chunk_id"),
        ("alias", "SELECT alias,alias_norm,entity_id,kind FROM alias "
         "WHERE entity_id LIKE 'unique_item:%' ORDER BY entity_id,alias_norm"),
    ):
        digest.update(label.encode())
        for row in conn.execute(sql):
            digest.update(json.dumps(tuple(row), ensure_ascii=False).encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def source_ledger_digest(conn: sqlite3.Connection, run_id: int) -> str:
    digest = hashlib.sha256()
    for row in conn.execute(
        "SELECT role,url,final_url,content_hash,fetched_at,from_cache "
        "FROM unique_refresh_page WHERE run_id=? ORDER BY url",
        (run_id,),
    ):
        digest.update(json.dumps(tuple(row), ensure_ascii=False).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _read(source: PageSource, url: str, *, force: bool, role: str,
          evidence: dict[str, PageEvidence]) -> FetchResult:
    result = source.fetch(url, force=force)
    if not result.ok:
        raise UniqueRefreshError(f"页面抓取失败：{url} HTTP {result.status_code}")
    requested = urlsplit(url)
    actual = urlsplit(result.final_url or result.url)
    if (actual.scheme, actual.hostname, actual.path) != (
        requested.scheme, requested.hostname, requested.path
    ):
        raise UniqueRefreshError(f"页面重定向到不同身份：{url} → {result.final_url}")
    evidence[url] = PageEvidence.from_result(role, result)
    return result


def _existing(conn: sqlite3.Connection) -> dict[str, Entity]:
    previous = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return {
            row["slug"]: _entity_from_row(row)
            for row in conn.execute(
                "SELECT * FROM entity WHERE entity_type='unique_item' ORDER BY slug"
            )
        }
    finally:
        conn.row_factory = previous


def _combine(primary: Entity, class_item: Entity | None,
             old: Entity | None, english: tuple[str, str] | None,
             detail: poedb_unique.DetailPage | None) -> Entity:
    item = primary.model_copy(deep=True)
    if old:
        item.attributes = {**old.attributes, **item.attributes}
    if class_item:
        if class_item.item_class:
            item.item_class = class_item.item_class
        item.attributes.update(class_item.attributes)
    item.name_en = (english[0] if english else None) or (detail.name_en if detail else None) or (
        old.name_en if old else None
    )
    base_en = (english[1] if english else None) or (old.attributes.get("base_type_en") if old else None)
    if base_en:
        item.attributes["base_type_en"] = base_en
    return item


def _review(item: Entity, reason: str) -> Review:
    return Review(item.slug, item.name_zh, reason, poedb_unique.LIST_URL, item.ref.url)


def _content(entity: Entity) -> str:
    """Include all indexed fields; the legacy content_hash omits attributes/ranges."""
    payload = entity.model_dump(mode="json")
    payload.pop("content_hash", None)
    payload.pop("league", None)
    ref = payload.get("ref") or {}
    ref.pop("fetched_at", None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _class_facts(entity: Entity) -> tuple[object, ...]:
    """A generic page may lack item_class even when another class page has it."""
    return (
        entity.name_zh,
        entity.base_type,
        entity.requirement.raw if entity.requirement else "",
        tuple((mod.kind.value, mod.text) for mod in entity.mods if mod.is_factual),
    )


def prepare_refresh(conn: sqlite3.Connection, source: PageSource, *,
                    force: bool = False, min_main_items: int = MIN_MAIN_ITEMS,
                    min_class_only_items: int = MIN_CLASS_ONLY_ITEMS,
                    min_english_items: int = MIN_ENGLISH_ITEMS) -> UniqueRefreshPlan:
    """Fetch and parse every source before a single database write."""
    if conn.in_transaction:
        raise UniqueRefreshError("预检连接已有未完成事务")
    old_fingerprint = entity_fingerprint(conn)
    old = _existing(conn)
    evidence: dict[str, PageEvidence] = {}

    list_result = _read(source, poedb_unique.LIST_URL, force=force,
                        role="main", evidence=evidence)
    list_items, list_stats = poedb_unique.extract_uniques(list_result.text)
    if list_stats.failed or len(list_items) < min_main_items:
        raise UniqueRefreshError(
            f"主列表不完整：成功 {len(list_items)}，失败 {list_stats.failed}，"
            f"最低 {min_main_items}"
        )
    main = {item.slug: item for item in list_items}
    if len(main) != len(list_items):
        raise UniqueRefreshError("主列表 slug 重复，拒绝覆盖")

    hub_url = f"{poedb_index.BASE}/{poedb_index.MODIFIERS_HUB}"
    hub = _read(source, hub_url, force=force, role="class_hub", evidence=evidence)
    pages = tuple(dict.fromkeys((
        *poedb_index._declared_mod_pages(hub.text),
        *poedb_index.PARENT_CLASS_PAGES,
    )))
    class_items: dict[str, Entity] = {}
    ambiguous: set[str] = set()
    for slug in pages:
        url = f"{poedb_index.BASE}/{slug}"
        page = _read(source, url, force=force, role="class", evidence=evidence)
        items, stats = poedb_unique.extract_uniques_from_class_page(page.text, slug, url)
        if stats.failed:
            raise UniqueRefreshError(f"类别页 {slug} 抽取失败 {stats.failed} 件")
        for item in items:
            prior = class_items.get(item.slug)
            if prior and _class_facts(prior) != _class_facts(item):
                ambiguous.add(item.slug)
            elif prior is None or (not prior.item_class and item.item_class):
                class_items[item.slug] = item

    en_result = _read(source, poedb_unique.EN_LIST_URL, force=force,
                      role="english_main", evidence=evidence)
    english = poedb_unique.extract_en_index(en_result.text)
    class_only = len(set(class_items) - set(main))
    if class_only < min_class_only_items or len(english) < min_english_items:
        raise UniqueRefreshError(
            f"补集来源不完整：类别页独有 {class_only}（最低 {min_class_only_items}），"
            f"英文索引 {len(english)}（最低 {min_english_items}）"
        )
    discovered = set(main) | set(class_items) | set(english)
    if old and len(discovered) < len(old) * (1 - MAX_INVENTORY_DROP):
        raise UniqueRefreshError(
            f"来源清单骤降：旧 {len(old)}，本轮 {len(discovered)}，超过 "
            f"{MAX_INVENTORY_DROP:.0%} 护栏"
        )

    # The English index excludes most jewel supplements. Fetch just their
    # English class pages; the detail page remains a second name source.
    need_en_pages = {
        class_items[s].attributes.get("source_page", "")
        for s in discovered - set(english)
        if s in class_items
    }
    for slug in sorted(need_en_pages - {""}):
        page = _read(source, f"{poedb_index.EN_BASE}/{slug}", force=force,
                     role="english_class", evidence=evidence)
        for item_slug, names in poedb_unique.extract_en_index(page.text).items():
            english.setdefault(item_slug, names)

    staged: dict[str, Entity] = {}
    preserved: set[str] = set()
    reviews: list[Review] = []
    extra_lines = 0
    detail_count = 0
    for slug in sorted(discovered):
        primary = main.get(slug) or class_items.get(slug)
        old_item = old.get(slug)
        detail_url = f"{poedb_index.BASE}/{slug}"
        result = _read(source, detail_url, force=force,
                       role="detail", evidence=evidence)
        detail = poedb_unique.parse_detail_page(result.text)
        detail_count += 1
        if primary is None:
            primary = poedb_unique.entity_from_detail(slug, detail_url, detail)
        primary = _combine(primary, class_items.get(slug), old_item,
                           english.get(slug), detail)
        primary.ref.fetched_at = result.fetched_at
        primary.league = result.fetched_at[:10]
        if slug in ambiguous:
            reviews.append(_review(primary, "多个类别页对同一物品给出不同事实"))
            if old_item:
                staged[slug] = old_item
                preserved.add(slug)
            continue
        if not detail.name_zh:
            reviews.append(_review(primary, "详情页缺中文名，无法证明页面身份"))
            if old_item:
                staged[slug] = old_item
                preserved.add(slug)
            continue
        diff = poedb_unique.compare_detail(
            detail, name_zh=primary.name_zh,
            base_type=primary.base_type or "",
            requirement_raw=primary.requirement.raw if primary.requirement else "",
            mod_texts=[mod.text for mod in primary.mods if mod.is_factual],
        )
        if diff.blocking:
            reviews.append(_review(primary, "；".join(diff.blocking)))
            if old_item:
                staged[slug] = old_item
                preserved.add(slug)
            continue
        if detail.mods:
            primary = merged_entity(primary, detail.mods)
            extra_lines += len(diff.detail_only)
        primary.content_hash = _entity_hash(primary)
        staged[slug] = primary

    for slug in sorted(set(old) - discovered):
        staged[slug] = old[slug]
        preserved.add(slug)
        reviews.append(_review(old[slug], "现有物品未出现在本轮主列表、类别页或英文索引；保留旧行"))

    ordered = tuple(staged[slug] for slug in sorted(staged))
    proof = hashlib.sha256()
    for url, page in sorted(evidence.items()):
        proof.update(json.dumps(
            (page.role, url, page.final_url, page.content_hash,
             page.fetched_at, int(page.from_cache)), ensure_ascii=False,
        ).encode("utf-8"))
        proof.update(b"\n")
    times = [page.fetched_at for page in evidence.values()]
    changed = sum(
        slug not in old or _content(item) != _content(old[slug])
        for slug, item in staged.items()
    )
    return UniqueRefreshPlan(
        entities=ordered, preserved_slugs=tuple(sorted(preserved)),
        reviews=tuple(reviews), old_fingerprint=old_fingerprint,
        old_count=len(old), discovered_count=len(discovered), list_count=len(main),
        class_only_count=class_only,
        detail_only_count=len(discovered - set(main) - set(class_items)),
        detail_pages=detail_count, detail_added_lines=extra_lines, changed_count=changed,
        source_digest=proof.hexdigest(), sources=tuple(evidence[url] for url in sorted(evidence)),
        source_pages=len(evidence),
        from_cache=sum(page.from_cache for page in evidence.values()),
        oldest_source_at=min(times), newest_source_at=max(times),
        list_hash=list_result.content_hash,
    )


def apply_refresh(conn: sqlite3.Connection, plan: UniqueRefreshPlan) -> dict[str, object]:
    """Publish the prepared corpus, search index and reviews atomically."""
    if conn.in_transaction:
        raise UniqueRefreshError("发布连接已有未完成事务")
    if not plan.entities:
        raise UniqueRefreshError("发布计划为空")
    started = time.monotonic()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if entity_fingerprint(conn) != plan.old_fingerprint:
            raise UniqueRefreshError("唯一装备实体在预检后已改变，请重新预检")
        preserved = set(plan.preserved_slugs)
        for item in plan.entities:
            if item.slug not in preserved:
                _upsert_entity(conn, item)
        fts_mod.clear_index(conn, entity_prefix=UNIQUE_PREFIX)
        alias_mod.clear_aliases(conn, entity_prefix=UNIQUE_PREFIX)
        vocab_mod.build_vocab(conn)
        expected = 0
        written = 0
        for item in plan.entities:
            cards = to_chunks(item)
            expected += len(cards)
            written += fts_mod.index_chunks(conn, cards)
        if written != expected:
            raise UniqueRefreshError("有不可索引的唯一装备卡片，拒绝部分发布")
        alias_count = alias_mod.index_aliases(conn, list(plan.entities))
        chunk_rows = conn.execute(
            "SELECT count(*) FROM chunk WHERE entity_id LIKE 'unique_item:%'"
        ).fetchone()[0]
        fts_rows = conn.execute(
            "SELECT count(*) FROM chunk_fts WHERE entity_id LIKE 'unique_item:%'"
        ).fetchone()[0]
        if (chunk_rows, fts_rows) != (written, written):
            raise UniqueRefreshError("唯一装备卡片与 FTS 行数不一致，拒绝发布")
        for review in plan.reviews:
            conn.execute(
                "INSERT INTO source_conflict(subject,field,kind,source_a,value_a,url_a,"
                "source_b,value_b,url_b,note,cause,first_seen,last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now')) "
                "ON CONFLICT(subject,field,source_a,source_b) DO UPDATE SET "
                "value_a=excluded.value_a,value_b=excluded.value_b,"
                "note=excluded.note,cause=excluded.cause,last_seen=datetime('now')",
                (
                    f"{review.name}（{review.slug}）", "详情页词缀", "unique_review",
                    "POEDB 列表/类别页", "待核对", review.source_url,
                    "POEDB 详情页", "待核对", review.detail_url,
                    review.reason[:400], "unique_refresh_review",
                ),
            )
        note = {
            "refresh_scope": "unique_complete_chain_v1",
            "source_digest": plan.source_digest,
            "list_hash": plan.list_hash,
            "source_pages": plan.source_pages,
            "detail_pages": plan.detail_pages,
            "detail_added_lines": plan.detail_added_lines,
            "from_cache": plan.from_cache,
            "oldest_source_at": plan.oldest_source_at,
            "newest_source_at": plan.newest_source_at,
            "reviews": len(plan.reviews),
            "preserved_entities": len(plan.preserved_slugs),
            "published_entities": len(plan.entities),
            "indexed_chunks": written,
        }
        conn.execute(
            "INSERT INTO ingest_run(source,category,started_at,finished_at,total,succeeded,"
            "failed,skipped,elapsed_ms,note) VALUES "
            "('POEDB','unique_refresh',datetime('now'),datetime('now'),?,?,?,?,?,?)",
            (plan.detail_pages, len(plan.entities), len(plan.reviews), 0,
             int((time.monotonic() - started) * 1000),
             json.dumps(note, ensure_ascii=False, sort_keys=True)),
        )
        run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "CREATE TABLE IF NOT EXISTS unique_refresh_page ("
            "run_id INTEGER NOT NULL,role TEXT NOT NULL,url TEXT NOT NULL,"
            "final_url TEXT NOT NULL,content_hash TEXT NOT NULL,"
            "fetched_at TEXT NOT NULL,from_cache INTEGER NOT NULL,"
            "PRIMARY KEY(run_id,url))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_unique_refresh_page_url "
            "ON unique_refresh_page(url,run_id DESC)"
        )
        conn.executemany(
            "INSERT INTO unique_refresh_page(run_id,role,url,final_url,content_hash,"
            "fetched_at,from_cache) VALUES (?,?,?,?,?,?,?)",
            [
                (run_id, page.role, page.url, page.final_url, page.content_hash,
                 page.fetched_at, int(page.from_cache))
                for page in plan.sources
            ],
        )
        if conn.execute(
            "SELECT count(*) FROM unique_refresh_page WHERE run_id=?", (run_id,)
        ).fetchone()[0] != plan.source_pages:
            raise UniqueRefreshError("来源页证明账不完整，拒绝发布")
        if source_ledger_digest(conn, run_id) != plan.source_digest:
            raise UniqueRefreshError("来源页证明账摘要不一致，拒绝发布")
        note["published_fingerprint"] = published_fingerprint(conn)
        conn.execute(
            "UPDATE ingest_run SET note=? WHERE run_id=?",
            (json.dumps(note, ensure_ascii=False, sort_keys=True), run_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        vocab_mod.build_vocab(conn)
        raise
    return {
        **plan.as_dict(), "applied": True,
        "run_id": run_id, "indexed_chunks": written, "indexed_aliases": alias_count,
    }
