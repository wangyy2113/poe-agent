"""Failures and cancellation never become successful answers or leak resources."""
from __future__ import annotations

import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api import chat as chat_api
from core.config import Config
from core.llm import LLMClient, LLMError, StreamEvent, TurnBudget, Usage
from core.model_policy import DEEPSEEK_MODEL
from orchestrator import loop
from tests.unit.test_deepseek_policy import chunk, response, secrets


class ModelStreamLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.client = LLMClient(Config({"llm": {"retry_delays": []}}), secrets())

    def test_close_and_account_on_success_error_cancel_and_budget_exhaustion(self):
        for mode in ("success", "error", "cancel", "budget"):
            with self.subTest(mode=mode):
                closed = Mock()
                def raw():
                    try:
                        yield chunk(content="片段")
                        if mode == "error":
                            raise RuntimeError("unit-test-secret https://gateway.example.test/v1 broken")
                    finally:
                        closed()
                budget = TurnBudget(1 if mode == "budget" else 100)
                with patch.object(self.client, "_invoke", return_value=(raw(), DEEPSEEK_MODEL)):
                    stream = self.client.stream([], budget=budget)
                    events = [next(stream)]
                    if mode == "cancel":
                        stream.close()
                    else:
                        events.extend(stream)
                closed.assert_called_once()
                self.assertEqual(30, budget.used.total_tokens)
                if mode in {"error", "budget"}:
                    self.assertEqual("error", events[-1].kind)
                    self.assertNotIn("tool_calls", [event.kind for event in events])
                self.assertNotIn("unit-test-secret", repr(events))
                self.assertNotIn("gateway.example.test", repr(events))

    def test_incomplete_response_cannot_release_tool_calls(self):
        for finish in (None, "length", "content_filter"):
            for streaming in (False, True):
                with self.subTest(finish=finish, streaming=streaming):
                    tool = SimpleNamespace(index=0, id="call", function=SimpleNamespace(name="tool", arguments='{"partial":'))
                    raw = chunk(calls=[tool]) if streaming else response(calls=[tool])
                    raw.choices[0].finish_reason = finish
                    budget = TurnBudget(100)
                    with patch.object(self.client, "_invoke", return_value=(iter([raw]) if streaming else raw, DEEPSEEK_MODEL)):
                        if streaming:
                            events = list(self.client.stream([], budget=budget))
                            self.assertEqual(["error"], [e.kind for e in events])
                        else:
                            with self.assertRaises(LLMError):
                                self.client.chat([], budget=budget)
                    self.assertEqual(30, budget.used.total_tokens)

    def test_rejected_model_still_counts_upstream_usage(self):
        for streaming in (False, True):
            budget = TurnBudget(100)
            raw = iter([chunk(model="other")]) if streaming else response(model="other")
            with patch.object(self.client, "_invoke", return_value=(raw, DEEPSEEK_MODEL)):
                if streaming:
                    self.assertEqual("error", list(self.client.stream([], budget=budget))[-1].kind)
                else:
                    with self.assertRaises(LLMError):
                        self.client.chat([], budget=budget)
            self.assertEqual(30, budget.used.total_tokens)


class TurnLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(loop, "get_config", return_value=Config({})))
        self.stack.enter_context(patch.object(loop, "_domain_terms", return_value=[]))
        self.persist = self.stack.enter_context(patch.object(loop, "_persist"))

    def test_deterministic_answers_do_not_construct_a_model_client(self):
        with patch.object(loop, "get_llm_client", side_effect=AssertionError("no credentials")) as client:
            for setup in (loop._TurnSetup(gap="缺据"), loop._TurnSetup(final="确定性回答")):
                with patch.object(loop, "_prepare_turn", return_value=setup):
                    answer = loop.run_turn("问题")
                    events = list(loop.stream_turn("问题"))
                self.assertEqual(setup.gap or setup.final, answer.answer)
                self.assertEqual("done", events[-1]["type"])
            client.assert_not_called()

    def test_failed_stream_discards_sentence_buffer_and_precision_round(self):
        for precision in ("", "exact_tool"):
            closed = Mock()
            def stream(_messages, **kwargs):
                try:
                    kwargs["budget"].add(Usage(total_tokens=17))
                    yield StreamEvent("content", text="尚未完成的半句话")
                    yield StreamEvent("error", detail="upstream interrupted")
                    raise AssertionError("must stop after error")
                finally:
                    closed()
            setup = loop._TurnSetup(precision_tool=precision)
            with (self.subTest(precision=precision),
                  patch.object(loop, "_prepare_turn", return_value=setup),
                  patch.object(loop._ToolLoopState, "start", return_value=loop._ToolLoopState(
                      question="q", precision_tool=precision, evidence=[], succeeded=True)),
                  patch.object(loop, "_terminal_gap") as terminal):
                events = list(loop.stream_turn("q", client=SimpleNamespace(stream=stream)))
            self.assertEqual(["meta", "error"], [event["type"] for event in events])
            self.assertNotIn("半句话", repr(events))
            self.assertEqual(17, self.persist.call_args.args[0].tokens["total"])
            terminal.assert_not_called()
            closed.assert_called_once()

    def test_cancel_turn_closes_model_stream_without_saving_success(self):
        closed = Mock()
        def stream(*_args, **_kwargs):
            try:
                yield StreamEvent("thinking")
                raise AssertionError("cancelled")
            finally:
                closed()
        with patch.object(loop, "_prepare_turn", return_value=loop._TurnSetup()):
            events = loop.stream_turn("q", client=SimpleNamespace(stream=stream))
            self.assertEqual("meta", next(events)["type"])
            self.assertEqual("thinking", next(events)["type"])
            events.close()
        closed.assert_called_once()
        self.persist.assert_not_called()

    def test_failed_batch_records_spent_tokens(self):
        def chat(_messages, **kwargs):
            kwargs["budget"].add(Usage(total_tokens=17))
            raise LLMError("failed")
        with patch.object(loop, "_prepare_turn", return_value=loop._TurnSetup()):
            result = loop.run_turn("q", client=SimpleNamespace(chat=chat))
        self.assertEqual(17, result.tokens["total"])
        self.assertEqual(17, self.persist.call_args.args[0].tokens["total"])


class ConversationStreamLifecycleTest(unittest.TestCase):
    def wire(self, protocol):
        async def consume(response):
            return "".join([part async for part in response.body_iterator])
        if protocol == "native":
            return asyncio.run(consume(chat_api.chat_stream(
                chat_api.ChatRequest(message="q", conversation_id="conv"), caller="test")))
        request = chat_api.OpenAIChatRequest(messages=[{"role": "user", "content": "q"}], model="poe-agent", stream=True)
        return "".join(chat_api._openai_stream(request, "q", "conv", 0, "test"))

    def test_both_protocols_save_complete_answers_with_notes_and_citations(self):
        events = [
            {"type": "sentence", "text": "答案"}, {"type": "note", "text": "附注"},
            {"type": "citations", "items": [{"label": "E1", "source": "POEDB", "title": "来源", "url": "https://example.test"}]},
            {"type": "done"},
        ]
        saved = []
        for protocol in ("native", "openai"):
            with (patch.object(chat_api, "stream_turn", return_value=iter(events)),
                  patch.object(chat_api, "_ensure_conversation"),
                  patch.object(chat_api, "_save_message") as save):
                self.wire(protocol)
            saved.append([c.args[2] for c in save.call_args_list if c.args[1] == "assistant"])
        self.assertEqual(saved[0], saved[1])
        self.assertIn("附注", saved[0][0])
        self.assertIn("https://example.test", saved[0][0])

    def test_both_protocols_reject_error_exception_and_truncated_streams(self):
        for protocol in ("native", "openai"):
            for mode in ("error", "exception", "truncated"):
                closed = Mock()
                def upstream(*_args, **_kwargs):
                    try:
                        yield {"type": "sentence", "text": "已收到的一句。"}
                        if mode == "error":
                            yield {"type": "error", "message": "中断"}
                            yield {"type": "done"}  # Even a misbehaving producer cannot save it.
                        elif mode == "exception":
                            raise RuntimeError("private details")
                    finally:
                        closed()
                with (self.subTest(protocol=protocol, mode=mode),
                      patch.object(chat_api, "stream_turn", side_effect=upstream),
                      patch.object(chat_api, "_ensure_conversation"),
                      patch.object(chat_api, "_save_message") as save):
                    wire = self.wire(protocol)
                self.assertIn('"error"', wire)
                self.assertNotIn('"finish_reason": "stop"', wire)
                self.assertNotIn('"type": "done"', wire)
                self.assertNotIn("private details", wire)
                self.assertFalse(any(c.args[1] == "assistant" for c in save.call_args_list))
                closed.assert_called_once()

    def test_close_before_done_does_not_save_partial_history(self):
        closed = Mock()
        def upstream(*_a, **_k):
            try:
                yield {"type": "sentence", "text": "半截回答"}
                yield {"type": "done"}
            finally:
                closed()
        with (patch.object(chat_api, "stream_turn", side_effect=upstream),
              patch.object(chat_api, "_save_message") as save):
            events = chat_api._conversation_stream("q", "conv", "test")
            next(events)
            events.close()
        save.assert_not_called()
        closed.assert_called_once()
