#!/usr/bin/env python3
"""导入**获取途径**（POEDB 详情页的 `Acquisition` 区块，第五十四轮）。

补的是唯一装备/通货/技能石三个域都写着「获取途径未采集」的一部分：
**命运卡与升级配方**。掉落来源（怎么刷）仍没有来源——第五十二轮实测过，
POEDB 详情页上没有怪物/区域掉落表。

**零新增抓取**：三类详情页第 32/34/53 轮都已抓齐并在缓存里。

    python scripts/import_acquisition.py --dry-run   # 只清点
    python scripts/import_acquisition.py             # 导入
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
from kb.sources.poedb_acquisition import (  # noqa: E402
    AcquisitionError,
    parse_acquisition,
)

#: 数量护栏。2026-09-01 实测 387 行。
MIN_ROWS = 300

GAP_QUESTION = "唯一装备与通货的获取途径"

#: 三类实体各自的 slug 来源。owner_kind 与 `poedb_acquisition` 的 CHECK 对齐。
_SOURCES: tuple[tuple[str, str], ...] = (
    ("unique_item",
     "SELECT name_zh, slug FROM entity WHERE entity_type='unique_item' AND slug IS NOT NULL"),
    ("currency",
     "SELECT name_zh, poedb_slug FROM poedb_currency WHERE poedb_slug IS NOT NULL"),
    ("skill_gem",
     "SELECT name_zh, poedb_slug FROM pob_skill_gem WHERE poedb_slug IS NOT NULL"),
)


def collect(conn, cache_dir: Path):  # noqa: ANN001, ANN201
    """扫三类详情页。返回 (行, 每类统计, 没有这一段的条数)。"""
    rows: list[tuple] = []
    stat: Counter = Counter()
    without: Counter = Counter()
    errors: list[str] = []
    for kind, sql in _SOURCES:
        for name_zh, slug in conn.execute(sql):
            path = cache_path_for(cache_dir, f"https://poedb.tw/cn/{slug}")
            if not path.is_file():
                without[f"{kind}:未缓存"] += 1
                continue
            try:
                routes = parse_acquisition(
                    path.read_text(encoding="utf-8", errors="ignore"))
            except AcquisitionError as exc:
                errors.append(f"{name_zh}({slug})：{exc}")
                continue
            if not routes:
                without[kind] += 1
                continue
            stat[kind] += len(routes)
            for index, route in enumerate(routes):
                rows.append((
                    slug, kind, index, route.source_zh, route.source_slug,
                    route.ingredient, route.offer_zh,
                    f"https://poedb.tw/cn/{slug}",
                ))
    return rows, stat, without, errors


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

    started = time.monotonic()
    rows, stat, without, errors = collect(conn, cfg.cache_dir)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    cards = sum(1 for r in rows if not r[5])
    recipes = len(rows) - cards
    print(f"获取途径 {len(rows)} 行，解析 {elapsed_ms} ms")
    print(f"  按类型：命运卡 {cards} 行 / 升级配方 {recipes} 行")
    print(f"  按实体：{dict(stat)}")
    print(f"  没有这一段的：{dict(without)}")
    for e in errors[:5]:
        print(f"  [结构] {e}")

    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0
    if len(rows) < MIN_ROWS and not args.allow_shrink:
        print(f"[中止] 只有 {len(rows)} 行，低于下限 {MIN_ROWS}（--allow-shrink 可强制）")
        return 2

    priority = int(cfg.get("sources.poedb.priority", 1) or 1)
    run = conn.execute(
        "INSERT INTO ingest_run(source,category,started_at,total) "
        "VALUES (?,?,datetime('now'),?)",
        (Source.POEDB.value, "poedb_acquisition", len(rows)),
    )
    run_id = int(run.lastrowid)
    conn.execute("SAVEPOINT acquisition")
    try:
        conn.execute("DELETE FROM poedb_acquisition")
        conn.executemany(
            "INSERT INTO poedb_acquisition(owner_slug,owner_kind,row_index,source_zh,"
            "source_slug,ingredient,offer_zh,url,source,source_priority,"
            "fetched_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            [(*r, Source.POEDB.value, priority) for r in rows],
        )
        gap_rows = record_gaps(conn, without)
        conn.execute("RELEASE acquisition")
    except Exception:
        conn.execute("ROLLBACK TO acquisition")
        conn.execute("RELEASE acquisition")
        raise
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
        "elapsed_ms=?,note=? WHERE run_id=?",
        (len(rows), elapsed_ms,
         f"命运卡 {cards} / 升级配方 {recipes}；台账 {gap_rows} 组", run_id),
    )
    conn.commit()
    print(f"已写入 {len(rows)} 行获取途径（run_id={run_id}）；台账 {gap_rows} 组")

    cards = reindex_unique_cards(conn)
    conn.commit()
    print(f"唯一装备卡片已重建 {cards} 张（获取途径现在写在卡上）")
    return 0


def reindex_unique_cards(conn) -> int:  # noqa: ANN001
    """把获取途径并进唯一装备的实体属性并重建卡片。

    **这一步是导入的一部分，不是可选项**：数据入库却没有出口是这个项目反复踩的坑
    （第二十九轮基础伤害、第五十四轮这一维本身都是先入库后补出口）。
    """
    from entities.taxonomy import EntityType  # noqa: PLC0415
    from kb import render as render_mod  # noqa: PLC0415
    from kb.pipeline import _entity_from_row, _upsert_entity  # noqa: PLC0415
    from retrieval import fts as fts_mod  # noqa: PLC0415

    by_slug: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT owner_slug, source_zh, ingredient FROM poedb_acquisition "
        "WHERE owner_kind='unique_item' ORDER BY owner_slug, row_index"
    ):
        by_slug.setdefault(r["owner_slug"], []).append(
            {"source_zh": r["source_zh"], "ingredient": r["ingredient"]})

    entities = []
    for row in conn.execute(
        "SELECT * FROM entity WHERE entity_type = ?", (EntityType.UNIQUE_ITEM.value,)
    ):
        entity = _entity_from_row(row)
        routes = by_slug.get(entity.slug or "")
        if routes:
            attrs = dict(entity.attributes)
            attrs["acquisition"] = routes
            entity = entity.model_copy(update={"attributes": attrs})
        entities.append(entity)

    for entity in entities:
        _upsert_entity(conn, entity)
    fts_mod.clear_index(conn, entity_prefix=f"{EntityType.UNIQUE_ITEM.value}:")
    return sum(
        fts_mod.index_chunks(conn, render_mod.to_chunks(entity)) for entity in entities
    )


def record_gaps(conn, without: Counter) -> int:  # noqa: ANN001
    """按实体类型登记「详情页上没有这一段」的条数。

    这不是待办而是**覆盖率**：八成唯一装备本来就没有 `Acquisition` 区块。
    记下来是为了让「查不到」在出口能说清是哪一种。
    """
    points = [
        (f"[no_acquisition_block] {kind} 有 {count} 项的详情页上没有 Acquisition 区块"
         "（不是漏抽，是页面本来就没有这一段）", count)
        for kind, count in without.most_common()
    ]
    return dbmod.record_ingest_gaps(
        conn, question=GAP_QUESTION, category="unique_item", points=points)


if __name__ == "__main__":
    raise SystemExit(main())
