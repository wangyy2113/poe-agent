"""工具实现。导入本包即完成注册（副作用在各模块的装饰器里）。"""
from __future__ import annotations

from tools.impl import craft as _craft  # noqa: F401  注册副作用
from tools.impl import kb_search as _kb_search  # noqa: F401  注册副作用
from tools.impl import mods as _mods  # noqa: F401  注册副作用

__all__ = ["_craft", "_kb_search", "_mods"]
