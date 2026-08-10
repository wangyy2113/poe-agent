"""制作组合精确路由的编排层离线测试。"""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.llm import LLMResponse, StreamEvent, ToolCall  # noqa: E402
from orchestrator import loop  # noqa: E402
from tools.registry import ToolResult  # noqa: E402


QUESTION = "项链上的最大生命和火焰抗性能否共存？"
TOOL_CALL = ToolCall(
    id="craft-1",
    name="check_mod_combination",
    arguments=json.dumps(
        {
            "item_class": "项链",
            "targets": [{"query": "最大生命"}, {"query": "火焰抗性"}],
        },
        ensure_ascii=False,
    ),
)
WRONG_TOOL_CALL = ToolCall(
    id="craft-wrong",
    name="check_mod_combination",
    arguments=json.dumps(
        {
            "item_class": "弓",
            "targets": [{"query": "物理伤害"}, {"query": "攻击速度"}],
        },
        ensure_ascii=False,
    ),
)
SECOND_TOOL_CALL = ToolCall(
    id="craft-2",
    name="check_mod_combination",
    arguments=TOOL_CALL.arguments,
)


class _FakeConfig:
    _VALUES = {
        "llm.max_tokens_per_turn": 1000,
        "llm.max_tool_calls": 2,
        "retrieval.factual_terms": [],
    }

    def get(self, key, default=None):
        return self._VALUES.get(key, default)


class _ChatClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = iter(responses)
        self.messages: list[list[dict]] = []

    def chat(self, messages, **_kwargs):
        self.messages.append([dict(message) for message in messages])
        return next(self._responses)


class _StreamClient:
    def __init__(self, rounds: list[list[StreamEvent]]) -> None:
        self._rounds = iter(rounds)

    def stream(self, _messages, **_kwargs):
        yield from next(self._rounds)


def _kb_result(*, sufficient: bool = False) -> ToolResult:
    return ToolResult(
        ok=True,
        data={
            "sufficient": sufficient,
            "reason": "BM25 依据不足",
            "missing_terms": ["词缀组合"],
        },
        citations=[
            {
                "chunk_id": "weak-hit",
                "entity_id": None,
                "title": "首轮伪证据",
                "url": "https://poedb.tw/cn/Weak",
                "source": "POEDB",
                "source_priority": 1,
                "can_support_facts": True,
                "text": "该命中不足以证明组合结论。",
            }
        ],
        meta={"channels": ["bm25"], "degraded": False},
    )


def _craft_result(*, gaps=None) -> ToolResult:
    return ToolResult(
        ok=True,
        data={"verdict": "unknown", "gaps": list(gaps or [])},
        citations=[
            {
                "chunk_id": "craft_solver:Amulets:1",
                "entity_id": None,
                "title": "项链词缀组合检查",
                "url": "https://poedb.tw/cn/Modifiers",
                "source": "POE1-Agent 确定性求解器（结构化输入：POEDB）",
                "source_priority": 1,
                "can_support_facts": True,
                "text": "页面结论为 unknown。",
                "channel": "derived",
            }
        ],
        meta={"channel": "structured"},
    )


