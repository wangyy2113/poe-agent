#!/usr/bin/env python3
"""导入**前后缀名额规则**（POEDB `/cn/Modifiers` × PoB `Classes/Item.lua`）。

补的是 `craft/feasibility.py` 从第一版起就写着的那条缺口：

    前后缀容量、来源适用条件和影响力要求尚未结构化入库。缺这些规则时结论必须是
    ``unknown``……模型允许未来补齐规则后返回 ``possible``，但本版不会凭常识产生它。

容量是三条缺口里**唯一无条件生效**的那条（另外两条只在候选含特殊来源或影响力时
才追加），所以补上它之后，最常见的问法第一次能得到 `possible`。

两侧对账：魔法 1+1 与稀有 3+3 **两边都说**，逐条比对；不重叠的各自标记
`poedb_only` / `pob_only`，**单侧来源不是冲突**。真对不上才写 `source_conflict`
（`kind='craft_rule_review'`，待人工核对，不作为跨来源冲突呈现）。

    python scripts/import_craft_rules.py --dry-run   # 只对账，不写库
    python scripts/import_craft_rules.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from entities.models import Entity, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, Source, make_entity_id  # noqa: E402
from kb import render_craft_rule  # noqa: E402
from kb.fetcher import cache_path_for  # noqa: E402
from kb.sources import pob as pob_mod  # noqa: E402
from kb.sources.poedb_modifiers import (  # noqa: E402
    MODIFIERS_URL,
    ModifiersStructureError,
    extract_affix_limits,
)
from kb.sources.pob_affix_limit import (  # noqa: E402
    ITEM_CLASS_FILE,
    AffixLimitSourceError,
    load_affix_limits,
)

CONFLICT_KIND = "craft_rule_review"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))

#: 规则条数下限。两侧合起来实测 6 条（魔法 default/flask、稀有 default/jewel/graft/
#: jewel_abyss_corrupted、传奇 default）。少于这个数说明某一侧没抽出来。
MIN_RULES = 5


def main() -> int:
    parser = argparse.ArgumentParser(description="导入前后缀名额规则")
    parser.add_argument("--dry-run", action="store_true", help="只对账，不写库")
    parser.add_argument("--pob-path", default=None, help="PoB 仓库位置")
    args = parser.parse_args()

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()

    page = cache_path_for(cfg.cache_dir, MODIFIERS_URL)
    if not page.is_file():
        print(f"POEDB 词缀页未抓取：{MODIFIERS_URL}\n先跑 `python -m kb.pipeline --harvest`")
        return 2
    poedb_rules = extract_affix_limits(page.read_text(encoding="utf-8", errors="ignore"))

    repo = pob_mod.load_repo(args.pob_path, cfg)
    pob_rules = load_affix_limits(repo.data_file(ITEM_CLASS_FILE))
    print(
        f"POEDB {len(poedb_rules)} 条、PoB {len(pob_rules)} 条"
        f"（{repo.version_label} {repo.short_commit}）"
    )

    by_key: dict[tuple[str, str], dict] = {}
    for rule in poedb_rules:
        by_key[(rule.rarity, rule.item_kind)] = {
            "rarity": rule.rarity,
            "item_kind": rule.item_kind,
            "prefix_limit": rule.prefix_limit,
            "suffix_limit": rule.suffix_limit,
            "poedb_text": rule.source_text,
            "pob_text": None,
            "pob_line": None,
        }

    mismatches: list[tuple[str, str, str]] = []
    for rule in pob_rules:
        key = (rule.rarity, rule.item_kind)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = {
                "rarity": rule.rarity,
                "item_kind": rule.item_kind,
                "prefix_limit": rule.prefix_limit,
                "suffix_limit": rule.suffix_limit,
                "poedb_text": None,
                "pob_text": rule.source_text,
                "pob_line": rule.line,
            }
            continue
        existing["pob_text"] = rule.source_text
        existing["pob_line"] = rule.line
        # 重叠部分逐条比对——这就是这一维的对账口径
        if (existing["prefix_limit"], existing["suffix_limit"]) != (
            rule.prefix_limit,
            rule.suffix_limit,
        ):
            mismatches.append(
                (
                    f"{rule.rarity}·{rule.item_kind}",
                    f"{existing['prefix_limit']}+{existing['suffix_limit']}",
                    f"{rule.prefix_limit}+{rule.suffix_limit}",
                )
            )

    rows = []
    for key in sorted(by_key):
        item = by_key[key]
        has_poedb = item["poedb_text"] is not None
        has_pob = item["pob_text"] is not None
        agreement = "both" if has_poedb and has_pob else ("poedb_only" if has_poedb else "pob_only")
        rows.append({**item, "agreement": agreement})

    overlap = [r for r in rows if r["agreement"] == "both"]
    print(f"两侧都有 {len(overlap)} 条，其中不一致 {len(mismatches)} 条")
    for subject, a, b in mismatches:
        print(f"  ✗ {subject}：POEDB {a} ≠ PoB {b}")
    for row in rows:
        limits = (
            "不受限制"
            if row["prefix_limit"] is None
            else f"{row['prefix_limit']}+{row['suffix_limit']}"
        )
        print(f"  {row['rarity']:4s} {row['item_kind']:22s} {limits:10s} [{row['agreement']}]")

    if len(rows) < MIN_RULES:
        print(f"规则条数 {len(rows)} 低于下限 {MIN_RULES}，拒绝写入")
        return 2
    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0

    priority = int(cfg.priority_of(Source.POEDB.value))
    conn.execute("SAVEPOINT craft_rule_snapshot")
    try:
        conn.execute("DELETE FROM craft_affix_capacity")
        conn.executemany(
            """
            INSERT INTO craft_affix_capacity(
                rarity, item_kind, prefix_limit, suffix_limit, agreement,
                poedb_text, pob_text, source, source_priority, url,
                pob_url, source_commit, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
            """,
            [
                (
                    row["rarity"], row["item_kind"], row["prefix_limit"],
                    row["suffix_limit"], row["agreement"], row["poedb_text"],
                    row["pob_text"],
                    # 推荐引用 POEDB（P1）；只有 PoB 说的那几条就引 PoB
                    Source.POEDB.value if row["poedb_text"] else Source.POB.value,
                    priority if row["poedb_text"] else int(cfg.priority_of(Source.POB.value)),
                    MODIFIERS_URL if row["poedb_text"]
                    else repo.blob_url(ITEM_CLASS_FILE, line=row["pob_line"]),
                    repo.blob_url(ITEM_CLASS_FILE, line=row["pob_line"])
                    if row["pob_line"] else None,
                    repo.commit or None,
                )
                for row in rows
            ],
        )
        for subject, a, b in mismatches:
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
                    subject, "前后缀名额", CONFLICT_KIND,
                    Source.POEDB.value, a, MODIFIERS_URL,
                    Source.POB.value, b, repo.blob_url(ITEM_CLASS_FILE),
                    "POEDB 词缀页与 PoB Classes/Item.lua 的名额规则对不上",
                ),
            )
        # 规则除了进表还要出一张卡：只进表的话它**只被工具内部消费**，
        # 而「稀有装备最多几个前缀」这种直接问法一条都命中不了
        # （实测 sufficient=False 却 covered 里有 craft_rule）。入库不等于取得出来。
        from kb.pipeline import _reindex_entity_type  # noqa: PLC0415

        entity = Entity(
            entity_id=make_entity_id(EntityType.CRAFT_RULE, "affix_capacity"),
            entity_type=EntityType.CRAFT_RULE,
            name_zh="前后缀名额规则",
            name_en="Affix Capacity",
            slug="affix_capacity",
            search_keywords=["前缀", "后缀", "名额", "上限", "稀有", "魔法"],
            attributes={"rules": rows},
            ref=SourceRef(
                source=Source.POEDB.value,
                url=MODIFIERS_URL,
                priority=priority,
                fetched_at=_iso(page.stat().st_mtime),
            ),
            # 空 content_hash 会让增量判据把这条实体当成「每次都变了」，
            # `selfcheck.py` 有专门一项在盯（第三十四轮加的）。
            content_hash=hashlib.sha256(
                json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        )
        chunks, aliases = _reindex_entity_type(
            conn, EntityType.CRAFT_RULE, [entity], render_craft_rule.to_chunks
        )
        conn.execute("RELEASE craft_rule_snapshot")
    except Exception:
        conn.execute("ROLLBACK TO craft_rule_snapshot")
        conn.execute("RELEASE craft_rule_snapshot")
        raise
    conn.commit()

    print(
        f"入库完成：{len(rows)} 条名额规则（不一致 {len(mismatches)} 条已登记待核对；"
        f"卡片 {chunks}、别名 {aliases}）"
    )
    print("提示：重跑 `python -m kb.pipeline --category coverage` 刷新覆盖声明。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ModifiersStructureError, AffixLimitSourceError) as exc:
        print(f"结构断言失败，已拒绝写入：{exc}")
        raise SystemExit(2) from exc
    except pob_mod.PoBNotFound as exc:
        print(str(exc))
        raise SystemExit(2) from exc
    finally:
        dbmod.close_db()
