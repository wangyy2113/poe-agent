"""命中率：我这一击打不打得中，以及要多少命中值才够。

## 它是「受击结算」的镜像，所以复用同一个函数

`calc/mitigation.py` 算的是**怪物打我**（我的闪避 vs 怪物命中值），这里算的是
**我打怪物**（怪物闪避 vs 我的命中值）。公式是同一个
（`CalcDefence.lua:57` 的 `calcs.hitChance`），所以本模块**直接调用**
`mitigation.hit_chance`，不另写一份——两处各写一份判据就是两份会分叉的口径
（规约第 3 条，闪避那一侧的取整与 5% 下限也就自动跟着一致）。

    命中率 = round(命中值 /(命中值 +(敌方闪避 / 5) ^ 0.9) × 125)，夹在 5%..100%

PoB 在这之上还乘一层**敌方格挡**（`CalcOffence.lua:2181`）：

    最终命中率 = 精准命中率 ×(1 − 敌方格挡 / 100)

两者含义不同：前者是「有没有打到」，后者是「打到了但被挡掉」。用户问「打不中」时
两种都算「没打中」，所以工具把两层分开写出来，而不是只给一个合并数。

## 敌方闪避不用用户提供

`monsterEvasionTable` 就在库里（100 级），PoB 的配置页也正是用它当敌方闪避的默认值
（`ConfigOptions.lua:2088`）。**但用了哪一级必须写进推导**——「83 级」这个前提是查出来的，
不是假定的（与 `mitigation` 里对怪物命中值的处理同一条纪律）。

## 反解：要多少命中值才够

`accuracy_for_chance` 把公式反过来解。它是这一块的**回代验证器**：

    命中值 = (敌方闪避 / 5) ^ 0.9 × 目标 /(125 − 目标)

因为上游把命中率 round 成整数，反解返回的是**最小能达标的整数命中值**，
判据由 `tests/unit/test_accuracy.py` 钉住：这个值达标、比它小 1 就不达标。
公式抄错或不等式写反，这一条立刻红。

## 刻意不做

* **传奇/终局头目的闪避倍率**：PoB 用 `data.bossStats.PinnacleEvasionMean`，
  那是**运行时从头目数据算出来的平均值**（`Data.lua:960`）而不是常量，本库没有采集
  头目数据。所以工具只按等级查表，并写明「这是普通怪物的闪避」。
* **「无法被闪避」**：`CannotBeEvaded` 是技能/词缀标记（如共鸣、部分近战），
  本库没有采集这一维，所以工具接受调用方显式声明，但不会自己判断哪个技能有这条。
* **DPS**：命中率只是 DPS 的一个乘数，另外几个（加成链、暴击、攻速）见各自模块。
"""

from __future__ import annotations

from dataclasses import dataclass

from calc import constants, mitigation
from calc.derive import Step

#: PoB 原文位置。命中率本体在 CalcDefence（已由 `calc/mitigation.py` 钉住行号），
#: 这里钉的是**攻击侧**那两行：精准命中率与敌方格挡。
OFFENCE_FILE = "src/Modules/CalcOffence.lua"
HIT_CHANCE_LINES = (2162, 2166)
BLOCK_LINE = 2181
#: 敌方闪避默认取等级表——PoB 配置页的占位值就是它。
EVASION_DEFAULT_LINE = 2088
CONFIG_FILE = "src/Modules/ConfigOptions.lua"


@dataclass(frozen=True)
class HitResult:
    accuracy: float
    #: 敌方闪避与它的来源等级（等级为 None 表示调用方直接给了闪避值）
    enemy_evasion: float
    enemy_level: int | None
    enemy_block_pct: float
    #: 只看命中值 vs 闪避的那一层
    accuracy_hit_chance: int
    #: 再乘上敌方格挡之后
    hit_chance: float
    #: 命中率被下限 5% 夹住了（堆闪避的怪物也不可能完全躲开）
    at_floor: bool
    always_hits: bool
    steps: tuple[Step, ...]

    @property
    def miss_chance(self) -> float:
        return round(100 - self.hit_chance, 2)


@dataclass(frozen=True)
class AccuracyNeeded:
    target_pct: int
    enemy_evasion: float
    enemy_level: int | None
    accuracy: float
    #: 达标时的实际命中率（≥ 目标，因为命中值取整数向上）
    achieved_pct: int
    steps: tuple[Step, ...]


def monster_evasion(level: int) -> float:
    """某等级**普通怪物**的闪避，取自 `monsterEvasionTable`。越界即抛异常，不外推。"""
    try:
        return constants.at_level(constants.MONSTER_EVASION_TABLE, int(level))
    except constants.MissingConstant as exc:
        raise mitigation.OutOfTableRange(str(exc)) from exc


