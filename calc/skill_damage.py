"""技能的**基础**伤害：把 PoB 的 effectiveness 公式复算出来。

## 这一块解掉了项目最老的一条自我限制

`pob_skill_level` 从第十二轮起就**刻意没有伤害列**，理由写在 `core/db.py` 的表注释里：
PoB 存的每级伤害是**乘数**（冰霜新星 1 级存 `0.8 / 1.2`），要按公式算出来才是
游戏里的显示值，而「算错就是带着引用的错数字」。第二十九轮补上了缺的那一半——
**对账口径**：POEDB 的技能页把区间写成「造成 (25 — 1122) - (38 — 1683) 冰霜伤害」，
括号里正好是宝石 1 级 → 自然最高级。20 个技能逐个对账，20/20 一致
（`scripts/verify_gem_damage.py`）。

## 公式（`CalcTools.lua:195-206`）

    availableEffectiveness =(SkillDamageBaseEffectiveness
                             + SkillDamageIncrementalEffectiveness ×(角色等级 − 1))
                            × 宝石 baseEffectiveness
                            ×(1 + 宝石 incrementalEffectiveness)^(角色等级 − 1)

    某个 stat 的值 = round(availableEffectiveness × 该级那一项的乘数)

「角色等级」取该宝石等级的 `levelRequirement`（PoB 的 `actorLevel` 默认值）。

## 两处必须照 PoB 来，否则对不上（都是实测撞出来的）

1. **同名 stat 要累加**：「传输之赤炼魔光」把最小伤害写了两次（`0.8` 与 `−0.64`），
   PoB 的 `stats[stat] = (stats[stat] or 0) + statValue` 把它们加成 0.16。
   只取第一项会算大 5 倍。
2. **先各自 round 再相加**，不是先相加再 round。同一颗宝石：round 后相加得
   4 / 268 / 401（与 POEDB 页面一致），相加后 round 得 6 / 267（两处都错）。

## 它给的是「基础伤害」

也就是技能面板上那一行，**不含**任何词缀加成、暴击、伤害有效度、辅助宝石乘数。
DPS 还要乘攻速/施法速度与整条加成链，那不是这一块的事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 「这一项是击中的最小/最大基础伤害」的 stat 名形状。
MIN_STAT_RE = re.compile(r"^(?:spell_|attack_)?minimum_base_(\w+)_damage$")
MAX_STAT_RE = re.compile(r"^(?:spell_|attack_)?maximum_base_(\w+)_damage$")
#: `statInterpolation` 的模式 3 = 按 effectiveness 缩放（另两种是线性插值，不走这条）。
EFFECTIVENESS_MODE = 3

#: PoB 原文位置。
TOOLS_FILE = "src/Modules/CalcTools.lua"
FORMULA_LINES = (195, 206)


@dataclass(frozen=True)
class BaseDamage:
    """一个宝石等级的基础伤害。`damage_type` 是 cold / fire / physical …"""

    minimum: int
    maximum: int
    damage_type: str
    actor_level: int
    effectiveness: float


def effectiveness(
    actor_level: int,
    base_effectiveness: float,
    incremental_effectiveness: float,
    *,
    game_base: float,
    game_incremental: float,
) -> float:
    """`availableEffectiveness`。两个 `gameConstants` 由调用方从常量表取。"""
    return (
        (game_base + game_incremental * (actor_level - 1))
        * base_effectiveness
        * (1 + incremental_effectiveness) ** (actor_level - 1)
    )


def base_damage(
    stat_names: dict[int, str],
    level_values: dict[int, float],
    interpolation: dict[int, int],
    *,
    actor_level: int,
    base_effectiveness: float,
    incremental_effectiveness: float,
    game_base: float,
    game_incremental: float,
) -> BaseDamage | None:
    """算某一级的最小/最大基础伤害。没有可算的伤害 stat 时返回 None。

    `stat_names` / `level_values` / `interpolation` 都是**位置 → 值**的映射，
    与 PoB 的 `grantedEffect.stats` / `levels[n]` / `statInterpolation` 一一对应。
    """
    eff = effectiveness(
        actor_level, base_effectiveness, incremental_effectiveness,
        game_base=game_base, game_incremental=game_incremental,
    )
    minimum = maximum = 0
    damage_type = ""
    has_min = has_max = False
    for index in sorted(stat_names):
        name = stat_names[index]
        if interpolation.get(index) != EFFECTIVENESS_MODE:
            continue
        raw = level_values.get(index)
        if not isinstance(raw, (int, float)):
            continue
        # **先 round 再累加**：顺序反了会差一两点（判据见模块 docstring）
        value = round(eff * float(raw))
        low = MIN_STAT_RE.match(name)
        high = MAX_STAT_RE.match(name)
        if low:
            minimum += value
            damage_type = damage_type or low.group(1)
            has_min = True
        elif high:
            maximum += value
            damage_type = damage_type or high.group(1)
            has_max = True
    if not (has_min and has_max):
        return None
    return BaseDamage(
        minimum=minimum, maximum=maximum, damage_type=damage_type,
        actor_level=actor_level, effectiveness=eff,
    )


#: 伤害类型 → 中文。POEDB 页面上就是这几个词。
DAMAGE_TYPE_ZH = {
    "cold": "冰霜", "fire": "火焰", "lightning": "闪电",
    "chaos": "混沌", "physical": "物理",
}
