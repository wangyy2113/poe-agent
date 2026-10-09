"""SQLite 一致性备份、清单校验、路径边界与离线恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core.backup import (
    BACKUP_FORMAT_VERSION,
    BackupError,
    BackupManifest,
    create_backup,
    list_backups,
    plan_restore,
    read_manifest,
    resolve_backup,
    restore_backup,
    verify_backup,
)


TABLES = ("entity", "chunk", "alias", "conversation", "message", "trace")


def _database(path: Path, *, marker: str = "old", wal: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
    for table in TABLES:
        conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, value TEXT)')
    conn.execute("CREATE TABLE mod_affix (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO entity(value) VALUES (?)", (marker,))
    conn.execute("INSERT INTO chunk(value) VALUES ('chunk')")
    conn.execute("PRAGMA user_version=7")
    conn.commit()
    return conn


def _manifest_file(database: Path) -> Path:
    return database.with_name(database.name + ".manifest.json")


class BackupCreateAndVerifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "poe.db"
        self.backups = self.root / "backups"
        self.conn = _database(self.db, wal=True)
        self.now = datetime(2026, 10, 9, 12, 34, 56, 123456, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def create(self, token: str = "abcdef0123456789") -> BackupManifest:
        return create_backup(
            self.db,
            self.backups,
            connection=self.conn,
            now=self.now,
            token=token,
        )

    def test_live_wal_snapshot_contains_committed_rows_without_checkpoint(self) -> None:
        self.conn.execute("INSERT INTO entity(value) VALUES ('from-wal')")
        self.conn.commit()
        wal = self.db.with_name(self.db.name + "-wal")
        self.assertTrue(wal.exists())

        manifest = self.create()

        snapshot = sqlite3.connect(self.backups / manifest.backup_name)
        try:
            values = [row[0] for row in snapshot.execute("SELECT value FROM entity ORDER BY id")]
        finally:
            snapshot.close()
        self.assertEqual(["old", "from-wal"], values)
        self.assertEqual(2, manifest.table_counts["entity"])

    def test_manifest_records_hash_size_schema_version_and_no_absolute_source_path(self) -> None:
        manifest = self.create()
        database = self.backups / manifest.backup_name

        self.assertEqual(BACKUP_FORMAT_VERSION, manifest.format_version)
        self.assertEqual("poe.db", manifest.source_name)
        self.assertNotIn(str(self.root), json.dumps(asdict(manifest)))
        self.assertEqual(database.stat().st_size, manifest.database_size)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), manifest.database_sha256)
        self.assertEqual(7, manifest.user_version)
        self.assertGreater(manifest.page_size, 0)
        self.assertGreater(manifest.page_count, 0)
        self.assertRegex(manifest.schema_sha256, r"^[0-9a-f]{64}$")

    def test_manifest_is_utf8_json_and_round_trips_strictly(self) -> None:
        manifest = self.create()
        database = self.backups / manifest.backup_name
        loaded = read_manifest(database)
        self.assertEqual(manifest, loaded)
        raw = json.loads(_manifest_file(database).read_text(encoding="utf-8"))
        self.assertEqual(sorted(asdict(manifest)), sorted(raw))

    def test_verify_checks_hash_schema_counts_and_quick_check(self) -> None:
        manifest = self.create()
        report = verify_backup(self.backups, manifest.backup_name)
        self.assertTrue(report.valid, report.issues)
        self.assertEqual("ok", report.quick_check)
        self.assertEqual(manifest.database_sha256, report.database_sha256)
        self.assertEqual(manifest.schema_sha256, report.schema_sha256)
        self.assertEqual(manifest.table_counts, report.table_counts)

    def test_verify_detects_valid_sqlite_content_tampering(self) -> None:
        manifest = self.create()
        database = self.backups / manifest.backup_name
        changed = sqlite3.connect(database)
        changed.execute("INSERT INTO entity(value) VALUES ('tampered')")
        changed.commit()
        changed.close()

        report = verify_backup(self.backups, manifest.backup_name)

        self.assertFalse(report.valid)
        self.assertTrue(any("SHA-256" in issue for issue in report.issues))
        self.assertTrue(any("行数" in issue for issue in report.issues))

    def test_verify_detects_corrupt_database_without_leaking_exception(self) -> None:
        manifest = self.create()
        database = self.backups / manifest.backup_name
        database.write_bytes(b"not sqlite")

        report = verify_backup(self.backups, manifest.backup_name)

        self.assertFalse(report.valid)
        self.assertTrue(any("无法读取" in issue for issue in report.issues))

    def test_verify_reports_missing_database(self) -> None:
        manifest = self.create()
        (self.backups / manifest.backup_name).unlink()
        report = verify_backup(self.backups, manifest.backup_name)
        self.assertFalse(report.valid)
        self.assertIn("备份数据库文件不存在", report.issues)

    def test_create_rejects_database_missing_required_tables(self) -> None:
        incomplete = self.root / "incomplete.db"
        sqlite3.connect(incomplete).close()
        with self.assertRaisesRegex(BackupError, "缺少必要表"):
            create_backup(incomplete, self.backups)
        self.assertEqual([], list(self.backups.glob("*.db")))
        self.assertEqual([], list(self.backups.glob("*.tmp")))

    def test_create_rejects_missing_source(self) -> None:
        with self.assertRaisesRegex(BackupError, "数据库不存在"):
            create_backup(self.root / "missing.db", self.backups)

    def test_each_create_gets_unique_name_and_list_is_newest_first(self) -> None:
        first = self.create("11111111aaaaaaaa")
        second = create_backup(
            self.db,
            self.backups,
            connection=self.conn,
            now=datetime(2026, 10, 9, 12, 35, tzinfo=timezone.utc),
            token="22222222bbbbbbbb",
        )
        items = list_backups(self.backups)
        self.assertEqual([second.backup_name, first.backup_name], [item["backup_name"] for item in items])
        self.assertTrue(all(item["complete"] for item in items))

    def test_list_nonexistent_directory_is_empty_and_does_not_create_it(self) -> None:
        missing = self.root / "never-created"
        self.assertEqual([], list_backups(missing))
        self.assertFalse(missing.exists())

    def test_list_keeps_malformed_manifest_visible_as_incomplete(self) -> None:
        self.backups.mkdir()
        name = "poe-20261009T123456.123456Z-abcdef01.db"
        _manifest_file(self.backups / name).write_text("{bad", encoding="utf-8")
        items = list_backups(self.backups)
        self.assertEqual(1, len(items))
        self.assertFalse(items[0]["complete"])
        self.assertIn("无法读取", items[0]["error"])

    def test_no_temporary_files_remain_after_success(self) -> None:
        self.create()
        self.assertEqual([], list(self.backups.glob(".*.tmp")))

    def test_duplicate_generated_name_is_rejected_without_overwriting_first(self) -> None:
        first = self.create()
        original = (self.backups / first.backup_name).read_bytes()
        with self.assertRaisesRegex(BackupError, "碰撞"):
            self.create()
        self.assertEqual(original, (self.backups / first.backup_name).read_bytes())


class BackupManifestValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "poe.db"
        conn = _database(self.db)
        conn.close()
        self.manifest = create_backup(
            self.db,
            self.root / "backups",
            now=datetime(2026, 10, 9, tzinfo=timezone.utc),
            token="abcdef01",
        )
        self.database = self.root / "backups" / self.manifest.backup_name

    def tearDown(self) -> None:
        self.temp.cleanup()

    def mutate(self, change) -> None:
        path = _manifest_file(self.database)
        raw = json.loads(path.read_text(encoding="utf-8"))
        change(raw)
        path.write_text(json.dumps(raw), encoding="utf-8")

    def test_unknown_manifest_field_is_rejected(self) -> None:
        self.mutate(lambda raw: raw.update(extra=True))
        with self.assertRaisesRegex(BackupError, "未知字段"):
            read_manifest(self.database)

    def test_missing_manifest_field_is_rejected(self) -> None:
        self.mutate(lambda raw: raw.pop("database_sha256"))
        with self.assertRaisesRegex(BackupError, "缺字段"):
            read_manifest(self.database)

    def test_future_manifest_version_is_rejected(self) -> None:
        self.mutate(lambda raw: raw.update(format_version=99))
        with self.assertRaisesRegex(BackupError, "不支持"):
            read_manifest(self.database)

    def test_invalid_hash_is_rejected(self) -> None:
        self.mutate(lambda raw: raw.update(database_sha256="not-a-hash"))
        with self.assertRaisesRegex(BackupError, "SHA-256"):
            read_manifest(self.database)

    def test_manifest_filename_mismatch_is_rejected(self) -> None:
        self.mutate(
            lambda raw: raw.update(backup_name="poe-20261009T000000.000000Z-ffffffff.db")
        )
        with self.assertRaisesRegex(BackupError, "文件名不匹配"):
            read_manifest(self.database)

    def test_non_object_manifest_is_rejected(self) -> None:
        _manifest_file(self.database).write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(BackupError, "顶层必须是对象"):
            read_manifest(self.database)


class BackupPathBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backups = self.root / "backups"
        self.backups.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rejects_traversal_absolute_and_unrecognized_names(self) -> None:
        for value in ("../poe-20261009T000000.000000Z-abcdef01.db", "/tmp/x.db", "latest.db", ""):
            with self.subTest(value=value), self.assertRaises(BackupError):
                resolve_backup(self.backups, value)

    def test_rejects_symlink_even_when_target_stays_inside_directory(self) -> None:
        target = self.backups / "real.db"
        target.write_bytes(b"x")
        link = self.backups / "poe-20261009T000000.000000Z-abcdef01.db"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("当前平台或权限不支持符号链接")
        with self.assertRaisesRegex(BackupError, "符号链接|越出"):
            resolve_backup(self.backups, link.name)

    def test_rejects_symlink_escape(self) -> None:
        outside = self.root / "outside.db"
        outside.write_bytes(b"x")
        link = self.backups / "poe-20261009T000000.000000Z-abcdef01.db"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("当前平台或权限不支持符号链接")
        with self.assertRaisesRegex(BackupError, "越出"):
            resolve_backup(self.backups, link.name)


class BackupRestoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "poe.db"
        self.backups = self.root / "backups"
        conn = _database(self.db, marker="restore-me")
        conn.close()
        self.manifest = create_backup(
            self.db,
            self.backups,
            now=datetime(2026, 10, 9, 1, tzinfo=timezone.utc),
            token="11111111",
        )
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE entity SET value='current'")
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def value(self) -> str:
        conn = sqlite3.connect(self.db)
        try:
            return str(conn.execute("SELECT value FROM entity").fetchone()[0])
        finally:
            conn.close()

    def test_plan_is_valid_and_describes_safety_backup(self) -> None:
        plan = plan_restore(self.db, self.backups, self.manifest.backup_name)
        self.assertTrue(plan.valid, plan.issues)
        self.assertTrue(plan.target_exists)
        self.assertTrue(plan.safety_backup_required)
        self.assertGreater(plan.target_size, 0)
        self.assertEqual((), plan.sidecars)

    def test_plan_rejects_any_sqlite_sidecar(self) -> None:
        sidecar = self.db.with_name(self.db.name + "-wal")
        sidecar.write_bytes(b"")
        plan = plan_restore(self.db, self.backups, self.manifest.backup_name)
        self.assertFalse(plan.valid)
        self.assertEqual(("poe.db-wal",), plan.sidecars)
        self.assertTrue(any("停止应用" in issue for issue in plan.issues))

    def test_restore_replaces_database_and_keeps_pre_restore_snapshot(self) -> None:
        result = restore_backup(self.db, self.backups, self.manifest.backup_name)
        self.assertTrue(result["restored"])
        self.assertEqual("restore-me", self.value())
        self.assertEqual("ok", result["quick_check"])
        self.assertIsNotNone(result["safety_backup"])
        safety = verify_backup(self.backups, result["safety_backup"])
        self.assertTrue(safety.valid, safety.issues)
        conn = sqlite3.connect(self.backups / result["safety_backup"])
        try:
            self.assertEqual("current", conn.execute("SELECT value FROM entity").fetchone()[0])
        finally:
            conn.close()

    def test_restore_to_missing_target_does_not_claim_safety_backup(self) -> None:
        self.db.unlink()
        result = restore_backup(self.db, self.backups, self.manifest.backup_name)
        self.assertIsNone(result["safety_backup"])
        self.assertEqual("restore-me", self.value())

    def test_restore_refuses_tampered_backup_before_touching_target(self) -> None:
        database = self.backups / self.manifest.backup_name
        conn = sqlite3.connect(database)
        conn.execute("UPDATE entity SET value='evil'")
        conn.commit()
        conn.close()
        with self.assertRaisesRegex(BackupError, "预检失败"):
            restore_backup(self.db, self.backups, self.manifest.backup_name)
        self.assertEqual("current", self.value())

    def test_restore_refuses_symlink_target(self) -> None:
        actual = self.root / "actual.db"
        os.replace(self.db, actual)
        try:
            self.db.symlink_to(actual)
        except (OSError, NotImplementedError):
            os.replace(actual, self.db)
            self.skipTest("当前平台或权限不支持符号链接")
        plan = plan_restore(self.db, self.backups, self.manifest.backup_name)
        self.assertFalse(plan.valid)
        self.assertTrue(any("符号链接" in issue for issue in plan.issues))
        with self.assertRaisesRegex(BackupError, "预检失败"):
            restore_backup(self.db, self.backups, self.manifest.backup_name)

    def test_copy_hash_mismatch_does_not_replace_target(self) -> None:
        original_copy = __import__("shutil").copyfile

        def corrupt_copy(source: Path, target: Path):
            result = original_copy(source, target)
            Path(target).write_bytes(Path(target).read_bytes() + b"changed")
            return result

        with patch("core.backup.shutil.copyfile", side_effect=corrupt_copy):
            with self.assertRaisesRegex(BackupError, "预检哈希不一致"):
                restore_backup(self.db, self.backups, self.manifest.backup_name)
        self.assertEqual("current", self.value())
        self.assertEqual([], list(self.root.glob(".*.restore.tmp")))


if __name__ == "__main__":
    unittest.main()