def hit_chance_vs_enemy(
    accuracy: float,
    *,
    enemy_level: int | None = None,
    enemy_evasion: float | None = None,
    enemy_block_pct: float = 0.0,
    cannot_be_evaded: bool = False,
) -> HitResult:
    """我这一击的命中率。

    * `accuracy`：我的命中值（角色面板上那个数）。
    * `enemy_level` / `enemy_evasion`：给等级就查表，给闪避就直接用。两个都不给
      则报错——替用户假定一个等级，等于把「打谁」这个前提凭空补上。
    * `enemy_block_pct`：敌方格挡几率。挡下来的那部分不算命中（`CalcOffence.lua:2181`）。
    * `cannot_be_evaded`：技能带「无法被闪避」时精准命中率直接 100%
      （`CalcOffence.lua:2162`）。本库不知道哪些技能有这条，所以由调用方声明。
    """
    if enemy_evasion is None:
        if enemy_level is None:
            raise ValueError("要么给敌方闪避，要么给敌方等级——不替用户假定打的是谁")
        enemy_evasion = monster_evasion(enemy_level)

    steps: list[Step] = []
    if enemy_level is not None:
        steps.append(
            Step(f"{enemy_level} 级怪物的闪避", "monsterEvasionTable", enemy_evasion)
        )
    if cannot_be_evaded:
        accuracy_chance = 100
        steps.append(Step("技能无法被闪避", "精准命中率直接取 100%", 100))
    else:
        accuracy_chance = mitigation.hit_chance(enemy_evasion, accuracy)
        steps.append(
            Step(
                "精准命中率",
                f"取整({accuracy:g} / ({accuracy:g} + ({enemy_evasion:g} / 5) ^ 0.9) × 125)"
                f"，夹在 {mitigation.HIT_CHANCE_FLOOR}%..100%",
                accuracy_chance,
            )
        )
    final = accuracy_chance * (1 - max(min(enemy_block_pct, 100.0), 0.0) / 100)
    if enemy_block_pct:
        steps.append(
            Step("再扣敌方格挡", f"{accuracy_chance:g}% × (1 - {enemy_block_pct:g}% / 100)",
                 round(final, 2))
        )
    return HitResult(
        accuracy=float(accuracy),
        enemy_evasion=float(enemy_evasion),
        enemy_level=enemy_level,
        enemy_block_pct=float(enemy_block_pct),
        accuracy_hit_chance=accuracy_chance,
        hit_chance=round(final, 2),
        at_floor=accuracy_chance <= mitigation.HIT_CHANCE_FLOOR and not cannot_be_evaded,
        always_hits=bool(cannot_be_evaded) or accuracy_chance >= 100,
        steps=tuple(steps),
    )


def accuracy_for_chance(
    target_pct: int,
    *,
    enemy_level: int | None = None,
    enemy_evasion: float | None = None,
) -> AccuracyNeeded:
    """要达到 `target_pct`% 精准命中率，最少需要多少命中值。

    **方向容易写反**（第二十六轮在异常门槛上踩过同一个坑）：用户问的是「需要多少
    **命中值**」，而正向公式给的是「这个命中值下的命中率」。所以这里解的是命中值，
    并且因为上游把命中率 round 成整数，返回的是**最小达标整数**——
    判据见 `tests/unit/test_accuracy.py` 的回代用例（它达标、少 1 就不达标）。
    """
    target = int(target_pct)
    if not 0 < target <= 100:
        raise ValueError("目标命中率要在 1..100 之间")
    if enemy_evasion is None:
        if enemy_level is None:
            raise ValueError("要么给敌方闪避，要么给敌方等级")
        enemy_evasion = monster_evasion(enemy_level)

    steps: list[Step] = []
    if enemy_level is not None:
        steps.append(
            Step(f"{enemy_level} 级怪物的闪避", "monsterEvasionTable", enemy_evasion)
        )
    if target <= mitigation.HIT_CHANCE_FLOOR:
        # 下限 5% 是白送的：命中值为 0 也有 5%
        steps.append(
            Step("命中率下限", f"{mitigation.HIT_CHANCE_FLOOR}% 不需要任何命中值", 0)
        )
        return AccuracyNeeded(target, float(enemy_evasion), enemy_level, 0.0,
                              mitigation.hit_chance(enemy_evasion, 0), tuple(steps))

    factor = (enemy_evasion / 5) ** 0.9
    # round 之后达标即可，所以解到 target - 0.5
    threshold = target - 0.5
    needed = factor * threshold / (125 - threshold)
    steps.append(Step("闪避项", f"({enemy_evasion:g} / 5) ^ 0.9", round(factor, 2)))
    steps.append(
        Step(
            "反解命中值",
            f"{factor:.2f} × {threshold:g} / (125 - {threshold:g})",
            round(needed, 2),
        )
    )
    accuracy = float(int(needed) + 1)
    while mitigation.hit_chance(enemy_evasion, accuracy - 1) >= target:
        accuracy -= 1  # 取整误差：往回收到真正的最小值
    while mitigation.hit_chance(enemy_evasion, accuracy) < target:
        accuracy += 1
    steps.append(Step("取最小达标整数", f"命中值 {accuracy:g}", accuracy))
    return AccuracyNeeded(
        target_pct=target,
        enemy_evasion=float(enemy_evasion),
        enemy_level=enemy_level,
        accuracy=accuracy,
        achieved_pct=mitigation.hit_chance(enemy_evasion, accuracy),
        steps=tuple(steps),
    )
