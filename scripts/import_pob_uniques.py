"""把 PoB 唯一装备导入 `pob_unique` 表（P2 来源，用于与 POEDB 交叉校验）。

    python scripts/import_pob_uniques.py --dry-run
    python scripts/import_pob_uniques.py

**只有 `variant_scope='current'` 的词缀可用于回答。** PoB 的文本块里历史版本与
当前版本混在一起（1293 件里 752 件带 Variant 标记），冈姆的壮志同时写着 +500
（3.25 之前）与 +1000（当前）。历史版本单独存一份，仅供版本 diff 分析。
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
from kb.sources.pob import PoBNotFound, PoBRepo, load_repo  # noqa: E402
from kb.sources.pob_uniques import PoBUnique, extract_all, to_row_mods  # noqa: E402


def to_row(item: PoBUnique, repo: PoBRepo, priority: int) -> tuple:
    current = to_row_mods(item, "current")
    return (
        item.uid, item.name_en, item.base_type_en or None, item.source_file,
        item.league or None, item.drop_source or None,
        item.level_req,
        # 存**按变体过滤后**的隐式条数，不是 `Implicits: N` 的原始行数。
        # 那 N 行可能是同一条隐式的多个变体版本（Ashcaller 写 3，当前变体只有 1）。
        item.implicit_count_for("current"),
        item.limited_to, item.radius or None,
        json.dumps(item.variant_names, ensure_ascii=False),
        json.dumps(item.current_variants),
        json.dumps(item.obsolete_variants),
        json.dumps(current, ensure_ascii=False),
        json.dumps(item.ranges_fingerprint("current")),
        repo.source_name(), priority,
        repo.blob_url(item.source_file),
        repo.version_label, repo.game_version or None,
    )


def upsert(conn, rows: list[tuple]) -> int:  # noqa: ANN001
    conn.executemany(
        """
        INSERT INTO pob_unique(
            uid, name_en, base_type_en, source_file, league, drop_source,
            level_req, implicit_count, limited_to, radius,
            variant_names, current_variants, obsolete_variants,
            mods, ranges_fp,
            source, source_priority, url, pob_version, game_version, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(uid) DO UPDATE SET
            name_en=excluded.name_en, base_type_en=excluded.base_type_en,
            source_file=excluded.source_file, league=excluded.league,
            drop_source=excluded.drop_source, level_req=excluded.level_req,
            implicit_count=excluded.implicit_count, limited_to=excluded.limited_to,
            radius=excluded.radius, variant_names=excluded.variant_names,
            current_variants=excluded.current_variants,
            obsolete_variants=excluded.obsolete_variants,
            mods=excluded.mods, ranges_fp=excluded.ranges_fp,
            url=excluded.url, pob_version=excluded.pob_version,
            game_version=excluded.game_version, updated_at=datetime('now')
        """,
        rows,
    )
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pob-path", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-unpinned", action="store_true")
    args = ap.parse_args()

    cfg = get_config()
    setup_logging(cfg.logs_dir, str(cfg.get("logging.level", "INFO")), 14)

    try:
        repo = load_repo(args.pob_path, cfg)
    except PoBNotFound as exc:
        print(str(exc))
        return 2

    priority = repo.priority(cfg)
    print(f"PoB：{repo.root}（{repo.origin}）")
    print(f"版本：{repo.version_label} ｜ 游戏 {repo.game_version or '未知'} ｜ "
          f"commit {repo.short_commit or '未知'}")
    if not repo.reproducible and not args.allow_unpinned:
        print("\n[中止] 读不到 commit SHA，引用不可复现。要继续请加 --allow-unpinned")
        return 2

    started = time.time()
    items, problems = extract_all(repo.root)
    elapsed = time.time() - started
    print(f"\n抽取 {len(items)} 件唯一装备，{elapsed:.1f}s")

    # ── 变体统计：这一节是本次导入最需要盯的地方 ──
    with_var = [i for i in items if i.has_variants]
    all_obsolete = [i for i in items if i.has_variants and not i.current_variants]
    mixed = [i for i in items if i.obsolete_variants and i.current_variants]
    multi_current = [i for i in items if len(i.current_variants) > 1]
    print("\n【变体划分】")
    print(f"  无变体               : {len(items) - len(with_var):,} 件（全部词缀有效）")
    print(f"  有变体               : {len(with_var):,} 件"
          f"（{len(with_var) / len(items):.0%}）")
    print(f"    含历史版本需过滤   : {len(mixed):,} 件")
    print(f"    多个当前变体       : {len(multi_current):,} 件"
          f"（选择型，如 Impresence 的 5 种元素）")
    print(f"    全部变体都是历史   : {len(all_obsolete):,} 件"
          f"{'  ← 这类会没有可用词缀，需检查' if all_obsolete else ''}")
    for i in all_obsolete[:5]:
        print(f"      {i.name_en}: {i.variant_names}")
        problems.append(f"{i.name_en}：全部变体都带 Pre 标记，过滤后没有可用词缀")

    dropped = sum(
        len(i.all_mods) - len(i.mods_for("current"))
        for i in items
    )
    print(f"\n  过滤掉的历史版本词缀 : {dropped:,} 条"
          f"（不过滤就会把过期数值当现值输出）")

    print("\n【变体过滤抽样】")
    for name in ("Kaom's Heart", "The Anvil", "Impresence", "Doryani's Invitation"):
        it = next((x for x in items if x.name_en == name), None)
        if it is None:
            continue
        print(f"  {name}")
        print(f"    变体 {len(it.variant_names)} 个 → 保留 {it.current_variants}"
              f"，排除 {it.obsolete_variants}")
        for m in it.mods_for("current")[:4]:
            print(f"      [{m.kind}] {m.text[:62]}")
        obs = it.mods_for("obsolete")
        if obs:
            print(f"    已排除的历史词缀 {len(obs)} 条，例：{obs[0].text[:56]}")

    print("\n【体检】")
    no_base = [i for i in items if not i.base_type_en]
    no_mods = [i for i in items if not i.mods_for("current")]
    bad_implicit = [
        i for i in items
        if i.implicit_count > len([m for m in i.all_mods if m.is_implicit])
    ]
    multi_base = [i for i in items if len(i.base_types) > 1]
    print(f"  有底材名             : {len(items) - len(no_base):,} / {len(items):,}")
    print(f"  有当前词缀           : {len(items) - len(no_mods):,}")
    print(f"  Implicits 行数声明一致  : {len(items) - len(bad_implicit):,}")
    print(f"  多底材行（按变体选）  : {len(multi_base):,} 件"
          f"（不按变体选会拿到旧版底材，多余的行还会被当成词缀）")
    for i in multi_base[:3]:
        print(f"    {i.name_en}: {i.base_type_raw} → 当前 {i.base_type_en!r}")
    if no_mods:
        # 有的装备本来就没词缀（Tabula Rasa 只有 `Sockets: W-W-W-W-W-W`），
        # 所以这里只列出来供确认，不当成错误
        print(f"    无当前词缀（可能本来就没有）：{[i.name_en for i in no_mods[:5]]}")
    dup_uid = [
        uid for uid, n in Counter(i.uid for i in items).items() if n > 1
    ]
    print(f"  uid 唯一（名字+底材）  : {len(items) - len(dup_uid):,} / {len(items):,}"
          f"{'' if not dup_uid else f'  ← {len(dup_uid)} 处重复'}")
    if bad_implicit:
        problems.append(f"{len(bad_implicit)} 件的 Implicits 声明数大于实际隐式词缀数")

    by_file = Counter(i.source_file.rsplit("/", 1)[-1] for i in items)
    print(f"  来自 {len(by_file)} 个文件，最多：{by_file.most_common(3)}")

    if problems:
        print(f"\n【需处理 {len(problems)} 项】")
        for p in problems[:15]:
            print(f"  {p}")
        if len(problems) > 15:
            print(f"  …另有 {len(problems) - 15} 项")

    if args.dry_run:
        print("\n（--dry-run，未写库）")
        return 0

    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()
    run = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at) VALUES (?,?,datetime('now'))",
        (repo.source_name(), "唯一装备"),
    )
    run_id = run.lastrowid

    rows = [to_row(i, repo, priority) for i in items]
    written = upsert(conn, rows)

    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _live_uniq(uid TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _live_uniq")
    conn.executemany("INSERT OR IGNORE INTO _live_uniq(uid) VALUES (?)",
                     [(i.uid,) for i in items])
    stale = conn.execute(
        "SELECT count(*) FROM pob_unique WHERE uid NOT IN (SELECT uid FROM _live_uniq)"
    ).fetchone()[0]
    if stale:
        conn.execute(
            "DELETE FROM pob_unique WHERE uid NOT IN (SELECT uid FROM _live_uniq)"
        )
        print(f"  清理 {stale} 条已失效旧行")

    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, "
        "failed=?, elapsed_ms=?, note=? WHERE run_id=?",
        (
            len(items), written, len(problems), int(elapsed * 1000),
            json.dumps(
                {
                    "commit": repo.commit,
                    "with_variants": len(with_var),
                    "obsolete_mods_dropped": dropped,
                    "problems": problems[:10],
                },
                ensure_ascii=False,
            ),
            run_id,
        ),
    )
    conn.commit()
    total = conn.execute("SELECT count(*) FROM pob_unique").fetchone()[0]
    print(f"\n已写入 {written:,} 件，pob_unique 现有 {total:,} 行（run_id={run_id}）")
    dbmod.close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
