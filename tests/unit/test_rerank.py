"""Optional ReRank: response contract, exact hits and failure fallback."""

from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from retrieval.fts import Hit
from retrieval.hybrid import retrieve
from retrieval.rerank import RerankClient, RerankError, configured_rerank_client


def _hit(chunk_id: str, channel: str, *, coverage: float = 1.0) -> Hit:
    return Hit(
        chunk_id=chunk_id, entity_id=chunk_id, text=f"猎首 装备 {chunk_id}",
        metadata={"title": chunk_id}, score=1.0, raw_score=0.0,
        channel=channel, source="POEDB", source_priority=1,
        coverage=coverage, matched_terms=["猎首", "装备"],
    )


class RerankClientTest(unittest.TestCase):
    def test_remote_rerank_cannot_bypass_single_model_policy(self) -> None:
        for model in ("deepseek-v4-1-flash-1017", "bge-reranker"):
            with self.subTest(model=model), patch("httpx.post") as post:
                with self.assertRaisesRegex(RerankError, "DeepSeek 单模型") as caught:
                    RerankClient("https://example.test", "private-token", model).rerank("猎首", ["装备"])
                self.assertNotIn("private-token", str(caught.exception))
                post.assert_not_called()

    def test_legacy_config_never_loads_another_models_credentials(self) -> None:
        with patch("core.config.get_secrets", side_effect=AssertionError("unexpected")):
            self.assertEqual((None, "配置关闭"), configured_rerank_client(False))
            client, reason = configured_rerank_client(True)
        self.assertIsNone(client)
        self.assertIn("DeepSeek 单模型", reason)


class HybridRerankTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_exact_entity_cannot_be_pushed_out_of_top_k(self) -> None:
        alias = _hit("canonical", "alias")
        lexical = _hit("unrelated", "fts")
        ranker = SimpleNamespace(rerank=lambda _query, _docs: [0.0, 1.0])
        with (
            patch("retrieval.hybrid.alias_mod.lookup", return_value=["canonical"]),
            patch("retrieval.hybrid.alias_mod.hits_for_entities", return_value=[alias]),
            patch("retrieval.hybrid.search_fts", return_value=[lexical]),
        ):
            result = retrieve(
                self.conn, "猎首 装备", top_k=1, rerank_enabled=True,
                rerank_client=ranker,
            )
        self.assertEqual("canonical", result.hits[0].chunk_id)
        self.assertTrue(result.sufficient)
        self.assertIn("rerank", result.channels)

    def test_failed_ranker_keeps_rrf_order_and_reports_degradation(self) -> None:
        lexical = _hit("canonical", "fts")

        def fail(_query, _docs):
            raise RerankError("HTTP 503")

        with (
            patch("retrieval.hybrid.alias_mod.lookup", return_value=[]),
            patch("retrieval.hybrid.search_fts", return_value=[lexical]),
        ):
            result = retrieve(
                self.conn, "猎首 装备", rerank_enabled=True,
                rerank_client=SimpleNamespace(rerank=fail),
            )
        self.assertEqual("canonical", result.hits[0].chunk_id)
        self.assertTrue(result.degraded)
        self.assertNotIn("rerank", result.channels)
        self.assertIn("ReRank 已自动降级（HTTP 503）", result.note)

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
