#!/usr/bin/env python3
"""导入宝石的**每级效果文本、属性需求与固定效果**（POEDB 详情页）。

第四十四轮加进来的「固定效果」是同一张页上的另一张表（`<th>固定</th>`），
所以并进本脚本而不是再开第三个——同一批页、同一批宝石、同一条「对不上就整颗拒绝」
的策略，另开脚本只会让 `import_pob_skills.py` 多一条级联提醒。
它与逐级表的分界是**有没有等级维度**：那张是「造成 25 - 38 冰霜伤害」（每级一个值），
这张是「有 25% 的几率对敌人造成冻结状态」（与等级无关）。

补的是第十二轮起就写在「未采集」里的两维。它们不是数据缺口而是**口径缺口**：
PoB 侧的 stat 是 id + 乘数（要按 `Modules/StatDescriber.lua` 渲染，而且渲出来是英文），
`reqStr/reqDex/reqInt` 是换算系数不是显示值。POEDB 详情页把两者按等级列成了表，
中文的、可引用的——判据与边界见 `kb/sources/poedb_gem_page.py` 的模块 docstring。

**每颗宝石入库前逐级对账**（需求等级 / 消耗 / 基础伤害三项，两边都有值才比）。
对不上就不入这一颗，并把差异写进 `source_conflict`（`kind='gem_review'`，
待人工核对，**不作为跨来源冲突呈现给用户**）。

    python scripts/import_gem_stats.py --dry-run   # 只清点与对账，不写库
    python scripts/import_gem_stats.py             # 导入并重建宝石卡片
    python scripts/import_gem_stats.py --fetch     # 缺页时联网补抓（820 页，遵守限速）
    python scripts/import_gem_stats.py --only 冰霜新星   # 只处理一颗，调试用
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
from kb.sources.poedb_gem_page import (  # noqa: E402
    GemFixedStats,
    GemPage,
    GemPageError,
    Mismatch,
    damage_column,
    parse_gem_fixed,
    parse_gem_page,
    reconcile,
    reconcile_fixed,
)
from kb.sources.pob_skills import poedb_url  # noqa: E402

#: 数量护栏：低于这个数就是抓取或解析出了问题，拒绝替换快照。
#: 2026-08-20 实测 820 颗里 **812 颗**能入库，8 颗因两侧逐级数值对不上被拒
#: （多为 PoB 发的是重做版本、POEDB 页面仍是旧版，逐条进 `source_conflict`）。
MIN_TOTAL_GEMS = 700

GAP_QUESTION = "技能石的效果文本与属性需求"
CONFLICT_KIND = "gem_review"


def load_targets(conn) -> list[dict]:  # noqa: ANN001
    """库里的宝石 + 它们的 PoB 每级数值（对账用）。"""
    gems = [
        dict(row)
        for row in conn.execute(
            "SELECT gem_id, poedb_slug, name_zh, name_en, natural_max_level "
            "FROM pob_skill_gem ORDER BY poedb_slug"
        )
    ]
    levels: dict[str, dict[int, dict]] = {}
    for row in conn.execute(
        "SELECT gem_id, gem_level, level_requirement, cost, base_damage_min, base_damage_max "
        "FROM pob_skill_level"
    ):
        base = None
        if row["base_damage_min"] is not None and row["base_damage_max"] is not None:
            base = (float(row["base_damage_min"]), float(row["base_damage_max"]))
        levels.setdefault(row["gem_id"], {})[int(row["gem_level"])] = {
            "level_requirement": row["level_requirement"],
            "cost": json.loads(row["cost"] or "{}"),
            "base_damage": base,
        }
    for gem in gems:
        gem["pob_levels"] = levels.get(gem["gem_id"], {})
    return gems


def page_html(slug: str, *, fetcher: Fetcher | None) -> str | None:
    """详情页 HTML：先看缓存，再看要不要联网。取不到返回 None（由调用方登记）。"""
    cfg = get_config()
    url = poedb_url(slug)
    path = cache_path_for(cfg.cache_dir, url)
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="ignore")
    if fetcher is None:
        return None
    try:
        return fetcher.fetch(url).text
    except OSError as exc:  # 单页失败只影响这一颗
        print(f"  [提示] {slug} 抓取失败：{exc}")
        return None


def stat_rows(gem_id: str, page: GemPage, *, priority: int) -> tuple[list[tuple], list[tuple]]:
    """一颗宝石 → (效果文本行, 每级行)。"""
    url = poedb_url(page.poedb_slug)
    lines = [
        (gem_id, line.index, line.text_zh, Source.POEDB.value, priority, url)
        for line in page.stat_lines
    ]
    levels = [
        (
            gem_id, row.gem_level, row.req_str, row.req_dex, row.req_int,
            json.dumps({str(k): v for k, v in sorted(row.stat_values.items())},
                       ensure_ascii=False),
            Source.POEDB.value, priority, url,
        )
        for row in page.levels
    ]
    return lines, levels


def fixed_rows(gem_id: str, slug: str, fixed: GemFixedStats, *, priority: int) -> list[tuple]:
    """一颗宝石的固定效果（第四十四轮）。只写中文文本，`Key: value` 不入库。"""
    url = poedb_url(slug)
    return [
        (gem_id, index, text, Source.POEDB.value, priority, url)
        for index, text in enumerate(fixed.prose)
    ]


_FIXED_INSERT = (
    "INSERT INTO poedb_gem_fixed_stat(gem_id,stat_index,text_zh,source,source_priority,"
    "url,fetched_at,updated_at) VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))"
)


def replace_snapshot(conn, per_gem: dict[str, GemPage], *, priority: int) -> tuple[int, int]:  # noqa: ANN001
    """整体替换两张表。全量替换而不是增量：页面重抓后列可能增删，
    增量对账的成本远高于重写几千行。"""
    lines: list[tuple] = []
    levels: list[tuple] = []
    for gem_id, page in per_gem.items():
        line_rows, level_rows = stat_rows(gem_id, page, priority=priority)
        lines.extend(line_rows)
        levels.extend(level_rows)
    conn.execute("DELETE FROM poedb_gem_stat")
    conn.execute("DELETE FROM poedb_gem_stat_level")
    conn.executemany(
        "INSERT INTO poedb_gem_stat(gem_id,stat_index,text_zh,source,source_priority,url,"
        "fetched_at,updated_at) VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
        lines,
    )
    conn.executemany(
        "INSERT INTO poedb_gem_stat_level(gem_id,gem_level,req_str,req_dex,req_int,"
        "values_json,source,source_priority,url,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
        levels,
    )
    return len(lines), len(levels)


def record_conflicts(conn, rows: list[tuple[dict, list[Mismatch]]]) -> int:  # noqa: ANN001
    """把对不上的宝石写进 `source_conflict`。

    `kind='gem_review'` 是**待人工核对**，不是已确认冲突：两侧对不上时更可能的解释是
    「PoB 发的是重做后的版本、POEDB 那一页还是旧版」，而不是「同一颗宝石两个数值」。
    需求 15.5 的并列展示只用 `kind='mod'`/`'spectre'` 那种已确认冲突
    （判据见 `orchestrator/citation.conflict_block` 与 CLAUDE.md 的数据层一节）。
    """
    for gem, mismatches in rows:
        note = "；".join(m.render() for m in mismatches[:3])
        if len(mismatches) > 3:
            note += f"（共 {len(mismatches)} 处）"
        conn.execute(
            """
            INSERT INTO source_conflict(subject, field, kind, source_a, value_a, url_a,
                                        source_b, value_b, url_b, note, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
            ON CONFLICT(subject, field, source_a, source_b) DO UPDATE SET
                value_a=excluded.value_a, value_b=excluded.value_b,
                note=excluded.note, last_seen=datetime('now')
            """,
            (
                f"{gem['name_zh']}（{gem['name_en']}）", "逐级数值", CONFLICT_KIND,
                Source.POEDB.value, mismatches[0].poedb, poedb_url(gem["poedb_slug"]),
                Source.POB.value, mismatches[0].pob, "",
                f"POEDB 详情页与 PoB 每级数值对不上：{note}",
            ),
        )
    return len(rows)


def main() -> int:  # noqa: PLR0915
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只清点与对账，不写库")
    parser.add_argument("--fetch", action="store_true",
                        help="缺页时联网补抓（每颗一页，遵守 robots 与限速）")
    parser.add_argument("--only", default="", help="只处理名字或 slug 匹配的宝石")
    parser.add_argument("--allow-shrink", action="store_true",
                        help="显式允许低于下限（仅人工确认上游变化后使用）")
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

    gems = load_targets(conn)
    if args.only:
        needle = args.only.lower()
        gems = [g for g in gems
                if needle in g["name_zh"].lower() or needle in g["poedb_slug"].lower()]
        if not gems:
            print(f"[中止] 没有匹配 {args.only!r} 的宝石")
            return 2

    started = time.monotonic()
    stat = Counter()
    per_gem: dict[str, GemPage] = {}
    per_fixed: dict[str, GemFixedStats] = {}
    conflicts: list[tuple[dict, list[Mismatch]]] = []
    no_page: list[str] = []
    no_table: list[str] = []
    no_fixed: list[str] = []
    for gem in gems:
        html = page_html(gem["poedb_slug"], fetcher=fetcher)
        if not html:
            stat["未取到详情页"] += 1
            no_page.append(gem["name_zh"])
            continue
        try:
            page = parse_gem_page(html, poedb_slug=gem["poedb_slug"])
        except GemPageError as exc:
            stat["页面结构异常"] += 1
            print(f"  [结构] {gem['name_zh']}：{exc}")
            continue
        if page is None:
            stat["没有逐级表"] += 1
            no_table.append(gem["name_zh"])
            continue
        fixed = parse_gem_fixed(html, poedb_slug=gem["poedb_slug"])
        # 固定表的 `Key: value` 是这一维唯一的对账口径，并进同一份分歧列表：
        # 对不上说明这一页讲的不是库里那颗宝石，那**整页的文本都不能用**
        # （与第三十二轮「宁可少 8 颗」同一条）。实测它补上了逐级对账的一个盲区——
        # 逐级表只能比**存在的列**，而破坏之冰锥地雷那一页根本没有魔力列，
        # 于是它一直漏网，而它的姊妹技能破坏之火屑地雷早就因同样的 5 倍差被拒。
        mismatches = reconcile(page, gem["pob_levels"]) + reconcile_fixed(
            fixed, gem["pob_levels"].get(1)
        )
        if mismatches:
            stat["对账不符"] += 1
            conflicts.append((gem, mismatches))
            print(f"  [不符] {gem['name_zh']}：{mismatches[0].render()}"
                  f"（共 {len(mismatches)} 处）")
            continue
        stat["可入库"] += 1
        stat["有伤害列"] += 1 if damage_column(page) else 0
        if fixed.prose:
            stat["有固定效果"] += 1
        elif not fixed.reconcile_kv:
            no_fixed.append(gem["name_zh"])
        per_gem[gem["gem_id"]] = page
        per_fixed[gem["gem_id"]] = fixed

    elapsed_ms = int((time.monotonic() - started) * 1000)
    lines = sum(len(p.stat_lines) for p in per_gem.values())
    level_rows = sum(len(p.levels) for p in per_gem.values())
    with_req = sum(1 for p in per_gem.values() if any(r.requirements for r in p.levels))
    print(f"宝石 {len(gems)} 颗，解析 {elapsed_ms} ms：{dict(sorted(stat.items()))}")
    fixed_total = sum(len(f.prose) for f in per_fixed.values())
    fixed_gems = sum(1 for f in per_fixed.values() if f.prose)
    kv_total = sum(len(f.reconcile_kv) for f in per_fixed.values())
    print(f"效果文本 {lines} 条、每级行 {level_rows}；带属性需求的 {with_req} 颗")
    print(f"固定效果 {fixed_total} 条（{fixed_gems} 颗）；"
          f"另有 {kv_total} 条内部字段只用于对账、不入库")
    if no_fixed:
        print(f"没有固定效果表 {len(no_fixed)} 颗：{'、'.join(no_fixed[:5])}"
              f"{' 等' if len(no_fixed) > 5 else ''}"
              "（该技能没有与等级无关的机制，**不是未采集**）")
    if no_table:
        print(f"没有逐级表 {len(no_table)} 颗：{'、'.join(no_table[:5])}"
              f"{' 等' if len(no_table) > 5 else ''}")
    if no_page:
        print(f"未取到详情页 {len(no_page)} 颗：{'、'.join(no_page[:5])}"
              f"{' 等' if len(no_page) > 5 else ''}（加 --fetch 可补抓）")

    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0
    if not args.only and len(per_gem) < MIN_TOTAL_GEMS and not args.allow_shrink:
        print(f"[中止] 只有 {len(per_gem)} 颗可入库，低于下限 {MIN_TOTAL_GEMS}"
              "（--allow-shrink 可强制）")
        return 2

    priority = int(cfg.get("sources.poedb.priority", 1) or 1)
    run = conn.execute(
        "INSERT INTO ingest_run(source,category,started_at,total) "
        "VALUES (?,?,datetime('now'),?)",
        (Source.POEDB.value, "poedb_gem_stat", len(per_gem)),
    )
    run_id = int(run.lastrowid)
    conn.execute("SAVEPOINT gem_stats")
    try:
        if args.only:
            # 单颗调试：只替换这几颗，别把整张表清掉
            for gem_id, page in per_gem.items():
                conn.execute("DELETE FROM poedb_gem_stat WHERE gem_id=?", (gem_id,))
                conn.execute("DELETE FROM poedb_gem_stat_level WHERE gem_id=?", (gem_id,))
                conn.execute("DELETE FROM poedb_gem_fixed_stat WHERE gem_id=?", (gem_id,))
                conn.executemany(_FIXED_INSERT, fixed_rows(
                    gem_id, page.poedb_slug, per_fixed[gem_id], priority=priority))
                line_rows, level_list = stat_rows(gem_id, page, priority=priority)
                conn.executemany(
                    "INSERT INTO poedb_gem_stat(gem_id,stat_index,text_zh,source,"
                    "source_priority,url,fetched_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))", line_rows)
                conn.executemany(
                    "INSERT INTO poedb_gem_stat_level(gem_id,gem_level,req_str,req_dex,"
                    "req_int,values_json,source,source_priority,url,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))", level_list)
            written_lines, written_levels = lines, level_rows
        else:
            written_lines, written_levels = replace_snapshot(conn, per_gem, priority=priority)
            conn.execute("DELETE FROM poedb_gem_fixed_stat")
            for gem_id, page in per_gem.items():
                conn.executemany(_FIXED_INSERT, fixed_rows(
                    gem_id, page.poedb_slug, per_fixed[gem_id], priority=priority))
        reviewed = record_conflicts(conn, conflicts)
        gap_rows = record_gaps(conn, no_page=no_page, no_table=no_table,
                               no_fixed=no_fixed, conflicts=conflicts)
        conn.execute("RELEASE gem_stats")
    except Exception:
        conn.execute("ROLLBACK TO gem_stats")
        conn.execute("RELEASE gem_stats")
        raise
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
        "elapsed_ms=?,note=? WHERE run_id=?",
        (len(per_gem), elapsed_ms,
         f"{written_lines} 条效果文本 / {written_levels} 行每级数值 / "
         f"{fixed_total} 条固定效果；待核对 {reviewed}；台账 {gap_rows} 组", run_id),
    )
    conn.commit()
    print(f"已写入 {written_lines} 条效果文本、{written_levels} 行每级数值、"
          f"{fixed_total} 条固定效果（run_id={run_id}）；"
          f"待人工核对 {reviewed} 颗，台账 {gap_rows} 组")

    cards = reindex_cards(conn)
    conn.commit()
    print(f"宝石卡片已重建 {cards} 张（效果文本与属性需求现在写在卡上）")
    return 0


def record_gaps(conn, *, no_page: list[str], no_table: list[str],  # noqa: ANN001
                no_fixed: list[str],
                conflicts: list[tuple[dict, list[Mismatch]]]) -> int:
    """按成因登记未入库项。三类分开——它们对应三个不同的动作。

    **`no_fixed` 不进台账**：那 21 颗是「这个技能没有与等级无关的机制」，
    不是缺口。把它记成待办会让「该采集什么」这个信号重新失真——
    第三十轮修的就是台账里混进了永远不该执行的条目。参数留着是为了让调用方
    显式表达「我知道有这一类，且我判断它不是缺口」。
    """
    points: list[tuple[str, int]] = []
    if no_page:
        points.append((
            f"[no_detail_page] {len(no_page)} 颗宝石的 POEDB 详情页未抓取："
            f"{'、'.join(no_page[:3])}{' 等' if len(no_page) > 3 else ''}"
            "（加 --fetch 可补抓）", len(no_page)))
    if no_table:
        points.append((
            f"[no_level_table] {len(no_table)} 颗宝石的详情页上没有逐级表："
            f"{'、'.join(no_table[:3])}{' 等' if len(no_table) > 3 else ''}"
            "（POEDB 那一页只有说明，效果文本与属性需求取不到）", len(no_table)))
    if conflicts:
        names = [gem["name_zh"] for gem, _ in conflicts]
        points.append((
            f"[level_mismatch] {len(names)} 颗宝石两侧逐级数值对不上，未入效果文本："
            f"{'、'.join(names[:3])}{' 等' if len(names) > 3 else ''}"
            "（多为 PoB 发的是重做版本、POEDB 页面仍是旧版，见 source_conflict）",
            len(names)))
    return dbmod.record_ingest_gaps(
        conn, question=GAP_QUESTION, category="partial/skill_gem", points=points,
    )


def reindex_cards(conn) -> int:  # noqa: ANN001
    """重建宝石卡片，让新入库的两维真的有出口。

    「数据入库但没有出口」是这个项目反复踩的坑（第二十九轮基础伤害那次也是：
    先重导入、后改渲染，结果录制里答的还是「证据中不含基础伤害数据」）。
    所以这一步是导入的一部分，不是可选项。
    """
    from scripts.import_pob_skills import rebuild_cards_from_db  # noqa: PLC0415

    return rebuild_cards_from_db(conn)


if __name__ == "__main__":
    raise SystemExit(main())
