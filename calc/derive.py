"""计算器共用的「推导」件：一步一步的算式，与钉住 commit 的引用。

抽出来的理由（第二十一轮）：抗性那一轮把 `Step` 与 `pob_url` 写在 `calc/resistance.py` 里，
第二个计算器（受击结算）要用同样的东西。同一个概念写两份，两份迟早不一致——
而这里不一致的表现是「两个工具的推导表长得不一样」，用户没法用同一种方式核对。

这两件事都是**计算器的公共约定**，不属于任何单一维度：

* `Step`：算出来的数字要连算式一起给，用户能照着核对到每一步
  （`passive_path` 那一轮定的规矩）。
* `pob_url`：引用必须与**常量入库那一刻的 commit** 一致。写死 commit 的话，
  重新导入后引用就指向另一份代码，而读者看不出来。
"""

from __future__ import annotations

from dataclasses import dataclass


def fmt(value: float) -> str:
    """给用户看的数字写法。

    大数用千分位而不是科学计数法：`%g` 会把 1086003 写成 `1.086e+06`，
    那种写法既没法照着核对，也会让出口闸门的数值回填对不上
    （模型会写 1,086,003 或 1086003，两者都不等于 `1.086e+06`）。
    """
    if abs(value) >= 10_000 and float(value).is_integer():
        return f"{value:,.0f}"
    if abs(value) >= 10_000:
        return f"{value:,.2f}"
    return f"{value:g}"


@dataclass(frozen=True)
class Step:
    """推导的一步。`expr` 是可照着核对的算式，不是说明文字。"""

    label: str
    expr: str
    value: float

    def render(self) -> str:
        return f"| {self.label} | {self.expr} | {fmt(self.value)} |"


def render_steps(steps: tuple[Step, ...] | list[Step]) -> list[str]:
    """推导表（含表头）。两个计算器共用同一张表的形状。"""
    return ["| 步骤 | 算式 | 结果 |", "| --- | --- | --- |", *(s.render() for s in steps)]


def join_terms(values: list[float], *, unit: str = "%") -> str:
    """把一串加项写成算式。负数写成 ``- 60%`` 而不是 ``+ -60%``。

    减号一律用 ASCII：出口闸门要把答案里的数字回填到证据原文里比对，
    证据写全角「−60」而模型写「-60」就会对不上，用户看到的是一句被拦掉的正确话。
    （闸门侧也做了归一，两头都不该产生这种差异。）
    """
    out = f"{values[0]:g}{unit}"
    for value in values[1:]:
        sign = "-" if value < 0 else "+"
        out += f" {sign} {abs(value):g}{unit}"
    return out


def pob_url(
    constant_url: str,
    path: str,
    *,
    line: int | None = None,
    lines: tuple[int, int] | None = None,
) -> str:
    """从常量的引用 URL 推出同一 commit 下另一个文件的引用。

    常量与公式来自同一份检出，所以同一个 commit 是可证的。
    推不出来（URL 形状变了）就返回原 URL，不编一个看起来对的。
    """
    marker = "/src/"
    if marker not in constant_url:
        return constant_url
    base = constant_url.split(marker, 1)[0]
    anchor = ""
    if lines is not None:
        anchor = f"#L{lines[0]}-L{lines[1]}"
    elif line is not None:
        anchor = f"#L{line}"
    return f"{base}/{path}{anchor}"
