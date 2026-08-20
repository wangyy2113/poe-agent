"""持续伤害：点燃、中毒、流血。

## 三条规则，形状一样、数字全不一样

每一种都是「**每秒**造成这一击某类伤害的百分之几，持续几秒」：

===== ============ ======== ============== ============================
异常   每秒占比      持续      伤害类型        默认叠加
===== ============ ======== ============== ============================
点燃   90%          4 秒     火焰（吃火抗）   1 层（只有最强的那个生效）
中毒   30%          2 秒     混沌（吃混沌抗） **无上限**，层层叠加
流血   70%          5 秒     物理           1 层（血之舞蹈改成 8 层）
===== ============ ======== ============== ============================

「每秒」这三个字是从 PoB 自己的说明字符串里确认的
（`CalcOffence.lua` 的 breakdown 写着 `x 0.9 (ignite deals 90% per second)`）——
所以点燃 4 秒总共打出 **360%**，不是 90%。这个口径搞错，结论会差 4 倍。

## 上游的单位不统一，照抄会差 100 倍

`BleedPercentBase = 70`（百分数），而 `PoisonPercentBase = 0.30`、
`IgnitePercentBase = 0.9`（小数）。PoB 对流血 `/ 100`、对另两个不除。
本模块按上游口径逐个处理，`tests/unit/test_ailment.py` 把这条差异钉住。

## 减伤那一段：流血**不吃护甲**

物理持续伤害只被敌人的**物理伤害减免**削（上限 75%，
`monsterConstants.maximum_physical_damage_reduction_%`），护甲不参与——
护甲只作用于**击中**（判据见 `calc/mitigation.py` 与
`CalcOffence.lua` 的流血 `effMult` 那一段，两处都只取 `PhysicalDamageReduction`）。
点燃吃火抗、中毒吃混沌抗，与击中同一套抗性算法。

## 不做的事

- **不算这一击有多大**：那要 PoB 的 effectiveness 公式与整条攻击链
  （`pob_skill_level` 刻意没有伤害列，判据见 `core/db.py` 的表注释）。
  这一击的伤害由调用方逐字给出。
- 不算暴击对异常的加成（`CritPoisonDotMulti` 那一套）、不算异常持续时间加成的
  各种来源、不算「感电 / 冰冻」——那两个是**门槛型**异常，公式与这里完全不同。
- 不算流血的移动加成（移动时流血伤害提高）与投掷层数上限的各种特例。
"""

from __future__ import annotations

from dataclasses import dataclass

from calc import constants
from calc.derive import Step

#: PoB 原文位置。引用必须带行号锚点。
OFFENCE_FILE = "src/Modules/CalcOffence.lua"
BLEED_LINES = (4360, 4392)
POISON_LINES = (4635, 4660)
IGNITE_LINES = (4976, 5046)
#: 「每秒」这个口径的出处：PoB 自己的 breakdown 字符串。
IGNITE_PER_SECOND_LINE = 5045
SETUP_STACK_LINE = 50

#: 三种伤害性异常的口径。`percent_is_fraction` 记住上游那个单位不统一的坑。
AILMENTS: dict[str, dict[str, object]] = {
    "点燃": {
        "en": "Ignite",
        "percent_key": constants.IGNITE_PERCENT,
        "duration_key": constants.IGNITE_DURATION,
        "percent_is_fraction": True,
        "damage_type": "火焰",
        "resist": "火焰抗性",
        "source": "这一击的火焰伤害",
        "default_stacks": 1,
        "stack_note": "默认只有**最强的那一个**点燃生效（除非有「可以叠加多个点燃」的词缀）",
        "lines": IGNITE_LINES,
    },
    "中毒": {
        "en": "Poison",
        "percent_key": constants.POISON_PERCENT,
        "duration_key": constants.POISON_DURATION,
        "percent_is_fraction": True,
        "damage_type": "混沌",
        "resist": "混沌抗性",
        "source": "这一击的物理与混沌伤害之和",
        "default_stacks": None,
        "stack_note": "中毒**没有层数上限**，每次命中都叠一层，所以真实 DPS 取决于攻速",
        "lines": POISON_LINES,
    },
    "流血": {
        "en": "Bleed",
        "percent_key": constants.BLEED_PERCENT,
        "duration_key": constants.BLEED_DURATION,
        "percent_is_fraction": False,
        "damage_type": "物理",
        "resist": "物理伤害减免（**护甲不参与**）",
        "source": "这一击的物理伤害",
        "default_stacks": 1,
        "stack_note": "默认只有 1 层（`CalcSetup.lua` 的 `BleedStacksMax` 基础值就是 1）；"
                      "「血之舞蹈」把上限改成 8 层",
        "lines": BLEED_LINES,
    },
}


class UnknownAilment(ValueError):
    """异常名不认识。三种的百分比、持续与抗性全不一样，猜错就是错数字。"""


