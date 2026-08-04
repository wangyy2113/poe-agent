#!/usr/bin/env python3
"""索引页清单一致性校验。

## 为什么需要

`kb/sources/poedb_index.py` 的 `INDEX_PAGES` 是**硬编码**清单。101 个词缀类别页
是从枢纽页动态发现的、且带结构断言，改版会立刻报错；但这 31 个索引页不是——
POEDB 新增或改名导航项时，清单会**静默变旧**，表现为知识库缺了一整类内容而没有
任何报错。这与「无据不答」相冲突：库里没有不等于游戏里没有，静默缺失会让 Agent
对本该能答的问题拒答，而我们不会知道。

本脚本把「固化清单」从「某次手抄的列表」变成「可对站点复核的列表」：

1. 从导航栏抽出全部 `/cn/` 链接，报告**站点有而清单无**的页面（覆盖缺口）；
2. 报告清单有而导航无的页面（正常：部分页面来自勘测而非导航，仅作信息）；
3. 校验清单里每一页都能取到（HTTP 200）。

    python scripts/verify_index_pages.py            # 走缓存，离线可跑
    python scripts/verify_index_pages.py --refresh  # 强制重抓导航来源页
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.fetcher import Fetcher  # noqa: E402
from kb.sources.poedb_index import BASE, INDEX_PAGES  # noqa: E402

#: 用哪一页来读导航栏。任意页面的导航栏都一样，取一个必然已缓存的。
NAV_SOURCE = "Unique_item"

#: 导航栏里非内容页，不计入覆盖缺口。
#: 只列**确认为站务页**的，不要拿它当「懒得支持就加进来」的垃圾桶。
NAV_NON_CONTENT = frozenset({"patreon"})


def nav_slugs(html: str) -> list[str]:
    """抽出导航栏内的 `/cn/` 页面 slug。"""
    start = html.find('<nav class="navbar')
    if start < 0:
        raise RuntimeError("未找到导航栏，POEDB 页面结构可能已变更")
    end = html.find("</nav>", start)
    if end < 0:
        raise RuntimeError("导航栏未闭合，无法界定范围")
    nav = html[start:end]
    slugs = set(re.findall(r'href="/cn/([A-Za-z_0-9%\-]+)"', nav))
    if not slugs:
        raise RuntimeError("导航栏内未发现任何 /cn/ 链接，结构可能已变更")
    return sorted(slugs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="索引页清单一致性校验")
    ap.add_argument("--refresh", action="store_true", help="强制重抓（忽略缓存）")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )

    print("=" * 78)
    print("索引页清单一致性校验")
    print("=" * 78)

    res = fetcher.fetch(f"{BASE}/{NAV_SOURCE}", force=args.refresh)
    if not res.ok:
        print(f"取导航来源页失败：HTTP {res.status_code}")
        return 2

    site = set(nav_slugs(res.text))
    listed = {p.slug for p in INDEX_PAGES}
    content = site - NAV_NON_CONTENT

    missing = sorted(content - listed)
    extra = sorted(listed - site)

    print(f"\n导航栏内容页 {len(content)} 个｜清单 {len(listed)} 项"
          f"（来源页 {'缓存' if res.from_cache else '实抓'}）")

    print(f"\n[1] 站点导航有、清单无 —— 覆盖缺口（必须为 0）: {len(missing)}")
    for s in missing:
        print(f"    ! {s}  → {BASE}/{s}")
    if not missing:
        print("    无")

    print(f"\n[2] 清单有、导航无 —— 来自勘测的补充项（正常）: {len(extra)}")
    print("    " + "、".join(extra) if extra else "    无")

    print(f"\n[3] 清单内每页可达性（{len(listed)} 页，走缓存）")
    unreachable: list[tuple[str, str]] = []
    for p in INDEX_PAGES:
        try:
            r = fetcher.fetch(p.url, force=False)
            if not r.ok:
                unreachable.append((p.slug, f"HTTP {r.status_code}"))
        except Exception as exc:  # noqa: BLE001
            unreachable.append((p.slug, f"{type(exc).__name__}: {exc}"))
    if unreachable:
        for slug, why in unreachable:
            print(f"    ! {slug}: {why}")
    else:
        print("    全部可达")

    print("\n" + "=" * 78)
    ok = not missing and not unreachable
    print("结论：通过" if ok else "结论：**未通过**")
    print(f"  覆盖缺口 {len(missing)}｜不可达 {len(unreachable)}")
    if missing:
        print("  处理方式：先用 `python scripts/probe_page.py <slug>` 看清页面内容，")
        print("  再决定加入 INDEX_PAGES（内容页）或 NAV_NON_CONTENT（站务页，须写理由）。")
        print("  不要凭 slug 猜，也不要放着不管。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
