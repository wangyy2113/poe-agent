#!/usr/bin/env python3
"""人工核对辅助：把列表页抽出的数据与 POEDB **详情页**逐条交叉核对。

这是对抽取质量的独立验证——不是「跑通没报错」，而是拿另一个数据源比对
同一批事实。词缀文本、底材、需求逐项比，不一致就列出来。

顺带取回权威英文名（列表页拿不到，只能从详情页的裸 div 取）。

    python scripts/verify_against_detail.py            # 抽样 20 条
    python scripts/verify_against_detail.py -n 5       # 抽样 5 条
    python scripts/verify_against_detail.py --slug Headhunter --slug Goldrim
    python scripts/verify_against_detail.py --write-en # 把核对到的英文名写回库
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.fetcher import Fetcher  # noqa: E402
from kb.sources.poedb_unique import parse_detail_page  # noqa: E402

#: 固定随机种子，保证抽样可复现
SAMPLE_SEED = 20260804


def _record_gap(conn, question: str, missing_point: str, category: str) -> None:  # noqa: ANN001
    """把核对发现的缺口记进台账。

    为什么核对脚本要写台账：差异不是「跑完看一眼」就算了的事。按设计，
    每条缺据都要进 `knowledge_gap`，按命中次数排序即成为后续采集优先级
    （需求 1.3）。只打印在终端里，下次就没人记得了。
    """
    row = conn.execute(
        "SELECT gap_id FROM knowledge_gap WHERE question=? AND missing_point=?",
        (question, missing_point),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE knowledge_gap SET hit_count=hit_count+1, last_seen=datetime('now') "
            "WHERE gap_id=?",
            (row["gap_id"],),
        )
    else:
        conn.execute(
            "INSERT INTO knowledge_gap(question, missing_point, guessed_category, "
            "hit_count, first_seen, last_seen) VALUES (?,?,?,1,datetime('now'),datetime('now'))",
            (question, missing_point, category),
        )


def _load_entities(conn, slugs: list[str], count: int) -> list[dict]:
    if slugs:
        placeholders = ",".join("?" * len(slugs))
        rows = conn.execute(
            f"SELECT entity_id, name_zh, name_en, slug, base_type, item_class, url, attributes "  # noqa: S608
            f"FROM entity WHERE slug IN ({placeholders})",
            slugs,
        ).fetchall()
        return [dict(r) for r in rows]

    rows = conn.execute(
        "SELECT entity_id, name_zh, name_en, slug, base_type, item_class, url, attributes "
        "FROM entity WHERE entity_type='unique_item' ORDER BY entity_id"
    ).fetchall()
    all_rows = [dict(r) for r in rows]
    rng = random.Random(SAMPLE_SEED)
    # 按类别分层抽样，避免全抽到同一类
    by_class: dict[str, list[dict]] = {}
    for r in all_rows:
        by_class.setdefault(r["item_class"] or "?", []).append(r)
    picked: list[dict] = []
    classes = sorted(by_class)
    while len(picked) < count and any(by_class[c] for c in classes):
        for c in classes:
            if not by_class[c] or len(picked) >= count:
                continue
            picked.append(by_class[c].pop(rng.randrange(len(by_class[c]))))
    return picked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--count", type=int, default=20, help="抽样条数")
    ap.add_argument("--slug", action="append", default=[], help="指定 slug（可多次）")
    ap.add_argument("--write-en", action="store_true", help="把核对到的权威英文名写回库")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    conn = dbmod.init_db(cfg.db_path)

    entities = _load_entities(conn, args.slug, args.count)
    if not entities:
        print("库中没有可核对的实体，请先运行 python -m kb.pipeline")
        return 1

    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )

    print("=" * 78)
    print(f"列表页 vs 详情页 交叉核对（{len(entities)} 条，限速 "
          f"{cfg.get('crawl.requests_per_second')} req/s）")
    print("=" * 78)

    ok_count = 0
    diff_count = 0
    err_count = 0
    en_updates: list[tuple[str, str]] = []
    all_diffs: list[tuple[str, list[str]]] = []

    for i, ent in enumerate(entities, 1):
        attrs = json.loads(ent["attributes"])
        list_mods = [
            m["text"] for m in attrs.get("mods", [])
            if m["kind"] != "description"
        ]
        list_req = (attrs.get("requirement") or {}).get("raw", "") or ""

        print(f"\n[{i}/{len(entities)}] {ent['name_zh']}  ({ent['slug']})")
        print(f"    {ent['url']}")

        try:
            res = fetcher.fetch(ent["url"])
            if not res.ok:
                print(f"    !! 抓取失败 HTTP {res.status_code}")
                err_count += 1
                continue
            detail = parse_detail_page(res.text)
        except Exception as exc:
            print(f"    !! 解析失败 {type(exc).__name__}: {exc}")
            err_count += 1
            continue

        diffs: list[str] = []

        if detail.name_zh and detail.name_zh != ent["name_zh"]:
            diffs.append(f"中文名不一致：列表={ent['name_zh']} 详情={detail.name_zh}")
        if detail.base_type and detail.base_type != (ent["base_type"] or ""):
            diffs.append(f"底材不一致：列表={ent['base_type']} 详情={detail.base_type}")

        detail_mods = detail.factual_mod_texts
        missing = [m for m in detail_mods if m not in list_mods]
        extra = [m for m in list_mods if m not in detail_mods]
        if missing:
            diffs.append(f"详情页有而列表页缺 {len(missing)} 条: {missing[:3]}")
            # 详情页有、我们没有 → 真实缺口，记台账驱动后续 L2 采集
            for m in missing:
                _record_gap(
                    conn,
                    f"{ent['name_zh']}（{ent['slug']}）的完整词缀",
                    f"详情页有而列表页没有的词条：{m}",
                    "unique_item/detail_only_mod",
                )
        if extra:
            diffs.append(f"列表页有而详情页缺 {len(extra)} 条: {extra[:3]}")

        if detail.requirement_raw and list_req:
            # 需求文本可能空白差异，压缩后比
            a = "".join(detail.requirement_raw.split())
            b = "".join(list_req.split())
            if a != b:
                diffs.append(f"需求不一致：列表={list_req} 详情={detail.requirement_raw}")

        print(f"    词缀：列表 {len(list_mods)} 条 / 详情 {len(detail_mods)} 条")
        print(f"    英文名（详情页权威）：{detail.name_en or '(未取到)'}")
        if detail.name_en and detail.name_en != (ent["name_en"] or ""):
            en_updates.append((ent["entity_id"], detail.name_en))

        if diffs:
            diff_count += 1
            all_diffs.append((ent["name_zh"], diffs))
            for d in diffs:
                print(f"    DIFF {d}")
        else:
            ok_count += 1
            print("    一致")

    conn.commit()

    print("\n" + "=" * 78)
    print(f"核对结果：一致 {ok_count} / 有差异 {diff_count} / 出错 {err_count}")

    if all_diffs:
        print("\n差异汇总：")
        for name, ds in all_diffs:
            print(f"  【{name}】")
            for d in ds:
                print(f"     - {d}")
        n_gap = conn.execute(
            "SELECT count(*) FROM knowledge_gap WHERE guessed_category LIKE 'unique_item/%'"
        ).fetchone()[0]
        print(f"\n  差异已记入缺口台账（当前 {n_gap} 条，可经 /admin/stats 查看）。")
        print("  说明：这类差异多为 POEDB 两个视图的固有差别——列表卡不显示"
              "「不可改变」这类物品属性，详情页才有。属**缺一条事实**而非答错，"
              "补法是对相关物品走 L2 详情页通道。")

    if en_updates:
        print(f"\n从详情页取到 {len(en_updates)} 个权威英文名"
              + ("（已写回）" if args.write_en else "（未写回，加 --write-en 可写入）"))
        for eid, en in en_updates[:8]:
            print(f"    {eid} -> {en}")
        if args.write_en:
            conn.executemany(
                "UPDATE entity SET name_en=?, updated_at=datetime('now') WHERE entity_id=?",
                [(en, eid) for eid, en in en_updates],
            )
            conn.commit()

    dbmod.close_db()
    # 有差异不算失败（可能是详情页含额外信息），但出错要暴露
    return 1 if err_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
