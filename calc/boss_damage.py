"""Boss 技能的一击伤害：按 PoB 的预设系数与怪物伤害表复算。

## 这块补的是「那一击有多大」，不是打法

`max_hit_calc` 一直要用户自报「这一击多少伤害」——那个数以前只能去别处查。
PoB 的 `Data/BossSkills.lua` 给了每个 Boss 技能的伤害系数，配上库里早就有的
`monsterDamageTable`，就能把那一击算出来。

**它不是攻略**：不含技能机制、阶段、站位与应对，那些本地没有来源。

## 公式照抄上游

`Modules/ConfigOptions.lua:2258`：

    round(monsterDamageTable[enemyLevel] * (damageMult[1] + rollRangeMult * damageMult[2])
          * bossData.UberDamageMultiplier)

* `damageMult = [基准, 每 1% roll 的增量]`；
* `rollRangeMult` 是伤害掷骰区间 0..100，PoB 里是一个配置项；
* Uber 变体另乘 `UberDamageMultiplier`（没有这一项就是不乘）。

## 掷骰区间不替用户假定

「谢勒的球打多少伤害」的**诚实答案是一个区间**：`rollRange=0` 是下界、`100` 是上界。
所以默认两端都给，只有用户明确指定时才算单点——与「不替用户假定打 83 级」
「不替用户假定稀有」同一条。

## 等级越界抛异常，不外推

`monsterDamageTable` 只有 1..100 级。越界时抛错而不是拿最后一格顶上：
外推出来的数字看起来完全正常，而它没有任何依据。

## 数字是 PoB 的建模值

这一维**只有 PoB 一个来源**（P2），GGG 没有公布 Boss 技能的伤害系数。
证据里要写明这一点，不能让它读起来像官方数值。
"""

from __future__ import annotations

from dataclasses import dataclass

from calc import constants
from calc.derive import Step

#: 怪物伤害表。等级 → 该等级的基准伤害。
DAMAGE_TABLE = "monsterDamageTable"

#: 掷骰区间的两端。PoB 的配置项是 0..100。
ROLL_MIN = 0.0
ROLL_MAX = 100.0

#: 公式出处，写进证据。
FORMULA_SOURCE = "Modules/ConfigOptions.lua:2258"


class BossDamageError(ValueError):
    """输入不足或越界。**不外推、不假定**。"""


@dataclass(frozen=True)
class BossHit:
    """某个技能在某等级打出的一击。"""

    damage_type: str
    roll_range: float
    damage: float
    steps: tuple[Step, ...]


def _table_value(level: int) -> float:
    table = constants.table(DAMAGE_TABLE)
    if not 1 <= level <= len(table):
        raise BossDamageError(
            f"怪物伤害表只有 1..{len(table)} 级，问到的是 {level} 级；"
            "本表不外推——外推出来的数字看起来正常，但没有依据"
        )
    return float(table[level - 1])


def hit_damage(
    *,
    level: int,
    base_mult: float,
    roll_mult: float,
    damage_type: str,
    roll_range: float,
    uber_mult: float | None = None,
) -> BossHit:
    """按上游原式算一击伤害。`roll_range` 是 0..100 的掷骰区间。"""
    if not ROLL_MIN <= roll_range <= ROLL_MAX:
        raise BossDamageError(
            f"掷骰区间必须在 {ROLL_MIN:g}..{ROLL_MAX:g} 之间，收到 {roll_range:g}"
        )
    table_value = _table_value(level)
    factor = base_mult + roll_range * roll_mult
    raw = table_value * factor
    steps = [
        Step("怪物伤害表", f"{DAMAGE_TABLE}[{level}]", table_value),
        Step(
            "技能倍率",
            f"{base_mult:g} + {roll_range:g} × {roll_mult:g}",
            factor,
        ),
        Step("未取整伤害", f"{table_value:g} × {factor:g}", raw),
    ]
    if uber_mult is not None:
        raw *= uber_mult
        steps.append(Step("Uber 倍率", f"× {uber_mult:g}", raw))
    damage = float(round(raw))
    steps.append(Step("取整", f"round({raw:g})", damage))
    return BossHit(
        damage_type=damage_type,
        roll_range=roll_range,
        damage=damage,
        steps=tuple(steps),
    )


def hit_range(
    *,
    level: int,
    base_mult: float,
    roll_mult: float,
    damage_type: str,
    uber_mult: float | None = None,
) -> tuple[BossHit, BossHit]:
    """掷骰区间两端。**默认给区间而不是单点**，理由见模块 docstring。"""
    low = hit_damage(
        level=level, base_mult=base_mult, roll_mult=roll_mult,
        damage_type=damage_type, roll_range=ROLL_MIN, uber_mult=uber_mult,
    )
    high = hit_damage(
        level=level, base_mult=base_mult, roll_mult=roll_mult,
        damage_type=damage_type, roll_range=ROLL_MAX, uber_mult=uber_mult,
    )
    return low, high
