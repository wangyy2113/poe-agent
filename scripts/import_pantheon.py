#!/usr/bin/env python3
"""导入**万神殿**：12 位神祇 / 32 层神力 / 41 条词缀。

补的是从第十七轮起就挂在 `harvested`（0 条入库）的那个域。当时的结论是
「缺的是**来源**而不是抽取器」——**那句是错的**，两个来源一直都在：

* PoB `Data/Pantheons.lua` 出结构、英文名、数值与 GGG stat id；
* POEDB `/cn/Soul_of_<英文名>` 出中文名、中文词缀、抓获头目与抓获地图。

而 `pantheon` 那条 spec **自己就写着** `pob_files=("Data/Pantheons.lua",)`。
判据与边界见两个抽取器的模块 docstring。

**每位神祇入库前逐条对账**：两侧都给 `(stat_id, value)`，所以是全量比对而不是抽样
（实测 41/41 全对）。对不上就整位不入，并把差异写进 `source_conflict`
（`kind='pantheon_review'`，待人工核对，**不作为跨来源冲突呈现给用户**）。

    python scripts/import_pantheon.py --dry-run   # 只清点与对账，不写库
    python scripts/import_pantheon.py             # 导入并重建神祇卡片
    python scripts/import_pantheon.py --fetch     # 缺页时联网补抓（12 页，遵守限速）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from entities.models import Entity, ModLine, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, ModKind, Source, make_entity_id  # noqa: E402
from kb.fetcher import Fetcher, cache_path_for  # noqa: E402
from kb import render_pantheon  # noqa: E402
from kb.sources import pob as pob_mod  # noqa: E402
from kb.sources.pob_pantheon import (  # noqa: E402
    PANTHEON_FILE,
    PantheonStructureError,
    PoBGod,
    load_pantheons,
)
from kb.sources.poedb_pantheon import (  # noqa: E402
    POEDB_BASE_ZH,
    Mismatch,
    PantheonPage,
    PantheonPageError,
    parse_pantheon_page,
    reconcile,
)

#: 数量护栏。万神殿是**固定 12 位神祇**（4 主 8 次）——这不是当前条数而是游戏规则，
#: 所以下限就按规则取，少一位就是抓取或解析出了问题。
MIN_GODS = 12

GAP_QUESTION = "万神殿神力的完整清单"
CONFLICT_KIND = "pantheon_review"


def poedb_url(slug: str) -> str:
    return f"{POEDB_BASE_ZH}/{slug}"


def load_pages(
    gods: list[PoBGod], *, fetcher: Fetcher | None, cache_dir: Path, force: bool
) -> tuple[list[tuple[PoBGod, PantheonPage]], list[tuple[str, str]]]:
    """抓 / 读逐神页并抽取。返回 (成功对, (神祇, 原因) 的失败清单)。"""
    ok: list[tuple[PoBGod, PantheonPage]] = []
    failed: list[tuple[str, str]] = []
    for god in gods:
        url = poedb_url(god.poedb_slug)
        html = ""
        if fetcher is not None:
            res = fetcher.fetch(url, force=force)
            html = res.text if res.ok else ""
        else:
            path = cache_path_for(cache_dir, url)
            if path.is_file():
                html = path.read_text(encoding="utf-8")
        if not html:
            failed.append((god.key, "页面未抓取（加 --fetch 联网补抓）"))
            continue
        try:
            page = parse_pantheon_page(
                html,
                pob_key=god.key,
                name_en=god.base_name_en,
                soul_count=len(god.souls),
            )
        except PantheonPageError as exc:
            # 两道桥接判据不过 = 这一页不是我们要的那位神祇。slug 是推出来的，
            # 推错时 POEDB 给的是别的条目而不是 404——所以必须由页面自证。
            failed.append((god.key, str(exc)))
            continue
        ok.append((god, page))
    return ok, failed


def to_entity(
    god: PoBGod, page: PantheonPage, repo: pob_mod.PoBRepo, priority: int, fetched_at: str
) -> Entity:
    """一位神祇 → 一个实体。

    `entity.mods` 存的是**中文**词缀：`stat_sources` 按 `mods[].text` 匹配，
    存英文会让「哪些渠道给混沌抗性」漏掉万神殿。英文原文另放 attributes。
    """
    tiers = []
    for tier, soul in zip(page.tiers, god.souls):
        tiers.append(
            {
                "tier": tier.index,
                "name_zh": tier.name_zh,
                "name_en": soul.name_en,
                "boss_zh": tier.boss_zh,
                "map_zh": tier.map_zh,
                "act_zh": tier.act_zh,
                "quest_zh": tier.quest_zh,
                "mods_zh": list(tier.mods_zh),
                "mods_en": [m.line_en for m in soul.mods],
                "stats": [[s, v] for s, v in tier.stats],
            }
        )

    # 每一层的中文名都进别名：用户会直接问「寡妇制造者阿玛拉在哪」。
    # 第 1 层的名字就是神祇名（已是 name_zh），不重复收。
    alt = [t["name_zh"] for t in tiers[1:] if t["name_zh"]]

    return Entity(
        entity_id=make_entity_id(EntityType.PANTHEON, page.poedb_slug),
        entity_type=EntityType.PANTHEON,
        name_zh=page.name_zh,
        name_en=page.name_en,
        slug=page.poedb_slug,
        mods=[
            ModLine(kind=ModKind.PANTHEON, text=text)
            for tier in tiers
            for text in tier["mods_zh"]
        ],
        search_keywords=[t["map_zh"] for t in tiers if t["map_zh"]],
        attributes={
            "god_key": page.god_key,
            "is_major": page.is_major,
            "tiers": tiers,
            "name_zh_alt": alt,
            "pob_url": repo.blob_url(PANTHEON_FILE),
            "pob_commit": repo.commit,
        },
        ref=SourceRef(
            source=Source.POEDB.value,
            url=page.poedb_url,
            priority=priority,
            fetched_at=fetched_at,
        ),
        version=repo.version_label,
        content_hash=page.content_hash,
    )


def record_conflicts(conn, rows: list[tuple[PoBGod, list[Mismatch]]]) -> int:  # noqa: ANN001
    """把对不上的神祇写进 `source_conflict`。

    `kind='pantheon_review'` 是**待人工核对**，不是已确认冲突：两侧对不上时更可能的
    解释是「PoB 发的是改动后的版本、POEDB 那一页还是旧版」，而不是「同一层神力两个
    数值」。需求 15.5 的并列展示只用 `kind='mod'`/`'spectre'` 那种已确认冲突。
    """
    for god, mismatches in rows:
        note = "；".join(str(m) for m in mismatches[:3])
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
                f"{god.key}（{god.base_name_en}）", "神力逐条数值", CONFLICT_KIND,
                Source.POEDB.value, mismatches[0].poedb, poedb_url(god.poedb_slug),
                Source.POB.value, mismatches[0].pob, "",
                f"POEDB 逐神页与 PoB Pantheons.lua 对不上：{note}",
            ),
        )
    return len(rows)


def write_rows(conn, pairs: list[tuple[PoBGod, PantheonPage]], *, priority: int,  # noqa: ANN001
               commit: str, fetched_at: str) -> None:
    conn.execute("DELETE FROM pantheon_soul")
    conn.executemany(
        """
        INSERT INTO pantheon_soul(
            god_key, tier, god_name_zh, god_name_en, is_major, name_zh,
            boss_zh, map_zh, act_zh, quest_zh, mods_zh, mods_pob, stats,
            source, source_priority, url, content_hash, source_commit,
            fetched_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        [
            (
                page.god_key, tier.index, page.name_zh, page.name_en,
                int(page.is_major), tier.name_zh, tier.boss_zh, tier.map_zh,
                tier.act_zh, tier.quest_zh,
                json.dumps(list(tier.mods_zh), ensure_ascii=False),
                json.dumps(
                    [
                        {"stat_id": m.stat_id, "value": m.value, "line_en": m.line_en}
                        for m in soul.mods
                    ],
                    ensure_ascii=False,
                ),
                json.dumps([[s, v] for s, v in tier.stats], ensure_ascii=False),
                Source.POEDB.value, priority, page.poedb_url, page.content_hash,
                commit, fetched_at,
            )
            for god, page in pairs
            for tier, soul in zip(page.tiers, god.souls)
        ],
    )


