#!/usr/bin/env python3
"""跨平台 SQLite 备份与离线恢复 CLI。

示例：
    python scripts/db_backup.py create
    python scripts/db_backup.py list
    python scripts/db_backup.py verify poe-20261009T120000.000000Z-deadbeef.db
    python scripts/db_backup.py restore poe-20261009T120000.000000Z-deadbeef.db
    python scripts/db_backup.py restore poe-20261009T120000.000000Z-deadbeef.db --apply

``restore`` 默认只输出预检计划；只有同时给出 ``--apply`` 才会替换配置中的数据库。
恢复前必须停止服务并确认 WAL/SHM 已消失。若目标库存在，会先自动创建安全快照。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backup import (  # noqa: E402
    BackupError,
    create_backup,
    list_backups,
    plan_restore,
    restore_backup,
    verify_backup,
)
from core.config import get_config  # noqa: E402


def _emit(value: dict[str, Any] | list[dict[str, Any]], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if isinstance(value, list):
        if not value:
            print("没有备份")
            return
        for item in value:
            status = "完整" if item.get("complete") else "不完整"
            print(
                f"{item.get('backup_name', '?')}  {status}  "
                f"{item.get('created_at', '-')}  {item.get('database_size', 0)} bytes"
            )
            if item.get("error"):
                print(f"  !! {item['error']}")
        return
    for key, item in value.items():
        if isinstance(item, (dict, list, tuple)):
            rendered = json.dumps(item, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(item)
        print(f"{key}: {rendered}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="POE1-Agent SQLite 一致性备份与离线恢复")
    parser.add_argument("--json", action="store_true", help="输出稳定 JSON，便于自动化消费")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="使用 SQLite Backup API 创建一致性快照")
    sub.add_parser("list", help="列出备份清单")
    verify = sub.add_parser("verify", help="校验清单、SHA-256、schema、行数和 quick_check")
    verify.add_argument("backup_name")
    restore = sub.add_parser("restore", help="恢复预检；显式 --apply 才执行")
    restore.add_argument("backup_name")
    restore.add_argument("--apply", action="store_true", help="通过预检后执行原子替换")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    cfg = get_config()
    try:
        if args.command == "create":
            result = asdict(create_backup(cfg.db_path, cfg.backup_dir))
            result["created"] = True
        elif args.command == "list":
            _emit(list_backups(cfg.backup_dir), json_output=args.json)
            return 0
        elif args.command == "verify":
            report = verify_backup(cfg.backup_dir, args.backup_name)
            _emit(report.as_dict(), json_output=args.json)
            return 0 if report.valid else 2
        else:
            plan = plan_restore(cfg.db_path, cfg.backup_dir, args.backup_name)
            if not args.apply:
                result = plan.as_dict()
                result["applied"] = False
                result["next_step"] = (
                    "停止服务并清除 sidecar 后加 --apply"
                    if not plan.valid
                    else "确认计划无误后重复命令并加 --apply"
                )
                _emit(result, json_output=args.json)
                return 0 if plan.valid else 2
            if not plan.valid:
                raise BackupError("恢复预检失败：" + "；".join(plan.issues))
            result = restore_backup(cfg.db_path, cfg.backup_dir, args.backup_name)
        _emit(result, json_output=args.json)
        return 0
    except BackupError as exc:
        _emit({"ok": False, "error": str(exc)}, json_output=args.json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
