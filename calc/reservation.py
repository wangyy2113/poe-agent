"""保留：开几个光环之后还剩多少魔力。

## 这一块的全部难点是**取整**

一个光环保留多少，PoB 是这么算的（`CalcPerform.lua:1985` 与 `558`）：

    每个技能：保留百分比 = max(round(基础% ×(100 + 增加)/100 × 更多
                                    /(1 + 保留效率/100)/ 效率更多, 2), 0)
    每个技能：保留定值   = max(round(基础值 × …… , 0), 0)      ← 取整到个位
    合计：    保留量 = Σ定值 + **ceil**(池 ×(Σ百分比)/ 100)

三个容易错的地方，每一个都能让结论从「开得起」变成「开不起」：

1. **百分比先各自 round 到 2 位小数，再求和，最后只 ceil 一次。**
   不是「每个光环各自 ceil 再相加」——那会多算好几点魔力。
2. **保留效率是除法**（`/(1 + 效率/100)`），不是减法。「保留效率提高 25%」
   让 50% 变 40%，而不是 25%。
3. **`更多 <= 0` 或 `增加 <= -100` 时保留为 0**（PoB 的显式分支）。

## 输入从哪来

光环的基础保留取自 `pob_skill_level`（`reservation_percent` / `reservation_flat`）——
第二十四轮才补上定值那一列，先前清晰 / 精准 / 活力在库里看起来「不保留任何魔力」。
池大小与各项加成由调用方逐字给出：本模块不猜「你大概有多少魔力」。

## 不做的事

- 不算「保留效率」与「减少保留」各自来自哪件装备——那要读 build。
- 不算血魔法（光环改为保留生命）：那是 `BloodMagicReserved` 旗标改写整条链路，
  且库里没有生命保留的每级数值（判据见 `core/db.py` 的表注释）。
- 不判「这几个光环搭不搭」——那是取向问题，不是计算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from calc.derive import Step

#: PoB 原文位置。引用必须带行号锚点。
PERFORM_FILE = "src/Modules/CalcPerform.lua"
RESERVE_LINES = (1964, 1990)
TOTAL_LINES = (533, 546)


@dataclass(frozen=True)
class AuraReservation:
    """一个技能的保留。`base_percent` 与 `base_flat` 取自宝石每级数值。"""

    name: str
    base_percent: float = 0.0
    base_flat: float = 0.0
    #: 算完（含增加 / 更多 / 效率）之后的百分比，已 round 到 2 位
    percent: float = 0.0
    #: 算完之后的定值，已取整
    flat: float = 0.0

    @property
    def has_data(self) -> bool:
        return bool(self.base_percent or self.base_flat)


@dataclass(frozen=True)
class ReservationResult:
    pool: float
    pool_name: str
    auras: tuple[AuraReservation, ...]
    total_percent: float
    total_flat: float
    #: 实际被保留掉的池量：Σ定值 + ceil(池 × Σ百分比 / 100)
    reserved: float
    unreserved: float
    unreserved_pct: float
    #: 保留超过池时 PoB 会夹住；夹住意味着**开不起**
    over_reserved: bool
    steps: tuple[Step, ...]


def _one(
    name: str,
    base_percent: float,
    base_flat: float,
    *,
    increased_pct: float,
    more_mult: float,
    efficiency_pct: float,
    efficiency_more: float,
) -> AuraReservation:
    """单个技能的保留。照 `CalcPerform.lua:1976-1988`。"""
    if more_mult <= 0 or increased_pct <= -100:
        # PoB 的显式分支：这两种情况下保留直接是 0
        return AuraReservation(name=name, base_percent=base_percent, base_flat=base_flat)
    efficiency = max(efficiency_pct, -100.0)
    divisor = (1 + efficiency / 100) * efficiency_more
    if divisor <= 0:
        # 效率被压到 −100%（PoB 的下限）时除数为 0，Lua 得到 inf、
        # 随后被夹到池上限——也就是「保留满、开不起」。Python 不能崩，
        # 所以直接返回一个必然超池的量，语义与上游一致。
        return AuraReservation(
            name=name, base_percent=base_percent, base_flat=base_flat,
            percent=float("inf") if base_percent else 0.0,
            flat=float("inf") if base_flat else 0.0,
        )
    factor = (100 + increased_pct) / 100 * more_mult / divisor
    return AuraReservation(
        name=name,
        base_percent=base_percent,
        base_flat=base_flat,
        percent=max(round(base_percent * factor, 2), 0.0) if base_percent else 0.0,
        flat=max(round(base_flat * factor), 0.0) if base_flat else 0.0,
    )


def reserve(
    pool: float,
    auras: list[tuple[str, float, float]],
    *,
    pool_name: str = "魔力",
    increased_pct: float = 0.0,
    more_mult: float = 1.0,
    efficiency_pct: float = 0.0,
    efficiency_more: float = 1.0,
) -> ReservationResult:
    """算一组技能的总保留与剩余。

    `auras` 是 ``[(名字, 基础百分比, 基础定值)]``，两项都可以为 0。
    加成参数作用在**每一个**技能上（全局词缀就是这样生效的）；
    只影响某一个光环的词缀，本模块不建模——那要按技能配词缀，属 build 的活。
    """
    computed = tuple(
        _one(
            name, base_percent, base_flat,
            increased_pct=increased_pct, more_mult=more_mult,
            efficiency_pct=efficiency_pct, efficiency_more=efficiency_more,
        )
        for name, base_percent, base_flat in auras
    )
    total_percent = sum(a.percent for a in computed)
    total_percent = total_percent if math.isinf(total_percent) else round(total_percent, 2)
    total_flat = sum(a.flat for a in computed)
    # **一次 ceil**：不是每个光环各自 ceil 再相加（那会多算几点）
    reserved_raw = (
        float("inf")
        if math.isinf(total_percent) or math.isinf(total_flat)
        else total_flat + math.ceil(pool * total_percent / 100)
    )
    reserved = min(reserved_raw, pool)
    unreserved = pool - reserved

    steps: list[Step] = []
    for aura in computed:
        parts: list[str] = []
        if aura.base_percent:
            parts.append(f"{aura.base_percent:g}%")
        if aura.base_flat:
            parts.append(f"{aura.base_flat:g} 点")
        expr = " + ".join(parts) or "无保留数据"
        if increased_pct or more_mult != 1.0 or efficiency_pct or efficiency_more != 1.0:
            expr += (
                f" ×(100+{increased_pct:g})/100 × {more_mult:g}"
                f" ÷(1+{efficiency_pct:g}/100) ÷ {efficiency_more:g}"
            )
        shown = aura.percent if aura.base_percent else aura.flat
        steps.append(Step(f"{aura.name}", expr, shown))
    steps.append(
        Step("百分比合计（各自 round 到 2 位后求和）",
             " + ".join(f"{a.percent:g}%" for a in computed if a.percent) or "0%",
             total_percent)
    )
    if total_flat:
        steps.append(
            Step("定值合计", " + ".join(f"{a.flat:g}" for a in computed if a.flat), total_flat)
        )
    steps.append(
        Step(
            f"保留掉的{pool_name}",
            f"{total_flat:g} + 向上取整({pool:g} × {total_percent:g}% ) ",
            reserved,
        )
    )
    steps.append(Step(f"剩余{pool_name}", f"{pool:g} - {reserved:g}", unreserved))
    return ReservationResult(
        pool=float(pool),
        pool_name=pool_name,
        auras=computed,
        total_percent=total_percent,
        total_flat=total_flat,
        reserved=reserved,
        unreserved=unreserved,
        unreserved_pct=(unreserved / pool * 100) if pool else 0.0,
        over_reserved=reserved_raw > pool,
        steps=tuple(steps),
    )
