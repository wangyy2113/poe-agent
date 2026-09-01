#!/usr/bin/env python3
"""导入**技能 × 辅助的搭配关系**（POEDB 详情页的「被辅助 / 可辅助」两个区块）。

「这个技能能配哪些辅助 / 多重投射能不能辅助冰霜新星」是最常问的一类问题，
而在这之前本库完全答不了：辅助宝石的 `skill_types` 是空的，也没有搭配表。

**只入两侧都确认的对。** 页面给了两个方向，双向一致性本身就是这一维的对账：
2026-09-01 实测 56,895 对两侧一致（入库），另有 2,201 对只有一侧说（按成因进台账）。
单侧来源不是冲突，但不能当成两侧确认过的事实——与唯一装备详情页
「两侧对不上就拒绝合并」是同一条。

**为什么另开脚本而不并进 `import_gem_stats.py`：** 那个脚本的护栏是
「某颗宝石逐级数值对不上就整颗拒绝」，而这一维的判据是**成对的双向一致**。
两套护栏塞进同一个 savepoint，会让「因为 A 拒绝」和「因为 B 拒绝」混在一起，
台账也就说不清该去补哪一边。

    python scripts/import_gem_supports.py --dry-run   # 只清点，不写库
    python scripts/import_gem_supports.py             # 导入
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
from kb.sources.poedb_gem_page import (  # noqa: E402
    GemPageError,
    parse_gem_support_links,
)
from kb.sources.pob_skills import poedb_url  # noqa: E402

#: 数量护栏：低于这个数就是解析或抓取出了问题，拒绝替换快照。
MIN_PAIRS = 50000

GAP_QUESTION = "技能与辅助的搭配关系"


def collect(conn) -> tuple[set, set, dict, list]:  # noqa: ANN001
    """两个方向各自的 (技能 slug, 辅助 slug) 集合。"""
    cfg = get_config()
    rows = [
        (r["gem_id"], r["poedb_slug"], r["name_zh"], r["gem_kind"])
        for r in conn.execute(
            "SELECT gem_id, poedb_slug, name_zh, gem_kind FROM pob_skill_gem "
            "WHERE poedb_slug IS NOT NULL ORDER BY poedb_slug"
        )
    ]
    by_slug = {slug: (gem_id, zh, kind) for gem_id, slug, zh, kind in rows}
    forward: set[tuple[str, str]] = set()
    reverse: set[tuple[str, str]] = set()
    errors: list[tuple[str, str]] = []
    for _gem_id, slug, _zh, _kind in rows:
        path = cache_path_for(cfg.cache_dir, poedb_url(slug))
        if not path.is_file():
            continue
        try:
            links = parse_gem_support_links(
                path.read_text(encoding="utf-8", errors="ignore"), poedb_slug=slug)
        except GemPageError as exc:
            errors.append((slug, str(exc)))
            continue
        forward.update((slug, s) for s in links.supported_by)
        reverse.update((k, slug) for k in links.can_support)
    return forward, reverse, by_slug, errors


def classify(pairs: set, by_slug: dict) -> Counter:
    """单侧对的成因。分类是为了让台账说得清「该去补哪一边」。"""
    out: Counter = Counter()
    for skill, support in pairs:
        if skill not in by_slug or support not in by_slug:
            out["对方 slug 不在库（未桥接的历史/未上线条目）"] += 1
        elif by_slug[skill][2] == "support":
            out["被辅助方本身是辅助宝石（可辅助那侧只列主动技能）"] += 1
        else:
            out["两边都在库，但只有一侧的清单收录了这一对"] += 1
    return out


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
    forward, reverse, by_slug, errors = collect(conn)
    both = forward & reverse
    only_f, only_r = forward - reverse, reverse - forward
    elapsed_ms = int((time.monotonic() - started) * 1000)

    print(f"解析 {elapsed_ms} ms：被辅助 {len(forward)} 对 / 可辅助 {len(reverse)} 对"
          f"｜解析错误 {len(errors)}")
    print(f"**两侧一致 {len(both)} 对**（入库）")
    print(f"只在被辅助侧 {len(only_f)}：{dict(classify(only_f, by_slug))}")
    print(f"只在可辅助侧 {len(only_r)}：{dict(classify(only_r, by_slug))}")
    for slug, msg in errors[:5]:
        print(f"  [结构] {slug}：{msg}")

    # 只入两侧都在库的对——外键要求两端都存在。`url` 取技能那一侧的详情页
    # （这一对是在那一页上读到的），所以顺带把 slug 一起带出来。
    rows = [
        (by_slug[skill][0], by_slug[support][0], skill)
        for skill, support in sorted(both)
        if skill in by_slug and support in by_slug
    ]
    dropped = len(both) - len(rows)
    if dropped:
        print(f"  （其中 {dropped} 对有一端不在库，不入库）")

    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0
    if len(rows) < MIN_PAIRS and not args.allow_shrink:
        print(f"[中止] 只有 {len(rows)} 对可入库，低于下限 {MIN_PAIRS}"
              "（--allow-shrink 可强制）")
        return 2

    priority = int(cfg.get("sources.poedb.priority", 1) or 1)
    run = conn.execute(
        "INSERT INTO ingest_run(source,category,started_at,total) "
        "VALUES (?,?,datetime('now'),?)",
        (Source.POEDB.value, "poedb_gem_support_pair", len(rows)),
    )
    run_id = int(run.lastrowid)
    conn.execute("SAVEPOINT gem_supports")
    try:
        conn.execute("DELETE FROM poedb_gem_support_pair")
        conn.executemany(
            "INSERT INTO poedb_gem_support_pair(skill_gem_id,support_gem_id,source,"
            "source_priority,url,fetched_at,updated_at) "
            "VALUES (?,?,?,?,?,datetime('now'),datetime('now'))",
            [(gem, sup, Source.POEDB.value, priority, poedb_url(slug))
             for gem, sup, slug in rows],
        )
        gap_rows = record_gaps(conn, only_f=only_f, only_r=only_r, by_slug=by_slug)
        conn.execute("RELEASE gem_supports")
    except Exception:
        conn.execute("ROLLBACK TO gem_supports")
        conn.execute("RELEASE gem_supports")
        raise
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
        "elapsed_ms=?,note=? WHERE run_id=?",
        (len(rows), elapsed_ms,
         f"{len(rows)} 对两侧一致；单侧 {len(only_f) + len(only_r)} 对未入库；"
         f"台账 {gap_rows} 组", run_id),
    )
    conn.commit()
    print(f"已写入 {len(rows)} 对搭配关系（run_id={run_id}）；台账 {gap_rows} 组")
    return 0


def record_gaps(conn, *, only_f: set, only_r: set, by_slug: dict) -> int:  # noqa: ANN001
    """单侧对按成因登记。**分方向记**：两侧缺的东西要补的地方不一样。"""
    points: list[tuple[str, int]] = []
    for label, pairs in (("被辅助", only_f), ("可辅助", only_r)):
        for cause, count in classify(pairs, by_slug).most_common():
            points.append((f"[one_sided] 只在「{label}」侧：{cause}", count))
    return dbmod.record_ingest_gaps(
        conn, question=GAP_QUESTION, category="skill_gem", points=points)


if __name__ == "__main__":
    raise SystemExit(main())
