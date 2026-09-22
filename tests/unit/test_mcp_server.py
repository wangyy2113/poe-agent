"""MCP stdio 入口的真实子进程与资源生命周期测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import mcp_server

REPO_ROOT = Path(__file__).resolve().parents[2]


def _message(method: str, request_id: int | None = None, params: dict | None = None) -> dict:
    message: dict = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        message["id"] = request_id
    if params is not None:
        message["params"] = params
    return message


class McpServerProcessTest(unittest.TestCase):
    def test_real_stdio_process_has_clean_stdout_allowlist_and_local_tool_call(self) -> None:
        requests = [
            _message("initialize", 1, {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "unittest", "version": "1"},
            }),
            _message("notifications/initialized"),
            _message("tools/list", 2, {}),
            _message("tools/call", 3, {"name": "kb_coverage", "arguments": {}}),
        ]
        input_text = "".join(
            json.dumps(message, ensure_ascii=False) + "\n" for message in requests
        )
        completed = subprocess.run(
            [sys.executable, "mcp_server.py", "--tools", "kb_coverage"],
            cwd=REPO_ROOT,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        lines = completed.stdout.splitlines()
        self.assertEqual(3, len(lines), "initialized 通知不得污染 stdout")
        responses = [json.loads(line) for line in lines]
        self.assertEqual([1, 2, 3], [response["id"] for response in responses])
        self.assertEqual(
            ["kb_coverage"],
            [tool["name"] for tool in responses[1]["result"]["tools"]],
        )
        result = responses[2]["result"]
        self.assertFalse(result["isError"])
        self.assertGreater(result["structuredContent"]["data"]["domains_total"], 0)
        self.assertEqual("meta", result["structuredContent"]["meta"]["channel"])

    def test_invalid_tool_allowlist_fails_without_protocol_output(self) -> None:
        completed = subprocess.run(
            [sys.executable, "mcp_server.py", "--tools", "not_registered"],
            cwd=REPO_ROOT,
            input="",
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

        self.assertEqual(2, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertIn("不允许暴露的工具", completed.stderr)


class McpServerLifecycleTest(unittest.TestCase):
    def test_eof_always_closes_sqlite(self) -> None:
        cfg = SimpleNamespace(db_path=Path("unused.db"), get=lambda *_args: 0)
        with (
            patch.object(mcp_server, "get_config", return_value=cfg),
            patch.object(mcp_server.dbmod, "init_db") as init_db,
            patch.object(mcp_server.dbmod, "close_db") as close_db,
            patch.object(mcp_server, "serve") as serve,
        ):
            exit_code = mcp_server.main(["--tools", "kb_coverage"])

        self.assertEqual(0, exit_code)
        init_db.assert_called_once_with(Path("unused.db"), vector_dim=0)
        serve.assert_called_once()
        close_db.assert_called_once_with()

    def test_allowlist_trims_and_deduplicates_in_client_order(self) -> None:
        self.assertEqual(
            ("kb_coverage", "entity_lookup"),
            mcp_server._tool_names(" kb_coverage,entity_lookup,kb_coverage "),
        )


if __name__ == "__main__":
    unittest.main()
