"""MCP 2025-06-18 的最小 stdio 服务端协议层。

只实现本项目需要的生命周期、ping、tools/list 与 tools/call。传输层严格使用
UTF-8、每行一个 JSON-RPC 消息；stdout 只写协议消息，诊断信息只能写 stderr。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO

from tools.registry import ToolRegistry, ToolResult

LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = (
    LATEST_PROTOCOL_VERSION,
    "2025-03-26",
    "2024-11-05",
)
SERVER_NAME = "poe1-agent"
SERVER_VERSION = "0.2.0"
MAX_MESSAGE_BYTES = 2 * 1024 * 1024

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
SERVER_NOT_INITIALIZED = -32002


def _error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _response(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _valid_request_id(value: object) -> bool:
    """JSON-RPC request id 只能是字符串或整数，不能是 null / bool。"""
    return isinstance(value, (str, int)) and not isinstance(value, bool)


def _tool_payload(result: ToolResult) -> dict[str, Any]:
    structured: dict[str, Any] = {
        "ok": result.ok,
        "data": result.data,
        "citations": result.citations,
        "meta": result.meta,
        "elapsed_ms": result.elapsed_ms,
    }
    if result.error:
        structured["error"] = result.error
    if result.ok and isinstance(result.data, dict) and isinstance(result.data.get("answer"), str):
        text = result.data["answer"]
    else:
        text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": not result.ok,
    }


@dataclass
class McpProtocol:
    registry: ToolRegistry
    exposed_tools: tuple[str, ...]
    initialize_responded: bool = False
    ready: bool = False
    protocol_version: str = LATEST_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        missing = [name for name in self.exposed_tools if self.registry.spec(name) is None]
        if missing:
            raise ValueError(f"MCP 暴露列表含未注册工具：{', '.join(missing)}")

    def handle(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "Invalid Request")
        request_id = message.get("id")
        is_notification = "id" not in message
        if not is_notification and not _valid_request_id(request_id):
            return _error(None, INVALID_REQUEST, "Invalid Request")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return None if is_notification else _error(request_id, INVALID_REQUEST, "Invalid Request")

        method = message["method"]
        params = message.get("params", {})
        if not isinstance(params, dict):
            return None if is_notification else _error(request_id, INVALID_PARAMS, "Invalid params")

        if method == "initialize":
            if is_notification:
                return None
            if self.initialize_responded:
                return _error(request_id, INVALID_REQUEST, "Server already initialized")
            requested = params.get("protocolVersion")
            if not isinstance(requested, str) or not requested:
                return _error(request_id, INVALID_PARAMS, "protocolVersion is required")
            capabilities = params.get("capabilities")
            client_info = params.get("clientInfo")
            if not isinstance(capabilities, dict):
                return _error(request_id, INVALID_PARAMS, "capabilities must be an object")
            if (
                not isinstance(client_info, dict)
                or not isinstance(client_info.get("name"), str)
                or not client_info["name"]
                or not isinstance(client_info.get("version"), str)
                or not client_info["version"]
            ):
                return _error(
                    request_id,
                    INVALID_PARAMS,
                    "clientInfo.name and clientInfo.version are required",
                )
            self.protocol_version = (
                requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
            )
            self.initialize_responded = True
            return _response(request_id, {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": "POE1 Agent",
                    "version": SERVER_VERSION,
                },
                "instructions": (
                    "只使用工具返回的结构化事实与 citations；unknown 和 boundary 不得省略，"
                    "不得用模型记忆补全游戏机制、概率、价格或制作路线。"
                ),
            })

        if method == "notifications/initialized":
            if not is_notification:
                return _error(request_id, INVALID_REQUEST, "initialized must be a notification")
            if self.initialize_responded:
                self.ready = True
            return None
        if method == "notifications/cancelled":
            if not is_notification:
                return _error(request_id, INVALID_REQUEST, "cancelled must be a notification")
            return None
        if method == "ping":
            return None if is_notification else _response(request_id, {})
        if not self.ready:
            return None if is_notification else _error(
                request_id, SERVER_NOT_INITIALIZED, "Server not initialized"
            )

        if method == "tools/list":
            if is_notification:
                return None
            cursor = params.get("cursor")
            if cursor not in (None, ""):
                return _error(request_id, INVALID_PARAMS, "This server does not paginate tools")
            tools = []
            for name in self.exposed_tools:
                spec = self.registry.spec(name)
                assert spec is not None
                tools.append({
                    "name": spec.name,
                    "description": spec.description,
                    "inputSchema": spec.parameters,
                    "annotations": {
                        "readOnlyHint": True,
                        "destructiveHint": False,
                        "idempotentHint": True,
                        "openWorldHint": spec.name.startswith("economy_"),
                    },
                })
            return _response(request_id, {"tools": tools})

        if method == "tools/call":
            if is_notification:
                return None
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not name:
                return _error(request_id, INVALID_PARAMS, "Tool name is required")
            if name not in self.exposed_tools:
                return _error(request_id, METHOD_NOT_FOUND, f"Tool not exposed: {name}")
            if not isinstance(arguments, dict):
                return _error(request_id, INVALID_PARAMS, "Tool arguments must be an object")
            result = self.registry.call(
                name, json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            )
            return _response(request_id, _tool_payload(result))

        return None if is_notification else _error(request_id, METHOD_NOT_FOUND, "Method not found")


def handle_line(protocol: McpProtocol, raw_line: bytes) -> dict[str, Any] | None:
    if len(raw_line) > MAX_MESSAGE_BYTES:
        return _error(None, INVALID_REQUEST, "Message exceeds 2 MiB limit")
    try:
        message = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error(None, PARSE_ERROR, "Parse error")
    return protocol.handle(message)


def serve(reader: BinaryIO, writer: BinaryIO, protocol: McpProtocol) -> None:
    """阻塞处理 stdio，直到客户端关闭 stdin。"""
    for raw_line in reader:
        if not raw_line.strip():
            continue
        try:
            reply = handle_line(protocol, raw_line)
        except Exception as exc:  # noqa: BLE001
            print(f"MCP internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
            reply = _error(None, INTERNAL_ERROR, "Internal error")
        if reply is None:
            continue
        encoded = json.dumps(reply, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        writer.write(encoded + b"\n")
        writer.flush()
