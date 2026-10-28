#!/usr/bin/env python3
"""从已缓存的 POEDB 类别页为现有底材补中文固有隐式。

    python scripts/backfill_base_implicits.py preview
    python scripts/backfill_base_implicits.py apply

先完整核对 PoB commit、底材身份和中文卡片数值；apply 先做 SQLite 一致性备份，
再用事务内的二次身份校验防止预检后底材快照变化。只更新隐式列与行内容哈希。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.backup import BackupError, create_backup  # noqa: E402
from core.config import get_config  # noqa: E402
from kb.sources.pob import PoBNotFound, load_repo  # noqa: E402
from kb.sources.pob_bases import (  # noqa: E402
    CATEGORIES, BaseItem, BaseStructureError, extract_category_bases,
)
from scripts.import_pob_bases import _page_html  # noqa: E402


@dataclass(frozen=True)
class BackfillPlan:
    source_fingerprint: str
    bases: tuple[BaseItem, ...]
    changed_ids: tuple[str, ...]
    chinese_count: int
    translation_drift_count: int


def _db_rows(conn: sqlite3.Connection) -> list[dict]:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(pob_base_item)")}
    if not cols:
        raise BaseStructureError("底材表不存在，请先导入 PoB 底材")
    implicit = "implicit_zh" if "implicit_zh" in cols else "NULL AS implicit_zh"
    rows = conn.execute(
        "SELECT base_id,name_en,name_zh,poedb_slug,poedb_page,implicit_text,"
        f"source_commit,content_hash,{implicit} FROM pob_base_item ORDER BY base_id"
    ).fetchall()
    return [dict(row) for row in rows]


def build_plan(
    conn: sqlite3.Connection, bases: list[BaseItem], commit: str
) -> BackfillPlan:
    """拒绝对旧 PoB 版本或不同底材快照做就地跨源回填。"""
    rows = _db_rows(conn)
    current = {row["base_id"]: row for row in rows}
    incoming = {base.base_id: base for base in bases}
    if len(current) != len(rows) or len(incoming) != len(bases) or current.keys() != incoming.keys():
        raise BaseStructureError(
            "当前底材快照与本地来源清单不一致；请先完整刷新底材，不能局部回填隐式"
        )
    changed: list[str] = []
    publish_bases: list[BaseItem] = []
    translation_drift = 0
    for base_id, row in current.items():
        base = incoming[base_id]
        expected = (
            base.name_en, base.poedb_slug, base.poedb_page,
            base.implicit_text, commit,
        )
        observed = tuple(row[key] for key in (
            "name_en", "poedb_slug", "poedb_page", "implicit_text",
            "source_commit",
        ))
        if observed != expected:
            raise BaseStructureError(
                f"底材 {base.name_en} 身份或 PoB 版本漂移；请先完整刷新底材"
            )
        if row["name_zh"] != base.name_zh:
            # 旧库仍可能保留上一版译名。只跳过该底材，不把当前卡片的中文隐式
            # 搭到旧中文名上；其它底材仍可安全回填。
            base = replace(base, name_zh=row["name_zh"], implicit_zh=None)
            translation_drift += 1
        previous = replace(base, implicit_zh=row["implicit_zh"])
        if row["content_hash"] not in {
            base.legacy_content_hash, previous.content_hash,
        }:
            raise BaseStructureError(
                f"底材 {base.name_en} 内容哈希漂移；请先完整刷新底材"
            )
        publish_bases.append(base)
        if row["implicit_zh"] != base.implicit_zh or row["content_hash"] != base.content_hash:
            changed.append(base_id)
    fingerprint = hashlib.sha256(json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return BackfillPlan(
        source_fingerprint=fingerprint,
        bases=tuple(publish_bases),
        changed_ids=tuple(changed),
        chinese_count=sum(bool(base.implicit_zh) for base in publish_bases),
        translation_drift_count=translation_drift,
    )


def apply_plan(conn: sqlite3.Connection, plan: BackfillPlan, commit: str) -> int:
    """一次事务只补列与哈希；备份应由调用方先创建。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        fresh = build_plan(conn, list(plan.bases), commit)
        if fresh.source_fingerprint != plan.source_fingerprint:
            raise BaseStructureError("备份后底材快照发生变化，拒绝发布中文隐式")
        by_id = {base.base_id: base for base in plan.bases}
        for base_id in plan.changed_ids:
            base = by_id[base_id]
            conn.execute(
                "UPDATE pob_base_item SET implicit_zh=?,content_hash=?,"
                "updated_at=datetime('now') WHERE base_id=?",
                (base.implicit_zh, base.content_hash, base_id),
            )
        conn.execute(
            "INSERT INTO ingest_run(source,category,started_at,finished_at,total,"
            "succeeded,failed,note) VALUES ('POEDB','base_implicit_zh',"
            "datetime('now'),datetime('now'),?,?,0,?)",
            (len(plan.bases), plan.chinese_count,
             f"中文固有隐式 {plan.chinese_count} 条；更新底材行 {len(plan.changed_ids)} 条"),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(plan.changed_ids)


def _extract() -> tuple[list[BaseItem], str]:
    cfg = get_config()
    repo = load_repo(None, cfg)
    if not repo.reproducible or len(repo.commit) != 40:
        raise BaseStructureError("PoB commit 未钉住，拒绝回填不可复现来源")
    bases: list[BaseItem] = []
    for category in CATEGORIES:
        got, _skipped = extract_category_bases(
            category, repo.root, _page_html(category, fetcher=None)
        )
        bases.extend(got)
    return bases, repo.commit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "apply"))
    args = parser.parse_args()
    cfg = get_config()
    try:
        bases, commit = _extract()
        if not cfg.db_path.is_file():
            raise BaseStructureError("数据库不存在，请先导入 PoB 底材")
        with sqlite3.connect(
            cfg.db_path.resolve(strict=True).as_uri() + "?mode=ro", uri=True
        ) as conn:
            conn.row_factory = sqlite3.Row
            plan = build_plan(conn, bases, commit)
        print(
            f"底材 {len(plan.bases)} 条；中文隐式 {plan.chinese_count} 条；"
            f"译名漂移跳过 {plan.translation_drift_count} 条；"
            f"待更新行 {len(plan.changed_ids)} 条"
        )
        if args.action == "preview":
            return 0
        backup = create_backup(cfg.db_path, cfg.backup_dir, source_name="base-implicit-zh")
        dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
        conn = dbmod.get_conn()
        updated = apply_plan(conn, plan, commit)
        print(f"已更新 {updated} 条；安全备份 {backup.backup_name}")
        return 0
    except (BaseStructureError, PoBNotFound, BackupError, sqlite3.Error, OSError) as exc:
        print(f"[中止] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
