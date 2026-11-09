"""Quality regressions: subject, field, conditions and conversation ownership."""
from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agents.knowledge import build_system_prompt
from core.config import Config
from core.llm import LLMClient, LLMResponse, StreamEvent, ToolCall, TokenBudgetExceeded, TurnBudget, Usage
from orchestrator import loop
from orchestrator.binding import Evidence, verify
from orchestrator.entity_answer import render_entity_answer
from orchestrator.excerpts import passages, render_selection
from orchestrator.question import resolve_followup, without_style
from tests.unit.test_deepseek_policy import secrets
from tools.registry import ToolResult


TEXT = """# 猎首（Headhunter）
**隐式词缀**
- [隐式词缀] +(25-40) 最大生命
**显式词缀**
- [显式词缀] 当你击败稀有怪物时，会获得该怪物属性修正 60 秒
来源：POEDB
https://poedb.tw/cn/Headhunter
"""


def evidence(text=TEXT):
    return Evidence("E1", "headhunter#0", "unique_item:Headhunter", "猎首（Headhunter）",
                    "https://poedb.tw/cn/Headhunter", "POEDB", 1, text,
                    claim_scope="excerpt")


class AnswerQualityTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE entity(entity_id TEXT, name_zh TEXT, name_en TEXT,
                                base_type TEXT, attributes TEXT);
            CREATE TABLE alias(alias_norm TEXT, entity_id TEXT);
            CREATE TABLE conversation(conversation_id TEXT, owner_id TEXT);
            CREATE TABLE message(conversation_id TEXT, role TEXT, content TEXT, created_at TEXT);
            INSERT INTO alias VALUES('猎首', 'unique_item:Headhunter');
            INSERT INTO alias VALUES('法血', 'unique_item:Mageblood');
            INSERT INTO conversation VALUES('chat', 'alice');
        """)
        attrs = {"mods": [
            {"kind": "implicit", "text": "+(25-40) 最大生命", "ranges": [[25, 40]]},
            {"kind": "explicit", "text": "当你击败稀有怪物时，会获得该怪物属性修正 60 秒", "ranges": [[60, 60]]},
        ], "acquisition": [{"source_zh": "疯医", "ingredient": ""}]}
        self.conn.execute("INSERT INTO entity VALUES(?,?,?,?,?)", (
            "unique_item:Headhunter", "猎首", "Headhunter", "皮革腰带", json.dumps(attrs)))

    def message(self, content):
        self.conn.execute("INSERT INTO message VALUES('chat','user',?,'2026-11-09')", (content,))

    def test_followup_uses_owned_subject_and_handles_stream_presave(self):
        self.message("猎首有哪些词缀？")
        self.message("它怎么获得？")
        self.assertEqual(("猎首怎么获得？", ""), resolve_followup(
            self.conn, "它怎么获得？", "chat", "alice"))
        self.assertTrue(resolve_followup(self.conn, "它怎么获得？", "chat", "bob")[1])

    def test_ambiguous_followup_does_not_pick_first_entity(self):
        self.message("猎首和法血有什么区别？")
        question, clarification = resolve_followup(self.conn, "它怎么获得？", "chat", "alice")
        self.assertEqual("它怎么获得？", question)
        self.assertTrue(clarification)

    def test_style_does_not_change_entity_route(self):
        with patch.object(loop.dbmod, "get_conn", return_value=self.conn):
            for question in ("猎首是什么？", "猎首是什么？请简短说明。", "猎首是什么，请用中文回答并附来源。"):
                self.assertEqual("entity_lookup", loop._preferred_tool_route(without_style(question)))
                answer = render_entity_answer(self.conn, question, [evidence()])
                self.assertIn("皮革腰带", answer)
                self.assertNotIn("仿品", answer)

    def test_range_endpoint_and_acquisition_use_existing_fields(self):
        answer = render_entity_answer(self.conn, "猎首的隐式词缀最多加多少生命？", [evidence()])
        self.assertIn("**40**", answer)
        self.assertIn("+(25-40)", answer)
        self.assertNotIn("60 秒", answer)
        self.assertIn("疯医", render_entity_answer(self.conn, "猎首怎么获得？", [evidence()]))

    def test_direct_entity_does_not_answer_comparisons_or_missing_stats(self):
        for question in ("猎首和法血有什么区别？", "猎首的隐式词缀加多少智慧？"):
            self.assertEqual("", render_entity_answer(self.conn, question, [evidence()]))

    def test_wrong_trigger_unit_and_mechanic_cannot_borrow_a_valid_citation(self):
        for claim in (
            "猎首会使你免疫所有法术伤害[[E1]]。",
            "当你击败普通怪物时，会获得该怪物属性修正 60 秒[[E1]]。",
            "猎首提供60%的法术压制几率[[E1]]。",
        ):
            self.assertEqual(1, verify(claim, [evidence()]).blocked)
        self.assertEqual(0, verify("+(25-40) 最大生命[[E1]]。", [evidence()]).blocked)

    def test_selection_renders_source_text_not_model_claims(self):
        items = passages([evidence()])
        self.assertEqual(2, len(items))
        text, labels = render_selection('["P2"]', items)
        self.assertIn("稀有怪物", text)
        self.assertIn("60 秒", text)
        self.assertEqual(["E1"], labels)
        for raw in ('["P999"]', '{"answer":"免疫法术"}', '["P2", "免疫法术"]', '[]'):
            self.assertEqual(("", []), render_selection(raw, items))

    def test_table_selection_preserves_header_and_all_rows(self):
        items = passages([evidence("# 等级\n|等级|魔力|\n|---|---|\n|20|23|\n")])
        text, _ = render_selection('["P1"]', items)
        self.assertIn("|等级|魔力|", text)
        self.assertIn("|20|23|", text)

    def test_calculator_route_and_parameters_follow_requested_quantity(self):
        question = "5000生命、75%火焰抗性，没有其他减伤，最多能承受多大的单次火焰击中？"
        self.assertEqual("max_hit_calc", loop._preferred_tool_route(question))
        self.assertIsNone(loop._CODE_INVOKED_TOOLS["max_hit_calc"](question))
        self.assertEqual({"suppression_chance": 100}, loop._mitigation_args("法术压制达到100%后，会免疫法术伤害吗？"))

    def test_budget_rejects_oversized_context_before_network(self):
        client = LLMClient(Config({"llm": {"retry_delays": []}}), secrets())
        with patch.object(client, "_invoke") as invoke:
            for streaming in (False, True):
                with self.assertRaises(TokenBudgetExceeded):
                    result = (client.stream if streaming else client.chat)(
                        [{"role": "user", "content": "资料" * 5000}], budget=TurnBudget(4096))
                    if streaming:
                        list(result)
            invoke.assert_not_called()
        self.assertLess(len(build_system_prompt()), 2000)

    def test_output_budget_reserves_room_for_input(self):
        client = LLMClient(Config({"llm": {"retry_delays": []}}), secrets())
        messages = [{"role": "user", "content": "问题" * 500}]
        maximum = client._request_tokens(messages, [], 8192, TurnBudget(4096))
        self.assertGreaterEqual(maximum, 2048)
        self.assertLess(maximum, 4096)

    def test_poe2_boundary_precedes_build_clarification(self):
        question = "推荐一套流放之路2开荒BD"
        result = loop.TurnResult(trace_id="test", question=question, answer="")
        with patch.object(loop, "_record_gaps"), patch.object(loop.REGISTRY, "call") as call:
            setup = loop._prepare_turn(question, result)
        self.assertTrue(result.evidence_gap)
        self.assertIn("只覆盖", setup.gap)
        call.assert_not_called()

    def test_direct_entity_stream_keeps_answer_and_citations(self):
        answer = render_entity_answer(self.conn, "猎首是什么？", [evidence()])

        def prepare(question, result, **kwargs):
            result.citations = loop.cit.collect([evidence()], ["E1"])
            return loop._TurnSetup(final=answer)

        with patch.object(loop, "_prepare_turn", side_effect=prepare), patch.object(loop, "_persist"):
            batch = loop.run_turn("猎首是什么？")
            events = list(loop.stream_turn("猎首是什么？"))
        self.assertEqual(batch.answer, events[1]["text"])
        self.assertEqual(["E1"], events[1]["labels"])
        self.assertEqual("citations", events[2]["type"])
        self.assertFalse(events[-1]["tokens"].get("total"))

    def test_stream_and_batch_only_emit_selected_source_not_model_json(self):
        items = passages([evidence()])
        setup = loop._TurnSetup(evidence=[evidence()], passages=items)
        client = SimpleNamespace(
            chat=lambda *a, **kw: SimpleNamespace(model="test", content='["P2"]', tool_calls=[]),
            stream=lambda *a, **kw: (event for event in [StreamEvent(kind="content", text='["P2"]')]),
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(loop, "_prepare_turn", return_value=setup))
            stack.enter_context(patch.object(loop, "_persist"))
            stack.enter_context(patch.object(loop, "_domain_terms", return_value=()))
            batch = loop.run_turn("猎首效果", client=client)
            events = list(loop.stream_turn("猎首效果", client=client))
        emitted = "".join(e["text"] for e in events if e["type"] == "sentence")
        self.assertEqual(batch.answer, emitted)
        self.assertIn("稀有怪物", emitted)
        self.assertNotIn("P2", emitted)
        self.assertEqual(["meta", "sentence", "citations", "done"], [e["type"] for e in events])

    def test_selected_answer_replay_rejects_tampered_final_text(self):
        from scripts.eval_run import replay_case
        from tests.golden.loader import Case
        from tests.golden.recording import Recording
        items = passages([evidence()])
        answer, _ = render_selection('["P1"]', items)
        record = Recording(case_id="quality", query="隐式生命", raw_answer='["P1"]',
                           evidence=[evidence()], final_answer=answer, answer_mode="source_selection")
        with patch.object(record, "check_fresh"), patch.object(loop, "_domain_terms", return_value=()):
            case = Case(id="quality", query=record.query, kind="numeric", expect_values=("25-40",))
            self.assertTrue(replay_case(self.conn, case, record, {}).passed)
            record.final_answer = "猎首免疫法术伤害"
            self.assertFalse(replay_case(self.conn, case, record, {}).passed)

    def test_model_tool_result_switches_to_selection_without_leaking_draft(self):
        call = ToolCall("call", "gem_support_match", '{}')
        result = ToolResult(ok=True, citations=[{
            "chunk_id": "support", "title": "技能搭配", "source": "POEDB",
            "url": "https://poedb.tw/cn/Freezing_Pulse", "source_priority": 1,
            "text": "多重投射能辅助冰霜脉冲。", "channel": "structured",
        }])
        client = SimpleNamespace(
            chat=Mock(side_effect=[
                LLMResponse(content="待核实的草稿", tool_calls=(call,)),
                LLMResponse(content='["P3"]'),
            ]),
            stream=Mock(side_effect=[
                (event for event in [StreamEvent(kind="content", text="待核实的草稿"),
                                    StreamEvent(kind="tool_calls", tool_calls=(call,))]),
                (event for event in [StreamEvent(kind="content", text='["P3"]')]),
            ]),
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(loop, "_prepare_turn", side_effect=lambda *a, **k:
                loop._TurnSetup(question="技能搭配", evidence=[evidence()], needs_model_call=True)))
            stack.enter_context(patch.object(loop, "_persist"))
            stack.enter_context(patch.object(loop, "_domain_terms", return_value=()))
            stack.enter_context(patch.object(loop, "_call_tool", return_value=(result, False)))
            batch = loop.run_turn("技能搭配", client=client)
            events = list(loop.stream_turn("技能搭配", client=client))
        emitted = "".join(e["text"] for e in events if e["type"] == "sentence")
        self.assertEqual(batch.answer, emitted)
        self.assertIn("多重投射能辅助冰霜脉冲", emitted)
        self.assertNotIn("待核实", emitted)
        self.assertNotIn("P3", emitted)
        self.assertEqual("source_selection", batch.retrieval_meta["answer_mode"])

    def test_calculation_and_clarification_preserve_model_usage_without_second_rewrite(self):
        from calc import constants
        from tests.unit.test_maxhit import fixture_conn
        from tools.impl.maxhit import max_hit_calc

        conn = fixture_conn()
        self.addCleanup(conn.close)
        self.addCleanup(constants.reset_cache)
        constants.load(conn, force=True)
        usage = Usage(prompt_tokens=1200, completion_tokens=200, reasoning_tokens=80, total_tokens=1400)
        for args, expected in [
            ({"total_resist_pct": 99}, "20,000"),
            ({"resist_pct": 99}, "需要核对计算前提"),
            ({"total_resist_pct": 99, "unsupported_conditions": ["Mind over Matter"]}, "不能忽略"),
        ]:
            with self.subTest(args=args), ExitStack() as stack:
                call = ToolCall("maxhit", "max_hit_calc", json.dumps({"life": 5000, "damage_type": "火焰", **args}))
                tool_result = max_hit_calc(**json.loads(call.arguments))

                def chat(*a, budget, **kw):
                    budget.add(usage)
                    return LLMResponse(content="未经计算的草稿500000", tool_calls=(call,), model="test", usage=usage)

                def stream(*a, budget, **kw):
                    budget.add(usage)
                    yield StreamEvent(kind="content", text="未经计算的草稿500000")
                    yield StreamEvent(kind="tool_calls", tool_calls=(call,))
                    yield StreamEvent(kind="usage", usage=usage, model="test")

                client = SimpleNamespace(chat=Mock(side_effect=chat), stream=Mock(side_effect=stream))
                stack.enter_context(patch.object(loop, "_prepare_turn", side_effect=lambda *a, **k:
                    loop._TurnSetup(question="承伤问题", preferred_tool="max_hit_calc",
                                    evidence=[evidence()], needs_model_call=True)))
                stack.enter_context(patch.object(loop, "_persist"))
                stack.enter_context(patch.object(loop, "_domain_terms", return_value=()))
                stack.enter_context(patch.object(loop, "_call_tool", return_value=(tool_result, False)))
                batch = loop.run_turn("承伤问题", client=client)
                events = list(loop.stream_turn("承伤问题", client=client))
                emitted = "".join(e["text"] for e in events if e["type"] == "sentence")
                self.assertEqual(batch.answer, emitted)
                self.assertIn(expected, emitted)
                self.assertNotIn("500000", emitted)
                self.assertNotIn("猎首", emitted)
                self.assertEqual(usage.as_dict(), batch.tokens)
                self.assertEqual(batch.tokens, events[-1]["tokens"])
                self.assertEqual(1, client.chat.call_count)
                self.assertEqual(1, client.stream.call_count)
                self.assertEqual(1, len(batch.citations))


if __name__ == "__main__":
    unittest.main()
