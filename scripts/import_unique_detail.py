#!/usr/bin/env python3
"""用唯一装备**详情页**补齐列表页漏掉的词条。

## 为什么要补

入库主源是 `/cn/Unique_item`（一页 2.5MB、1342 件全在里面），这一直是对站点最友好的
做法。但 `scripts/verify_against_detail.py` 抽样核对时反复报同一件事，台账里也记着：

    详情页有而列表页没有的词条：不可改变
    详情页有而列表页没有的词条：施法速度加快 (35-40)%

实测（2026-08-20，55 张缓存详情页）：48 件两侧完全一致，**7 件详情页多出词条**，
多出来的以「不可改变」为主——那是「这件不能被改造」的事实，**制作类问题正要用它**，
而列表页上没有。所以这一轮把 1342 张详情页抓齐，逐件比对后合并。

## 合并的前提是「详情页是列表页的超集」

判据在 `kb.sources.poedb_unique.compare_detail`：名字/底材/需求任一不一致，
或者列表页有而详情页没有的词条，都说明两侧讲的可能不是同一版本的物品——
那时**不合并**，登记成 `source_conflict`（`kind='unique_review'`，待人工核对）。
宁可少一条词缀，也不要把另一版本的词条并到这件物品上（与第三十二轮拒绝 8 颗
对不上的宝石是同一条纪律）。

合并时**整份换成详情页的词缀**而不是把多出来的插进去：详情页给的是权威顺序，
而 `entity.url` 本来就指向详情页——换过去之后引用与正文才真正对得上。

    python scripts/import_unique_detail.py --dry-run   # 只比对，不写库
    python scripts/import_unique_detail.py             # 合并并重建卡片
    python scripts/import_unique_detail.py --fetch      # 缺页时联网补抓（1342 页，遵守限速）
    python scripts/import_unique_detail.py --slug Headhunter   # 只处理一件，调试用
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from entities.models import Entity  # noqa: E402
from entities.taxonomy import EntityType, Source  # noqa: E402
from kb.fetcher import Fetcher, cache_path_for  # noqa: E402
from kb.sources.poedb_unique import (  # noqa: E402
    DetailDiff,
    DetailPage,
    StructureError,
    compare_detail,
    parse_detail_page,
)

CONFLICT_KIND = "unique_review"
GAP_QUESTION = "唯一装备的完整词缀（详情页）"


def load_uniques(conn, slugs: list[str]) -> list:  # noqa: ANN001
    sql = "SELECT * FROM entity WHERE entity_type = ?"
    args: list = [EntityType.UNIQUE_ITEM.value]
    if slugs:
        sql += f" AND slug IN ({','.join('?' * len(slugs))})"
        args.extend(slugs)
    return list(conn.execute(sql + " ORDER BY slug", args))


def page_html(url: str, *, fetcher: Fetcher | None) -> str | None:
    """详情页 HTML：先看缓存，再看要不要联网。取不到返回 None。"""
    path = cache_path_for(get_config().cache_dir, url)
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="ignore")
    if fetcher is None:
        return None
    try:
        return fetcher.fetch(url).text
    except OSError as exc:  # 单页失败只影响这一件
        print(f"  [提示] {url} 抓取失败：{exc}")
        return None


def parse_or_none(html: str) -> DetailPage | None:
    """解析详情页；结构不符时返回 None 而不是让整批崩掉。

    1342 张页面里一定有不长这样的（实测第一版没捕获，`--dry-run` 直接抛
    `StructureError`）。**单件的结构问题不该让另外 1341 件进不来**，
    但也不能静默——调用方按「页面结构异常」计数并登记。
    """
    try:
        return parse_detail_page(html)
    except StructureError:
        return None


def merged_entity(entity: Entity, detail_mods: list) -> Entity:  # noqa: ANN001
    """把词缀整份换成详情页的，并按新词缀重算检索关键词。

    关键词必须跟着换：它是向量通道关闭时的语义检索补偿（需求 3.12），
    新词条不进关键词就等于「入库了但检索不到」——这个项目在宝石那两轮
    反复撞的就是这类「没有出口」的入库。
    """
    from kb.sources.poedb_unique import _make_keywords  # noqa: PLC0415

    return entity.model_copy(update={
        "mods": detail_mods,
        "search_keywords": _make_keywords(
            entity.name_zh, entity.base_type or "", detail_mods
        ),
    })


def record_conflict(conn, row, diff: DetailDiff) -> None:  # noqa: ANN001
    """两侧对不上就登记待核对。**不是**已确认冲突：更可能的解释是版本不同。"""
    conn.execute(
        """
        INSERT INTO source_conflict(subject, field, kind, source_a, value_a, url_a,
                                    source_b, value_b, url_b, note, cause,
                                    first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
        ON CONFLICT(subject, field, source_a, source_b) DO UPDATE SET
            value_a=excluded.value_a, value_b=excluded.value_b,
            note=excluded.note, cause=excluded.cause, last_seen=datetime('now')
        """,
        (
            f"{row['name_zh']}（{row['slug']}）", "详情页词缀", CONFLICT_KIND,
            f"{Source.POEDB.value} 列表页", str(len(diff.list_only)),
            "https://poedb.tw/cn/Unique_item",
            f"{Source.POEDB.value} 详情页", str(len(diff.detail_only)), row["url"],
            "；".join(diff.blocking)[:400], "detail_page_mismatch",
        ),
    )


def main() -> int:  # noqa: PLR0915
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只比对，不写库")
    parser.add_argument("--fetch", action="store_true",
                        help="缺页时联网补抓（每件一页，遵守 robots 与限速）")
    parser.add_argument("--slug", action="append", default=[], help="只处理这些 slug")
    args = parser.parse_args()
    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()

    fetcher = None
    if args.fetch:
        fetcher = Fetcher(
            cache_dir=cfg.cache_dir,
            user_agent=str(cfg.get("crawl.user_agent")),
            requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
            timeout=float(cfg.get("crawl.timeout_seconds", 60)),
            cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
        )

    from kb.pipeline import _entity_from_row  # noqa: PLC0415

    rows = load_uniques(conn, args.slug)
    if not rows:
        print("[中止] 库里没有唯一装备实体，先跑 python -m kb.pipeline --category unique_item")
        return 2

    started = time.monotonic()
    stat = Counter()
    merged: list[tuple[Entity, DetailDiff]] = []
    conflicts: list[tuple] = []
    no_page: list[str] = []
    added_lines = Counter()
    for row in rows:
        html = page_html(row["url"], fetcher=fetcher)
        if not html:
            stat["未取到详情页"] += 1
            no_page.append(row["name_zh"])
            continue
        detail = parse_or_none(html)
        if detail is None:
            stat["页面结构异常"] += 1
            continue
        entity = _entity_from_row(row)
        attrs = json.loads(row["attributes"] or "{}")
        if not detail.mods:
            # 实测这一支全是**片段 / 契约**类物品：两侧都是 0 条词缀，那是一致而不是缺陷。
            # 初版把它记成「详情页词缀为空」，读起来像抽取坏了——**计数的措辞也会误导排查**，
            # 所以按列表页那侧是否也为空分开记。
            has_listed = any(m.is_factual for m in entity.mods)
            stat["两侧都没有词缀（片段/契约类）" if not has_listed else "详情页取不到词缀"] += 1
            continue
        diff = compare_detail(
            detail,
            name_zh=row["name_zh"],
            base_type=row["base_type"] or "",
            requirement_raw=(attrs.get("requirement") or {}).get("raw", "") or "",
            mod_texts=[m.text for m in entity.mods if m.is_factual],
        )
        if diff.blocking:
            stat["两侧对不上"] += 1
            conflicts.append((row, diff))
            continue
        if not diff.detail_only:
            stat["两侧一致"] += 1
            continue
        stat["详情页更全"] += 1
        for text in diff.detail_only:
            added_lines[text] += 1
        merged.append((merged_entity(entity, detail.mods), diff))

    elapsed_ms = int((time.monotonic() - started) * 1000)
    print(f"唯一装备 {len(rows)} 件，比对 {elapsed_ms} ms：{dict(sorted(stat.items()))}")
    if added_lines:
        top = "、".join(f"{text}×{count}" for text, count in added_lines.most_common(5))
        print(f"补进来的词条 {sum(added_lines.values())} 条（{len(added_lines)} 种）：{top}")
    if conflicts:
        print(f"两侧对不上 {len(conflicts)} 件，登记待人工核对：")
        for row, diff in conflicts[:5]:
            print(f"  - {row['name_zh']}：{'；'.join(diff.blocking)[:110]}")
    if no_page:
        print(f"未取到详情页 {len(no_page)} 件：{'、'.join(no_page[:5])}"
              f"{' 等' if len(no_page) > 5 else ''}（加 --fetch 可补抓）")
    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0

    run = conn.execute(
        "INSERT INTO ingest_run(source,category,started_at,total) "
        "VALUES (?,?,datetime('now'),?)",
        (Source.POEDB.value, "unique_detail", len(merged)),
    )
    run_id = int(run.lastrowid)
    conn.execute("SAVEPOINT unique_detail")
    try:
        chunks = write_merged(conn, [entity for entity, _ in merged])
        for row, diff in conflicts:
            record_conflict(conn, row, diff)
        gap_rows = record_gaps(conn, no_page=no_page, conflicts=conflicts)
        conn.execute("RELEASE unique_detail")
    except Exception:
        conn.execute("ROLLBACK TO unique_detail")
        conn.execute("RELEASE unique_detail")
        raise
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
        "elapsed_ms=?,note=? WHERE run_id=?",
        (len(merged), elapsed_ms,
         f"{len(merged)} 件补进 {sum(added_lines.values())} 条词条；"
         f"待核对 {len(conflicts)}；台账 {gap_rows} 组", run_id),
    )
    conn.commit()
    print(f"已合并 {len(merged)} 件（卡片重建 {chunks} 张）；"
          f"待人工核对 {len(conflicts)} 件，台账 {gap_rows} 组（run_id={run_id}）")
    return 0


def write_merged(conn, entities: list[Entity]) -> int:  # noqa: ANN001
    """写回实体并重渲染这些物品的卡片。

    **只重建被改动的那几件**：`fts_mod.clear_index` 只吃前缀，所以这里按
    `entity_id` 精确删——唯一装备有 1342 件，为了改 100 件而清空整类索引
    是那个函数注释里警告过的坑（曾把 24,418 张词缀卡一起清空）。
    """
    from kb.pipeline import _upsert_entity  # noqa: PLC0415
    from kb.render import to_chunks  # noqa: PLC0415
    from retrieval import alias as alias_mod  # noqa: PLC0415
    from retrieval import fts as fts_mod  # noqa: PLC0415

    chunks = 0
    for entity in entities:
        _upsert_entity(conn, entity)
        conn.execute("DELETE FROM chunk_fts WHERE entity_id = ?", (entity.entity_id,))
        conn.execute("DELETE FROM chunk WHERE entity_id = ?", (entity.entity_id,))
        chunks += fts_mod.index_chunks(conn, to_chunks(entity))
    if entities:
        # 关键词变了，别名表里的名字没变——但重新索引一次是幂等的，
        # 漏掉反而会让新词条进不了别名侧的检索
        alias_mod.index_aliases(conn, entities)
    return chunks


def record_gaps(conn, *, no_page: list[str], conflicts: list[tuple]) -> int:  # noqa: ANN001
    """按成因登记未合并项。两类分开——一类是补抓，一类是人工核对。"""
    points: list[tuple[str, int]] = []
    if no_page:
        points.append((
            f"[no_detail_page] {len(no_page)} 件唯一装备的详情页未抓取："
            f"{'、'.join(no_page[:3])}{' 等' if len(no_page) > 3 else ''}"
            "（加 --fetch 可补抓）", len(no_page)))
    if conflicts:
        names = [row["name_zh"] for row, _ in conflicts]
        points.append((
            f"[detail_mismatch] {len(names)} 件唯一装备两侧词缀对不上，未合并："
            f"{'、'.join(names[:3])}{' 等' if len(names) > 3 else ''}"
            "（见 source_conflict 的 unique_review）", len(names)))
    return dbmod.record_ingest_gaps(
        conn, question=GAP_QUESTION, category="partial/unique_item", points=points,
    )


if __name__ == "__main__":
    raise SystemExit(main())
