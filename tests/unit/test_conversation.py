"""会话历史与重命名接口的 SQLite 契约。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from api.chat import (
    ConversationUpdate,
    _ensure_conversation,
    _save_message,
    conversation,
    conversations,
    delete_conversation,
    rename_conversation,
)
from core import db as dbmod
from core.db import SCHEMA
from main import create_app


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO conversation("
        "conversation_id,title,created_at,updated_at,owner_id"
        ") VALUES (?,?,datetime('now'),datetime('now'),'owner-a')",
        ("conv-1", "首条问题"),
    )
    conn.executemany(
        "INSERT INTO message(message_id, conversation_id, role, content, created_at) "
        "VALUES (?,?,?,?,datetime('now'))",
        [
            ("msg-1", "conv-1", "user", "首条问题"),
            ("msg-2", "conv-1", "assistant", "回答"),
        ],
    )
    conn.commit()
    return conn


class ConversationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _database()
        self.db = patch("api.chat.dbmod.get_conn", return_value=self.conn)
        self.db.start()

    def tearDown(self) -> None:
        self.db.stop()
        self.conn.close()

    def test_history_returns_persisted_title_and_messages(self) -> None:
        result = conversation("conv-1", caller="owner-a")
        self.assertEqual(result["title"], "首条问题")
        self.assertEqual([m["role"] for m in result["messages"]], ["user", "assistant"])

    def test_rename_trims_and_persists_title(self) -> None:
        req = ConversationUpdate(title="  火花配装复盘  ")
        result = rename_conversation("conv-1", req, caller="owner-a")
        self.assertEqual(result["title"], "火花配装复盘")
        self.assertEqual(
            self.conn.execute(
                "SELECT title FROM conversation WHERE conversation_id = 'conv-1'"
            ).fetchone()["title"],
            "火花配装复盘",
        )

    def test_empty_and_oversized_titles_are_rejected(self) -> None:
        for title in ("   ", "x" * 61):
            with self.subTest(title=title), self.assertRaises(ValidationError):
                ConversationUpdate(title=title)

    def test_missing_conversation_is_404(self) -> None:
        with self.assertRaises(HTTPException) as history:
            conversation("missing", caller="test")
        self.assertEqual(history.exception.status_code, 404)

        with self.assertRaises(HTTPException) as rename:
            rename_conversation(
                "missing", ConversationUpdate(title="不存在"), caller="test"
            )
        self.assertEqual(rename.exception.status_code, 404)

    def test_owner_isolation_hides_history_rename_and_delete(self) -> None:
        operations = (
            lambda: conversation("conv-1", caller="owner-b"),
            lambda: rename_conversation(
                "conv-1", ConversationUpdate(title="越权"), caller="owner-b"
            ),
            lambda: delete_conversation("conv-1", caller="owner-b"),
        )
        for operation in operations:
            with (
                self.subTest(operation=operation),
                self.assertRaises(HTTPException) as caught,
            ):
                operation()
            self.assertEqual(caught.exception.status_code, 404)

    def test_list_is_owner_scoped_recent_first_and_has_summary(self) -> None:
        self.conn.execute(
            "INSERT INTO conversation("
            "conversation_id,title,created_at,updated_at,owner_id"
            ") VALUES ('conv-2','最近会话',datetime('now','+1 second'),"
            "datetime('now','+1 second'),'owner-a')"
        )
        self.conn.execute(
            "INSERT INTO conversation("
            "conversation_id,title,created_at,updated_at,owner_id"
            ") VALUES ('other','他人会话',datetime('now','+2 seconds'),"
            "datetime('now','+2 seconds'),'owner-b')"
        )
        self.conn.executemany(
            "INSERT INTO message(message_id,conversation_id,role,content,created_at) "
            "VALUES (?,?,?,?,?)",
            [
                ("msg-3", "conv-2", "user", "第一句", "2026-09-24 10:00:00"),
                ("msg-4", "conv-2", "assistant", "最后一行\n  摘要", "2026-09-24 11:00:00"),
            ],
        )
        self.conn.commit()

        result = conversations(limit=30, caller="owner-a")
        self.assertEqual(result.total, 2)
        self.assertEqual(result.items[0].conversation_id, "conv-2")
        self.assertEqual(result.items[0].message_count, 2)
        self.assertEqual(result.items[0].last_message_preview, "最后一行 摘要")
        self.assertNotIn("other", [item.conversation_id for item in result.items])

    def test_legacy_conversation_is_claimed_only_by_first_id_holder(self) -> None:
        self.conn.execute(
            "INSERT INTO conversation("
            "conversation_id,title,created_at,updated_at,owner_id"
            ") VALUES ('legacy','旧会话',datetime('now'),datetime('now'),'')"
        )
        self.conn.commit()

        self.assertEqual(conversation("legacy", caller="owner-a")["title"], "旧会话")
        owner = self.conn.execute(
            "SELECT owner_id FROM conversation WHERE conversation_id='legacy'"
        ).fetchone()["owner_id"]
        self.assertEqual(owner, "owner-a")
        with self.assertRaises(HTTPException):
            conversation("legacy", caller="owner-b")

    def test_ensure_creates_owned_conversation_and_rejects_cross_owner_reuse(self) -> None:
        _ensure_conversation("new-conv", "新的第一条问题", "owner-a")
        row = self.conn.execute(
            "SELECT title,owner_id FROM conversation WHERE conversation_id='new-conv'"
        ).fetchone()
        self.assertEqual((row["title"], row["owner_id"]), ("新的第一条问题", "owner-a"))
        with self.assertRaises(HTTPException) as caught:
            _ensure_conversation("new-conv", "越权续写", "owner-b")
        self.assertEqual(caught.exception.status_code, 404)

    def test_delete_removes_owned_conversation_and_messages(self) -> None:
        result = delete_conversation("conv-1", caller="owner-a")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["deleted_messages"], 2)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM conversation").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM message").fetchone()[0], 0
        )
        _save_message("conv-1", "assistant", "流式结束后的迟到写入")
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM message").fetchone()[0], 0
        )

    def test_openapi_exposes_list_read_rename_and_delete(self) -> None:
        paths = create_app().openapi()["paths"]
        self.assertIn("get", paths["/poe_agent/conversations"])
        self.assertEqual(
            {"get", "patch", "delete"},
            set(paths["/poe_agent/conversations/{conversation_id}"]),
        )


class ConversationMigrationTest(unittest.TestCase):
    def test_owner_migration_is_idempotent_and_keeps_legacy_rows(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE conversation (conversation_id TEXT PRIMARY KEY, title TEXT, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO conversation VALUES ('legacy','旧会话','created','updated')"
        )
        dbmod._migrate_conversation_owner(conn)
        dbmod._migrate_conversation_owner(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation)")}
        self.assertIn("owner_id", columns)
        self.assertEqual(
            conn.execute("SELECT owner_id FROM conversation").fetchone()["owner_id"], ""
        )
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(conversation)")}
        self.assertIn("idx_conversation_owner_updated", indexes)
        conn.close()


class ConversationFrontendContractTest(unittest.TestCase):
    def test_server_sync_rename_and_delete_controls_are_wired(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        page = (root / "static/index.html").read_text(encoding="utf-8")
        app = (root / "static/app.js").read_text(encoding="utf-8")
        self.assertIn('id="rename-chat"', page)
        self.assertIn('id="delete-chat"', page)
        self.assertIn("/conversations?limit=30", app)
        self.assertIn("method: 'PATCH'", app)
        self.assertIn("method: 'DELETE'", app)
        self.assertIn("customTitle: true", app)
        self.assertIn("current && current.customTitle", app)


if __name__ == "__main__":
    unittest.main()
