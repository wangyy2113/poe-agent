"""加密（Veiled）通货的保守状态转移与类别候选目录。

操作语义只来自 ``poedb_currency.effect_zh``。``mod_affix.category='veiled'``
只用于列出某底材类别已有的隐匿来源词缀目录；它不证明每次揭露会展示哪些选项，
也不能把生成权重换算成加密通货的结果概率。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from craft.capacity import CapacityAssessment, assess_affix_capacity, normalize_rarity
from parsers.item_mods import ItemClassRef, resolve_item_class

VeiledVerdict = Literal["compatible", "incompatible", "unknown"]
CheckStatus = Literal["pass", "fail", "unknown"]


@dataclass(frozen=True)
class VeiledItemState:
    rarity: str | None = None
    item_class: str = ""
    prefix_count: int | None = None
    suffix_count: int | None = None
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False
    incomplete_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class VeiledCheck:
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
class VeiledSpec:
    metadata_id: str
    name_zh: str
    name_en: str
    expected_effect: str
    action: str
    aliases: tuple[str, ...] = ()
    metamod_operation: str = ""


@dataclass
class VeiledOperationResult:
    verdict: VeiledVerdict
    operation: str
    operation_en: str
    metadata_id: str
    effect_text: str
    action: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    before: VeiledItemState
    after: dict[str, Any]
    checks: list[VeiledCheck] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    veiled_pool: dict[str, Any] | None = None
    metamod_interactions: list[dict[str, str | None]] = field(default_factory=list)
    capacity: CapacityAssessment | None = None
    boundary: str = (
        "compatible 只表示已提供状态满足 POEDB 效果原文中的稀有度与可移除词缀前提；"
        "类别目录不是单次揭露选项，也不提供选项数量、具体结果概率、步骤、成本或路线"
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
            "veiled_pool": self.veiled_pool,
            "metamod_interactions": self.metamod_interactions,
            "boundary": self.boundary,
        }


VEILED_SPECS = (
    VeiledSpec(
        "Metadata/Items/Currency/CurrencyRerollRareVeiled",
        "加密崇高石",
        "Veiled Exalted Orb",
        "为一个稀有物品移除一个随机词缀，并添加一个随机加密词缀",
        "remove_one_add_veiled",
        aliases=("隐匿崇高石",),
    ),
    VeiledSpec(
        "Metadata/Items/Currency/CurrencyRerollRareVeiledChaos",
        "加密混沌石",
        "Veiled Chaos Orb",
        "重置一件稀有物品上的所有词缀，并包含一个随机的加密词缀",
        "reroll_all_with_veiled",
        aliases=("隐匿混沌石",),
        metamod_operation="隐匿混沌石",
    ),
)
OPERATION_NAMES = tuple(
    name for spec in VEILED_SPECS
    for name in (spec.name_zh, spec.name_en, *spec.aliases)
)


def _resolve(conn: sqlite3.Connection, query: str) -> tuple[VeiledSpec, sqlite3.Row] | None:
    value = (query or "").strip()
    if not value:
        return None
    matches = [
        spec for spec in VEILED_SPECS
        if value == spec.name_zh or value.casefold() == spec.name_en.casefold()
        or value in spec.aliases
    ]
    if len(matches) != 1:
        return None
    spec = matches[0]
    row = conn.execute(
        "SELECT * FROM poedb_currency WHERE metadata_id=?", (spec.metadata_id,)
    ).fetchone()
    return (spec, row) if row is not None else None


def _candidate_pool(
    conn: sqlite3.Connection, item_class: str
) -> tuple[dict[str, Any] | None, ItemClassRef]:
    ref = resolve_item_class(conn, item_class)
    if not ref.ok:
        return None, ref
    placeholders = ",".join("?" for _ in ref.pages)
    rows = conn.execute(
        f"SELECT mod_code,name_zh,gen_label,correct_group,ilvl,weight,text_zh,url,item_class_page "
        f"FROM mod_affix WHERE category='veiled' AND item_class_page IN ({placeholders}) "
        "ORDER BY gen_type,correct_group,text_zh,item_class_page",
        ref.pages,
    ).fetchall()
    unique: dict[tuple[str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = (str(row["mod_code"]), str(row["gen_label"]), str(row["text_zh"]))
        unique.setdefault(key, row)
    candidates = [
        {
            "mod_code": str(row["mod_code"]),
            "name_zh": str(row["name_zh"] or ""),
            "position": str(row["gen_label"] or "未知"),
            "group": str(row["correct_group"] or ""),
            "item_level": int(row["ilvl"] or 0),
            "weight": int(row["weight"] or 0),
            "text": str(row["text_zh"] or ""),
            "url": str(row["url"] or ""),
        }
        for row in unique.values()
    ]
    prefix_count = sum(c["position"] == "前缀" for c in candidates)
    suffix_count = sum(c["position"] == "后缀" for c in candidates)
    return {
        "item_class": ref.name_zh or item_class,
        "scope": f"{len(ref.pages)} 个 POEDB 底材页面",
        "pages": ref.pages,
        "source_rows": len(rows),
        "unique_candidates": len(candidates),
        "prefix_candidates": prefix_count,
        "suffix_candidates": suffix_count,
        "available_positions": [
            label for label, count in (("前缀", prefix_count), ("后缀", suffix_count)) if count
        ],
        "candidates": candidates,
        "caveat": (
            "这是该类别在本库中的隐匿来源词缀目录；不代表一次揭露必定展示这些条目，"
            "weight 也不等于加密通货的结果概率"
        ),
    }, ref


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
    return [{
        "guard": str(row["guard_text"]),
        "outcome": str(row["outcome"]),
        "raw_value": str(row["raw_value"]),
        "note": str(row["note_text"]) if row["note_text"] else None,
        "source_url": str(row["source_url"]),
    } for row in rows]


def _possible_counts(
    state: VeiledItemState,
    positions: list[str],
    prefix_limit: int,
    suffix_limit: int,
) -> list[dict[str, int]]:
    if state.prefix_count is None or state.suffix_count is None:
        return []
    possibilities: set[tuple[int, int]] = set()
    for removed in ("前缀", "后缀"):
        p = state.prefix_count - (removed == "前缀")
        s = state.suffix_count - (removed == "后缀")
        if p < 0 or s < 0:
            continue
        for added in positions or ("前缀", "后缀"):
            after_p = p + (added == "前缀")
            after_s = s + (added == "后缀")
            if after_p <= prefix_limit and after_s <= suffix_limit:
                possibilities.add((after_p, after_s))
    return [
        {"prefix_count": prefix_count, "suffix_count": suffix_count}
        for prefix_count, suffix_count in sorted(possibilities)
    ]


def evaluate_veiled_operation(
    conn: sqlite3.Connection,
    operation: str,
    state: VeiledItemState,
) -> VeiledOperationResult | None:
    resolved = _resolve(conn, operation)
    if resolved is None:
        return None
    spec, row = resolved
    url = str(row["url"])
    effect = str(row["effect_zh"] or "")
    source_ok = effect == spec.expected_effect
    checks = [VeiledCheck(
        "source_contract", "pass" if source_ok else "unknown",
        "POEDB 效果原文与已审计语义契约逐字一致"
        if source_ok else "POEDB 效果原文已变化，拒绝沿用旧状态转移",
        url,
    )]
    unknown: list[str] = []
    capacity: CapacityAssessment | None = None

    if source_ok:
        rarity = normalize_rarity(state.rarity)
        if rarity is None:
            checks.append(VeiledCheck("rarity", "unknown", "需要提供物品稀有度；效果原文要求稀有物品", url))
        elif rarity != "稀有":
            checks.append(VeiledCheck("rarity", "fail", f"当前是{rarity}物品，但效果原文要求稀有物品", url))
        else:
            checks.append(VeiledCheck("rarity", "pass", "当前是稀有物品，与效果原文一致", url))

    pool: dict[str, Any] | None = None
    positions: list[str] = []
    if state.item_class:
        pool, ref = _candidate_pool(conn, state.item_class)
        if pool is None:
            checks.append(VeiledCheck(
                "veiled_pool", "unknown", ref.reason or "无法解析底材类别的隐匿词缀目录"
            ))
        elif not pool["unique_candidates"]:
            checks.append(VeiledCheck(
                "veiled_pool", "unknown",
                f"已解析{pool['item_class']}，但本库没有该类别的隐匿来源词缀记录；不据此断言不可使用"
            ))
        else:
            positions = list(pool["available_positions"])
            checks.append(VeiledCheck(
                "veiled_pool", "pass",
                f"{pool['item_class']}在 {pool['scope']} 中有 {pool['unique_candidates']} 条去重候选目录记录；"
                "这不是单次揭露选项或概率",
                str(pool["candidates"][0]["url"]) if pool["candidates"] else "",
            ))
    else:
        unknown.append("未给底材类别，无法列出本库对应的隐匿词缀候选目录")

    after: dict[str, Any] = {
        "rarity": "稀有" if source_ok else None,
        "prefix_count": state.prefix_count if source_ok else None,
        "suffix_count": state.suffix_count if source_ok else None,
        "possible_affix_counts": [],
        "modifier_change": "" if source_ok else "unknown",
        "has_veiled_modifier": True if source_ok else None,
        "veiled_modifier_position": (positions or ["前缀", "后缀"]) if source_ok else [],
        "random_outcome": True if source_ok else None,
    }
    if not source_ok:
        unknown.append("来源原文变化后的状态转移")
    elif spec.action == "reroll_all_with_veiled":
        after.update(
            prefix_count=None,
            suffix_count=None,
            modifier_change="all_modifiers_rerolled_with_one_random_veiled_modifier",
        )
        unknown.extend(("重置后的前后缀数量与非加密词缀", "随机加密词缀及揭露选项与概率"))
    else:
        after["modifier_change"] = "one_random_modifier_removed_and_one_random_veiled_modifier_added"
        if state.prefix_count is None or state.suffix_count is None:
            checks.append(VeiledCheck(
                "removable_modifier", "unknown", "需要完整前缀数与后缀数，才能确认有可随机移除的词缀"
            ))
            after["prefix_count"] = None
            after["suffix_count"] = None
        elif state.prefix_count + state.suffix_count == 0:
            checks.append(VeiledCheck("removable_modifier", "fail", "当前没有词缀可供随机移除"))
        else:
            capacity = assess_affix_capacity(
                conn, state.rarity, state.prefix_count, state.suffix_count,
                incomplete_reason=state.incomplete_reason,
            )
            if capacity.status == "fail":
                checks.append(VeiledCheck("affix_capacity", "fail", capacity.summary))
            elif capacity.status == "unknown" or capacity.prefix_limit is None or capacity.suffix_limit is None:
                checks.append(VeiledCheck(
                    "affix_capacity", "unknown", capacity.missing or "无法确定前后缀名额"
                ))
            else:
                checks.append(VeiledCheck(
                    "removable_modifier", "pass",
                    f"当前共有 {state.prefix_count + state.suffix_count} 条前后缀，可随机移除一条",
                ))
                after["possible_affix_counts"] = _possible_counts(
                    state, positions, capacity.prefix_limit, capacity.suffix_limit
                )
                if len(after["possible_affix_counts"]) == 1:
                    only = after["possible_affix_counts"][0]
                    after["prefix_count"] = only["prefix_count"]
                    after["suffix_count"] = only["suffix_count"]
                else:
                    after["prefix_count"] = None
                    after["suffix_count"] = None
        unknown.extend(("随机移除的具体词缀", "随机加密词缀及揭露选项与概率"))

    special = []
    if state.corrupted:
        special.append("已腐化")
    if state.mirrored:
        special.append("已镜像")
    if state.fractured:
        special.append("含裂隙词缀")
    if special:
        checks.append(VeiledCheck(
            "special_state", "unknown", "、".join(special) + "的完整操作规则不在效果原文中"
        ))
        unknown.append("特殊状态下的完整可执行性与保留/移除语义")
    if state.incomplete_reason:
        checks.append(VeiledCheck("state_completeness", "unknown", state.incomplete_reason))

    statuses = {check.status for check in checks}
    verdict: VeiledVerdict = (
        "incompatible" if "fail" in statuses else "unknown" if "unknown" in statuses else "compatible"
    )
    return VeiledOperationResult(
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
        veiled_pool=pool,
        metamod_interactions=_metamod_rows(conn, spec.metamod_operation),
        capacity=capacity,
    )
