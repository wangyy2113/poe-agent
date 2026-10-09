#!/usr/bin/env python3
"""无真实数据、密钥或网络的干净安装发布冒烟。

CI 在 Linux / Windows 与 Python 3.10 / 3.12 上运行：初始化全新 SQLite、写入一行、从
活跃 WAL 连接创建一致性快照、深度校验、关闭后恢复到新目标，并确认 FastAPI schema 暴露
受控备份接口。它验证的是全新机器可启动的基础设施，不伪装真实知识库或在线问答验收。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.backup import create_backup, restore_backup, verify_backup  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        database = root / "poe.db"
        backup_dir = root / "backups"
        restored = root / "restored.db"
        conn = dbmod.init_db(database)
        conn.execute(
            "INSERT INTO entity(entity_id,entity_type,name_zh,name_en,attributes,source,"
            "source_priority,url,content_hash,fetched_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ci-smoke", "probe", "发布冒烟", "Release smoke", "{}", "ci", 99,
                "https://example.invalid/ci-smoke", "ci-smoke",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
        manifest = create_backup(database, backup_dir, connection=conn, source_name="ci-smoke")
        report = verify_backup(backup_dir, manifest.backup_name)
        if not report.valid:
            print(f"备份校验失败：{report.issues}")
            return 1
        dbmod.close_db()
        result = restore_backup(restored, backup_dir, manifest.backup_name)
        check = sqlite3.connect(restored)
        try:
            row = check.execute("SELECT name_zh FROM entity WHERE entity_id='ci-smoke'").fetchone()
        finally:
            check.close()
        if row is None or row[0] != "发布冒烟" or result["quick_check"] != "ok":
            print("恢复内容校验失败")
            return 1

        # 延迟导入，避免 create_app 的全局配置影响上面的临时数据库。
        from main import create_app  # noqa: PLC0415

        paths = create_app().openapi()["paths"]
        required = {
            "/poe_agent/admin/backups",
            "/poe_agent/admin/backups/{backup_name}/verify",
        }
        missing = required - paths.keys()
        if missing:
            print(f"OpenAPI 缺少备份端点：{sorted(missing)}")
            return 1
        print("发布冒烟通过：全新 SQLite → WAL 一致性快照 → 深度校验 → 离线恢复 → OpenAPI")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        dbmod.close_db()
