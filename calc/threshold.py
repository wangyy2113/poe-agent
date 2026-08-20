"""门槛型异常：感电 / 冰缓 / 焦灼 / 脆弱 / 干枯（以及冰冻的那一条门槛）。

## 与持续伤害完全不是一套公式

`calc/ailment.py` 那三种（点燃/中毒/流血）按**伤害的百分比**每秒掉血；
这五种不掉血，它们的**强度**取决于「这一击的伤害 ÷ 目标的异常门槛」：

    效果 = 基数 ×(伤害 ÷ 异常门槛)^0.4 × 效果加成      再夹在上下限之间

基数：冰缓 / 感电 / 焦灼 **50**、脆弱 **10**、干枯 **100/3**。
上下限来自 `nonDamagingAilment`（感电 5–50%、冰缓 5–30%、焦灼 0–30%、
脆弱 0–6%、干枯 0–20%），这一轮才入库。

反过来问「要多少伤害才能感电到 X%」，PoB 有现成的反函数：

    需要的门槛 = 伤害 ×(基数 × 效果加成 ÷ X)^2.5

## 异常门槛怎么来

    门槛 = 等级表[等级] ×（头目 7.68）×（地图倍率）×（头目异常倍率）

- 等级表是 `monsterAilmentThresholdTable`（100 级，来自 `DefaultMonsterStats.dat`）。
- **头目额外乘 7.68**——这个数是 `CalcOffence.lua` 里的硬编码字面量，
  不在常量表里，所以由 `tests/unit/test_threshold.py` 直接对着上游原文钉住。
- 66 级起还要乘地图倍率：普通怪 `mapLevelLifeMult[等级]`、
  头目 `mapLevelBossLifeMult[等级] × mapLevelBossAilmentMult[等级]`。

PoB 自己的说法是「异常门槛大致等于生命，头目除外——头目的门槛另有专门的倍率」。

## 冰冻只给一条能钉住的事实

冰冻的强度是**持续时间**，走的是另一条分支，本模块**不算它的时长曲线**——
PoB 那段只算了 `FreezeDurationMod`，完整曲线我没能在上游找到可引用的原式。
能钉住的是它自己写下的那句话：**要冻住最短的 0.3 秒，目标的异常门槛不能超过
这一击伤害的 20 倍**（`CalcOffence.lua` 的 breakdown 原文），以及
`nonDamagingAilment.Freeze` 的 0.3–3 秒上下限。多的不猜。
"""

from __future__ import annotations

from dataclasses import dataclass

from calc import constants
from calc.derive import Step

#: PoB 原文位置。
OFFENCE_FILE = "src/Modules/CalcOffence.lua"
THRESHOLD_LINES = (5124, 5135)
EFFECT_LINES = (5136, 5170)
FREEZE_NOTE_LINE = 5202
#: 头目的异常门槛倍率。**硬编码在上游代码里**（`enemyTypeMult = isBoss and 7.68 or 1`），
#: 不在常量表——所以由单测对着原文钉住，与剧情抗性惩罚同一处理。
BOSS_TYPE_MULT = 7.68
#: 门槛表覆盖 1..100 级；地图倍率表覆盖 66..90 级（普通）与 1..90（头目）。
MAP_MULT_MIN_LEVEL = 66

#: 五种「有强度」的门槛型异常。基数与关联伤害类型都是上游写死的。
#: 冰冻不在这里——它的强度是持续时间，走另一条分支（见模块 docstring）。
AILMENTS: dict[str, dict[str, object]] = {
    "冰缓": {"en": "Chill", "base": 50.0, "type": "冰霜", "effect": "降低目标行动速度"},
    "感电": {"en": "Shock", "base": 50.0, "type": "闪电", "effect": "提高目标受到的伤害"},
    "焦灼": {"en": "Scorch", "base": 50.0, "type": "火焰", "effect": "降低目标元素抗性"},
    "脆弱": {"en": "Brittle", "base": 10.0, "type": "冰霜", "effect": "提高对目标的暴击率"},
    "干枯": {"en": "Sap", "base": 100 / 3, "type": "闪电", "effect": "降低目标造成的伤害"},
}
EXPONENT = 0.4
INVERSE_EXPONENT = 2.5


