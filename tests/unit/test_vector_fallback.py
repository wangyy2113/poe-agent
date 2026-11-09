"""向量扩展不可用时的运行时降级契约。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import db as dbmod
from retrieval.hybrid import RetrievalResult, retrieve


class VectorChannelStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self) -> None:
        self.conn.close()

    def test_配置关闭时不把扩展可用冒充为通道启用(self) -> None:
        with (
            patch.object(dbmod, "_conn", self.conn),
            patch.object(
                dbmod,
                "_vector_status",
                dbmod.VectorStatus(True, "sqlite-vec test"),
            ),
        ):
            status = dbmod.vector_channel_status(False)

        self.assertFalse(status.available)
        self.assertEqual("配置关闭", status.reason)

    def test_配置请求启用但扩展不可用时自动禁用并保留原因(self) -> None:
        with (
            patch.object(dbmod, "_conn", self.conn),
            patch.object(
                dbmod,
                "_vector_status",
                dbmod.VectorStatus(False, "当前 Python 不支持扩展加载"),
            ),
        ):
            status = dbmod.vector_channel_status(True)

        self.assertFalse(status.available)
        self.assertEqual("当前 Python 不支持扩展加载", status.reason)

    def test_扩展可用但向量表未建立时仍保持禁用(self) -> None:
        with (
            patch.object(dbmod, "_conn", self.conn),
            patch.object(
                dbmod,
                "_vector_status",
                dbmod.VectorStatus(True, "sqlite-vec test"),
            ),
        ):
            status = dbmod.vector_channel_status(True)

        self.assertFalse(status.available)
        self.assertIn("向量表未建立", status.reason)

    def test_配置扩展向量表与查询适配器齐全时才启用(self) -> None:
        self.conn.execute("CREATE TABLE chunk_vec(chunk_id TEXT PRIMARY KEY)")
        with (
            patch.object(dbmod, "_conn", self.conn),
            patch.object(
                dbmod,
                "_vector_status",
                dbmod.VectorStatus(True, "sqlite-vec test"),
            ),
        ):
            status = dbmod.vector_channel_status(True, query_adapter_ready=True)

        self.assertTrue(status.available)
        self.assertEqual("sqlite-vec test", status.reason)

    def test_扩展与表齐全但查询适配器未接入时不冒充已启用(self) -> None:
        self.conn.execute("CREATE TABLE chunk_vec(chunk_id TEXT PRIMARY KEY)")
        with (
            patch.object(dbmod, "_conn", self.conn),
            patch.object(
                dbmod,
                "_vector_status",
                dbmod.VectorStatus(True, "sqlite-vec test"),
            ),
        ):
            status = dbmod.vector_channel_status(True)

        self.assertFalse(status.available)
        self.assertEqual("向量查询适配器尚未接入", status.reason)

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
                status = dbmod.vector_channel_status(True)
                self.assertFalse(status.available)
                self.assertEqual("测试环境禁止扩展加载", status.reason)
            finally:
                dbmod.close_db()

        self.assertTrue(
            any("向量通道禁用：测试环境禁止扩展加载" in line for line in logs.output)
        )


class RetrievalFallbackNoteTest(unittest.TestCase):
    def test_运行时禁用原因进入降级元数据且不宣称vector参与(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            with (
                patch("retrieval.hybrid.alias_mod.lookup", return_value=[]),
                patch("retrieval.hybrid.search_fts", return_value=[]),
            ):
                result = retrieve(
                    conn,
                    "猎首",
                    vector_enabled=False,
                    vector_disabled_reason="当前 Python 不支持扩展加载",
                )
        finally:
            conn.close()

        self.assertTrue(result.degraded)
        self.assertNotIn("vector", result.channels)
        self.assertIn("已自动降级", result.note)
        self.assertIn("当前 Python 不支持扩展加载", result.note)
        self.assertIn("BM25 + 实体精确匹配", result.note)


class SearchEntryPointFallbackTest(unittest.TestCase):
    class _Config:
        db_path = None

        @staticmethod
        def get(key: str, default=None):
            values = {
                "retrieval.vector.enabled": True,
                "retrieval.rerank.enabled": False,
            }
            return values.get(key, default)

    def test_kb_search使用运行时有效值而非原始配置(self) -> None:
        import tools.impl.kb_search as search_mod

        captured: dict[str, object] = {}

        def fake_retrieve(_conn, query, **kwargs):
            captured.update(kwargs)
            return RetrievalResult(query=query, degraded=True, note="fallback")

        runtime = dbmod.VectorStatus(False, "扩展加载被禁用")
        with (
            patch.object(search_mod, "get_config", return_value=self._Config()),
            patch.object(search_mod.dbmod, "get_conn", return_value=object()),
            patch.object(
                search_mod.dbmod,
                "vector_channel_status",
                return_value=runtime,
            ),
            patch.object(search_mod, "retrieve", side_effect=fake_retrieve),
        ):
            result = search_mod.kb_search("猎首")

        self.assertTrue(result.ok)
        self.assertIs(captured["vector_enabled"], False)
        self.assertIn("DeepSeek 单模型", captured["vector_disabled_reason"])

    def test_http_search使用同一运行时有效值(self) -> None:
        import api.search as search_api

        captured: dict[str, object] = {}

        def fake_retrieve(_conn, query, **kwargs):
            captured.update(kwargs)
            return RetrievalResult(query=query, degraded=True, note="fallback")

        runtime = dbmod.VectorStatus(False, "扩展加载被禁用")
        with (
            patch.object(search_api, "get_config", return_value=self._Config()),
            patch.object(search_api.dbmod, "get_conn", return_value=object()),
            patch.object(
                search_api.dbmod,
                "vector_channel_status",
                return_value=runtime,
            ),
            patch.object(search_api, "retrieve", side_effect=fake_retrieve),
        ):
            response = search_api.search(search_api.SearchRequest(query="猎首"))

        self.assertTrue(response.degraded)
        self.assertIs(captured["vector_enabled"], False)
        self.assertIn("DeepSeek 单模型", captured["vector_disabled_reason"])


class HealthFallbackTest(unittest.TestCase):
    class _Config(SearchEntryPointFallbackTest._Config):
        db_path = Path("/tmp/poe-agent-vector-test.db")

    def test_health区分配置请求与运行时启用状态(self) -> None:
        import main

        conn = sqlite3.connect(":memory:")
        conn.executescript(
            "CREATE TABLE entity(id TEXT);"
            "CREATE TABLE chunk(id TEXT);"
            "CREATE TABLE alias(id TEXT);"
        )
        secrets = SimpleNamespace(
            llm_base_url="",
            llm_model="",
            embedding_base_url="",
            embedding_model="",
            credential_specs=lambda: [object()],
        )
        client = SimpleNamespace(
            credential_status=lambda: [{"healthy": True}],
        )
        runtime = dbmod.VectorStatus(False, "扩展加载被禁用")
        try:
            with (
                patch.object(main, "cfg", self._Config()),
                patch.object(main.dbmod, "get_conn", return_value=conn),
                patch.object(
                    main.dbmod,
                    "vector_status",
                    return_value=dbmod.VectorStatus(False, "扩展加载被禁用"),
                ),
                patch.object(
                    main.dbmod,
                    "vector_channel_status",
                    return_value=runtime,
                ),
                patch.object(main, "get_secrets", return_value=secrets),
                patch.object(main, "get_llm_client", return_value=client),
            ):
                result = main.health()
        finally:
            conn.close()

        self.assertEqual("degraded", result["status"])
        self.assertFalse(result["vector"]["extension_available"])
        self.assertTrue(result["vector"]["requested"])
        self.assertFalse(result["vector"]["enabled"])
        self.assertIn("DeepSeek 单模型", result["vector"]["reason"])


if __name__ == "__main__":
    unittest.main()
