"""回复：能量护盾回充、生命/魔力/护盾偷取、魔力再生。

## 这三件事共用一个坑：**速率**与**总量**不是一回事

- 护盾回充是**每秒 33% 最大护盾**（约 3 秒回满），但**要先等 2 秒**没受伤害才开始。
  「多久回满」= 延迟 + 池 / 每秒回充量，两段都要说，只说一段就答错了。
- 偷取有**两层上限**：每秒最多回复池的 20%（生命/魔力）或 10%（护盾），
  而**单次偷取实例**最多回复池的 10%。前者限速、后者限量，混成一个数就是错的。
- 魔力再生的基数写成「每分钟百分比」（105%/分钟），换成每秒是 1.75%。
  拿 105 当每秒用会差 60 倍。

## 一个重复键，以及为什么它是安全的

`Modules/Data.lua` 里 `EnergyShieldRechargeBase` **写了两次**：先是
`characterConstants["energy_shield_recharge_rate_per_minute_%"] / 60 / 100`（表达式），
紧接着一行字面量 `0.33`。Lua 后者覆盖前者，我们的解析器同样取后者。

两者相差 1% 以内（2000 / 60 / 100 = 0.3333…），所以取字面量不改变结论——
但这件事**要被证明而不是被相信**：`tests/unit/test_recovery.py` 拿库里那条
characterConstants 反算来核对。引用锚点也指向**后一行**（第 249 行），
指向前一行等于让读者去看一行没有被采用的代码。

## 不做的事

- 不算「击中回复」「药剂回复」「再生率」这些独立机制（各有自己的 mod 与上限）。
- 不算 Mind over Matter（伤害分流到魔力）——那是受击链上的池保护，属另一块。
- 不读 build：池大小、偷取百分比、增益百分比一律由调用方逐字给出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from calc import constants
from calc.derive import Step

#: PoB 原文位置。引用必须带行号锚点。
DEFENCE_FILE = "src/Modules/CalcDefence.lua"
OFFENCE_FILE = "src/Modules/CalcOffence.lua"
SETUP_FILE = "src/Modules/CalcSetup.lua"
RECHARGE_LINES = (1320, 1350)
LEECH_RATE_LINES = (3896, 3925)
LEECH_DURATION_LINES = (3547, 3553)
LEECH_CAP_LINES = (46, 55)
MANA_REGEN_LINE = 497

PoolName = Literal["生命", "魔力", "能量护盾"]

#: 池 → (速率上限常量, 单次实例上限常量)。三种池的值不同，所以是三套键。
_LEECH_KEYS: dict[str, tuple[str, str]] = {
    "生命": (constants.MAX_LIFE_LEECH_RATE, constants.MAX_LIFE_LEECH_INSTANCE),
    "魔力": (constants.MAX_MANA_LEECH_RATE, constants.MAX_MANA_LEECH_INSTANCE),
    "能量护盾": (constants.MAX_ES_LEECH_RATE, constants.MAX_ES_LEECH_INSTANCE),
}


class UnknownPool(ValueError):
    """池名不认识。不猜——三种池的上限不同，猜错就是错数字。"""


def _resolve_pool(pool: str) -> str:
    text = (pool or "").strip()
    aliases = {
        "生命": "生命", "血": "生命", "life": "生命",
        "魔力": "魔力", "蓝": "魔力", "mana": "魔力",
        "能量护盾": "能量护盾", "护盾": "能量护盾", "es": "能量护盾",
        "energy shield": "能量护盾",
    }
    for alias, name in aliases.items():
        if text.lower() == alias or alias in text.lower():
            return name
    raise UnknownPool(f"认不出回复的池 {pool!r}；只支持生命 / 魔力 / 能量护盾")


@dataclass(frozen=True)
class RechargeResult:
    """能量护盾回充。`per_second` 是 PoB 的 `EnergyShieldRecharge`（已取整）。"""

    energy_shield: float
    base_pct: float
    per_second: int
    delay_seconds: float
    #: 从开始回充到回满要多久（不含延迟）
    seconds_to_full: float
    #: 受伤害后到回满的总时长（含延迟）
    total_seconds: float
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class LeechResult:
    """一次偷取实例的量、速率与两层上限。"""

    pool_name: str
    pool: float
    #: 这一击偷到的量（伤害 × 偷取%），未夹单次上限
    amount: float
    #: 单次实例上限（池的百分比）
    instance_cap_pct: float
    instance_cap: float
    #: 实际生效的单次量
    effective_amount: float
    #: 一次实例每秒回复多少（池 × 2% × 加成）
    instance_rate: float
    #: 这次实例持续多久
    duration: float
    #: 速率上限（每秒最多回复池的百分之几）
    rate_cap_pct: float
    rate_cap: float
    #: 同时叠了几个实例时的总速率（已夹上限）
    total_rate: float
    instances: int
    steps: tuple[Step, ...]

    @property
    def rate_capped(self) -> bool:
        return self.instances * self.instance_rate > self.rate_cap

    @property
    def amount_capped(self) -> bool:
        return self.amount > self.instance_cap


@dataclass(frozen=True)
class ManaRegenResult:
    mana: float
    base_pct_per_minute: float
    per_second: float
    steps: tuple[Step, ...]


def energy_shield_recharge(
    energy_shield: float,
    *,
    increased_recharge_pct: float = 0.0,
    more_recharge_mult: float = 1.0,
    recovery_rate_pct: float = 0.0,
    faster_start_pct: float = 0.0,
) -> RechargeResult:
    """护盾回充速率与「多久回满」。

    照 `CalcDefence.lua:1325-1349`：
    ``回充 = 最大护盾 × 0.33 ×(1 + 增加/100)× 更多``，再乘「回复速率」修正后取整。
    延迟是 `pobMisc.EnergyShieldRechargeDelay`（2 秒），
    「更快开始回充」按 ``延迟 /(1 + 加快/100)`` 缩短。
    """
    base = constants.get(constants.ES_RECHARGE_BASE)
    delay_base = constants.get(constants.ES_RECHARGE_DELAY)

    inc = 1 + increased_recharge_pct / 100
    recovery_mod = 1 + recovery_rate_pct / 100
    raw = energy_shield * base * inc * more_recharge_mult
    per_second = int(round(raw * recovery_mod))
    delay = delay_base / (1 + faster_start_pct / 100)
    to_full = energy_shield / per_second if per_second > 0 else float("inf")

    steps = [
        Step(
            "每秒回充（基础）",
            f"{energy_shield:g} × {base:g}（每秒 {base * 100:g}% 最大护盾）",
            round(energy_shield * base, 2),
        )
    ]
    if increased_recharge_pct:
        steps.append(Step("增加的回充速度", f"× (1 + {increased_recharge_pct:g} / 100)", inc))
    if more_recharge_mult != 1.0:
        steps.append(Step("更多回充速度", f"× {more_recharge_mult:g}", more_recharge_mult))
    if recovery_rate_pct:
        steps.append(Step("护盾回复速率修正", f"× {recovery_mod:g}", recovery_mod))
    steps.append(Step("每秒回充", f"取整({raw * recovery_mod:.2f})", per_second))
    steps.append(
        Step(
            "开始回充的延迟",
            f"{delay_base:g} 秒"
            + (f" ÷ (1 + {faster_start_pct:g} / 100)" if faster_start_pct else ""),
            round(delay, 2),
        )
    )
    steps.append(
        Step("回满耗时（不含延迟）", f"{energy_shield:g} / {per_second}", round(to_full, 2))
    )
    steps.append(Step("受伤后回满共需", f"{delay:g} + {to_full:.2f}", round(delay + to_full, 2)))
    return RechargeResult(
        energy_shield=float(energy_shield),
        base_pct=base * 100,
        per_second=per_second,
        delay_seconds=delay,
        seconds_to_full=to_full,
        total_seconds=delay + to_full,
        steps=tuple(steps),
    )


def leech(
    pool: float,
    *,
    pool_name: str = "生命",
    leech_pct: float = 0.0,
    hit_damage: float = 0.0,
    increased_leech_rate_pct: float = 0.0,
    instances: int = 1,
) -> LeechResult:
    """一次偷取实例的量、速率、持续时间与两层上限。

    照 `CalcOffence.lua:3898`（每实例速率 = 池 × `LeechRateBase` × 速率加成）、
    `3547-3552`（持续时间 = 量 / 池 / `LeechRateBase`）与
    `3921`（总速率夹在速率上限内），上限由 `CalcSetup.lua:46-54` 从常量表播种。
    """
    name = _resolve_pool(pool_name)
    rate_key, instance_key = _LEECH_KEYS[name]
    base = constants.get(constants.LEECH_RATE_BASE)
    rate_cap_pct = constants.get(rate_key) / 60          # 每分钟 → 每秒
    instance_cap_pct = constants.get(instance_key)

    amount = hit_damage * leech_pct / 100
    instance_cap = pool * instance_cap_pct / 100
    effective_amount = min(amount, instance_cap) if amount else 0.0
    rate_mod = 1 + increased_leech_rate_pct / 100
    instance_rate = pool * base * rate_mod
    rate_cap = pool * rate_cap_pct / 100
    total_rate = min(instances * instance_rate, rate_cap)
    duration = (
        effective_amount / pool / base if pool > 0 and effective_amount else 0.0
    )

    steps: list[Step] = []
    if hit_damage and leech_pct:
        steps.append(
            Step(
                "这一击偷到的量",
                f"{hit_damage:g} × {leech_pct:g}% ",
                round(amount, 2),
            )
        )
        steps.append(
            Step(
                f"单次实例上限（{name}的 {instance_cap_pct:g}%）",
                f"{pool:g} × {instance_cap_pct:g}%",
                round(instance_cap, 2),
            )
        )
        steps.append(
            Step("实际单次回复量", f"min({amount:.2f}, {instance_cap:.2f})",
                 round(effective_amount, 2))
        )
        steps.append(
            Step(
                "这次实例持续",
                f"{effective_amount:.2f} / ({pool:g} × {base:g})",
                round(duration, 2),
            )
        )
    steps.append(
        Step(
            "一个实例每秒回复",
            f"{pool:g} × {base:g}（每秒 {base * 100:g}% 池）"
            + (f" × {rate_mod:g}" if increased_leech_rate_pct else ""),
            round(instance_rate, 2),
        )
    )
    if instances != 1:
        steps.append(
            Step(f"{instances} 个实例同时生效", f"{instances} × {instance_rate:.2f}",
                 round(instances * instance_rate, 2))
        )
    steps.append(
        Step(
            f"速率上限（每秒 {rate_cap_pct:g}% {name}）",
            f"{pool:g} × {rate_cap_pct:g}%",
            round(rate_cap, 2),
        )
    )
    steps.append(
        Step("实际每秒回复", f"min({instances * instance_rate:.2f}, {rate_cap:.2f})",
             round(total_rate, 2))
    )
    return LeechResult(
        pool_name=name,
        pool=float(pool),
        amount=amount,
        instance_cap_pct=instance_cap_pct,
        instance_cap=instance_cap,
        effective_amount=effective_amount,
        instance_rate=instance_rate,
        duration=duration,
        rate_cap_pct=rate_cap_pct,
        rate_cap=rate_cap,
        total_rate=total_rate,
        instances=int(instances),
        steps=tuple(steps),
    )


def mana_regen(
    mana: float, *, increased_regen_pct: float = 0.0, flat_regen: float = 0.0
) -> ManaRegenResult:
    """魔力再生。基数是**每分钟百分比**（105%/分钟 = 每秒 1.75%）。

    照 `CalcSetup.lua:497`：``ManaRegen BASE = ManaRegenBase × 魔力``，
    其中 ``ManaRegenBase = characterConstants["mana_regeneration_rate_per_minute_%"] / 60 / 100``。
    那一项在 `Modules/Data.lua` 里是**表达式**，所以不进白名单——直接用被引用的常量。
    """
    per_minute = constants.get(constants.MANA_REGEN_RATE_PER_MINUTE)
    per_second_base = per_minute / 60 / 100
    inc = 1 + increased_regen_pct / 100
    per_second = (mana * per_second_base + flat_regen) * inc

    steps = [
        Step(
            "基础每秒再生",
            f"{mana:g} × {per_minute:g}% / 60 = 每秒 {per_second_base * 100:.2f}% 魔力",
            round(mana * per_second_base, 2),
        )
    ]
    if flat_regen:
        steps.append(Step("定值再生", f"+ {flat_regen:g}/秒", flat_regen))
    if increased_regen_pct:
        steps.append(Step("增加的魔力再生", f"× (1 + {increased_regen_pct:g} / 100)", inc))
    steps.append(Step("每秒再生", f"{per_second:.2f}", round(per_second, 2)))
    return ManaRegenResult(
        mana=float(mana),
        base_pct_per_minute=per_minute,
        per_second=per_second,
        steps=tuple(steps),
    )
