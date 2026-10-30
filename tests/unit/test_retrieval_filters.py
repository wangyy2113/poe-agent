"""All retrieval channels obey the same category, league and version filters."""

from __future__ import annotations

import json
import sqlite3
import unittest

from retrieval.hybrid import retrieve


class RetrievalFiltersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            "CREATE TABLE alias(alias TEXT, alias_norm TEXT, entity_id TEXT, kind TEXT);"
            "CREATE TABLE chunk(chunk_id TEXT PRIMARY KEY, entity_id TEXT, "
            "text TEXT, metadata TEXT);"
            "CREATE VIRTUAL TABLE chunk_fts USING fts5("
            "tokens, keywords, chunk_id UNINDEXED, entity_id UNINDEXED);"
        )
        for chunk_id, category, league, version in (
            ("old_item", "item", "Standard", "3.28"),
            ("new_item", "item", "Mercenaries", "3.29"),
            ("new_skill", "skill", "Mercenaries", "3.29"),
        ):
            self.conn.execute(
                "INSERT INTO chunk VALUES (?,?,?,?)",
                (chunk_id, chunk_id, f"Alpha 装备 {chunk_id}", json.dumps({
                    "title": chunk_id, "category": category, "league": league,
                    "version": version, "source": "POEDB", "source_priority": 1,
                })),
            )
            self.conn.execute(
                "INSERT INTO chunk_fts VALUES (?,?,?,?)",
                ("alpha", "", chunk_id, chunk_id),
            )
            self.conn.execute(
                "INSERT INTO alias VALUES (?,?,?,?)",
                ("Alpha", "alpha", chunk_id, "name_en"),
            )

    def tearDown(self) -> None:
        self.conn.close()

    def test_exact_alias_and_fts_are_both_filtered(self) -> None:
        result = retrieve(
            self.conn, "Alpha", category="item", league="Mercenaries",
            version="3.29",
        )
        self.assertEqual(["new_item"], [hit.chunk_id for hit in result.hits])
        self.assertTrue(result.sufficient)
        self.assertEqual("实体精确命中", result.reason)

    def test_missing_version_never_returns_other_version_as_evidence(self) -> None:
        result = retrieve(
            self.conn, "Alpha", category="item", version="3.30",
        )
        self.assertEqual([], result.hits)
        self.assertFalse(result.sufficient)


if __name__ == "__main__":
    unittest.main()
