#!/usr/bin/env python3
"""真实数据只读验证：胸甲 normal 同侧完整池与条件权重概率。

只读打开生产 ``data/poe.db``，分别验证三件事：生产库最新 page audit 是否足以
放行线上 exact；当前缓存能否逐页迁移为生产旧行集的 provenance；以及当前缓存与
当前 parser 能否在全新内存库中形成自洽的 v2 快照并通过算法验证。迁移资格与算法
验证严格分轨，缓存变化绝不能给旧生产行集补签审计。脚本不会写生产库。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db import SCHEMA  # noqa: E402
from core.config import get_config  # noqa: E402
from craft.pool import (  # noqa: E402
    POOL_SNAPSHOT_MODE,
    affix_page_digest,
    analyze_affix_pool,
    verify_affix_page_snapshot,
)
from kb.fetcher import FetchResult, cache_path_for  # noqa: E402
from kb.pipeline import _mod_page_audit, _replace_mod_page  # noqa: E402
from kb.sources import poedb_mods  # noqa: E402
from kb.sources.pob import load_repo  # noqa: E402
from kb.sources.pob_bases import (  # noqa: E402
    ARMOUR_TAG_TO_PAGE,
    POEDB_BODY_CN_URL,
    POEDB_BODY_EN_URL,
    extract_body_armour_bases,
)
from scripts.import_pob_bases import replace_snapshot  # noqa: E402

EXPECTED_POSITIVE_NORMAL = {
    "Body_Armours_str": 109,
    "Body_Armours_dex": 109,
    "Body_Armours_int": 125,
    "Body_Armours_str_dex": 124,
    "Body_Armours_str_int": 140,
    "Body_Armours_dex_int": 140,
    "Body_Armours_str_dex_int": 206,
}
PAGE_TO_TAG = {page: tag for tag, page in ARMOUR_TAG_TO_PAGE.items()}


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _build_cached_page_audits(
    conn: sqlite3.Connection, cache_dir: Path
) -> dict[str, dict[str, object]]:
    """在空内存库中用同一次缓存解析建立自洽 v2 页审计。"""
    pages: dict[str, dict[str, object]] = {}
    for page in EXPECTED_POSITIVE_NORMAL:
        url = f"https://poedb.tw/cn/{page}"
        path = cache_path_for(cache_dir, url)
        html = path.read_text(encoding="utf-8")
        mods, stats = poedb_mods.extract_mods(html, page, url)
        count, digest = _replace_mod_page(conn, page, mods)
        fetched_at = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
        result = FetchResult(
            url=url,
            text=html,
            content_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
            from_cache=True,
            fetched_at=fetched_at,
            final_url=url,
        )
        pages[page] = _mod_page_audit(
            slug=page,
            url=url,
            result=result,
            rows=count,
            digest=digest,
            stats=stats,
        )
    conn.execute(
        "INSERT INTO ingest_run(source,category,started_at,finished_at,total,succeeded,"
        "failed,note) VALUES ('POEDB','mod_affix',datetime('now'),datetime('now'),7,7,0,?)",
        (json.dumps({"snapshot_mode": POOL_SNAPSHOT_MODE, "pages": pages}, sort_keys=True),),
    )
    conn.commit()
    return pages


def _compare_page_snapshots(
    production: sqlite3.Connection, current: sqlite3.Connection
) -> dict[str, dict[str, object]]:
    """只判断新缓存能否为旧生产行集提供逐页 provenance，不修改任一行集。"""
    comparison: dict[str, dict[str, object]] = {}
    for page in EXPECTED_POSITIVE_NORMAL:
        production_count, production_digest = affix_page_digest(production, page)
        current_count, current_digest = affix_page_digest(current, page)
        comparison[page] = {
            "matches": (production_count, production_digest)
            == (current_count, current_digest),
            "production_rows": production_count,
            "production_digest": production_digest,
            "current_rows": current_count,
            "current_digest": current_digest,
        }
    return comparison


def _resolved_weight(weights: dict[str, int], page: str) -> int | None:
    for tag in (PAGE_TO_TAG[page], "body_armour", "armour", "default"):
        if tag in weights:
            return int(weights[tag])
    return None


def _p1_p2_weight_audit(conn: sqlite3.Connection) -> tuple[int, int, int]:
    pages = tuple(EXPECTED_POSITIVE_NORMAL)
    placeholders = ",".join("?" for _ in pages)
    p1_rows = conn.execute(
        f"SELECT mod_code,item_class_page,gen_type,ilvl,weight FROM mod_affix "
        f"WHERE item_class_page IN ({placeholders}) AND category='normal' AND weight>0",
        pages,
    ).fetchall()
    matched = unmapped = conflicts = 0
    for row in p1_rows:
        decoded = unquote(str(row["mod_code"] or ""))
        mod_id = re.split(r"[\\/]", decoded)[-1]
        candidates = conn.execute(
            "SELECT weights FROM pob_mod WHERE mod_id=? AND ilvl=? AND affix_type=?",
            (
                mod_id,
                row["ilvl"],
                "Prefix" if row["gen_type"] == 1 else "Suffix",
            ),
        ).fetchall()
        values: list[int] = []
        for candidate in candidates:
            try:
                weights = json.loads(candidate["weights"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(weights, dict):
                continue
            value = _resolved_weight(weights, str(row["item_class_page"]))
            if value is not None:
                values.append(value)
        if not values:
            unmapped += 1
        elif int(row["weight"]) in values:
            matched += 1
        else:
            conflicts += 1
    return matched, unmapped, conflicts


def main() -> int:
    cfg = get_config()
    db_path = Path(cfg.db_path)
    before = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    production_audit = {
        page: verify_affix_page_snapshot(source, page)
        for page in EXPECTED_POSITIVE_NORMAL
    }
    production_ready = sum(proof is not None for proof, _reason in production_audit.values())
    production_weight_audit = _p1_p2_weight_audit(source)
    production_positive_rows = int(source.execute(
        "SELECT count(*) FROM mod_affix WHERE item_class_page IN "
        f"({','.join('?' for _ in EXPECTED_POSITIVE_NORMAL)}) "
        "AND category='normal' AND weight>0",
        tuple(EXPECTED_POSITIVE_NORMAL),
    ).fetchone()[0])
    if (
        sum(production_weight_audit) != production_positive_rows
        or production_weight_audit[2] != 0
    ):
        raise AssertionError(
            "生产快照 P1/P2 权重审计不闭合："
            f"matched={production_weight_audit[0]}, "
            f"unmapped={production_weight_audit[1]}, "
            f"conflicts={production_weight_audit[2]}, "
            f"expected_total={production_positive_rows}"
        )

    target = _memory_db()
    try:
        repo = load_repo(None, cfg)
        cn_path = cache_path_for(cfg.cache_dir, POEDB_BODY_CN_URL)
        en_path = cache_path_for(cfg.cache_dir, POEDB_BODY_EN_URL)
        bases = extract_body_armour_bases(
            repo.root,
            cn_path.read_text(encoding="utf-8"),
            en_path.read_text(encoding="utf-8"),
        )
        replace_snapshot(target, bases, repo, repo.priority(cfg))

        # P2 对账需要 pob_mod，但仍只复制到全新内存库。
        pob_columns = [str(row[1]) for row in source.execute("PRAGMA table_info(pob_mod)")]
        pob_sql = (
            f"INSERT INTO pob_mod({','.join(pob_columns)}) "
            f"VALUES ({','.join('?' for _ in pob_columns)})"
        )
        pob_rows = source.execute(f"SELECT {','.join(pob_columns)} FROM pob_mod").fetchall()
        target.executemany(
            pob_sql, [tuple(row[column] for column in pob_columns) for row in pob_rows]
        )

        # 当前缓存只为同一次 fresh parse 建立审计，绝不覆盖或补签生产旧行集。
        page_audits = _build_cached_page_audits(target, cfg.cache_dir)
        snapshot_comparison = _compare_page_snapshots(source, target)
        migration_mismatches = {
            page: values
            for page, values in snapshot_comparison.items()
            if values["matches"] is not True
        }
        fresh_audit = {
            page: verify_affix_page_snapshot(target, page)
            for page in EXPECTED_POSITIVE_NORMAL
        }
        fresh_ready = sum(proof is not None for proof, _reason in fresh_audit.values())
        if fresh_ready != len(EXPECTED_POSITIVE_NORMAL):
            reasons = sorted({
                reason for proof, reason in fresh_audit.values() if proof is None
            })
            raise AssertionError(f"当前缓存 fresh v2 审计未闭合：{reasons}")

        by_tag = Counter(base.armour_tag for base in bases)
        if len(bases) != 123:
            raise AssertionError(f"PoB 胸甲底材应为 123，实际 {len(bases)}")
        if set(by_tag) != set(ARMOUR_TAG_TO_PAGE):
            raise AssertionError(f"胸甲属性标签集合异常：{dict(by_tag)}")

        actual_counts = {
            page: int(target.execute(
                "SELECT count(*) FROM mod_affix WHERE item_class_page=? "
                "AND category='normal' AND weight>0",
                (page,),
            ).fetchone()[0])
            for page in EXPECTED_POSITIVE_NORMAL
        }
        if actual_counts != EXPECTED_POSITIVE_NORMAL:
            raise AssertionError(
                f"七页 normal 正权重行数变化：expected={EXPECTED_POSITIVE_NORMAL}, "
                f"actual={actual_counts}"
            )
        invalid = int(target.execute(
            "SELECT count(*) FROM mod_affix WHERE item_class_page IN "
            f"({','.join('?' for _ in EXPECTED_POSITIVE_NORMAL)}) AND category='normal' "
            "AND (weight IS NULL OR weight<0)",
            tuple(EXPECTED_POSITIVE_NORMAL),
        ).fetchone()[0])
        if invalid:
            raise AssertionError(f"七页存在 {invalid} 条 NULL/负权重 normal 行")

        fresh_weight_audit = _p1_p2_weight_audit(target)
        fresh_total = sum(actual_counts.values())
        if (
            sum(fresh_weight_audit) != fresh_total
            or fresh_weight_audit[2] != 0
        ):
            raise AssertionError(
                "当前缓存 P1/P2 权重审计不闭合："
                f"matched={fresh_weight_audit[0]}, unmapped={fresh_weight_audit[1]}, "
                f"conflicts={fresh_weight_audit[2]}, expected_total={fresh_total}"
            )

        report = analyze_affix_pool(
            target,
            base_type="铁制背心",
            item_level=100,
            generation_side="prefix",
            target_query="IncreasedLife",
        )
        if report.status != "exact":
            raise AssertionError(f"代表性条件池未形成 exact：{report.to_dict()}")

        forbidden_keys = {"steps", "currency_cost", "market_cost", "fastest_route"}
        leaked = forbidden_keys & set(report.to_dict())
        if leaked:
            raise AssertionError(f"条件池结果越界字段：{sorted(leaked)}")

        print("=" * 84)
        print("Body Armour Normal Affix Pool & Conditional Raw Odds v1")
        print("=" * 84)
        print(f"PoB：{repo.version_label} commit={repo.commit}")
        print(f"胸甲底材：{len(bases)}；标签分布：{dict(sorted(by_tag.items()))}")
        print(f"七页 normal 正权重行：{actual_counts}")
        print(f"生产库真实 v2 page audit：{production_ready}/7 页可放行 exact")
        if production_ready != len(EXPECTED_POSITIVE_NORMAL):
            reasons = sorted({
                reason for proof, reason in production_audit.values() if proof is None
            })
            print(
                "  fail-closed：生产库尚未完成 v2 来源审计，线上条件池会返回 unknown；"
                f"原因={reasons}"
            )

        migration_status = "eligible" if not migration_mismatches else "not_migratable"
        print(
            "当前缓存 → 生产旧行集 provenance 迁移资格："
            f"{migration_status}（不匹配 {len(migration_mismatches)}/7 页；未写生产库）"
        )
        for page, values in migration_mismatches.items():
            print(
                f"  {page}: production={values['production_rows']} rows/"
                f"{str(values['production_digest'])[:12]}，current={values['current_rows']} rows/"
                f"{str(values['current_digest'])[:12]}"
            )

        print(
            "当前缓存 × fresh 内存行集 × v2 审计："
            + ", ".join(
                f"{page}={audit['rows']} rows/{str(audit['digest'])[:12]}"
                f"/src:{str(audit['source_content_hash'])[:12]}"
                for page, audit in page_audits.items()
            )
        )
        print(
            "生产快照 P1/P2 权重审计："
            f"匹配 {production_weight_audit[0]}，P2 无对应 {production_weight_audit[1]}，"
            f"冲突 {production_weight_audit[2]}（按当前行数闭合，不依赖 legacy 固定计数）"
        )
        print(
            "当前缓存 P1/P2 canonical-ID 覆盖："
            f"匹配 {fresh_weight_audit[0]}，P2 无对应 {fresh_weight_audit[1]}，"
            f"冲突 {fresh_weight_audit[2]}（仅报告覆盖退化，不伪造映射）"
        )
        print(
            "代表性 fresh-cache exact：铁制背心 / ilvl 100 / prefix / IncreasedLife -> "
            f"{report.target_weight}/{report.total_weight} = "
            f"{report.probability_numerator}/{report.probability_denominator} "
            f"({report.probability_decimal})，digest={report.pool_digest}"
        )
    finally:
        target.close()
        source.close()

    after = (db_path.stat().st_size, db_path.stat().st_mtime_ns)
    if after != before:
        raise AssertionError(f"生产数据库发生变化：before={before}, after={after}")
    print(f"生产库只读校验：size/mtime 未变 {after}")
    print(
        "结论：当前缓存算法验证通过；旧行集迁移资格单独报告；"
        "生产 v2 审计未就绪时保持 unknown，不伪装 exact"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
