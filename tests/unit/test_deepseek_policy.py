"""Enforce a single upstream model at every network boundary, with no live calls."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import httpx
from openai import APIStatusError

from core.config import Config, Secrets
from core.llm import EmptyContentError, LLMClient, LLMError, TurnBudget, _normalize_base_url
from core.model_policy import DEEPSEEK_MODEL, ModelPolicyError, require_model


def secrets(**overrides):
    values = dict(llm_base_url="https://gateway.example.test", llm_api_key="unit-test-secret",
                  llm_api_keys="", llm_model=DEEPSEEK_MODEL)
    values.update(overrides)
    return Secrets(_env_file=None, **values)


def usage(reasoning=0):
    return NS(prompt_tokens=10, completion_tokens=20, total_tokens=30,
              completion_tokens_details={"reasoning_tokens": reasoning})


def response(content="正文", calls=(), model=DEEPSEEK_MODEL, reasoning=""):
    return NS(model=model, usage=usage(), choices=[NS(finish_reason="stop", message=NS(
        content=content, tool_calls=calls, reasoning_content=reasoning,
    ))])


def chunk(content=None, calls=(), model=DEEPSEEK_MODEL, reasoning=None):
    return NS(model=model, usage=usage(), choices=[NS(finish_reason="stop", delta=NS(
        content=content, tool_calls=calls, reasoning_content=reasoning,
    ))])


class DeepSeekPolicyTest(unittest.TestCase):
    def client(self, **values):
        return LLMClient(Config(raw={"llm": {"retry_delays": []}}), secrets(**values))

    def test_only_pinned_model_and_deepseek_alias_are_allowed(self):
        self.assertEqual(require_model(), DEEPSEEK_MODEL)
        self.assertEqual(require_model("deepseek"), DEEPSEEK_MODEL)
        for model in ("glm-5.2", "claude-opus-5", "gpt-4o", "deepseek-other", "", 1):
            with self.subTest(model=model), self.assertRaises(ModelPolicyError):
                require_model(model)

    def test_default_configuration_pins_the_model(self):
        with patch.dict("os.environ", {}, clear=True):
            config = Secrets(_env_file=None, llm_base_url="https://example.test")
        self.assertEqual(config.llm_model, DEEPSEEK_MODEL)

    def test_legacy_keys_are_normalized_deduplicated_and_never_route_models(self):
        s = secrets(llm_api_keys=f"key-a, key-b:{DEEPSEEK_MODEL},key-a, key-c:deepseek")
        self.assertEqual(s.credential_keys(), ["key-a", "key-b", "key-c"])
        with self.assertRaises(ModelPolicyError) as caught:
            secrets(llm_api_keys="private-secret:glm-5.2").credential_keys()
        self.assertNotIn("private-secret", str(caught.exception))

    def test_invalid_model_or_legacy_alias_fails_before_client_creation(self):
        with patch("core.llm.OpenAI") as sdk:
            with self.assertRaises(LLMError):
                self.client(llm_model="glm-5.2")
            for cfg in ({"model_aliases": {"other": "gpt-4o"}},
                        {"gate_model": "claude-opus-5"}, {"vision_model": "gpt-4o"}):
                with self.subTest(cfg=cfg), self.assertRaises(LLMError):
                    LLMClient(Config(raw={"llm": cfg}), secrets())
        sdk.assert_not_called()

    def test_call_level_override_is_rejected_for_chat_and_stream(self):
        c = self.client()
        with patch.object(c, "_client_for") as sdk:
            for method in (c.chat, lambda *a, **kw: list(c.stream(*a, **kw))):
                with self.assertRaises(LLMError):
                    method([{"role": "user", "content": "test"}], model="gpt-4o")
        sdk.assert_not_called()

    def test_auth_failover_keeps_model_and_scrubs_keys(self):
        c = self.client(llm_api_keys="unit-secret-first,unit-secret-second")
        req = httpx.Request("POST", "https://example.test/v1/chat/completions")
        error = APIStatusError("unit-secret-first unauthorized", response=httpx.Response(401, request=req), body={})
        create = Mock(side_effect=[error, response()])
        with patch.object(c, "_client_for", return_value=NS(chat=NS(completions=NS(create=create)))) as clients:
            result = c.chat([{"role": "user", "content": "test"}])
        self.assertEqual(result.model, DEEPSEEK_MODEL)
        self.assertEqual([x.args[0].index for x in clients.call_args_list], [0, 1])
        self.assertTrue(all(x.kwargs["model"] == DEEPSEEK_MODEL for x in create.call_args_list))
        status = json.dumps(c.credential_status())
        self.assertNotIn("unit-secret-first", status)
        self.assertNotIn("unit-secret-second", status)

    def test_reasoning_is_optional_and_never_exposed(self):
        c = self.client()
        with patch.object(c, "_invoke", return_value=(response(reasoning="private reasoning"), DEEPSEEK_MODEL)):
            result = c.chat([])
        self.assertEqual(result.content, "正文")
        self.assertEqual(result.usage.reasoning_tokens, 0)
        self.assertEqual(result.reasoning_chars, 17)
        self.assertNotIn("private reasoning", repr(result))

    def test_empty_http_success_is_failure_even_without_reasoning_usage(self):
        c = self.client()
        budget = TurnBudget(100)
        with patch.object(c, "_invoke", return_value=(response(content=""), DEEPSEEK_MODEL)):
            with self.assertRaises(EmptyContentError):
                c.chat([], budget=budget)
        self.assertEqual(budget.used.total_tokens, 30)

    def test_stream_empty_response_is_not_silent_success(self):
        c = self.client()
        with patch.object(c, "_invoke", return_value=(iter([chunk()]), DEEPSEEK_MODEL)):
            events = list(c.stream([]))
        self.assertEqual([e.kind for e in events], ["usage", "error"])
        self.assertEqual(events[0].usage.total_tokens, 30)

    def test_reported_other_model_is_rejected_before_content_is_exposed(self):
        c = self.client()
        with patch.object(c, "_invoke", return_value=(response(model="other", content="unsafe"), DEEPSEEK_MODEL)):
            with self.assertRaises(LLMError):
                c.chat([])
        with patch.object(c, "_invoke", return_value=(iter([chunk(model="other", content="unsafe")]), DEEPSEEK_MODEL)):
            events = list(c.stream([]))
        self.assertEqual([e.kind for e in events], ["error"])
        self.assertNotIn("unsafe", repr(events))

    def test_stream_tool_arguments_reassemble_and_reasoning_never_leaks(self):
        c = self.client()
        chunks = [chunk(reasoning="private reasoning"),
                  chunk(calls=[NS(index=0, id="call-1", function=NS(name="lookup", arguments='{"q":'))]),
                  chunk(calls=[NS(index=0, id=None, function=NS(name=None, arguments='"猎首"}'))])]
        with patch.object(c, "_invoke", return_value=(iter(chunks), DEEPSEEK_MODEL)):
            events = list(c.stream([]))
        self.assertEqual([e.kind for e in events], ["thinking", "tool_calls", "usage"])
        self.assertEqual(json.loads(events[1].tool_calls[0].arguments), {"q": "猎首"})
        self.assertNotIn("private reasoning", repr(events))
        self.assertEqual(events[-1].model, DEEPSEEK_MODEL)

    def test_vision_uses_same_model_and_credential(self):
        c = self.client()
        create = Mock(return_value=response())
        with patch.object(c, "_client_for", return_value=NS(chat=NS(completions=NS(create=create)))):
            c.chat([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,test"}}]}])
        self.assertEqual(create.call_args.kwargs["model"], DEEPSEEK_MODEL)
        self.assertEqual(create.call_args.kwargs["messages"][0]["content"][0]["type"], "image_url")

    def test_gateway_url_rejects_inline_secrets_and_normalizes_v1(self):
        for url in ("https://key:private@example.test", "https://example.test?key=private", "file:///tmp/model"):
            with self.subTest(url=url), self.assertRaises(LLMError) as caught:
                _normalize_base_url(url)
            self.assertNotIn("private", str(caught.exception))
        self.assertEqual(_normalize_base_url("https://example.test/v1/"), "https://example.test/v1")


class ModelMaintenanceScriptsTest(unittest.TestCase):
    def test_old_backfill_apply_stops_before_opening_config_or_database(self):
        from scripts import backfill_vectors
        with (patch("sys.argv", ["backfill_vectors.py", "--apply"]),
              patch.object(backfill_vectors, "get_config") as config,
              patch.object(backfill_vectors.sqlite3, "connect") as connect):
            self.assertEqual(backfill_vectors.main(), 2)
        config.assert_not_called()
        connect.assert_not_called()

    def test_probe_rejects_override_before_loading_credentials(self):
        import io
        from scripts import probe_gateway
        for args in (["--model", "other"], ["--base-url", "https://other.example"]):
            with (self.subTest(args=args), patch("sys.argv", ["probe_gateway.py", *args]),
                  patch("sys.stderr", io.StringIO()),
                  patch.object(probe_gateway, "get_secrets") as config):
                with self.assertRaises(SystemExit) as caught:
                    probe_gateway.main()
                self.assertEqual(caught.exception.code, 2)
                config.assert_not_called()
