"""The single-model runtime is local, healthy and independent of credentials."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from core import db as dbmod
from core.config import Config
from retrieval.hybrid import _rrf_fuse
from retrieval.runtime import configured_retrieve
from tests.unit.test_retrieval_sources import _hit


class LocalRetrievalTest(unittest.TestCase):
    def test_扩展探测失败不阻断数据库启动并写入日志(self) -> None:
        unavailable = dbmod.VectorStatus(False, "测试环境禁止扩展加载")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(dbmod, "_conn", None),
            patch.object(dbmod, "_vector_status", None),
            patch.object(dbmod, "_probe_vector", return_value=unavailable),
            self.assertLogs("core.db", level="INFO") as logs,
        ):
            conn = dbmod.init_db(Path(directory) / "poe.db", vector_dim=384)
            try:
                self.assertEqual(1, conn.execute("SELECT 1").fetchone()[0])
                status = dbmod.vector_status()
                self.assertFalse(status.available)
                self.assertEqual("测试环境禁止扩展加载", status.reason)
            finally:
                dbmod.close_db()

        self.assertTrue(
            any("向量通道禁用：测试环境禁止扩展加载" in line for line in logs.output)
        )

    def test_old_remote_settings_cannot_make_network_calls_or_fake_degradation(self):
        from api.search import SearchRequest, search
        from tools.impl.kb_search import kb_search
        config = Config({"retrieval": {"top_k": 4, "min_coverage": 0.6,
                        "vector": {"enabled": True}, "rerank": {"enabled": True}}})
        with (
            closing(sqlite3.connect(":memory:")) as conn,
            patch("core.config.get_secrets", side_effect=AssertionError("no credentials")),
            patch("retrieval.runtime.get_config", return_value=config),
            patch("tools.impl.kb_search.get_config", return_value=config),
            patch("core.db.get_conn", return_value=conn),
            patch("retrieval.hybrid.alias_mod.lookup", return_value=["canonical"]),
            patch("retrieval.hybrid.alias_mod.hits_for_entities", return_value=[_hit("canonical", "alias")]),
            patch("retrieval.hybrid.search_fts", return_value=[]),
            patch("httpx.post") as post,
        ):
            result = configured_retrieve(conn, "猎首 装备")
            api = search(SearchRequest(query="猎首 装备"))
            tool = kb_search("猎首 装备")
        self.assertTrue(result.sufficient)
        self.assertFalse(result.degraded)
        self.assertEqual("", result.note)
        self.assertEqual(result.channels, api.channels)
        self.assertEqual(result.channels, tool.meta["channels"])
        self.assertFalse(tool.meta["degraded"])
        post.assert_not_called()

    def test_fusion_does_not_compound_penalty_when_candidates_are_reused(self):
        hit = _hit("secondary", "fts")
        hit.source_priority = 3
        first = _rrf_fuse({"fts": [hit]}, 60, 0.08)
        second = _rrf_fuse({"fts": [hit]}, 60, 0.08)
        self.assertEqual(first, second)
        self.assertEqual(1.0, hit.score)
        self.assertEqual(0.84, first[0].score)

    def test_shared_config_preserves_filters_and_timeout(self):
        config = Config({"retrieval": {"top_k": 7, "timeout_seconds": 0.5}})
        with (patch("retrieval.runtime.get_config", return_value=config),
              patch("retrieval.runtime.retrieve") as retrieve):
            configured_retrieve(None, "query", top_k=3, category="item", league="L", version="V")
        self.assertEqual(retrieve.call_args.kwargs, dict(
            top_k=3, min_coverage=0.5, rrf_k=60, priority_decay=0.08,
            timeout_seconds=0.5, category="item", league="L", version="V"))

    def test_health_distinguishes_disabled_capabilities_from_configured_requests(self):
        from types import SimpleNamespace
        import main
        from tests.unit.test_deepseek_policy import secrets
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript("CREATE TABLE entity(id); CREATE TABLE chunk(id); CREATE TABLE alias(id); "
                               "CREATE TABLE economy_snapshot(category,fetched_at);")
            for requested in (False, True):
                config = Config({"retrieval": {"vector": {"enabled": requested}, "rerank": {"enabled": requested}}})
                with (patch.object(main, "cfg", config),
                      patch.object(main.dbmod, "get_conn", return_value=conn),
                      patch.object(main.dbmod, "vector_status", return_value=dbmod.VectorStatus(True, "available")),
                      patch.object(main, "get_secrets", return_value=secrets()),
                      patch.object(main, "get_llm_client", return_value=SimpleNamespace(credential_status=lambda: [{"healthy": True}]))):
                    result = main.health()
                self.assertEqual("degraded" if requested else "ok", result["status"])
                self.assertFalse(result["vector"]["enabled"])
                self.assertFalse(result["rerank"]["configured"])
                self.assertIn("DeepSeek 单模型", result["vector"]["reason"])
