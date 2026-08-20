"""Max Hit：一发最多能挨多大的伤害。

## 它是前六块计算器的合流

一发打到身上，依次经过：**抗性**（`calc/resistance.py`）→
**护甲与物理减伤**（`calc/mitigation.py`）→ 生命/护盾池。
Max Hit 就是把这条链**反解**：给定池子，问原始伤害最大能是多少。

没有护甲时是一步除法：

    最大承受伤害 = 池 ÷（抗性倍率 × 减伤倍率 × 承受倍率）− 定值承伤

有护甲时不是——因为护甲减伤本身依赖伤害大小（`护甲 /(护甲 + 5 × 伤害)`），
反解要解一个二次方程。PoB 把它写成了闭式（`CalcDefence.lua:3198-3214`）：

    a = 5 ×(1 − 定值减伤 + 压制)× 承受倍率 ×(抗性倍率 × 转化)²
    b = ((压制 − 定值减伤)× 有效护甲 × 承受倍率 − 5 ×(池 − 定值承伤 × 承受倍率))× 抗性倍率 × 转化
    c = −有效护甲 ×(池 − 定值承伤 × 承受倍率)
    原始伤害 = (√(b² − 4ac) − b) ÷ 2a

再夹在两个边界之间：「完全不算护甲」与「护甲吃满减伤上限」。

## 这一块的验证方式：自洽回代

上游没有可对账的期望值，所以这里用**回代**：把算出来的 Max Hit 重新走一遍
「抗性 → 护甲 → 池」的正向链路，结果必须正好等于池子。
`tests/unit/test_maxhit.py` 对一批参数组合都做这件事——公式抄错、
系数写反、上下限夹反，回代都会立刻对不上。

## 刻意不做的部分（这是本模块最重要的边界）

PoB 的完整 Max Hit 还包含**伤害转化**（一发伤害按 damageShiftTable 分成多种类型）、
守护技能吸收、灵体、Ward、以及 **Mind over Matter**（伤害分流到魔力，池子要按
`poolProtected` 重新折算）。它自己的注释写着：完整解「可能要解一个六次多项式」，
所以它对多类型转化用的是**多轮平滑逼近**（`maxHitSmoothingPasses`）。

本模块只做**单一伤害类型、100% 不转化**的那一支：这一支在 PoB 里是闭式解，
可以精确复算。其余情形工具会明确说「不算」，而不是拿这一支的结果冒充。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from calc import constants
from calc.derive import Step, fmt

#: PoB 原文位置。
DEFENCE_FILE = "src/Modules/CalcDefence.lua"
SIMPLE_LINES = (3175, 3179)
QUADRATIC_LINES = (3198, 3214)
#: 完整解为什么做不到——PoB 自己的注释（六次多项式那段）。
COMMENT_LINE = 3196


@dataclass(frozen=True)
class MaxHitResult:
    pool: float
    resist_pct: float
    armour: float
    flat_reduction_pct: float
    taken_mult: float
    overwhelm_pct: float
    #: 抗性倍率 = 1 − 抗性/100
    resist_mult: float
    #: 不算护甲时能挨多少（也是有护甲时的下界）
    without_armour: float
    #: 护甲吃满减伤上限时能挨多少（上界）
    with_max_reduction: float
    max_hit: float
    #: 这一发实际会被护甲挡掉多少（回代出来的）
    armour_reduction_pct: float
    #: 相当于裸池的多少倍
    effective_multiplier: float
    steps: tuple[Step, ...]

    @property
    def armour_capped(self) -> bool:
        return self.max_hit >= self.with_max_reduction


def _bounds(
    pool: float, resist_mult: float, taken_mult: float, taken_flat: float,
    overwhelm_pct: float, reduction_cap: float,
) -> tuple[float, float]:
    """两个边界：完全不算护甲、以及护甲吃满减伤上限。照 `CalcDefence.lua:3211-3212`。"""
    if resist_mult <= 0 or taken_mult <= 0 or pool <= 0:
        return 0.0, 0.0
    no_dr = pool / resist_mult / taken_mult * (1 - taken_flat * taken_mult / pool)
    room = 1 - (reduction_cap - overwhelm_pct) / 100
    max_dr = no_dr / room if room > 0 else math.inf
    return no_dr, max_dr


def max_hit(
    pool: float,
    *,
    resist_pct: float = 0.0,
    armour: float = 0.0,
    flat_reduction_pct: float = 0.0,
    taken_mult: float = 1.0,
    taken_flat: float = 0.0,
    overwhelm_pct: float = 0.0,
) -> MaxHitResult:
    """一发最多能挨多大的原始伤害（单一伤害类型、不转化）。

    * `pool`：这一发能吃掉的池子（生命 + 能量护盾；本模块不做 MoM 的池保护折算）。
    * `resist_pct`：该类型的**实际生效抗性**（用 `calc/resistance.py` 算出来的那个）。
    * `armour` / `flat_reduction_pct`：护甲与另有的物理减伤。护甲只对物理生效，
      调用方负责别把它用在元素伤害上。
    * `taken_mult`：「受到的伤害减少/增加」等乘算修正（0.8 表示少受 20%）。
    * `overwhelm_pct`：敌方的「压制」（无视物理减伤）。
    """
    reduction_cap = constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION)
    resist_mult = 1 - resist_pct / 100
    flat_dr = flat_reduction_pct / 100

    steps = [
        Step("抗性倍率", f"1 - {resist_pct:g}% / 100", round(resist_mult, 4)),
    ]
    if taken_mult != 1.0:
        steps.append(Step("承受倍率修正", f"× {taken_mult:g}", taken_mult))

    no_dr, max_dr = _bounds(
        pool, resist_mult, taken_mult, taken_flat, overwhelm_pct, reduction_cap
    )
    steps.append(
        Step(
            "不算护甲时能挨",
            f"{fmt(pool)} ÷ {resist_mult:g} ÷ {taken_mult:g}"
            + (f" ×(1 - {taken_flat:g} × {taken_mult:g} / {fmt(pool)})" if taken_flat else ""),
            round(no_dr, 2),
        )
    )

    if armour <= 0 and not flat_dr:
        result = no_dr
        armour_reduction = 0.0
    elif armour <= 0:
        # 只有定值减伤：仍是一步除法（护甲那一项为 0，二次方程退化）
        result = no_dr / (1 - max(min(flat_dr - overwhelm_pct / 100, reduction_cap / 100), 0))
        armour_reduction = flat_reduction_pct
        steps.append(
            Step("定值物理减伤", f"÷ (1 - {flat_reduction_pct:g}% / 100)", round(result, 2))
        )
    else:
        # 护甲减伤依赖伤害本身 → 解二次方程（PoB 的闭式）
        resist_x_convert = resist_mult  # 单一类型、100% 转化 ⇒ 转化系数为 1
        pool_after_flat = pool - taken_flat * taken_mult
        a = 5 * (1 - flat_dr + overwhelm_pct / 100) * taken_mult * resist_x_convert ** 2
        b = (
            (overwhelm_pct / 100 - flat_dr) * armour * taken_mult
            - 5 * pool_after_flat
        ) * resist_x_convert
        c = -armour * pool_after_flat
        raw = (
            (math.sqrt(max(b * b - 4 * a * c, 0)) - b) / (2 * a) if a else math.inf
        )
        result = math.floor(max(min(raw, max_dr), no_dr))
        steps.append(
            Step(
                "二次方程的系数",
                f"a = {a:.4g}，b = {b:.4g}，c = {c:.4g}",
                round(raw, 2),
            )
        )
        steps.append(
            Step(
                "护甲吃满减伤上限时能挨",
                f"{fmt(no_dr)} ÷ (1 - ({reduction_cap:g}% - {overwhelm_pct:g}%) / 100)",
                round(max_dr, 2),
            )
        )
        steps.append(
            Step(
                "夹在两个边界之间并向下取整",
                f"floor(min(max({fmt(raw)}, {fmt(no_dr)}), {fmt(max_dr)}))",
                result,
            )
        )
        # 回代出这一发实际被护甲挡掉多少：护甲减伤看的是**过抗性之后**的伤害
        after_resist = result * resist_mult
        armour_reduction = min(
            armour / (armour + 5 * after_resist) * 100 + flat_reduction_pct
            - overwhelm_pct,
            reduction_cap,
        ) if after_resist > 0 else 0.0

    multiplier = result / pool if pool else 0.0
    steps.append(
        Step("相当于裸池的倍数", f"{fmt(result)} ÷ {fmt(pool)}", round(multiplier, 2))
    )
    return MaxHitResult(
        pool=float(pool), resist_pct=resist_pct, armour=float(armour),
        flat_reduction_pct=flat_reduction_pct, taken_mult=taken_mult,
        overwhelm_pct=overwhelm_pct, resist_mult=resist_mult,
        without_armour=no_dr, with_max_reduction=max_dr, max_hit=float(result),
        armour_reduction_pct=armour_reduction, effective_multiplier=multiplier,
        steps=tuple(steps),
    )


def taken_from(
    raw_damage: float,
    *,
    resist_pct: float = 0.0,
    armour: float = 0.0,
    flat_reduction_pct: float = 0.0,
    taken_mult: float = 1.0,
    taken_flat: float = 0.0,
    overwhelm_pct: float = 0.0,
) -> float:
    """正向链路：一发 `raw_damage` 打到身上，实际掉多少池。

    这是 `max_hit` 的**回代验证器**——两者必须互为反函数
    （判据见 `tests/unit/test_maxhit.py` 的 `RoundTripTest`）。
    照 `CalcDefence.lua:1971-2050` 的顺序：抗性 → 护甲/减伤 → 定值承伤 → 乘算修正。
    """
    reduction_cap = constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION)
    after_resist = raw_damage * (1 - resist_pct / 100)
    reduction = flat_reduction_pct
    if armour > 0 and after_resist > 0:
        reduction += armour / (armour + 5 * after_resist) * 100
    reduction = max(min(reduction - overwhelm_pct, reduction_cap), 0)
    return (after_resist * (1 - reduction / 100) + taken_flat) * taken_mult
