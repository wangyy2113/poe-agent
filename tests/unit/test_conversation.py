"""会话历史与重命名接口的 SQLite 契约。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from api.chat import ConversationUpdate, conversation, rename_conversation
from core.db import SCHEMA


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO conversation VALUES (?,?,datetime('now'),datetime('now'))",
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
        result = conversation("conv-1", caller="test")
        self.assertEqual(result["title"], "首条问题")
        self.assertEqual([m["role"] for m in result["messages"]], ["user", "assistant"])

    def test_rename_trims_and_persists_title(self) -> None:
        req = ConversationUpdate(title="  火花配装复盘  ")
        result = rename_conversation("conv-1", req, caller="test")
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


class ConversationFrontendContractTest(unittest.TestCase):
    def test_rename_button_calls_patch_and_marks_custom_title(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        page = (root / "static/index.html").read_text(encoding="utf-8")
        app = (root / "static/app.js").read_text(encoding="utf-8")
        self.assertIn('id="rename-chat"', page)
        self.assertIn("method: 'PATCH'", app)
        self.assertIn("customTitle: true", app)
        self.assertIn("current && current.customTitle", app)


if __name__ == "__main__":
    unittest.main()
