"""POE1 制作领域逻辑。

当前只实现目标词缀组合的可行性约束检查；制作步骤、概率与成本属于后续独立层。
"""

from __future__ import annotations

from craft.feasibility import analyze_feasibility

__all__ = ["analyze_feasibility"]