def main() -> int:  # noqa: PLR0915
    parser = argparse.ArgumentParser(description="导入万神殿（PoB × POEDB 逐神页）")
    parser.add_argument("--dry-run", action="store_true", help="只清点与对账，不写库")
    parser.add_argument("--fetch", action="store_true", help="缺页时联网补抓")
    parser.add_argument("--force", action="store_true", help="忽略缓存重新抓取")
    parser.add_argument("--pob-path", default=None, help="PoB 仓库位置")
    args = parser.parse_args()

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()
    started = time.time()

    repo = pob_mod.load_repo(args.pob_path, cfg)
    gods = load_pantheons(repo.data_file(PANTHEON_FILE))
    print(
        f"{repo.version_label}（{repo.short_commit}）："
        f"{len(gods)} 位神祇、{sum(len(g.souls) for g in gods)} 层神力、"
        f"{sum(len(s.mods) for g in gods for s in g.souls)} 条词缀"
    )

    fetcher = None
    if args.fetch:
        fetcher = Fetcher(
            cache_dir=cfg.cache_dir,
            user_agent=str(cfg.get("crawl.user_agent")),
            requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
            timeout=float(cfg.get("crawl.timeout_seconds", 60)),
            cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
        )

    pairs, failed = load_pages(
        gods, fetcher=fetcher, cache_dir=cfg.cache_dir, force=args.force
    )
    for key, why in failed:
        print(f"  ✗ {key}：{why}")

    # ── 对账：两侧都有 (stat_id, value)，所以全量比对而不是抽样 ──
    accepted: list[tuple[PoBGod, PantheonPage]] = []
    rejected: list[tuple[PoBGod, list[Mismatch]]] = []
    for god, page in pairs:
        mismatches = reconcile(page, god)
        if mismatches:
            rejected.append((god, mismatches))
            print(f"  ✗ {god.key} 对账不一致，整位拒绝：")
            for m in mismatches[:3]:
                print(f"      {m}")
        else:
            accepted.append((god, page))

    tiers = sum(len(p.tiers) for _, p in accepted)
    mods = sum(len(t.stats) for _, p in accepted for t in p.tiers)
    print(
        f"对账通过 {len(accepted)}/{len(gods)} 位神祇："
        f"{tiers} 层、{mods} 条词缀逐条一致"
    )

    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0

    if len(accepted) < MIN_GODS:
        print(
            f"数量护栏触发：只有 {len(accepted)} 位神祇通过，"
            f"低于下限 {MIN_GODS}（万神殿固定 4 主 8 次），已拒绝写入"
        )
        return 2

    priority = int(cfg.priority_of(Source.POEDB.value))
    fetched_at = max(
        (
            cache_path_for(cfg.cache_dir, p.poedb_url).stat().st_mtime
            for _, p in accepted
        ),
        default=0,
    )
    fetched_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(fetched_at))

    cur = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at) VALUES (?,?,datetime('now'))",
        (Source.POEDB.value, "pantheon"),
    )
    run_id = cur.lastrowid
    conn.commit()

    entities = [
        to_entity(god, page, repo, priority, fetched_iso) for god, page in accepted
    ]

    conn.execute("SAVEPOINT pantheon_snapshot")
    try:
        write_rows(
            conn, accepted, priority=priority, commit=repo.commit, fetched_at=fetched_iso
        )
        conflicts = record_conflicts(conn, rejected)
        gap_rows = record_gaps(conn, failed, rejected)
        # 复用 pipeline 的那一处：清 FTS/别名 → 删 entity → 写 entity →
        # 建词表 → 渲卡片 → 建别名，**顺序有讲究**，不要再抄一遍
        from kb.pipeline import _reindex_entity_type  # noqa: PLC0415

        chunks, aliases = _reindex_entity_type(
            conn, EntityType.PANTHEON, entities, render_pantheon.to_chunks
        )
        conn.execute("RELEASE pantheon_snapshot")
    except Exception:
        conn.execute("ROLLBACK TO pantheon_snapshot")
        conn.execute("RELEASE pantheon_snapshot")
        raise

    elapsed_ms = int((time.time() - started) * 1000)
    note = json.dumps(
        {
            "gods": len(accepted),
            "tiers": tiers,
            "mods": mods,
            "rejected_mismatch": len(rejected),
            "missing_page": len(failed),
            "conflicts": conflicts,
            "gap_rows": gap_rows,
            "chunks": chunks,
            "aliases": aliases,
        },
        ensure_ascii=False,
    )
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, failed=0, "
        "elapsed_ms=?, note=? WHERE run_id=?",
        (len(gods), len(accepted), elapsed_ms, note, run_id),
    )
    conn.commit()

    print(
        f"入库完成：{len(accepted)} 位神祇、{tiers} 层、{mods} 条词缀"
        f"（卡片 {chunks}、别名 {aliases}），耗时 {elapsed_ms}ms"
    )
    print("提示：重跑 `python -m kb.pipeline --category coverage` 刷新覆盖声明。")
    return 0


