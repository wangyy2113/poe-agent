#!/usr/bin/env python3
"""导入技能石的**获取途径**（POEDB 详情页的「使命」表）。

补的是从第十二轮起就写在宝石域「未采集」里的一维。它既不是数据缺口也不是口径缺口，
而是**没去看那张表**：`kb/coverage.py` 当时的注释写着「任务奖励与掉落不在任何已缓存页上」，
可第三十二轮为取效果文本抓的 820 张详情页里就有它，而 `kb/sources/poedb_gem_page.py`
自己的 docstring 也写着「详情页上还有『品质变体』『任务奖励』等好几张表」。
与万神殿（第三十五轮）、Boss（第三十八轮）、宝石效果文本（第三十二轮）是同一个错误的
第四次：**停在一页/一个字段上就下「缺来源」的结论**。

2026-09-01 实测：820 颗里 **668 颗**有这张表，共 **1265 行**、零解析错误。

## 边界：本轮挪一格，不是挪到底

入库的是**任务奖励 / 任务商人 / 起始箱**三条途径。**掉落来源仍未采集**——
另外 152 颗（瓦尔 52、觉醒辅助、契约类）页面上根本没有这张表，它们靠掉落。
这与第二十九轮「基础伤害入库、DPS 仍拒答」是同一个形状：挪一格要说清挪了哪一格。

## 这一维只有 POEDB 一个来源

PoB 的 `Gems.lua` 没有任务奖励数据，所以没有跨来源对账可做。能做的是**页面内自证**，
入库前跑三条（见 `audit()`）：列数必须是 4、章节必须是数字、获取途径标记必须在白名单里
（未知标记直接抛错而不是归到「其它」——归类错了没人看得出来）。

    python scripts/import_gem_quests.py --dry-run   # 只清点，不写库
    python scripts/import_gem_quests.py             # 导入并重建宝石卡片
    python scripts/import_gem_quests.py --only 冰霜新星   # 只处理一颗，调试用
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from entities.taxonomy import Source  # noqa: E402
from kb.fetcher import Fetcher, cache_path_for  # noqa: E402
from kb.sources.poedb_gem_page import (  # noqa: E402
    GemPageError,
    GemQuestRow,
    parse_gem_quests,
)
from kb.sources.pob_skills import poedb_url  # noqa: E402

#: 数量护栏：低于这个数就是抓取或解析出了问题，拒绝替换快照。
#: 2026-09-01 实测 820 颗里 668 颗有这张表。
MIN_TOTAL_GEMS = 600

GAP_QUESTION = "技能石的获取途径"


def load_targets(conn) -> list[dict]:  # noqa: ANN001
    return [
        dict(row)
        for row in conn.execute(
            "SELECT gem_id, poedb_slug, name_zh, name_en FROM pob_skill_gem "
            "WHERE poedb_slug IS NOT NULL ORDER BY poedb_slug"
        )
    ]


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


def merge_rows(rows: tuple[GemQuestRow, ...]) -> list[GemQuestRow]:
    """按主键归并，**职业取并集**。

    页面同一个 `(使命, 途径, NPC)` 下会出现两行（实测 1265 → 1261，4 处）：
    三处是同一批职业换了个顺序（元素要害 / 衰弱 / 时空锁链——正是上游漏了图标
    又重列一遍的那几行），一处（元素扩散(辅)）两行职业**不相交**
    （`贵族·暗影·女巫` 与 `圣堂武僧`）。所以只能取并集：直接丢掉一行会少掉圣堂武僧，
    而那正是用户要问的「我这个职业能不能拿」。
    """
    merged: dict[tuple, GemQuestRow] = {}
    for row in rows:
        key = (row.quest_slug, row.source_kind, row.npc_zh or "")
        seen = merged.get(key)
        if seen is None:
            merged[key] = row
            continue
        classes = list(seen.classes) + [c for c in row.classes if c not in seen.classes]
        # 原文保留**更全**的那一份：它要被引用，不能是被截短的半句。
        raw = seen.classes_raw if len(seen.classes) >= len(row.classes) else row.classes_raw
        merged[key] = GemQuestRow(
            act=seen.act, quest_zh=seen.quest_zh, quest_slug=seen.quest_slug,
            source_kind=seen.source_kind, npc_zh=seen.npc_zh,
            classes=tuple(classes), classes_raw=raw,
        )
    return list(merged.values())


def quest_rows(gem_id: str, slug: str, rows: list[GemQuestRow], *, priority: int) -> list[tuple]:
    url = poedb_url(slug)
    return [
        (
            gem_id, row.act, row.quest_zh, row.quest_slug, row.source_kind,
            row.npc_zh or "", json.dumps(list(row.classes), ensure_ascii=False),
            row.classes_raw, Source.POEDB.value, priority, url,
        )
        for row in rows
    ]


_INSERT = (
    "INSERT INTO poedb_gem_quest(gem_id,act,quest_zh,quest_slug,source_kind,npc_zh,"
    "classes_json,classes_raw,source,source_priority,url,fetched_at,updated_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))"
)


def audit(per_gem: dict[str, list[GemQuestRow]]) -> dict[str, object]:
    """页面内自证。这一维没有第二个来源，所以只能报清楚**它自己说了什么**。"""
    kinds = Counter()
    acts = Counter()
    quests: dict[str, set[int]] = defaultdict(set)
    npcs = Counter()
    for rows in per_gem.values():
        for row in rows:
            kinds[row.source_kind] += 1
            acts[row.act] += 1
            quests[row.quest_zh].add(row.act)
            if row.npc_zh:
                npcs[row.npc_zh] += 1
    return {
        "kinds": dict(sorted(kinds.items())),
        "acts": dict(sorted(acts.items())),
        "quests": len(quests),
        "npcs": dict(sorted(npcs.items(), key=lambda x: -x[1])),
        # 同名使命跨章节的条数。**不是错误**：`act` 是按行的事实而不是使命的属性，
        # 实测 2 行商人行如此。报出来是为了下次上游变化时能看见。
        "multi_act_quests": {q: sorted(v) for q, v in quests.items() if len(v) > 1},
    }


def main() -> int:  # noqa: PLR0915
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只清点，不写库")
    parser.add_argument("--fetch", action="store_true", help="缺页时联网补抓")
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
    per_gem: dict[str, list[GemQuestRow]] = {}
    slugs: dict[str, str] = {}
    no_page: list[str] = []
    no_table: list[str] = []
    raw_total = 0
    for gem in gems:
        html = page_html(gem["poedb_slug"], fetcher=fetcher)
        if not html:
            stat["未取到详情页"] += 1
            no_page.append(gem["name_zh"])
            continue
        try:
            rows = parse_gem_quests(html, poedb_slug=gem["poedb_slug"])
        except GemPageError as exc:
            stat["页面结构异常"] += 1
            print(f"  [结构] {gem['name_zh']}：{exc}")
            continue
        if not rows:
            stat["没有使命表"] += 1
            no_table.append(gem["name_zh"])
            continue
        raw_total += len(rows)
        stat["可入库"] += 1
        per_gem[gem["gem_id"]] = merge_rows(rows)
        slugs[gem["gem_id"]] = gem["poedb_slug"]

    elapsed_ms = int((time.monotonic() - started) * 1000)
    total_rows = sum(len(r) for r in per_gem.values())
    report = audit(per_gem)
    print(f"宝石 {len(gems)} 颗，解析 {elapsed_ms} ms：{dict(sorted(stat.items()))}")
    print(f"途径行 {raw_total} 行 → 归并后 {total_rows} 行"
          f"（同键取职业并集，实测 4 处）")
    print(f"  途径分布 {report['kinds']}｜章节 {report['acts']}"
          f"｜使命 {report['quests']} 个｜商人 {report['npcs']}")
    if report["multi_act_quests"]:
        print(f"  同名使命跨章节（非错误，act 是按行的事实）：{report['multi_act_quests']}")
    if no_table:
        print(f"没有使命表 {len(no_table)} 颗：{'、'.join(no_table[:5])}"
              f"{' 等' if len(no_table) > 5 else ''}"
              "（瓦尔/觉醒/契约类，靠掉落——掉落来源本轮未采集）")
    if no_page:
        print(f"未取到详情页 {len(no_page)} 颗（加 --fetch 可补抓）")

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
        (Source.POEDB.value, "poedb_gem_quest", len(per_gem)),
    )
    run_id = int(run.lastrowid)
    conn.execute("SAVEPOINT gem_quests")
    try:
        if args.only:
            # 单颗调试：只替换这几颗，别把整张表清掉
            for gem_id, rows in per_gem.items():
                conn.execute("DELETE FROM poedb_gem_quest WHERE gem_id=?", (gem_id,))
                conn.executemany(
                    _INSERT, quest_rows(gem_id, slugs[gem_id], rows, priority=priority))
        else:
            conn.execute("DELETE FROM poedb_gem_quest")
            for gem_id, rows in per_gem.items():
                conn.executemany(
                    _INSERT, quest_rows(gem_id, slugs[gem_id], rows, priority=priority))
        gap_rows = record_gaps(conn, no_page=no_page, no_table=no_table)
        conn.execute("RELEASE gem_quests")
    except Exception:
        conn.execute("ROLLBACK TO gem_quests")
        conn.execute("RELEASE gem_quests")
        raise
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
        "elapsed_ms=?,note=? WHERE run_id=?",
        (len(per_gem), elapsed_ms,
         f"{total_rows} 行获取途径；台账 {gap_rows} 组", run_id),
    )
    conn.commit()
    print(f"已写入 {total_rows} 行获取途径（run_id={run_id}）；台账 {gap_rows} 组")

    cards = reindex_cards(conn)
    conn.commit()
    print(f"宝石卡片已重建 {cards} 张（获取途径现在写在卡上）")
    return 0


def record_gaps(conn, *, no_page: list[str], no_table: list[str]) -> int:  # noqa: ANN001
    """按成因登记未入库项。

    `no_quest_table` 那一条是本轮的**边界本身**，不是待办：那 152 颗靠掉落，
    而掉落来源没有本地来源。写进台账是为了让「还差什么」这个信号说得出它。
    """
    points: list[tuple[str, int]] = []
    if no_page:
        points.append((
            f"[no_detail_page] {len(no_page)} 颗宝石的 POEDB 详情页未抓取："
            f"{'、'.join(no_page[:3])}{' 等' if len(no_page) > 3 else ''}"
            "（加 --fetch 可补抓）", len(no_page)))
    if no_table:
        points.append((
            f"[no_quest_table] {len(no_table)} 颗宝石的详情页上没有使命表："
            f"{'、'.join(no_table[:3])}{' 等' if len(no_table) > 3 else ''}"
            "（瓦尔/觉醒辅助/契约类，靠掉落获得；**掉落来源本地无来源**）",
            len(no_table)))
    return dbmod.record_ingest_gaps(
        conn, question=GAP_QUESTION, category="skill_gem", points=points)


def reindex_cards(conn) -> int:  # noqa: ANN001
    """重建宝石卡片，让新入库的这一维真的有出口。

    「数据入库但没有出口」是这个项目反复踩的坑（第二十九轮基础伤害那次就是
    先重导入、后改渲染，结果录制里答的还是「证据中不含」）。所以这一步是
    导入的一部分，不是可选项。
    """
    from scripts.import_pob_skills import rebuild_cards_from_db  # noqa: PLC0415

    return rebuild_cards_from_db(conn)


if __name__ == "__main__":
    raise SystemExit(main())
