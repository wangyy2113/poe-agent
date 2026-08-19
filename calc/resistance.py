"""抗性与最大抗性：按 PoB 的原式复算，并把每一步都写出来。

## 为什么是复算，而不是查一个数

抗性不是一个存量值，而是一串**有顺序**的运算：先把所有「+X% 抗性」加起来
（含地图减抗与诅咒的负值），再乘上「增加的抗性」，**截断小数**，最后夹在下限与
上限之间。顺序错了结论会反向，最典型的一条是「超出上限的抗性能不能防穿透」——
答案取决于「夹上限」与「减穿透」谁先谁后，而这在代码里是明写的：

    calcs.resistances：final = max(min(total, max), min)          ← 先夹
    damage taken     ：resMult = 1 - (resist - enemyPen) / 100    ← 后减穿透

所以**超抗防的是「降低抗性」与诅咒（它们进 total、在夹之前），不防穿透**。
这个结论不是经验之谈，是两行代码的先后顺序，引用里给出了两处的行号锚点。

## 这里只做「算」，不做「猜」

三个常量全部取自 `game_constant`（缺失即抛 `MissingConstant`，不给默认值）：

* 基础最大抗性 75  ← `characterConstants.base_maximum_all_resistances_%`
  （PoB 在 `CalcSetup.lua` 里就是把这个常量作为四种最大抗性的 BASE 加进去的）
* 最大抗性的硬上限 90 ← `pobMisc.MaxResistCap`
* 抗性下限 −200      ← `pobMisc.ResistFloor`

输入侧一律由**调用方逐字给出**：本模块不猜「你大概有多少抗」，没给就是 0。
`passive_path` 那一轮定下的规矩在这里同样成立——算出来的数字要连推导一起给，
用户能照着核对到每一步。

## 与 PoB 的差异（写出来，不藏着）

* PoB 是从整个 modDB 求和得出 base / inc / more 的；这里把三者当**输入**。
  也就是说本模块复算的是**公式**，不是「读你的 build」——那要等 PoB 导入。
* `Melding of the Flesh` 一类的 OVERRIDE 用 `max_override` 表达（它绕过 90 上限，
  PoB 里 `modDB:Override` 就在 `m_min(MaxResistCap, ...)` 之前短路）。
* 图腾抗性（`Totem*Resist`）与持续伤害抗性（`*ResistOverTime`）本模块不算：
  PoB 有独立分支，参数也不同，凑一份「看起来对」的比不算更糟。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from calc import constants

#: 四种抗性。PoB 的 `resistTypeList`，其中只有前三种算元素抗性（`isElemental`）。
ELEMENTS_ZH: dict[str, str] = {
    "火焰": "Fire",
    "冰霜": "Cold",
    "闪电": "Lightning",
    "混沌": "Chaos",
}
#: 只有这三种吃「+X% 全部元素抗性」与「+X% 最大元素抗性」。
#: 混沌不吃——把「+4% 最大元素抗性」算到混沌上是这一块最常见的错。
ELEMENTAL = ("Fire", "Cold", "Lightning")

#: PoB 计算抗性的原文位置。引用必须带行号锚点，点开就是这一段。
FORMULA_FILE = "src/Modules/CalcDefence.lua"
FORMULA_LINES = (578, 615)
#: 「抗性怎么变成减伤」在同一文件的另一处：`resMult = 1 - (resist - enemyPen) / 100`。
TAKEN_LINE = 1970
#: 剧情抗性惩罚：PoB 在建角色时就把它作为四种抗性的 BASE 加进去
#: （`CalcSetup.lua:503-506`，`env.configInput.resistancePenalty or -60`）。
SETUP_FILE = "src/Modules/CalcSetup.lua"
PENALTY_LINES = (503, 506)
CONFIG_FILE = "src/Modules/ConfigOptions.lua"
CONFIG_PENALTY_LINE = 135

#: 三档剧情抗性惩罚，取自 `ConfigOptions.lua:135` 的下拉项（默认第 3 项 = −60）。
#: 这三个数是代码里的字面量、不在常量表里，所以由
#: `tests/unit/test_resistance.py` 直接对着上游原文钉住——公式与字面量漂移
#: 此前没有任何东西能拦（`check_doc_facts.py` 只拦文档里的数字）。
CAMPAIGN_PENALTIES: dict[int, str] = {
    0: "无惩罚",
    -30: "第五幕之后",
    -60: "第十幕之后，PoB 默认档",
}


class UnknownElement(ValueError):
    """抗性类型不认识。不猜——猜错了整条结论反向。"""


@dataclass(frozen=True)
class Step:
    """推导的一步。`expr` 是可照着核对的算式，不是说明文字。"""

    label: str
    expr: str
    value: float

    def render(self) -> str:
        shown = f"{self.value:g}"
        return f"| {self.label} | {self.expr} | {shown} |"


@dataclass(frozen=True)
class ResistanceResult:
    """一种抗性的完整结算。所有字段都是 PoB 同名 output 的对应物。"""

    element_zh: str
    element_en: str
    is_elemental: bool
    #: 未夹上下限、已截断小数的总抗（PoB 的 `<elem>ResistTotal`）
    total: int
    #: 该抗性的最大抗性（PoB 的 `max`）
    max_resist: int
    #: 抗性下限（PoB 的 `min`，全局 −200）
    floor: int
    #: 实际生效的抗性（PoB 的 `<elem>Resist`）
    final: int
    #: 超出上限的部分（PoB 的 `<elem>ResistOverCap`）
    over_cap: int
    #: 距离上限还差多少（PoB 的 `Missing<elem>Resist`）
    missing: int
    #: 该类型伤害的承受倍率：1 − (final − 穿透)/100
    taken_mult: float
    #: 敌方穿透（逐字来自输入，0 表示未给）
    enemy_penetration: float
    steps: tuple[Step, ...]

    @property
    def capped(self) -> bool:
        return self.total >= self.max_resist

    @property
    def taken_pct(self) -> float:
        """承受伤害占原始伤害的百分比。"""
        return self.taken_mult * 100


def _join_terms(values: list[float]) -> str:
    """把一串加项写成算式。负数写成 ``- 60%`` 而不是 ``+ -60%``。

    减号一律用 ASCII：出口闸门要把答案里的数字回填到证据原文里比对，
    证据写全角「−60」而模型写「-60」就会对不上，用户看到的是一句被拦掉的正确话。
    """
    out = f"{values[0]:g}%"
    for value in values[1:]:
        sign = "-" if value < 0 else "+"
        out += f" {sign} {abs(value):g}%"
    return out


def _resolve(element: str) -> tuple[str, str]:
    """中文或英文名 → `(中文名, 英文名)`。认不出就抛异常。"""
    text = (element or "").strip()
    for zh, en in ELEMENTS_ZH.items():
        if text == zh or text.startswith(zh) or text.lower() == en.lower():
            return zh, en
    # 「元素抗性」这种整类问法由调用方拆成三次调用，本函数不替它决定
    raise UnknownElement(
        f"认不出抗性类型 {element!r}；只支持 {'、'.join(ELEMENTS_ZH)}"
    )


def max_resistance(
    element: str = "火焰",
    *,
    extra_max_specific: float = 0.0,
    extra_max_elemental: float = 0.0,
    max_override: float | None = None,
) -> tuple[int, tuple[Step, ...]]:
    """最大抗性 = ``min(90, 75 + 各项最大抗性加成)``，OVERRIDE 直接短路。

    照 `CalcDefence.lua:581`：
    ``max = modDB:Override(...) or m_min(MaxResistCap, modDB:Sum("BASE", ...))``。
    """
    _, element_en = _resolve(element)
    is_elemental = element_en in ELEMENTAL
    base_max = constants.get(constants.MAX_ALL_RESISTANCES)
    cap = constants.get(constants.MAX_RESIST_CAP)

    if max_override is not None:
        # OVERRIDE 在 `m_min(MaxResistCap, ...)` **之前**短路，所以它能超过 90
        # （`Melding of the Flesh` 就是这么工作的）。
        value = math.trunc(max_override)
        return value, (
            Step("最大抗性（被覆写）", f"OVERRIDE {max_override:g}%，不再受 {cap:g}% 上限约束", value),
        )

    applicable_elemental = extra_max_elemental if is_elemental else 0.0
    summed = base_max + extra_max_specific + applicable_elemental
    value = math.trunc(min(cap, summed))
    steps = [
        Step("基础最大抗性", f"游戏常量 {base_max:g}%", base_max),
    ]
    if extra_max_specific:
        steps.append(
            Step("该抗性的最大抗性加成", f"+{extra_max_specific:g}%", extra_max_specific)
        )
    if extra_max_elemental:
        if is_elemental:
            steps.append(
                Step("最大元素抗性加成", f"+{extra_max_elemental:g}%", extra_max_elemental)
            )
        else:
            # 这一步要**显式写出来**：静默丢掉它，用户会以为工具漏算了
            steps.append(
                Step(
                    "最大元素抗性加成（对混沌不生效）",
                    f"+{extra_max_elemental:g}% 只作用于火焰/冰霜/闪电，混沌抗性不吃",
                    0,
                )
            )
    steps.append(
        Step("最大抗性", f"min({cap:g}%, {summed:g}%)", value)
    )
    return value, tuple(steps)


def resistance(
    element: str = "火焰",
    *,
    base_specific: float = 0.0,
    base_elemental: float = 0.0,
    resist_penalty: float = 0.0,
    increased_pct: float = 0.0,
    more_mult: float = 1.0,
    campaign_penalty: float = 0.0,
    extra_max_specific: float = 0.0,
    extra_max_elemental: float = 0.0,
    max_override: float | None = None,
    enemy_penetration: float = 0.0,
) -> ResistanceResult:
    """按 PoB 原式算一种抗性，并给出逐步推导。

    参数对应 PoB 的 mod 类型，而不是「装备上写了什么」：

    * `base_specific`：该抗性的「+X% 抗性」之和，**含负数**（地图减抗、元素冲击、
      诅咒都是负的 BASE）。
    * `base_elemental`：「+X% 全部元素抗性」之和；混沌不吃。
    * `resist_penalty`：地图减抗词缀、诅咒、元素冲击等**负值**之和。它与
      `base_specific` 只差一个符号，单独开一项是为了**让调用方不必自己先减**——
      自己减掉之后，用户问题里的那个数（「我火抗 120」）就不在推导里了，
      于是出口闸门会把「120% − 60% = 60%」这句正确的话拦掉（第二十轮实测）。
    * `increased_pct` / `more_mult`：`calcLib.mod` 的 INC 与 MORE，乘在 BASE 之和上，
      且**整体不小于 0**（`m_max(..., 0)`）。这类词缀很少见，默认没有。
    * `campaign_penalty`：剧情抗性惩罚（0 / −30 / −60），PoB 把它当成四种抗性的
      BASE 加进去。**默认不加**：角色面板上看到的抗性已经含了它，替用户再减一次
      就是把一个对的数字算错。只有在「把装备上的词缀加起来」时才需要显式传入。
    * `enemy_penetration`：敌方穿透。它在**夹上限之后**才减，所以超抗防不住它。
    """
    element_zh, element_en = _resolve(element)
    is_elemental = element_en in ELEMENTAL
    floor = math.trunc(constants.get(constants.RESIST_FLOOR))

    max_resist, max_steps = max_resistance(
        element_zh,
        extra_max_specific=extra_max_specific,
        extra_max_elemental=extra_max_elemental,
        max_override=max_override,
    )

    applicable_elemental = base_elemental if is_elemental else 0.0
    base_sum = base_specific + applicable_elemental + resist_penalty + campaign_penalty
    inc = max((1 + increased_pct / 100) * more_mult, 0.0)
    total = math.trunc(base_sum * inc)
    final = max(min(total, max_resist), floor)
    over_cap = max(0, total - max_resist)
    missing = max(0, max_resist - final)
    taken_mult = 1 - (final - enemy_penetration) / 100

    steps: list[Step] = [
        Step(f"{element_zh}抗性加总", f"{base_specific:g}%", base_specific),
    ]
    if base_elemental:
        if is_elemental:
            steps.append(
                Step("全部元素抗性加成", f"+{base_elemental:g}%", base_elemental)
            )
        else:
            steps.append(
                Step(
                    "全部元素抗性加成（对混沌不生效）",
                    f"+{base_elemental:g}% 只作用于火焰/冰霜/闪电",
                    0,
                )
            )
    if resist_penalty:
        steps.append(
            Step("减抗 / 诅咒等负值", f"{resist_penalty:g}%", resist_penalty)
        )
    if campaign_penalty:
        steps.append(
            Step(
                "剧情抗性惩罚",
                f"{campaign_penalty:g}%（{CAMPAIGN_PENALTIES.get(int(campaign_penalty), '自定义档')}）",
                campaign_penalty,
            )
        )
    if len(steps) > 1:
        terms = [base_specific]
        if base_elemental:
            terms.append(applicable_elemental)
        if resist_penalty:
            terms.append(resist_penalty)
        if campaign_penalty:
            terms.append(campaign_penalty)
        steps.append(Step("加总", _join_terms(terms), base_sum))
    if increased_pct or more_mult != 1.0:
        steps.append(
            Step(
                "增加/更多的抗性",
                f"max((1 + {increased_pct:g}/100) × {more_mult:g}, 0) = {inc:g}",
                inc,
            )
        )
    steps.append(
        Step(
            "总抗性（小数截断）",
            f"截断({base_sum:g}% × {inc:g})" if inc != 1.0 else f"截断({base_sum:g}%)",
            total,
        )
    )
    steps.extend(max_steps)
    steps.append(
        Step(
            "实际生效抗性",
            f"max(min({total}%, {max_resist}%), {floor}%)",
            final,
        )
    )
    if over_cap:
        steps.append(
            Step("超出上限（超抗）", f"{total}% - {max_resist}% = {over_cap}%", over_cap)
        )
    if missing:
        steps.append(
            Step("距上限还差", f"{max_resist}% - {final}% = {missing}%", missing)
        )
    if enemy_penetration:
        steps.append(
            Step(
                "承受倍率（含穿透）",
                f"1 - ({final}% - {enemy_penetration:g}%) / 100",
                taken_mult,
            )
        )
    else:
        steps.append(Step("承受倍率", f"1 - {final}% / 100", taken_mult))

    return ResistanceResult(
        element_zh=element_zh,
        element_en=element_en,
        is_elemental=is_elemental,
        total=total,
        max_resist=max_resist,
        floor=floor,
        final=final,
        over_cap=over_cap,
        missing=missing,
        taken_mult=taken_mult,
        enemy_penetration=enemy_penetration,
        steps=tuple(steps),
    )


def pob_url(
    constant_url: str, path: str, *, line: int | None = None, lines: tuple[int, int] | None = None
) -> str:
    """从常量的引用 URL 推出同一 commit 下另一个文件的引用。

    为什么要推而不是写死：引用必须与**入库那一刻的 commit** 一致。写死一个 commit
    的话，重新导入常量后引用就指向另一份代码，而读者看不出来——那正是钉 commit
    要防的事。常量与公式来自同一份检出，所以同一个 commit 是可证的。

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


ElementName = Literal["火焰", "冰霜", "闪电", "混沌"]
