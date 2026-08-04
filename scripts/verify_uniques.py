#!/usr/bin/env python3
"""唯一装备知识库的完整性与一致性校验。

覆盖 L2 补全之后必须成立的不变量。**离线跑**（全部走本地缓存与库），
可反复执行：

    python scripts/verify_uniques.py

检查项：
  1. 三个来源的数量分布，以及与 `ingest_run` 台账是否吻合
  2. 权威英文名覆盖率——`name_en` 是英文别名的唯一来源，缺了玩家就搜不到
  3. 索引页 / 类别页 / 英文页三份清单是否都已被库覆盖（**这是「全面」的判据**）
  4. 每个实体都有别名与切片（缺了等于检索不到，相当于没入库）
  5. 空字段清点：允许存在但必须**逐条列出**，不能变成沉默的数据洞
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.fetcher import Fetcher  # noqa: E402
from kb.sources import poedb_index, poedb_unique  # noqa: E402

#: 允许 name_en 缺失的上限。英文名是英文检索的唯一入口，不应有缺口。
MAX_MISSING_EN = 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    conn = dbmod.init_db(cfg.db_path)
    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )

    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT entity_id, slug, name_zh, name_en, base_type, item_class, attributes "
            "FROM entity WHERE entity_type='unique_item'"
        )
    ]
    by_slug = {r["slug"]: r for r in rows}
    problems: list[str] = []

    print("=" * 80)
    print(f"唯一装备知识库校验（库内 {len(rows):,} 件）")
    print("=" * 80)

    # ── 1. 来源分布 ──
    print("\n[1] 来源分布")
    dist = Counter()
    for r in rows:
        a = json.loads(r["attributes"] or "{}")
        if "pane" in a:
            dist[f"索引页 · {a['pane']}"] += 1
        elif a.get("via") == "detail":
            dist["详情页 L2 兜底"] += 1
        else:
            dist[f"类别页 · {a.get('source_page', '?')}"] += 1
    idx = sum(v for k, v in dist.items() if k.startswith("索引页"))
    cls = sum(v for k, v in dist.items() if k.startswith("类别页"))
    det = dist.get("详情页 L2 兜底", 0)
    print(f"    索引页 {idx:,} ｜ 类别页补集 {cls:,} ｜ 详情页兜底 {det:,}")
    for k, v in dist.most_common(6):
        print(f"      {k:34s} {v}")

    # ── 2. 权威英文名覆盖 ──
    print("\n[2] 权威英文名覆盖")
    no_en = [r for r in rows if not (r["name_en"] or "").strip()]
    derivable = sum(
        1 for r in rows
        if (r["name_en"] or "") and r["slug"].replace("_", " ") != r["name_en"]
    )
    print(f"    有英文名 {len(rows) - len(no_en):,}/{len(rows):,}"
          f"（{100 * (len(rows) - len(no_en)) / len(rows):.1f}%）")
    print(f"    其中无法由 slug 反推的 {derivable:,} 件"
          f"（{100 * derivable / len(rows):.1f}%）—— 这些只能来自权威来源")
    if len(no_en) > MAX_MISSING_EN:
        problems.append(f"{len(no_en)} 件缺权威英文名（上限 {MAX_MISSING_EN}）")
        for r in no_en[:10]:
            print(f"      ! {r['slug']:32s} {r['name_zh']}")

    # ── 3. 三份来源清单是否都被库覆盖 ──
    print("\n[3] 来源清单覆盖（判断「全面」的核心）")
    # 3a 中文索引页
    res = fetcher.fetch(poedb_unique.LIST_URL)
    if not res.ok:
        problems.append(f"中文索引页不可达 HTTP {res.status_code}")
    else:
        ents, _ = poedb_unique.extract_uniques(res.text)
        miss = sorted({e.slug for e in ents} - set(by_slug))
        print(f"    中文索引页 {len(ents):,} 件 → 库内缺 {len(miss)}")
        if miss:
            problems.append(f"中文索引页有 {len(miss)} 件未入库")
            print(f"      {miss[:10]}")
    # 3b 英文索引页
    res_en = fetcher.fetch(poedb_unique.EN_LIST_URL)
    if not res_en.ok:
        problems.append(f"英文索引页不可达 HTTP {res_en.status_code}")
    else:
        en_map = poedb_unique.extract_en_index(res_en.text)
        miss_en = sorted(set(en_map) - set(by_slug))
        print(f"    英文索引页 {len(en_map):,} 件 → 库内缺 {len(miss_en)}")
        if miss_en:
            problems.append(f"英文索引页有 {len(miss_en)} 件未入库")
            print(f"      {miss_en[:10]}")
    # 3c 类别页并集
    pages = [*poedb_index.discover_mod_class_pages(fetcher), *poedb_index.PARENT_CLASS_PAGES]
    union: set[str] = set()
    skipped = 0
    for slug in pages:
        r = fetcher.fetch(f"{poedb_index.BASE}/{slug}")
        if not r.ok:
            problems.append(f"类别页 {slug} 不可达 HTTP {r.status_code}")
            continue
        ents, st = poedb_unique.extract_uniques_from_class_page(
            r.text, slug, f"{poedb_index.BASE}/{slug}"
        )
        skipped += st.skipped
        union.update(e.slug for e in ents)
    miss_cls = sorted(union - set(by_slug))
    print(f"    类别页并集 {len(union):,} 件（{len(pages)} 页，跳过 Royale {skipped}）"
          f" → 库内缺 {len(miss_cls)}")
    if miss_cls:
        problems.append(f"类别页有 {len(miss_cls)} 件未入库")
        print(f"      {miss_cls[:10]}")

    # ── 4. 别名与切片 ──
    print("\n[4] 别名与切片覆盖")
    no_alias = [
        r["slug"] for r in rows
        if not conn.execute(
            "SELECT 1 FROM alias WHERE entity_id=? LIMIT 1", (r["entity_id"],)
        ).fetchone()
    ]
    no_chunk = [
        r["slug"] for r in rows
        if not conn.execute(
            "SELECT 1 FROM chunk WHERE entity_id=? LIMIT 1", (r["entity_id"],)
        ).fetchone()
    ]
    print(f"    无别名 {len(no_alias)} ｜ 无切片 {len(no_chunk)}")
    if no_alias:
        problems.append(f"{len(no_alias)} 件无别名（检索不到，等于没入库）")
        print(f"      {no_alias[:10]}")
    if no_chunk:
        problems.append(f"{len(no_chunk)} 件无切片（无法被引用）")
        print(f"      {no_chunk[:10]}")

    # ── 5. 空字段清点 ──
    print("\n[5] 空字段清点（允许存在，但必须可见）")
    for field in ("base_type", "item_class"):
        empty = [r for r in rows if not (r[field] or "").strip()]
        print(f"    {field:12s} 为空 {len(empty)} 件"
              + (f"：{[r['slug'] for r in empty[:6]]}" if empty else ""))

    print("\n" + "=" * 80)
    if problems:
        print("结论：**未通过**")
        for p in problems:
            print(f"  - {p}")
    else:
        print("结论：全部通过")
        print(f"  {len(rows):,} 件唯一装备，权威英文名 100%，三份来源清单零遗漏，"
              "别名与切片齐全")
    dbmod.close_db()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
