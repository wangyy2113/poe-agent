#!/usr/bin/env python3
"""Preflight and atomically refresh POEDB unique items with their detail lines.

    python scripts/refresh_unique_items.py status
    python scripts/refresh_unique_items.py preview          # offline, existing cache
    python scripts/refresh_unique_items.py preview --fetch  # fresh live preflight
    python scripts/refresh_unique_items.py apply            # live fetch + backup + publish

The live path checks the complete list/class/English/detail chain (roughly
1,450 pages). At the configured 0.5 requests/sec this can take about 50 minutes.
No database row is changed until every page has been fetched and parsed.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backup import BackupError, create_backup  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.pipeline import _make_fetcher  # noqa: E402
from kb.sources.poedb_index import BASE as POEDB_CN_BASE  # noqa: E402
from kb.sources.poedb_unique import StructureError  # noqa: E402
from kb.unique_refresh import (  # noqa: E402
    CachedPageSource,
    UniqueRefreshError,
    apply_refresh,
    prepare_refresh,
    published_fingerprint,
    source_ledger_digest,
)


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def status(conn: sqlite3.Connection) -> dict[str, object]:
    count = conn.execute(
        "SELECT count(*) FROM entity WHERE entity_type='unique_item'"
    ).fetchone()[0]
    row = conn.execute(
        "SELECT run_id,finished_at,total,succeeded,failed,note FROM ingest_run "
        "WHERE category='unique_refresh' AND finished_at IS NOT NULL "
        "ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    last = None
    if row:
        source = json.loads(row["note"] or "{}")
        has_ledger = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unique_refresh_page'"
        ).fetchone() is not None
        pages = (
            conn.execute(
                "SELECT count(*) FROM unique_refresh_page WHERE run_id=?", (row["run_id"],)
            ).fetchone()[0]
            if has_ledger else 0
        )
        bundle_ok = published_fingerprint(conn) == source.get("published_fingerprint")
        ledger_ok = (
            has_ledger and pages == source.get("source_pages")
            and source_ledger_digest(conn, row["run_id"]) == source.get("source_digest")
        )
        last = {
            "run_id": row["run_id"], "finished_at": row["finished_at"],
            "total": row["total"], "succeeded": row["succeeded"],
            "reviews": row["failed"], "source": source,
            "integrity": "valid" if bundle_ok and ledger_ok else "drifted",
            "bundle_ok": bundle_ok, "source_ledger_ok": ledger_ok,
        }
    return {"entities": count, "last_complete_refresh": last}


def proof(conn: sqlite3.Connection, slug: str, *, run_id: int | None = None) -> dict[str, object]:
    """Look up one item's detail-page provenance in a published run."""
    if not re.fullmatch(r"[A-Za-z0-9_.%:-]+", slug):
        raise UniqueRefreshError("slug 只能包含页面标识字符，不得包含路径分隔符")
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unique_refresh_page'"
    ).fetchone() is None:
        return {"slug": slug, "proof": None}
    url = f"{POEDB_CN_BASE}/{slug}"
    row = conn.execute(
        "SELECT p.*,r.finished_at FROM unique_refresh_page p "
        "JOIN ingest_run r ON r.run_id=p.run_id "
        "WHERE p.url=? AND p.role='detail' AND (? IS NULL OR p.run_id=?) "
        "ORDER BY p.run_id DESC LIMIT 1",
        (url, run_id, run_id),
    ).fetchone()
    return {"slug": slug, "proof": dict(row) if row else None}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="只读查看当前行数与最近完整刷新")
    proof_cmd = sub.add_parser("proof", help="只读查看一件装备的详情页来源证明")
    proof_cmd.add_argument("--slug", required=True, help="POEDB 页面 slug")
    proof_cmd.add_argument("--run-id", type=int, help="指定历史刷新 run_id")
    preview = sub.add_parser("preview", help="只读预检；默认只用已有缓存")
    preview.add_argument("--fetch", action="store_true", help="强制重新抓取全部来源页")
    sub.add_parser("apply", help="强制抓取、完整预检、备份后原子发布")
    return parser


def _emit(result: dict[str, object], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if "error" in result:
        print(f"失败：{result['error']}")
        return
    if "last_complete_refresh" in result:
        print(f"当前唯一装备 {result['entities']} 件；最近完整刷新："
              f"{result['last_complete_refresh'] or '无'}")
        return
    if "proof" in result:
        print(f"{result['slug']} 来源证明：{result['proof'] or '尚无完整刷新记录'}")
        return
    print(f"来源 {result['source_pages']} 页（详情 {result['detail_pages_checked']}），"
          f"发现 {result['discovered_entities']} 件，计划发布 "
          f"{result['published_entities']} 件；详情页补充 "
          f"{result['detail_added_lines']} 条词缀，待核对 {result['review_count']} 件")
    print(f"来源时间 {result['oldest_source_at']} 至 {result['newest_source_at']}；"
          f"缓存页 {result['from_cache']}；摘要 {str(result['source_digest'])[:16]}")
    for review in result["reviews"][:5]:
        print(f"  待核对 {review['slug']}：{review['reason']}")
    if result.get("applied"):
        print(f"已原子发布，卡片 {result['indexed_chunks']} 张；"
              f"备份 {result['backup_name']}")
    else:
        print("未修改数据库。")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    cfg = get_config()
    setup_logging(cfg.logs_dir, str(cfg.get("logging.level", "INFO")))
    try:
        if args.command == "status":
            with closing(_connect(cfg.db_path, readonly=True)) as conn:
                result = status(conn)
            _emit(result, as_json=args.json)
            return 0
        if args.command == "proof":
            with closing(_connect(cfg.db_path, readonly=True)) as conn:
                result = proof(conn, args.slug, run_id=args.run_id)
            _emit(result, as_json=args.json)
            return 0 if result["proof"] else 2

        online = args.command == "apply" or args.fetch
        source = _make_fetcher(cfg) if online else CachedPageSource(cfg.cache_dir)
        with closing(_connect(cfg.db_path, readonly=True)) as conn:
            plan = prepare_refresh(conn, source, force=online)
        if args.command == "preview":
            result = {**plan.as_dict(), "mode": "live" if online else "cached"}
            _emit(result, as_json=args.json)
            return 0

        backup = create_backup(cfg.db_path, cfg.backup_dir)
        with closing(_connect(cfg.db_path, readonly=False)) as conn:
            result = apply_refresh(conn, plan)
        result["backup_name"] = backup.backup_name
        result["mode"] = "live"
        _emit(result, as_json=args.json)
        return 1 if plan.reviews else 0
    except (UniqueRefreshError, StructureError, BackupError, sqlite3.Error,
            OSError, httpx.HTTPError, RuntimeError) as exc:
        _emit({"error": str(exc)}, as_json=args.json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
