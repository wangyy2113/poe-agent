#!/usr/bin/env python3
"""离线导入 PoB 底材注册表（全装备类别）。

读取本地 PoB ``Data/Bases/*.lua`` 与已经缓存的 POEDB 中英文类别页。
**默认不发起网络请求**——页面由 `kb.pipeline` 或本脚本的 `--fetch` 预先抓好；
PoB commit 未钉住、页面缺失或跨来源名称多解时拒绝。

桥接不上的底材分三族处理（判据见 `kb/sources/pob_bases.py` 的模块 docstring）：
PoB 伪条目按规则排除，变体展开与实验底材跳过并**写入缺口台账**——
静默丢弃等于让知识库不知道自己缺什么。

    python scripts/import_pob_bases.py --dry-run          # 只清点
    python scripts/import_pob_bases.py                    # 全类别导入
    python scripts/import_pob_bases.py --category belt     # 只导一个类别
    python scripts/import_pob_bases.py --fetch            # 缺页时联网补抓（遵守限速）
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
from entities.taxonomy import Source  # noqa: E402
from kb.fetcher import Fetcher, cache_path_for  # noqa: E402
from kb.sources.pob import PoBNotFound, PoBRepo, load_repo  # noqa: E402
from kb.sources.pob_bases import (  # noqa: E402
    BODY_BASE_FILE,
    CATEGORIES,
    CATEGORY_BY_KEY,
    POEDB_BODY_CN_URL,
    POEDB_BODY_EN_URL,
    SKIP_PSEUDO,
    BaseCategory,
    BaseItem,
    BaseStructureError,
    BodyArmourBase,
    SkippedBase,
    extract_body_armour_bases,
    extract_category_bases,
)

#: 全类别导入的绝对下限。2026-08-18 实测可桥接 1022 条，留出上游删项的余量。
MIN_TOTAL_BASES = 900
#: 单类别下限：胸甲曾单独导入，保留它的护栏语义。
MIN_BODY_BASES = 100
MAX_RELATIVE_DROP = 0.20


def to_row(base: BaseItem, repo: PoBRepo, priority: int) -> tuple:
    return (
        base.base_id,
        base.name_en,
        base.name_zh,
        base.poedb_slug,
        base.category_key,
        base.poedb_page,
        base.item_class,
        base.subtype,
        base.socket_limit,
        json.dumps(base.tags, ensure_ascii=False),
        json.dumps(base.influence_tags, ensure_ascii=False, sort_keys=True),
        base.armour_tag,
        base.modifier_page,
        base.implicit_text,
        json.dumps(base.implicit_mod_types, ensure_ascii=False),
        json.dumps(base.implicit_ids, ensure_ascii=False),
        json.dumps(base.stats, ensure_ascii=False, sort_keys=True),
        base.stats_kind,
        json.dumps(base.requirements, ensure_ascii=False, sort_keys=True),
        base.source_file,
        Source.POB.value,
        priority,
        repo.blob_url(base.source_file),
        base.poedb_url,
        repo.commit,
        repo.version_label,
        repo.game_version,
        base.content_hash,
    )


def validate_snapshot_size(
    conn, incoming: int, *, allow_shrink: bool = False, minimum: int | None = None  # noqa: ANN001
) -> None:
    """数量护栏：低于绝对下限或相对大幅缩减时拒绝替换。

    `minimum` 缺省用全类别下限；只导单个类别时由调用方传入该类别的下限。
    """
    floor = MIN_TOTAL_BASES if minimum is None else minimum
    existing = int(conn.execute("SELECT count(*) FROM pob_base_item").fetchone()[0])
    reason = ""
    if incoming < floor:
        reason = f"低于绝对下限 {floor}（incoming={incoming}）"
    elif existing and incoming < existing * (1 - MAX_RELATIVE_DROP):
        reason = f"相对缩减超过 {MAX_RELATIVE_DROP:.0%}（{existing} -> {incoming}）"
    if reason and not allow_shrink:
        raise BaseStructureError(f"拒绝替换底材快照：{reason}")


def replace_snapshot(
    conn, bases: list[BaseItem], repo: PoBRepo, priority: int,  # noqa: ANN001
    *, categories: list[str] | None = None,
) -> int:
    """整体替换快照。`categories` 非空时只替换这些类别，其余类别的行保留。"""
    rows = [to_row(base, repo, priority) for base in bases]
    if categories:
        conn.executemany(
            "DELETE FROM pob_base_item WHERE category_key = ?",
            [(key,) for key in categories],
        )
    else:
        conn.execute("DELETE FROM pob_base_item")
    conn.executemany(
        """
        INSERT INTO pob_base_item(
            base_id,name_en,name_zh,poedb_slug,category_key,poedb_page,item_class,
            subtype,socket_limit,tags,influence_tags,armour_tag,modifier_page,
            implicit_text,implicit_mod_types,implicit_ids,stats,stats_kind,
            requirements,source_file,source,source_priority,pob_url,poedb_url,
            source_commit,pob_version,game_version,content_hash,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        rows,
    )
    return len(rows)


