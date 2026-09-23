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

import math
from dataclasses import dataclass
from typing import Literal

from calc import constants, maxhit

Outcome = Literal["survives_full_range", "roll_dependent", "fails_full_range"]

_ELEMENTAL = {"Fire", "Cold", "Lightning"}
_SUPPORTED = {"Physical", "Fire", "Cold", "Lightning", "Chaos"}


class BossSurvivalError(ValueError):
    """输入无法安全进入单次命中闭式计算。"""


def _snap_near_integer(value: float) -> float:
    """消除浮点运算对“恰好耗尽”边界造成的假偏差。"""
    nearest = round(value)
    return float(nearest) if math.isclose(value, nearest, rel_tol=1e-12, abs_tol=1e-9) else value


def _first_depletion_hit(pool: float, taken_per_hit: float) -> int | None:
    if taken_per_hit <= 0:
        return None
    return math.ceil(_snap_near_integer(pool / taken_per_hit))


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
    earliest_depletion_hit: int | None
    latest_depletion_hit: int | None
    guaranteed_survivable_hits: int | None
    maximum_survivable_hits: int | None
    minimum_pool_for_any_roll: int
    minimum_pool_for_full_range: int
    pool_shortfall_for_any_roll: float
    pool_shortfall_for_full_range: float
    required_resist_for_full_range_pct: int | None
    resist_shortfall_pct: float | None
    required_resist_status: str
    required_armour_for_full_range: int | None
    armour_shortfall: float | None
    required_armour_status: str
    max_raw_hit: float
    margin_low: float
    margin_high: float
    outcome: Outcome

    def as_dict(self) -> dict[str, float | int | str | None]:
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
            "earliest_depletion_hit": self.earliest_depletion_hit,
            "latest_depletion_hit": self.latest_depletion_hit,
            "guaranteed_survivable_hits": self.guaranteed_survivable_hits,
            "maximum_survivable_hits": self.maximum_survivable_hits,
            "minimum_pool_for_any_roll": self.minimum_pool_for_any_roll,
            "minimum_pool_for_full_range": self.minimum_pool_for_full_range,
            "pool_shortfall_for_any_roll": self.pool_shortfall_for_any_roll,
            "pool_shortfall_for_full_range": self.pool_shortfall_for_full_range,
            "required_resist_for_full_range_pct": self.required_resist_for_full_range_pct,
            "resist_shortfall_pct": self.resist_shortfall_pct,
            "required_resist_status": self.required_resist_status,
            "required_armour_for_full_range": self.required_armour_for_full_range,
            "armour_shortfall": self.armour_shortfall,
            "required_armour_status": self.required_armour_status,
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
    total_taken_low = _snap_near_integer(taken_low * hit_count)
    total_taken_high = _snap_near_integer(taken_high * hit_count)
    threshold = maxhit.max_hit(float(pool), **kwargs)
    earliest_depletion_hit = _first_depletion_hit(pool, taken_high)
    latest_depletion_hit = _first_depletion_hit(pool, taken_low)
    minimum_pool_for_any_roll = math.floor(total_taken_low) + 1
    minimum_pool_for_full_range = math.floor(total_taken_high) + 1

    required_resist: int | None = None
    resist_shortfall: float | None = None
    required_resist_status = "not_applicable"
    required_armour: int | None = None
    armour_shortfall: float | None = None
    required_armour_status = "not_applicable"
    if physical:
        required_armour_status = "unreachable"
        if raw_high == 0:
            required_armour = 0
        else:
            zero_armour_taken = maxhit.taken_from(
                raw_high,
                armour=0,
                flat_reduction_pct=flat_used,
                taken_mult=taken_mult,
                overwhelm_pct=overwhelm_used,
            ) * hit_count
            if zero_armour_taken < pool:
                required_armour = 0
            reduction_cap = constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION)
            maximum_reduction = min(
                max(flat_used + 100.0 - overwhelm_used, 0.0), reduction_cap
            )
            minimum_possible_taken = _snap_near_integer(
                raw_high * (1 - maximum_reduction / 100) * taken_mult * hit_count
            )
            if required_armour is None and minimum_possible_taken < pool:
                required_reduction = 100 * (
                    1 - pool / (raw_high * taken_mult * hit_count)
                )
                target_armour_reduction = required_reduction - flat_used + overwhelm_used
                boundary = (
                    5 * raw_high * target_armour_reduction
                    / (100 - target_armour_reduction)
                )
                required_armour = max(0, math.floor(boundary) + 1)
                while _snap_near_integer(
                    maxhit.taken_from(
                        raw_high,
                        armour=required_armour,
                        flat_reduction_pct=flat_used,
                        taken_mult=taken_mult,
                        overwhelm_pct=overwhelm_used,
                    ) * hit_count
                ) >= pool:
                    required_armour += 1
        if required_armour is not None:
            required_armour_status = "reachable"
            armour_shortfall = round(max(required_armour - armour_used, 0.0), 2)
    else:
        required_resist_status = "unreachable_at_90"
        for candidate in range(-200, 91):
            candidate_effective = candidate - penetration
            candidate_taken = _snap_near_integer(
                raw_high * (1 - candidate_effective / 100) * taken_mult * hit_count
            )
            if candidate_taken < pool:
                required_resist = candidate
                required_resist_status = "reachable"
                resist_shortfall = round(max(candidate - before_pen, 0.0), 2)
                break

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
        earliest_depletion_hit=earliest_depletion_hit,
        latest_depletion_hit=latest_depletion_hit,
        guaranteed_survivable_hits=(
            earliest_depletion_hit - 1 if earliest_depletion_hit is not None else None
        ),
        maximum_survivable_hits=(
            latest_depletion_hit - 1 if latest_depletion_hit is not None else None
        ),
        minimum_pool_for_any_roll=minimum_pool_for_any_roll,
        minimum_pool_for_full_range=minimum_pool_for_full_range,
        pool_shortfall_for_any_roll=round(
            max(minimum_pool_for_any_roll - pool, 0.0), 2
        ),
        pool_shortfall_for_full_range=round(
            max(minimum_pool_for_full_range - pool, 0.0), 2
        ),
        required_resist_for_full_range_pct=required_resist,
        resist_shortfall_pct=resist_shortfall,
        required_resist_status=required_resist_status,
        required_armour_for_full_range=required_armour,
        armour_shortfall=armour_shortfall,
        required_armour_status=required_armour_status,
        max_raw_hit=threshold.max_hit,
        margin_low=round(pool - total_taken_low, 2),
        margin_high=round(pool - total_taken_high, 2),
        outcome=outcome,
    )
