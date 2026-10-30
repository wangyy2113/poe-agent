#!/usr/bin/env python3
"""预检或原子发布 PoE Wiki 终局 Boss 攻略章节。默认不写数据库。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from kb.boss_guides import PAGES, fetch_guides, publish  # noqa: E402
from kb.fetcher import Fetcher  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="PoE Wiki Boss 攻略预检与发布")
    parser.add_argument("--keys", default=",".join(PAGES), help="逗号分隔的 Boss key")
    parser.add_argument("--force", action="store_true", help="忽略 HTTP 缓存")
    parser.add_argument("--apply", action="store_true", help="核验所有页面后原子发布")
    args = parser.parse_args()
    keys = [item.strip() for item in args.keys.split(",") if item.strip()]
    if len(keys) != len(set(keys)) or not keys or any(item not in PAGES for item in keys):
        parser.error(f"Boss key 无效；可选：{', '.join(PAGES)}")
    cfg = get_config()
    fetcher = Fetcher(
        cache_dir=cfg.cache_dir, user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )
    guides = fetch_guides(fetcher, force=args.force, keys=keys)
    for guide in guides:
        print(f"{guide.boss_key}: {guide.page_title}，{len(guide.sections)} 节，{guide.source_url}")
    if not args.apply:
        print("只预检，未修改数据库；加 --apply 发布")
        return 0
    conn = dbmod.init_db(cfg.db_path)
    try:
        result = publish(conn, guides)
        print(f"发布 {result['received']} 篇，变更 {result['changed']} 篇，切片 {result['chunks']} 张")
    finally:
        dbmod.close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
