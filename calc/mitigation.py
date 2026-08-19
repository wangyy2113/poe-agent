"""受击结算：打不中（闪避 / 格挡 / 法术压制）与打中了减多少（护甲）。

## 为什么这几件事要放一起

它们回答的是同一个问题的四段：一次攻击落到角色身上，先看**能不能命中**（闪避），
命中了看**能不能挡下来**（格挡）或**削弱**（法术压制），剩下的伤害才进减伤
（护甲 / 物理减伤）与抗性（`calc/resistance.py`）。**顺序**决定结论，
而顺序在 PoB 的代码里、不在数据里——与抗性那一轮同一个理由。

## 三条最容易记错的事，都是代码说的

1. **护甲不是固定百分比减伤。** 减伤 = `护甲 /（护甲 + 5 × 这一击的物理伤害）`，
   所以「10000 护甲减多少伤害」这个问题**在不给伤害数值时没有答案**。
   本模块因此把「这一击多大」列为必需输入，不给就不算。
2. **命中率有 5% 的下限，闪避率有 95% 的上限。** 闪避堆到无穷也躲不掉全部攻击
   （`hitChance` 的 `m_max(m_min(round(rawChance), 100), 5)` 与 `EvadeChanceCap`）。
3. **法术压制减的是 40%，不是 50%**（`SuppressionEffect = 40`）。这是社区里最常见的记错。

## 怪物命中值直接取库里的等级表

`monsterAccuracyTable` 早就在 `game_constant` 里（100 级，来自 `DefaultMonsterStats.dat`），
而 PoB 给敌人加命中就是 `enemyDB:NewMod("Accuracy", "BASE", monsterAccuracyTable[enemyLevel])`。
所以「85 级怪物打我」这类问题不需要用户提供命中值——但**用了哪一级要写在推导里**。

## 不做的事

- **不算头目的护甲 / 闪避倍率**：PoB 的 `data.bossStats` 是在载入时按一份头目清单
  现算的均值（`100 + armourTotal / count`），不是字面常量，本库没有那份清单。
- 不算命中率的攻击方视角（角色打怪物的命中）：那要角色命中值与怪物闪避表，
  是攻击侧的活，与这里的防御视角参数不同。
- 不算闪避的「不能被闪避」「必定命中」等旗标，也不读 build。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from calc import constants
from calc.derive import Step

#: PoB 的权威实现位置。引用必须带行号锚点，点开就是这几行。
DEFENCE_FILE = "src/Modules/CalcDefence.lua"
HIT_CHANCE_LINES = (57, 63)
ARMOUR_LINES = (65, 76)
BLOCK_LINES = (660, 694)
EVADE_LINES = (1071, 1085)
SUPPRESSION_LINES = (1115, 1130)

#: 命中率的下限。`hitChance` 结尾 `m_max(m_min(round(rawChance), 100), 5)`。
#: 它与 `EvadeChanceCap = 95` 是同一件事的两面：闪避堆到无穷也总有 5% 会被命中。
HIT_CHANCE_FLOOR = 5


class OutOfTableRange(ValueError):
    """怪物等级超出等级表覆盖范围。不外推——表外的值是编造。"""


@dataclass(frozen=True)
class HitChanceResult:
    """一次攻击的命中 / 闪避结算。数值口径与 PoB 同名 output 一致。"""

    evasion: float
    accuracy: float
    monster_level: int | None
    #: PoB 的 `calcs.hitChance`：已取整并夹在 5..100
    hit_chance: int
    #: PoB 的 `output.EvadeChance`，夹在 0..95
    evade_chance: float
    evade_cap: float
    steps: tuple[Step, ...]

    @property
    def capped(self) -> bool:
        return self.evade_chance >= self.evade_cap


@dataclass(frozen=True)
class ArmourResult:
    """护甲对**这一击**的减伤。"""

    armour: float
    hit_damage: float
    #: 未夹上限的原始减伤（PoB 的 `armourReductionF`，未取整）
    raw_reduction: float
    #: 取整并夹上限后的减伤（PoB 的 `armourReduction` + `DamageReductionMax`）
    reduction: int
    reduction_cap: float
    #: 减伤后这一击还剩多少伤害
    damage_after: float
    steps: tuple[Step, ...]

    @property
    def capped(self) -> bool:
        return self.raw_reduction >= self.reduction_cap


@dataclass(frozen=True)
class BlockResult:
    """格挡几率与它的上限。攻击格挡与法术格挡是两套数，但公式相同。"""

    total: float
    max_block: int
    chance: float
    over_cap: float
    hard_cap: float
    base_max: float
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class SuppressionResult:
    """法术压制：几率与效果。"""

    chance: float
    chance_cap: float
    effect: float
    #: 期望减伤 = 几率 × 效果
    average_reduction: float
    steps: tuple[Step, ...]


def monster_accuracy(level: int) -> float:
    """某等级怪物的命中值，取自 `monsterAccuracyTable`。

    越界即抛异常，不做截断——表外的命中值是编造。
    """
    try:
        return constants.at_level(constants.MONSTER_ACCURACY_TABLE, int(level))
    except constants.MissingConstant as exc:
        raise OutOfTableRange(str(exc)) from exc


def hit_chance(evasion: float, accuracy: float) -> int:
    """PoB 的 `calcs.hitChance`：``round(acc /(acc +(eva / 5) ^ 0.9) * 125)``，夹在 5..100。

    命中值为负时 PoB 直接返回 5（下限），这里照抄。

    命中值与闪避**同时为 0** 时上游是 `0 / 0`（未定义行为），这里取 125 → 夹成 100%：
    「没有闪避」的含义就是必被命中。这是本模块唯一一处上游没有定义、由我们选的分支，
    所以写在这里，而不是留给读者猜。
    """
    if accuracy < 0:
        return HIT_CHANCE_FLOOR
    raw = accuracy / (accuracy + (evasion / 5) ** 0.9) * 125 if (evasion or accuracy) else 125
    return int(max(min(round(raw), 100), HIT_CHANCE_FLOOR))


def evade(
    evasion: float,
    *,
    accuracy: float | None = None,
    monster_level: int | None = None,
    flat_evade_chance: float = 0.0,
    increased_evade_pct: float = 0.0,
) -> HitChanceResult:
    """闪避率。命中值可以直接给，也可以给怪物等级由等级表查。

    照 `CalcDefence.lua:1083`：
    ``EvadeChance = clamp((100 -(hitChance(eva, acc) - 固定闪避几率)) × inc, 0, 95)``。
    """
    if accuracy is None:
        if monster_level is None:
            raise ValueError("要么给敌方命中值，要么给怪物等级——两个都没有就没法算")
        accuracy = monster_accuracy(monster_level)
    cap = constants.get(constants.EVADE_CHANCE_CAP)

    chance_to_hit = hit_chance(evasion, accuracy)
    inc = 1 + increased_evade_pct / 100
    raw_evade = (100 - (chance_to_hit - flat_evade_chance)) * inc
    final = max(min(cap, raw_evade), 0.0)

    steps: list[Step] = []
    if monster_level is not None:
        steps.append(
            Step(
                f"{monster_level} 级怪物的命中值",
                f"等级表第 {monster_level} 项",
                accuracy,
            )
        )
    steps.append(
        Step(
            "命中率",
            f"取整({accuracy:g} / ({accuracy:g} + ({evasion:g} / 5) ^ 0.9) × 125)"
            f"，夹在 {HIT_CHANCE_FLOOR}%..100%",
            chance_to_hit,
        )
    )
    if flat_evade_chance:
        steps.append(
            Step("固定闪避几率", f"命中率再减 {flat_evade_chance:g}%", flat_evade_chance)
        )
    if increased_evade_pct:
        steps.append(
            Step("增加的闪避几率", f"× (1 + {increased_evade_pct:g} / 100)", inc)
        )
    steps.append(
        Step(
            "闪避率",
            f"min({cap:g}%, 100% - {chance_to_hit}%"
            + (f" + {flat_evade_chance:g}%" if flat_evade_chance else "")
            + (f"，再 × {inc:g}" if increased_evade_pct else "")
            + ")",
            final,
        )
    )
    return HitChanceResult(
        evasion=float(evasion),
        accuracy=float(accuracy),
        monster_level=monster_level,
        hit_chance=chance_to_hit,
        evade_chance=final,
        evade_cap=cap,
        steps=tuple(steps),
    )


def armour(armour_value: float, hit_damage: float, *, flat_reduction: float = 0.0) -> ArmourResult:
    """护甲对一次 `hit_damage` 大小的物理打击减多少伤害。

    照 `CalcDefence.lua:66-76`：``护甲 /(护甲 + 5 × 伤害) × 100``，取整，
    再与「物理减伤」相加后夹在 `maximum_physical_damage_reduction_%`（90%）以内。

    `hit_damage` 必须由调用方给：**减伤依赖这一击有多大**，没有它这个问题没有答案。
    """
    cap = constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION)
    if armour_value == 0 and hit_damage == 0:
        raw = 0.0
    else:
        raw = armour_value / (armour_value + hit_damage * 5) * 100
    total_raw = raw + flat_reduction
    reduction = int(max(min(round(total_raw), cap), 0))
    after = hit_damage * (1 - reduction / 100)

    steps = [
        Step(
            "护甲减伤（未取整）",
            f"{armour_value:g} / ({armour_value:g} + 5 × {hit_damage:g}) × 100",
            round(raw, 2),
        )
    ]
    if flat_reduction:
        steps.append(
            Step("另有的物理减伤", f"+ {flat_reduction:g}%", flat_reduction)
        )
    steps.append(
        Step(
            "实际减伤",
            f"min(取整({total_raw:.2f}%), 上限 {cap:g}%)",
            reduction,
        )
    )
    steps.append(
        Step(
            "这一击剩余伤害",
            f"{hit_damage:g} × (1 - {reduction}% / 100)",
            round(after, 2),
        )
    )
    return ArmourResult(
        armour=float(armour_value),
        hit_damage=float(hit_damage),
        raw_reduction=total_raw,
        reduction=reduction,
        reduction_cap=cap,
        damage_after=after,
        steps=tuple(steps),
    )


def block(
    chance: float,
    *,
    extra_max_block: float = 0.0,
    spell: bool = False,
) -> BlockResult:
    """格挡几率与上限。

    照 `CalcDefence.lua:661` 与 `699`：
    ``最大格挡 = min(基础最大格挡 + 加成, 硬上限 90%)``、
    ``格挡几率 = min(总几率, 最大格挡)``，超出部分记在 `over_cap`。

    `spell=True` 用法术格挡的基础最大值（两者都是 75%，但常量是两条，不合并）。
    """
    base_max = constants.get(
        constants.MAX_SPELL_BLOCK if spell else constants.MAX_BLOCK
    )
    hard_cap = constants.get(constants.BLOCK_CHANCE_CAP)
    max_block = int(math.trunc(min(base_max + extra_max_block, hard_cap)))
    final = min(chance, max_block)
    over = max(0.0, chance - max_block)

    kind = "法术格挡" if spell else "攻击格挡"
    steps = [Step(f"基础最大{kind}", f"游戏常量 {base_max:g}%", base_max)]
    if extra_max_block:
        steps.append(
            Step(f"最大{kind}加成", f"+{extra_max_block:g}%", extra_max_block)
        )
    steps.append(
        Step(
            f"最大{kind}",
            f"min({base_max:g}% + {extra_max_block:g}%, 硬上限 {hard_cap:g}%)",
            max_block,
        )
    )
    steps.append(Step(f"{kind}几率", f"min({chance:g}%, {max_block}%)", final))
    if over:
        steps.append(
            Step("超出上限", f"{chance:g}% - {max_block}% = {over:g}%", over)
        )
    return BlockResult(
        total=float(chance),
        max_block=max_block,
        chance=final,
        over_cap=over,
        hard_cap=hard_cap,
        base_max=base_max,
        steps=tuple(steps),
    )


def suppression(chance: float) -> SuppressionResult:
    """法术压制：几率夹在 100% 以内，命中时减 **40%** 法术伤害。"""
    cap = constants.get(constants.SUPPRESSION_CHANCE_CAP)
    effect = constants.get(constants.SUPPRESSION_EFFECT)
    final = max(min(chance, cap), 0.0)
    average = final / 100 * effect
    steps = [
        Step("压制几率", f"min({chance:g}%, 上限 {cap:g}%)", final),
        Step("压制效果", f"游戏常量 {effect:g}%（不是 50%）", effect),
        Step("平均减伤", f"{final:g}% × {effect:g}% / 100", round(average, 2)),
    ]
    return SuppressionResult(
        chance=final,
        chance_cap=cap,
        effect=effect,
        average_reduction=average,
        steps=tuple(steps),
    )
