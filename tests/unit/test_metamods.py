"""Craft of Exile Metamod Matrix：抽取、存储、审计、覆盖与工具边界。"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from core.llm import LLMResponse, StreamEvent, ToolCall  # noqa: E402
from kb.coverage import probe  # noqa: E402
from kb.pipeline import (  # noqa: E402
    _replace_metamod_interactions,
    _validate_metamod_snapshot_size,
    run_poedb_metamods,
)
from kb.sources.craftofexile_metamods import (  # noqa: E402
    METAMOD_MIRROR_URL,
    METAMOD_SOURCE_URL,
    MetamodMatrixStructureError,
    extract_metamod_interactions,
)
from orchestrator import binding, loop  # noqa: E402
from orchestrator.binding import Evidence  # noqa: E402
from tools.impl.metamods import metamod_interaction_matrix  # noqa: E402
from tools.registry import REGISTRY, ToolResult  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "poedb_metamods_sample.html"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _populated_conn(*, with_audit: bool = True) -> sqlite3.Connection:
    conn = _conn()
    interactions, stats = extract_metamod_interactions(_load_fixture())
    _replace_metamod_interactions(
        conn, interactions, "2026-08-10T00:00:00+00:00"
    )
    if with_audit:
        conn.execute(
            "INSERT INTO ingest_run(source,category,started_at,finished_at,total,succeeded,"
            "skipped,note) VALUES (?,?,datetime('now'),datetime('now'),?,?,?,?)",
            (
                "Craft of Exile",
                "metamod_matrix",
                stats.total_cells,
                stats.explicit_cells,
                len(stats.blank_cells),
                json.dumps(
                    {"blank_cells": [cell.canonical() for cell in stats.blank_cells]},
                    ensure_ascii=False,
                ),
            ),
        )
    conn.commit()
    return conn


class TestMetamodExtractor(unittest.TestCase):
    def test_真实裁剪样本保留四态注释与空白缺口(self):
        interactions, stats = extract_metamod_interactions(_load_fixture())

        self.assertEqual(stats.sections, 5)
        self.assertEqual(stats.operation_rows, 5)
        self.assertEqual(stats.total_cells, 20)
        self.assertEqual(stats.explicit_cells, 18)
        self.assertEqual(len(stats.blank_cells), 2)
        self.assertEqual(
            stats.outcomes,
            {"yes": 10, "no": 2, "not_applicable": 2, "conditional": 4},
        )
        conditional = next(
            item for item in interactions
            if item.operation_text == "新增" and item.guard_text == "无法骰出攻击词缀"
        )
        self.assertEqual(conditional.outcome, "conditional")
        self.assertEqual(conditional.raw_value, "YES & NO")
        self.assertEqual(conditional.note_text, "Does not apply to the remove part")
        self.assertEqual(conditional.version_scope, "unknown")
        self.assertEqual(conditional.source_url, METAMOD_SOURCE_URL)
        self.assertEqual(conditional.mirror_url, METAMOD_MIRROR_URL)
        self.assertEqual(
            {(cell.operation_text, cell.guard_text) for cell in stats.blank_cells},
            {
                ("异能混沌石", "无法骰出攻击词缀"),
                ("异能混沌石", "无法骰出法术词缀"),
            },
        )

    def test_稳定键不随结果变化但内容哈希会变化(self):
        first, _ = extract_metamod_interactions(_load_fixture())
        changed_html = _load_fixture().replace(
            '<span class="green">YES</span></td>\n      <td><span class="green">YES</span>',
            '<span class="fire">NO</span></td>\n      <td><span class="green">YES</span>',
            1,
        )
        second, _ = extract_metamod_interactions(changed_html)
        self.assertEqual(first[0].interaction_id, second[0].interaction_id)
        self.assertNotEqual(first[0].content_hash, second[0].content_hash)

    def test_关键来源结构与未知非空值整批拒绝(self):
        cases = (
            _load_fixture().replace(METAMOD_SOURCE_URL, "https://example.invalid", 1),
            _load_fixture().replace("可能并不总是与游戏的当前状态一致", "仅供参考", 1),
            _load_fixture().replace("前缀无法被变更", "前缀锁定", 1),
            _load_fixture().replace(
                '<span class="green">YES</span>',
                '<span class="green">MAYBE</span>',
                1,
            ),
        )
        for html in cases:
            with self.subTest():
                with self.assertRaises(MetamodMatrixStructureError):
                    extract_metamod_interactions(html)

    def test_conditional缺原始注释整批拒绝(self):
        html = _load_fixture().replace(
            '<br><span class="small">Does not apply to the remove part</span>',
            "",
            1,
        )
        with self.assertRaisesRegex(MetamodMatrixStructureError, "缺少条件注释"):
            extract_metamod_interactions(html)

    def test_重复操作名拒绝以保持operation加guard唯一(self):
        html = _load_fixture().replace("玷污的神圣泪珠", "混沌石", 1)
        with self.assertRaisesRegex(MetamodMatrixStructureError, "操作名.*重复"):
            extract_metamod_interactions(html)


class TestMetamodStorageCoverageAndPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _populated_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_只入明确单元格且来源严格标为P4(self):
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM craft_metamod_interaction"
            ).fetchone()[0],
            18,
        )
        sources = self.conn.execute(
            "SELECT DISTINCT source,source_priority,source_url,mirror_url,version_scope "
            "FROM craft_metamod_interaction"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in sources],
            [("Craft of Exile", 4, METAMOD_SOURCE_URL, METAMOD_MIRROR_URL, "unknown")],
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT outcome FROM craft_metamod_interaction "
                "WHERE operation_text='异能混沌石' AND guard_text='无法骰出攻击词缀'"
            ).fetchone()
        )

    def test_首次截短快照默认拒绝但可显式覆盖(self):
        fresh = _conn()
        try:
            with self.assertRaisesRegex(MetamodMatrixStructureError, "安全下限"):
                _validate_metamod_snapshot_size(fresh, 18)
            audit = _validate_metamod_snapshot_size(
                fresh, 18, allow_shrink=True
            )
            self.assertTrue(audit["allow_shrink_applied"])
            self.assertEqual(audit["guard_reason"], "below_absolute_minimum")
        finally:
            fresh.close()

    def test_pipeline把空白格与版本缺口写入持久审计(self):
        class FakeConfig:
            db_path = Path("unused.db")

            @staticmethod
            def get(_key, default=None):
                return default

        response = SimpleNamespace(
            ok=True,
            text=_load_fixture(),
            status_code=200,
            content_hash="fixture-page-hash",
            from_cache=True,
            fetched_at="2026-08-10T00:00:00+00:00",
        )
        fetcher = SimpleNamespace(fetch=lambda _url, force=False: response)
        self.conn.execute("DELETE FROM craft_metamod_interaction")
        self.conn.commit()
        with (
            patch("kb.pipeline.get_config", return_value=FakeConfig()),
            patch("kb.pipeline.dbmod.init_db", return_value=self.conn),
            patch("kb.pipeline._make_fetcher", return_value=fetcher),
        ):
            self.assertEqual(run_poedb_metamods(allow_shrink=True), 0)

        run = self.conn.execute(
            "SELECT total,succeeded,skipped,note FROM ingest_run "
            "WHERE category='metamod_matrix' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(run[:3]), (20, 18, 2))
        note = json.loads(run["note"])
        self.assertEqual(note["version_scope"], "unknown")
        self.assertEqual(len(note["blank_cells"]), 2)
        self.assertEqual(note["source_url"], METAMOD_SOURCE_URL)
        self.assertEqual(note["mirror_url"], METAMOD_MIRROR_URL)
        self.assertEqual(
            note["boundary"], "matrix_cells_only_no_execution_or_route_semantics"
        )

    def test_coverage独立成partial且完整craft_rule仍不可用(self):
        statuses = {status.key: status for status in probe(self.conn)}
        matrix = statuses["metamod_matrix"]
        rules = statuses["craft_rule"]
        self.assertEqual(matrix.status, "partial")
        self.assertEqual(matrix.item_count, 18)
        self.assertEqual(
            set(matrix.channels), {"工艺方式", "丰收园艺", "兽猎工艺", "灾魇工艺", "异能工艺"}
        )
        self.assertIn("metamod_interaction_matrix", matrix.spec.tools)
        self.assertIn("空白格", matrix.spec.partial_note)
        self.assertNotIn("check_mod_combination", matrix.spec.tools)
        self.assertNotIn(rules.status, ("ingested", "partial"))


class TestMetamodTool(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _populated_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_精确查询返回P4受限Citation(self):
        with patch("tools.impl.metamods.dbmod.get_conn", return_value=self.conn):
            result = metamod_interaction_matrix("新增", "无法骰出攻击词缀")

        self.assertTrue(result.ok)
        self.assertTrue(result.data["found"])
        self.assertEqual(len(result.citations), 1)
        evidence = result.citations[0]
        self.assertEqual(evidence["source"], "Craft of Exile")
        self.assertEqual(evidence["source_priority"], 4)
        self.assertEqual(evidence["url"], METAMOD_MIRROR_URL)
        self.assertEqual(evidence["claim_scope"], "metamod_matrix")
        self.assertEqual(evidence["claim_data"]["outcome"], "conditional")
        self.assertEqual(evidence["claim_data"]["raw_value"], "YES & NO")
        self.assertEqual(
            evidence["claim_data"]["note_text"],
            "Does not apply to the remove part",
        )
        self.assertIn("版本范围：unknown", evidence["text"])
        self.assertNotIn("steps", result.data)
        self.assertNotIn("probability", result.data)
        self.assertNotIn("cost", result.data)

    def test_不做同义词匹配且空白格单独声明缺据(self):
        with patch("tools.impl.metamods.dbmod.get_conn", return_value=self.conn):
            synonym = metamod_interaction_matrix("加词", "无法骰出攻击词缀")
            blank = metamod_interaction_matrix("异能混沌石", "无法骰出攻击词缀")
        self.assertTrue(synonym.ok)
        self.assertFalse(synonym.data["found"])
        self.assertFalse(synonym.data["source_blank"])
        self.assertTrue(blank.ok)
        self.assertFalse(blank.data["found"])
        self.assertTrue(blank.data["source_blank"])
        self.assertIn("不能推断为 N/A", blank.data["missing_reason"])
        self.assertEqual(blank.citations, [])

    def test_参数schema严格且与运行时一致(self):
        with patch("tools.impl.metamods.dbmod.get_conn", return_value=self.conn):
            self.assertFalse(metamod_interaction_matrix("", "无法骰出攻击词缀").ok)
        spec = REGISTRY.spec("metamod_interaction_matrix")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(set(spec.parameters["required"]), {"operation", "guard"})
        self.assertFalse(spec.parameters.get("additionalProperties", True))


class TestMetamodClaimScope(unittest.TestCase):
    def setUp(self) -> None:
        self.evidence = [
            Evidence(
                label="E1",
                chunk_id="metamod_interaction:test",
                entity_id=None,
                title="Metamod Matrix：新增 × 无法骰出攻击词缀",
                url=METAMOD_MIRROR_URL,
                source="Craft of Exile",
                source_priority=4,
                text=(
                    "操作：新增；Guard：无法骰出攻击词缀；原始结果：YES & NO；"
                    "原始注释：Does not apply to the remove part；版本范围：unknown"
                ),
                claim_scope="metamod_matrix",
                claim_data={
                    "section": "丰收园艺",
                    "operation_text": "新增",
                    "guard_text": "无法骰出攻击词缀",
                    "outcome": "conditional",
                    "raw_value": "YES & NO",
                    "note_text": "Does not apply to the remove part",
                    "version_scope": "unknown",
                },
            )
        ]

    def _verify(self, sentence: str):
        return binding.verify(sentence, self.evidence)

    def test_只放行逐字存在性结果注释与边界(self):
        allowed = (
            "来源矩阵包含“新增”与“无法骰出攻击词缀”的交互记录[[E1]]。",
            "来源矩阵记录“新增”在“无法骰出攻击词缀”下的原始结果为 YES & NO[[E1]]。",
            "来源矩阵对“新增”与“无法骰出攻击词缀”的原始注释为 Does not apply to the remove part[[E1]]。",
            "该矩阵记录不证明它在给定装备状态下可执行[[E1]]。",
        )
        for sentence in allowed:
            with self.subTest(sentence=sentence):
                result = self._verify(sentence)
                self.assertEqual(result.blocked, 0, [v.reason for v in result.verdicts])
                self.assertEqual(result.used_labels, ["E1"])

    def test_错误原字段和所有外推语义均被拦(self):
        forbidden = (
            "来源矩阵记录“新增”在“无法骰出攻击词缀”下的原始结果为 YES[[E1]]。",
            "来源矩阵记录“重骰”在“无法骰出攻击词缀”下的原始结果为 YES & NO[[E1]]。",
            "来源矩阵对“新增”与“无法骰出攻击词缀”的原始注释为 错误注释[[E1]]。",
            "这个 YES & NO 表示该操作可以用于当前装备[[E1]]。",
            "该操作会移除现有攻击词缀[[E1]]。",
            "第一步使用该操作[[E1]]。",
            "该操作成功率是 50%[[E1]]。",
            "该操作市场成本是 5 神圣石[[E1]]。",
            "这是最快且最经济的路线[[E1]]。",
            "该矩阵适用于当前赛季[[E1]]。",
            "建议采用该矩阵操作[[E1]]。",
        )
        for sentence in forbidden:
            with self.subTest(sentence=sentence):
                result = self._verify(sentence)
                self.assertEqual(result.blocked, 1)
                self.assertEqual(result.used_labels, [])
                self.assertIn("Metamod 矩阵证据", result.verdicts[0].reason)

    def test_批量与流式对矩阵越界语义判定一致(self):
        sentence = "该操作成功率是 50%[[E1]]。"
        batch = self._verify(sentence)
        gate = binding.StreamingGate(self.evidence)
        streamed = gate.feed(sentence)
        streamed.extend(gate.flush())
        self.assertEqual(batch.blocked, 1)
        self.assertEqual(len(streamed), 1)
        self.assertFalse(streamed[0].ok)
        self.assertEqual(gate.result().blocked, batch.blocked)
        self.assertEqual(gate.result().used_labels, batch.used_labels)

    def test_证据块明确展示窄claim_scope(self):
        rendered = binding.render_evidence_block(self.evidence)
        self.assertIn("仅可支撑逐字 operation × guard", rendered)


class TestMetamodPrecisionRoute(unittest.TestCase):
    PAIRS = (
        ("崇高石", "无法骰出攻击词缀"),
        ("玷污的崇高石", "无法骰出攻击词缀"),
        ("异能混沌石", "无法骰出攻击词缀"),
    )

    def test_明确矩阵问法命中且最长操作名不被子串抢占(self):
        with patch("orchestrator.loop._known_metamod_pairs", return_value=self.PAIRS):
            question = "Craft of Exile 矩阵里玷污的崇高石与无法骰出攻击词缀的结果是什么？"
            self.assertEqual(
                loop._precision_tool_route(question), "metamod_interaction_matrix"
            )
            self.assertEqual(
                loop._metamod_precision_query(question)[1:],
                ("玷污的崇高石", "无法骰出攻击词缀"),
            )
            self.assertEqual(
                loop._validate_precision_call(
                    question,
                    "metamod_interaction_matrix",
                    {"operation": "玷污的崇高石", "guard": "无法骰出攻击词缀"},
                ),
                "",
            )

    def test_不含矩阵限定同义词改参和多余参数均拒绝(self):
        with patch("orchestrator.loop._known_metamod_pairs", return_value=self.PAIRS):
            plain = "异能混沌石与无法骰出攻击词缀怎么互动？"
            self.assertEqual(loop._precision_tool_route(plain), "")
            question = "元词缀矩阵中异能混沌石与无法骰出攻击词缀的结果？"
            for args in (
                {"operation": "混沌石", "guard": "无法骰出攻击词缀"},
                {"operation": "异能混沌石", "guard": "不能出攻击"},
                {
                    "operation": "异能混沌石",
                    "guard": "无法骰出攻击词缀",
                    "section": "异能工艺",
                },
            ):
                with self.subTest(args=args):
                    self.assertTrue(
                        loop._validate_precision_call(
                            question, "metamod_interaction_matrix", args
                        )
                    )

    def test_精确Citation必须有窄scope和完整键值(self):
        citation = {
            "chunk_id": "metamod_interaction:test",
            "url": METAMOD_MIRROR_URL,
            "channel": "structured",
            "claim_scope": "metamod_matrix",
            "claim_data": {
                "operation_text": "新增",
                "guard_text": "无法骰出攻击词缀",
                "raw_value": "YES & NO",
            },
        }
        result = ToolResult(ok=True, citations=[citation])
        self.assertEqual(
            loop._precision_citations("metamod_interaction_matrix", result),
            [citation],
        )
        bad = dict(citation, claim_scope="general")
        self.assertEqual(
            loop._precision_citations(
                "metamod_interaction_matrix", ToolResult(ok=True, citations=[bad])
            ),
            [],
        )


class TestMetamodOrchestratorEndToEnd(unittest.TestCase):
    QUESTION = "Craft of Exile 矩阵里新增与无法骰出攻击词缀的结果是什么？"
    TOOL_CALL = ToolCall(
        id="metamod-1",
        name="metamod_interaction_matrix",
        arguments=json.dumps(
            {"operation": "新增", "guard": "无法骰出攻击词缀"},
            ensure_ascii=False,
        ),
    )
    ANSWER = "来源矩阵记录“新增”在“无法骰出攻击词缀”下的原始结果为 YES & NO[[E1]]。"

    class _Config:
        values = {
            "llm.max_tokens_per_turn": 1000,
            "llm.max_tool_calls": 2,
            "retrieval.factual_terms": [],
        }

        def get(self, key, default=None):
            return self.values.get(key, default)

    class _ChatClient:
        def __init__(self, responses):
            self.responses = iter(responses)

        def chat(self, _messages, **_kwargs):
            return next(self.responses)

    class _StreamClient:
        def __init__(self, rounds):
            self.rounds = iter(rounds)

        def stream(self, _messages, **_kwargs):
            yield from next(self.rounds)

    @staticmethod
    def _kb_result() -> ToolResult:
        return ToolResult(
            ok=True,
            data={
                "sufficient": False,
                "reason": "BM25 依据不足",
                "missing_terms": ["元词缀矩阵"],
            },
            meta={"channels": ["bm25"], "degraded": False},
        )

    @staticmethod
    def _matrix_result() -> ToolResult:
        return ToolResult(
            ok=True,
            data={"found": True, "boundary": "matrix only"},
            citations=[
                {
                    "chunk_id": "metamod_interaction:test",
                    "entity_id": None,
                    "title": "Metamod Matrix：新增 × 无法骰出攻击词缀",
                    "url": METAMOD_MIRROR_URL,
                    "source": "Craft of Exile",
                    "source_priority": 4,
                    "can_support_facts": True,
                    "claim_scope": "metamod_matrix",
                    "claim_data": {
                        "operation_text": "新增",
                        "guard_text": "无法骰出攻击词缀",
                        "outcome": "conditional",
                        "raw_value": "YES & NO",
                        "note_text": "Does not apply to the remove part",
                        "version_scope": "unknown",
                    },
                    "text": "操作：新增；Guard：无法骰出攻击词缀；原始结果：YES & NO",
                    "channel": "structured",
                }
            ],
            meta={"channel": "structured"},
        )

    def _patches(self):
        def registry_call(name, _raw):
            if name == "kb_search":
                return self._kb_result()
            if name == "metamod_interaction_matrix":
                return self._matrix_result()
            raise AssertionError(name)

        stack = ExitStack()
        stack.enter_context(patch("orchestrator.loop.get_config", return_value=self._Config()))
        stack.enter_context(patch("orchestrator.loop.build_system_prompt", return_value="system"))
        stack.enter_context(patch("orchestrator.loop._prepare_tools", return_value=[]))
        stack.enter_context(patch("orchestrator.loop._domain_terms", return_value=()))
        stack.enter_context(patch("orchestrator.loop._persist"))
        stack.enter_context(patch("orchestrator.loop._record_gaps"))
        stack.enter_context(
            patch(
                "orchestrator.loop._known_metamod_pairs",
                return_value=(("新增", "无法骰出攻击词缀"),),
            )
        )
        stack.enter_context(patch("orchestrator.loop.REGISTRY.call", side_effect=registry_call))
        return stack

    def test_kb不足时批量路径仍必须先形成精确矩阵Citation(self):
        client = self._ChatClient(
            [
                LLMResponse(content="", tool_calls=(self.TOOL_CALL,)),
                LLMResponse(content=self.ANSWER),
            ]
        )
        with self._patches():
            result = loop.run_turn(self.QUESTION, client=client)
        self.assertFalse(result.evidence_gap)
        self.assertEqual(result.retrieval_meta["precision_route"], "metamod_interaction_matrix")
        self.assertEqual([e.chunk_id for e in result.evidence], ["metamod_interaction:test"])
        self.assertEqual(result.blocked, 0)
        self.assertEqual([citation.label for citation in result.citations], ["E1"])

    def test_kb不足时流式路径也不提前下发未验证正文(self):
        client = self._StreamClient(
            [
                [StreamEvent(kind="tool_calls", tool_calls=(self.TOOL_CALL,))],
                [StreamEvent(kind="content", text=self.ANSWER)],
            ]
        )
        with self._patches():
            events = list(loop.stream_turn(self.QUESTION, client=client))
        self.assertTrue(any(e.get("type") == "tool" for e in events))
        sentences = [e for e in events if e.get("type") == "sentence"]
        self.assertEqual(len(sentences), 1)
        self.assertEqual(sentences[0]["text"], self.ANSWER)
        self.assertTrue(any(e.get("type") == "citations" for e in events))
        self.assertFalse(any(e.get("type") == "gap" for e in events))


class TestMetamodSemanticReviewRegressions(unittest.TestCase):
    def test_dataclass固化来源版本映射且ID与布局解耦(self):
        from kb.sources.craftofexile_metamods import MetamodInteraction

        base = {
            "section": "工艺方式",
            "operation_text": "混沌石",
            "guard_text": "无法骰出攻击词缀",
            "outcome": "yes",
            "raw_value": "YES",
            "note_text": None,
            "row_order": 0,
            "column_order": 2,
        }
        original = MetamodInteraction(**base)
        renamed = MetamodInteraction(**{**base, "section": "分区改名"})
        moved = MetamodInteraction(**{**base, "row_order": 9, "column_order": 3})
        self.assertEqual(original.interaction_id, renamed.interaction_id)
        self.assertEqual(original.interaction_id, moved.interaction_id)
        self.assertEqual(original.content_hash, renamed.content_hash)
        self.assertEqual(original.content_hash, moved.content_hash)

        invalid = (
            {"source_url": "https://example.invalid"},
            {"mirror_url": "https://example.invalid"},
            {"version_scope": "current"},
            {"outcome": "no", "raw_value": "YES"},
            {"outcome": "conditional", "raw_value": "YES & NO", "note_text": None},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(MetamodMatrixStructureError):
                    MetamodInteraction(**{**base, **changes})
        for extra in ({"source": "POEDB"}, {"source_priority": 1}):
            with self.subTest(extra=extra):
                with self.assertRaises(TypeError):
                    MetamodInteraction(**{**base, **extra})

    def test_前插无关操作不改变既有ID与内容哈希(self):
        before, _ = extract_metamod_interactions(_load_fixture())
        inserted_row = """
    <tr>
      <td class="label align-middle">无关操作</td>
      <td><span class="green">YES</span></td><td><span class="green">YES</span></td>
      <td><span class="green">YES</span></td><td><span class="green">YES</span></td>
    </tr>
