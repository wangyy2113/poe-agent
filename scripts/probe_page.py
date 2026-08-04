#!/usr/bin/env python3
"""探测某个 POEDB 页面装了什么（标题 / 标题层级 / 是否含词缀载荷 / 表格与 tab 数 / 正文预览）。

配合 `scripts/verify_index_pages.py` 使用：那个脚本报出「站点导航有、清单无」的页面后，
用本脚本先看清它到底是内容页还是站务页，再决定是加入 `INDEX_PAGES` 还是加入
`NAV_NON_CONTENT`。**不要凭 slug 猜**——`/cn/Exceptional` 看着像修饰词，实际是
「卓越」宝石标签索引页（赋予 / 增幅 / 启蒙 等 55 项）。

    python scripts/probe_page.py Exceptional
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.fetcher import Fetcher  # noqa: E402


def strip(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    slug = sys.argv[1] if len(sys.argv) > 1 else "Exceptional"
    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    f = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )
    res = f.fetch(f"https://poedb.tw/cn/{slug}")
    print(f"{slug}: HTTP {res.status_code}  {len(res.text):,} 字符  cache={res.from_cache}")
    m = re.search(r"<title>(.*?)</title>", res.text, re.S)
    print("title :", strip(m.group(1)) if m else "?")

    body = res.text
    for tag in ("h1", "h2", "h3"):
        found = [strip(x) for x in re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", body, re.S)]
        found = [x for x in found if x][:8]
        if found:
            print(f"{tag:6s}:", " | ".join(x[:60] for x in found))

    print("new ModsView :", "new ModsView(" in body)
    print("<table> 数   :", body.count("<table"))
    print("tab-pane 数  :", body.count("tab-pane"))
    # 主内容区文本预览
    i = body.find("</nav>")
    j = body.find("<footer")
    mid = body[i:j] if i > 0 and j > i else body
    print("\n--- 主体文本预览（400 字）---")
    print(strip(mid)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
