"""Question-level regressions: intent, conditions, evidence, tools and output parity."""
from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.llm import LLMResponse, StreamEvent, ToolCall, Usage
from orchestrator import loop, tool_selection
from orchestrator.answer import compose, schema
from orchestrator.binding import Evidence
from orchestrator.excerpts import source_quotes
from orchestrator.question import conversation_context
from tools.registry import ToolResult


def evidence(label="E1", text="当你击败稀有怪物时，会获得该怪物属性修正 60 秒", scope="excerpt"):
    return Evidence(label, f"chunk:{label}", "unique_item:Headhunter", "猎首", "https://poedb.tw/cn/Headhunter", "POEDB", 1, text, claim_scope=scope)


def call(name, args):
    return ToolCall(name, name, json.dumps(args, ensure_ascii=False))


class AnswerContractTest(unittest.TestCase):
    def test_answer_schema_only_offers_supported_operations_and_current_ids(self):
        def fields(items):
            return schema(items)["function"]["parameters"]["properties"]["blocks"]["items"]["properties"]
        self.assertNotIn("quote", fields([]))
        self.assertNotIn("text", fields([evidence()]))
        self.assertNotIn("citations", fields([evidence()]))
        self.assertEqual(["Q1.1"], fields([evidence()])["quote"]["enum"])
        general = fields([evidence(scope="general")])
        self.assertIn("text", general)
        self.assertEqual(["E1"], general["citations"]["items"]["enum"])
        self.assertNotIn("quote", general)

    def test_clarification_cannot_smuggle_unverified_numeric_examples(self):
        for ask in ("Mind over Matter分流比例（例如常见40%）", "最大火抗是否改为90%", "字段" * 70):
            with self.subTest(ask=ask), self.assertRaises(ValueError):
                compose({"status": "clarification", "blocks": [{"ask": ask}]}, [])
        raw, checked = compose({"status": "clarification", "blocks": [{"ask": "实际生效的最大火焰抗性"}]}, [])
        self.assertIn("最大火焰抗性", raw)
        self.assertEqual(0, checked.blocked)

    def test_complete_fact_is_restored_with_its_subject_and_citation(self):
        raw, result = compose({"status": "answer", "blocks": [{"quote": "Q1.1"}]}, [evidence()])
        self.assertIn("猎首", raw)
        self.assertIn("稀有怪物", raw)
        self.assertEqual(0, result.blocked)
        self.assertEqual(["E1"], result.used_labels)

    def test_quote_ids_do_not_shift_when_evidence_is_added(self):
        first = source_quotes([evidence()])
        second = source_quotes([evidence(), evidence("E2", "+(25-40) 最大生命")])
        self.assertEqual(first["Q1.1"], second["Q1.1"])

    def test_full_table_keeps_header_and_rows(self):
        e = evidence(text="|等级|消耗|\n|---|---|\n|20|23|\n|21|24|")
        raw, result = compose({"status": "answer", "blocks": [{"quote": "Q1.1"}]}, [e])
        self.assertIn("|等级|消耗|", raw)
        self.assertIn("|21|24|", raw)
        self.assertEqual(0, result.blocked)

    def test_quote_keeps_section_conditions_with_the_field(self):
        e = evidence(text="## 被击中时\n获得10点生命\n## 击败怪物时\n获得20点生命")
        raw, result = compose({"status": "answer", "blocks": [{"quote": "Q1.2"}]}, [e])
        self.assertIn("击败怪物时", raw)
        self.assertNotIn("被击中时", raw)
        self.assertIn("20点生命", raw)
        self.assertEqual(0, result.blocked)

    def test_missing_quote_or_citation_is_rejected(self):
        for block in ({"quote": "Q9.1"}, {"text": "60秒", "citations": ["E9"]}):
            with self.subTest(block=block), self.assertRaises(ValueError):
                compose({"status": "answer", "blocks": [block]}, [evidence()])

    def test_changed_trigger_unit_and_unsupported_number_stay_blocked(self):
        for text in ("当你击败普通怪物时，会获得该怪物属性修正 60 秒", "法术伤害压制率提高60%", "效果持续600秒"):
            _, result = compose({"status": "answer", "blocks": [{"text": text, "citations": ["E1"]}]}, [evidence()])
            self.assertGreater(result.blocked, 0)

    def test_citations_cover_each_sentence_without_relaxing_numeric_check(self):
        e = evidence(text="基础暴击率5%，增加100%，最终暴击率10%。", scope="calculation")
        _, result = compose({"status": "answer", "blocks": [{"text": "最终暴击率10%。基础5%，增加100%。", "citations": ["E1"]}]}, [e])
        self.assertEqual(0, result.blocked)
        _, invalid = compose({"status": "answer", "blocks": [{"text": "最终暴击率90%。", "citations": ["E1"]}]}, [e])
        self.assertEqual(1, invalid.blocked)

    def test_empty_gap_has_a_finite_answer_without_inventing_nonexistence(self):
        raw, binding = compose({"status": "gap", "blocks": []}, [])
        self.assertIn("未找到", raw)
        self.assertNotIn("不存在", raw)
        self.assertEqual(0, binding.blocked)
        with self.assertRaises(ValueError):
            compose({"status": "answer", "blocks": []}, [])


