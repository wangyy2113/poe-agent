"""Harvest 品质附魔的保守状态转移。

覆盖 ``harvest_craft`` 中 8 条护甲与 6 条武器品质附魔。只按 POEDB 原文替换
品质的默认收益；原文没有给出的取整、叠加、移除旧附魔与特殊状态规则保持未知。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from craft.harvest_operations import HarvestCheck, HarvestCost

HarvestEnchantVerdict = Literal["compatible", "incompatible", "unknown"]


@dataclass(frozen=True)
class HarvestEnchantSpec:
    key: str
    effect_text: str
    item_kind: str
    stat_label: str
    quality_step: int
    value_per_step: float
    value_unit: str
    replaced_quality_effect: str


_ARMOUR_EFFECTS = (
    ("cold_resistance", "冰霜抗性", "增加1%冰霜抗性。", "%"),
    ("fire_resistance", "火焰抗性", "增加1%火焰抗性。", "%"),
    ("lightning_resistance", "闪电抗性", "增加1%闪电抗性。", "%"),
    ("strength", "力量", "增加1点力量。", "点"),
    ("dexterity", "敏捷", "增加1点敏捷。", "点"),
    ("intelligence", "智慧", "增加1点智慧。", "点"),
    ("maximum_life", "生命", "最多增加1点生命。", "点"),
    ("maximum_mana", "魔力", "最多增加1点魔力。", "点"),
)

ENCHANT_SPECS: tuple[HarvestEnchantSpec, ...] = (
    *(HarvestEnchantSpec(
        key=f"body_armour_{key}",
        effect_text=f"附魔一件护甲。品质不会增加其防御，品质每增加2%，{phrase}",
        item_kind="body_armour",
        stat_label=label,
        quality_step=2,
        value_per_step=1,
        value_unit=unit,
        replaced_quality_effect="防御",
    ) for key, label, phrase, unit in _ARMOUR_EFFECTS),
    HarvestEnchantSpec(
        "weapon_elemental_damage",
        "附魔一件武器。品质不会增加其物理伤害，品质每增加2%，元素伤害增加1%",
        "weapon", "元素伤害", 2, 1, "%", "物理伤害",
    ),
    HarvestEnchantSpec(
        "weapon_accuracy",
        "附魔一件武器。品质不会增加其物理伤害，品质每增加2%，精度增加1%",
        "weapon", "精度", 2, 1, "%", "物理伤害",
    ),
    HarvestEnchantSpec(
        "weapon_area_of_effect",
        "附魔一件武器。品质不会增加其物理伤害，品质每增加4%，效果区域增加1%",
        "weapon", "效果区域", 4, 1, "%", "物理伤害",
    ),
    HarvestEnchantSpec(
        "weapon_critical_strike_chance",
        "附魔一件武器。品质不会增加其物理伤害，品质每增加4%，暴击率增加1%",
        "weapon", "暴击率", 4, 1, "%", "物理伤害",
    ),
    HarvestEnchantSpec(
        "weapon_attack_speed",
        "附魔一件武器。品质不会增加其物理伤害，品质每增加8%，攻击速度增加1%",
        "weapon", "攻击速度", 8, 1, "%", "物理伤害",
    ),
    HarvestEnchantSpec(
        "melee_weapon_range",
        "附魔一件近战武器。品质不会增加其物理伤害，品质每增加10%，武器范围增加 0.1 米",
        "melee_weapon", "武器范围", 10, 0.1, "米", "物理伤害",
    ),
)
SUPPORTED_ENCHANT_EFFECTS = frozenset(spec.effect_text for spec in ENCHANT_SPECS)
_BY_EFFECT = {spec.effect_text: spec for spec in ENCHANT_SPECS}


@dataclass(frozen=True)
class HarvestEnchantItemState:
    item_kind: str | None = None
    quality: int | None = None
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False
    incomplete_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HarvestEnchantResult:
    verdict: HarvestEnchantVerdict
    craft_id: str
    effect_text: str
    enchant_key: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    costs: list[HarvestCost]
    before: HarvestEnchantItemState
    after: dict[str, Any]
    checks: list[HarvestCheck] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    boundary: str = (
        "compatible 只表示已提供状态通过 POEDB 效果原文明示的物品类别与品质条件；"
        "原始费用不是市场成本，不补取整、叠加、替换旧附魔、步骤或路线"
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "craft_id": self.craft_id,
            "effect_text": self.effect_text,
            "enchant_key": self.enchant_key,
            "source": self.source,
            "source_priority": self.source_priority,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "costs": [cost.as_dict() for cost in self.costs],
            "before": self.before.as_dict(),
            "after": self.after,
            "checks": [check.as_dict() for check in self.checks],
            "unknown_fields": self.unknown_fields,
            "boundary": self.boundary,
        }


def _selector_effect(selector: str) -> str:
    value = (selector or "").strip()
    if value in SUPPORTED_ENCHANT_EFFECTS:
        return value
    compact = value.replace(" ", "").replace("品质附魔", "").replace("附魔", "")
    for spec in ENCHANT_SPECS:
        prefix = "护甲" if spec.item_kind == "body_armour" else (
            "近战武器" if spec.item_kind == "melee_weapon" else "武器"
        )
        aliases = {prefix + spec.stat_label, prefix + "品质" + spec.stat_label}
        if spec.key == "melee_weapon_range":
            aliases.update({"近战武器范围", "近战武器品质范围"})
        if compact in aliases:
            return spec.effect_text
    return value


def _resolve(conn: sqlite3.Connection, selector: str) -> tuple[sqlite3.Row, HarvestEnchantSpec] | None:
    effect = _selector_effect(selector)
    spec = _BY_EFFECT.get(effect)
    if spec is None:
        return None
    rows = conn.execute(
        "SELECT * FROM harvest_craft WHERE effect_text=? ORDER BY craft_id", (effect,)
    ).fetchall()
    return (rows[0], spec) if len(rows) == 1 else None


def _costs(conn: sqlite3.Connection, craft_id: str) -> list[HarvestCost]:
    rows = conn.execute(
        "SELECT amount,currency_name_zh,currency_metadata_id,currency_url "
        "FROM harvest_craft_cost WHERE craft_id=? ORDER BY position",
        (craft_id,),
    ).fetchall()
    return [HarvestCost(
        amount=int(row["amount"]),
        currency_name_zh=str(row["currency_name_zh"]),
        currency_metadata_id=str(row["currency_metadata_id"]),
        currency_url=str(row["currency_url"]),
    ) for row in rows]


def _kind_check(spec: HarvestEnchantSpec, state: HarvestEnchantItemState, url: str) -> HarvestCheck:
    kind = state.item_kind
    expected = spec.item_kind
    labels = {"body_armour": "护甲", "weapon": "武器", "melee_weapon": "近战武器"}
    if kind is None:
        return HarvestCheck("item_kind", "unknown", f"需要确认物品是否为{labels[expected]}", url)
    compatible = kind == expected or (expected == "weapon" and kind == "melee_weapon")
    if not compatible:
        actual = labels.get(kind, "其它物品")
        return HarvestCheck(
            "item_kind", "fail", f"当前是{actual}，但效果原文只描述{labels[expected]}", url,
        )
    return HarvestCheck("item_kind", "pass", f"当前物品类别符合{labels[expected]}要求", url)


def evaluate_harvest_enchant(
    conn: sqlite3.Connection,
    selector: str,
    state: HarvestEnchantItemState,
) -> HarvestEnchantResult | None:
    resolved = _resolve(conn, selector)
    if resolved is None:
        return None
    row, spec = resolved
    url = str(row["url"])
    checks = [
        HarvestCheck("source_contract", "pass", "POEDB 效果原文与已审计品质附魔逐字一致", url),
        _kind_check(spec, state, url),
    ]
    unknown_fields = ["与已有附魔、影响力或其它品质效果的叠加/替换规则"]
    derived_value: float | None = None
    if state.quality is None:
        checks.append(HarvestCheck(
            "quality", "unknown", "需要提供当前品质才能按原文计算附魔收益", url,
        ))
    elif state.quality < 0:
        checks.append(HarvestCheck("quality", "fail", "品质不能为负数", url))
    elif state.quality % spec.quality_step:
        checks.append(HarvestCheck(
            "quality", "unknown",
            f"当前品质 {state.quality}% 不是 {spec.quality_step}% 的整数倍；来源未说明余数如何取整",
            url,
        ))
        unknown_fields.append("品质不能整除步长时的取整方式与最终收益")
    else:
        derived_value = round(
            state.quality // spec.quality_step * spec.value_per_step, 10
        )
        checks.append(HarvestCheck(
            "quality", "pass",
            f"当前品质 {state.quality}% 可按每 {spec.quality_step}% 的原文比例精确换算",
            url,
        ))

    special = []
    if state.corrupted:
        special.append("已腐化")
    if state.mirrored:
        special.append("已镜像")
    if state.fractured:
        special.append("含裂隙词缀")
    if special:
        checks.append(HarvestCheck(
            "special_state", "unknown", "、".join(special) + "的 Harvest 规则未纳入本状态机",
        ))
        unknown_fields.append("特殊状态下的完整可执行性")
    if state.incomplete_reason:
        checks.append(HarvestCheck("state_completeness", "unknown", state.incomplete_reason))

    costs = _costs(conn, str(row["craft_id"]))
    if not costs:
        checks.append(HarvestCheck("cost_contract", "unknown", "该目录项缺少原始费用子项"))
    else:
        checks.append(HarvestCheck(
            "cost_contract", "pass",
            "原始费用：" + " + ".join(f"{cost.amount}x {cost.currency_name_zh}" for cost in costs),
            url,
        ))

    if any(check.status == "fail" for check in checks):
        verdict: HarvestEnchantVerdict = "incompatible"
    elif any(check.status == "unknown" for check in checks):
        verdict = "unknown"
    else:
        verdict = "compatible"

    after = {
        "quality": state.quality,
        "default_quality_effect": "disabled",
        "replaced_quality_effect": spec.replaced_quality_effect,
        "granted_stat": spec.stat_label,
        "quality_step_percent": spec.quality_step,
        "value_per_step": spec.value_per_step,
        "value_unit": spec.value_unit,
        "derived_value": derived_value,
    }
    return HarvestEnchantResult(
        verdict=verdict,
        craft_id=str(row["craft_id"]),
        effect_text=spec.effect_text,
        enchant_key=spec.key,
        source=str(row["source"]),
        source_priority=int(row["source_priority"]),
        source_url=url,
        fetched_at=str(row["fetched_at"]),
        costs=costs,
        before=state,
        after=after,
        checks=checks,
        unknown_fields=list(dict.fromkeys(unknown_fields)),
    )
