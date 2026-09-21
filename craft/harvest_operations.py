"""Harvest 装备重铸操作的保守状态转移。

只支持 ``harvest_craft`` 中 20 条可逐字分类的装备重铸原文，并携带其
``harvest_craft_cost`` 原始费用。元素词缀转换与新增/移除由相邻领域模块处理；
材料转换、牺牲与附魔继续停在目录层，不会被相似关键词误归为重铸语义。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from craft.capacity import normalize_rarity

HarvestVerdict = Literal["compatible", "incompatible", "unknown"]
CheckStatus = Literal["pass", "fail", "unknown"]

GUARANTEED_TAGS = (
    "元素", "冰霜", "召唤生物", "属性", "掉落", "攻击", "施法", "暴击",
    "混沌", "火焰", "物理", "生命", "速度", "闪电", "防御", "魔力",
)
_DROP_EFFECT = "重铸一件带随机词缀的稀有物品，其中包括一个掉落属性"
_GUARANTEED_EFFECTS = {
    tag: (
        _DROP_EFFECT if tag == "掉落"
        else f"重铸一件带随机词缀的稀有物品，其中包括一个{tag}词缀"
    )
    for tag in GUARANTEED_TAGS
}
INFLUENCE_EFFECT = "重铸一件带随机词缀的受影响的稀有物品，其中包括一个影响词缀"
SAME_MORE_EFFECT = "重铸一件稀有物品，大概率会得到相同词缀类型"
SAME_LESS_EFFECT = "重铸一件稀有物品，大概率不会得到相同词缀类型"
RANDOMIZE_INFLUENCE_EFFECT = (
    "随机化受影响的普通物品、魔法物品或稀有物品的影响类型，并使用新的随机词缀重铸物品"
)
SUPPORTED_EFFECTS = frozenset({
    *_GUARANTEED_EFFECTS.values(),
    INFLUENCE_EFFECT,
    SAME_MORE_EFFECT,
    SAME_LESS_EFFECT,
    RANDOMIZE_INFLUENCE_EFFECT,
})


@dataclass(frozen=True)
class HarvestItemState:
    rarity: str | None = None
    influenced: bool | None = None
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False
    incomplete_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HarvestCheck:
    key: str
    status: CheckStatus
    summary: str
    source_url: str = ""

    def as_dict(self) -> dict[str, str]:
        out = {"key": self.key, "status": self.status, "summary": self.summary}
        if self.source_url:
            out["source_url"] = self.source_url
        return out


@dataclass(frozen=True)
class HarvestCost:
    amount: int
    currency_name_zh: str
    currency_metadata_id: str
    currency_url: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HarvestOperationResult:
    verdict: HarvestVerdict
    craft_id: str
    effect_text: str
    mode: str
    guaranteed_tag: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    costs: list[HarvestCost]
    before: HarvestItemState
    after: dict[str, Any]
    checks: list[HarvestCheck] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    boundary: str = (
        "compatible 只表示已提供状态通过 Harvest 效果原文中的稀有度与影响状态前提；"
        "原始费用不是市场成本，也不提供具体结果词缀、数值、概率、步骤或路线"
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "craft_id": self.craft_id,
            "effect_text": self.effect_text,
            "mode": self.mode,
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


def _classify(effect: str) -> tuple[str, str] | None:
    for tag, expected in _GUARANTEED_EFFECTS.items():
        if effect == expected:
            return "reforge_guaranteed_tag", tag
    if effect == INFLUENCE_EFFECT:
        return "reforge_guaranteed_influence", "影响"
    if effect == SAME_MORE_EFFECT:
        return "reforge_same_types_more_likely", ""
    if effect == SAME_LESS_EFFECT:
        return "reforge_same_types_less_likely", ""
    if effect == RANDOMIZE_INFLUENCE_EFFECT:
        return "randomize_influence_and_reforge", ""
    return None


def _selector_effect(selector: str) -> str:
    value = (selector or "").strip()
    if value in SUPPORTED_EFFECTS:
        return value
    if value in _GUARANTEED_EFFECTS:
        return _GUARANTEED_EFFECTS[value]
    aliases = {
        "影响": INFLUENCE_EFFECT,
        "影响词缀": INFLUENCE_EFFECT,
        "大概率相同": SAME_MORE_EFFECT,
        "相同词缀类型": SAME_MORE_EFFECT,
        "大概率不同": SAME_LESS_EFFECT,
        "不同词缀类型": SAME_LESS_EFFECT,
        "随机影响类型": RANDOMIZE_INFLUENCE_EFFECT,
        "影响类型随机化": RANDOMIZE_INFLUENCE_EFFECT,
    }
    return aliases.get(value, value)


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


def _rarity_check(mode: str, state: HarvestItemState, url: str) -> HarvestCheck:
    normalized = normalize_rarity(state.rarity)
    allowed = (
        ("普通", "魔法", "稀有")
        if mode == "randomize_influence_and_reforge" else ("稀有",)
    )
    if normalized is None:
        return HarvestCheck(
            "rarity", "unknown",
            "需要给出物品稀有度；效果原文要求" + "、".join(allowed) + "物品",
            url,
        )
    if normalized not in allowed:
        return HarvestCheck(
            "rarity", "fail",
            f"当前是{normalized}物品，但效果原文只描述{'、'.join(allowed)}物品",
            url,
        )
    return HarvestCheck("rarity", "pass", f"当前稀有度为{normalized}，与效果原文一致", url)


def evaluate_harvest_reforge(
    conn: sqlite3.Connection,
    selector: str,
    state: HarvestItemState,
) -> HarvestOperationResult | None:
    row = _resolve(conn, selector)
    if row is None:
        return None
    effect = str(row["effect_text"])
    classified = _classify(effect)
    if classified is None:
        return None
    mode, guaranteed_tag = classified
    url = str(row["url"])
    checks = [
        HarvestCheck("source_contract", "pass", "POEDB 效果原文与已审计重铸语义逐字一致", url),
        _rarity_check(mode, state, url),
    ]
    unknown_fields: list[str] = []

    if mode in {"reforge_guaranteed_influence", "randomize_influence_and_reforge"}:
        if state.influenced is None:
            checks.append(HarvestCheck(
                "influence", "unknown", "效果原文要求受影响物品；需要提供物品是否有影响力", url
            ))
        elif not state.influenced:
            checks.append(HarvestCheck(
                "influence", "fail", "当前物品无影响力，但效果原文要求受影响物品", url
            ))
        else:
            checks.append(HarvestCheck("influence", "pass", "当前物品有影响力，与效果原文一致", url))

    special = []
    if state.corrupted:
        special.append("已腐化")
    if state.mirrored:
        special.append("已镜像")
    if state.fractured:
        special.append("含裂隙词缀")
    if special:
        checks.append(HarvestCheck(
            "special_state", "unknown", "、".join(special) + "的 Harvest 规则未纳入本 v1 状态机"
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
            "原始费用：" + " + ".join(f"{c.amount}x {c.currency_name_zh}" for c in costs),
            url,
        ))

    if any(check.status == "fail" for check in checks):
        verdict: HarvestVerdict = "incompatible"
    elif any(check.status == "unknown" for check in checks):
        verdict = "unknown"
    else:
        verdict = "compatible"

    normalized = normalize_rarity(state.rarity)
    selection_bias = ""
    if mode == "reforge_same_types_more_likely":
        selection_bias = "same_modifier_types_more_likely"
        unknown_fields.append("“大概率”的具体概率与最终保留/生成哪些词缀类型")
    elif mode == "reforge_same_types_less_likely":
        selection_bias = "same_modifier_types_less_likely"
        unknown_fields.append("“大概率不会”的具体概率与最终生成哪些词缀类型")

    after = {
        "rarity": normalized,
        "prefix_count": None,
        "suffix_count": None,
        "modifier_identities": "all_random_properties_rerolled",
        "guaranteed_tag": guaranteed_tag or None,
        "selection_bias": selection_bias or None,
        "influence_state": (
            "randomized" if mode == "randomize_influence_and_reforge"
            else "not_specified_by_source"
        ),
    }
    unknown_fields.extend((
        "重铸后的前后缀数量与具体词缀",
        "重铸后各词缀的具体数值",
    ))
    if guaranteed_tag:
        unknown_fields.append(f"保底{guaranteed_tag}标签对应的具体词缀与数值")

    return HarvestOperationResult(
        verdict=verdict,
        craft_id=str(row["craft_id"]),
        effect_text=effect,
        mode=mode,
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
