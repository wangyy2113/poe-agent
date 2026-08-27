#!/usr/bin/env python3
"""导入 **Boss 技能的伤害建模**（PoB `Data/BossSkills.lua` / `Data/Bosses.lua`）。

`boss` 是最后一个 `harvested` 域。它的 spec 长期写着「缺的不是抽取器而是**来源**」——
关于 POEDB 那半句是对的（缓存页里确实没有 Boss 技能），但那条 spec 的
`pob_files=()` 是空的，而 PoB 检出里一直有这两个文件。**同一个空元组骗了第二次**
（第一次是万神殿）。

**补的是伤害建模，不是打法**：某技能在某等级打多少、穿多少、Uber 乘多少，
以及 Boss 的护甲/闪避倍率。机制与应对本地没有来源，仍在 partial_note 里。

    python scripts/import_pob_bosses.py --dry-run   # 只清点与 tooltip 抽验
    python scripts/import_pob_bosses.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from entities.taxonomy import Source  # noqa: E402
from kb.sources import pob as pob_mod  # noqa: E402
from kb.sources.pob_bosses import (  # noqa: E402
    BOSS_SKILLS_FILE,
    BOSSES_FILE,
    BossSourceError,
    load_all,
)

#: 条数下限。实测 10 条技能 / 22 个 Boss。
MIN_SKILLS = 8
MIN_BOSSES = 18

GAP_QUESTION = "Boss 技能与应对"


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 Boss 技能的伤害建模")
    parser.add_argument("--dry-run", action="store_true", help="只清点与抽验，不写库")
    parser.add_argument("--pob-path", default=None, help="PoB 仓库位置")
    args = parser.parse_args()

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()

    repo = pob_mod.load_repo(args.pob_path, cfg)
    if not repo.reproducible:
        print("PoB commit 未知，引用不可复现，拒绝写入")
        return 2
    skills, bosses, stats = load_all(repo.root / "src" / "Data")

    print(f"{repo.version_label}（{repo.short_commit}）：{stats.skills} 条技能、{stats.bosses} 个 Boss")
    print(
        f"tooltip 抽验：{stats.tooltip_matched}/{stats.tooltip_checked} 一致"
        "（**抽验不是全量**：只有少数 tooltip 复述了穿透数字）"
    )
    for line in stats.tooltip_mismatch:
        print(f"  ✗ {line}")
    if stats.blank_penetrations:
        print(
            f"上游写成空值的穿透 {len(stats.blank_penetrations)} 项，按「没有这一项」处理："
            + "、".join(stats.blank_penetrations)
        )

    if stats.tooltip_mismatch:
        print("tooltip 与结构化字段对不上，拒绝写入（字段可能读错，或上游改了一处）")
        return 2
    if stats.skills < MIN_SKILLS or stats.bosses < MIN_BOSSES:
        print(f"条数低于下限（技能 {MIN_SKILLS} / Boss {MIN_BOSSES}），拒绝写入")
        return 2

    # **不做中文名桥接**：本地没有这些终局 Boss 的中文名（第一版写了一个桥接函数，
    # 它不但 0/32 全空，还用 `except Exception` 吞掉了「poedb_spectre 没有 name_en 列」
    # 这个 schema 错误——一个永远返回空又不报错的函数，比没有更糟）。
    if args.dry_run:
        for skill in skills:
            low = ", ".join(f"{k} {v[0]:g}+{v[1]:g}/roll" for k, v in skill.damage_mults.items())
            print(f"  {skill.name:26s} {skill.damage_type:16s} {low}")
        print("（--dry-run，未写库）")
        return 0

    priority = int(cfg.priority_of(Source.POB.value))
    skill_url = repo.blob_url(BOSS_SKILLS_FILE)
    boss_url = repo.blob_url(BOSSES_FILE)

    conn.execute("SAVEPOINT boss_snapshot")
    try:
        conn.execute("DELETE FROM pob_boss_skill")
        conn.execute("DELETE FROM pob_boss")
        conn.executemany(
            """
            INSERT INTO pob_boss_skill(
                name, damage_type, damage_mults, penetrations,
                uber_penetrations, uber_damage_mult, speed, uber_speed,
                crit_chance, earlier_uber, tooltip, source, source_priority,
                pob_url, source_commit, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            """,
            [
                (
                    s.name, s.damage_type,
                    json.dumps({k: list(v) for k, v in s.damage_mults.items()}),
                    json.dumps(s.penetrations), json.dumps(s.uber_penetrations),
                    s.uber_damage_mult, s.speed, s.uber_speed, s.crit_chance,
                    int(s.earlier_uber), s.tooltip, Source.POB.value, priority,
                    skill_url, repo.commit,
                )
                for s in skills
            ],
        )
        conn.executemany(
            """
            INSERT INTO pob_boss(
                key, armour_mult, evasion_mult, is_uber,
                source, source_priority, pob_url, source_commit, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,datetime('now'))
            """,
            [
                (
                    b.key, b.armour_mult, b.evasion_mult,
                    int(b.is_uber), Source.POB.value, priority, boss_url, repo.commit,
                )
                for b in bosses
            ],
        )
        gap_rows = dbmod.record_ingest_gaps(
            conn,
            question=GAP_QUESTION,
            category="not_collected/boss",
            points=[
                (
                    "[no_source] Boss 技能的**机制说明与应对打法**未采集："
                    "PoB 只给伤害建模，POEDB 的 Monsters 页也没有打法",
                    stats.skills,
                ),
                (
                    f"[no_zh_name] {len(skills)} 条技能、{len(bosses)} 个 Boss "
                    "只有英文名：本地没有这些终局 Boss 的中文译名来源",
                    len(skills) + len(bosses),
                ),
            ],
        )
        conn.execute("RELEASE boss_snapshot")
    except Exception:
        conn.execute("ROLLBACK TO boss_snapshot")
        conn.execute("RELEASE boss_snapshot")
        raise
    conn.commit()

    print(f"入库完成：{stats.skills} 条技能、{stats.bosses} 个 Boss（台账 {gap_rows} 条）")
    print("提示：重跑 `python -m kb.pipeline --category coverage` 刷新覆盖声明。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BossSourceError as exc:
        print(f"结构断言失败，已拒绝写入：{exc}")
        raise SystemExit(2) from exc
    except pob_mod.PoBNotFound as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    finally:
        dbmod.close_db()
