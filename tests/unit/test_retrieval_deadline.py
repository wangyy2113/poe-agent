"""A retrieval deadline keeps completed lexical evidence but closes the gate."""

from __future__ import annotations

import itertools
import sqlite3
import unittest
from unittest.mock import patch

from retrieval.fts import Hit
from retrieval.hybrid import retrieve


def _hit(channel: str) -> Hit:
    return Hit(
        chunk_id="item", entity_id="item", text="猎首 装备",
        metadata={"title": "猎首"}, score=1.0, raw_score=0.0,
        channel=channel, source="POEDB", source_priority=1,
        coverage=1.0, matched_terms=["猎首", "装备"],
    )


class RetrievalBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_expired_budget_retains_fts_hit_but_rejects_nonexact_evidence(self) -> None:
        clock = itertools.chain([0.0], itertools.repeat(2.0))
        with (
            patch("retrieval.hybrid.time.monotonic", side_effect=clock),
            patch("retrieval.hybrid.alias_mod.lookup", return_value=[]),
            patch("retrieval.hybrid.search_fts", return_value=[_hit("fts")]),
        ):
            result = retrieve(
                self.conn, "猎首 装备", timeout_seconds=1.0,
            )
        self.assertEqual(["item"], [hit.chunk_id for hit in result.hits])
        self.assertEqual(["fts"], result.channels)
        self.assertFalse(result.sufficient)
        self.assertTrue(result.degraded)
        self.assertIn("检索超时", result.reason)

    def test_exact_entity_remains_sufficient_after_optional_channel_timeout(self) -> None:
        clock = itertools.chain([0.0], itertools.repeat(2.0))
        with (
            patch("retrieval.hybrid.time.monotonic", side_effect=clock),
            patch("retrieval.hybrid.alias_mod.lookup", return_value=["item"]),
            patch("retrieval.hybrid.alias_mod.hits_for_entities", return_value=[_hit("alias")]),
            patch("retrieval.hybrid.search_fts", return_value=[]),
        ):
            result = retrieve(self.conn, "猎首 装备", timeout_seconds=1.0)
        self.assertTrue(result.sufficient)
        self.assertEqual("实体精确命中", result.reason)
        self.assertTrue(result.degraded)
        self.assertIn("检索超时", result.note)


if __name__ == "__main__":
    unittest.main()
