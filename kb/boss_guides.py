"""PoE Wiki 终局 Boss 原文段落的保守采集与引用。

仅采明确白名单页面的攻略相关章节；不采玩家评论、导航框、掉落表或历史版本。
这些是 P3 Wiki 页面，不是 GGG 官方机制数据。页面可变，故保存抓取时刻与内容哈希。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

from selectolax.parser import HTMLParser

from entities.models import Chunk, DocumentMetadata
from entities.taxonomy import Category, Source
from kb.fetcher import Fetcher
from retrieval.fts import index_chunks
from retrieval.tokenize import tokens_for_fts

BASE = "https://www.poewiki.net/wiki/"
PAGES: dict[str, tuple[str, str]] = {
    "maven": ("The_Maven", "The Maven"),
    "sirus": ("Sirus,_Awakener_of_Worlds", "Sirus, Awakener of Worlds"),
    "shaper": ("The_Shaper", "The Shaper"),
    "elder": ("The_Elder", "The Elder"),
    "uber_elder": ("Uber_Elder", "The Elder (The Shaper's Realm)"),
    "cortex": ("Cortex", "Cortex"),
    "searing_exarch": ("The_Searing_Exarch", "The Searing Exarch"),
    "eater_of_worlds": ("The_Eater_of_Worlds", "The Eater of Worlds"),
}
INCLUDE = frozenset({
    "Entry", "Recommendations", "Mechanics", "Skills", "Strategy", "Encounters",
    "Maven boss", "Uber boss variant", "Modifiers", "Layout",
})


@dataclass(frozen=True)
class GuideSection:
    heading: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class BossGuide:
    boss_key: str
    page_title: str
    sections: tuple[GuideSection, ...]
    source_url: str
    content_hash: str
    fetched_at: str


def _line(text: str) -> str:
    return " ".join(text.split())


def parse_page(html: str, boss_key: str, fetched_at: str) -> BossGuide:
    if boss_key not in PAGES:
        raise ValueError("Boss 页面不在白名单")
    slug, expected_title = PAGES[boss_key]
    tree = HTMLParser(html)
    heading = tree.css_first("h1#firstHeading")
    content = tree.css_first("#mw-content-text[lang='en'] .mw-parser-output")
    if heading is None or _line(heading.text(strip=True)) != expected_title or content is None:
        raise ValueError(f"PoE Wiki Boss 页面身份不符：{boss_key}")
    sections: list[GuideSection] = []
    current = ""
    lines: list[str] = []

    def finish() -> None:
        if current and lines:
            sections.append(GuideSection(current, tuple(lines)))

    for node in content.iter():
        if node.tag == "h2":
            finish()
            current = _line(node.text(strip=True))
            lines = []
            continue
        if current not in INCLUDE:
            continue
        if node.tag in ("h3", "h4"):
            label = _line(node.text(strip=True))
            if label:
                lines.append(f"## {label}")
        elif node.tag == "p":
            value = _line(node.text(separator=" ", strip=True))
            if value:
                lines.append(value)
        elif node.tag in ("ul", "ol"):
            for item in node.css("li"):
                value = _line(item.text(separator=" ", strip=True))
                if value:
                    lines.append(f"• {value}")
        elif node.tag == "table" and "wikitable" in (node.attributes.get("class") or ""):
            for row in node.css("tr")[:150]:
                cells = [_line(cell.text(separator=" ", strip=True))
                         for cell in row.css("th,td")]
                if cells:
                    lines.append(" | ".join(cells))
    finish()
    body = "\n".join(line for section in sections for line in section.lines)
    if len(sections) < 2 or len(body) < 200 or len(body) > 400_000:
        raise ValueError(f"PoE Wiki Boss 攻略章节缺失或超限：{boss_key}")
    digest = hashlib.sha256(json.dumps(
        [(section.heading, section.lines) for section in sections],
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")).hexdigest()
    return BossGuide(boss_key, expected_title, tuple(sections), BASE + slug,
                     digest, fetched_at)


def _verify_url(actual: str, wanted: str) -> None:
    found, expected = urlparse(actual or wanted), urlparse(wanted)
    if found.scheme != "https" or found.hostname != "www.poewiki.net" or (
        found.path != expected.path
    ):
        raise ValueError(f"PoE Wiki 页面重定向到非预期地址：{actual}")


def fetch_guides(fetcher: Fetcher, *, force: bool = False,
                 keys: Iterable[str] = PAGES) -> list[BossGuide]:
    selected = tuple(dict.fromkeys(keys))
    if not selected or any(key not in PAGES for key in selected):
        raise ValueError("Boss 页面白名单选择无效")
    guides: list[BossGuide] = []
    for key in selected:
        url = BASE + PAGES[key][0]
        response = fetcher.fetch(url, force=force)
        if not response.ok:
            raise RuntimeError(f"PoE Wiki Boss 页面抓取失败：{key} HTTP {response.status_code}")
        _verify_url(response.final_url, url)
        guides.append(parse_page(response.text, key, response.fetched_at))
    return guides


def _chunks(guide: BossGuide) -> list[Chunk]:
    metadata = DocumentMetadata(
        title=guide.page_title, category=Category.ATLAS, subcategory="Boss 攻略原文",
        source=Source.WIKI.value, source_priority=3, url=guide.source_url,
        language="en", tags=[guide.page_title, "PoE1", "Boss"],
        fetched_at=guide.fetched_at,
    )
    chunks: list[Chunk] = []
    for index, section in enumerate(guide.sections):
        group = ""
        part = 0
        for line in section.lines:
            if group and len(group) + len(line) + 1 > 1800:
                text = f"{guide.page_title} / {section.heading}\n{group}"
                chunks.append(Chunk(
                    chunk_id=f"boss_guide:{guide.boss_key}:{index}:{part}",
                    entity_id=f"boss_guide:{guide.boss_key}", text=text,
                    tokens=tokens_for_fts(text),
                    keywords=f"{guide.page_title} {section.heading} Boss guide",
                    metadata=metadata,
                ))
                group, part = "", part + 1
            group = f"{group}\n{line}" if group else line
        if group:
            text = f"{guide.page_title} / {section.heading}\n{group}"
            chunks.append(Chunk(
                chunk_id=f"boss_guide:{guide.boss_key}:{index}:{part}",
                entity_id=f"boss_guide:{guide.boss_key}", text=text,
                tokens=tokens_for_fts(text),
                keywords=f"{guide.page_title} {section.heading} Boss guide",
                metadata=metadata,
            ))
    return chunks


def publish(conn: sqlite3.Connection, guides: Iterable[BossGuide]) -> dict[str, int]:
    batch = list(guides)
    if len({guide.boss_key for guide in batch}) != len(batch):
        raise ValueError("Boss 攻略发布批次含重复页面")
    changed = 0
    chunks = 0
    with conn:
        for guide in batch:
            old = conn.execute(
                "SELECT content_hash FROM boss_guide WHERE boss_key=?", (guide.boss_key,)
            ).fetchone()
            if old is not None and old["content_hash"] == guide.content_hash:
                continue
            changed += 1
            conn.execute(
                "INSERT INTO boss_guide(boss_key,page_title,sections,source_url,"
                "content_hash,fetched_at,updated_at) VALUES(?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(boss_key) DO UPDATE SET page_title=excluded.page_title,"
                "sections=excluded.sections,source_url=excluded.source_url,"
                "content_hash=excluded.content_hash,fetched_at=excluded.fetched_at,"
                "updated_at=datetime('now')",
                (guide.boss_key, guide.page_title, json.dumps(
                    [{"heading": section.heading, "lines": section.lines}
                     for section in guide.sections], ensure_ascii=False),
                 guide.source_url, guide.content_hash, guide.fetched_at),
            )
            entity_id = f"boss_guide:{guide.boss_key}"
            conn.execute(
                "INSERT INTO entity(entity_id,entity_type,name_zh,name_en,slug,attributes,"
                "source,source_priority,url,content_hash,fetched_at,updated_at) "
                "VALUES(?, 'boss_guide',?,?,?,?,?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(entity_id) DO UPDATE SET name_zh=excluded.name_zh,"
                "name_en=excluded.name_en,attributes=excluded.attributes,"
                "url=excluded.url,content_hash=excluded.content_hash,"
                "fetched_at=excluded.fetched_at,updated_at=datetime('now')",
                (entity_id, guide.page_title, guide.page_title, guide.boss_key,
                 json.dumps({"sections": [s.heading for s in guide.sections]},
                            ensure_ascii=False), Source.WIKI.value, 3, guide.source_url,
                 guide.content_hash, guide.fetched_at),
            )
            conn.execute("DELETE FROM chunk_fts WHERE entity_id=?", (entity_id,))
            conn.execute("DELETE FROM chunk WHERE entity_id=?", (entity_id,))
            chunks += index_chunks(conn, _chunks(guide))
    return {"received": len(batch), "changed": changed, "chunks": chunks}


def list_guides(conn: sqlite3.Connection) -> list[dict[str, str | int]]:
    return [dict(row) for row in conn.execute(
        "SELECT boss_key,page_title,source_url,fetched_at,content_hash "
        "FROM boss_guide ORDER BY boss_key"
    )]


def detail(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute("SELECT * FROM boss_guide WHERE boss_key=?", (key,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["sections"] = json.loads(result["sections"])
    return result


def sync_latest(conn: sqlite3.Connection, *, force: bool = False) -> int:
    from core.config import get_config  # noqa: PLC0415

    cfg = get_config()
    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )
    guides = fetch_guides(fetcher, force=force)
    result = publish(conn, guides)
    with conn:
        conn.execute(
            "INSERT INTO ingest_run(source,category,started_at,finished_at,total,succeeded,"
            "failed,skipped,note) VALUES(?, ?, datetime('now'),datetime('now'),?,?,?,?,?)",
            (Source.WIKI.value, "boss_guides", len(guides), len(guides), 0,
             len(guides) - result["changed"],
             f"changed={result['changed']}; chunks={result['chunks']}"),
        )
    return 0
