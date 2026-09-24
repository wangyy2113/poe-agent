"""显式输入的单技能命中 DPS 链。

复算 PoB 的基础伤害、伤害转化、增伤/更多、抗性与穿透、暴击、命中和频率主链。
输入必须由调用方明确提供；本模块不从不完整装备或描述中猜任何一项。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping


DAMAGE_TYPES = ("physical", "lightning", "cold", "fire", "chaos")
TYPE_ZH = {
    "physical": "物理", "lightning": "闪电", "cold": "冰霜",
    "fire": "火焰", "chaos": "混沌",
}
TYPE_ALIASES = {**{key: key for key in DAMAGE_TYPES}, **{value: key for key, value in TYPE_ZH.items()}}
OFFENCE_FILE = "src/Modules/CalcOffence.lua"
CONVERSION_LINES = (1879, 1922)
BASE_DAMAGE_LINES = (3163, 3180)
DAMAGE_LINES = (68, 151)
RATE_LINES = (2404, 2418)
SCALED_DAMAGE_LINES = (3101, 3127)
ROLL_LINES = (3298, 3313)
ENEMY_EFFECT_LINES = (3319, 3421)
FINAL_DPS_LINES = (3615, 3628)
BURST_LINES = (3624, 3680)
MAX_VALUE = 1_000_000_000_000_000.0


@dataclass(frozen=True)
class DamageRange:
    minimum: float
    maximum: float

    @property
    def average(self) -> float:
        return (self.minimum + self.maximum) / 2

    def scale(self, multiplier: float) -> "DamageRange":
        return DamageRange(self.minimum * multiplier, self.maximum * multiplier)

    def add(self, other: "DamageRange") -> "DamageRange":
        return DamageRange(self.minimum + other.minimum, self.maximum + other.maximum)


@dataclass(frozen=True)
class Conversion:
    source: str
    target: str
    percent: float
    kind: str = "global"  # global / skill / gain


@dataclass(frozen=True)
class TypeDamage:
    damage_type: str
    before_mitigation: DamageRange
    raw_resistance: float
    penetration: float
    effective_resistance: float
    resistance_ignore_chance_pct: float
    resistance_inversion_chance_pct: float
    resistance_unmodifiable: bool
    mitigation_multiplier: float
    after_mitigation: DamageRange
    damage_roll_chance_pct: float
    non_crit_average: float
    crit_average: float


@dataclass(frozen=True)
class HitDpsResult:
    conversion: dict[str, dict[str, object]]
    by_type: tuple[TypeDamage, ...]
    non_crit_hit: DamageRange
    non_crit_average: float
    crit_average: float
    average_hit: float
    average_damage_per_use: float
    dps: float
    hit_chance_pct: float
    crit_chance_pct: float
    crit_multiplier_pct: float
    crit_effect: float
    damage_roll_chance_pct: float
    crit_damage_roll_chance_pct: float
    extreme_luck: bool
    double_damage_chance_pct: float
    triple_damage_chance_pct: float
    scaled_damage_effect: float
    raw_action_rate: float
    action_rate: float
    cooldown_seconds: float
    triggered: bool
    skill_dps_multiplier: float
    quantity_multiplier: float
    burst_hit_count: float
    repeat_damage_multiplier: float
    average_burst_damage: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def damage_type(value: str) -> str:
    normalized = TYPE_ALIASES.get(str(value).strip().lower())
    if normalized is None:
        raise ValueError(f"未知伤害类型 {value!r}；只支持物理、闪电、冰霜、火焰、混沌")
    return normalized


def _number(value: float, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字")
    number = float(value)
    if not math.isfinite(number) or number < minimum or abs(number) > MAX_VALUE:
        raise ValueError(f"{label} 超出有效范围")
    return number


def _pob_round(value: float) -> float:
    """PoB ``Common.lua`` 的正数 round：``floor(value + 0.5)``。

    Python 的内置 ``round`` 会把 0.5 舍入到偶数，不能用来复刻伤害链。
    """
    return float(math.floor(value + 0.5))


def _bounded(value: float, label: str) -> float:
    if not math.isfinite(value) or abs(value) > MAX_VALUE:
        raise ValueError(f"{label} 超出有效范围")
    return value


def _chance(value: float, label: str, *, signed: bool = False) -> float:
    minimum = -100.0 if signed else 0.0
    chance = _number(value, label, minimum=-MAX_VALUE)
    if chance < minimum or chance > 100:
        raise ValueError(f"{label} 必须在 {minimum:g}%..100%")
    return chance


def _roll_average(damage: DamageRange, chance_pct: float, extreme: bool) -> float:
    """复刻 PoB 对幸运/不幸伤害区间的期望值模型。"""
    chance = chance_pct / 100
    rolls = abs(chance * (2 if extreme else 1)) + 2
    normal = damage.minimum / rolls + damage.maximum / rolls
    if chance >= 0:
        adjusted = damage.minimum / rolls + (rolls - 1) * damage.maximum / rolls
    else:
        adjusted = (rolls - 1) * damage.minimum / rolls + damage.maximum / rolls
    return normal * (1 - abs(chance)) + adjusted * abs(chance)


def effective_action_rate(raw_rate: float, cooldown_seconds: float = 0.0, *, triggered: bool = False) -> float:
    """按 PoB 的 hitTime/cooldown 顺序得到实际每秒击中次数。"""
    rate = _number(raw_rate, "action_rate")
    cooldown = _number(cooldown_seconds, "cooldown_seconds")
    if rate <= 0:
        raise ValueError("action_rate 必须大于 0")
    if cooldown == 0:
        return rate
    hit_time = 1 / rate
    return 1 / max(hit_time, cooldown) if triggered else 1 / (hit_time + cooldown)


def _ranges(raw: Mapping[str, tuple[float, float] | list[float]] | None, label: str) -> dict[str, DamageRange]:
    out = {kind: DamageRange(0.0, 0.0) for kind in DAMAGE_TYPES}
    for raw_type, pair in (raw or {}).items():
        kind = damage_type(raw_type)
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError(f"{label}.{raw_type} 必须是 [最小值, 最大值]")
        low = _number(pair[0], f"{label}.{raw_type}.minimum")
        high = _number(pair[1], f"{label}.{raw_type}.maximum")
        if low > high:
            raise ValueError(f"{label}.{raw_type} 的最小值不能大于最大值")
        out[kind] = DamageRange(low, high)
    return out


def _type_numbers(raw: Mapping[str, float] | None, label: str, *, minimum: float = -MAX_VALUE) -> dict[str, float]:
    out: dict[str, float] = {}
    for raw_type, value in (raw or {}).items():
        out[damage_type(raw_type)] = _number(value, f"{label}.{raw_type}", minimum=minimum)
    return out


def _conversion_table(conversions: Iterable[Conversion]) -> dict[str, dict[str, object]]:
    table: dict[str, dict[str, object]] = {}
    for source in DAMAGE_TYPES:
        table[source] = {"conversion": {}, "gain": {}, "mult": 1.0}

    grouped: dict[str, dict[str, dict[str, float]]] = {}
    for item in conversions:
        source, target = damage_type(item.source), damage_type(item.target)
        if DAMAGE_TYPES.index(target) <= DAMAGE_TYPES.index(source):
            raise ValueError(f"不支持 {TYPE_ZH[source]} → {TYPE_ZH[target]}：伤害只能沿 PoB 转化顺序前进")
        if item.kind not in {"global", "skill", "gain"}:
            raise ValueError("conversion.kind 只支持 global、skill、gain")
        percent = _number(item.percent, "转化/额外获得比例")
        bucket = grouped.setdefault(source, {}).setdefault(
            target, {"global": 0.0, "skill": 0.0, "gain": 0.0}
        )
        bucket[item.kind] += percent

    for source, destinations in grouped.items():
        skill_total = sum(parts["skill"] for parts in destinations.values())
        global_total = sum(parts["global"] for parts in destinations.values())
        if skill_total > 100:
            skill_factor, global_factor = 100 / skill_total, 0.0
        elif skill_total + global_total > 100:
            skill_factor = 1.0
            global_factor = (100 - skill_total) / global_total
        else:
            skill_factor = global_factor = 1.0
        converted = 0.0
        for target, parts in destinations.items():
            conversion = (parts["skill"] * skill_factor + parts["global"] * global_factor) / 100
            gain = parts["gain"] / 100
            table[source]["conversion"][target] = conversion
            table[source]["gain"][target] = gain
            table[source][target] = conversion + gain
            converted += conversion
        table[source]["mult"] = 1 - min(converted, 1.0)
    return table


def hit_dps(
    *,
    base_damage: Mapping[str, tuple[float, float] | list[float]],
    added_damage: Mapping[str, tuple[float, float] | list[float]] | None = None,
    base_multiplier: float = 1.0,
    damage_effectiveness: float = 1.0,
    added_damage_multiplier: float = 1.0,
    increased_damage_pct: float = 0.0,
    increased_by_type: Mapping[str, float] | None = None,
    more_damage_multipliers: Iterable[float] = (),
    more_by_type: Mapping[str, float] | None = None,
    conversions: Iterable[Conversion] = (),
    enemy_resistance: Mapping[str, float] | None = None,
    penetration: Mapping[str, float] | None = None,
    enemy_physical_reduction_pct: float = 0.0,
    overwhelm_physical_reduction_pct: float = 0.0,
    physical_reduction_ignore_chance_pct: float = 0.0,
    resistance_ignore_chance_pct: Mapping[str, float] | None = None,
    resistance_inversion_chance_pct: Mapping[str, float] | None = None,
    unmodifiable_resistance_types: Iterable[str] = (),
    enemy_damage_taken_increased_pct: float = 0.0,
    enemy_damage_taken_increased_by_type: Mapping[str, float] | None = None,
    enemy_damage_taken_more_multiplier: float = 1.0,
    enemy_damage_taken_more_by_type: Mapping[str, float] | None = None,
    hit_chance_pct: float = 100.0,
    crit_chance_pct: float = 0.0,
    crit_multiplier_pct: float = 150.0,
    damage_roll_chance_pct: float = 0.0,
    crit_damage_roll_chance_pct: float | None = None,
    extreme_luck: bool = False,
    double_damage_chance_pct: float = 0.0,
    triple_damage_chance_pct: float = 0.0,
    double_damage_chance_on_crit_pct: float = 0.0,
    triple_damage_chance_on_crit_pct: float = 0.0,
    action_rate: float = 1.0,
    cooldown_seconds: float = 0.0,
    triggered: bool = False,
    skill_dps_multiplier: float = 1.0,
    quantity_multiplier: float = 1.0,
    burst_hit_count: float = 1.0,
    repeat_damage_multiplier: float = 1.0,
) -> HitDpsResult:
    """复算单技能 hit DPS 与一次爆发；不含异常、持续伤害和穿刺。"""
    base = _ranges(base_damage, "base_damage")
    added = _ranges(added_damage, "added_damage")
    if not any(item.maximum > 0 for item in base.values()) and not any(item.maximum > 0 for item in added.values()):
        raise ValueError("至少需要一组非零基础伤害或附加伤害")

    base_mult = _number(base_multiplier, "base_multiplier")
    effectiveness = _number(damage_effectiveness, "damage_effectiveness")
    added_mult = _number(added_damage_multiplier, "added_damage_multiplier")
    global_inc = _number(increased_damage_pct, "increased_damage_pct", minimum=-100.0)
    type_inc = _type_numbers(increased_by_type, "increased_by_type", minimum=-100.0)
    type_more = _type_numbers(more_by_type, "more_by_type", minimum=0.0)
    global_more = 1.0
    for value in more_damage_multipliers:
        global_more = _bounded(
            global_more * _number(value, "more_damage_multipliers"),
            "more_damage_multipliers 的乘积",
        )
    resist = _type_numbers(enemy_resistance, "enemy_resistance", minimum=-200.0)
    pen = _type_numbers(penetration, "penetration")
    if "physical" in resist:
        raise ValueError("enemy_resistance 不接受物理伤害；请使用 enemy_physical_reduction_pct")
    if "physical" in pen:
        raise ValueError("penetration 不接受物理伤害；物理穿透/压倒尚未实现")
    physical_reduction = _number(enemy_physical_reduction_pct, "enemy_physical_reduction_pct")
    if physical_reduction > 90:
        raise ValueError("敌人物理伤害减免不能超过 90%")
    overwhelm = _chance(overwhelm_physical_reduction_pct, "overwhelm_physical_reduction_pct")
    physical_ignore = _chance(
        physical_reduction_ignore_chance_pct, "physical_reduction_ignore_chance_pct"
    )
    ignore_resist = _type_numbers(resistance_ignore_chance_pct, "resistance_ignore_chance_pct")
    invert_resist = _type_numbers(resistance_inversion_chance_pct, "resistance_inversion_chance_pct")
    for kind, chance in (*ignore_resist.items(), *invert_resist.items()):
        if kind == "physical":
            raise ValueError("抗性忽略/反转不接受物理伤害")
        _chance(chance, f"{kind} 抗性概率")
    immutable_resist = {damage_type(kind) for kind in unmodifiable_resistance_types}
    if "physical" in immutable_resist:
        raise ValueError("unmodifiable_resistance_types 不接受物理伤害")
    if "chaos" in immutable_resist:
        raise ValueError("unmodifiable_resistance_types 只支持闪电、冰霜和火焰")
    taken_inc = _number(enemy_damage_taken_increased_pct, "enemy_damage_taken_increased_pct", minimum=-100.0)
    taken_inc_type = _type_numbers(
        enemy_damage_taken_increased_by_type,
        "enemy_damage_taken_increased_by_type",
        minimum=-100.0,
    )
    taken_more = _number(enemy_damage_taken_more_multiplier, "enemy_damage_taken_more_multiplier")
    taken_more_type = _type_numbers(
        enemy_damage_taken_more_by_type, "enemy_damage_taken_more_by_type", minimum=0.0
    )
    hit_chance = _chance(hit_chance_pct, "hit_chance_pct")
    crit_chance = _chance(crit_chance_pct, "crit_chance_pct")
    crit_multi = _number(crit_multiplier_pct, "crit_multiplier_pct")
    normal_roll_chance = _chance(damage_roll_chance_pct, "damage_roll_chance_pct", signed=True)
    crit_roll_chance = _chance(
        crit_damage_roll_chance_pct if crit_damage_roll_chance_pct is not None else normal_roll_chance,
        "crit_damage_roll_chance_pct",
        signed=True,
    )
    triple = min(100.0, _chance(triple_damage_chance_pct, "triple_damage_chance_pct")
                 + _chance(triple_damage_chance_on_crit_pct,
                           "triple_damage_chance_on_crit_pct") * crit_chance / 100)
    double = min(100.0, _chance(double_damage_chance_pct, "double_damage_chance_pct")
                 + _chance(double_damage_chance_on_crit_pct,
                           "double_damage_chance_on_crit_pct") * crit_chance / 100)
    # PoB 让三倍伤害覆盖同时发生的双倍伤害概率。
    double = max(double - triple * double / 100, 0.0)
    scaled_effect = 1 + double / 100 + 2 * triple / 100
    raw_rate = _number(action_rate, "action_rate")
    cooldown = _number(cooldown_seconds, "cooldown_seconds")
    rate = effective_action_rate(raw_rate, cooldown, triggered=triggered)
    dps_mult = _number(skill_dps_multiplier, "skill_dps_multiplier")
    quantity = _number(quantity_multiplier, "quantity_multiplier")
    burst_hits = _number(burst_hit_count, "burst_hit_count")
    repeat_mult = _number(repeat_damage_multiplier, "repeat_damage_multiplier")
    if dps_mult <= 0 or quantity <= 0:
        raise ValueError("频率、技能 DPS 倍率与数量倍率必须大于 0")
    if burst_hits < 1:
        raise ValueError("burst_hit_count 必须至少为 1")

    initial = {
        kind: base[kind].scale(base_mult).add(added[kind].scale(effectiveness * added_mult))
        for kind in DAMAGE_TYPES
    }
    table = _conversion_table(conversions)

    def calculate(kind: str, lineage: frozenset[str]) -> DamageRange:
        flags = lineage | {kind}
        incoming = DamageRange(0.0, 0.0)
        for source in DAMAGE_TYPES[:DAMAGE_TYPES.index(kind)]:
            fraction = float(table[source].get(kind, 0.0))
            if fraction:
                incoming = incoming.add(calculate(source, flags).scale(fraction))
        # PoB 先合计所有转入/额外获得分支；只有区间两端都非零时才在这里取整。
        if incoming.minimum != 0 and incoming.maximum != 0:
            incoming = DamageRange(_pob_round(incoming.minimum), _pob_round(incoming.maximum))
        if initial[kind].minimum == 0 and initial[kind].maximum == 0:
            return incoming
        inc = max(0.0, 1 + (global_inc + sum(type_inc.get(item, 0.0) for item in flags)) / 100)
        more = global_more
        for item in flags:
            more = _bounded(more * type_more.get(item, 1.0), "more_by_type 的乘积")
        own = initial[kind].scale(_bounded(inc * more, "伤害修正倍率"))
        # CalcOffence.lua:127-128 在当前类型基础伤害与已取整的传入伤害相加后再 round。
        return DamageRange(
            _pob_round(_bounded(own.minimum + incoming.minimum, "最小伤害")),
            _pob_round(_bounded(own.maximum + incoming.maximum, "最大伤害")),
        )

    breakdown: list[TypeDamage] = []
    total_noncrit = DamageRange(0.0, 0.0)
    noncrit_average = 0.0
    crit_average = 0.0
    for kind in DAMAGE_TYPES:
        before = calculate(kind, frozenset()).scale(
            _bounded(float(table[kind]["mult"]) * scaled_effect, "伤害倍增期望倍率")
        )
        raw_resist = 0.0
        used_penetration = 0.0
        ignored = 0.0
        inverted = 0.0
        if kind == "physical":
            raw_resist = max(0.0, physical_reduction - overwhelm)
            ignored = physical_ignore
            effective_resist = raw_resist * (1 - physical_ignore / 100)
        else:
            raw_resist = max(-200.0, min(90.0, resist.get(kind, 0.0)))
            used_penetration = pen.get(kind, 0.0)
            ignored = ignore_resist.get(kind, 0.0)
            inverted = invert_resist.get(kind, 0.0)
            if kind in immutable_resist:
                effective_resist = raw_resist - 2 * inverted / 100 * raw_resist
                used_penetration = ignored = 0.0
            else:
                normal_effective = raw_resist - used_penetration
                inverted_effective = -raw_resist - used_penetration
                effective_resist = (
                    normal_effective * (1 - inverted / 100)
                    + inverted_effective * inverted / 100
                ) * (1 - ignored / 100)
        inc_taken_mult = max(
            0.0, 1 + (taken_inc + taken_inc_type.get(kind, 0.0)) / 100
        )
        more_taken_mult = _bounded(
            taken_more * taken_more_type.get(kind, 1.0), "按类型承伤倍率"
        )
        mitigation = (1 - effective_resist / 100) * inc_taken_mult * more_taken_mult
        after = before.scale(_bounded(mitigation, "承伤倍率"))
        if before.maximum or after.maximum:
            noncrit = _bounded(_roll_average(before, normal_roll_chance, extreme_luck) * mitigation,
                               "非暴击平均伤害")
            crit = _bounded(
                _roll_average(before.scale(crit_multi / 100), crit_roll_chance, extreme_luck)
                * mitigation,
                "暴击平均伤害",
            )
            breakdown.append(TypeDamage(
                kind, before, raw_resist, used_penetration, effective_resist,
                ignored, inverted, kind in immutable_resist, mitigation, after,
                normal_roll_chance, noncrit, crit,
            ))
            total_noncrit = total_noncrit.add(after)
            noncrit_average += noncrit
            crit_average += crit

    crit_effect = 1 - crit_chance / 100 + crit_chance / 100 * crit_multi / 100
    average_hit = _bounded(
        noncrit_average * (1 - crit_chance / 100) + crit_average * crit_chance / 100,
        "平均击中伤害",
    )
    average_use = _bounded(average_hit * hit_chance / 100, "每次使用期望伤害")
    total_dps = _bounded(average_use * rate * dps_mult * quantity, "命中 DPS")
    burst_damage = _bounded(
        average_use + average_use * (burst_hits - 1) * repeat_mult,
        "平均爆发伤害",
    )
    return HitDpsResult(
        conversion=table, by_type=tuple(breakdown), non_crit_hit=total_noncrit,
        non_crit_average=noncrit_average, crit_average=crit_average,
        average_hit=average_hit, average_damage_per_use=average_use, dps=total_dps,
        hit_chance_pct=hit_chance, crit_chance_pct=crit_chance,
        crit_multiplier_pct=crit_multi, crit_effect=crit_effect,
        damage_roll_chance_pct=normal_roll_chance,
        crit_damage_roll_chance_pct=crit_roll_chance, extreme_luck=bool(extreme_luck),
        double_damage_chance_pct=double, triple_damage_chance_pct=triple,
        scaled_damage_effect=scaled_effect, raw_action_rate=raw_rate, action_rate=rate,
        cooldown_seconds=cooldown, triggered=bool(triggered),
        skill_dps_multiplier=dps_mult, quantity_multiplier=quantity,
        burst_hit_count=burst_hits, repeat_damage_multiplier=repeat_mult,
        average_burst_damage=burst_damage,
    )