class TestPrecisionRoute(unittest.TestCase):
    def _patches(self, registry_call):
        stack = ExitStack()
        stack.enter_context(patch("orchestrator.loop.get_config", return_value=_FakeConfig()))
        stack.enter_context(patch("orchestrator.loop.build_system_prompt", return_value="system"))
        stack.enter_context(patch("orchestrator.loop._prepare_tools", return_value=[]))
        stack.enter_context(patch("orchestrator.loop._domain_terms", return_value=()))
        stack.enter_context(patch("orchestrator.loop._persist"))
        stack.enter_context(patch("orchestrator.loop._record_gaps"))
        stack.enter_context(patch("orchestrator.loop.REGISTRY.call", side_effect=registry_call))
        return stack

    def test_组合问法同义词命中且否定元问题不误路由(self):
        routed = (
            QUESTION,
            "项链上的最大生命和火焰抗性会冲突吗？",
            "项链上的最大生命和火焰抗性互斥吗？",
            "项链上的最大生命和火焰抗性能一起出现吗？",
            "项链能同时有最大生命和火焰抗性吗？",
            "项链上能同时存在最大生命和火焰抗性吗？",
            "项链上能一起出现最大生命和火焰抗性吗？",
        )
        for question in routed:
            with self.subTest(question=question):
                self.assertEqual(
                    loop._precision_tool_route(question), "check_mod_combination"
                )
        self.assertEqual(loop._precision_tool_route("最大生命词缀有多少档？"), "")
        self.assertEqual(
            loop._precision_tool_route("不要判断能否共存，只解释词缀组"), ""
        )
        self.assertEqual(loop._precision_tool_route("共存是什么意思？"), "")
        self.assertEqual(loop._precision_tool_route("请勿判断这两个词缀是否冲突"), "")
        self.assertEqual(
            loop._precision_tool_route(
                "无需帮我判断项链上的最大生命和火焰抗性能否共存？"
            ),
            "",
        )
        self.assertEqual(
            loop._precision_tool_route(
                "不必判断项链上的最大生命和火焰抗性能否共存？"
            ),
            "",
        )
        self.assertEqual(
            loop._precision_tool_route(
                "为什么要检查项链上的最大生命和火焰抗性能否共存？"
            ),
            "",
        )
        colon_redirected = (
            "共存是什么意思：真正想问项链上的最大生命和火焰抗性能否共存？"
        )
        self.assertEqual(
            loop._precision_tool_route(colon_redirected), "check_mod_combination"
        )
        self.assertEqual(
            loop._validate_precision_call(
                colon_redirected,
                "check_mod_combination",
                json.loads(TOOL_CALL.arguments),
            ),
            "",
        )
        redirected = (
            "我不是问共存是什么意思，我是问项链上的最大生命和火焰抗性是否冲突？"
        )
        self.assertEqual(
            loop._precision_tool_route(redirected), "check_mod_combination"
        )
        self.assertEqual(
            loop._validate_precision_call(
                redirected, "check_mod_combination", json.loads(TOOL_CALL.arguments)
            ),
            "",
        )
        interference = (
            "先对比弓的物理伤害和攻击速度，再判断项链的最大生命和火焰抗性能否共存？"
        )
        self.assertIn(
            "item_class",
            loop._validate_precision_call(
                interference,
                "check_mod_combination",
                json.loads(WRONG_TOOL_CALL.arguments),
            ),
        )
        three_targets = "项链上的最大生命、火焰抗性和最大魔力能否共存？"
        self.assertIn(
            "全部目标",
            loop._validate_precision_call(
                three_targets, "check_mod_combination", json.loads(TOOL_CALL.arguments)
            ),
        )
        broad = {
            "item_class": "项链",
            "targets": [{"query": "生命"}, {"query": "抗性"}],
        }
        self.assertIn(
            "完整",
            loop._validate_precision_call(QUESTION, "check_mod_combination", broad),
        )

        background_rarity = (
            "先看稀有弓的背景数据，但请判断项链上的最大生命和火焰抗性能否共存？"
        )
        with_rarity = json.loads(TOOL_CALL.arguments)
        with_rarity["rarity"] = "稀有"
        self.assertIn(
            "rarity",
            loop._validate_precision_call(
                background_rarity, "check_mod_combination", with_rarity
            ),
        )
        explicit_rarity = "稀有项链上的最大生命和火焰抗性能否共存？"
        self.assertEqual(
            loop._validate_precision_call(
                explicit_rarity, "check_mod_combination", with_rarity
            ),
            "",
        )

        sourced_question = "项链上的精华最大生命和火焰抗性能否共存？"
        wrong_source = {
            "item_class": "项链",
            "targets": [
                {"query": "最大生命"},
                {"query": "火焰抗性", "source": "精华"},
            ],
        }
        self.assertIn(
            "target.source",
            loop._validate_precision_call(
                sourced_question, "check_mod_combination", wrong_source
            ),
        )
        correct_source = {
            "item_class": "项链",
            "targets": [
                {"query": "最大生命", "source": "精华"},
                {"query": "火焰抗性"},
            ],
        }
        self.assertEqual(
            loop._validate_precision_call(
                sourced_question, "check_mod_combination", correct_source
            ),
            "",
        )

    def test_批量路径在kb不足时仍调用精确工具(self):
        calls: list[str] = []

        def registry_call(name, _arguments):
            calls.append(name)
            return _kb_result() if name == "kb_search" else _craft_result()

        client = _ChatClient(
            [
                LLMResponse(content="", tool_calls=(TOOL_CALL,), model="fake"),
                LLMResponse(content="页面结论为 unknown[[E1]]。", model="fake"),
            ]
        )
        with self._patches(registry_call):
            result = loop.run_turn(QUESTION, client=client)

        self.assertEqual(calls, ["kb_search", "check_mod_combination"])
        self.assertFalse(result.evidence_gap)
        self.assertEqual(result.retrieval_meta["precision_route"], "check_mod_combination")
        self.assertEqual([e.chunk_id for e in result.evidence], ["craft_solver:Amulets:1"])
        self.assertEqual([citation.label for citation in result.citations], ["E1"])
        self.assertIn("unknown", result.answer)
        first_prompt = client.messages[0][1]["content"]
        self.assertIn("回答前必须先调用该工具", first_prompt)
        self.assertNotIn("首轮伪证据", first_prompt)

    def test_谓词前置问法在批量和流式路径均可通过(self):
        questions = (
            "项链上能同时存在最大生命和火焰抗性吗？",
            "项链上能一起出现最大生命和火焰抗性吗？",
        )
        arguments = json.loads(TOOL_CALL.arguments)
        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(
                    loop._validate_precision_call(
                        question, "check_mod_combination", arguments
                    ),
                    "",
                )

        def registry_call(name, _arguments):
            return _kb_result() if name == "kb_search" else _craft_result()

        batch_client = _ChatClient(
            [
                LLMResponse(content="", tool_calls=(TOOL_CALL,), model="fake"),
                LLMResponse(content="页面结论为 unknown[[E1]]。", model="fake"),
            ]
        )
        with self._patches(registry_call):
            batch_result = loop.run_turn(questions[0], client=batch_client)
        self.assertFalse(batch_result.evidence_gap)
        self.assertEqual(
            [e.chunk_id for e in batch_result.evidence],
            ["craft_solver:Amulets:1"],
        )

        stream_client = _StreamClient(
            [
                [StreamEvent(kind="tool_calls", tool_calls=(TOOL_CALL,))],
                [StreamEvent(kind="content", text="页面结论为 unknown[[E1]]。")],
            ]
        )
        with self._patches(registry_call):
            stream_events = list(loop.stream_turn(questions[1], client=stream_client))
        self.assertTrue(any(event["type"] == "sentence" for event in stream_events))
        self.assertFalse(stream_events[-1]["evidence_gap"])

    def test_kb充分时组合回答仍只能绑定本次solver证据(self):
        def registry_call(name, _arguments):
            return _kb_result(sufficient=True) if name == "kb_search" else _craft_result()

        client = _ChatClient(
            [
                LLMResponse(content="", tool_calls=(TOOL_CALL,), model="fake"),
                LLMResponse(content="页面结论为 unknown[[E1]]。", model="fake"),
            ]
        )
        with self._patches(registry_call):
            result = loop.run_turn(QUESTION, client=client)

        self.assertFalse(result.evidence_gap)
        self.assertEqual([e.chunk_id for e in result.evidence], ["craft_solver:Amulets:1"])
        self.assertNotIn("weak-hit", [e.chunk_id for e in result.evidence])
        self.assertTrue(all("确定性求解器" in e.source for e in result.evidence))

    def test_无关参数的同名工具调用不能解锁批量正文(self):
        calls: list[str] = []

        def registry_call(name, _arguments):
            calls.append(name)
            return _kb_result()

        client = _ChatClient(
            [
                LLMResponse(content="", tool_calls=(WRONG_TOOL_CALL,), model="fake"),
                LLMResponse(content="模型用无关求解结果判断可以共存。", model="fake"),
            ]
        )
        with self._patches(registry_call):
            result = loop.run_turn(QUESTION, client=client)

        self.assertEqual(calls, ["kb_search"])
        self.assertTrue(result.evidence_gap)
        self.assertIn("不作组合判断", result.answer)
        self.assertFalse(result.tool_calls[-1]["ok"])
        self.assertNotIn("可以共存", result.answer)

    def test_批量路径未调用精确工具时安全拒答(self):
        client = _ChatClient([LLMResponse(content="模型试图直接判断可以共存。")])
        with self._patches(lambda _name, _arguments: _kb_result()):
            result = loop.run_turn(QUESTION, client=client)

        self.assertTrue(result.evidence_gap)
        self.assertIn("不作组合判断", result.answer)
        self.assertNotIn("直接判断", result.answer)

    def test_流式路径成功前正文被彻底丢弃(self):
        timeline: list[tuple[str, str]] = []

        def registry_call(name, _arguments):
            if name == "kb_search":
                return _kb_result()
            timeline.append(("registry", name))
            return _craft_result()

        client = _StreamClient(
            [
                [
                    StreamEvent(kind="content", text="工具前半句"),
                    StreamEvent(kind="tool_calls", tool_calls=(TOOL_CALL,)),
                ],
                [StreamEvent(kind="content", text="页面结论为 unknown[[E1]]。")],
            ]
        )
        with self._patches(registry_call):
            events = []
            for event in loop.stream_turn(QUESTION, client=client):
                events.append(event)
                timeline.append(("event", event["type"]))

        success_index = timeline.index(("registry", "check_mod_combination"))
        visible_indexes = [
            index
            for index, item in enumerate(timeline)
            if item in {("event", "sentence"), ("event", "blocked")}
        ]
        self.assertTrue(visible_indexes)
        self.assertTrue(all(index > success_index for index in visible_indexes))
        self.assertNotIn("工具前半句", json.dumps(events, ensure_ascii=False))
        self.assertTrue(any(event["type"] == "sentence" for event in events))
        self.assertEqual(events[-1]["type"], "done")

    def test_流式成功后再次调用精确工具则整轮拒答(self):
        craft_calls = 0

        def registry_call(name, _arguments):
            nonlocal craft_calls
            if name == "kb_search":
                return _kb_result()
            craft_calls += 1
            return _craft_result()

        client = _StreamClient(
            [
                [StreamEvent(kind="tool_calls", tool_calls=(TOOL_CALL,))],
                [
                    StreamEvent(kind="content", text="第二次调用前正文。"),
                    StreamEvent(kind="tool_calls", tool_calls=(SECOND_TOOL_CALL,)),
                ],
                [StreamEvent(kind="content", text="第二次调用后正文[[E1]]。")],
            ]
        )
        with self._patches(registry_call):
            events = list(loop.stream_turn(QUESTION, client=client))

        self.assertEqual(craft_calls, 1)
        self.assertFalse(
            any(event["type"] in {"sentence", "blocked", "citations"} for event in events)
        )
        serialized = json.dumps(events, ensure_ascii=False)
        self.assertNotIn("第二次调用前正文", serialized)
        self.assertNotIn("第二次调用后正文", serialized)
        self.assertTrue(events[-1]["evidence_gap"])

    def test_无关参数的同名工具调用不能解锁流式正文(self):
        calls: list[str] = []

        def registry_call(name, _arguments):
            calls.append(name)
            return _kb_result()

        client = _StreamClient(
            [
                [StreamEvent(kind="tool_calls", tool_calls=(WRONG_TOOL_CALL,))],
                [StreamEvent(kind="content", text="模型用无关结果判断可以共存。")],
            ]
        )
        with self._patches(registry_call):
            events = list(loop.stream_turn(QUESTION, client=client))

        self.assertEqual(calls, ["kb_search"])
        self.assertFalse(
            any(event["type"] in {"sentence", "blocked", "citations"} for event in events)
        )
        self.assertNotIn("无关结果", json.dumps(events, ensure_ascii=False))
        self.assertTrue(events[-1]["evidence_gap"])

    def test_流式路径未调用精确工具时安全拒答(self):
        client = _StreamClient(
            [[StreamEvent(kind="content", text="模型试图直接判断可以共存。")]]
        )
        with self._patches(lambda _name, _arguments: _kb_result()):
            events = list(loop.stream_turn(QUESTION, client=client))

        self.assertFalse(
            any(event["type"] in {"sentence", "blocked", "citations"} for event in events)
        )
        gap = next(event for event in events if event["type"] == "gap")
        self.assertIn("不作组合判断", gap["text"])
        self.assertNotIn("直接判断", json.dumps(events, ensure_ascii=False))
        self.assertTrue(events[-1]["evidence_gap"])


