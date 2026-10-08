"""显式输入的单技能 DPS 链。

复算 PoB 的基础伤害、伤害转化、增伤/更多、抗性与穿透、暴击、命中、频率、战斗周期、
稳态穿刺、伤害性异常、非异常技能 DoT 与终结等效输出主链。
输入必须由调用方明确提供；本模块不从不完整装备或描述中猜任何一项。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

from calc import constants


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
COOLDOWN_TICK_LINES = (274, 285)
COMBAT_EFFECT_LINES = (2805, 2861)
SCALED_DAMAGE_LINES = (3101, 3127)
CULL_LINES = (3129, 3137)
ROLL_LINES = (3298, 3313)
ENEMY_EFFECT_LINES = (3319, 3421)
FINAL_DPS_LINES = (3615, 3628)
BURST_LINES = (3624, 3680)
IMPALE_LINES = (5388, 5437)
IMPALE_DPS_LINES = (5943, 6000)
AILMENT_CHANCE_LINES = (3987, 4097)
AILMENT_DAMAGE_LINES = (4125, 4202)
BLEED_DPS_LINES = (4204, 4475)
POISON_DPS_LINES = (4477, 4780)
IGNITE_DPS_LINES = (4782, 5110)
SKILL_DOT_LINES = (5560, 5650)
SKILL_DOT_STACK_LINES = (5653, 5705)
TOTAL_DOT_LINES = (6042, 6054)
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
class DamagingAilmentDps:
    name: str
    name_en: str
    source_damage: DamageRange
    crit_source_damage: DamageRange
    chance_on_hit_pct: float
    chance_on_crit_pct: float
    effective_chance_pct: float
    application_rate: float
    duration_seconds: float
    stack_potential: float
    active_stacks: float
    stack_limit: float | None
    average_roll_pct: float
    ailment_crit_chance_pct: float
    average_source_damage: float
    percent_per_second: float
    dot_multiplier_pct: float
    crit_dot_multiplier_pct: float
    mitigation_pct: float
    damage_taken_multiplier: float
    single_instance_dps: float
    single_instance_total_damage: float
    uncapped_dps: float
    dps: float
    dps_cap: float
    capped: bool


@dataclass(frozen=True)
class SkillDotType:
    damage_type: str
    base_dps: float
    increased_pct: float
    more_multiplier: float
    dot_multiplier_pct: float
    aura_effect_multiplier: float
    mitigation_pct: float
    damage_taken_multiplier: float
    effective_multiplier: float
    dps: float


@dataclass(frozen=True)
class SkillDotDps:
    by_type: tuple[SkillDotType, ...]
    per_instance_uncapped_dps: float
    per_instance_dps: float
    can_stack: bool
    duration_seconds: float
    application_rate: float
    stack_potential: float
    active_instances: float
    stack_limit: float | None
    instance_multiplier: float
    uncapped_dps: float
    dps: float
    dps_cap: float
    capped: bool


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
    ruthless_blow_chance_pct: float
    ruthless_blow_effect: float
    fist_of_war_uptime_pct: float
    fist_of_war_effect: float
    exerted_attack_uptime_pct: float
    exerted_attack_effect: float
    combat_damage_effect: float
    raw_action_rate: float
    tick_limited_action_rate: float
    action_rate_tick_capped: bool
    action_repeats: int
    channelled: bool
    action_rate: float
    cooldown_seconds: float
    effective_cooldown_seconds: float
    cooldown_tick_rounded: bool
    server_tick_seconds: float | None
    cooldown_uses: int
    triggered: bool
    skill_dps_multiplier: float
    quantity_multiplier: float
    burst_hit_count: float
    repeat_damage_multiplier: float
    average_burst_damage: float
    physical_hit_before_mitigation_average: float
    impale_chance_pct: float
    impale_stacks: float
    impale_stored_damage_pct: float
    impale_multiplier: float
    impale_dps: float
    combined_dps: float
    cull_percent: float
    cull_multiplier: float
    culling_dps: float
    effective_dps_with_cull: float
    damaging_ailments: tuple[DamagingAilmentDps, ...]
    ailment_dps: float
    skill_dot: SkillDotDps | None
    skill_dot_dps: float
    total_dot_dps_uncapped: float
    total_dot_dps: float
    total_dot_dps_cap: float
    total_dot_dps_capped: bool
    total_dps: float
    effective_total_dps_with_cull: float

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


def effective_cooldown(
    cooldown_seconds: float,
    *,
    server_tick_seconds: float | None = None,
    cooldown_uses: int = 1,
) -> tuple[float, bool]:
    """按 PoB 规则把单次充能冷却向上对齐服务器 tick。"""
    cooldown = _number(cooldown_seconds, "cooldown_seconds")
    if isinstance(cooldown_uses, bool) or not isinstance(cooldown_uses, int) or cooldown_uses < 1:
        raise ValueError("cooldown_uses 必须是至少为 1 的整数")
    if server_tick_seconds is None:
        return cooldown, False
    tick = _number(server_tick_seconds, "server_tick_seconds")
    if tick <= 0:
        raise ValueError("server_tick_seconds 必须大于 0")
    if cooldown == 0 or cooldown_uses > 1:
        return cooldown, False
    rounded = math.ceil(cooldown / tick) * tick
    return rounded, not math.isclose(rounded, cooldown, rel_tol=0, abs_tol=1e-12)


def effective_action_rate(
    raw_rate: float,
    cooldown_seconds: float = 0.0,
    *,
    triggered: bool = False,
    server_tick_seconds: float | None = None,
    cooldown_uses: int = 1,
    action_repeats: int = 1,
    channelled: bool = False,
) -> float:
    """按 PoB 的 hitTime/cooldown 顺序得到实际每秒击中次数。"""
    rate = _number(raw_rate, "action_rate")
    if (
        isinstance(action_repeats, bool)
        or not isinstance(action_repeats, int)
        or action_repeats < 1
    ):
        raise ValueError("action_repeats 必须是至少为 1 的整数")
    if server_tick_seconds is not None and not channelled:
        tick = _number(server_tick_seconds, "server_tick_seconds")
        if tick <= 0:
            raise ValueError("server_tick_seconds 必须大于 0")
        rate = min(rate, action_repeats / tick)
    cooldown, _rounded = effective_cooldown(
        cooldown_seconds,
        server_tick_seconds=server_tick_seconds,
        cooldown_uses=cooldown_uses,
    )
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


_AILMENT_SPECS: dict[str, tuple[str, str, str, str, float | None]] = {
    "ignite": ("点燃", "Ignite", constants.IGNITE_PERCENT, constants.IGNITE_DURATION, 1.0),
    "poison": ("中毒", "Poison", constants.POISON_PERCENT, constants.POISON_DURATION, None),
    "bleed": ("流血", "Bleed", constants.BLEED_PERCENT, constants.BLEED_DURATION, 1.0),
}
_AILMENT_ALIASES = {
    "ignite": "ignite", "点燃": "ignite",
    "poison": "poison", "中毒": "poison",
    "bleed": "bleed", "流血": "bleed",
}


def _ailment_range(value: object, label: str) -> DamageRange:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise ValueError(f"{label} 必须是 [最小值, 最大值]")
    low = _number(value[0], f"{label}.minimum")  # type: ignore[arg-type]
    high = _number(value[1], f"{label}.maximum")  # type: ignore[arg-type]
    if low > high:
        raise ValueError(f"{label} 的最小值不能大于最大值")
    return DamageRange(low, high)


def _calculate_damaging_ailments(
    raw_ailments: Iterable[Mapping[str, object]],
    *,
    crit_chance_pct: float,
    hit_chance_pct: float,
    action_rate: float,
    skill_dps_multiplier: float,
    quantity_multiplier: float,
    ruthless_chance_pct: float,
    fist_of_war_effect: float,
    shared_taken_increased_pct: float,
    shared_taken_increased_by_type: Mapping[str, float],
    shared_taken_more_multiplier: float,
    shared_taken_more_by_type: Mapping[str, float],
    enemy_resistance: Mapping[str, float],
    enemy_physical_reduction_pct: float,
) -> tuple[DamagingAilmentDps, ...]:
    """按 PoB 的稳态层数与强实例加权模型计算三种伤害性异常。

    ``source_damage`` 是异常专用的减伤前源伤区间。调用方必须显式提供它，避免把穿透、
    双倍/三倍伤害或仅击中生效的修正错误带入异常伤害。
    """
    ailments = list(raw_ailments)
    if not ailments:
        return ()
    if len(ailments) > 3:
        raise ValueError("damaging_ailments 最多各提供一种点燃、中毒和流血")
    cap = constants.get(constants.DOT_DPS_CAP)
    monster_phys_cap = constants.get(constants.MONSTER_MAX_PHYS_DR)
    seen: set[str] = set()
    results: list[DamagingAilmentDps] = []
    for index, raw in enumerate(ailments):
        if not isinstance(raw, Mapping):
            raise ValueError(f"damaging_ailments[{index}] 必须是对象")
        alias = str(raw.get("name", "")).strip().lower()
        kind = _AILMENT_ALIASES.get(alias)
        if kind is None:
            raise ValueError("异常 name 只支持 ignite/点燃、poison/中毒、bleed/流血")
        if kind in seen:
            raise ValueError(f"damaging_ailments 不能重复提供 {_AILMENT_SPECS[kind][0]}")
        seen.add(kind)
        zh, en, percent_key, duration_key, default_limit = _AILMENT_SPECS[kind]
        if "source_damage" not in raw:
            raise ValueError(f"{zh}必须显式提供异常专用 source_damage，不能从最终命中伤害反推")
        source = _ailment_range(raw["source_damage"], f"{zh}.source_damage")
        crit_source_value = raw.get("crit_source_damage")
        crit_source = _ailment_range(
            raw["source_damage"] if crit_source_value is None else crit_source_value,
            f"{zh}.crit_source_damage",
        )
        chance_hit = _chance(  # type: ignore[arg-type]
            raw.get("chance_on_hit_pct", 0.0), f"{zh}.chance_on_hit_pct"
        )
        chance_crit_value = raw.get("chance_on_crit_pct")
        chance_crit = _chance(  # type: ignore[arg-type]
            chance_hit if chance_crit_value is None else chance_crit_value,
            f"{zh}.chance_on_crit_pct",
        )
        effective_chance = (
            chance_hit * (1 - crit_chance_pct / 100)
            + chance_crit * crit_chance_pct / 100
        )
        extra_stacks = _number(
            raw.get("stacks_per_application", 1.0), f"{zh}.stacks_per_application"  # type: ignore[arg-type]
        )
        if extra_stacks <= 0:
            raise ValueError(f"{zh}.stacks_per_application 必须大于 0")
        duration_value = raw.get("base_duration_seconds")
        duration_base = _number(
            constants.get(duration_key) if duration_value is None else duration_value,  # type: ignore[arg-type]
            f"{zh}.base_duration_seconds",
        )
        duration_inc = _number(
            raw.get("duration_increased_pct", 0.0),  # type: ignore[arg-type]
            f"{zh}.duration_increased_pct",
            minimum=-100.0,
        )
        faster_pct = _number(
            raw.get("faster_damage_pct", 0.0),  # type: ignore[arg-type]
            f"{zh}.faster_damage_pct",
            minimum=-99.999999,
        )
        rate_mod = 1 + faster_pct / 100
        duration = duration_base * (1 + duration_inc / 100) / rate_mod
        # PoB 只把 quantityMultiplier 接入中毒层数；点燃/流血以同一目标上的强实例/层数上限
        # 结算，不能把投射物数量直接当成更多同时生效层数。
        application_quantity = quantity_multiplier if kind == "poison" else 1.0
        application_rate = (
            action_rate * skill_dps_multiplier * application_quantity
            * hit_chance_pct / 100 * effective_chance / 100 * extra_stacks
        )
        stack_potential = application_rate * duration
        raw_limit = raw.get("stack_limit", default_limit)
        stack_limit = (
            None
            if raw_limit is None
            else _number(raw_limit, f"{zh}.stack_limit")  # type: ignore[arg-type]
        )
        if stack_limit is not None and stack_limit <= 0:
            raise ValueError(f"{zh}.stack_limit 必须大于 0")
        active_stacks = min(stack_potential, stack_limit) if stack_limit is not None else stack_potential

        if (
            kind in {"ignite", "bleed"}
            and stack_limit is not None
            and stack_potential / stack_limit > 1
        ):
            roll = (stack_potential - (stack_limit - 1) / 2) / (stack_potential + 1)
        else:
            roll = 0.5
        roll = min(max(roll, 0.0), 1.0)
        candidate_count = max(stack_potential, 1.0) if kind in {"ignite", "bleed"} else 1.0
        ailment_crit_chance = 100 * (1 - (1 - crit_chance_pct / 100) ** candidate_count)
        normal_source = source.minimum + (source.maximum - source.minimum) * roll
        critical_source = crit_source.minimum + (crit_source.maximum - crit_source.minimum) * roll
        normal_dot_multi = _number(  # type: ignore[arg-type]
            raw.get("dot_multiplier_pct", 0.0),
            f"{zh}.dot_multiplier_pct",
            minimum=-100.0,
        )
        crit_dot_value = raw.get("crit_dot_multiplier_pct")
        crit_dot_multi = _number(
            normal_dot_multi if crit_dot_value is None else crit_dot_value,  # type: ignore[arg-type]
            f"{zh}.crit_dot_multiplier_pct",
            minimum=-100.0,
        )
        chance_from_hit = chance_hit / 100 * (1 - ailment_crit_chance / 100)
        chance_from_crit = chance_crit / 100 * ailment_crit_chance / 100
        chance_total = chance_from_hit + chance_from_crit
        if chance_total > 0:
            average_source = (
                normal_source * (1 + normal_dot_multi / 100) * chance_from_hit
                + critical_source * (1 + crit_dot_multi / 100) * chance_from_crit
            ) / chance_total
        else:
            average_source = 0.0

        percent_raw = constants.get(percent_key)
        percent = percent_raw * 100 if kind in {"ignite", "poison"} else percent_raw
        effect_inc = _number(
            raw.get("ailment_effect_increased_pct", 0.0),  # type: ignore[arg-type]
            f"{zh}.ailment_effect_increased_pct",
            minimum=-100.0,
        )
        ruthless_multi = _number(
            raw.get("ruthless_blow_ailment_multiplier", 1.0),  # type: ignore[arg-type]
            f"{zh}.ruthless_blow_ailment_multiplier",
        )
        ruthless_effect = 1 + ruthless_chance_pct / 100 * (ruthless_multi - 1)
        warcry_more = _number(
            raw.get("exerted_ailment_more_damage_pct", 0.0),  # type: ignore[arg-type]
            f"{zh}.exerted_ailment_more_damage_pct",
        )
        warcry_uptime = _chance(
            raw.get("exerted_ailment_uptime_pct", 0.0),  # type: ignore[arg-type]
            f"{zh}.exerted_ailment_uptime_pct",
        )
        combat_effect = ruthless_effect * fist_of_war_effect * (
            1 + warcry_more / 100 * warcry_uptime / 100
        )

        damage_type_key = (
            "fire" if kind == "ignite" else "chaos" if kind == "poison" else "physical"
        )
        if "enemy_mitigation_pct" in raw:
            mitigation_pct = _number(
                raw["enemy_mitigation_pct"], f"{zh}.enemy_mitigation_pct", minimum=-200.0  # type: ignore[arg-type]
            )
        elif kind == "bleed":
            mitigation_pct = min(max(enemy_physical_reduction_pct, 0.0), monster_phys_cap)
        else:
            mitigation_pct = max(
                -200.0, min(90.0, enemy_resistance.get(damage_type_key, 0.0))
            )
        if mitigation_pct > (monster_phys_cap if kind == "bleed" else 90):
            raise ValueError(f"{zh}.enemy_mitigation_pct 超出有效上限")
        ailment_taken_inc = _number(
            raw.get(
                "enemy_damage_taken_increased_pct",
                shared_taken_increased_pct + shared_taken_increased_by_type.get(damage_type_key, 0.0),
            ),  # type: ignore[arg-type]
            f"{zh}.enemy_damage_taken_increased_pct",
            minimum=-100.0,
        )
        ailment_taken_more = _number(
            raw.get(
                "enemy_damage_taken_more_multiplier",
                shared_taken_more_multiplier * shared_taken_more_by_type.get(damage_type_key, 1.0),
            ),  # type: ignore[arg-type]
            f"{zh}.enemy_damage_taken_more_multiplier",
        )
        taken_multiplier = max(0.0, 1 + ailment_taken_inc / 100) * ailment_taken_more
        single_dps = _bounded(
            average_source * percent / 100 * combat_effect
            * (1 + effect_inc / 100) * rate_mod
            * (1 - mitigation_pct / 100) * taken_multiplier,
            f"{zh}单实例 DPS",
        )
        uncapped = _bounded(single_dps * active_stacks, f"{zh}稳态 DPS")
        capped_dps = min(uncapped, cap)
        results.append(DamagingAilmentDps(
            name=zh, name_en=en, source_damage=source, crit_source_damage=crit_source,
            chance_on_hit_pct=chance_hit, chance_on_crit_pct=chance_crit,
            effective_chance_pct=effective_chance, application_rate=application_rate,
            duration_seconds=duration, stack_potential=stack_potential,
            active_stacks=active_stacks, stack_limit=stack_limit,
            average_roll_pct=roll * 100, ailment_crit_chance_pct=ailment_crit_chance,
            average_source_damage=average_source, percent_per_second=percent,
            dot_multiplier_pct=normal_dot_multi,
            crit_dot_multiplier_pct=crit_dot_multi, mitigation_pct=mitigation_pct,
            damage_taken_multiplier=taken_multiplier, single_instance_dps=single_dps,
            single_instance_total_damage=single_dps * duration,
            uncapped_dps=uncapped, dps=capped_dps, dps_cap=cap,
            capped=uncapped > cap,
        ))
    return tuple(results)


def _calculate_skill_dot(
    raw: Mapping[str, object] | None,
    *,
    action_rate: float,
    skill_dps_multiplier: float,
    quantity_multiplier: float,
    shared_taken_increased_pct: float,
    shared_taken_increased_by_type: Mapping[str, float],
    shared_taken_more_multiplier: float,
    shared_taken_more_by_type: Mapping[str, float],
    enemy_resistance: Mapping[str, float],
    enemy_physical_reduction_pct: float,
) -> SkillDotDps | None:
    """计算技能自身的非异常 DoT，复刻 PoB 的逐类型与叠层顺序。

    ``base_damage_per_second`` 必须已经是技能面板的每秒基础值；它不会从命中伤害
    推导。穿透、命中率、暴击、双倍/三倍伤害均不会进入这条持续伤害链。
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("skill_dot 必须是对象")
    base_raw = raw.get("base_damage_per_second")
    if not isinstance(base_raw, Mapping) or not base_raw:
        raise ValueError("skill_dot.base_damage_per_second 必须提供至少一种伤害类型")
    base = _type_numbers(base_raw, "skill_dot.base_damage_per_second")  # type: ignore[arg-type]
    if not any(value > 0 for value in base.values()):
        raise ValueError("skill_dot.base_damage_per_second 至少需要一个正数")

    global_inc = _number(
        raw.get("increased_damage_pct", 0.0),  # type: ignore[arg-type]
        "skill_dot.increased_damage_pct", minimum=-100.0,
    )
    type_inc_raw = raw.get("increased_by_type")
    if type_inc_raw is not None and not isinstance(type_inc_raw, Mapping):
        raise ValueError("skill_dot.increased_by_type 必须是对象")
    type_inc = _type_numbers(
        type_inc_raw, "skill_dot.increased_by_type", minimum=-100.0  # type: ignore[arg-type]
    )
    global_more = 1.0
    raw_more = raw.get("more_damage_multipliers", ())
    if not isinstance(raw_more, (tuple, list)):
        raise ValueError("skill_dot.more_damage_multipliers 必须是数组")
    if len(raw_more) > 50:
        raise ValueError("skill_dot.more_damage_multipliers 最多 50 项")
    for value in raw_more:
        global_more = _bounded(
            global_more * _number(value, "skill_dot.more_damage_multipliers"),
            "skill_dot.more_damage_multipliers 的乘积",
        )
    type_more_raw = raw.get("more_by_type")
    if type_more_raw is not None and not isinstance(type_more_raw, Mapping):
        raise ValueError("skill_dot.more_by_type 必须是对象")
    type_more = _type_numbers(
        type_more_raw, "skill_dot.more_by_type", minimum=0.0  # type: ignore[arg-type]
    )
    dot_multi = _number(
        raw.get("dot_multiplier_pct", 0.0),  # type: ignore[arg-type]
        "skill_dot.dot_multiplier_pct", minimum=-100.0,
    )
    dot_multi_type_raw = raw.get("dot_multiplier_by_type")
    if dot_multi_type_raw is not None and not isinstance(dot_multi_type_raw, Mapping):
        raise ValueError("skill_dot.dot_multiplier_by_type 必须是对象")
    dot_multi_type = _type_numbers(
        dot_multi_type_raw, "skill_dot.dot_multiplier_by_type", minimum=-100.0  # type: ignore[arg-type]
    )
    aura = _number(
        raw.get("aura_effect_multiplier", 1.0),  # type: ignore[arg-type]
        "skill_dot.aura_effect_multiplier",
    )

    mitigation_raw = raw.get("enemy_mitigation_pct_by_type")
    if mitigation_raw is not None and not isinstance(mitigation_raw, Mapping):
        raise ValueError("skill_dot.enemy_mitigation_pct_by_type 必须是对象")
    mitigation_override = _type_numbers(
        mitigation_raw, "skill_dot.enemy_mitigation_pct_by_type", minimum=-200.0  # type: ignore[arg-type]
    )
    dot_taken_inc = _number(
        raw.get("enemy_damage_taken_increased_pct", 0.0),  # type: ignore[arg-type]
        "skill_dot.enemy_damage_taken_increased_pct", minimum=-100.0,
    )
    dot_taken_inc_type_raw = raw.get("enemy_damage_taken_increased_by_type")
    if dot_taken_inc_type_raw is not None and not isinstance(dot_taken_inc_type_raw, Mapping):
        raise ValueError("skill_dot.enemy_damage_taken_increased_by_type 必须是对象")
    dot_taken_inc_type = _type_numbers(
        dot_taken_inc_type_raw,
        "skill_dot.enemy_damage_taken_increased_by_type",
        minimum=-100.0,  # type: ignore[arg-type]
    )
    dot_taken_more = _number(
        raw.get("enemy_damage_taken_more_multiplier", 1.0),  # type: ignore[arg-type]
        "skill_dot.enemy_damage_taken_more_multiplier",
    )
    dot_taken_more_type_raw = raw.get("enemy_damage_taken_more_by_type")
    if dot_taken_more_type_raw is not None and not isinstance(dot_taken_more_type_raw, Mapping):
        raise ValueError("skill_dot.enemy_damage_taken_more_by_type 必须是对象")
    dot_taken_more_type = _type_numbers(
        dot_taken_more_type_raw,
        "skill_dot.enemy_damage_taken_more_by_type",
        minimum=0.0,  # type: ignore[arg-type]
    )

    cap = constants.get(constants.DOT_DPS_CAP)
    monster_phys_cap = constants.get(constants.MONSTER_MAX_PHYS_DR)
    rows: list[SkillDotType] = []
    per_instance_uncapped = 0.0
    for kind in DAMAGE_TYPES:
        base_dps = base.get(kind, 0.0)
        if base_dps <= 0:
            continue
        inc = global_inc + type_inc.get(kind, 0.0)
        more = _bounded(global_more * type_more.get(kind, 1.0), "技能 DoT 更多伤害乘积")
        multi = dot_multi + dot_multi_type.get(kind, 0.0)
        if kind in mitigation_override:
            mitigation = mitigation_override[kind]
        elif kind == "physical":
            mitigation = min(max(enemy_physical_reduction_pct, 0.0), monster_phys_cap)
        else:
            mitigation = min(max(enemy_resistance.get(kind, 0.0), -200.0), 90.0)
        if kind == "physical" and not 0 <= mitigation <= monster_phys_cap:
            raise ValueError(
                f"skill_dot.enemy_mitigation_pct_by_type.physical 必须在 0%..{monster_phys_cap:g}%"
            )
        if kind != "physical" and not -200 <= mitigation <= 90:
            raise ValueError(
                f"skill_dot.enemy_mitigation_pct_by_type.{kind} 必须在 -200%..90%"
            )
        taken_inc = (
            shared_taken_increased_pct
            + shared_taken_increased_by_type.get(kind, 0.0)
            + dot_taken_inc
            + dot_taken_inc_type.get(kind, 0.0)
        )
        taken_more = _bounded(
            shared_taken_more_multiplier
            * shared_taken_more_by_type.get(kind, 1.0)
            * dot_taken_more
            * dot_taken_more_type.get(kind, 1.0),
            "技能 DoT 敌方承伤更多乘积",
        )
        damage_taken = max(0.0, 1 + taken_inc / 100) * taken_more
        effective = _bounded((1 - mitigation / 100) * damage_taken, "技能 DoT 有效倍率")
        dps = _bounded(
            base_dps * max(0.0, 1 + inc / 100) * more
            * max(0.0, 1 + multi / 100) * aura * effective,
            f"{TYPE_ZH[kind]}技能 DoT",
        )
        rows.append(SkillDotType(
            damage_type=kind, base_dps=base_dps, increased_pct=inc,
            more_multiplier=more, dot_multiplier_pct=multi,
            aura_effect_multiplier=aura, mitigation_pct=mitigation,
            damage_taken_multiplier=damage_taken, effective_multiplier=effective,
            dps=dps,
        ))
        per_instance_uncapped = _bounded(
            per_instance_uncapped + dps, "技能 DoT 单实例合计"
        )
    per_instance = min(per_instance_uncapped, cap)
    can_stack = bool(raw.get("can_stack", False))
    duration = _number(
        raw.get("duration_seconds", 0.0),  # type: ignore[arg-type]
        "skill_dot.duration_seconds",
    )
    rate_multiplier = _number(
        raw.get("application_rate_multiplier", 1.0),  # type: ignore[arg-type]
        "skill_dot.application_rate_multiplier",
    )
    if rate_multiplier <= 0:
        raise ValueError("skill_dot.application_rate_multiplier 必须大于 0")
    instance_multiplier = _number(
        raw.get("instance_multiplier", 1.0),  # type: ignore[arg-type]
        "skill_dot.instance_multiplier",
    )
    if instance_multiplier <= 0:
        raise ValueError("skill_dot.instance_multiplier 必须大于 0")
    raw_limit = raw.get("stack_limit")
    stack_limit = (
        None if raw_limit is None
        else _number(raw_limit, "skill_dot.stack_limit")  # type: ignore[arg-type]
    )
    if stack_limit is not None and stack_limit <= 0:
        raise ValueError("skill_dot.stack_limit 必须大于 0")
    if can_stack:
        application_rate = _bounded(
            action_rate * rate_multiplier * skill_dps_multiplier * quantity_multiplier,
            "技能 DoT 施加率",
        )
        stack_potential = _bounded(application_rate * duration, "技能 DoT 潜在层数")
        active_instances = (
            min(stack_potential, stack_limit) if stack_limit is not None else stack_potential
        )
    else:
        application_rate = 0.0
        stack_potential = instance_multiplier
        active_instances = (
            min(instance_multiplier, stack_limit) if stack_limit is not None
            else instance_multiplier
        )
    uncapped = _bounded(per_instance * active_instances, "技能 DoT 总 DPS")
    return SkillDotDps(
        by_type=tuple(rows), per_instance_uncapped_dps=per_instance_uncapped,
        per_instance_dps=per_instance, can_stack=can_stack,
        duration_seconds=duration, application_rate=application_rate,
        stack_potential=stack_potential, active_instances=active_instances,
        stack_limit=stack_limit, instance_multiplier=instance_multiplier,
        uncapped_dps=uncapped, dps=min(uncapped, cap), dps_cap=cap,
        capped=uncapped > cap or per_instance_uncapped > cap,
    )


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
    ruthless_blow_every_hits: int = 0,
    ruthless_blow_hit_multiplier: float = 1.0,
    fist_of_war_more_damage_pct: float = 0.0,
    fist_of_war_cooldown_seconds: float = 0.0,
    exerted_attack_more_damage_pct: float = 0.0,
    exerted_attack_uptime_pct: float = 0.0,
    action_rate: float = 1.0,
    cooldown_seconds: float = 0.0,
    server_tick_seconds: float | None = None,
    cooldown_uses: int = 1,
    action_repeats: int = 1,
    channelled: bool = False,
    triggered: bool = False,
    skill_dps_multiplier: float = 1.0,
    quantity_multiplier: float = 1.0,
    burst_hit_count: float = 1.0,
    repeat_damage_multiplier: float = 1.0,
    impale_chance_pct: float = 0.0,
    impale_chance_on_crit_pct: float | None = None,
    impale_stacks: float = 0.0,
    impale_base_stored_damage_pct: float = 10.0,
    impale_effect_increased_pct: float = 0.0,
    impale_effect_more_multiplier: float = 1.0,
    impale_duration_seconds: float = 4.0,
    enemy_impale_physical_reduction_pct: float | None = None,
    ignore_enemy_impale_physical_reduction: bool = False,
    enemy_reflected_damage_taken_increased_pct: float = 0.0,
    enemy_reflected_damage_taken_more_multiplier: float = 1.0,
    cull_percent: float = 0.0,
    critical_cull_percent: float = 0.0,
    damaging_ailments: Iterable[Mapping[str, object]] = (),
    skill_dot: Mapping[str, object] | None = None,
) -> HitDpsResult:
    """复算单技能 hit、穿刺、异常、技能 DoT、终结等效 DPS 与一次爆发。"""
    base = _ranges(base_damage, "base_damage")
    added = _ranges(added_damage, "added_damage")
    if (
        not any(item.maximum > 0 for item in base.values())
        and not any(item.maximum > 0 for item in added.values())
        and skill_dot is None
    ):
        raise ValueError("至少需要一组非零基础/附加命中伤害或 skill_dot")

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
    effective_cd, cooldown_rounded = effective_cooldown(
        cooldown,
        server_tick_seconds=server_tick_seconds,
        cooldown_uses=cooldown_uses,
    )
    if (
        isinstance(action_repeats, bool)
        or not isinstance(action_repeats, int)
        or action_repeats < 1
    ):
        raise ValueError("action_repeats 必须是至少为 1 的整数")
    tick_limited_rate = raw_rate
    if server_tick_seconds is not None and not channelled:
        tick = _number(server_tick_seconds, "server_tick_seconds")
        if tick <= 0:
            raise ValueError("server_tick_seconds 必须大于 0")
        tick_limited_rate = min(raw_rate, action_repeats / tick)
    rate = effective_action_rate(
        raw_rate,
        cooldown,
        triggered=triggered,
        server_tick_seconds=server_tick_seconds,
        cooldown_uses=cooldown_uses,
        action_repeats=action_repeats,
        channelled=channelled,
    )
    dps_mult = _number(skill_dps_multiplier, "skill_dps_multiplier")
    quantity = _number(quantity_multiplier, "quantity_multiplier")
    burst_hits = _number(burst_hit_count, "burst_hit_count")
    repeat_mult = _number(repeat_damage_multiplier, "repeat_damage_multiplier")
    if dps_mult <= 0 or quantity <= 0:
        raise ValueError("频率、技能 DPS 倍率与数量倍率必须大于 0")
    if burst_hits < 1:
        raise ValueError("burst_hit_count 必须至少为 1")

    if isinstance(ruthless_blow_every_hits, bool) or not isinstance(ruthless_blow_every_hits, int):
        raise ValueError("ruthless_blow_every_hits 必须是整数")
    if ruthless_blow_every_hits < 0:
        raise ValueError("ruthless_blow_every_hits 不能为负数")
    ruthless_multiplier = _number(ruthless_blow_hit_multiplier, "ruthless_blow_hit_multiplier")
    ruthless_chance = (
        _pob_round(100 / ruthless_blow_every_hits) if ruthless_blow_every_hits else 0.0
    )
    ruthless_effect = 1 + ruthless_chance / 100 * (ruthless_multiplier - 1)
    fist_more = _number(fist_of_war_more_damage_pct, "fist_of_war_more_damage_pct") / 100
    fist_cooldown = _number(fist_of_war_cooldown_seconds, "fist_of_war_cooldown_seconds")
    if fist_more > 0 and fist_cooldown <= 0:
        raise ValueError("启用战争之拳伤害时必须提供正数 fist_of_war_cooldown_seconds")
    fist_uptime = min((1 / rate) / fist_cooldown, 1.0) if fist_more > 0 else 0.0
    fist_effect = 1 + fist_more * fist_uptime
    exert_uptime = _chance(exerted_attack_uptime_pct, "exerted_attack_uptime_pct")
    exert_more = _number(exerted_attack_more_damage_pct, "exerted_attack_more_damage_pct") / 100
    exert_effect = 1 + exert_more * exert_uptime / 100
    combat_effect = _bounded(
        ruthless_effect * fist_effect * exert_effect, "战斗机制期望倍率"
    )

    impale_chance = _chance(impale_chance_pct, "impale_chance_pct")
    impale_crit_chance = _chance(
        impale_chance if impale_chance_on_crit_pct is None else impale_chance_on_crit_pct,
        "impale_chance_on_crit_pct",
    )
    effective_impale_chance = (
        impale_chance * (1 - crit_chance / 100) + impale_crit_chance * crit_chance / 100
    )
    impale_stack_count = _number(impale_stacks, "impale_stacks")
    impale_base = _number(impale_base_stored_damage_pct, "impale_base_stored_damage_pct")
    impale_inc = _number(
        impale_effect_increased_pct, "impale_effect_increased_pct", minimum=-100.0
    )
    impale_more = _number(impale_effect_more_multiplier, "impale_effect_more_multiplier")
    impale_duration = _number(impale_duration_seconds, "impale_duration_seconds")
    impale_stored_pct = impale_base * (1 + impale_inc / 100) * impale_more
    impale_resist = _number(
        physical_reduction if enemy_impale_physical_reduction_pct is None
        else enemy_impale_physical_reduction_pct,
        "enemy_impale_physical_reduction_pct",
    )
    if impale_resist > 90:
        raise ValueError("敌人穿刺物理伤害减免不能超过 90%")
    if ignore_enemy_impale_physical_reduction:
        impale_resist = 0.0
    reflected_taken_inc = _number(
        enemy_reflected_damage_taken_increased_pct,
        "enemy_reflected_damage_taken_increased_pct",
        minimum=-100.0,
    )
    reflected_taken_more = _number(
        enemy_reflected_damage_taken_more_multiplier,
        "enemy_reflected_damage_taken_more_multiplier",
    )
    regular_cull = _chance(cull_percent, "cull_percent")
    crit_cull = _chance(critical_cull_percent, "critical_cull_percent")
    if regular_cull >= 100 or crit_cull >= 100:
        raise ValueError("终结比例必须小于 100%")

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
    physical_pre_mitigation_average = 0.0
    for kind in DAMAGE_TYPES:
        before = calculate(kind, frozenset()).scale(
            _bounded(
                float(table[kind]["mult"]) * scaled_effect * combat_effect,
                "伤害倍增期望倍率",
            )
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
            pre_noncrit = _roll_average(before, normal_roll_chance, extreme_luck)
            pre_crit = _roll_average(
                before.scale(crit_multi / 100), crit_roll_chance, extreme_luck
            )
            noncrit = _bounded(pre_noncrit * mitigation, "非暴击平均伤害")
            crit = _bounded(
                pre_crit * mitigation,
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
            if kind == "physical":
                physical_pre_mitigation_average = _bounded(
                    pre_noncrit * (1 - crit_chance / 100)
                    + pre_crit * crit_chance / 100,
                    "减伤前物理平均击中",
                )

    crit_effect = 1 - crit_chance / 100 + crit_chance / 100 * crit_multi / 100
    average_hit = _bounded(
        noncrit_average * (1 - crit_chance / 100) + crit_average * crit_chance / 100,
        "平均击中伤害",
    )
    average_use = _bounded(average_hit * hit_chance / 100, "每次使用期望伤害")
    hit_total_dps = _bounded(average_use * rate * dps_mult * quantity, "命中 DPS")
    burst_damage = _bounded(
        average_use + average_use * (burst_hits - 1) * repeat_mult,
        "平均爆发伤害",
    )
    impale_taken = _bounded(
        max(
            0.0,
            1
            + (
                taken_inc
                + taken_inc_type.get("physical", 0.0)
                + reflected_taken_inc
            )
            / 100,
        )
        * taken_more
        * taken_more_type.get("physical", 1.0)
        * reflected_taken_more,
        "穿刺承伤倍率",
    )
    impale_multiplier = (
        impale_stored_pct / 100
        * impale_stack_count
        * effective_impale_chance / 100
        * (1 - impale_resist / 100)
        * impale_taken
    )
    impale_dps = _bounded(
        physical_pre_mitigation_average
        * impale_multiplier
        * hit_chance / 100
        * rate
        * dps_mult
        * quantity
        * (1 if impale_duration > 0 else 0),
        "穿刺 DPS",
    )
    combined_dps = _bounded(hit_total_dps + impale_dps, "命中与穿刺合计 DPS")
    ailment_results = _calculate_damaging_ailments(
        damaging_ailments,
        crit_chance_pct=crit_chance,
        hit_chance_pct=hit_chance,
        action_rate=rate,
        skill_dps_multiplier=dps_mult,
        quantity_multiplier=quantity,
        ruthless_chance_pct=ruthless_chance,
        fist_of_war_effect=fist_effect,
        shared_taken_increased_pct=taken_inc,
        shared_taken_increased_by_type=taken_inc_type,
        shared_taken_more_multiplier=taken_more,
        shared_taken_more_by_type=taken_more_type,
        enemy_resistance=resist,
        enemy_physical_reduction_pct=physical_reduction,
    )
    ailment_dps = _bounded(sum(item.dps for item in ailment_results), "伤害性异常 DPS")
    skill_dot_result = _calculate_skill_dot(
        skill_dot,
        action_rate=rate,
        skill_dps_multiplier=dps_mult,
        quantity_multiplier=quantity,
        shared_taken_increased_pct=taken_inc,
        shared_taken_increased_by_type=taken_inc_type,
        shared_taken_more_multiplier=taken_more,
        shared_taken_more_by_type=taken_more_type,
        enemy_resistance=resist,
        enemy_physical_reduction_pct=physical_reduction,
    )
    skill_dot_dps = skill_dot_result.dps if skill_dot_result is not None else 0.0
    dot_cap = (
        constants.get(constants.DOT_DPS_CAP)
        if ailment_results or skill_dot_result is not None else 0.0
    )
    total_dot_uncapped = _bounded(
        ailment_dps + skill_dot_dps, "技能与异常持续伤害合计"
    )
    total_dot_dps = min(total_dot_uncapped, dot_cap)
    all_damage_dps = _bounded(
        combined_dps + total_dot_dps, "命中、穿刺与持续伤害总 DPS"
    )
    cull_hit_rate = hit_chance / 100 * rate * dps_mult
    effective_crit_cull = crit_cull * (
        1 - (1 - crit_chance / 100) ** cull_hit_rate
    ) if crit_cull > 0 and cull_hit_rate > 0 else 0.0
    effective_cull = max(regular_cull, effective_crit_cull)
    cull_multiplier = 100 / (100 - effective_cull)
    culling_dps = _bounded(combined_dps * (cull_multiplier - 1), "终结等效 DPS")
    effective_dps_with_cull = _bounded(combined_dps * cull_multiplier, "含终结等效 DPS")
    effective_total_dps_with_cull = _bounded(
        all_damage_dps * cull_multiplier, "含持续伤害与终结的总等效 DPS"
    )
    return HitDpsResult(
        conversion=table, by_type=tuple(breakdown), non_crit_hit=total_noncrit,
        non_crit_average=noncrit_average, crit_average=crit_average,
        average_hit=average_hit, average_damage_per_use=average_use, dps=hit_total_dps,
        hit_chance_pct=hit_chance, crit_chance_pct=crit_chance,
        crit_multiplier_pct=crit_multi, crit_effect=crit_effect,
        damage_roll_chance_pct=normal_roll_chance,
        crit_damage_roll_chance_pct=crit_roll_chance, extreme_luck=bool(extreme_luck),
        double_damage_chance_pct=double, triple_damage_chance_pct=triple,
        scaled_damage_effect=scaled_effect,
        ruthless_blow_chance_pct=ruthless_chance, ruthless_blow_effect=ruthless_effect,
        fist_of_war_uptime_pct=fist_uptime * 100, fist_of_war_effect=fist_effect,
        exerted_attack_uptime_pct=exert_uptime, exerted_attack_effect=exert_effect,
        combat_damage_effect=combat_effect,
        raw_action_rate=raw_rate, tick_limited_action_rate=tick_limited_rate,
        action_rate_tick_capped=tick_limited_rate < raw_rate,
        action_repeats=action_repeats, channelled=bool(channelled), action_rate=rate,
        cooldown_seconds=cooldown, effective_cooldown_seconds=effective_cd,
        cooldown_tick_rounded=cooldown_rounded,
        server_tick_seconds=server_tick_seconds, cooldown_uses=cooldown_uses,
        triggered=bool(triggered),
        skill_dps_multiplier=dps_mult, quantity_multiplier=quantity,
        burst_hit_count=burst_hits, repeat_damage_multiplier=repeat_mult,
        average_burst_damage=burst_damage,
        physical_hit_before_mitigation_average=physical_pre_mitigation_average,
        impale_chance_pct=effective_impale_chance, impale_stacks=impale_stack_count,
        impale_stored_damage_pct=impale_stored_pct, impale_multiplier=impale_multiplier,
        impale_dps=impale_dps, combined_dps=combined_dps,
        cull_percent=effective_cull, cull_multiplier=cull_multiplier,
        culling_dps=culling_dps, effective_dps_with_cull=effective_dps_with_cull,
        damaging_ailments=ailment_results, ailment_dps=ailment_dps,
        skill_dot=skill_dot_result, skill_dot_dps=skill_dot_dps,
        total_dot_dps_uncapped=total_dot_uncapped, total_dot_dps=total_dot_dps,
        total_dot_dps_cap=dot_cap, total_dot_dps_capped=total_dot_uncapped > dot_cap,
        total_dps=all_damage_dps,
        effective_total_dps_with_cull=effective_total_dps_with_cull,
    )
