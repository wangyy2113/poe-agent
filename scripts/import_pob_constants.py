"""把 PoB `Misc.lua` 的游戏常量导入 `game_constant` 表（`calc/` 的真源）。

    python scripts/import_pob_constants.py --dry-run
    python scripts/import_pob_constants.py

标量逐个成行，这样每个常量都能被单独引用（需求 12：常量附来源与版本）。
`dat_source` 记 GGG 原始表名，比「来自 PoB」精确一级。

引用 URL 带行号锚点，点开就是那一块常量。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calc import constants  # noqa: E402
from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.sources.pob import MISC_URL_FILE, PoBNotFound, load_repo  # noqa: E402
from kb.sources.pob_misc import extract, flatten  # noqa: E402

#: `calc/` 依赖的常量清单以 `calc.constants.REQUIRED` 为唯一真源。
#: 在这里再抄一份的话，两边迟早不一致，而不一致的表现是「calc 运行时才炸」。
REQUIRED_KEYS = constants.REQUIRED


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
    groups, problems = extract(repo.root)
    rows = flatten(groups)
    elapsed = time.time() - started

    print(f"\n抽取 {len(groups)} 组、{len(rows)} 行常量，{elapsed:.2f}s")
    print(f"{'组':<32}{'类型':<12}{'条目':>6}  出处（GGG 原始表）")
    print("─" * 96)
    for g in sorted(groups, key=lambda x: (x.kind, x.name)):
        mark = "" if g.dat_source_direct else "  ↳延续"
        print(f"{g.name:<32}{g.kind:<12}{g.count:>6}  "
              f"{g.dat_source or '（无出处注释）'}{mark}")

    print("\n【关键常量抽检】")
    by_key = {r["key"]: r for r in rows}
    missing: list[str] = []
    for key in REQUIRED_KEYS:
        r = by_key.get(key)
        if r is None:
            missing.append(key)
            print(f"  [缺失] {key}")
            continue
        if r["kind"] == "scalar":
            print(f"  {key:<56} = {r['value_num']:g}   ← {r['dat_source']}")
        else:
            arr = r["value_json"] or []
            print(f"  {key:<56} = {len(arr)} 级表 "
                  f"[{arr[0]:g} … {arr[-1]:g}]   ← {r['dat_source']}")
    if missing:
        problems.append(f"缺少 {len(missing)} 个关键常量：{', '.join(missing)}")

    if problems:
        print(f"\n【需处理 {len(problems)} 项】")
        for p in problems:
            print(f"  {p}")

    if args.dry_run:
        print("\n（--dry-run，未写库）")
        return 1 if missing else 0

    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()
    run = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at) VALUES (?,?,datetime('now'))",
        (repo.source_name(), "游戏常量"),
    )
    run_id = run.lastrowid

    payload = [
        (
            r["key"], r["group_name"], r["dat_source"] or None,
            0 if r["dat_source_direct"] else 1, r["kind"],
            r["value_num"],
            json.dumps(r["value_json"], ensure_ascii=False) if r["value_json"] is not None else None,
            repo.source_name(), priority,
            repo.blob_url(MISC_URL_FILE, line=r["line"] or None),
            repo.version_label, repo.game_version or None,
        )
        for r in rows
    ]
    conn.executemany(
        """
        INSERT INTO game_constant(
            key, group_name, dat_source, dat_source_inherited, kind,
            value_num, value_json,
            source, source_priority, url, pob_version, game_version, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(key) DO UPDATE SET
            group_name=excluded.group_name, dat_source=excluded.dat_source,
            dat_source_inherited=excluded.dat_source_inherited,
            kind=excluded.kind, value_num=excluded.value_num,
            value_json=excluded.value_json, url=excluded.url,
            pob_version=excluded.pob_version, game_version=excluded.game_version,
            updated_at=datetime('now')
        """,
        payload,
    )

    # 清掉本次没出现的旧行：上游删了常量，留着会被 calc 读到一个已不存在的值
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _live_const(key TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _live_const")
    conn.executemany("INSERT OR IGNORE INTO _live_const(key) VALUES (?)",
                     [(r["key"],) for r in rows])
    stale = conn.execute(
        "SELECT count(*) FROM game_constant WHERE key NOT IN (SELECT key FROM _live_const)"
    ).fetchone()[0]
    if stale:
        conn.execute(
            "DELETE FROM game_constant WHERE key NOT IN (SELECT key FROM _live_const)"
        )
        print(f"  清理 {stale} 条已失效常量（上游已删除）")

    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, "
        "failed=?, elapsed_ms=?, note=? WHERE run_id=?",
        (
            len(rows), len(rows), len(problems), int(elapsed * 1000),
            json.dumps(
                {"groups": len(groups), "commit": repo.commit, "problems": problems[:10]},
                ensure_ascii=False,
            ),
            run_id,
        ),
    )
    conn.commit()
    total = conn.execute("SELECT count(*) FROM game_constant").fetchone()[0]
    print(f"\n已写入 {len(rows)} 行，game_constant 现有 {total} 行（run_id={run_id}）")
    print(f"引用样例：{repo.blob_url(MISC_URL_FILE, line=62)}")
    dbmod.close_db()
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
