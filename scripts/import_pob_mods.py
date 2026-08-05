"""把 PoB 的词缀导入 `pob_mod` 表（P2 来源）。

    python scripts/import_pob_mods.py --dry-run   # 只清点与体检，不写库
    python scripts/import_pob_mods.py             # 导入
    POB_PATH=/path/to/PathOfBuilding python scripts/import_pob_mods.py

只写新表 `pob_mod`，不触碰 `mod_affix`（POEDB 侧）。两个源各自独立保存，
比对交给 `scripts/verify_pob_mods.py`——这样冲突才会被发现，而不是被 UPSERT
悄悄覆盖掉。

引用 URL 钉住当前 commit。若 commit 读不到，脚本**拒绝导入**：那样入库的
引用不可复现，等于伪造溯源。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.sources.pob import PoBNotFound, PoBRepo, load_repo  # noqa: E402
from kb.sources.pob_mods import PoBMod, extract_all  # noqa: E402

logger = logging.getLogger("import_pob_mods")


def to_row(m: PoBMod, repo: PoBRepo, priority: int) -> tuple:
    return (
        m.uid, m.mod_id, m.mod_kind, m.source_file,
        m.affix_type or None, m.affix_name or None, m.mod_group or None, m.ilvl,
        m.text, m.text_template, m.loose_template,
        json.dumps([[a, b] for a, b in m.ranges]),
        json.dumps(m.weights, ensure_ascii=False),
        json.dumps(m.weight_multipliers, ensure_ascii=False),
        json.dumps(m.spawnable_tags, ensure_ascii=False),
        json.dumps(m.tags, ensure_ascii=False),
        json.dumps(m.behaviour_tags, ensure_ascii=False),
        json.dumps(m.item_types, ensure_ascii=False),
        m.variant_id or None,
        json.dumps(m.trade_hashes),
        json.dumps(m.stat_order),
        repo.source_name(), priority,
        repo.blob_url(m.source_file),
        repo.version_label,
    )


def upsert(conn, rows: list[tuple]) -> int:  # noqa: ANN001
    conn.executemany(
        """
        INSERT INTO pob_mod(
            uid, mod_id, mod_kind, source_file,
            affix_type, affix_name, mod_group, ilvl,
            text_en, text_template, loose_template, ranges,
            weights, weight_mults, spawn_tags, tags, behaviour_tags, item_types,
            variant_id, trade_hashes, stat_order,
            source, source_priority, url, pob_version, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(uid) DO UPDATE SET
            affix_type=excluded.affix_type, affix_name=excluded.affix_name,
            mod_group=excluded.mod_group, ilvl=excluded.ilvl,
            text_en=excluded.text_en, text_template=excluded.text_template,
            loose_template=excluded.loose_template, ranges=excluded.ranges,
            weights=excluded.weights, weight_mults=excluded.weight_mults,
            spawn_tags=excluded.spawn_tags, tags=excluded.tags,
            behaviour_tags=excluded.behaviour_tags, item_types=excluded.item_types,
            variant_id=excluded.variant_id,
            trade_hashes=excluded.trade_hashes, stat_order=excluded.stat_order,
            url=excluded.url, pob_version=excluded.pob_version,
            updated_at=datetime('now')
        """,
        rows,
    )
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pob-path", default=None)
    ap.add_argument("--dry-run", action="store_true", help="只体检，不写库")
    ap.add_argument("--allow-unpinned", action="store_true",
                    help="即使读不到 commit 也导入（引用将不可复现，不建议）")
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
    print(f"来源：{repo.source_name()}（P{priority}）")
    if not repo.reproducible:
        msg = ("读不到 PoB 的 commit SHA，入库的引用将指向可变分支、无法复现。"
               "这等于伪造溯源，默认拒绝导入。")
        if not args.allow_unpinned:
            print(f"\n[中止] {msg}\n  确实要继续请加 --allow-unpinned")
            return 2
        print(f"\n[警告] {msg}")

    started = time.time()
    mods, problems = extract_all(repo.root)
    elapsed = time.time() - started

    by_kind = Counter(m.mod_kind for m in mods)
    print(f"\n抽取 {len(mods):,} 条词缀，{elapsed:.1f}s")
    print(f"{'种类':<18}{'条目':>7}  样例")
    print("─" * 92)
    for kind, n in by_kind.most_common():
        sample = next(m for m in mods if m.mod_kind == kind)
        print(f"{kind:<18}{n:>7}  {sample.affix_name or '(无词缀名)':<16} {sample.text[:44]}")

    # ── 体检：这些是会让下游静默出错的问题，必须显式暴露 ──
    print("\n【体检】")
    no_group = [m for m in mods if not m.mod_group]
    no_ilvl = [m for m in mods if m.ilvl is None]
    inconsistent = [m for m in mods if not m.is_consistent]
    no_weight = [m for m in mods if not m.weights]
    with_trade = [m for m in mods if m.trade_hashes]
    with_types = [m for m in mods if m.item_types]
    with_behaviour = [m for m in mods if m.behaviour_tags]
    print(f"  有 group（跨语言对账主键）: {len(mods) - len(no_group):,} / {len(mods):,}")
    print(f"  有 ilvl                  : {len(mods) - len(no_ilvl):,}")
    print(f"  有权重向量               : {len(mods) - len(no_weight):,}")
    print(f"  有 tradeHashes           : {len(with_trade):,}  → 可打通 Trade API")
    print(f"  有适用物品类型           : {len(with_types):,}  → 工艺台制作范围")
    print(f"  有机制标记               : {len(with_behaviour):,}  → 权重乘数条件")
    print(f"  模板 # 数 = 区间数        : {len(mods) - len(inconsistent):,}"
          f"{'' if not inconsistent else f'  ← {len(inconsistent)} 条不一致'}")
    if inconsistent:
        print("    不一致样例（模板可变位与区间数不符，反查会失配）：")
        for m in inconsistent[:5]:
            print(f"      {m.uid}: 模板={m.text_template[:52]!r} 区间={m.ranges}")

    # 下界 > 上界 = 符号或记法没归一。这是会让下游把减伤算成加伤的缺陷，
    # 必须当硬错误暴露，不能只在日志里过一眼。
    anomalies = [m for m in mods if m.range_anomalies]
    print(f"  区间下界 <= 上界          : {len(mods) - len(anomalies):,}"
          f"{'' if not anomalies else f'  ← {len(anomalies)} 条颠倒'}")
    if anomalies:
        print("    颠倒样例：")
        for m in anomalies[:5]:
            print(f"      {m.uid}: {m.text[:48]!r} → {m.ranges}")
        problems.append(f"{len(anomalies)} 条区间下界大于上界，负区间记法未归一")

    # uid 撞车必须查出来：UPSERT 会让后来者覆盖先来者，条目就这么静默消失了。
    seen: dict[str, PoBMod] = {}
    collisions: list[tuple[PoBMod, PoBMod]] = []
    for m in mods:
        prev = seen.get(m.uid)
        if prev is not None:
            collisions.append((prev, m))
        else:
            seen[m.uid] = m
    print(f"  uid 唯一                 : {len(seen):,}"
          f"{'' if not collisions else f'  ← {len(collisions)} 处撞车'}")
    for prev, cur in collisions[:5]:
        same = prev.text == cur.text and prev.ilvl == cur.ilvl
        print(f"    {cur.uid}")
        print(f"      A: ilvl={prev.ilvl} {prev.text[:56]!r}")
        print(f"      B: ilvl={cur.ilvl} {cur.text[:56]!r}")
        print(f"      → {'内容完全相同，上游重复条目，覆盖无损失' if same else '内容不同，会丢数据！'}")
    if any(p.text != c.text or p.ilvl != c.ilvl for p, c in collisions):
        problems.append(
            f"uid 撞车且内容不同（{len(collisions)} 处），stable_mod_id 的哈希种子需加字段"
        )

    if problems:
        print(f"\n【需处理 {len(problems)} 项】")
        for p in problems:
            print(f"  {p}")

    if args.dry_run:
        print("\n（--dry-run，未写库）")
        return 0

    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()
    run = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at) "
        "VALUES (?,?,datetime('now'))",
        (repo.source_name(), "词缀"),
    )
    run_id = run.lastrowid

    rows = [to_row(m, repo, priority) for m in mods]
    written = upsert(conn, rows)

    # 清掉本次没出现的旧行。合成 uid 的哈希种子一变（或 PoB 删了条目），
    # 旧行就成了没有来源的孤儿——留着会被检索命中，答出一条现在已不存在的词缀。
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _live(uid TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _live")
    conn.executemany("INSERT OR IGNORE INTO _live(uid) VALUES (?)",
                     [(m.uid,) for m in mods])
    stale = conn.execute(
        "SELECT count(*) FROM pob_mod WHERE uid NOT IN (SELECT uid FROM _live)"
    ).fetchone()[0]
    if stale:
        conn.execute("DELETE FROM pob_mod WHERE uid NOT IN (SELECT uid FROM _live)")
        print(f"  清理 {stale} 条已失效旧行（uid 变更或上游删除）")

    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, "
        "failed=?, elapsed_ms=?, note=? WHERE run_id=?",
        (
            len(mods), written, len(problems), int(elapsed * 1000),
            json.dumps(
                {
                    "pob_version": repo.version_label,
                    "commit": repo.commit,
                    "by_kind": dict(by_kind),
                    "problems": problems[:10],
                },
                ensure_ascii=False,
            ),
            run_id,
        ),
    )
    conn.commit()
    total = conn.execute("SELECT count(*) FROM pob_mod").fetchone()[0]
    print(f"\n已写入 {written:,} 条，pob_mod 现有 {total:,} 行（run_id={run_id}）")
    print(f"引用样例：{repo.blob_url('Data/ModExplicit.lua')}")
    dbmod.close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
