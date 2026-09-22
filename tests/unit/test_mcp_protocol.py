"""MCP stdio 协议层：生命周期、工具发现、调用与传输边界。"""

from __future__ import annotations

import io
import json
import unittest

from core.mcp_protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    LATEST_PROTOCOL_VERSION,
    MAX_MESSAGE_BYTES,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SERVER_NOT_INITIALIZED,
    McpProtocol,
    handle_line,
    serve,
)
from tools.registry import ToolRegistry, ToolResult


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.register(
        name="echo",
        description="回显中文输入",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    def echo(text: str) -> ToolResult:
        answer = f"结果：{text}"
        return ToolResult(
            ok=True,
            data={"answer": answer, "value": text},
            citations=[{"title": "测试证据", "url": "https://example.test"}],
            meta={"channel": "structured"},
        )

    @registry.register(
        name="failure",
        description="返回受控错误",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    def failure() -> ToolResult:
        return ToolResult(ok=False, error="受控失败")

    return registry


def _request(method: str, request_id: object = 1, params: object | None = None) -> dict:
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _initialize_params(version: str = LATEST_PROTOCOL_VERSION) -> dict:
    return {
        "protocolVersion": version,
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    }


class McpProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = McpProtocol(_registry(), ("echo", "failure"))

    def initialize(self, version: str = LATEST_PROTOCOL_VERSION) -> dict:
        response = self.protocol.handle(_request("initialize", params=_initialize_params(version)))
        self.assertIsNotNone(response)
        self.assertIsNone(self.protocol.handle({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }))
        return response

    def test_initialize_negotiates_supported_version_and_capabilities(self) -> None:
        response = self.initialize("2024-11-05")
        self.assertEqual("2024-11-05", response["result"]["protocolVersion"])
        self.assertEqual({"listChanged": False}, response["result"]["capabilities"]["tools"])
        self.assertEqual("poe1-agent", response["result"]["serverInfo"]["name"])

    def test_unknown_protocol_falls_back_to_latest(self) -> None:
        response = self.initialize("1900-01-01")
        self.assertEqual(LATEST_PROTOCOL_VERSION, response["result"]["protocolVersion"])

    def test_tools_require_completed_initialization(self) -> None:
        response = self.protocol.handle(_request("tools/list"))
        self.assertEqual(SERVER_NOT_INITIALIZED, response["error"]["code"])
        self.protocol.handle(_request("initialize", params=_initialize_params()))
        response = self.protocol.handle(_request("tools/list", request_id=2))
        self.assertEqual(SERVER_NOT_INITIALIZED, response["error"]["code"])

    def test_initialize_requires_client_capabilities_and_identity(self) -> None:
        invalid = (
            {"protocolVersion": LATEST_PROTOCOL_VERSION},
            {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": []},
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test"},
            },
        )
        for params in invalid:
            with self.subTest(params=params):
                protocol = McpProtocol(_registry(), ("echo",))
                response = protocol.handle(_request("initialize", params=params))
                self.assertEqual(INVALID_PARAMS, response["error"]["code"])
                self.assertFalse(protocol.initialize_responded)

    def test_tools_list_converts_registry_schema_and_annotations(self) -> None:
        self.initialize()
        response = self.protocol.handle(_request("tools/list"))
        tools = response["result"]["tools"]
        self.assertEqual(["echo", "failure"], [tool["name"] for tool in tools])
        self.assertEqual("object", tools[0]["inputSchema"]["type"])
        self.assertTrue(tools[0]["annotations"]["readOnlyHint"])
        self.assertFalse(tools[0]["annotations"]["destructiveHint"])
        self.assertFalse(tools[0]["annotations"]["openWorldHint"])

    def test_tools_list_rejects_unknown_cursor(self) -> None:
        self.initialize()
        response = self.protocol.handle(_request("tools/list", params={"cursor": "next"}))
        self.assertEqual(INVALID_PARAMS, response["error"]["code"])

    def test_tool_call_returns_text_structured_content_and_citations(self) -> None:
        self.initialize()
        response = self.protocol.handle(_request("tools/call", params={
            "name": "echo", "arguments": {"text": "猎首"},
        }))
        result = response["result"]
        self.assertFalse(result["isError"])
        self.assertEqual("结果：猎首", result["content"][0]["text"])
        self.assertEqual("猎首", result["structuredContent"]["data"]["value"])
        self.assertEqual("测试证据", result["structuredContent"]["citations"][0]["title"])

    def test_tool_failure_is_mcp_tool_error_not_jsonrpc_error(self) -> None:
        self.initialize()
        response = self.protocol.handle(_request("tools/call", params={
            "name": "failure", "arguments": {},
        }))
        self.assertNotIn("error", response)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual("受控失败", response["result"]["structuredContent"]["error"])

    def test_tool_call_validates_name_arguments_and_allowlist(self) -> None:
        self.initialize()
        for params, code in (
            ({"arguments": {}}, INVALID_PARAMS),
            ({"name": "echo", "arguments": []}, INVALID_PARAMS),
            ({"name": "hidden", "arguments": {}}, METHOD_NOT_FOUND),
        ):
            with self.subTest(params=params):
                response = self.protocol.handle(_request("tools/call", params=params))
                self.assertEqual(code, response["error"]["code"])

    def test_invalid_request_unknown_method_and_duplicate_initialize(self) -> None:
        self.assertEqual(INVALID_REQUEST, self.protocol.handle({
            "jsonrpc": "1.0", "id": 1, "method": "ping",
        })["error"]["code"])
        self.initialize()
        self.assertEqual(METHOD_NOT_FOUND, self.protocol.handle(
            _request("unknown/method")
        )["error"]["code"])
        self.assertEqual(INVALID_REQUEST, self.protocol.handle(_request(
            "initialize", params=_initialize_params()
        ))["error"]["code"])

    def test_request_id_must_be_non_null_string_or_integer(self) -> None:
        for request_id in (None, True, 1.5, [], {}):
            with self.subTest(request_id=request_id):
                response = self.protocol.handle(_request("ping", request_id=request_id))
                self.assertEqual(INVALID_REQUEST, response["error"]["code"])
                self.assertIsNone(response["id"])
        self.assertEqual("ok", self.protocol.handle(_request("ping", request_id="ok"))["id"])

    def test_notifications_never_emit_responses(self) -> None:
        self.assertIsNone(self.protocol.handle({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }))
        self.initialize()
        self.assertIsNone(self.protocol.handle({
            "jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1},
        }))
        self.assertIsNone(self.protocol.handle({
            "jsonrpc": "2.0", "method": "notifications/unknown",
        }))

    def test_parse_error_utf8_error_and_size_limit(self) -> None:
        for raw, code in (
            (b"{broken}\n", PARSE_ERROR),
            (b"\xff\n", PARSE_ERROR),
            (b" " * (MAX_MESSAGE_BYTES + 1), INVALID_REQUEST),
        ):
            with self.subTest(code=code):
                self.assertEqual(code, handle_line(self.protocol, raw)["error"]["code"])

    def test_stdio_is_one_utf8_json_message_per_output_line(self) -> None:
        requests = [
            _request("initialize", params=_initialize_params()),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            _request("tools/call", request_id="二", params={
                "name": "echo", "arguments": {"text": "中文"},
            }),
        ]
        reader = io.BytesIO(b"".join(
            json.dumps(item, ensure_ascii=False).encode("utf-8") + b"\n" for item in requests
        ))
        writer = io.BytesIO()
        serve(reader, writer, self.protocol)
        lines = writer.getvalue().decode("utf-8").splitlines()
        self.assertEqual(2, len(lines), "initialized 通知不得产生响应行")
        decoded = [json.loads(line) for line in lines]
        self.assertEqual("二", decoded[1]["id"])
        self.assertEqual("结果：中文", decoded[1]["result"]["content"][0]["text"])

    def test_exposed_tool_names_are_validated_at_startup(self) -> None:
        with self.assertRaisesRegex(ValueError, "未注册工具"):
            McpProtocol(_registry(), ("missing",))


if __name__ == "__main__":
    unittest.main()