def record_skipped(conn, skipped: list[SkippedBase]) -> int:  # noqa: ANN001
    """把跳过的底材写进缺口台账，按 (类别, 原因) 聚合。

    不逐条写：41 条底材会淹掉台账里真正的采集待办。聚合成「哪个类别、什么原因、
    多少条、样例是谁」，按频次排序仍然能看出该补什么。
    `pob_pseudo` 不写——PoB 的伪条目不是知识缺口，POEDB 没有它们是正确的。
    """
    groups: dict[tuple[str, str], list[SkippedBase]] = {}
    for item in skipped:
        if item.reason == SKIP_PSEUDO:
            continue
        groups.setdefault((item.category, item.reason), []).append(item)
    written = 0
    for category, by_reason in sorted(_by_category(groups).items()):
        points = [
            (
                f"[{reason}] {category} 类别有 {len(items)} 条 PoB 底材无法与 POEDB 桥接："
                f"{'、'.join(i.name_en for i in items[:3])}"
                f"{' 等' if len(items) > 3 else ''}（{items[0].detail}）",
                len(items),
            )
            for reason, items in sorted(by_reason.items())
        ]
        written += dbmod.record_ingest_gaps(
            conn, question=f"{category} 底材的完整清单",
            category="not_collected/base_item", points=points,
        )
    return written


def _by_category(
    groups: dict[tuple[str, str], list[SkippedBase]],
) -> dict[str, dict[str, list[SkippedBase]]]:
    """`(类别, 原因)` → 按类别分组：台账整组替换的单位是 question，也就是类别。"""
    out: dict[str, dict[str, list[SkippedBase]]] = {}
    for (category, reason), items in groups.items():
        out.setdefault(category, {})[reason] = items
    return out


