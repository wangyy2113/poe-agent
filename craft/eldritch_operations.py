"""古灵（Eldritch）通货的保守状态转移。

只把 ``poedb_currency.effect_zh`` 中逐字写明的 12 种操作结构化：四档余烬、
四档溶液、古灵混沌/崇高/剥离石与冲突石。随机词缀、概率、隐式词缀具体内容、
特殊装备状态与未给出的等级推进规则一律保持未知。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from craft.capacity import CapacityAssessment, assess_affix_capacity

EldritchVerdict = Literal["compatible", "incompatible", "unknown"]
CheckStatus = Literal["pass", "fail", "unknown"]
Dominance = Literal["焚界者", "灭界者", "平衡", "无"]

ELDRITCH_ARMOUR_SLOTS = ("body_armour", "boots", "gloves", "helmet")
SLOT_LABELS = {
    "body_armour": "身体装备",
    "boots": "脚部装备",
    "gloves": "手部装备",
    "helmet": "头部装备",
    "other": "其他",
}


@dataclass(frozen=True)
class EldritchItemState:
    rarity: str | None = None
    item_slot: str | None = None
    dominance: Dominance | None = None
    prefix_count: int | None = None
    suffix_count: int | None = None
    has_searing_implicit: bool | None = None
    has_eater_implicit: bool | None = None
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False
    incomplete_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EldritchCheck:
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
class EldritchSpec:
    metadata_id: str
    name_zh: str
    name_en: str
    expected_effect: str
    action: str
    influence: str = ""
    tier: str = ""
    aliases: tuple[str, ...] = ()
    metamod_operation: str = ""


@dataclass
class EldritchOperationResult:
    verdict: EldritchVerdict
    operation: str
    operation_en: str
    metadata_id: str
    effect_text: str
    action: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    before: EldritchItemState
    after: dict[str, Any]
    checks: list[EldritchCheck] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    metamod_interactions: list[dict[str, str | None]] = field(default_factory=list)
    capacity: CapacityAssessment | None = None
    boundary: str = (
        "compatible 只表示已提供状态满足 POEDB 效果原文与结构化名额约束；"
        "不等于完整游戏可执行性，也不提供具体随机词缀、概率、步骤、成本或路线"
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "operation": self.operation,
            "operation_en": self.operation_en,
            "metadata_id": self.metadata_id,
            "effect_text": self.effect_text,
            "action": self.action,
            "source": self.source,
            "source_priority": self.source_priority,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "before": self.before.as_dict(),
            "after": self.after,
            "checks": [check.as_dict() for check in self.checks],
            "unknown_fields": self.unknown_fields,
            "metamod_interactions": self.metamod_interactions,
            "boundary": self.boundary,
        }


def _implicit_spec(side: str, level: int, tier: str, zh: str, en: str) -> EldritchSpec:
    opposite = "灭界者" if side == "焚界者" else "焚界者"
    noun = "余烬" if side == "焚界者" else "溶液"
    effect = (
        f"给身体装备、脚部装备、手部装备、头部装备 添加一个{tier}{side}基底词缀 "
        f"它会替换除{opposite}基底词缀外的所有原有基底词缀"
    )
    key = "Ember" if noun == "余烬" else "Ichor"
    return EldritchSpec(
        f"Metadata/Items/Currency/CurrencyEldritch{key}{level}", zh, en, effect,
        "set_implicit", side, tier,
    )


ELDRITCH_SPECS = (
    _implicit_spec("焚界者", 1, "次级", "次级古灵余烬", "Lesser Eldritch Ember"),
    _implicit_spec("焚界者", 2, "高级", "高级古灵余烬", "Greater Eldritch Ember"),
    _implicit_spec("焚界者", 3, "上级", "上级古灵余烬", "Grand Eldritch Ember"),
    _implicit_spec("焚界者", 4, "卓越", "卓越古灵余烬", "Exceptional Eldritch Ember"),
    _implicit_spec("灭界者", 1, "次级", "次级古灵溶液", "Lesser Eldritch Ichor"),
    _implicit_spec("灭界者", 2, "高级", "高级古灵溶液", "Greater Eldritch Ichor"),
    _implicit_spec("灭界者", 3, "上级", "上级古灵溶液", "Grand Eldritch Ichor"),
    _implicit_spec("灭界者", 4, "卓越", "卓越古灵溶液", "Exceptional Eldritch Ichor"),
    EldritchSpec(
        "Metadata/Items/Currency/CurrencyEldritchRerollRare", "古灵混沌石",
        "Eldritch Chaos Orb", "如果首要为焚界者，则重置前缀 如果首要为灭界者，则重置后缀",
        "reroll_dominant_side", aliases=("异能混沌石",), metamod_operation="异能混沌石",
    ),
    EldritchSpec(
        "Metadata/Items/Currency/CurrencyEldritchAddModToRare", "古灵崇高石",
        "Eldritch Exalted Orb", "如果首要为焚界者，则添加一个前缀 如果首要为灭界者，则添加一个后缀",
        "add_dominant_side", aliases=("异能崇高石",), metamod_operation="异能崇高石",
    ),
    EldritchSpec(
        "Metadata/Items/Currency/CurrencyEldritchRemoveMod", "古灵剥离石",
        "Eldritch Orb of Annulment", "如果首要为焚界者，则移除一个前缀 如果首要为灭界者，则移除一个后缀",
        "remove_dominant_side", aliases=("古灵无效石", "异能无效石"), metamod_operation="异能无效石",
    ),
    EldritchSpec(
        "Metadata/Items/Currency/CurrencyConflictOrb", "冲突石", "Orb of Conflict",
        "随机提高一件物品上焚界者或灭界者词缀的强度，同时降低另外一个 被降低的次级词缀会被移除",
        "conflict",
    ),
)
SPECS_BY_ID = {spec.metadata_id: spec for spec in ELDRITCH_SPECS}
OPERATION_NAMES = tuple(
    name for spec in ELDRITCH_SPECS
    for name in (spec.name_zh, spec.name_en, *spec.aliases)
)


def _resolve(conn: sqlite3.Connection, query: str) -> tuple[EldritchSpec, sqlite3.Row] | None:
    value = (query or "").strip()
    if not value:
        return None
    matched = [
        spec for spec in ELDRITCH_SPECS
        if value == spec.name_zh or value.casefold() == spec.name_en.casefold()
        or value in spec.aliases
    ]
    if len(matched) != 1:
        return None
    spec = matched[0]
    row = conn.execute(
        "SELECT * FROM poedb_currency WHERE metadata_id=?", (spec.metadata_id,)
    ).fetchone()
    return (spec, row) if row is not None else None


def _metamod_rows(conn: sqlite3.Connection, operation: str) -> list[dict[str, str | None]]:
    if not operation:
        return []
    try:
        rows = conn.execute(
            "SELECT guard_text,outcome,raw_value,note_text,source_url FROM "
            "craft_metamod_interaction WHERE operation_text=? ORDER BY column_order",
            (operation,),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "guard": str(row["guard_text"]),
            "outcome": str(row["outcome"]),
            "raw_value": str(row["raw_value"]),
            "note": str(row["note_text"]) if row["note_text"] else None,
            "source_url": str(row["source_url"]),
        }
        for row in rows
    ]


def _base_after(state: EldritchItemState) -> dict[str, Any]:
    return {
        "prefix_count": state.prefix_count,
        "suffix_count": state.suffix_count,
        "has_searing_implicit": state.has_searing_implicit,
        "has_eater_implicit": state.has_eater_implicit,
        "searing_implicit_tier": None,
        "eater_implicit_tier": None,
        "explicit_modifier_change": "unchanged",
        "implicit_modifier_change": "unchanged",
        "random_outcome": False,
    }


def _dominant_side(state: EldritchItemState, url: str) -> tuple[str | None, EldritchCheck]:
    if state.dominance is None:
        return None, EldritchCheck(
            "dominance", "unknown", "需要提供焚界者或灭界者哪一方为首要", url
        )
    if state.dominance not in ("焚界者", "灭界者"):
        return None, EldritchCheck(
            "dominance", "fail", f"当前首要状态为{state.dominance}，不满足效果原文的两个条件分支", url
        )
    side = "prefix" if state.dominance == "焚界者" else "suffix"
    return side, EldritchCheck(
        "dominance", "pass", f"{state.dominance}为首要，效果原文作用于{'前缀' if side == 'prefix' else '后缀'}", url
    )


def evaluate_eldritch_operation(
    conn: sqlite3.Connection,
    operation: str,
    state: EldritchItemState,
) -> EldritchOperationResult | None:
    resolved = _resolve(conn, operation)
    if resolved is None:
        return None
    spec, row = resolved
    url = str(row["url"])
    effect = str(row["effect_zh"] or "")
    source_ok = effect == spec.expected_effect
    checks = [EldritchCheck(
        "source_contract", "pass" if source_ok else "unknown",
        "POEDB 效果原文与已审计语义契约逐字一致"
        if source_ok else "POEDB 效果原文已变化，拒绝沿用旧状态转移",
        url,
    )]
    unknown: list[str] = []
    after = _base_after(state)
    capacity: CapacityAssessment | None = None

    if not source_ok:
        after.update({
            "prefix_count": None, "suffix_count": None,
            "has_searing_implicit": None, "has_eater_implicit": None,
            "explicit_modifier_change": "unknown",
            "implicit_modifier_change": "unknown",
            "random_outcome": None,
        })
        unknown.append("来源原文变化后的状态转移")
    elif spec.action == "set_implicit":
        if state.item_slot is None:
            checks.append(EldritchCheck(
                "item_slot", "unknown", "需要提供装备部位；原文只列出身体、脚部、手部与头部装备", url
            ))
        elif state.item_slot not in ELDRITCH_ARMOUR_SLOTS:
            checks.append(EldritchCheck(
                "item_slot", "fail", f"当前部位为{SLOT_LABELS.get(state.item_slot, state.item_slot)}，不在效果原文列出的四类装备内", url
            ))
        else:
            checks.append(EldritchCheck(
                "item_slot", "pass", f"{SLOT_LABELS[state.item_slot]}在效果原文列出的四类装备内", url
            ))
        if spec.influence == "焚界者":
            after["has_searing_implicit"] = True
            after["searing_implicit_tier"] = spec.tier
            after["implicit_modifier_change"] = "set_searing_and_replace_all_except_eater"
        else:
            after["has_eater_implicit"] = True
            after["eater_implicit_tier"] = spec.tier
            after["implicit_modifier_change"] = "set_eater_and_replace_all_except_searing"
        after["random_outcome"] = True
        unknown.extend((f"新增的具体{spec.influence}基底词缀", "同档可选词缀与各结果概率"))

    elif spec.action in {"reroll_dominant_side", "add_dominant_side", "remove_dominant_side"}:
        side, dominance_check = _dominant_side(state, url)
        checks.append(dominance_check)
        if side:
            count = state.prefix_count if side == "prefix" else state.suffix_count
            side_zh = "前缀" if side == "prefix" else "后缀"
            if spec.action == "reroll_dominant_side":
                after[f"{side}_count"] = None
                after["explicit_modifier_change"] = f"{side}_modifiers_rerolled"
                after["random_outcome"] = True
                unknown.append(f"重置后的{side_zh}数量、具体词缀与数值")
            elif spec.action == "remove_dominant_side":
                if count is None:
                    checks.append(EldritchCheck(
                        "target_affix", "unknown", f"需要提供当前{side_zh}数量，才能判断是否有可移除词缀"
                    ))
                    after[f"{side}_count"] = None
                elif count == 0:
                    checks.append(EldritchCheck(
                        "target_affix", "fail", f"当前没有{side_zh}可供移除"
                    ))
                else:
                    checks.append(EldritchCheck(
                        "target_affix", "pass", f"当前有 {count} 条{side_zh}，条件执行后减少一条"
                    ))
                    after[f"{side}_count"] = count - 1
                after["explicit_modifier_change"] = f"one_random_{side}_removed"
                after["random_outcome"] = True
                unknown.append(f"随机移除的具体{side_zh}")
            else:
                if state.prefix_count is None or state.suffix_count is None:
                    checks.append(EldritchCheck(
                        "affix_capacity", "unknown", "添加词缀前需要完整前缀数与后缀数"
                    ))
                    after[f"{side}_count"] = None
                else:
                    capacity = assess_affix_capacity(
                        conn, state.rarity, state.prefix_count, state.suffix_count,
                        incomplete_reason=state.incomplete_reason,
                    )
                    remaining = capacity.prefix_remaining if side == "prefix" else capacity.suffix_remaining
                    if capacity.status == "fail" or remaining == 0:
                        checks.append(EldritchCheck(
                            "affix_capacity", "fail", f"目标{side_zh}没有可用名额；{capacity.summary}"
                        ))
                    elif capacity.status == "unknown" or remaining is None:
                        checks.append(EldritchCheck(
                            "affix_capacity", "unknown", capacity.missing or "无法确定目标侧名额"
                        ))
                        after[f"{side}_count"] = None
                    else:
                        checks.append(EldritchCheck(
                            "affix_capacity", "pass", f"目标{side_zh}仍有 {remaining} 个名额", capacity.rule_url
                        ))
                        after[f"{side}_count"] = count + 1 if count is not None else None
                after["explicit_modifier_change"] = f"one_random_{side}_added"
                after["random_outcome"] = True
                unknown.append(f"新增的具体{side_zh}与数值")

    elif spec.action == "conflict":
        for key, present, label in (
            ("searing_implicit", state.has_searing_implicit, "焚界者"),
            ("eater_implicit", state.has_eater_implicit, "灭界者"),
        ):
            if present is None:
                checks.append(EldritchCheck(key, "unknown", f"需要确认物品是否有{label}基底词缀", url))
            elif not present:
                checks.append(EldritchCheck(key, "fail", f"没有{label}基底词缀，无法满足同时升一降一的原文", url))
            else:
                checks.append(EldritchCheck(key, "pass", f"已提供{label}基底词缀", url))
        after["implicit_modifier_change"] = "random_one_raised_other_lowered"
        after["random_outcome"] = True
        unknown.extend(("随机提高的是焚界者还是灭界者词缀及其概率", "升降后的确切档级"))
        if state.has_searing_implicit and state.has_eater_implicit:
            unknown.append("若被降低者为次级，哪一条基底词缀会被移除")

    special = []
    if state.corrupted:
        special.append("已腐化")
    if state.mirrored:
        special.append("已镜像")
    if state.fractured:
        special.append("含裂隙词缀")
    if special:
        checks.append(EldritchCheck(
            "special_state", "unknown", "、".join(special) + "的完整操作规则不在效果原文中"
        ))
        unknown.append("特殊状态下的完整可执行性与保留/移除语义")
    if state.incomplete_reason:
        checks.append(EldritchCheck("state_completeness", "unknown", state.incomplete_reason))

    statuses = {check.status for check in checks}
    verdict: EldritchVerdict = (
        "incompatible" if "fail" in statuses else "unknown" if "unknown" in statuses else "compatible"
    )
    return EldritchOperationResult(
        verdict=verdict,
        operation=spec.name_zh,
        operation_en=spec.name_en,
        metadata_id=spec.metadata_id,
        effect_text=effect,
        action=spec.action,
        source=str(row["source"]),
        source_priority=int(row["source_priority"]),
        source_url=url,
        fetched_at=str(row["fetched_at"]),
        before=state,
        after=after,
        checks=checks,
        unknown_fields=list(dict.fromkeys(unknown)),
        metamod_interactions=_metamod_rows(conn, spec.metamod_operation),
        capacity=capacity,
    )