class TestStructuredGapLedger(unittest.TestCase):
    def test_最终成功结果覆盖中间缺口(self):
        final = ToolResult(ok=True, data={"gaps": []})
        with patch("orchestrator.loop._record_gaps") as record:
            loop._record_final_tool_result_gaps(
                "问题", {"check_mod_combination": final}
            )
        record.assert_not_called()

    def test_结构化缺口按类别写入且同轮去重(self):
        result = ToolResult(
            ok=True,
            data={
                "gaps": [
                    {"code": "target_not_resolved", "message": "目标未召回"},
                    {"code": "target_not_resolved", "message": "目标未召回"},
                    {"code": "candidate_search_truncated", "message": "候选超过上限"},
                    {"code": "affix_capacity", "message": "容量规则缺失"},
                ]
            },
        )
        seen: set[tuple[str, str]] = set()
        with patch("orchestrator.loop._record_gaps") as record:
            loop._record_tool_result_gaps("问题", result, seen=seen)
            loop._record_tool_result_gaps("问题", result, seen=seen)

        self.assertEqual(record.call_count, 3)
        record.assert_any_call(
            "问题", ["[target_not_resolved] 目标未召回"], category="not_recalled/mod_affix"
        )
        record.assert_any_call(
            "问题",
            ["[candidate_search_truncated] 候选超过上限"],
            category="analysis_limit/craft_feasibility",
        )
        record.assert_any_call(
            "问题", ["[affix_capacity] 容量规则缺失"], category="not_collected/craft_rule"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
