"""Awakener's Orb two-item state transition, bound to the POEDB effect text.

This module deliberately models only what the currency page states: the donor
is destroyed, its influence is added to another item of the same item class,
and the target is reforged as a rare item with both influences and new
modifiers.  It does not infer retained influenced modifiers, affix counts,
rolls, probabilities, item-level rules, or a crafting route.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from craft.influence import resolve_influence

Verdict = Literal["compatible", "incompatible", "unknown"]
CheckStatus = Literal["pass", "fail", "unknown"]

METADATA_ID = "Metadata/Items/AtlasExiles/ApplyInfluence"
NAME_ZH = "觉醒者之石"
NAME_EN = "Awakener's Orb"
EXPECTED_EFFECT = (
    "摧毁一件物品，将它的影响效果添加给另一件同类物品 "
    "后者会重铸为一件稀有物品，并兼有两者的影响效果和新词缀。"
)


@dataclass(frozen=True)
class AwakenerItemState:
    item_class: str = ""
    rarity: str = ""
    influences: tuple[str, ...] = ()
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False
    incomplete_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_class": self.item_class,
            "rarity": self.rarity,
            "influences": list(self.influences),
            "corrupted": self.corrupted,
            "mirrored": self.mirrored,
            "fractured": self.fractured,
            "incomplete_reason": self.incomplete_reason,
        }


@dataclass(frozen=True)
class AwakenerCheck:
    key: str
    status: CheckStatus
    summary: str
    source_url: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "summary": self.summary,
            "source_url": self.source_url,
        }


@dataclass
class AwakenerOperationResult:
    verdict: Verdict
    effect_text: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    donor: AwakenerItemState
    target: AwakenerItemState
    after: dict[str, Any]
    checks: list[AwakenerCheck] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    boundary: str = (
        "compatible 只表示两个已提供输入满足 POEDB 效果原文可审计的同类与双影响条件；"
        "不表示会保留任一指定词缀，也不提供重铸后的前后缀、数值、概率、步骤、成本或路线"
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "found": True,
            "verdict": self.verdict,
            "operation": NAME_ZH,
            "operation_en": NAME_EN,
            "metadata_id": METADATA_ID,
            "effect_text": self.effect_text,
            "source": self.source,
            "source_priority": self.source_priority,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "donor": self.donor.as_dict(),
            "target": self.target.as_dict(),
            "after": self.after,
            "checks": [check.as_dict() for check in self.checks],
            "unknown_fields": list(dict.fromkeys(self.unknown_fields)),
            "boundary": self.boundary,
        }


def _canonical_influences(values: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    known: list[str] = []
    unknown: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        spec = resolve_influence(value)
        if spec is None:
            unknown.append(value)
        else:
            known.append(spec.name_zh)
    return tuple(dict.fromkeys(known)), tuple(dict.fromkeys(unknown))


def _influence_check(
    label: str, state: AwakenerItemState, source_url: str
) -> tuple[AwakenerCheck, tuple[str, ...]]:
    known, unknown = _canonical_influences(state.influences)
    if unknown:
        return AwakenerCheck(
            f"{label}_influence", "unknown",
            f"{label}含未纳入六种传统影响映射的标记：{'、'.join(unknown)}",
            source_url,
        ), known
    if not known:
        return AwakenerCheck(
            f"{label}_influence", "unknown",
            f"需要提供{label}的一种传统影响，才能验证原文所说的影响效果",
            source_url,
        ), known
    if len(known) != 1:
        return AwakenerCheck(
            f"{label}_influence", "unknown",
            f"{label}给出了 {len(known)} 种传统影响；来源原文没有定义多影响输入的行为",
            source_url,
        ), known
    return AwakenerCheck(
        f"{label}_influence", "pass", f"{label}影响：{known[0]}", source_url
    ), known


def evaluate_awakener_orb(
    conn: sqlite3.Connection,
    donor: AwakenerItemState,
    target: AwakenerItemState,
) -> AwakenerOperationResult | None:
    row = conn.execute(
        "SELECT * FROM poedb_currency WHERE metadata_id=?", (METADATA_ID,)
    ).fetchone()
    if row is None:
        return None

    effect = str(row["effect_zh"] or "")
    source_url = str(row["url"])
    checks: list[AwakenerCheck] = []
    unknown_fields = [
        "重铸后的具体新词缀、前后缀数量与数值",
        "来源原文未说明会保留任一输入装备的哪条具体词缀",
        "成功率、期望消耗、完整步骤与市场成本",
    ]

    source_ok = effect == EXPECTED_EFFECT
    checks.append(AwakenerCheck(
        "source_contract",
        "pass" if source_ok else "unknown",
        "POEDB 效果原文与已审计语义契约逐字一致"
        if source_ok else "POEDB 效果原文已变化，拒绝沿用旧状态转移",
        source_url,
    ))

    donor_check, donor_influences = _influence_check("供体", donor, source_url)
    target_check, target_influences = _influence_check("目标", target, source_url)
    checks.extend((donor_check, target_check))

    if len(donor_influences) == len(target_influences) == 1:
        if donor_influences[0] == target_influences[0]:
            checks.append(AwakenerCheck(
                "distinct_influences", "unknown",
                "两件物品是同一种影响；来源原文没有定义这种输入是否可执行",
                source_url,
            ))
        else:
            checks.append(AwakenerCheck(
                "distinct_influences", "pass",
                f"两种影响不同：{donor_influences[0]} + {target_influences[0]}",
                source_url,
            ))
    else:
        checks.append(AwakenerCheck(
            "distinct_influences", "unknown", "需要先唯一确定两件物品各自的一种传统影响",
            source_url,
        ))

    donor_class = donor.item_class.strip()
    target_class = target.item_class.strip()
    if not donor_class or not target_class:
        checks.append(AwakenerCheck(
            "same_item_class", "unknown", "需要提供供体与目标的物品类别，才能验证同类物品",
            source_url,
        ))
    elif donor_class.casefold() != target_class.casefold():
        checks.append(AwakenerCheck(
            "same_item_class", "fail",
            f"供体类别 {donor_class} 与目标类别 {target_class} 不同，不满足原文的同类物品",
            source_url,
        ))
    else:
        checks.append(AwakenerCheck(
            "same_item_class", "pass", f"两件物品类别相同：{target_class}", source_url
        ))

    special: list[str] = []
    for label, state in (("供体", donor), ("目标", target)):
        flags = []
        if state.corrupted:
            flags.append("已腐化")
        if state.mirrored:
            flags.append("已镜像")
        if state.fractured:
            flags.append("含裂隙词缀")
        if flags:
            special.append(f"{label}{'、'.join(flags)}")
    if special:
        checks.append(AwakenerCheck(
            "special_state", "unknown",
            "；".join(special) + "；来源原文没有定义这些特殊状态的完整可执行性",
            source_url,
        ))
        unknown_fields.append("特殊状态下的完整可执行性")

    incomplete = [
        f"{label}：{state.incomplete_reason}"
        for label, state in (("供体", donor), ("目标", target))
        if state.incomplete_reason
    ]
    if incomplete:
        checks.append(AwakenerCheck(
            "state_completeness", "unknown", "；".join(incomplete), source_url
        ))

    statuses = {check.status for check in checks}
    verdict: Verdict = (
        "incompatible" if "fail" in statuses else "unknown" if "unknown" in statuses else "compatible"
    )
    combined = list(dict.fromkeys((*target_influences, *donor_influences)))
    after = {
        "donor_destroyed": True if source_ok else None,
        "target_rarity": "稀有" if source_ok else None,
        "target_influences": combined if source_ok and len(combined) == 2 else [],
        "target_modifier_identities": "reforged_with_new_modifiers" if source_ok else "unknown",
        "target_prefix_count": None,
        "target_suffix_count": None,
    }
    if not source_ok:
        unknown_fields.append("来源原文变化后的状态转移")

    return AwakenerOperationResult(
        verdict=verdict,
        effect_text=effect,
        source=str(row["source"]),
        source_priority=int(row["source_priority"]),
        source_url=source_url,
        fetched_at=str(row["fetched_at"]),
        donor=donor,
        target=target,
        after=after,
        checks=checks,
        unknown_fields=unknown_fields,
    )
