#!/usr/bin/env python3
"""预检并增量发布 GGG 官方 PoE1 Patch Notes / Hotfix 首帖。

默认只预检，不写数据库。来源抓取沿用项目的 robots、限速与磁盘缓存；
所有请求及页面身份核对成功之后，--apply 才会在单事务中发布。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from kb.fetcher import Fetcher  # noqa: E402
from kb.patch_notes import discover, fetch_notes, publish  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="GGG 官方 PoE1 补丁公告预检与发布")
    parser.add_argument("--pages", type=int, default=1, help="目录页数，1–50")
    parser.add_argument("--max-threads", type=int, default=30, help="最多抓取的补丁帖，1–1000")
    parser.add_argument("--force", action="store_true", help="忽略未过期缓存，重新抓取")
    parser.add_argument("--apply", action="store_true", help="预检全部通过后发布")
    args = parser.parse_args()
    if not 1 <= args.pages <= 50 or not 1 <= args.max_threads <= 1000:
        parser.error("pages 必须为 1–50，max-threads 必须为 1–1000")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = get_config()
    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )
    started = time.monotonic()
    listings = discover(fetcher, pages=args.pages, force=args.force)[:args.max_threads]
    notes = fetch_notes(fetcher, listings, force=args.force)
    print(f"已核验目录 {args.pages} 页、官方首帖 {len(notes)} 篇；耗时 {time.monotonic()-started:.1f}s")
    for note in notes[:10]:
        print(f"  {note.published_on} {note.title}  {note.url}")
    if len(notes) > 10:
        print(f"  另有 {len(notes)-10} 篇已核验，预览省略；发布时全部写入")
    if not args.apply:
        print("只预检，未修改数据库；加 --apply 发布")
        return 0
    conn = dbmod.init_db(cfg.db_path)
    try:
        result = publish(conn, notes)
        print(f"已发布 {result['received']} 篇，更新 {result['changed']} 篇、{result['chunks']} 个切片")
        return 0
    finally:
        dbmod.close_db()


if __name__ == "__main__":
    raise SystemExit(main())
