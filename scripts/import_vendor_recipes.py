#!/usr/bin/env python3
"""导入**商店配方**（POEDB `/cn/Vendor_recipe_system`，第五十一轮）。

补的是通货 / 精华 / 圣甲虫三个域 `partial_note` 里都写着的「获取途径未采集」的一半：
**怎么换**。掉落来源（怎么刷）仍未采集——那是另一回事，边界写在覆盖声明里。

这一页**一直在缓存里**（179KB、21 张表、201 行），从来没被抽取过。
本轮先翻缓存、后动手，因此**没有去抓 113 张通货详情页**。

## 已知不全：详情页上还有列表页没有的配方

2026-09-01（第五十二轮）为验证掉落来源抓了一张 `/cn/Chaos_Orb`，顺带发现
**它自己的「商店配方」区块里有两条不在这 201 条里**
（`60–74 物等整套 → 混沌石`、`1x 阿兹里的秘宝库 → 混沌石`）。
也就是说这一维**只覆盖了列表页那一份**。补齐要逐个抓 113 张详情页（每张约 1MB），
与命运卡那一维可以一趟抓完——两者都还没做，这里记下来免得下次以为已经全了。

    python scripts/import_vendor_recipes.py --dry-run   # 只清点
    python scripts/import_vendor_recipes.py             # 导入
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from entities.taxonomy import Source  # noqa: E402
from kb.fetcher import cache_path_for  # noqa: E402
from kb.sources.poedb_vendor import VendorRecipeError, parse_vendor_recipes  # noqa: E402

PAGE_URL = "https://poedb.tw/cn/Vendor_recipe_system"

#: 数量护栏。2026-09-01 实测 201 条。
MIN_RECIPES = 150


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只清点，不写库")
    parser.add_argument("--allow-shrink", action="store_true",
                        help="显式允许低于下限（仅人工确认上游变化后使用）")
    args = parser.parse_args()
    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()

    path = cache_path_for(cfg.cache_dir, PAGE_URL)
    if not path.is_file():
        print(f"[中止] 页面未缓存：{PAGE_URL}\n"
              "先跑 `python -m kb.pipeline --category currency --harvest` 之类把它抓下来")
        return 2

    started = time.monotonic()
    try:
        recipes = parse_vendor_recipes(path.read_text(encoding="utf-8", errors="ignore"))
    except VendorRecipeError as exc:
        print(f"[中止] 页面结构与预期不符：{exc}")
        return 2
    elapsed_ms = int((time.monotonic() - started) * 1000)

    by_cat = Counter(r.category for r in recipes)
    print(f"商店配方 {len(recipes)} 条 / {len(by_cat)} 类，解析 {elapsed_ms} ms")
    for cat, count in by_cat.most_common(6):
        print(f"    {count:3d}  {cat}")
    if len(by_cat) > 6:
        print(f"    …另有 {len(by_cat) - 6} 类")

    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0
    if len(recipes) < MIN_RECIPES and not args.allow_shrink:
        print(f"[中止] 只有 {len(recipes)} 条，低于下限 {MIN_RECIPES}（--allow-shrink 可强制）")
        return 2

    priority = int(cfg.get("sources.poedb.priority", 1) or 1)
    fetched_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    run = conn.execute(
        "INSERT INTO ingest_run(source,category,started_at,total) "
        "VALUES (?,?,datetime('now'),?)",
        (Source.POEDB.value, "poedb_vendor_recipe", len(recipes)),
    )
    run_id = int(run.lastrowid)
    conn.execute("SAVEPOINT vendor_recipes")
    try:
        conn.execute("DELETE FROM poedb_vendor_recipe")
        conn.executemany(
            "INSERT INTO poedb_vendor_recipe(category,block_index,row_index,result,"
            "ingredients,note,source,source_priority,url,fetched_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            [
                (r.category, r.block_index, r.row_index, r.result, r.ingredients,
                 r.note, Source.POEDB.value, priority, PAGE_URL, fetched_at)
                for r in recipes
            ],
        )
        conn.execute("RELEASE vendor_recipes")
    except Exception:
        conn.execute("ROLLBACK TO vendor_recipes")
        conn.execute("RELEASE vendor_recipes")
        raise
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
        "elapsed_ms=?,note=? WHERE run_id=?",
        (len(recipes), elapsed_ms, f"{len(by_cat)} 类商店配方", run_id),
    )
    conn.commit()
    print(f"已写入 {len(recipes)} 条商店配方（run_id={run_id}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