@dataclass(frozen=True)
class AilmentResult:
    name: str
    name_en: str
    hit_damage: float
    #: 每秒占这一击的比例（已按上游单位归一成百分数）
    percent_per_second: float
    duration: float
    stacks: int
    #: 未计减伤、未夹上限的每秒伤害
    base_dps: float
    #: 计入抗性 / 物理减伤之后的每秒伤害
    dps: float
    #: 夹在 DOT 上限之内的每秒伤害
    dps_capped: float
    total: float
    dps_cap: float
    mitigation_pct: float
    dot_multiplier_pct: float
    steps: tuple[Step, ...]

    @property
    def capped(self) -> bool:
        return self.dps > self.dps_cap


def _resolve(name: str) -> str:
    text = (name or "").strip()
    for zh, spec in AILMENTS.items():
        if text == zh or zh in text or text.lower() == str(spec["en"]).lower():
            return zh
    raise UnknownAilment(
        f"认不出持续伤害类型 {name!r}；只支持 {'、'.join(AILMENTS)}"
        "（感电与冰冻是门槛型异常，公式不同，本模块不算）"
    )


def ailment_dot(
    name: str,
    hit_damage: float,
    *,
    stacks: int = 1,
    mitigation_pct: float = 0.0,
    dot_multiplier_pct: float = 0.0,
    increased_effect_pct: float = 0.0,
    faster_rate_pct: float = 0.0,
) -> AilmentResult:
    """算一种伤害性异常的每秒与总伤害。

    * `hit_damage`：造成异常的那一击的**对应类型**伤害（见 `AILMENTS[..]["source"]`）。
    * `mitigation_pct`：目标的抗性（点燃/中毒）或物理伤害减免（流血）。
    * `dot_multiplier_pct`：「持续伤害加成」（PoB 的 `DotMultiplier`）。
    * `increased_effect_pct`：「异常效果提高」（`AilmentEffect`）。
    * `faster_rate_pct`：「燃烧/流血更快」——它**同时**抬高每秒伤害并缩短持续时间，
      总伤害不变（PoB 的 `rateMod` 同时乘 DPS、除持续时间）。
    """
    zh = _resolve(name)
    spec = AILMENTS[zh]
    raw = constants.get(str(spec["percent_key"]))
    percent = raw * 100 if spec["percent_is_fraction"] else raw
    duration_base = constants.get(str(spec["duration_key"]))
    cap = constants.get(constants.DOT_DPS_CAP)

    dot_multi = 1 + dot_multiplier_pct / 100
    effect = 1 + increased_effect_pct / 100
    rate = 1 + faster_rate_pct / 100
    used_stacks = max(int(stacks), 1)

    base_dps = hit_damage * percent / 100 * dot_multi * effect * rate * used_stacks
    mitigation = 1 - mitigation_pct / 100
    dps = base_dps * mitigation
    dps_capped = min(dps, cap)
    duration = duration_base / rate
    total = dps_capped * duration

    steps = [
        Step(
            f"{zh}每秒基础伤害",
            f"{hit_damage:g} × {percent:g}%（{spec['source']}，每秒）",
            round(hit_damage * percent / 100, 2),
        )
    ]
    if dot_multiplier_pct:
        steps.append(Step("持续伤害加成", f"× (1 + {dot_multiplier_pct:g}/100)", dot_multi))
    if increased_effect_pct:
        steps.append(Step("异常效果提高", f"× (1 + {increased_effect_pct:g}/100)", effect))
    if faster_rate_pct:
        steps.append(
            Step(
                "更快的燃烧/流血",
                f"每秒 × {rate:g}，同时持续时间 ÷ {rate:g}（总伤害不变）",
                rate,
            )
        )
    if used_stacks != 1:
        steps.append(Step("同时生效的层数", f"× {used_stacks}", used_stacks))
    if mitigation_pct:
        steps.append(
            Step(
                f"目标的{spec['resist']}",
                f"× (1 - {mitigation_pct:g}/100)",
                round(mitigation, 4),
            )
        )
    steps.append(Step("每秒伤害", f"{dps:.2f}", round(dps, 2)))
    if dps > cap:
        steps.append(
            Step("持续伤害上限", f"min({dps:,.0f}, {cap:,.0f})", dps_capped)
        )
    steps.append(
        Step(
            "持续时间",
            f"{duration_base:g} 秒" + (f" ÷ {rate:g}" if faster_rate_pct else ""),
            round(duration, 2),
        )
    )
    steps.append(
        Step("总伤害", f"{dps_capped:.2f} × {duration:g} 秒", round(total, 2))
    )
    return AilmentResult(
        name=zh,
        name_en=str(spec["en"]),
        hit_damage=float(hit_damage),
        percent_per_second=percent,
        duration=duration,
        stacks=used_stacks,
        base_dps=base_dps,
        dps=dps,
        dps_capped=dps_capped,
        total=total,
        dps_cap=cap,
        mitigation_pct=mitigation_pct,
        dot_multiplier_pct=dot_multiplier_pct,
        steps=tuple(steps),
    )
