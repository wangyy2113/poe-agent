#!/usr/bin/env python3
"""离线导入 PoB 幽魂建模数据，并与 POEDB 侧对账。

补的是 `poedb_spectre`（P1，清单与倍率）没有的那几维：四抗性、护甲/闪避/能量护盾系数、
攻击间隔、技能 id。判据与两类差异的区分见 `kb/sources/pob_spectres.py` 的模块 docstring。

**不发起网络请求**（PoB 是本地检出）；commit 未钉住时拒绝导入。

    python scripts/import_pob_spectres.py --dry-run   # 只清点与对账
    python scripts/import_pob_spectres.py             # 写库
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
from kb.sources.pob import PoBNotFound, PoBRepo, load_repo  # noqa: E402
from kb.sources.pob_spectres import (  # noqa: E402
    CONFLICT_GRANULARITY,
    CONFLICT_VALUE,
    SPECTRE_FILE,
    PoBSpectre,
    SpectreDiff,
    SpectreStructureError,
    extract_spectres,
    reconcile,
)

#: 数量护栏。2026-08-19 实测 268 条。
MIN_SPECTRES = 200
MAX_RELATIVE_DROP = 0.20


def to_row(entry: PoBSpectre, repo: PoBRepo, priority: int) -> tuple:
    return (
        entry.metadata_id, entry.name_en, entry.life_mult, entry.damage_mult,
        entry.armour_mult, entry.evasion_mult, entry.es_mult,
        entry.fire_resist, entry.cold_resist, entry.lightning_resist, entry.chaos_resist,
        entry.attack_time, entry.attack_range, entry.accuracy, entry.damage_spread,
        json.dumps(entry.skill_ids, ensure_ascii=False),
        json.dumps(entry.monster_tags, ensure_ascii=False),
        SPECTRE_FILE, Source.POB.value, priority, repo.blob_url(SPECTRE_FILE),
        repo.commit, repo.version_label, repo.game_version, entry.content_hash,
    )


def replace_snapshot(conn, entries: list[PoBSpectre], repo: PoBRepo, priority: int) -> int:  # noqa: ANN001
    conn.execute("DELETE FROM pob_spectre")
    conn.executemany(
        """
        INSERT INTO pob_spectre(
            metadata_id,name_en,life_mult,damage_mult,armour_mult,evasion_mult,es_mult,
            fire_resist,cold_resist,lightning_resist,chaos_resist,
            attack_time,attack_range,accuracy,damage_spread,skill_ids,monster_tags,
            source_file,source,source_priority,pob_url,source_commit,pob_version,
            game_version,content_hash,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        [to_row(e, repo, priority) for e in entries],
    )
    return len(entries)