def record_gaps(conn, failed: list[tuple[str, str]],  # noqa: ANN001
                rejected: list[tuple[PoBGod, list[Mismatch]]]) -> int:
    """登记未入库的神祇。量级是**缺多少位**，不是跑了几次。"""
    points: list[tuple[str, int]] = []
    if failed:
        names = "、".join(k for k, _ in failed[:3])
        points.append(
            (
                f"[no_page] {len(failed)} 位神祇的 POEDB 逐神页取不到：{names}"
                f"{' 等' if len(failed) > 3 else ''}",
                len(failed),
            )
        )
    if rejected:
        names = "、".join(g.key for g, _ in rejected[:3])
        points.append(
            (
                f"[detail_mismatch] {len(rejected)} 位神祇两侧逐条数值对不上，未入库："
                f"{names}{' 等' if len(rejected) > 3 else ''}（见 source_conflict）",
                len(rejected),
            )
        )
    if not points:
        return 0
    return dbmod.record_ingest_gaps(
        conn, question=GAP_QUESTION, category="not_collected/pantheon", points=points
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (PantheonStructureError, PantheonPageError) as exc:
        print(f"结构断言失败，已拒绝写入：{exc}")
        raise SystemExit(2) from exc
    except pob_mod.PoBNotFound as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    finally:
        dbmod.close_db()
