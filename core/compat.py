"""项目最低 Python 3.10 与较新标准库之间的集中兼容层。"""

from __future__ import annotations

try:
    from enum import StrEnum as StrEnum
except ImportError:  # Python 3.10；StrEnum 从 3.11 才进入标准库
    from enum import Enum

    class StrEnum(str, Enum):
        """Python 3.10 的最小 StrEnum 等价实现。

        本项目所有成员都显式给字符串值，不依赖标准库 StrEnum 的 ``auto()`` 小写规则。
        """

        def __str__(self) -> str:
            return str(self.value)
