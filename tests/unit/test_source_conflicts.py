"""跨来源 confirmed/review 查询、聚合、工具与 API 契约。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api.conflicts import (
    SourceConflictLookupRequest,
    conflict_detail as conflict_detail_api,
    conflict_lookup as conflict_lookup_api,
    conflict_summary as conflict_summary_api,
)
from core import db as dbmod
from kb.conflicts import (
    ConflictQueryError,
    MAX_DETAIL_TEXT_LENGTH,
    get_conflict,
    lookup_conflicts,
    status_for_kind,
    summarize_conflicts,
)
from orchestrator import loop
from orchestrator.loop import _preferred_tool_route
from tests.unit.test_boss_survival import _conn
from tools.impl.conflicts import source_conflict_lookup, source_conflict_summary
from tools.registry import REGISTRY


def _insert(
    conn,
    *, subject: str, field: str = "value", kind: str = "spectre",
    source_a: str = "POEDB", value_a: str = "100",
    source_b: str = "Path of Building", value_b: str = "1.0",
    resolved: int = 0, cause: str | None = None,
    note: str | None = None, url_a: str | None = "https://poedb.tw/a",
    url_b: str | None = "https://github.com/PathOfBuildingCommunity/PathOfBuilding",
) -> int:
    cursor = conn.execute(
        "INSERT INTO source_conflict("
        "subject,field,kind,source_a,value_a,url_a,source_b,value_b,url_b,note,cause,"
        "resolved,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            subject, field, kind, source_a, value_a, url_a,
            source_b, value_b, url_b, note, cause, resolved,
            "2026-01-01T00:00:00Z", "2026-10-09T00:00:00Z",
        ),
    )
    return int(cursor.lastrowid)


def _fixture_conn():
    conn = _conn()
    conn.execute("DELETE FROM source_conflict")
    _insert(
        conn, subject="spectre=Cursed Mariner", field="life_damage",
        value_a="生命 100%、伤害 165%", value_b="生命 1×、伤害 1.32×",
    )
    _insert(
        conn, subject="spectre=Ancient Suffering", field="life_damage",
        value_a="生命 180%、伤害 120%", value_b="生命 1.5×、伤害 1.2×",
    )
    _insert(
        conn, subject="mod=Confirmed Example", field="ranges", kind="mod",
        value_a="(10-20)", value_b="(11-21)",
    )
    _insert(
        conn, subject="spectre=Resolved", field="life_damage", resolved=1,
        value_a="100", value_b="1.0", note="已人工处理",
    )
    _insert(
        conn, subject="mod=Review A", field="ranges", kind="mod_review",
        value_a="(10-20)", value_b="(10-21)", cause="ladder_partial",
    )
    _insert(
        conn, subject="mod=Review B", field="ranges", kind="mod_review",
        value_a="(30-40)", value_b="(31-40)", cause="ladder_ambiguous",
    )
    _insert(
        conn, subject="spectre=Granularity", field="variants",
        kind="spectre_review", value_a="3 variants", value_b="1 model",
        cause=None,
    )
    _insert(
        conn, subject="literal%_token", field="special", kind="tree_review",
        value_a="A", value_b="B", cause="escaped_query",
    )
    conn.commit()
    return conn


class ConflictDomainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _fixture_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_kind_suffix_is_the_only_status_classifier(self) -> None:
        self.assertEqual("confirmed", status_for_kind("spectre"))
        self.assertEqual("confirmed", status_for_kind("reviewed"))
        self.assertEqual("review", status_for_kind("spectre_review"))
        with self.assertRaisesRegex(ConflictQueryError, "不能为空"):
            status_for_kind("")

    def test_default_query_returns_only_active_confirmed(self) -> None:
        result = lookup_conflicts(self.conn)
        self.assertEqual(3, result.total_matching)
        self.assertEqual(3, result.confirmed_count)
        self.assertEqual(0, result.review_count)
        self.assertEqual(0, result.resolved_count)
        self.assertTrue(all(item.status == "confirmed" for item in result.items))
        self.assertTrue(all(not item.resolved for item in result.items))

    def test_review_requires_explicit_status(self) -> None:
        result = lookup_conflicts(self.conn, status="review", limit=20)
        self.assertEqual(4, result.total_matching)
        self.assertEqual(0, result.confirmed_count)
        self.assertEqual(4, result.review_count)
        self.assertTrue(all(item.kind.endswith("_review") for item in result.items))

    def test_all_status_and_resolution_keeps_categories_separate(self) -> None:
        result = lookup_conflicts(
            self.conn, status="all", resolution="all", limit=20,
        )
        self.assertEqual(8, result.total_matching)
        self.assertEqual(4, result.confirmed_count)
        self.assertEqual(4, result.review_count)
        self.assertEqual(1, result.resolved_count)

    def test_resolved_filter_only_returns_resolved_rows(self) -> None:
        result = lookup_conflicts(
            self.conn, status="confirmed", resolution="resolved", limit=20,
        )
        self.assertEqual(1, result.total_matching)
        self.assertTrue(result.items[0].resolved)
        self.assertEqual("spectre=Resolved", result.items[0].subject)

    def test_query_searches_subject_field_and_values_with_literal_wildcards(self) -> None:
        subject = lookup_conflicts(
            self.conn, query="Cursed Mariner", status="all", limit=20,
        )
        self.assertEqual(1, subject.total_matching)
        by_value = lookup_conflicts(
            self.conn, query="1.32×", status="confirmed", limit=20,
        )
        self.assertEqual("spectre=Cursed Mariner", by_value.items[0].subject)
        escaped = lookup_conflicts(
            self.conn, query="%_", status="review", limit=20,
        )
        self.assertEqual(1, escaped.total_matching)
        self.assertEqual("literal%_token", escaped.items[0].subject)

    def test_kind_and_source_filters_are_parameterized_and_composable(self) -> None:
        result = lookup_conflicts(
            self.conn, status="review", kinds=["mod_review"],
            sources=["Path of Building"], limit=20,
        )
        self.assertEqual(2, result.total_matching)
        self.assertTrue(all(item.kind == "mod_review" for item in result.items))
        none = lookup_conflicts(
            self.conn, status="review", kinds=["mod_review"],
            sources=["Unknown Source"], limit=20,
        )
        self.assertEqual(0, none.total_matching)

    def test_cursor_pages_without_duplicates_and_preserves_total(self) -> None:
        first = lookup_conflicts(
            self.conn, status="review", limit=2,
        )
        self.assertTrue(first.has_more)
        self.assertIsNotNone(first.next_cursor)
        second = lookup_conflicts(
            self.conn, status="review", limit=2, cursor=first.next_cursor,
        )
        self.assertFalse(second.has_more)
        self.assertEqual(4, first.total_matching)
        self.assertEqual(4, second.total_matching)
        first_ids = {item.conflict_id for item in first.items}
        second_ids = {item.conflict_id for item in second.items}
        self.assertFalse(first_ids & second_ids)

    def test_cursor_is_bound_to_filter_scope(self) -> None:
        first = lookup_conflicts(self.conn, status="review", limit=1)
        with self.assertRaisesRegex(ConflictQueryError, "过滤条件"):
            lookup_conflicts(
                self.conn, status="confirmed", limit=1,
                cursor=first.next_cursor,
            )
        with self.assertRaisesRegex(ConflictQueryError, "解码|损坏"):
            lookup_conflicts(self.conn, cursor="not-a-cursor")
        with self.assertRaisesRegex(ConflictQueryError, "解码"):
            lookup_conflicts(
                self.conn, status="review", limit=1,
                cursor=f"{first.next_cursor}!",
            )

    def test_filter_array_order_does_not_invalidate_cursor(self) -> None:
        first = lookup_conflicts(
            self.conn, status="review",
            kinds=["tree_review", "mod_review"], limit=1,
        )
        second = lookup_conflicts(
            self.conn, status="review",
            kinds=["mod_review", "tree_review"], limit=1,
            cursor=first.next_cursor,
        )
        self.assertEqual(("mod_review", "tree_review"), second.kinds)
        self.assertFalse({first.items[0].conflict_id} & {
            item.conflict_id for item in second.items
        })

    def test_limit_and_filter_validation_are_bounded(self) -> None:
        for kwargs in (
            {"limit": 0}, {"limit": True}, {"status": "pending"},
            {"resolution": "open"}, {"kinds": ["x"] * 21},
            {"sources": [""]}, {"query": "x" * 161},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ConflictQueryError):
                    lookup_conflicts(self.conn, **kwargs)

    def test_summary_never_uses_table_total_as_confirmed_count(self) -> None:
        summary = summarize_conflicts(self.conn)
        self.assertEqual(8, summary.total_count)
        self.assertEqual(4, summary.confirmed_count)
        self.assertEqual(3, summary.confirmed_active_count)
        self.assertEqual(4, summary.review_count)
        self.assertEqual(1, summary.resolved_count)
        by_kind = {row.kind: row for row in summary.kinds}
        self.assertEqual("confirmed", by_kind["spectre"].status)
        self.assertEqual("review", by_kind["mod_review"].status)
        causes = {row.cause: row.review_count for row in summary.review_causes}
        self.assertEqual(1, causes["未标注"])

    def test_summary_separates_source_pair_confirmed_and_review(self) -> None:
        summary = summarize_conflicts(self.conn)
        pair = next(
            row for row in summary.source_pairs
            if row.source_a == "POEDB" and row.source_b == "Path of Building"
        )
        self.assertEqual(4, pair.confirmed_count)
        self.assertEqual(4, pair.review_count)
        self.assertEqual(8, pair.total_count)

    def test_empty_table_summary_is_all_zero(self) -> None:
        self.conn.execute("DELETE FROM source_conflict")
        summary = summarize_conflicts(self.conn)
        self.assertEqual(0, summary.total_count)
        self.assertEqual(0, summary.confirmed_count)
        self.assertEqual(0, summary.review_count)
        self.assertEqual((), summary.kinds)
        self.assertEqual((), summary.source_pairs)
        self.assertEqual((), summary.review_causes)

    def test_exact_detail_retains_review_status_and_source_references(self) -> None:
        row = lookup_conflicts(self.conn, status="review", query="Review A").items[0]
        self.conn.execute(
            "UPDATE source_conflict SET ref_a=?, ref_b=? WHERE conflict_id=?",
            ("poedb:42", "pob:mod-42", row.conflict_id),
        )
        detail = get_conflict(self.conn, row.conflict_id)
        self.assertIsNotNone(detail)
        self.assertEqual("review", detail.status)
        self.assertEqual("poedb:42", detail.ref_a)
        self.assertEqual("pob:mod-42", detail.ref_b)
        self.assertEqual("(10-20)", detail.value_a)
        self.assertFalse(detail.value_a_truncated)
        self.assertEqual(len("(10-20)"), detail.value_a_length)

    def test_detail_bounds_large_values_and_rejects_invalid_id(self) -> None:
        value = "x" * (MAX_DETAIL_TEXT_LENGTH + 7)
        conflict_id = _insert(self.conn, subject="oversize", value_a=value)
        detail = get_conflict(self.conn, conflict_id)
        self.assertEqual(MAX_DETAIL_TEXT_LENGTH, len(detail.value_a))
        self.assertEqual(len(value), detail.value_a_length)
        self.assertTrue(detail.value_a_truncated)
        self.assertFalse(detail.value_b_truncated)
        self.assertIsNone(get_conflict(self.conn, 999_999))
        for invalid in (0, -1, True, "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ConflictQueryError):
                get_conflict(self.conn, invalid)


class ConflictToolAndApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _fixture_conn()
        self.patcher = patch.object(dbmod, "get_conn", return_value=self.conn)
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.conn.close()

    def test_confirmed_lookup_attaches_conflict_block_and_fact_citations(self) -> None:
        result = source_conflict_lookup(query="Cursed Mariner")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["found"])
        self.assertEqual(1, result.data["confirmed_count"])
        self.assertIn("conflict_block", result.data)
        self.assertIn("来源冲突", result.data["conflict_block"])
        self.assertTrue(result.citations[0]["can_support_facts"])
        self.assertEqual("structured", result.citations[0]["channel"])

    def test_review_lookup_never_attaches_conflict_block(self) -> None:
        result = source_conflict_lookup(status="review", query="Review")
        self.assertTrue(result.ok)
        self.assertEqual(2, result.data["review_count"])
        self.assertNotIn("conflict_block", result.data)
        self.assertIn("不是已确认冲突", result.data["review_notice"])
        self.assertIn("待核对差异（不是已确认冲突）", result.data["answer"])
        self.assertTrue(all(not row["can_support_facts"] for row in result.citations))

    def test_mixed_lookup_renders_confirmed_and_review_in_separate_sections(self) -> None:
        result = source_conflict_lookup(status="all", resolution="all", limit=20)
        answer = result.data["answer"]
        self.assertIn("#### 已确认冲突", answer)
        self.assertIn("#### 待核对差异（不是已确认冲突）", answer)
        self.assertEqual(4, result.data["confirmed_count"])
        self.assertEqual(4, result.data["review_count"])

    def test_empty_lookup_is_successful_and_does_not_invent_conflicts(self) -> None:
        result = source_conflict_lookup(query="不存在的对象")
        self.assertTrue(result.ok)
        self.assertFalse(result.data["found"])
        self.assertEqual(0, result.data["total_matching"])
        self.assertNotIn("conflict_block", result.data)
        self.assertIn("没有匹配记录", result.data["answer"])

    def test_summary_tool_discloses_confirmed_review_and_causes(self) -> None:
        result = source_conflict_summary()
        self.assertTrue(result.ok)
        self.assertEqual(8, result.data["total_count"])
        self.assertEqual(4, result.data["confirmed_count"])
        self.assertEqual(4, result.data["review_count"])
        self.assertIn("Review 成因", result.data["answer"])
        self.assertIn("#### 按来源对", result.data["answer"])
        self.assertIn("不把 review 数量", result.data["boundary"])

    def test_markdown_cells_escape_pipes_and_collapse_newlines(self) -> None:
        _insert(
            self.conn, subject="spectre=Pipe", value_a="A|B\nC", value_b="D",
        )
        self.conn.commit()
        result = source_conflict_lookup(query="Pipe")
        self.assertIn("A\\|B C", result.data["answer"])

    def test_chat_route_uses_safe_defaults_and_explicit_review(self) -> None:
        self.assertEqual(
            {"status": "confirmed", "resolution": "active"},
            loop._source_conflict_lookup_args("知识库里有哪些跨来源冲突"),
        )
        self.assertEqual(
            {"status": "review", "resolution": "active", "query": "Review A"},
            loop._source_conflict_lookup_args("查询“Review A”的待核对差异"),
        )
        self.assertEqual(
            {
                "status": "all", "resolution": "all",
                "kinds": ["spectre_review"],
            },
            loop._source_conflict_lookup_args(
                "已确认冲突和 review，包括已解决，kind=spectre_review"
            ),
        )

    def test_chat_conflict_tools_are_code_invoked_and_direct(self) -> None:
        for name in ("source_conflict_lookup", "source_conflict_summary"):
            self.assertIn(name, loop._CODE_INVOKED_TOOLS)
            self.assertIn(name, loop._DETERMINISTIC_DIRECT_TOOLS)
            self.assertNotIn(name, loop._TOOLS_WITHOUT_CITATIONS)

    def test_tool_rejects_cursor_reused_with_other_filter(self) -> None:
        first = source_conflict_lookup(status="review", limit=1)
        second = source_conflict_lookup(
            status="confirmed", limit=1, cursor=first.data["next_cursor"],
        )
        self.assertFalse(second.ok)
        self.assertIn("过滤条件", second.error)

    def test_api_lookup_and_summary_use_user_safe_models(self) -> None:
        response = conflict_lookup_api(SourceConflictLookupRequest(
            status="review", kinds=["mod_review"], limit=10,
        ), _caller="test")
        self.assertEqual(2, response.total_matching)
        self.assertEqual("review", response.items[0].status)
        self.assertIsNotNone(response.review_notice)
        summary = conflict_summary_api(_caller="test")
        self.assertEqual(4, summary.confirmed_count)
        self.assertEqual(4, summary.review_count)

    def test_api_maps_bad_cursor_to_422(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            conflict_lookup_api(SourceConflictLookupRequest(
                cursor="broken", limit=1,
            ), _caller="test")
        self.assertEqual(422, caught.exception.status_code)
        self.assertEqual(
            "invalid_source_conflict_query", caught.exception.detail["error"]
        )

    def test_api_exact_detail_keeps_review_unconfirmed_and_resolved_historical(self) -> None:
        from fastapi import HTTPException

        review_id = lookup_conflicts(
            self.conn, query="Review A", status="review",
        ).items[0].conflict_id
        review = conflict_detail_api(review_id, _caller="test")
        self.assertEqual("review", review.status)
        self.assertFalse(review.can_support_facts)
        self.assertIn("不裁决", review.boundary)
        resolved_id = lookup_conflicts(
            self.conn, query="Resolved", resolution="resolved",
        ).items[0].conflict_id
        self.assertFalse(conflict_detail_api(resolved_id, _caller="test").can_support_facts)
        with self.assertRaises(HTTPException) as missing:
            conflict_detail_api(999_999, _caller="test")
        self.assertEqual(404, missing.exception.status_code)

    def test_http_detail_serialization_and_missing_id(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.conflicts import router as conflict_router
        from api.deps import require_user

        app = FastAPI()
        app.include_router(conflict_router)
        app.dependency_overrides[require_user] = lambda: "test"
        review_id = lookup_conflicts(
            self.conn, query="Review A", status="review",
        ).items[0].conflict_id
        review_detail = get_conflict(self.conn, review_id)
        with patch("api.conflicts.dbmod.get_conn", return_value=None), patch(
            "api.conflicts.get_conflict",
            side_effect=lambda _conn, conflict_id: review_detail if conflict_id == review_id else None,
        ):
            client = TestClient(app)
            detail = client.get(f"/conflicts/{review_id}")
            self.assertEqual(200, detail.status_code)
            payload = detail.json()
            self.assertEqual("review", payload["status"])
            self.assertFalse(payload["can_support_facts"])
            self.assertEqual(len("(10-20)"), payload["value_a_length"])
            self.assertEqual(404, client.get("/conflicts/999999").status_code)
            self.assertEqual(422, client.get("/conflicts/0").status_code)

    def test_openapi_registry_knowledge_and_routes_are_wired(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS
        from main import create_app

        for name in ("source_conflict_lookup", "source_conflict_summary"):
            self.assertIn(name, ALLOWED_TOOLS)
            self.assertIsNotNone(REGISTRY.spec(name))
        self.assertEqual(
            "source_conflict_lookup",
            _preferred_tool_route("知识库里有哪些已确认的跨来源数据冲突"),
        )
        self.assertEqual(
            "source_conflict_summary",
            _preferred_tool_route("跨来源冲突有多少，按 kind 怎么分布"),
        )
        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/conflicts/lookup", paths)
        self.assertIn("/poe_agent/conflicts/summary", paths)
        self.assertIn("/poe_agent/conflicts/{conflict_id}", paths)


if __name__ == "__main__":
    unittest.main()
