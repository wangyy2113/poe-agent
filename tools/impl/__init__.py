"""工具实现。导入本包即完成注册（副作用在各模块的装饰器里）。"""

from __future__ import annotations

from tools.impl import kb_search as _kb_search  # noqa: F401  注册副作用

__all__ = ["_kb_search"]
