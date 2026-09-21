"""基础通货制作操作的保守状态转移。

状态变化只来自 ``poedb_currency.effect_zh`` 的逐字效果；前后缀空位只由
``craft_affix_capacity`` 判定。没有来源支撑的腐化、镜像、裂隙、影响力与完整
Metamod 执行语义不在这里补写，命中这些特殊状态时保持 ``unknown``。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from craft.capacity import CapacityAssessment, assess_affix_capacity, normalize_rarity

OperationVerdict = Literal["compatible", "incompatible", "unknown"]
CheckStatus = Literal["pass", "fail", "unknown"]


@dataclass(frozen=True)
class ItemState:
    rarity: str | None = None
    prefix_count: int | None = None
    suffix_count: int | None = None
    item_kind: str = "default"
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False
    incomplete_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StateCheck:
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
class OperationSpec:
    metadata_id: str
    name_zh: str
    name_en: str
    expected_effect: str
    action: str
    required_rarity: str | None = None
    resulting_rarities: tuple[str, ...] = ()
    requires_open_affix: bool = False


@dataclass
class OperationResult:
    verdict: OperationVerdict
    operation: str
    operation_en: str
    metadata_id: str
    effect_text: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    before: ItemState
    after: dict[str, Any]
    checks: list[StateCheck] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    capacity: CapacityAssessment | None = None
    boundary: str = (
        "compatible 只表示已提供状态通过效果原文中的稀有度前提与结构化前后缀名额；"
        "不等于完整游戏可执行性，也不提供结果词缀、成功率、步骤、成本或路线"
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "operation": self.operation,
            "operation_en": self.operation_en,
            "metadata_id": self.metadata_id,
            "effect_text": self.effect_text,
            "source": self.source,
            "source_priority": self.source_priority,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "before": self.before.as_dict(),
            "after": self.after,
            "checks": [check.as_dict() for check in self.checks],
            "unknown_fields": self.unknown_fields,
            "boundary": self.boundary,
        }


OPERATION_SPECS = (
    OperationSpec(
        "Metadata/Items/Currency/CurrencyAddModToMagic", "增幅石", "Orb of Augmentation",
        "用一个新随机词缀强化一个魔法物品", "add_one", "魔法", ("魔法",), True,
    ),
    OperationSpec(
        "Metadata/Items/Currency/CurrencyAddModToRare", "崇高石", "Exalted Orb",
        "用一个新随机词缀强化一个稀有物品", "add_one", "稀有", ("稀有",), True,
    ),
    OperationSpec(
        "Metadata/Items/Currency/CurrencyConvertToNormal", "重铸石", "Orb of Scouring",
        "将一件物品上的所有属性删除，变回普通物品", "clear_to_normal", None, ("普通",),
    ),
    OperationSpec(
        "Metadata/Items/Currency/CurrencyModValues", "神圣石", "Divine Orb",
        "重置一件物品上随机词缀的数值", "reroll_values",
    ),
    OperationSpec(
        "Metadata/Items/Currency/CurrencyRemoveMod", "剥离石", "Orb of Annulment",
        "随机移除物品上的一条属性", "remove_one",
    ),
    OperationSpec(
        "Metadata/Items/Currency/CurrencyRerollMagic", "改造石", "Orb of Alteration",
        "重置一件魔法物品上的所有属性", "reroll_all", "魔法", ("魔法",),
    ),
    OperationSpec(
        "Metadata/Items/Currency/CurrencyRerollRare", "混沌石", "Chaos Orb",
        "用新的随机属性来重铸一个稀有物品", "reroll_all", "稀有", ("稀有",),
    ),
    OperationSpec(
        "Metadata/Items/Currency/CurrencyUpgradeMagicToRare", "富豪石", "Regal Orb",
        "将一件魔法物品升级成稀有物品", "upgrade", "魔法", ("稀有",),
    ),
    OperationSpec(
        "Metadata/Items/Currency/CurrencyUpgradeRandomly", "机会石", "Orb of Chance",
        "将一件普通物品随机升级成魔法，稀有或传奇物品", "random_upgrade", "普通",
        ("魔法", "稀有", "传奇"),
    ),
    OperationSpec(
        "Metadata/Items/Currency/CurrencyUpgradeToRare", "点金石", "Orb of Alchemy",
        "将一个普通物品升级为稀有物品", "upgrade", "普通", ("稀有",),
    ),
)
SPECS_BY_ID = {spec.metadata_id: spec for spec in OPERATION_SPECS}
OPERATION_NAMES = tuple(
    name for spec in OPERATION_SPECS for name in (spec.name_zh, spec.name_en)
)


def _resolve(conn: sqlite3.Connection, query: str) -> tuple[OperationSpec, sqlite3.Row] | None:
    value = (query or "").strip()
    if not value:
        return None
    rows = conn.execute(
        "SELECT * FROM poedb_currency WHERE name_zh=? OR lower(name_en)=lower(?) "
        "ORDER BY metadata_id",
        (value, value),
    ).fetchall()
    supported = [(SPECS_BY_ID.get(str(row["metadata_id"])), row) for row in rows]
    supported = [(spec, row) for spec, row in supported if spec is not None]
    return supported[0] if len(supported) == 1 else None


def _base_after(state: ItemState) -> dict[str, Any]:
    return {
        "rarity": normalize_rarity(state.rarity),
        "possible_rarities": [],
        "prefix_count": state.prefix_count,
        "suffix_count": state.suffix_count,
        "possible_affix_counts": [],
        "modifier_identities": "unchanged",
        "modifier_values": "unchanged",
    }


def _transition(spec: OperationSpec, state: ItemState, capacity: CapacityAssessment | None) -> tuple[dict[str, Any], list[str]]:
    after = _base_after(state)
    unknown: list[str] = []
    action = spec.action
    if spec.resulting_rarities:
        if len(spec.resulting_rarities) == 1:
            after["rarity"] = spec.resulting_rarities[0]
        else:
            after["rarity"] = None
            after["possible_rarities"] = list(spec.resulting_rarities)

    if action == "add_one":
        after["modifier_identities"] = "one_random_modifier_added"
        possibilities: list[dict[str, int]] = []
        if state.prefix_count is not None and state.suffix_count is not None and capacity:
            if (capacity.prefix_remaining or 0) > 0:
                possibilities.append({"prefix_count": state.prefix_count + 1, "suffix_count": state.suffix_count})
            if (capacity.suffix_remaining or 0) > 0:
                possibilities.append({"prefix_count": state.prefix_count, "suffix_count": state.suffix_count + 1})
        after["prefix_count"] = None
        after["suffix_count"] = None
        after["possible_affix_counts"] = possibilities
        if len(possibilities) > 1:
            unknown.append("新增随机词缀是前缀还是后缀，以及具体词缀与数值")
        else:
            unknown.append("新增的具体词缀与数值")
    elif action == "reroll_all":
        after["prefix_count"] = None
        after["suffix_count"] = None
        after["modifier_identities"] = "all_properties_rerolled"
        after["modifier_values"] = "rerolled_with_properties"
        unknown.append("重铸后的前后缀数量、具体词缀与数值")
    elif action == "clear_to_normal":
        after.update(prefix_count=0, suffix_count=0, modifier_identities="all_properties_removed")
    elif action == "reroll_values":
        after["modifier_values"] = "random_modifier_values_rerolled"
        unknown.append("各随机词缀重置后的具体数值")
    elif action == "remove_one":
        possibilities = []
        if state.prefix_count is not None and state.suffix_count is not None:
            if state.prefix_count > 0:
                possibilities.append({"prefix_count": state.prefix_count - 1, "suffix_count": state.suffix_count})
            if state.suffix_count > 0:
                possibilities.append({"prefix_count": state.prefix_count, "suffix_count": state.suffix_count - 1})
        after.update(prefix_count=None, suffix_count=None, possible_affix_counts=possibilities,
                     modifier_identities="one_random_property_removed")
        unknown.append("随机移除的是哪条属性；仅凭效果原文不能锁定前缀或后缀")
    elif action == "upgrade":
        after.update(prefix_count=None, suffix_count=None, modifier_identities="not_specified_by_source")
        unknown.append("升级后的前后缀数量、具体词缀与数值")
    elif action == "random_upgrade":
        after.update(prefix_count=None, suffix_count=None, modifier_identities="not_specified_by_source")
        unknown.extend(("随机升级后的稀有度", "升级后的前后缀数量、具体词缀与数值"))
    return after, unknown


def evaluate_operation(
    conn: sqlite3.Connection,
    operation: str,
    state: ItemState,
) -> OperationResult | None:
    resolved = _resolve(conn, operation)
    if resolved is None:
        return None
    spec, row = resolved
    checks: list[StateCheck] = []
    unknown_fields: list[str] = []
    effect = str(row["effect_zh"] or "")
    source_ok = effect == spec.expected_effect
    checks.append(StateCheck(
        "source_contract",
        "pass" if source_ok else "unknown",
        "POEDB 效果原文与已审计语义契约逐字一致" if source_ok else "POEDB 效果原文已变化，拒绝沿用旧状态转移",
        str(row["url"]),
    ))

    normalized = normalize_rarity(state.rarity)
    if spec.required_rarity:
        if normalized is None:
            checks.append(StateCheck("rarity", "unknown", f"需要给出稀有度；效果原文要求{spec.required_rarity}物品", str(row["url"])))
        elif normalized != spec.required_rarity:
            checks.append(StateCheck("rarity", "fail", f"当前是{normalized}物品，但效果原文只描述{spec.required_rarity}物品", str(row["url"])))
        else:
            checks.append(StateCheck("rarity", "pass", f"当前稀有度为{normalized}，与效果原文一致", str(row["url"])))

    special = []
    if state.corrupted:
        special.append("已腐化")
    if state.mirrored:
        special.append("已镜像")
    if state.fractured:
        special.append("含裂隙词缀")
    if special:
        checks.append(StateCheck("special_state", "unknown", "、".join(special) + "的操作规则未纳入本 v1 状态机"))
        unknown_fields.append("特殊状态下的完整可执行性与保留/移除语义")
    if state.incomplete_reason:
        checks.append(StateCheck("state_completeness", "unknown", state.incomplete_reason))

    capacity: CapacityAssessment | None = None
    if spec.requires_open_affix:
        if state.prefix_count is None or state.suffix_count is None:
            checks.append(StateCheck("affix_capacity", "unknown", "新增随机词缀前必须给出完整前缀数与后缀数"))
        else:
            capacity = assess_affix_capacity(
                conn, state.rarity, state.prefix_count, state.suffix_count,
                item_kind=state.item_kind,
            )
            if capacity.status == "unknown":
                checks.append(StateCheck("affix_capacity", "unknown", capacity.missing, capacity.rule_url))
            elif capacity.status == "fail":
                checks.append(StateCheck("affix_capacity", "unknown", "输入状态自身超过名额规则，无法据此模拟新增", capacity.rule_url))
            elif not (capacity.prefix_remaining or capacity.suffix_remaining):
                checks.append(StateCheck("affix_capacity", "fail", "前缀与后缀都已达到上限，没有可新增词缀的位置", capacity.rule_url))
            else:
                checks.append(StateCheck("affix_capacity", "pass", (
                    f"仍有前缀 {capacity.prefix_remaining}、后缀 {capacity.suffix_remaining} 个位置；"
                    "只证明存在空位，不代表任何具体词缀会出现"
                ), capacity.rule_url))

    if any(check.status == "fail" for check in checks):
        verdict: OperationVerdict = "incompatible"
    elif any(check.status == "unknown" for check in checks):
        verdict = "unknown"
    else:
        verdict = "compatible"
    after, transition_unknown = _transition(spec, state, capacity)
    unknown_fields.extend(transition_unknown)
    if not source_ok:
        after = _base_after(state)
        unknown_fields.append("来源原文变化后的状态转移")

    return OperationResult(
        verdict=verdict,
        operation=spec.name_zh,
        operation_en=spec.name_en,
        metadata_id=spec.metadata_id,
        effect_text=effect,
        source=str(row["source"]),
        source_priority=int(row["source_priority"]),
        source_url=str(row["url"]),
        fetched_at=str(row["fetched_at"]),
        before=state,
        after=after,
        checks=checks,
        unknown_fields=list(dict.fromkeys(unknown_fields)),
        capacity=capacity,
    )
