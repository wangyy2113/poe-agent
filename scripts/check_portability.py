#!/usr/bin/env python3
"""静态检查 Python 代码中的跨平台回归。

这不是通用 linter；只钉住本项目已经明确禁止的几类写法。检查基于 AST，避免把注释、
文档里的命令示例或 URL 误当成代码。返回非零即可直接接入本地验证或 CI。
"""

from __future__ import annotations

import ast
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = (
    "agents", "api", "calc", "core", "craft", "entities", "kb", "orchestrator",
    "parsers", "retrieval", "scripts", "tools",
)
BANNED_MODULES = {"fcntl", "resource", "pwd", "grp", "termios"}
BANNED_OS_CALLS = {"os.fork", "os.system", "os.popen"}
BANNED_SHELL_TOOLS = {"ps", "grep", "awk", "kill", "pkill", "nohup"}
HARD_CODED_PYTHON = re.compile(
    r"^(?:/(?:[^/]+/)*python(?:\d+(?:\.\d+)*)?"
    r"|[A-Za-z]:[\\/](?:[^\\/]+[\\/])*python(?:\d+(?:\.\d+)*)?\.exe)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class Issue:
    path: Path
    line: int
    code: str
    message: str

    def render(self, root: Path = ROOT) -> str:
        try:
            path = self.path.relative_to(root)
        except ValueError:
            path = self.path
        return f"{path}:{self.line}: {self.code} {self.message}"


def python_sources(root: Path = ROOT) -> list[Path]:
    files = [root / "main.py"]
    for dirname in SOURCE_DIRS:
        base = root / dirname
        if base.is_dir():
            files.extend(base.rglob("*.py"))
    return sorted(path for path in files if path.is_file() and "__pycache__" not in path.parts)


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.issues: list[Issue] = []
        self.module_aliases: dict[str, str] = {}
        self.call_aliases: dict[str, str] = {}

    def issue(self, node: ast.AST, code: str, message: str) -> None:
        self.issues.append(Issue(self.path, getattr(node, "lineno", 1), code, message))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            binding = alias.asname or root
            self.module_aliases[binding] = alias.name if alias.asname else root
            if root in BANNED_MODULES:
                self.issue(node, "PORT001", f"禁止导入平台专有模块 {root}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        root = module.split(".", 1)[0]
        if root in BANNED_MODULES:
            self.issue(node, "PORT001", f"禁止导入平台专有模块 {root}")
        for alias in node.names:
            self.call_aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and HARD_CODED_PYTHON.fullmatch(node.value.strip()):
            self.issue(node, "PORT002", "禁止硬编码绝对 Python 解释器路径")

    def visit_Call(self, node: ast.Call) -> None:
        qualified = self._qualified_name(node.func)
        if qualified in BANNED_OS_CALLS:
            self.issue(node, "PORT003", f"禁止调用平台/外壳相关函数 {qualified}")
        if qualified == "os.path.join":
            self.issue(node, "PORT004", "文件系统路径必须用 pathlib 组合")
        if qualified.startswith("subprocess."):
            self._check_subprocess(node)
        if qualified == "open" or qualified.endswith(".open"):
            self._check_text_open(node)
        if qualified.endswith(".read_text") or qualified.endswith(".write_text"):
            self._require_encoding(node, "PORT007", "文本读写必须显式指定 encoding")
        self.generic_visit(node)

    def _qualified_name(self, node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return self.call_aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parts = [node.attr]
            value = node.value
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name):
                parts.append(self.module_aliases.get(value.id, value.id))
                return ".".join(reversed(parts))
            # Path("...").open() 这类链式调用没有可解析的根名称，但末端方法仍足以
            # 判断是否属于本检查关心的文本 I/O API。
            return node.attr
        return ""

    def _check_subprocess(self, node: ast.Call) -> None:
        if any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        ):
            self.issue(node, "PORT005", "禁止 subprocess 使用 shell=True")
        head = _command_head(node.args[0]) if node.args else ""
        if head and Path(head).name.lower() in BANNED_SHELL_TOOLS:
            self.issue(node, "PORT006", f"禁止依赖外部 shell 工具 {Path(head).name}")

    def _check_text_open(self, node: ast.Call) -> None:
        mode_node = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
        if mode_node is None:
            # builtins/gzip.open(file, mode) 与 Path.open(mode) 的位置不同。若方法调用的
            # 第一个位置参数本身长得像文件模式，就按 Path.open 处理。
            first = node.args[0] if node.args else None
            path_mode = (
                isinstance(node.func, ast.Attribute)
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and re.fullmatch(r"[rwaxt+b]+", first.value)
            )
            index = 0 if path_mode else 1
            mode_node = node.args[index] if len(node.args) > index else None
        mode = mode_node.value if isinstance(mode_node, ast.Constant) else None
        if isinstance(mode, str) and "b" in mode:
            return
        self._require_encoding(node, "PORT007", "文本 open 必须显式指定 encoding")

    def _require_encoding(self, node: ast.Call, code: str, message: str) -> None:
        encoding = next((kw.value for kw in node.keywords if kw.arg == "encoding"), None)
        if encoding is None or isinstance(encoding, ast.Constant) and encoding.value is None:
            self.issue(node, code, message)


def _command_head(node: ast.expr) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return shlex.split(node.value)[0]
        except (ValueError, IndexError):
            return ""
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        first = node.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


def scan_file(path: Path) -> list[Issue]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return [Issue(path, getattr(exc, "lineno", 1) or 1, "PORT000", type(exc).__name__)]
    visitor = _Visitor(path)
    visitor.visit(tree)
    return sorted(visitor.issues)


def scan(paths: Iterable[Path]) -> list[Issue]:
    return sorted(issue for path in paths for issue in scan_file(path))


def main() -> int:
    issues = scan(python_sources())
    if issues:
        print("平台中立性检查失败：")
        for issue in issues:
            print(f"  {issue.render()}")
        return 1
    print(f"平台中立性检查通过：{len(python_sources())} 个 Python 文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
