"""GGG 官方 PoE1 版本公告：论坛首帖逐页核对、增量发布与可引用检索。

只认官方 Patch Notes 分区中的 3.x/更高版本补丁或 Hotfix 标题，并核验帖子
首楼的标题、GGG 作者身份与日期。社区回复从不进入知识库。论坛日期没有时区，
因此只保存来源明确给出的日历日期，不推断准确发布时间。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from entities.models import Chunk, DocumentMetadata
from entities.taxonomy import Category, Source
from kb.fetcher import Fetcher
from retrieval.fts import index_chunks
from retrieval.tokenize import tokens_for_fts

INDEX_URL = "https://www.pathofexile.com/forum/view-forum/patch-notes"
THREAD_PREFIX = "https://www.pathofexile.com/forum/view-thread/"
SOURCE_PRIORITY = 6
_TITLE = re.compile(
    r"^(?P<version>[3-9]\.\d+(?:\.\d+)?[a-z]?) (?P<kind>Patch Notes|Hotfix(?: \d+)?)$",
    re.IGNORECASE,
)
_THREAD_HREF = re.compile(r"^/forum/view-thread/(?P<id>[1-9]\d*)$")


@dataclass(frozen=True)
class PatchListing:
    thread_id: int
    version: str
    kind: str
    title: str
    published_on: str

    @property
    def url(self) -> str:
        return f"{THREAD_PREFIX}{self.thread_id}"


@dataclass(frozen=True)
class PatchNote(PatchListing):
    body: str
    content_hash: str
    fetched_at: str


def _classify_title(title: str) -> tuple[str, str] | None:
    match = _TITLE.fullmatch(" ".join(title.split()))
    if not match:
        return None
    kind = "hotfix" if match.group("kind").lower().startswith("hotfix") else "patch"
    return match.group("version"), kind


def _source_date(raw: str) -> str:
    value = raw.strip().lstrip(",").strip()
    return datetime.strptime(value, "%b %d, %Y, %I:%M:%S %p").date().isoformat()


def parse_listing(html: str) -> list[PatchListing]:
    tree = HTMLParser(html)
    if "Patch Notes" not in (tree.css_first("title").text(strip=True)
                             if tree.css_first("title") else ""):
        raise ValueError("GGG 公告目录页面身份不符")
    items: list[PatchListing] = []
    seen: set[int] = set()
    for row in tree.css("tr"):
        link = row.css_first(".thread_title .title a")
        date = row.css_first(".postBy .post_date")
        if link is None:
            continue
        title = " ".join(link.text(strip=True).split())
        classified = _classify_title(title)
        if classified is None:
            continue
        match = _THREAD_HREF.fullmatch(link.attributes.get("href", ""))
        if match is None or date is None:
            raise ValueError(f"GGG 公告目录行结构变化：{title}")
        thread_id = int(match.group("id"))
        if thread_id in seen:
            raise ValueError(f"GGG 公告目录重复线程：{thread_id}")
        seen.add(thread_id)
        items.append(PatchListing(
            thread_id, classified[0], classified[1], title,
            _source_date(date.text(strip=True)),
        ))
    if not items:
        raise ValueError("GGG 公告目录没有可识别的 PoE1 补丁标题")
    return items


def parse_thread(html: str, listing: PatchListing, fetched_at: str) -> PatchNote:
    tree = HTMLParser(html)
    heading = tree.css_first("h1.layoutBoxTitle")
    if heading is None or " ".join(heading.text(strip=True).split()) != listing.title:
        raise ValueError(f"GGG 公告标题与目录不一致：{listing.thread_id}")
    breadcrumb = tree.css_first(".breadcrumb")
    if breadcrumb is None or "Patch Notes" not in breadcrumb.text(strip=True):
        raise ValueError(f"GGG 公告不在补丁分区：{listing.thread_id}")
    first = tree.css_first("table.forumPostListTable tr")
    if first is None or "staff" not in (first.attributes.get("class") or "").split():
        raise ValueError(f"GGG 公告首帖不是官方 staff：{listing.thread_id}")
    author = first.css_first(".posted-by .roleLabel.staffText")
    body_node = first.css_first("div.content")
    date_node = first.css_first(".posted-by .post_date")
    if (author is None or "Grinding Gear Games" not in author.text(strip=True)
            or body_node is None or date_node is None):
        raise ValueError(f"GGG 公告首帖缺官方身份、正文或日期：{listing.thread_id}")
    if _source_date(date_node.text(strip=True)) != listing.published_on:
        raise ValueError(f"GGG 公告发布日期与目录不一致：{listing.thread_id}")
    lines = [" ".join(line.split()) for line in body_node.text(separator="\n", strip=True).splitlines()]
    lines = [line for line in lines if line]
    if lines and lines[0] == listing.title:
        lines.pop(0)
    body = "\n".join(lines)
    if not body or len(body) > 500_000:
        raise ValueError(f"GGG 公告正文为空或超限：{listing.thread_id}")
    digest = hashlib.sha256(
        f"{listing.title}\n{listing.published_on}\n{body}".encode("utf-8")
    ).hexdigest()
    return PatchNote(**vars(listing), body=body, content_hash=digest, fetched_at=fetched_at)


def _verify_final_url(actual: str, expected: str) -> None:
    parsed = urlparse(actual or expected)
    wanted = urlparse(expected)
    if parsed.scheme != "https" or parsed.hostname != "www.pathofexile.com" or (
        parsed.path.rstrip("/") != wanted.path.rstrip("/")
    ):
        raise ValueError(f"GGG 页面重定向到非预期地址：{actual}")


def discover(fetcher: Fetcher, *, pages: int = 1, force: bool = False) -> list[PatchListing]:
    if not 1 <= pages <= 50:
        raise ValueError("pages 必须为 1–50")
    found: dict[int, PatchListing] = {}
    for page in range(1, pages + 1):
        url = INDEX_URL if page == 1 else f"{INDEX_URL}/page/{page}"
        response = fetcher.fetch(url, force=force)
        if not response.ok:
            raise RuntimeError(f"GGG 公告目录抓取失败：HTTP {response.status_code}")
        _verify_final_url(response.final_url, url)
        for item in parse_listing(response.text):
            previous = found.setdefault(item.thread_id, item)
            if previous != item:
                raise ValueError(f"GGG 跨页线程资料不一致：{item.thread_id}")
    return list(found.values())


def fetch_notes(
    fetcher: Fetcher, listings: Iterable[PatchListing], *, force: bool = False
) -> list[PatchNote]:
    notes: list[PatchNote] = []
    for listing in listings:
        response = fetcher.fetch(listing.url, force=force)
        if not response.ok:
            raise RuntimeError(f"GGG 公告抓取失败：{listing.thread_id} HTTP {response.status_code}")
        _verify_final_url(response.final_url, listing.url)
        notes.append(parse_thread(response.text, listing, response.fetched_at))
    return notes


def _chunks(note: PatchNote) -> list[Chunk]:
    metadata = DocumentMetadata(
        title=note.title, category=Category.BASICS, subcategory="官方版本公告",
        source=Source.PATCH_NOTES.value, source_priority=SOURCE_PRIORITY,
        url=note.url, version=note.version, language="en",
        tags=[note.version, note.kind, "PoE1", "Patch Notes"],
        fetched_at=note.fetched_at,
    )
    groups: list[str] = []
    current = ""
    for line in note.body.splitlines():
        if current and len(current) + len(line) + 1 > 1500:
            groups.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        groups.append(current)
    return [Chunk(
        chunk_id=f"patch_note:{note.thread_id}:{index}",
        entity_id=f"patch_note:{note.thread_id}",
        text=f"{note.title}（{note.published_on}）\n{group}",
        tokens=tokens_for_fts(f"{note.title} {group}"),
        keywords=f"{note.version} {note.kind} GGG official patch notes",
        metadata=metadata,
    ) for index, group in enumerate(groups)]


def publish(conn: sqlite3.Connection, notes: Iterable[PatchNote]) -> dict[str, int]:
    """所有页先解析成功再调用；单事务发布公告、实体与可引用切片。"""
    batch = list(notes)
    if len({note.thread_id for note in batch}) != len(batch):
        raise ValueError("发布批次含重复线程")
    changed = 0
    chunks = 0
    with conn:
        for note in batch:
            old = conn.execute(
                "SELECT content_hash FROM ggg_patch_note WHERE thread_id=?", (note.thread_id,)
            ).fetchone()
            if old is not None and old["content_hash"] == note.content_hash:
                continue
            changed += 1
            conn.execute(
                "INSERT INTO ggg_patch_note(thread_id,version,kind,title,published_on,body,"
                "source_url,content_hash,fetched_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(thread_id) DO UPDATE SET version=excluded.version,kind=excluded.kind,"
                "title=excluded.title,published_on=excluded.published_on,body=excluded.body,"
                "source_url=excluded.source_url,content_hash=excluded.content_hash,"
                "fetched_at=excluded.fetched_at,updated_at=datetime('now')",
                (note.thread_id, note.version, note.kind, note.title, note.published_on,
                 note.body, note.url, note.content_hash, note.fetched_at),
            )
            conn.execute(
                "INSERT INTO entity(entity_id,entity_type,name_zh,name_en,slug,attributes,"
                "source,source_priority,url,version,content_hash,fetched_at,updated_at) "
                "VALUES(?, 'patch_note',?,?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(entity_id) DO UPDATE SET name_zh=excluded.name_zh,"
                "name_en=excluded.name_en,slug=excluded.slug,attributes=excluded.attributes,"
                "source=excluded.source,source_priority=excluded.source_priority,"
                "url=excluded.url,version=excluded.version,content_hash=excluded.content_hash,"
                "fetched_at=excluded.fetched_at,updated_at=datetime('now')",
                (f"patch_note:{note.thread_id}", note.title, note.title, str(note.thread_id),
                 json.dumps({"kind": note.kind, "published_on": note.published_on},
                            ensure_ascii=False), Source.PATCH_NOTES.value, SOURCE_PRIORITY,
                 note.url, note.version, note.content_hash, note.fetched_at),
            )
            conn.execute("DELETE FROM chunk_fts WHERE entity_id=?", (f"patch_note:{note.thread_id}",))
            conn.execute("DELETE FROM chunk WHERE entity_id=?", (f"patch_note:{note.thread_id}",))
            chunks += index_chunks(conn, _chunks(note))
    return {"received": len(batch), "changed": changed, "chunks": chunks}


def search(
    conn: sqlite3.Connection, *, version: str = "", query: str = "", limit: int = 20,
) -> list[dict[str, str | int]]:
    """精确版本或正文子串查询；默认按来源日期倒序，不猜当前生效版本。"""
    if not 1 <= limit <= 100:
        raise ValueError("limit 必须为 1–100")
    if len(version) > 40 or len(query) > 200:
        raise ValueError("查询过长")
    rows = conn.execute(
        "SELECT thread_id,version,kind,title,published_on,source_url,fetched_at,"
        "length(body) AS body_length FROM ggg_patch_note "
        "WHERE (?='' OR version=?) AND (?='' OR instr(lower(body),lower(?))>0) "
        "ORDER BY published_on DESC,thread_id DESC LIMIT ?",
        (version, version, query, query, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def detail(conn: sqlite3.Connection, thread_id: int) -> dict[str, str | int] | None:
    row = conn.execute("SELECT * FROM ggg_patch_note WHERE thread_id=?", (thread_id,)).fetchone()
    return dict(row) if row is not None else None


def sync_latest(conn: sqlite3.Connection, *, force: bool = False,
                pages: int = 1, max_threads: int = 30) -> int:
    """供现有知识同步调度器使用；只抓最近官方目录，不删除历史公告。"""
    if not 1 <= pages <= 50 or not 1 <= max_threads <= 1000:
        raise ValueError("公告同步页数或帖数超限")
    from core.config import get_config  # noqa: PLC0415

    cfg = get_config()
    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )
    listings = discover(fetcher, pages=pages, force=force)[:max_threads]
    notes = fetch_notes(fetcher, listings, force=force)
    result = publish(conn, notes)
    with conn:
        conn.execute(
            "INSERT INTO ingest_run(source,category,started_at,finished_at,total,succeeded,"
            "failed,skipped,note) VALUES(?, ?, datetime('now'),datetime('now'),?,?,?,?,?)",
            (Source.PATCH_NOTES.value, "ggg_patch_notes", len(notes), len(notes), 0,
             len(notes) - result["changed"],
             f"pages={pages}; changed={result['changed']}; chunks={result['chunks']}"),
        )
    return 0


def _literal_mention(line: str, term: str) -> bool:
    """按英文词边界匹配；Arc 不得误命中 Archmage。只复述来源原句。"""
    pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
    return re.search(pattern, line, re.IGNORECASE) is not None


def mentions(
    conn: sqlite3.Connection, terms: Iterable[str], *, version: str = "",
    limit: int = 50,
) -> list[dict[str, str | int | bool]]:
    """精确定位官方公告的逐行原文，不把文字命中解释为机制生效或数值变化。"""
    unique = list(dict.fromkeys(term.strip() for term in terms if 2 <= len(term.strip()) <= 120))
    if not unique or len(unique) > 100 or not 1 <= limit <= 200 or len(version) > 40:
        raise ValueError("查询词或结果上限无效")
    rows = conn.execute(
        "SELECT thread_id,version,title,published_on,body,source_url "
        "FROM ggg_patch_note WHERE (?='' OR version=?) "
        "ORDER BY published_on DESC,thread_id DESC",
        (version, version),
    ).fetchall()
    output: list[dict[str, str | int | bool]] = []
    for row in rows:
        for line_no, raw in enumerate(row["body"].splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            for term in unique:
                if _literal_mention(line, term):
                    output.append({
                        "thread_id": row["thread_id"], "version": row["version"],
                        "title": row["title"], "published_on": row["published_on"],
                        "source_url": row["source_url"], "line_number": line_no,
                        "term": term, "source_excerpt": line[:1000],
                        "excerpt_truncated": len(line) > 1000,
                    })
                    if len(output) >= limit:
                        return output
    return output
