"""Harvest 定向词缀操作的保守状态转移。

只支持 ``harvest_craft`` 中可逐字分类的 12 条元素伤害/抗性转换与 11 条
新增指定标签并移除随机词缀。结果只表达来源原文能冻结的条件变化，不补词缀池、
具体位阶、数值、概率或完整游戏可执行性。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from craft.harvest_operations import HarvestCheck, HarvestCost

HarvestModifierVerdict = Literal["compatible", "incompatible", "unknown"]

ELEMENTS = ("冰霜", "火焰", "闪电")
ELEMENT_KINDS = ("伤害", "抗性")
REMOVE_ADD_TAGS = (
    "冰霜", "攻击", "施法", "暴击", "混沌", "火焰",
    "物理", "生命", "速度", "闪电", "防御",
)


def _conversion_effect(source: str, target: str, kind: str) -> str:
    return f"将一个提供{source}{kind}的词缀变为近似位阶，但提供{target}{kind}的词缀"


CONVERSION_EFFECTS = {
    (source, target, kind): _conversion_effect(source, target, kind)
    for kind in ELEMENT_KINDS
    for source in ELEMENTS
    for target in ELEMENTS
    if source != target
}
REMOVE_ADD_EFFECTS = {
    tag: f"添加一个新的{tag}词缀，并从不受影响的物品中去掉另一个随机词缀"
    for tag in REMOVE_ADD_TAGS
}
SUPPORTED_MODIFIER_EFFECTS = frozenset({
    *CONVERSION_EFFECTS.values(),
    *REMOVE_ADD_EFFECTS.values(),
})


@dataclass(frozen=True)
class HarvestModifierItemState:
    influenced: bool | None = None
    source_modifier_present: bool | None = None
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False
    incomplete_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HarvestModifierResult:
    verdict: HarvestModifierVerdict
    craft_id: str
    effect_text: str
    mode: str
    source_modifier: str
    target_modifier: str
    guaranteed_tag: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    costs: list[HarvestCost]
    before: HarvestModifierItemState
    after: dict[str, Any]
    checks: list[HarvestCheck] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    boundary: str = (
        "compatible 只表示已提供状态通过 POEDB 效果原文明示的条件；原始费用不是市场成本，"
        "也不提供词缀池、具体位阶或数值、概率、步骤与路线"
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "craft_id": self.craft_id,
            "effect_text": self.effect_text,
            "mode": self.mode,
            "source_modifier": self.source_modifier,
            "target_modifier": self.target_modifier,
            "guaranteed_tag": self.guaranteed_tag,
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


def _classify(effect: str) -> tuple[str, str, str, str] | None:
    for (source, target, kind), expected in CONVERSION_EFFECTS.items():
        if effect == expected:
            return "convert_elemental_modifier", source + kind, target + kind, ""
    for tag, expected in REMOVE_ADD_EFFECTS.items():
        if effect == expected:
            return "add_tag_remove_random", "", "", tag
    return None


def _selector_effect(selector: str) -> str:
    value = (selector or "").strip()
    if value in SUPPORTED_MODIFIER_EFFECTS:
        return value
    compact = value.replace(" ", "").replace("→", "转").replace("->", "转")
    for (source, target, kind), effect in CONVERSION_EFFECTS.items():
        aliases = {
            f"{source}{kind}转{target}{kind}",
            f"{source}{kind}转{target}",
            f"{source}{kind}变为{target}{kind}",
        }
        if compact in aliases:
            return effect
    for tag, effect in REMOVE_ADD_EFFECTS.items():
        if compact in {
            f"新增{tag}移除随机词缀",
            f"添加{tag}移除随机词缀",
            f"新增{tag}并移除随机词缀",
            f"添加{tag}并移除随机词缀",
        }:
            return effect
    return value


def _resolve(conn: sqlite3.Connection, selector: str) -> sqlite3.Row | None:
    effect = _selector_effect(selector)
    rows = conn.execute(
        "SELECT * FROM harvest_craft WHERE effect_text=? ORDER BY craft_id", (effect,)
    ).fetchall()
    supported = [row for row in rows if _classify(str(row["effect_text"])) is not None]
    return supported[0] if len(supported) == 1 else None


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


def evaluate_harvest_modifier_operation(
    conn: sqlite3.Connection,
    selector: str,
    state: HarvestModifierItemState,
) -> HarvestModifierResult | None:
    row = _resolve(conn, selector)
    if row is None:
        return None
    effect = str(row["effect_text"])
    classified = _classify(effect)
    if classified is None:
        return None
    mode, source_modifier, target_modifier, guaranteed_tag = classified
    url = str(row["url"])
    checks = [
        HarvestCheck("source_contract", "pass", "POEDB 效果原文与已审计操作语义逐字一致", url),
    ]
    unknown_fields: list[str] = []

    if mode == "convert_elemental_modifier":
        if state.source_modifier_present is None:
            checks.append(HarvestCheck(
                "source_modifier", "unknown",
                f"需要确认物品是否有一个提供{source_modifier}的词缀", url,
            ))
        elif state.source_modifier_present:
            checks.append(HarvestCheck(
                "source_modifier", "pass", f"已确认物品有{source_modifier}词缀", url,
            ))
        else:
            checks.append(HarvestCheck(
                "source_modifier", "fail", f"物品没有{source_modifier}词缀，缺少原文要求的转换对象", url,
            ))
        after = {
            "removed_modifier": source_modifier,
            "added_modifier": target_modifier,
            "tier_relation": "approximately_same_tier",
            "modifier_count_change": "one_replaced_by_one",
            "influence_state": "not_specified_by_source",
        }
        unknown_fields.extend((
            "近似位阶的精确映射规则与最终位阶",
            "转换后词缀的具体数值",
        ))
    else:
        if state.influenced is None:
            checks.append(HarvestCheck(
                "influence", "unknown", "需要确认物品是否不受影响", url,
            ))
        elif state.influenced:
            checks.append(HarvestCheck(
                "influence", "fail", "当前物品受影响，但效果原文要求不受影响的物品", url,
            ))
        else:
            checks.append(HarvestCheck(
                "influence", "pass", "当前物品不受影响，与效果原文一致", url,
            ))
        after = {
            "removed_modifier": "one_random_other_modifier",
            "added_modifier": f"one_{guaranteed_tag}_tag_modifier",
            "tier_relation": "not_specified_by_source",
            "modifier_count_change": "one_added_one_removed",
            "influence_state": "uninfluenced_input_required",
        }
        unknown_fields.extend((
            f"新增{guaranteed_tag}标签对应的具体词缀、位阶与数值",
            "被随机移除的具体词缀",
            "当前物品类别的目标词缀池与完整可执行性",
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
        unknown_fields.append("特殊状态下的完整可执行性与保留/移除语义")
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
        verdict: HarvestModifierVerdict = "incompatible"
    elif any(check.status == "unknown" for check in checks):
        verdict = "unknown"
    else:
        verdict = "compatible"

    return HarvestModifierResult(
        verdict=verdict,
        craft_id=str(row["craft_id"]),
        effect_text=effect,
        mode=mode,
        source_modifier=source_modifier,
        target_modifier=target_modifier,
        guaranteed_tag=guaranteed_tag,
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
