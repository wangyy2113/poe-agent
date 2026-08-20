"""暴击：暴击率、暴击加成，以及它们合起来对平均伤害的贡献。

## 三条容易记错的

1. **暴击率上限是 100%**，不是社区常说的 95%——PoB 给 `CritChanceCap` 播的基础值
   就是 100（`CalcSetup.lua:110`）。
2. **基础暴击加成 150%**（`characterConstants.base_critical_strike_multiplier`）。
   PoB 内部把它存成 `+50`（`CalcSetup.lua:501` 减了 100），显示时才是 150%——
   照抄内部值会少一整倍。
3. **「增加的暴击率」乘在基础暴击上**：``(基础 + 定值)×(1 + 增加/100)× 更多``。
   所以基础暴击越低，百分比加成越不值——这是「爪 vs 法杖」那类比较的算术来源。

## 平均伤害倍率

    平均倍率 =(1 − 暴击率)+ 暴击率 × 暴击加成          （PoB 的 `CritEffect`）

这个数**不需要知道这一击有多大**，所以它是攻击侧唯一能在「伤害数值未采集」的前提下
如实算出来的东西：40% 暴击 + 350% 加成 ⇒ 0.6 + 0.4 × 3.5 = **2.0**，也就是平均伤害翻倍。

## 基础值从库里查

技能的每级基础暴击在 `pob_skill_level.crit_chance`（7600 行），武器底材的
`CritChanceBase` 在 `pob_base_item.stats` 里。两者都不用用户报数。

## 不做的事

- 不算「幸运暴击」（`CritChanceLucky` 两次取高）、不算暴击的命中判定、
  不算暴击对异常的加成（`CritPoisonDotMulti` 那一套）。
- **不算这一击有多大**：那要 PoB 的 effectiveness 公式与整条攻击链，
  `pob_skill_level` 刻意没有伤害列（判据见 `core/db.py` 的表注释）。
  所以本模块给的是**倍率**，不是 DPS。
"""

from __future__ import annotations

from dataclasses import dataclass

from calc import constants
from calc.derive import Step

#: PoB 原文位置。
OFFENCE_FILE = "src/Modules/CalcOffence.lua"
SETUP_FILE = "src/Modules/CalcSetup.lua"
CHANCE_LINES = (2926, 2940)
EFFECT_LINE = 3090
CAP_LINE = 110
BASE_MULTI_LINE = 501

#: 暴击率上限。**硬编码在上游代码里**（`CalcSetup.lua:110` 给 `CritChanceCap` 播 100），
#: 不在常量表——所以由单测对着原文钉住，与头目 7.68、剧情惩罚 −60 同一处理。
#: 社区常说的「95% 上限」在 PoB 3.29 的数据里不成立。
CRIT_CHANCE_CAP = 100.0


@dataclass(frozen=True)
class CritResult:
    base_chance: float
    flat_chance: float
    increased_pct: float
    more_mult: float
    #: 未夹上限的暴击率
    raw_chance: float
    chance: float
    cap: float
    multi_pct: float
    #: 平均伤害倍率（PoB 的 `CritEffect`）
    average_multiplier: float
    steps: tuple[Step, ...]

    @property
    def capped(self) -> bool:
        return self.raw_chance > self.cap

    @property
    def damage_gain_pct(self) -> float:
        """相对「完全不暴击」多出来的百分比。"""
        return (self.average_multiplier - 1) * 100


def crit(
    base_chance: float,
    *,
    flat_chance: float = 0.0,
    increased_pct: float = 0.0,
    more_mult: float = 1.0,
    multi_pct: float | None = None,
    extra_multi_pct: float = 0.0,
) -> CritResult:
    """暴击率与平均伤害倍率。

    * `base_chance`：技能或武器的**基础**暴击率（库里查得到）。
    * `flat_chance`：「+X% 暴击率」这类定值加值（罕见，多来自珠宝/技能）。
    * `increased_pct` / `more_mult`：「暴击率提高」与「更多暴击率」。
    * `multi_pct`：暴击加成。不给就用基础值 150%，再加上 `extra_multi_pct`。
    """
    cap = CRIT_CHANCE_CAP
    base_multi = constants.get(constants.BASE_CRIT_MULTIPLIER)
    multi = (multi_pct if multi_pct is not None else base_multi) + extra_multi_pct

    raw = (base_chance + flat_chance) * (1 + increased_pct / 100) * more_mult
    chance = max(min(raw, cap), 0.0)
    portion = chance / 100
    average = 1 - portion + portion * (multi / 100)

    steps = [Step("基础暴击率", f"{base_chance:g}%", base_chance)]
    if flat_chance:
        steps.append(Step("定值暴击率加值", f"+ {flat_chance:g}%", flat_chance))
    if increased_pct:
        steps.append(
            Step("增加的暴击率", f"×(1 + {increased_pct:g}/100)", 1 + increased_pct / 100)
        )
    if more_mult != 1.0:
        steps.append(Step("更多的暴击率", f"× {more_mult:g}", more_mult))
    steps.append(
        Step(
            "暴击率",
            f"min({raw:.2f}%, 上限 {cap:g}%)" if raw > cap else f"{raw:.2f}%",
            round(chance, 2),
        )
    )
    if multi_pct is None and extra_multi_pct:
        steps.append(
            Step("暴击加成", f"基础 {base_multi:g}% + {extra_multi_pct:g}%", multi)
        )
    else:
        steps.append(Step("暴击加成", f"{multi:g}%", multi))
    steps.append(
        Step(
            "平均伤害倍率",
            f"(1 - {portion:.4f}) + {portion:.4f} × {multi / 100:g}",
            round(average, 4),
        )
    )
    return CritResult(
        base_chance=float(base_chance), flat_chance=flat_chance,
        increased_pct=increased_pct, more_mult=more_mult, raw_chance=raw,
        chance=chance, cap=cap, multi_pct=multi, average_multiplier=average,
        steps=tuple(steps),
    )
