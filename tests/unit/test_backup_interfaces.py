"""备份 CLI 与管理 API 的受控接口。"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from api.admin import backups, create_database_backup, verify_database_backup
from core.config import Config
from scripts.db_backup import main
from tests.unit.test_backup import _database


class BackupInterfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "poe.db"
        self.backups_dir = self.root / "backups"
        self.conn = _database(self.db, marker="cli", wal=True)
        self.config = Config(raw={"paths": {"db": str(self.db), "backups": str(self.backups_dir)}})

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        stream = io.StringIO()
        with patch("scripts.db_backup.get_config", return_value=self.config), contextlib.redirect_stdout(stream):
            code = main(argv)
        return code, stream.getvalue()

    def test_cli_create_list_and_verify_json(self) -> None:
        create_code, create_text = self.run_cli(["--json", "create"])
        created = json.loads(create_text)
        self.assertEqual(0, create_code)
        self.assertTrue(created["created"])
        name = created["backup_name"]

        list_code, list_text = self.run_cli(["--json", "list"])
        items = json.loads(list_text)
        self.assertEqual(0, list_code)
        self.assertEqual(name, items[0]["backup_name"])

        verify_code, verify_text = self.run_cli(["--json", "verify", name])
        report = json.loads(verify_text)
        self.assertEqual(0, verify_code)
        self.assertTrue(report["valid"])

    def test_cli_restore_without_apply_only_prints_plan(self) -> None:
        _, create_text = self.run_cli(["--json", "create"])
        name = json.loads(create_text)["backup_name"]
        self.conn.close()
        changed = sqlite3.connect(self.db)
        changed.execute("UPDATE entity SET value='changed'")
        changed.commit()
        changed.close()

        code, text = self.run_cli(["--json", "restore", name])

        self.assertEqual(0, code)
        self.assertFalse(json.loads(text)["applied"])
        check = sqlite3.connect(self.db)
        try:
            self.assertEqual("changed", check.execute("SELECT value FROM entity").fetchone()[0])
        finally:
            check.close()
        self.conn = sqlite3.connect(self.db)

    def test_cli_restore_apply_rejects_live_wal_and_preserves_target(self) -> None:
        _, create_text = self.run_cli(["--json", "create"])
        name = json.loads(create_text)["backup_name"]
        self.conn.execute("UPDATE entity SET value='still-live'")
        self.conn.commit()
        code, text = self.run_cli(["--json", "restore", name, "--apply"])
        self.assertEqual(2, code)
        self.assertIn("预检失败", json.loads(text)["error"])
        self.assertEqual("still-live", self.conn.execute("SELECT value FROM entity").fetchone()[0])

    def test_cli_verify_bad_name_returns_stable_error_exit(self) -> None:
        code, text = self.run_cli(["--json", "verify", "../poe.db"])
        self.assertEqual(2, code)
        self.assertFalse(json.loads(text)["ok"])

    def test_admin_create_uses_live_connection_and_lists_manifest(self) -> None:
        self.conn.execute("INSERT INTO entity(value) VALUES ('api-live')")
        self.conn.commit()
        with (
            patch("api.admin.get_config", return_value=self.config),
            patch("api.admin.dbmod.get_conn", return_value=self.conn),
        ):
            result = create_database_backup()
            listing = backups()
        self.assertTrue(result["created"])
        self.assertEqual(2, result["manifest"]["table_counts"]["entity"])
        self.assertEqual(result["manifest"]["backup_name"], listing["items"][0]["backup_name"])
        self.assertIn("manifests_only", listing["boundary"])

    def test_admin_verify_discloses_offline_restore_boundary(self) -> None:
        with (
            patch("api.admin.get_config", return_value=self.config),
            patch("api.admin.dbmod.get_conn", return_value=self.conn),
        ):
            name = create_database_backup()["manifest"]["backup_name"]
            result = verify_database_backup(name)
        self.assertTrue(result["valid"])
        self.assertIn("offline_cli_only", result["restore_boundary"])

    def test_admin_verify_rejects_path_escape_as_400(self) -> None:
        with patch("api.admin.get_config", return_value=self.config):
            with self.assertRaises(HTTPException) as raised:
                verify_database_backup("../poe.db")
        self.assertEqual(400, raised.exception.status_code)

    def test_admin_create_failure_is_503(self) -> None:
        missing = Config(
            raw={
                "paths": {
                    "db": str(self.root / "missing.db"),
                    "backups": str(self.backups_dir),
                }
            }
        )
        with (
            patch("api.admin.get_config", return_value=missing),
            patch("api.admin.dbmod.get_conn", return_value=self.conn),
            self.assertRaises(HTTPException) as raised,
        ):
            create_database_backup()
        self.assertEqual(503, raised.exception.status_code)


if __name__ == "__main__":
    unittest.main()