class QuestionPreparationTest(unittest.TestCase):
    def test_terminology_candidates_preserve_the_full_question_and_need_model_review(self):
        question = "不要偷取词缀的腰带，要生命偷取的腰带"
        search = ToolResult(ok=True, data={"sufficient": True}, citations=[{
            "chunk_id": "item", "title": "候选", "text": "候选原文", "source": "POEDB",
            "url": "https://poedb.tw/cn/Headhunter", "claim_scope": "excerpt"}])
        with patch.object(loop.REGISTRY, "call", return_value=search) as invoke:
            result = loop.TurnResult("test", question, "")
            setup = loop._prepare_turn(question, result)
        self.assertEqual({"query": question}, json.loads(invoke.call_args.args[1]))
        self.assertEqual("", result.answer)
        self.assertFalse(setup.done)
        self.assertIn(question, setup.messages[1]["content"])
        self.assertIn("候选原文", setup.messages[1]["content"])
        self.assertEqual("excerpt", setup.evidence[0].claim_scope)

    def test_failed_terminology_search_still_allows_model_tools(self):
        with patch.object(loop.REGISTRY, "call", return_value=ToolResult(ok=False, error="unavailable")):
            result = loop.TurnResult("test", "偷取词缀的珠宝", "")
            setup = loop._prepare_turn(result.question, result)
        self.assertFalse(setup.done)
        self.assertEqual([], setup.evidence)
        self.assertIn("尚未提供事实依据", setup.messages[1]["content"])

    def test_initial_retrieval_is_a_candidate_list_not_an_automatic_answer(self):
        search = ToolResult(ok=True, data={"sufficient": False}, citations=[{"title": "候选装备", "entity_id": "candidate", "text": "候选不是结论"}])
        for question in ("不是问猎首，法血有哪些词缀？", "5%基础暴击，增加100%，最终暴击率多少？", "词缀怎么偷取？"):
            with self.subTest(question=question), patch.object(loop.REGISTRY, "call", return_value=search) as invoke, patch.object(loop, "_apply_dimension_gate", side_effect=lambda q, d, **kw: (d, True)):
                result = loop.TurnResult("test", question, "")
                setup = loop._prepare_turn(question, result)
            self.assertFalse(setup.done)
            is_calculation = "暴击" in question
            self.assertEqual(0 if is_calculation else 1, invoke.call_count)
            self.assertEqual(question, setup.question)
            self.assertEqual(0 if is_calculation else 1, len(setup.evidence))
            self.assertIn(question, setup.messages[1]["content"])
            self.assertIn("尚未提供事实依据" if is_calculation else "已按原问题检索候选", setup.messages[1]["content"])
            self.assertIn("select_tools", [s["function"]["name"] for s in setup.tools])

    def test_tool_selection_is_per_turn_and_limited_to_allowed_tools(self):
        a = loop._ToolLoopState.start(loop._TurnSetup(tools=tool_selection.schemas(["kb_search"])), "两个问题")
        b = loop._ToolLoopState.start(loop._TurnSetup(tools=tool_selection.schemas(["kb_search"])), "另一个会话")
        result = loop.TurnResult("test", "两个问题", "")
        loop._handle_tool_call(a, result, call("select_tools", {"names": ["crit_calc", "recovery_calc"]}))
        self.assertIn("crit_calc", [s["function"]["name"] for s in a.tools])
        self.assertNotIn("crit_calc", [s["function"]["name"] for s in b.tools])
        self.assertFalse(tool_selection.select({"names": ["rebuild_index"]}).ok)
        with patch.object(loop.REGISTRY, "call") as registry:
            self.assertFalse(loop._call_tool("任意问题", "", "rebuild_index", "{}")[0].ok)
        registry.assert_not_called()

    def test_owned_context_preserves_numeric_conditions_and_current_override(self):
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE conversation(conversation_id,owner_id);CREATE TABLE message(conversation_id,role,content,created_at);INSERT INTO conversation VALUES('chat','alice');")
        for index, text in enumerate(("5000生命，总火抗99%，无其他减伤", "最大火抗改为90%", "那有25%穿透呢？")):
            conn.execute("INSERT INTO message VALUES('chat','user',?,?)", (text, str(index)))
        text = conversation_context(conn, "那有25%穿透呢？", "chat", "alice")
        self.assertIn("5000", text); self.assertIn("99%", text); self.assertIn("90%", text)
        self.assertNotIn("25%", text)
        self.assertEqual("", conversation_context(conn, "追问", "chat", "bob"))

    def test_followup_is_understood_with_history_before_search_rewriting(self):
        with patch.object(loop, "conversation_context", return_value="上轮用户问猎首词缀"), \
             patch.object(loop.dbmod, "get_conn"), \
             patch.object(loop, "build_prompt_memory_context", return_value=SimpleNamespace(text="", count=0, truncated=False)), \
             patch.object(loop.REGISTRY, "call") as invoke:
            result = loop.TurnResult("test", "它怎么获得？", "")
            setup = loop._prepare_turn(result.question, result, owner_id="alice", conversation_id="chat")
        invoke.assert_not_called()
        self.assertIn("上轮用户问猎首词缀", setup.messages[1]["content"])
        self.assertIn("它怎么获得", setup.messages[1]["content"])
        self.assertFalse(setup.done)


