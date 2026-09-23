"""Boss 单次或重复同一命中与角色防御池的有界生存校验。

这层只负责把两条已经审计过的计算链拼起来：

* ``calc.boss_damage`` 给出 Boss 技能的原始单次命中；
* ``calc.maxhit`` 按抗性、护甲/物理减伤、承受倍率算实际承伤与 Max Hit。

它只能把用户指定的同一技能按“每次命中、期间无恢复”累加。它刻意不处理
持续伤害、真实 Boss 连招或不同技能组合、伤害转化、格挡/闪避/压制、
守护技能、Ward、Mind over Matter 或恢复。缺少这些维度时输出边界，不用单次
命中结论冒充完整 Boss 生存能力。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from calc import maxhit

Outcome = Literal["survives_full_range", "roll_dependent", "fails_full_range"]

_ELEMENTAL = {"Fire", "Cold", "Lightning"}
_SUPPORTED = {"Physical", "Fire", "Cold", "Lightning", "Chaos"}


class BossSurvivalError(ValueError):
    """输入无法安全进入单次命中闭式计算。"""


@dataclass(frozen=True)
class BossSurvivalResult:
    damage_type: str
    pool: float
    raw_low: float
    raw_high: float
    hit_count: int
    total_raw_low: float
    total_raw_high: float
    resist_before_penetration: float
    penetration_pct: float
    effective_resist_pct: float
    taken_low: float
    taken_high: float
    total_taken_low: float
    total_taken_high: float
    remaining_low: float
    remaining_high: float
    max_raw_hit: float
    margin_low: float
    margin_high: float
    outcome: Outcome

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "damage_type": self.damage_type,
            "pool": self.pool,
            "raw_low": self.raw_low,
            "raw_high": self.raw_high,
            "hit_count": self.hit_count,
            "total_raw_low": self.total_raw_low,
            "total_raw_high": self.total_raw_high,
            "resist_before_penetration": self.resist_before_penetration,
            "penetration_pct": self.penetration_pct,
            "effective_resist_pct": self.effective_resist_pct,
            "taken_low": self.taken_low,
            "taken_high": self.taken_high,
            "total_taken_low": self.total_taken_low,
            "total_taken_high": self.total_taken_high,
            "remaining_low": self.remaining_low,
            "remaining_high": self.remaining_high,
            "max_raw_hit": self.max_raw_hit,
            "margin_low": self.margin_low,
            "margin_high": self.margin_high,
            "outcome": self.outcome,
        }


def assess_single_hit(
    *,
    damage_type: str,
    raw_low: float,
    raw_high: float,
    pool: float,
    resist_pct: float,
    penetration_pct: float = 0.0,
    armour: float = 0.0,
    flat_physical_reduction_pct: float = 0.0,
    taken_mult: float = 1.0,
    overwhelm_pct: float = 0.0,
) -> BossSurvivalResult:
    """比较一个单伤害类型命中的伤害区间与给定防御池。

    ``resist_pct`` 是角色面板/上游计算后、Boss 技能穿透前的实际抗性；穿透在这里
    逐字按技能预设扣除。物理伤害的抗性固定为 0，护甲与物理减伤只在该分支生效。
    """
    return assess_repeated_hits(
        damage_type=damage_type,
        raw_low=raw_low,
        raw_high=raw_high,
        pool=pool,
        resist_pct=resist_pct,
        penetration_pct=penetration_pct,
        armour=armour,
        flat_physical_reduction_pct=flat_physical_reduction_pct,
        taken_mult=taken_mult,
        overwhelm_pct=overwhelm_pct,
        hit_count=1,
    )


def assess_repeated_hits(
    *,
    damage_type: str,
    raw_low: float,
    raw_high: float,
    pool: float,
    resist_pct: float,
    hit_count: int = 1,
    penetration_pct: float = 0.0,
    armour: float = 0.0,
    flat_physical_reduction_pct: float = 0.0,
    taken_mult: float = 1.0,
    overwhelm_pct: float = 0.0,
) -> BossSurvivalResult:
    """按“同一技能每次命中、期间无恢复”累加指定次数。

    护甲等命中结算仍逐击计算，再累加每击承伤；不把累计原始伤害
    当成一记更大的物理命中。
    """
    dtype = str(damage_type).strip()
    if dtype not in _SUPPORTED:
        raise BossSurvivalError(f"不支持的单次命中伤害类型：{dtype or '未提供'}")
    if pool <= 0:
        raise BossSurvivalError("防御池必须大于 0")
    if (
        isinstance(hit_count, bool)
        or not isinstance(hit_count, int)
        or not 1 <= hit_count <= 100
    ):
        raise BossSurvivalError("命中次数必须是 1..100 的整数")
    if raw_low < 0 or raw_high < 0 or raw_low > raw_high:
        raise BossSurvivalError("原始伤害区间无效")
    if taken_mult <= 0:
        raise BossSurvivalError("承受伤害倍率必须大于 0")
    if armour < 0:
        raise BossSurvivalError("护甲不能为负数")
    if penetration_pct < 0:
        raise BossSurvivalError("技能穿透不能为负数")
    if not 0 <= flat_physical_reduction_pct <= 90:
        raise BossSurvivalError("额外物理减伤必须在 0%..90% 之间")
    if not 0 <= overwhelm_pct <= 100:
        raise BossSurvivalError("压制物理减伤必须在 0%..100% 之间")
    if dtype != "Physical" and not -200 <= resist_pct <= 90:
        raise BossSurvivalError("穿透前实际抗性必须在 -200%..90% 之间")

    before_pen = 0.0 if dtype == "Physical" else float(resist_pct)
    penetration = float(penetration_pct) if dtype in _ELEMENTAL else 0.0
    effective_resist = before_pen - penetration
    physical = dtype == "Physical"
    armour_used = float(armour) if physical else 0.0
    flat_used = float(flat_physical_reduction_pct) if physical else 0.0
    overwhelm_used = float(overwhelm_pct) if physical else 0.0

    kwargs = {
        "resist_pct": effective_resist,
        "armour": armour_used,
        "flat_reduction_pct": flat_used,
        "taken_mult": float(taken_mult),
        "overwhelm_pct": overwhelm_used,
    }
    taken_low = maxhit.taken_from(float(raw_low), **kwargs)
    taken_high = maxhit.taken_from(float(raw_high), **kwargs)
    total_taken_low = taken_low * hit_count
    total_taken_high = taken_high * hit_count
    threshold = maxhit.max_hit(float(pool), **kwargs)

    # 等于池子会把池子降到 0，不能写成“存活”。
    if total_taken_high < pool:
        outcome: Outcome = "survives_full_range"
    elif total_taken_low >= pool:
        outcome = "fails_full_range"
    else:
        outcome = "roll_dependent"

    return BossSurvivalResult(
        damage_type=dtype,
        pool=float(pool),
        raw_low=float(raw_low),
        raw_high=float(raw_high),
        hit_count=hit_count,
        total_raw_low=round(float(raw_low) * hit_count, 2),
        total_raw_high=round(float(raw_high) * hit_count, 2),
        resist_before_penetration=before_pen,
        penetration_pct=penetration,
        effective_resist_pct=effective_resist,
        taken_low=round(taken_low, 2),
        taken_high=round(taken_high, 2),
        total_taken_low=round(total_taken_low, 2),
        total_taken_high=round(total_taken_high, 2),
        remaining_low=round(pool - total_taken_low, 2),
        remaining_high=round(pool - total_taken_high, 2),
        max_raw_hit=threshold.max_hit,
        margin_low=round(pool - total_taken_low, 2),
        margin_high=round(pool - total_taken_high, 2),
        outcome=outcome,
    )
