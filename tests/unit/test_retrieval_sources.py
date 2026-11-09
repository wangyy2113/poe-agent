"""Lexical evidence keeps factual source boundaries intact."""
from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from retrieval.fts import Hit
from retrieval.hybrid import retrieve


def _hit(chunk_id: str, channel: str) -> Hit:
    return Hit(chunk_id=chunk_id, entity_id=chunk_id, text="猎首 装备",
               metadata={}, score=1.0, raw_score=0.0, channel=channel,
               source="POEDB", source_priority=1,
               coverage=1.0, matched_terms=["猎首", "装备"])


class FactualSourceTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_community_exact_match_never_opens_factual_gate(self) -> None:
        community = _hit("community", "alias")
        community.source_priority = 7
        with (
            patch("retrieval.hybrid.alias_mod.lookup", return_value=["community"]),
            patch("retrieval.hybrid.alias_mod.hits_for_entities", return_value=[community]),
            patch("retrieval.hybrid.search_fts", return_value=[]),
        ):
            result = retrieve(self.conn, "猎首 装备")
        self.assertEqual("community", result.hits[0].chunk_id)
        self.assertFalse(result.sufficient)
        self.assertEqual(0.0, result.top_coverage)
        self.assertIn("社区资料", result.reason)

    def test_last_factual_priority_still_opens_exact_match_gate(self) -> None:
        factual = _hit("priority-six", "alias")
        factual.source_priority = 6
        with (
            patch("retrieval.hybrid.alias_mod.lookup", return_value=["priority-six"]),
            patch("retrieval.hybrid.alias_mod.hits_for_entities", return_value=[factual]),
            patch("retrieval.hybrid.search_fts", return_value=[]),
        ):
            result = retrieve(self.conn, "猎首 装备")
        self.assertTrue(result.sufficient)


if __name__ == "__main__":
    unittest.main()