class ModelLoopTest(unittest.TestCase):
    def run_both(self, rounds, results):
        usage = Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120)
        def clients():
            batch, stream = iter(rounds), iter(rounds)
            def chat(*args, budget, **kwargs):
                budget.add(usage)
                return LLMResponse(content="不应外发的草稿", tool_calls=tuple(next(batch)), model="test")
            def streaming(*args, budget, **kwargs):
                budget.add(usage)
                yield StreamEvent(kind="content", text="不应外发的草稿")
                yield StreamEvent(kind="tool_calls", tool_calls=tuple(next(stream)))
                yield StreamEvent(kind="usage", usage=usage, model="test")
            return SimpleNamespace(chat=chat, stream=streaming)
        def invoke(q, p, name, raw):
            return results[name], False
        with ExitStack() as stack:
            stack.enter_context(patch.object(loop, "_prepare_turn", side_effect=lambda *a, **kw: loop._TurnSetup(question="两个问题", tools=tool_selection.schemas(["entity_lookup", "crit_calc"]), needs_model_call=True)))
            stack.enter_context(patch.object(loop, "_persist"))
            stack.enter_context(patch.object(loop, "_domain_terms", return_value=()))
            stack.enter_context(patch.object(loop, "_record_gaps"))
            stack.enter_context(patch.object(loop, "_call_tool", side_effect=invoke))
            client = clients()
            batch = loop.run_turn("两个问题", client=client)
            events = list(loop.stream_turn("两个问题", client=client))
        text = "".join(e["text"] for e in events if e["type"] in ("sentence", "gap", "note"))
        self.assertEqual(batch.answer, text)
        self.assertNotIn("草稿", text)
        self.assertEqual(len(rounds) * 120, batch.tokens["total"])
        self.assertEqual(batch.tokens, events[-1]["tokens"])
        return batch, events

    def test_one_tool_success_does_not_finish_a_compound_question(self):
        result = ToolResult(ok=True, data={"kind": "result"}, citations=[{"chunk_id": "calc", "text": "最终暴击率10%。", "source": "Path of Building", "url": "https://example.org/calc", "source_priority": 2, "channel": "derived"}])
        other = ToolResult(ok=True, citations=[{"chunk_id": "item", "title": "猎首", "text": "+(25-40) 最大生命", "source": "POEDB", "url": "https://poedb.tw/cn/Headhunter", "source_priority": 1, "claim_scope": "excerpt"}])
        batch, _ = self.run_both([
            [call("crit_calc", {"base_chance": 5, "increased_pct": 100})],
            [call("entity_lookup", {"name": "猎首"})],
            [call("finish_answer", {"status": "answer", "blocks": [{"text": "最终暴击率10%。", "citations": ["E1"]}, {"quote": "Q2.1"}]})],
        ], {"crit_calc": result, "entity_lookup": other})
        self.assertIn("10%", batch.answer); self.assertIn("25-40", batch.answer)
        self.assertEqual(2, len(batch.citations))

    def test_rejected_draft_is_repaired_before_any_output(self):
        result = ToolResult(ok=True, citations=[{"chunk_id": "item", "text": "效果持续60秒", "source": "POEDB", "url": "https://poedb.tw/cn/Headhunter", "source_priority": 1, "claim_scope": "excerpt"}])
        batch, _ = self.run_both([
            [call("entity_lookup", {"name": "猎首"})],
            [call("finish_answer", {"status": "answer", "blocks": [{"text": "效果持续600秒", "citations": ["E1"]}]})],
            [call("finish_answer", {"status": "answer", "blocks": [{"quote": "Q1.1"}]})],
        ], {"entity_lookup": result})
        self.assertNotIn("600", batch.answer)
        self.assertIn("60秒", batch.answer)
        self.assertFalse(batch.tool_calls[1]["ok"])