"""
        changed_html = _load_fixture().replace(
            '    <tr>\n      <td class="label align-middle"><a href="Chaos_Orb">混沌石</a></td>',
            inserted_row
            + '    <tr>\n      <td class="label align-middle"><a href="Chaos_Orb">混沌石</a></td>',
            1,
        )
        after, _ = extract_metamod_interactions(changed_html)
        old = next(
            item for item in before
            if item.operation_text == "混沌石" and item.guard_text == "无法骰出攻击词缀"
        )
        new = next(
            item for item in after
            if item.operation_text == "混沌石" and item.guard_text == "无法骰出攻击词缀"
        )
        self.assertNotEqual(old.row_order, new.row_order)
        self.assertEqual(old.interaction_id, new.interaction_id)
        self.assertEqual(old.content_hash, new.content_hash)

    def test_SQLite拒绝非法provenance版本映射与conditional空注释(self):
        conn = _populated_conn()
        try:
            row = dict(conn.execute(
                "SELECT * FROM craft_metamod_interaction WHERE "
                "operation_text='混沌石' AND guard_text='无法骰出攻击词缀'"
            ).fetchone())
            columns = tuple(row)
            cases = {
                "source": {"source": "POEDB"},
                "source_priority": {"source_priority": 1},
                "source_url": {"source_url": "https://example.invalid"},
                "mirror_url": {"mirror_url": "https://example.invalid"},
                "version_scope": {"version_scope": "current"},
                "raw_outcome": {"raw_value": "YES", "outcome": "no"},
                "conditional_null_note": {
                    "raw_value": "YES & NO", "outcome": "conditional", "note_text": None
                },
                "conditional_empty_note": {
                    "raw_value": "YES & NO", "outcome": "conditional", "note_text": ""
                },
            }
            sql = (
                f"INSERT INTO craft_metamod_interaction({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})"
            )
            for index, (name, changes) in enumerate(cases.items()):
                bad = {
                    **row,
                    "interaction_id": f"metamod:invalid:{index}",
                    "operation_text": f"非法操作{index}",
                    **changes,
                }
                with self.subTest(name=name):
                    with self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(sql, tuple(bad[column] for column in columns))
        finally:
            conn.close()

    def test_真实审计空白长键不会回退为混沌石(self):
        conn = _populated_conn()
        try:
            with patch("orchestrator.loop.dbmod.get_conn", return_value=conn):
                question = "元词缀矩阵中异能混沌石与无法骰出攻击词缀的结果是什么？"
                self.assertEqual(
                    loop._metamod_precision_query(question)[1:],
                    ("异能混沌石", "无法骰出攻击词缀"),
                )
                self.assertEqual(
                    loop._precision_tool_route(question), "metamod_interaction_matrix"
                )
        finally:
            conn.close()

    def test_否定作用域选择正向操作且指令否定不阻断(self):
        conn = _populated_conn()
        try:
            cases = (
                (
                    "元词缀矩阵里不是异能混沌石，而是混沌石与无法骰出攻击词缀的结果？",
                    "混沌石",
                ),
                (
                    "元词缀矩阵里别解释机制，只给混沌石与无法骰出攻击词缀的原始结果。",
                    "混沌石",
                ),
                (
                    "元词缀矩阵里不要解释机制只给混沌石与无法骰出攻击词缀的原始结果。",
                    "混沌石",
                ),
                (
                    "元词缀矩阵里不要异能混沌石只看混沌石与无法骰出攻击词缀的结果。",
                    "混沌石",
                ),
                (
                    "元词缀矩阵里不要解释机制只给异能混沌石与无法骰出攻击词缀的原始结果。",
                    "异能混沌石",
                ),
                (
                    "元词缀矩阵里只给混沌石与无法骰出攻击词缀的原始结果不要解释机制。",
                    "混沌石",
                ),
                (
                    "不要用其他来源只看元词缀矩阵中混沌石与无法骰出攻击词缀的原始结果。",
                    "混沌石",
                ),
                (
                    "不看其他资料只查元词缀矩阵中混沌石与无法骰出攻击词缀的原始结果。",
                    "混沌石",
                ),
                (
                    "排除其他来源只查元词缀矩阵中混沌石与无法骰出攻击词缀的原始结果。",
                    "混沌石",
                ),
                (
                    "不要用元词缀矩阵，改为参考元词缀矩阵中混沌石与无法骰出攻击词缀的原始结果。",
                    "混沌石",
                ),
                (
                    "只查元词缀矩阵，不要用其他来源，混沌石与无法骰出攻击词缀的原始结果是什么？",
                    "混沌石",
                ),
                (
                    "元词缀矩阵，不要用其他来源，只看混沌石与无法骰出攻击词缀的原始结果。",
                    "混沌石",
                ),
                (
                    "只看来源矩阵；请勿使用其他资料；混沌石与无法骰出攻击词缀的结果是什么？",
                    "混沌石",
                ),
            )
            source_negated = (
                "我不是问元词缀矩阵，只看混沌石与无法骰出攻击词缀怎么互动。",
                "不要用元词缀矩阵，混沌石与无法骰出攻击词缀怎么互动。",
                "元词缀矩阵不是我想问的，只看混沌石与无法骰出攻击词缀怎么互动。",
                "只看混沌石与无法骰出攻击词缀怎么互动，元词缀矩阵不用。",
                "元词缀矩阵，不用，只看混沌石与无法骰出攻击词缀怎么互动。",
                "元词缀矩阵？不用。只看混沌石与无法骰出攻击词缀怎么互动。",
                "元词缀矩阵也不用，只看混沌石与无法骰出攻击词缀怎么互动。",
                "不用参考元词缀矩阵，只看混沌石与无法骰出攻击词缀怎么互动。",
                "不看这个元词缀矩阵，只看混沌石与无法骰出攻击词缀怎么互动。",
                "元词缀矩阵不要作为来源，只看混沌石与无法骰出攻击词缀怎么互动。",
                "请不要把元词缀矩阵作为来源，只看混沌石与无法骰出攻击词缀怎么互动。",
                "元词缀矩阵我不想用，只看混沌石与无法骰出攻击词缀怎么互动。",
                "元词缀矩阵先排除掉，只看混沌石与无法骰出攻击词缀怎么互动。",
                "请不要将元词缀矩阵作为来源，只看混沌石与无法骰出攻击词缀怎么互动。",
                "请勿把元词缀矩阵作为来源，只看混沌石与无法骰出攻击词缀怎么互动。",
                "我不想参考元词缀矩阵，只看混沌石与无法骰出攻击词缀怎么互动。",
                "先参考元词缀矩阵，再不要使用元词缀矩阵作为来源，只看混沌石与无法骰出攻击词缀怎么互动。",
                "请参考来源矩阵，但请勿把元词缀矩阵作为来源，只看混沌石与无法骰出攻击词缀怎么互动。",
            )
            with patch("orchestrator.loop.dbmod.get_conn", return_value=conn):
                for question, expected_operation in cases:
                    with self.subTest(question=question):
                        self.assertEqual(
                            loop._metamod_precision_query(question)[1:],
                            (expected_operation, "无法骰出攻击词缀"),
                        )
                        self.assertEqual(
                            loop._precision_tool_route(question),
                            "metamod_interaction_matrix",
                        )
                for question in source_negated:
                    with self.subTest(question=question):
                        self.assertEqual(loop._metamod_precision_query(question), ("", "", ""))
                        self.assertNotEqual(
                            loop._precision_tool_route(question),
                            "metamod_interaction_matrix",
                        )
                ambiguous = "元词缀矩阵中未知操作与无法骰出攻击词缀的原始结果。"
                self.assertEqual(
                    loop._metamod_precision_query(ambiguous)[1:], ("", "")
                )
                self.assertEqual(
                    loop._precision_tool_route(ambiguous),
                    "metamod_interaction_matrix",
                )
        finally:
            conn.close()

    def test_来源排除同时裁剪schema并在执行层拒绝伪造调用(self):
        question = (
            "请不要将元词缀矩阵作为来源，只看混沌石与无法骰出攻击词缀怎么互动。"
        )
        schemas = [
            {"type": "function", "function": {"name": "kb_search"}},
            {"type": "function", "function": {"name": "metamod_interaction_matrix"}},
        ]
        with patch("orchestrator.loop._prepare_tools", return_value=schemas):
            filtered = loop._tools_for_question(question)
        self.assertEqual(
            [schema["function"]["name"] for schema in filtered], ["kb_search"]
        )
        with patch("orchestrator.loop.REGISTRY.call") as registry_call:
            result, matched = loop._call_tool(
                question,
                "",
                "metamod_interaction_matrix",
                json.dumps(
                    {"operation": "混沌石", "guard": "无法骰出攻击词缀"},
                    ensure_ascii=False,
                ),
            )
        self.assertFalse(result.ok)
        self.assertIn("明确排除了", result.error)
        self.assertFalse(matched)
        registry_call.assert_not_called()

    def test_claim原字段变体与字段内marker在批量流式均拒绝(self):
        case = TestMetamodClaimScope(methodName="test_只放行逐字存在性结果注释与边界")
        case.setUp()
        forbidden = (
            "来源矩阵记录“新 增”在“无法骰出攻击词缀”下的原始结果为 YES & NO[[E1]]。",
            "来源矩阵记录“新增”在“无法骰出攻击词缀”下的原始结果为 yes & no[[E1]]。",
            "来源矩阵对“新增”与“无法骰出攻击词缀”的原始注释为 Doesnotapplytotheremovepart[[E1]]。",
            "来源矩阵记录“新[[E1]]增”在“无法骰出攻击词缀”下的原始结果为 YES & NO。",
            "来源矩阵对“新增”与“无法骰出攻击词缀”的原始注释为 Does[[E1]] not apply to the remove part。",
        )
        for sentence in forbidden:
            with self.subTest(sentence=sentence):
                batch = binding.verify(sentence, case.evidence)
                gate = binding.StreamingGate(case.evidence)
                gate.feed(sentence)
                gate.flush()
                streamed = gate.result()
                self.assertEqual(batch.blocked, 1)
                self.assertEqual(batch.used_labels, [])
                self.assertEqual(streamed.blocked, 1)
                self.assertEqual(streamed.used_labels, [])

    def test_fetch_extract_commit异常均形成终态且不破坏旧快照(self):
        class FakeConfig:
            db_path = Path("unused.db")

            @staticmethod
            def get(_key, default=None):
                return default

        def snapshot(conn):
            return [tuple(row) for row in conn.execute(
                "SELECT interaction_id,content_hash FROM craft_metamod_interaction "
                "ORDER BY interaction_id"
            )]

        def latest_run(conn):
            return conn.execute(
                "SELECT finished_at,failed,succeeded,note FROM ingest_run "
                "WHERE category='metamod_matrix' ORDER BY run_id DESC LIMIT 1"
            ).fetchone()

        response = SimpleNamespace(
            ok=True,
            text=_load_fixture(),
            status_code=200,
            content_hash="fixture-page-hash",
            from_cache=True,
            fetched_at="2026-08-10T00:00:00+00:00",
        )
        scenarios = ("fetch_exception", "extract_exception", "commit_exception")
        for status in scenarios:
            conn = _populated_conn()
            before = snapshot(conn)
            fetcher = SimpleNamespace(fetch=lambda _url, force=False: response)
            patches = ExitStack()
            try:
                patches.enter_context(patch("kb.pipeline.get_config", return_value=FakeConfig()))
                patches.enter_context(patch("kb.pipeline.dbmod.init_db", return_value=conn))
                patches.enter_context(patch("kb.pipeline._make_fetcher", return_value=fetcher))
                if status == "fetch_exception":
                    fetcher.fetch = lambda _url, force=False: (_ for _ in ()).throw(
                        RuntimeError("sensitive fetch detail")
                    )
                    expected = RuntimeError
                elif status == "extract_exception":
                    patches.enter_context(patch(
                        "kb.pipeline.craftofexile_metamods.extract_metamod_interactions",
                        side_effect=RuntimeError("sensitive extract detail"),
                    ))
                    expected = RuntimeError
                else:
                    def fail_after_delete(target_conn, _items, _fetched_at):
                        target_conn.execute("DELETE FROM craft_metamod_interaction")
                        raise sqlite3.OperationalError("sensitive SQL detail")

                    patches.enter_context(patch(
                        "kb.pipeline._replace_metamod_interactions",
                        side_effect=fail_after_delete,
                    ))
                    expected = sqlite3.OperationalError
                with self.subTest(status=status):
                    with self.assertRaises(expected):
                        run_poedb_metamods(allow_shrink=True)
                    self.assertEqual(snapshot(conn), before)
                    run = latest_run(conn)
                    self.assertIsNotNone(run["finished_at"])
                    self.assertEqual((run["failed"], run["succeeded"]), (1, 0))
                    note = json.loads(run["note"])
                    self.assertEqual(note["status"], status)
                    self.assertEqual(note["error_type"], expected.__name__)
                    self.assertNotIn("sensitive", run["note"])
            finally:
                patches.close()
                conn.close()


class TestMetamodBlankOrchestratorRegression(unittest.TestCase):
    QUESTION = (
        "只查 Craft of Exile 矩阵，不要用其他来源，异能混沌石与"
        "无法骰出攻击词缀的原始结果是什么？"
    )
    TOOL_CALL = ToolCall(
        id="metamod-blank",
        name="metamod_interaction_matrix",
        arguments=json.dumps(
            {"operation": "异能混沌石", "guard": "无法骰出攻击词缀"},
            ensure_ascii=False,
        ),
    )

    def _patches(self, conn, *, kb_result=None, tools=None):
        base = TestMetamodOrchestratorEndToEnd(
            methodName="test_kb不足时批量路径仍必须先形成精确矩阵Citation"
        )

        def registry_call(name, raw):
            if name == "kb_search":
                return kb_result or base._kb_result()
            if name == "metamod_interaction_matrix":
                args = json.loads(raw)
                return metamod_interaction_matrix(**args)
            raise AssertionError(name)

        stack = ExitStack()
        stack.enter_context(patch("orchestrator.loop.get_config", return_value=base._Config()))
        stack.enter_context(patch("orchestrator.loop.build_system_prompt", return_value="system"))
        stack.enter_context(
            patch("orchestrator.loop._prepare_tools", return_value=tools or [])
        )
        stack.enter_context(patch("orchestrator.loop._domain_terms", return_value=()))
        stack.enter_context(patch("orchestrator.loop._persist"))
        stack.enter_context(patch("orchestrator.loop._record_gaps"))
        stack.enter_context(patch("orchestrator.loop.dbmod.get_conn", return_value=conn))
        stack.enter_context(patch("tools.impl.metamods.dbmod.get_conn", return_value=conn))
        stack.enter_context(patch("orchestrator.loop.REGISTRY.call", side_effect=registry_call))
        return stack, base

    def test_批量与流式对源空白格只输出专用缺据(self):
        conn = _populated_conn()
        try:
            stack, base = self._patches(conn)
            with stack:
                batch_client = base._ChatClient([
                    LLMResponse(content="", tool_calls=(self.TOOL_CALL,)),
                    LLMResponse(content="来源矩阵记录了错误的 YES[[E1]]。"),
                ])
                result = loop.run_turn(self.QUESTION, client=batch_client)
            self.assertTrue(result.evidence_gap)
            self.assertIn("单元格为空", result.answer)
            self.assertIn("不能推断为 N/A", result.answer)
            self.assertEqual(result.evidence, [])
            self.assertEqual(result.citations, [])

            stack, base = self._patches(conn)
            with stack:
                stream_client = base._StreamClient([
                    [StreamEvent(kind="tool_calls", tool_calls=(self.TOOL_CALL,))],
                    [StreamEvent(kind="content", text="来源矩阵记录了错误的 YES[[E1]]。")],
                ])
                events = list(loop.stream_turn(self.QUESTION, client=stream_client))
            self.assertEqual(len([e for e in events if e.get("type") == "gap"]), 1)
            self.assertFalse(any(e.get("type") == "sentence" for e in events))
            self.assertFalse(any(e.get("type") == "citations" for e in events))
            gap = next(e["text"] for e in events if e.get("type") == "gap")
            self.assertIn("单元格为空", gap)
            self.assertIn("不能推断为 N/A", gap)
        finally:
            conn.close()

    def test_来源后置否定时批量与流式均禁止矩阵工具(self):
        question = (
            "请不要把元词缀矩阵作为来源，只看混沌石与无法骰出攻击词缀怎么互动。"
        )
        conn = _populated_conn()
        try:
            # KB 不充分时直接缺据，不应进入 LLM。
            class NoBatchLLM:
                def chat(self, *_args, **_kwargs):
                    raise AssertionError("来源被否定后不应进入 LLM 或矩阵工具")

            class NoStreamLLM:
                def stream(self, *_args, **_kwargs):
                    raise AssertionError("来源被否定后不应进入 LLM 或矩阵工具")

            stack, _base = self._patches(conn)
            with stack:
                result = loop.run_turn(question, client=NoBatchLLM())
            self.assertTrue(result.evidence_gap)
            self.assertNotIn("precision_route", result.retrieval_meta)
            self.assertEqual([call["tool"] for call in result.tool_calls], ["kb_search"])

            stack, _base = self._patches(conn)
            with stack:
                events = list(loop.stream_turn(question, client=NoStreamLLM()))
            self.assertTrue(any(event.get("type") == "gap" for event in events))
            self.assertFalse(any(event.get("type") == "tool" for event in events))

            # 即使 KB 充分且模型伪造被排除的同名调用，执行层也必须拒绝，
            # 不能让矩阵 Citation 进入 batch/stream evidence。
            sufficient = ToolResult(
                ok=True,
                data={"sufficient": True, "reason": "", "missing_terms": []},
                citations=[],
                meta={"channels": ["bm25"], "degraded": False},
            )
            excluded_call = ToolCall(
                id="excluded-metamod",
                name="metamod_interaction_matrix",
                arguments=json.dumps(
                    {"operation": "混沌石", "guard": "无法骰出攻击词缀"},
                    ensure_ascii=False,
                ),
            )
            schemas = [
                {"type": "function", "function": {"name": "kb_search"}},
                {
                    "type": "function",
                    "function": {"name": "metamod_interaction_matrix"},
                },
            ]
            stack, base = self._patches(conn, kb_result=sufficient, tools=schemas)
            with stack:
                result = loop.run_turn(
                    question,
                    client=base._ChatClient([
                        LLMResponse(content="", tool_calls=(excluded_call,)),
                        LLMResponse(content="没有可用依据。"),
                    ]),
                )
            self.assertFalse(result.tool_calls[-1]["ok"])
            self.assertIn("明确排除了", result.tool_calls[-1]["error"])
            self.assertEqual(result.evidence, [])
            self.assertEqual(result.citations, [])

            stack, base = self._patches(conn, kb_result=sufficient, tools=schemas)
            with stack:
                events = list(loop.stream_turn(
                    question,
                    client=base._StreamClient([
                        [StreamEvent(kind="tool_calls", tool_calls=(excluded_call,))],
                        [StreamEvent(kind="content", text="没有可用依据。")],
                    ]),
                ))
            self.assertFalse(any(event.get("type") == "citations" for event in events))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