def record_diffs(conn, diffs: list[SpectreDiff], repo: PoBRepo) -> int:  # noqa: ANN001
    """差异落 `source_conflict`，**两类分开的 kind**。

    `kind='spectre'` 是真分歧（两侧都只有一条却对不上），按需求 15.5 可并列展示给用户；
    `kind='spectre_review'` 是粒度差异（POEDB 记得更细），**不得当作冲突呈现**——
    与 `mod_review` 同一个约定。混成一类就是把「谁记错了」和「谁记得更细」当成一件事。
    """
    rows = []
    for diff in diffs:
        kind = "spectre" if diff.cause == CONFLICT_VALUE else "spectre_review"
        note = (
            f"POEDB 同名种类 {diff.poedb_rows} 个，PoB 只建模 1 个"
            if diff.cause == CONFLICT_GRANULARITY
            else "两侧各只有一条记录，生命/伤害系数对不上"
        )
        rows.append((
            diff.subject, "life_damage_mult", kind,
            Source.POEDB.value, diff.poedb_value, diff.name_zh,
            "https://poedb.tw/cn/Monsters",
            Source.POB.value, diff.pob_value, diff.name_en,
            repo.blob_url(SPECTRE_FILE), note, diff.cause,
        ))
    conn.executemany(
        """
        INSERT INTO source_conflict(
            subject,field,kind,source_a,value_a,ref_a,url_a,
            source_b,value_b,ref_b,url_b,note,cause,first_seen,last_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
        ON CONFLICT(subject,field,source_a,source_b) DO UPDATE SET
            value_a=excluded.value_a, value_b=excluded.value_b, kind=excluded.kind,
            note=excluded.note, cause=excluded.cause, last_seen=datetime('now')
        """,
        rows,
    )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pob-path", default=None)
    parser.add_argument("--dry-run", action="store_true", help="只解析与对账，不写库")
    parser.add_argument("--allow-shrink", action="store_true",
                        help="显式允许低于下限或大幅缩减（仅人工确认上游变化后使用）")
    args = parser.parse_args()
    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")

    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()

    try:
        repo = load_repo(args.pob_path, cfg)
        if not repo.reproducible or len(repo.commit) != 40:
            raise SpectreStructureError("PoB commit 未钉住，拒绝导入不可复现引用")
        started = time.monotonic()
        entries = extract_spectres(repo.root)
        elapsed_ms = int((time.monotonic() - started) * 1000)
    except (PoBNotFound, SpectreStructureError, OSError) as exc:
        print(f"[中止] {exc}")
        return 2

    poedb_rows = [
        {"name_zh": r["name_zh"], "poedb_slug": r["poedb_slug"],
         "life_pct": r["life_pct"], "damage_pct": r["damage_pct"]}
        for r in conn.execute("SELECT name_zh,poedb_slug,life_pct,damage_pct FROM poedb_spectre")
    ]
    diffs, stats = reconcile(entries, poedb_rows)

    has_armour = sum(1 for e in entries if e.armour_mult is not None)
    print(f"PoB：{repo.version_label} commit={repo.commit}")
    print(f"幽魂：{len(entries)} 条，解析 {elapsed_ms} ms"
          f"（护甲 {has_armour}、闪避 {sum(1 for e in entries if e.evasion_mult is not None)}、"
          f"能量护盾 {sum(1 for e in entries if e.es_mult is not None)} 条有系数）")
    print(f"技能 id：{sum(len(e.skill_ids) for e in entries)} 个（只存不呈现）")
    print("对账（按 slug→英文名 配对）：")
    print(f"  配对 {stats['paired_names']} 个名字，其中逐条一致 {stats['agree']}")
    print(f"  真分歧 {stats[CONFLICT_VALUE]}、粒度差异 {stats[CONFLICT_GRANULARITY]}")
    print(f"  PoB 独有 {stats['pob_only']}、POEDB 独有 {stats['poedb_only']}、"
          f"PoB 同名多解不判 {stats['ambiguous_pob_name']}")
    for diff in [d for d in diffs if d.cause == CONFLICT_VALUE][:5]:
        print(f"    [分歧] {diff.name_zh}（{diff.name_en}）"
              f"POEDB {diff.poedb_value} vs PoB {diff.pob_value}")
    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0

    existing = int(conn.execute("SELECT count(*) FROM pob_spectre").fetchone()[0])
    if len(entries) < MIN_SPECTRES or (
        existing and len(entries) < existing * (1 - MAX_RELATIVE_DROP)
    ):
        if not args.allow_shrink:
            print(f"[中止] 拒绝替换快照：{existing} -> {len(entries)}（下限 {MIN_SPECTRES}）")
            return 2

    priority = repo.priority(cfg)
    run = conn.execute(
        "INSERT INTO ingest_run(source,category,started_at,total) "
        "VALUES (?,?,datetime('now'),?)",
        (Source.POB.value, "pob_spectre", len(entries)),
    )
    run_id = int(run.lastrowid)
    conn.execute("SAVEPOINT pob_spectre_snapshot")
    try:
        written = replace_snapshot(conn, entries, repo, priority)
        # 只清本轮会重写的两类，别动词缀那 464 条
        conn.execute("DELETE FROM source_conflict WHERE kind IN ('spectre','spectre_review')")
        conflicts = record_diffs(conn, diffs, repo)
        conn.execute("RELEASE pob_spectre_snapshot")
    except Exception:
        conn.execute("ROLLBACK TO pob_spectre_snapshot")
        conn.execute("RELEASE pob_spectre_snapshot")
        raise
    note = json.dumps(
        {"spectres": written, "conflicts": conflicts, **{k: v for k, v in stats.items()}},
        ensure_ascii=False,
    )
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
        "elapsed_ms=?,note=? WHERE run_id=?",
        (written, elapsed_ms, note, run_id),
    )
    conn.commit()
    by_kind = Counter(
        r[0] for r in conn.execute(
            "SELECT kind FROM source_conflict WHERE kind IN ('spectre','spectre_review')"
        )
    )
    print(f"已写入 {written} 条；差异登记 {conflicts} 条 {dict(by_kind)}（run_id={run_id}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