class ToolBoundaryTest(unittest.TestCase):
    def test_selected_schemas_do_not_modify_registered_parameters(self):
        from copy import deepcopy
        from tools.registry import REGISTRY
        original = deepcopy(REGISTRY.spec("crit_calc").parameters)
        tools = tool_selection.schemas(["crit_calc"])
        properties = tools[0]["function"]["parameters"]["properties"]
        self.assertIn("unsupported_conditions", properties)
        properties["base_chance"]["description"] = "altered"
        self.assertEqual(original, REGISTRY.spec("crit_calc").parameters)

    def test_unrepresented_conditions_never_reach_the_calculator(self):
        state = loop._ToolLoopState.start(loop._TurnSetup(), "护甲击中，敌人无视10%物理减伤")
        result = loop.TurnResult("test", state.question, "")
        with patch.object(loop.REGISTRY, "call") as invoke:
            loop._handle_tool_call(state, result, call("mitigation_calc", {
                "armour": 10000, "hit_damage": 5000,
                "unsupported_conditions": ["敌人无视10%物理减伤"],
            }))
            loop._handle_tool_call(state, result, call("mitigation_calc", {"armour": 10000, "hit_damage": 5000}))
            loop._handle_tool_call(state, result, call("max_hit_calc", {"pool": 5000}))
            invoke.assert_not_called()
        loop._handle_tool_call(state, result, call("finish_answer", {
            "status": "clarification", "blocks": [{"report": "mitigation_calc"}],
        }))
        self.assertIn("10%", result.answer)
        self.assertIn("未执行计算", result.answer)
        self.assertEqual(0, result.blocked)

    def test_untrusted_reports_and_non_object_finish_are_rejected(self):
        with self.assertRaises(ValueError):
            compose({"status": "answer", "blocks": [{"report": "invented"}]}, [])
        state = loop._ToolLoopState.start(loop._TurnSetup(), "问答")
        result = loop.TurnResult("test", state.question, "")
        loop._handle_tool_call(state, result, call("finish_answer", ["invalid"]))
        self.assertFalse(result.tool_calls[-1]["ok"])
        self.assertEqual("", result.answer)

    def test_quote_preserves_embedded_punctuation_without_losing_citation(self):
        source = evidence(text='• Lantern Explosion - "Incineration!" - deals Fire damage.\nSecond sentence!')
        raw, checked = compose({"status": "answer", "blocks": [{"quote": "Q1.1"}]}, [source])
        self.assertIn('"Incineration!"', raw)
        self.assertEqual(0, checked.blocked)
        self.assertEqual(["E1"], checked.used_labels)

    def test_untrusted_source_cannot_be_selected_as_quote(self):
        from dataclasses import replace
        source = replace(evidence(), can_support_facts=False, source_priority=7)
        with self.assertRaises(ValueError):
            compose({"status": "answer", "blocks": [{"quote": "Q1.1"}]}, [source])

    def test_bad_prose_requests_repair_instead_of_partial_output(self):
        state = loop._ToolLoopState.start(loop._TurnSetup(evidence=[evidence()]), "问答")
        self.assertIn("finish_answer", loop._draft_feedback(state, "效果持续600秒[[E1]]。"))
        self.assertEqual("", loop._draft_feedback(state, "当你击败稀有怪物时，会获得该怪物属性修正 60 秒[[E1]]"))

    def test_model_tool_limit_is_an_explicit_failure(self):
        tc = call("select_tools", {"purpose": "读取实体", "names": ["entity_lookup"]})
        client = SimpleNamespace(chat=lambda *a, **kw: LLMResponse(content="", tool_calls=(tc,), model="test"))
        config = SimpleNamespace(get=lambda key, default=None: 1 if key == "llm.max_tool_calls" else default)
        with patch.object(loop, "get_config", return_value=config), patch.object(loop, "_persist"):
            result = loop.run_turn("猎首是什么？", client=client)
        self.assertTrue(result.evidence_gap)
        self.assertEqual("tool_limit", result.retrieval_meta["generation_error"])
        self.assertIn("调用上限", result.answer)

    def test_missing_alias_does_not_claim_entity_absence(self):
        from tools.impl.kb_search import entity_lookup
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        with patch("tools.impl.kb_search.dbmod.get_conn", return_value=conn), patch("retrieval.alias.lookup", return_value=[]):
            result = entity_lookup("未收录的俗称")
        self.assertFalse(result.data["found"])
        self.assertIn("不代表", result.data["reason"])

    def test_entity_field_endpoint_retains_provenance(self):
        from tools.impl.kb_search import entity_lookup, kb_search
        from retrieval.fts import Hit
        from retrieval.hybrid import RetrievalResult
        from dataclasses import replace
        conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript("CREATE TABLE entity(entity_id,name_zh,name_en,base_type,attributes,url,source,source_priority);CREATE TABLE chunk(chunk_id,entity_id,text);")
        attrs = {"mods": [{"kind": "implicit", "text": "+(25-40) 最大生命", "ranges": [[25, 40]]}]}
        conn.execute("INSERT INTO entity VALUES(?,?,?,?,?,?,?,?)", ("unique_item:Headhunter", "猎首", "Headhunter", "皮革腰带", json.dumps(attrs), "https://poedb.tw/cn/Headhunter", "POEDB", 1))
        conn.execute("INSERT INTO chunk VALUES('item','unique_item:Headhunter','+(25-40) 最大生命')")
        with patch("tools.impl.kb_search.dbmod.get_conn", return_value=conn), patch("retrieval.alias.lookup", return_value=["unique_item:Headhunter"]):
            result = entity_lookup("猎首", "猎首的隐式词缀最多加多少生命？")
        self.assertIn("**40**", result.citations[0]["text"])
        self.assertEqual("POEDB", result.citations[0]["source"])
        self.assertIn(":field:", result.citations[0]["chunk_id"])
        self.assertNotIn("[[E1]]", result.citations[0]["text"])
        hit = Hit("item", "unique_item:Headhunter", "+(25-40) 最大生命",
                  {"title": "猎首", "url": "https://poedb.tw/cn/Headhunter"},
                  1, 0, "alias", "POEDB", 1)
        query = "猎首的隐式词缀最多加多少生命？"
        retrieval = RetrievalResult(query=query, hits=[hit], sufficient=True)
        with patch("tools.impl.kb_search.dbmod.get_conn", return_value=conn), \
             patch("tools.impl.kb_search.configured_retrieve", return_value=retrieval):
            found = kb_search(query)
            self.assertEqual(result.citations[0]["text"], found.citations[0]["text"])
            self.assertEqual(result.citations[0]["chunk_id"], found.citations[0]["chunk_id"])
            retrieval.hits.append(replace(hit, chunk_id="other", entity_id="unique_item:Mageblood"))
            ambiguous = kb_search(query)
            self.assertTrue(all(":field:" not in c["chunk_id"] for c in ambiguous.citations))
        from orchestrator.binding import number_evidence, verify
        self.assertGreater(verify("猎首使你免疫所有伤害[[E1]]", number_evidence(result.citations)).blocked, 0)

    def test_calculator_numbers_do_not_license_reversed_semantics(self):
        from orchestrator.binding import verify
        state = loop._ToolLoopState.start(loop._TurnSetup(), "99%总抗性，5000生命")
        result = loop.TurnResult("test", state.question, "")
        tr = ToolResult(ok=True, citations=[{
            "chunk_id": "calc", "title": "承伤计算", "text": "总抗性99%，最大抗性75%，实际生效抗性75%。",
            "source": "Path of Building", "url": "https://example.org/calc", "channel": "derived",
        }])
        with patch.object(loop, "_call_tool", return_value=(tr, False)):
            loop._handle_tool_call(state, result, call("max_hit_calc", {}))
        self.assertGreater(verify("因此实际生效抗性99%[[E1]]", state.evidence).blocked, 0)
        _, checked = compose({"status": "answer", "blocks": [{"quote": "Q1.1"}]}, state.evidence)
        self.assertEqual(0, checked.blocked)

    def test_unused_tool_boundaries_do_not_pollute_the_answer(self):
        from orchestrator.citation import collect
        e = evidence()
        result = loop.TurnResult("test", "猎首效果", "已引用的边界")
        result.citations = collect([e], [e.label])
        result.tool_calls = [
            {"tool": "entity_lookup", "evidence_ids": [e.chunk_id], "data": {"boundary": "已引用的边界"}},
            {"tool": "acquisition_routes", "evidence_ids": [], "data": {"boundary": "无关工具的范围声明"}},
        ]
        self.assertEqual([], loop._apply_notes(result, [e]))
        self.assertEqual("已引用的边界", result.answer)

    def test_selected_numeric_result_still_gets_its_tool_boundary(self):
        from orchestrator.citation import collect
        e = evidence()
        result = loop.TurnResult("test", "承伤计算", "临界值20000")
        result.citations = collect([e], [e.label])
        result.tool_calls = [{"tool": "max_hit_calc", "evidence_ids": [e.chunk_id],
                              "data": {"boundary": "达到临界值会耗尽池子，不保证存活"}}]
        loop._apply_notes(result, [e])
        self.assertIn("不保证存活", result.answer)

    def test_selected_but_unexecuted_tool_cannot_be_replaced_by_excerpt(self):
        state = loop._ToolLoopState.start(loop._TurnSetup(evidence=[evidence()]), "问题")
        state.planned_tools.add("mitigation_calc")
        result = loop.TurnResult("test", state.question, "")
        loop._handle_tool_call(state, result, call("finish_answer", {"status": "answer", "blocks": [{"quote": "Q1.1"}]}))
        self.assertFalse(result.tool_calls[-1]["ok"])
        self.assertIn("mitigation_calc", result.tool_calls[-1]["error"])
        self.assertEqual("", result.answer)

    def test_specialized_scope_cannot_bypass_validation_with_quote_id(self):
        from dataclasses import replace
        source = replace(evidence(), claim_scope="metamod_matrix")
        self.assertEqual({}, source_quotes([source]))
        with self.assertRaises(ValueError):
            compose({"status": "answer", "blocks": [{"quote": "Q1.1"}]}, [source])

    def test_grounded_recording_replays_the_same_composer(self):
        from scripts.eval_run import replay_case
        from tests.golden.loader import Case
        from tests.golden.recording import Recording
        args = {"status": "answer", "blocks": [{"quote": "Q1.1"}]}
        raw, binding = compose(args, [evidence()])
        record = Recording(case_id="grounded", query="效果？", raw_answer=raw,
                           evidence=[evidence()], final_answer=binding.public_text,
                           answer_mode="grounded_blocks", answer_payload={"arguments": args})
        restored = Recording.from_dict(record.to_dict())
        self.assertEqual(record.answer_payload, restored.answer_payload)
        case = Case(id="grounded", query=record.query, kind="numeric", expect_values=("60",))
        with patch.object(record, "check_fresh"), patch.object(loop, "_domain_terms", return_value=()):
            self.assertTrue(replay_case(None, case, record, {}).passed)
            record.final_answer = "效果持续600秒"
            self.assertFalse(replay_case(None, case, record, {}).passed)

    def test_clarification_asks_for_inputs_without_claiming_game_facts(self):
        raw, result = compose({"status": "clarification", "blocks": [{"ask": "当前赛季、职业/升华、等级和目标内容"}]}, [])
        self.assertIn("请补充或确认", raw)
        self.assertEqual(0, result.blocked)
        self.assertEqual([], result.used_labels)
        with self.assertRaises(ValueError):
            compose({"status": "answer", "blocks": [{"ask": "等级"}]}, [])

    def test_retrieval_gap_does_not_license_nonexistence_claim(self):
        from orchestrator.binding import verify
        self.assertGreater(verify("检索未命中，因此该装备不存在。", []).blocked, 0)
        self.assertEqual(0, verify("知识库未收录该装备的资料。", []).blocked)


if __name__ == "__main__":
    unittest.main()
