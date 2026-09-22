"""POE1-Agent MCP stdio 入口。

客户端示例命令：``/absolute/path/to/.venv/bin/python /absolute/path/to/mcp_server.py``。
stdout 专用于 MCP JSON-RPC；运行诊断只允许写 stderr。
"""

from __future__ import annotations

import argparse
import sys

from agents.knowledge import ALLOWED_TOOLS
from core import db as dbmod
from core.config import get_config
from core.mcp_protocol import McpProtocol, serve
from tools.registry import REGISTRY

# 导入即注册工具，与 Web 服务入口保持同一套实现。
import tools.impl  # noqa: F401,E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="POE1-Agent MCP stdio server")
    parser.add_argument(
        "--tools",
        help="仅暴露逗号分隔的工具名；默认暴露 Knowledge Agent 的全部只读工具",
    )
    return parser


def _tool_names(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return tuple(ALLOWED_TOOLS)
    names = tuple(dict.fromkeys(name.strip() for name in raw.split(",") if name.strip()))
    if not names:
        raise ValueError("--tools 至少需要一个工具名")
    unknown = [name for name in names if name not in ALLOWED_TOOLS]
    if unknown:
        raise ValueError(f"不允许暴露的工具：{', '.join(unknown)}")
    return names


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        names = _tool_names(args.tools)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cfg = get_config()
    dim = int(cfg.get("retrieval.vector.dim", 0) or 0)
    try:
        dbmod.init_db(cfg.db_path, vector_dim=dim)
        serve(sys.stdin.buffer, sys.stdout.buffer, McpProtocol(REGISTRY, names))
    finally:
        dbmod.close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