class UnknownAilment(ValueError):
    """异常名不认识。五种的基数与上下限都不同，猜错就是错数字。"""


class OutOfTableRange(ValueError):
    """等级超出门槛表或地图倍率表的范围。不外推。"""


def _resolve(name: str) -> str:
    text = (name or "").strip()
    for zh, spec in AILMENTS.items():
        if text == zh or zh in text or text.lower() == str(spec["en"]).lower():
            return zh
    raise UnknownAilment(
        f"认不出门槛型异常 {name!r}；只支持 {'、'.join(AILMENTS)}"
        "（冰冻的强度是持续时间、走另一条公式，本模块只给它的门槛判据；"
        "点燃/中毒/流血是伤害性异常，用 ailment_dot_calc）"
    )


def _bounds(ailment_en: str) -> tuple[float, float]:
    """上下限取自 `nonDamagingAilment`。"""
    low = constants.get(f"pobMisc.nonDamagingAilment.{ailment_en}.min")
    high = constants.get(f"pobMisc.nonDamagingAilment.{ailment_en}.max")
    return low, high


@dataclass(frozen=True)
class ThresholdResult:
    level: int
    is_boss: bool
    base_threshold: float
    map_mult: float
    boss_mult: float
    threshold: float
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class AilmentEffectResult:
    name: str
    name_en: str
    hit_damage: float
    threshold: float
    ratio: float
    base: float
    #: 夹上下限之前的原始值
    raw_effect: float
    effect: float
    precision: int
    min_effect: float
    max_effect: float
    duration: float
    #: 要达到上限需要多大的伤害（反函数）
    damage_for_max: float
    steps: tuple[Step, ...]

    @property
    def capped(self) -> bool:
        return self.raw_effect >= self.max_effect

    @property
    def below_min(self) -> bool:
        return self.raw_effect < self.min_effect


def enemy_threshold(level: int, *, is_boss: bool = False) -> ThresholdResult:
    """某等级怪物 / 头目的异常门槛。照 `CalcOffence.lua:5126-5134`。"""
    lvl = int(level)
    try:
        base = constants.at_level(constants.MONSTER_AILMENT_THRESHOLD_TABLE, lvl)
    except constants.MissingConstant as exc:
        raise OutOfTableRange(str(exc)) from exc

    map_mult = 1.0
    boss_mult = 1.0
    steps = [Step(f"{lvl} 级的基础异常门槛", f"等级表第 {lvl} 项", base)]
    if is_boss:
        boss_mult = BOSS_TYPE_MULT
        steps.append(Step("头目倍率", f"× {BOSS_TYPE_MULT}（上游硬编码）", BOSS_TYPE_MULT))
    if lvl >= MAP_MULT_MIN_LEVEL:
        if is_boss:
            life = constants.at_level("mapLevelBossLifeMult", lvl)
            ailment = constants.at_level("mapLevelBossAilmentMult", lvl)
            map_mult = life * ailment
            steps.append(
                Step("头目地图倍率", f"生命 × {life:g} × 异常 × {ailment:g}", round(map_mult, 4))
            )
        else:
            map_mult = constants.get(f"mapLevelLifeMult.{lvl}")
            steps.append(Step("地图倍率", f"× {map_mult:g}", map_mult))

    threshold = base * boss_mult * map_mult
    steps.append(
        Step(
            "异常门槛",
            f"{base:,.0f}"
            + (f" × {BOSS_TYPE_MULT}" if is_boss else "")
            + (f" × {map_mult:g}" if lvl >= MAP_MULT_MIN_LEVEL else ""),
            round(threshold, 2),
        )
    )
    return ThresholdResult(
        level=lvl, is_boss=is_boss, base_threshold=base, map_mult=map_mult,
        boss_mult=boss_mult, threshold=threshold, steps=tuple(steps),
    )


