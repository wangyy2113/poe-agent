"""Optional embedding and sqlite-vec path, using deterministic local fake vectors."""

from __future__ import annotations

import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import sqlite_vec
except ImportError:  # Windows on ARM / Python without extension wheel
    sqlite_vec = None

from core import db as dbmod
from retrieval.embedding import EmbeddingClient, EmbeddingError
from retrieval.hybrid import retrieve
from retrieval.runtime import configured_vector_runtime, vector_runtime
from retrieval.vector import backfill_vectors, search_vector, vector_index_status


class FakeClient:
    model = "bge-m3-test"

    def __init__(self, fail_after: int = 0):
        self.calls = 0
        self.fail_after = fail_after

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail_after and self.calls > self.fail_after:
            raise EmbeddingError("模拟远端失败")
        return [
            [1.0, 0.0] if "Alpha" in text else [0.0, 1.0]
            for text in texts
        ]


class EmbeddingClientTest(unittest.TestCase):
    def test_response_is_sorted_by_index_and_validated(self) -> None:
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [
                    {"index": 1, "embedding": [0, 1]},
                    {"index": 0, "embedding": [1, 0]},
                ]}

        with patch("retrieval.embedding.httpx.post", return_value=Response()) as post:
            vectors = EmbeddingClient("https://example.test/v1", "test-key", "bge-m3").embed(
                ["Alpha", "Beta"]
            )
        self.assertEqual([[1.0, 0.0], [0.0, 1.0]], vectors)
        self.assertEqual("https://example.test/v1/embeddings", post.call_args.args[0])
        self.assertNotIn("test-key", str(vectors))

    def test_bad_status_or_nonfinite_vector_fails_without_echoing_key(self) -> None:
        class Response:
            status_code = 400

        client = EmbeddingClient("https://example.test", "private-token", "wrong-model")
        with patch("retrieval.embedding.httpx.post", return_value=Response()):
            with self.assertRaisesRegex(EmbeddingError, "HTTP 400") as caught:
                client.embed(["query"])
        self.assertNotIn("private-token", str(caught.exception))

        class NonFinite:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{"index": 0, "embedding": [float("nan")]}]}

        with patch("retrieval.embedding.httpx.post", return_value=NonFinite()):
            with self.assertRaisesRegex(EmbeddingError, "非有限"):
                client.embed(["query"])

    def test_transient_http_failure_retries_with_bounded_backoff(self) -> None:
        class Response:
            def __init__(self, status_code: int):
                self.status_code = status_code

            @staticmethod
            def json():
                return {"data": [{"index": 0, "embedding": [1, 0]}]}

        with (
            patch("retrieval.embedding.httpx.post", side_effect=[
                Response(503), Response(200),
            ]) as post,
            patch("retrieval.embedding.time.sleep") as sleep,
        ):
            result = EmbeddingClient("https://example.test", "token", "bge-m3").embed(
                ["query"]
            )
        self.assertEqual([[1.0, 0.0]], result)
        self.assertEqual(2, post.call_count)
        sleep.assert_called_once_with(0.25)


class VectorIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        if sqlite_vec is None:
            self.skipTest("sqlite-vec wheel 不可用")
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
        except Exception as exc:  # noqa: BLE001  平台扩展加载错误类型不一致
            self.conn.close()
            self.skipTest(f"SQLite 扩展加载不可用：{exc}")
        self.conn.execute(
            "CREATE TABLE chunk(chunk_id TEXT PRIMARY KEY, entity_id TEXT, "
            "text TEXT NOT NULL, metadata TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE VIRTUAL TABLE chunk_fts USING fts5("
            "tokens, keywords, chunk_id UNINDEXED, entity_id UNINDEXED)"
        )
        for chunk_id, text, category in (
            ("a", "Alpha fire resistance", "item"),
            ("b", "Beta cold resistance", "skill"),
        ):
            self.conn.execute(
                "INSERT INTO chunk VALUES (?,?,?,?)",
                (chunk_id, chunk_id, text,
                 '{"source":"POEDB","source_priority":1,"category":"' + category + '"}'),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_disabled_vector_path_does_not_require_llm_secrets(self) -> None:
        with patch("retrieval.runtime.get_secrets", side_effect=AssertionError("unexpected")):
            status, client = configured_vector_runtime(self.conn, False)
        self.assertFalse(status.available)
        self.assertIsNone(client)

    def test_backfill_query_model_guard_and_invalidation(self) -> None:
        client = FakeClient()
        result = backfill_vectors(self.conn, client, batch_size=1)
        self.assertEqual((2, 2), (result.dimension, result.indexed_count))
        cached_client = FakeClient()
        backfill_vectors(self.conn, cached_client, batch_size=2)
        self.assertEqual(0, cached_client.calls)
        self.assertTrue(vector_index_status(self.conn, client.model).available)
        self.assertFalse(vector_index_status(self.conn, "other-model").available)
        hits = search_vector(self.conn, "Alpha", client, limit=2)
        self.assertEqual("a", hits[0].chunk_id)
        self.assertEqual("vector", hits[0].channel)
        self.assertEqual(["a"], [
            hit.chunk_id for hit in search_vector(
                self.conn, "Alpha", client, limit=2, category="item"
            )
        ])
        self.conn.execute("UPDATE chunk SET text='Alpha changed' WHERE chunk_id='a'")
        self.assertFalse(vector_index_status(self.conn, client.model).available)

    def test_failed_backfill_never_marks_partial_index_ready(self) -> None:
        with self.assertRaisesRegex(EmbeddingError, "模拟远端失败"):
            backfill_vectors(self.conn, FakeClient(fail_after=1), batch_size=1)
        status = vector_index_status(self.conn, FakeClient.model)
        self.assertFalse(status.available)
        self.assertIn("尚未回填", status.reason)

    def test_runtime_enables_only_matching_complete_index_and_credentials(self) -> None:
        backfill_vectors(self.conn, FakeClient())
        secrets = SimpleNamespace(
            embedding_base_url="https://example.test",
            embedding_api_key="token",
            embedding_model=FakeClient.model,
        )
        with (
            patch.object(dbmod, "_conn", self.conn),
            patch.object(dbmod, "_vector_status", dbmod.VectorStatus(True, "sqlite-vec test")),
        ):
            status, client = vector_runtime(self.conn, True, secrets)
            self.assertTrue(status.available)
            self.assertIsNotNone(client)
            secrets.embedding_model = "other-model"
            status, client = vector_runtime(self.conn, True, secrets)
            self.assertFalse(status.available)
            self.assertIsNone(client)
            self.assertIn("模型", status.reason)

    def test_hybrid_falls_back_on_remote_error(self) -> None:
        class FailedClient(FakeClient):
            def embed(self, texts: list[str]) -> list[list[float]]:
                raise EmbeddingError("HTTP 503")

        backfill_vectors(self.conn, FakeClient())
        with (
            patch("retrieval.hybrid.alias_mod.lookup", return_value=[]),
            patch("retrieval.hybrid.search_fts", return_value=[]),
        ):
            result = retrieve(
                self.conn, "Alpha", vector_enabled=True,
                embedding_client=FailedClient(),
            )
        self.assertTrue(result.degraded)
        self.assertNotIn("vector", result.channels)
        self.assertIn("HTTP 503", result.note)


if __name__ == "__main__":
    unittest.main()
