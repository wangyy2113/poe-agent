"""攻击速度：每秒几次，以及一次要多久。

## 上游的顺序（`CalcOffence.lua:2230-2241` 与 `:2337`）

    武器攻速 = 底材基础攻速 ×(1 + 技能的 attackSpeedMultiplier / 100)
    基础间隔 = 1 / 武器攻速
    每秒次数 = 1 /(基础间隔 ÷ round((1 + 增加% / 100)× 更多倍率, 2))

两个容易记错的地方，都由上游的写法决定：

* **技能的攻速倍率乘在武器攻速上**（不是乘在最终每秒次数上），而 PoB 存的
  `attackSpeedMultiplier` 是**增量**（存 -30 表示 70%），显示口径见
  `Classes/GemTooltip.lua`。二连击存 `attackSpeedMultiplier`，双持不在本模块范围。
* **`round(…, 2)` 在分母上**：先把「(1+增加)×更多」四舍五入到两位小数，再去除基础间隔。
  照直算不取整会在高攻速时差出一次攻击（与 `calc/reservation.py` 的取整同一类坑）。

## PoB 没有给每秒次数设上限

社区常说「攻速有服务器 tick 上限（1/0.033 ≈ 30.3 次/秒）」。`data.misc.ServerTickRate`
确实是这个数，但在 PoB 里它**只出现在创伤（Trauma）叠层的可维持计算里**
（`CalcOffence.lua:2250`），`output.Speed` 本身不夹上限。本模块复算的是 PoB，
所以也不夹——但把这条差异写进证据，不让用户以为 PoB 帮他算过 tick 上限。

## 刻意不做

双持（两把武器交替、各自攻速）、`SkillAttackTime`（部分技能覆盖攻击时间）、
`TotalAttackTime` 之类的定值加时、行动速度（冰缓/致盲）与图腾/陷阱的施放速率。
这些都在上游的同一段里，但每一项都要额外的输入，猜不得。
"""

from __future__ import annotations

from dataclasses import dataclass

from calc.derive import Step

OFFENCE_FILE = "src/Modules/CalcOffence.lua"
#: 武器攻速 × 技能倍率 → 基础间隔
BASE_TIME_LINES = (2230, 2241)
#: 每秒次数（注意 round(…, 2) 在分母里）
SPEED_LINE = 2337
#: 服务器 tick 只在创伤那一段出现，不是 Speed 的上限
TRAUMA_CAP_LINE = 2250


@dataclass(frozen=True)
class SpeedResult:
    #: 武器底材的基础攻速（次/秒）
    base_rate: float
    #: 技能的攻速倍率增量（PoB 原值：-30 表示「基础攻速的 70%」）
    skill_multiplier: float
    increased_pct: float
    more_mult: float
    #: 技能倍率之后的武器攻速
    weapon_rate: float
    #: 每秒攻击次数
    rate: float
    #: 一次攻击的间隔（秒）
    interval: float
    steps: tuple[Step, ...]


def attack_rate(
    base_rate: float,
    *,
    skill_multiplier: float = 0.0,
    increased_pct: float = 0.0,
    more_mult: float = 1.0,
) -> SpeedResult:
    """每秒攻击次数。

    * `base_rate`：武器底材的基础攻速（`pob_base_item.stats.AttackRateBase`）。
    * `skill_multiplier`：技能的 `attackSpeedMultiplier`**增量**（存 -30 = 70%）。
    * `increased_pct`：所有「攻击速度加快 X%」的加总（加算）。
    * `more_mult`：「更多攻击速度」这类乘算修正，1.0 表示没有。
    """
    if base_rate <= 0:
        raise ValueError("武器基础攻速必须大于 0——没有武器就没有攻击速度这一维")
    weapon_rate = base_rate * (1 + skill_multiplier / 100)
    base_time = 1 / weapon_rate
    multiplier = round((1 + increased_pct / 100) * more_mult, 2)
    if multiplier <= 0:
        raise ValueError("攻速修正总量为 0 或负数，PoB 在这里会得到无穷长的攻击间隔")
    interval = base_time / multiplier
    rate = 1 / interval

    steps = [Step("武器基础攻速", f"{base_rate:g} 次/秒", base_rate)]
    if skill_multiplier:
        steps.append(
            Step(
                "技能攻速倍率",
                f"{base_rate:g} × (1 + {skill_multiplier:g}% / 100)"
                f" = 基础攻速的 {100 + skill_multiplier:g}%",
                round(weapon_rate, 4),
            )
        )
    steps.append(Step("基础攻击间隔", f"1 / {weapon_rate:g}", round(base_time, 4)))
    steps.append(
        Step(
            "攻速修正（先取整到两位）",
            f"round((1 + {increased_pct:g}% / 100) × {more_mult:g}, 2)",
            multiplier,
        )
    )
    steps.append(Step("攻击间隔", f"{base_time:.4f} ÷ {multiplier:g}", round(interval, 4)))
    steps.append(Step("每秒攻击次数", f"1 / {interval:.4f}", round(rate, 2)))
    return SpeedResult(
        base_rate=float(base_rate),
        skill_multiplier=float(skill_multiplier),
        increased_pct=float(increased_pct),
        more_mult=float(more_mult),
        weapon_rate=weapon_rate,
        rate=round(rate, 2),
        interval=round(interval, 4),
        steps=tuple(steps),
    )