def ailment_effect(
    name: str,
    hit_damage: float,
    *,
    level: int = 83,
    is_boss: bool = False,
    threshold: float | None = None,
    effect_mod_pct: float = 0.0,
) -> AilmentEffectResult:
    """一次 `hit_damage` 的打击能造成多强的门槛型异常。

    `threshold` 直接给就用它；否则按等级 / 是否头目查表。
    """
    zh = _resolve(name)
    spec = AILMENTS[zh]
    base = float(spec["base"])
    low, high = _bounds(str(spec["en"]))
    duration = constants.get(f"pobMisc.nonDamagingAilment.{spec['en']}.duration")
    precision = int(constants.get(f"pobMisc.nonDamagingAilment.{spec['en']}.precision"))

    steps: list[Step] = []
    if threshold is None:
        computed = enemy_threshold(level, is_boss=is_boss)
        threshold = computed.threshold
        steps.extend(computed.steps)
    else:
        steps.append(Step("异常门槛（由问题给出）", f"{threshold:,.0f}", threshold))

    mod = 1 + effect_mod_pct / 100
    ratio = hit_damage / threshold if threshold else 0.0
    raw = base * (ratio ** EXPONENT) * mod if ratio > 0 else 0.0
    # `precision` 也是上游数据的一部分：感电 / 冰缓 / 焦灼 显示到整数位，脆弱到 2 位。
    # 不按它取整，答案里会出现 19.2592% 这种游戏里不存在的精度。
    effect = round(max(min(raw, high), low), precision) if raw > 0 else 0.0
    # 要顶到上限需要多大的伤害：由 X = 基数 ×(d/T)^0.4 反解 d = T ×(X/基数)^2.5。
    # **不能照抄 PoB 的 `thresh`**——那个函数返回的是「某伤害下达到 X 所需的门槛」，
    # 方向相反。写反了会得到「需要的伤害 = 当前伤害」这种自证式的废话（实测踩过）。
    damage_for_max = (
        threshold * ((high / (base * mod)) ** INVERSE_EXPONENT) if base * mod else 0.0
    )

    steps.append(
        Step(
            "伤害与门槛之比",
            f"{hit_damage:,.0f} ÷ {threshold:,.0f}",
            round(ratio, 4),
        )
    )
    steps.append(
        Step(
            f"{zh}强度（未夹上下限）",
            f"{base:g} × {ratio:.4f} ^ {EXPONENT}"
            + (f" × {mod:g}" if effect_mod_pct else ""),
            round(raw, 2),
        )
    )
    steps.append(
        Step(
            f"{zh}强度",
            f"夹在 {low:g}% 与 {high:g}% 之间",
            round(effect, 2),
        )
    )
    return AilmentEffectResult(
        name=zh, name_en=str(spec["en"]), hit_damage=float(hit_damage),
        threshold=float(threshold), ratio=ratio, base=base, raw_effect=raw,
        effect=effect, precision=precision, min_effect=low, max_effect=high,
        duration=duration,
        damage_for_max=damage_for_max, steps=tuple(steps),
    )


def damage_for_effect(
    name: str, target_effect: float, threshold: float, *, effect_mod_pct: float = 0.0
) -> float:
    """反函数：要造成 `target_effect`% 的异常，这一击要打多大。

    照 PoB 的 `thresh` 函数改写：``需要的门槛 = 伤害 ×(基数 × 加成 ÷ 目标值)^2.5``，
    两边对调即得所需伤害。
    """
    zh = _resolve(name)
    base = float(AILMENTS[zh]["base"])
    if target_effect <= 0:
        raise ValueError("目标强度要大于 0")
    mod = 1 + effect_mod_pct / 100
    return threshold / ((base * mod / target_effect) ** INVERSE_EXPONENT)


def freeze_threshold_note(hit_damage: float) -> tuple[float, float, float]:
    """冰冻能钉住的那条事实。

    返回 ``(最短时长, 最长时长, 能冻住 0.3 秒所需的门槛上限)``——
    最后一项就是 PoB 自己写的「目标的异常门槛不能超过这一击伤害的 20 倍」。
    """
    low = constants.get("pobMisc.nonDamagingAilment.Freeze.min")
    high = constants.get("pobMisc.nonDamagingAilment.Freeze.max")
    return low, high, hit_damage * 20
