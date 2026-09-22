"""自动同步计划、事实快照、版本 diff 与失败边界。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from api import admin as admin_api
from core import db as dbmod
from fastapi import HTTPException
from kb.sync import (
    SyncAlreadyRunning,
    build_plan,
    diff_snapshots,
    load_sync_run,
    run_sync,
    snapshot_category,
    validate_categories,
)


SCHEMA = """
CREATE TABLE ingest_run (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT,
    finished_at TEXT,
    succeeded INTEGER DEFAULT 0
);
CREATE TABLE mod_affix (
    uid TEXT PRIMARY KEY,
    text_zh TEXT,
    game_version TEXT,
    updated_at TEXT
);
CREATE TABLE poedb_currency (
    metadata_id TEXT PRIMARY KEY,
    effect_zh TEXT,
    version TEXT,
    fetched_at TEXT,
    updated_at TEXT
);
CREATE TABLE knowledge_sync_run (
    sync_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    requested TEXT NOT NULL,
    planned TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT,
    error TEXT
);
CREATE TABLE knowledge_sync_diff (
    sync_id TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    before_fingerprint TEXT NOT NULL,
    after_fingerprint TEXT NOT NULL,
    before_total INTEGER NOT NULL,
    after_total INTEGER NOT NULL,
    added INTEGER NOT NULL,
    removed INTEGER NOT NULL,
    changed INTEGER NOT NULL,
    unchanged INTEGER NOT NULL,
    table_counts_before TEXT NOT NULL,
    table_counts_after TEXT NOT NULL,
    versions_before TEXT NOT NULL,
    versions_after TEXT NOT NULL,
    examples TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    error TEXT,
    PRIMARY KEY(sync_id, category)
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


class SyncPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_plan_distinguishes_never_stale_and_fresh(self) -> None:
        self.conn.executemany(
            "INSERT INTO ingest_run(category,finished_at,succeeded) VALUES (?,?,?)",
            (
                ("mod_affix", "2026-09-01 00:00:00", 10),
                ("currency", "2026-09-21 13:00:00", 10),
            ),
        )
        plan = build_plan(
            self.conn,
            categories=("mods", "currency", "maps"),
            stale_after_hours=24,
            now=datetime(2026, 9, 22, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(
            [("mods", True, "stale"), ("currency", False, "fresh"),
             ("maps", True, "never_succeeded")],
            [(item.category, item.due, item.reason) for item in plan],
        )

    def test_failed_or_unfinished_ingest_is_not_a_success(self) -> None:
        self.conn.executemany(
            "INSERT INTO ingest_run(category,finished_at,succeeded) VALUES (?,?,?)",
            (("mod_affix", None, 100), ("mod_affix", "2026-09-22 11:00:00", 0)),
        )
        item = build_plan(
            self.conn,
            categories=("mods",),
            now=datetime(2026, 9, 22, 12, tzinfo=timezone.utc),
        )[0]
        self.assertTrue(item.due)
        self.assertEqual("never_succeeded", item.reason)

    def test_category_allowlist_rejects_unsafe_unique_chain(self) -> None:
        with self.assertRaisesRegex(ValueError, "不支持自动同步"):
            validate_categories(("unique_item",))


class SnapshotDiffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_diff_tracks_added_removed_changed_and_version_transition(self) -> None:
        self.conn.executemany(
            "INSERT INTO mod_affix(uid,text_zh,game_version,updated_at) VALUES (?,?,?,?)",
            (("keep", "旧值", "3.29", "old"), ("gone", "删除", "3.29", "old")),
        )
        before = snapshot_category(self.conn, "mods")
        self.conn.execute("UPDATE mod_affix SET text_zh='新值',game_version='3.30',updated_at='new' WHERE uid='keep'")
        self.conn.execute("DELETE FROM mod_affix WHERE uid='gone'")
        self.conn.execute(
            "INSERT INTO mod_affix(uid,text_zh,game_version,updated_at) VALUES ('added','新增','3.30','new')"
        )
        after = snapshot_category(self.conn, "mods")
        diff = diff_snapshots("mods", before, after)

        self.assertEqual("attention", diff.status)
        self.assertEqual((1, 1, 1, 0), (diff.added, diff.removed, diff.changed, diff.unchanged))
        self.assertEqual(["3.29"], diff.versions_before["mod_affix.game_version"])
        self.assertEqual(["3.30"], diff.versions_after["mod_affix.game_version"])
        self.assertEqual({"mod_affix": 2}, diff.table_counts_before)
        self.assertEqual({"mod_affix": 2}, diff.table_counts_after)
        self.assertEqual(["mod_affix:[\"gone\"]"], diff.examples["removed"])

    def test_timestamp_only_change_is_ignored(self) -> None:
        self.conn.execute(
            "INSERT INTO mod_affix(uid,text_zh,game_version,updated_at) VALUES ('same','值','3.29','old')"
        )
        before = snapshot_category(self.conn, "mods")
        self.conn.execute("UPDATE mod_affix SET updated_at='new' WHERE uid='same'")
        after = snapshot_category(self.conn, "mods")
        diff = diff_snapshots("mods", before, after)
        self.assertEqual("unchanged", diff.status)
        self.assertEqual(before.fingerprint, after.fingerprint)


class RunSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_success_persists_diff_summary_and_refreshes_coverage(self) -> None:
        calls: list[object] = []

        def runner(force: bool) -> int:
            calls.append(force)
            self.conn.execute(
                "INSERT INTO mod_affix(uid,text_zh,game_version,updated_at) "
                "VALUES ('new','新增','3.30','now')"
            )
            self.conn.commit()
            return 0

        result = run_sync(
            self.conn,
            ("mods",),
            runners={"mods": runner},
            force=True,
            mode="auto",
            refresh_coverage=lambda: calls.append("coverage") or 0,
        )

        self.assertEqual("succeeded", result["status"])
        self.assertEqual([True, "coverage"], calls)
        loaded = load_sync_run(self.conn, str(result["sync_id"]))
        self.assertIsNotNone(loaded)
        self.assertEqual({"planned": 1, "completed": 1, "unchanged": 0,
                          "changed": 1, "attention": 0, "failed": 0,
                          "added": 1, "removed": 0, "modified": 0},
                         loaded["summary"])
        self.assertEqual("changed", loaded["diffs"][0]["status"])

    def test_failure_rolls_back_uncommitted_rows_and_stops_following_categories(self) -> None:
        called: list[str] = []

        def fail(_force: bool) -> int:
            self.conn.execute(
                "INSERT INTO mod_affix(uid,text_zh,game_version,updated_at) "
                "VALUES ('partial','不应提交','3.30','now')"
            )
            raise RuntimeError("boom")

        def should_not_run(_force: bool) -> int:
            called.append("currency")
            return 0

        result = run_sync(
            self.conn,
            ("mods", "currency"),
            runners={"mods": fail, "currency": should_not_run},
        )

        self.assertEqual("failed", result["status"])
        self.assertEqual([], called)
        self.assertEqual(0, self.conn.execute("SELECT count(*) FROM mod_affix").fetchone()[0])
        self.assertEqual(1, len(result["diffs"]))
        self.assertEqual("failed", result["diffs"][0]["status"])

    def test_recent_running_sync_blocks_overlap(self) -> None:
        self.conn.execute(
            "INSERT INTO knowledge_sync_run(sync_id,mode,requested,planned,status,started_at) "
            "VALUES ('active','auto','[]','[]','running',datetime('now'))"
        )
        with self.assertRaisesRegex(SyncAlreadyRunning, "active"):
            run_sync(self.conn, ("mods",), runners={"mods": lambda _force: 0})

    def test_removal_promotes_whole_run_to_attention(self) -> None:
        self.conn.execute(
            "INSERT INTO mod_affix(uid,text_zh,game_version,updated_at) "
            "VALUES ('gone','将被删除','3.29','old')"
        )
        self.conn.commit()

        def remove(_force: bool) -> int:
            self.conn.execute("DELETE FROM mod_affix WHERE uid='gone'")
            self.conn.commit()
            return 0

        result = run_sync(self.conn, ("mods",), runners={"mods": remove})
        self.assertEqual("attention", result["status"])
        self.assertEqual(1, result["summary"]["attention"])

    def test_stale_running_sync_is_marked_failed_and_recovered(self) -> None:
        self.conn.execute(
            "INSERT INTO knowledge_sync_run(sync_id,mode,requested,planned,status,started_at) "
            "VALUES ('stale','auto','[]','[]','running','2020-01-01 00:00:00')"
        )
        result = run_sync(self.conn, ("mods",), runners={"mods": lambda _force: 0})
        stale = self.conn.execute(
            "SELECT status,error FROM knowledge_sync_run WHERE sync_id='stale'"
        ).fetchone()
        self.assertEqual("succeeded", result["status"])
        self.assertEqual("failed", stale["status"])
        self.assertIn("超过 12 小时", stale["error"])

    def test_production_schema_accepts_attention_and_table_count_json(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(dbmod.SCHEMA)
        conn.execute(
            "INSERT INTO mod_affix(uid,mod_code,item_class_page,category,family_list,"
            "source,source_priority,url,updated_at) "
            "VALUES ('gone','x','Bows','normal','[]','POEDB',1,'https://example.test','old')"
        )
        conn.commit()

        def remove(_force: bool) -> int:
            conn.execute("DELETE FROM mod_affix WHERE uid='gone'")
            conn.commit()
            return 0

        result = run_sync(conn, ("mods",), runners={"mods": remove})
        loaded = load_sync_run(conn, str(result["sync_id"]))
        self.assertEqual("attention", result["status"])
        self.assertEqual("attention", loaded["status"])
        self.assertEqual({"mod_affix": 1}, loaded["diffs"][0]["table_counts_before"])


class AdminSyncViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.addCleanup(self.conn.close)

    def test_plan_endpoint_is_read_only_and_lists_due_categories(self) -> None:
        cfg = SimpleNamespace(get=lambda key, default=None: {
            "knowledge_sync.categories": ["mods", "currency"],
            "knowledge_sync.stale_after_hours": 168,
        }.get(key, default))
        with (
            patch.object(admin_api.dbmod, "get_conn", return_value=self.conn),
            patch.object(admin_api, "get_config", return_value=cfg),
        ):
            result = admin_api.sync_plan(categories="", stale_after_hours=24)
        self.assertEqual(["mods", "currency"], result["due"])
        self.assertEqual(
            "read_only_plan_use_scripts_sync_knowledge_py_apply_to_execute",
            result["boundary"],
        )
        self.assertEqual(0, self.conn.execute("SELECT count(*) FROM knowledge_sync_run").fetchone()[0])

    def test_run_views_decode_json_and_404_unknown_id(self) -> None:
        result = run_sync(self.conn, ("mods",), runners={"mods": lambda _force: 0})
        with patch.object(admin_api.dbmod, "get_conn", return_value=self.conn):
            listing = admin_api.sync_runs(limit=20)
            detail = admin_api.sync_run_detail(str(result["sync_id"]))
            with self.assertRaises(HTTPException) as caught:
                admin_api.sync_run_detail("missing")
        self.assertEqual(["mods"], listing["items"][0]["planned"])
        self.assertEqual("unchanged", detail["diffs"][0]["status"])
        self.assertEqual(404, caught.exception.status_code)


if __name__ == "__main__":
    unittest.main()
