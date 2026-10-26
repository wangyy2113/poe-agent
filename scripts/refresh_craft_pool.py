#!/usr/bin/env python3
"""刷新七张胸甲词缀页，让普通同侧概率池恢复可验证的来源快照。

    python scripts/refresh_craft_pool.py status
    python scripts/refresh_craft_pool.py preview
    python scripts/refresh_craft_pool.py apply

preview/apply 都强制向 POEDB 重新抓取七页并完整解析。preview 不改数据库；
apply 先创建 SQLite 一致性备份，再事务化发布七页的 normal 行、审计和词缀卡片索引。
精华、影响、地渊等其它来源行保留原样，避免上游 hover 哈希改版破坏 PoB 标识关联。
任何一页失败都不会发布部分词缀池。运行时遵守项目抓取器的 robots 与限速配置。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backup import BackupError, create_backup  # noqa: E402
from core.config import get_config  # noqa: E402
from craft.pool import verify_affix_page_snapshot  # noqa: E402
from craft.pool_refresh import (  # noqa: E402
    POOL_PAGES,
    PoolRefreshError,
    apply_refresh,
    plan_refresh,
    prepare_refresh,
)
from kb.pipeline import _make_fetcher  # noqa: E402


def _connect(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    for slug in POOL_PAGES:
        proof, reason = verify_affix_page_snapshot(conn, slug)
        pages.append({
            "slug": slug,
            "ready": proof is not None,
            "reason": reason,
            "proof": proof.to_dict() if proof else None,
        })
    return {
        "ready": all(page["ready"] for page in pages),
        "ready_pages": sum(page["ready"] for page in pages),
        "total_pages": len(pages),
        "pages": pages,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="只读检查七页来源证明与过期状态")
    sub.add_parser("preview", help="强制抓取七页并展示预检与行集变化，不写数据库")
    sub.add_parser("apply", help="强制抓取、预检、备份后原子发布七页和索引")
    return parser


def _emit(value: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if value.get("error"):
        print(f"失败：{value['error']}")
        return
    if "pages" in value:
        print(f"来源证明：{value['ready_pages']}/{value['total_pages']} 页就绪")
        for page in value["pages"]:
            print(f"  {page['slug']}: {'就绪' if page['ready'] else page['reason']}")
        return
    print(f"normal 行预检：{value['page_count']} 页，"
          f"{value['old_rows']} → {value['new_rows']} 行，"
          f"新增 {value['added']} / 移除 {value['removed']}；"
          f"保留旧 ID {value['preserved_legacy_ids']}，新不透明 ID "
          f"{value['new_opaque_ids']}")
    for page in value["changes"]:
        print(f"  {page['slug']}: {page['old_rows']} → {page['new_rows']}，"
              f"+{page['added']}/-{page['removed']}，旧 ID "
              f"{page['preserved_legacy_ids']}，正权重前/后缀 "
              f"{page['positive_prefixes']}/{page['positive_suffixes']}")
    if value.get("applied"):
        print(f"已发布；词缀卡片 {value['indexed_mod_chunks']} 张；"
              f"安全备份 {value['backup_name']}")
    else:
        print("未修改数据库；检查变化后运行 apply 发布。")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    cfg = get_config()
    try:
        if args.command == "status":
            with closing(_connect(cfg.db_path, readonly=True)) as conn:
                result = status(conn)
            _emit(result, json_output=args.json)
            return 0 if result["ready"] else 2

        pages = prepare_refresh(_make_fetcher(cfg))
        with closing(_connect(cfg.db_path, readonly=True)) as conn:
            plan = plan_refresh(conn, pages)
        if args.command == "preview":
            _emit(plan.to_dict(), json_output=args.json)
            return 0

        backup = create_backup(cfg.db_path, cfg.backup_dir)
        with closing(_connect(cfg.db_path, readonly=False)) as conn:
            result = apply_refresh(conn, plan)
        result["backup_name"] = backup.backup_name
        _emit(result, json_output=args.json)
        return 0
    except (PoolRefreshError, BackupError, sqlite3.Error, OSError) as exc:
        _emit({"ready": False, "error": str(exc)}, json_output=args.json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