def _page_html(category: BaseCategory, *, fetcher: Fetcher | None) -> dict[str, tuple[str, str]]:
    """取该类别全部页面的中英文 HTML。缺页时按 `--fetch` 决定补抓还是报错。"""
    cfg = get_config()
    out: dict[str, tuple[str, str]] = {}
    for page in category.pages:
        htmls: list[str] = []
        for url in (page.cn_url, page.en_url):
            path = cache_path_for(cfg.cache_dir, url)
            if path.is_file():
                htmls.append(path.read_text(encoding="utf-8", errors="ignore"))
                continue
            if fetcher is None:
                raise BaseStructureError(
                    f"缺少本地缓存 {url}（{path.name}）；加 --fetch 允许联网补抓"
                )
            htmls.append(fetcher.fetch(url).text)
        out[page.slug] = (htmls[0], htmls[1])
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pob-path", default=None)
    parser.add_argument("--dry-run", action="store_true", help="只解析和体检，不写库")
    parser.add_argument(
        "--category", action="append", default=None,
        help=f"只导入指定类别，可重复。可选：{', '.join(c.key for c in CATEGORIES)}",
    )
    parser.add_argument("--fetch", action="store_true",
                        help="缺少 POEDB 页面缓存时联网补抓（遵守 robots 与限速）")
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="显式允许低于绝对下限或相对大幅缩减（仅人工确认上游变化后使用）",
    )
    args = parser.parse_args()
    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")

    if args.category:
        unknown = [k for k in args.category if k not in CATEGORY_BY_KEY]
        if unknown:
            print(f"[中止] 未知类别 {unknown}，可选 {list(CATEGORY_BY_KEY)}")
            return 2
        categories = [CATEGORY_BY_KEY[k] for k in args.category]
    else:
        categories = list(CATEGORIES)

    fetcher = None
    if args.fetch:
        fetcher = Fetcher(
            cache_dir=cfg.cache_dir,
            user_agent=str(cfg.get("crawl.user_agent")),
            requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
            timeout=float(cfg.get("crawl.timeout_seconds", 60)),
            cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
        )

    bases: list[BaseItem] = []
    skipped: list[SkippedBase] = []
    try:
        repo = load_repo(args.pob_path, cfg)
        if not repo.reproducible or len(repo.commit) != 40:
            raise BaseStructureError("PoB commit 未钉住，拒绝导入不可复现引用")
        started = time.monotonic()
        for category in categories:
            pages = _page_html(category, fetcher=fetcher)
            got, missed = extract_category_bases(category, repo.root, pages)
            bases.extend(got)
            skipped.extend(missed)
            print(f"  {category.key:8s} 入库 {len(got):4d}  跳过 {len(missed):2d}")
        elapsed_ms = int((time.monotonic() - started) * 1000)
    except (PoBNotFound, BaseStructureError, OSError) as exc:
        print(f"[中止] {exc}")
        return 2

    by_class = Counter(base.item_class for base in bases)
    by_reason = Counter(item.reason for item in skipped)
    print(f"\nPoB：{repo.version_label} commit={repo.commit}")
    print(f"底材：{len(bases)} 条（{len(categories)} 个类别），解析 {elapsed_ms} ms")
    print(f"ItemClass 分布：{dict(sorted(by_class.items()))}")
    print(f"跳过 {len(skipped)} 条：{dict(sorted(by_reason.items()))}")
    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0

    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()
    only = [c.key for c in categories] if args.category else None
    minimum = None
    if args.category:
        minimum = MIN_BODY_BASES if only == ["body"] else 1
    try:
        if not args.category:
            validate_snapshot_size(conn, len(bases), allow_shrink=args.allow_shrink)
        run = conn.execute(
            "INSERT INTO ingest_run(source,category,started_at,total) "
            "VALUES (?,?,datetime('now'),?)",
            (Source.POB.value, "pob_base_item", len(bases)),
        )
        run_id = int(run.lastrowid)
        conn.execute("SAVEPOINT pob_base_snapshot")
        try:
            written = replace_snapshot(
                conn, bases, repo, repo.priority(cfg), categories=only
            )
            gap_rows = record_skipped(conn, skipped)
            conn.execute("RELEASE pob_base_snapshot")
        except Exception:
            conn.execute("ROLLBACK TO pob_base_snapshot")
            conn.execute("RELEASE pob_base_snapshot")
            raise
        note = (
            f"{written} 条底材（{len(categories)} 类别）；"
            f"跳过 {len(skipped)} 条 {dict(sorted(by_reason.items()))}；"
            f"台账登记 {gap_rows} 组"
        )
        conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
            "elapsed_ms=?,note=? WHERE run_id=?",
            (written, elapsed_ms, note, run_id),
        )
        conn.commit()
    except BaseStructureError as exc:
        conn.rollback()
        print(f"[中止] {exc}")
        return 2
    total = int(conn.execute("SELECT count(*) FROM pob_base_item").fetchone()[0])
    print(f"已写入 {written} 条，表内共 {total} 条（run_id={run_id}）")
    print(f"跳过项已按 (类别, 原因) 聚合登记进缺口台账：{gap_rows} 组")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
