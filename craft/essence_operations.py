"""精华制作操作的保守状态转移。

只消费 ``poedb_essence.base_effect``、``level_cap`` 与
``poedb_essence_mod`` 的页面原字段。不会从精华等阶、普通词缀池或社区经验外推
成功率、额外词缀数量、制作步骤与成本。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from craft.capacity import normalize_rarity
from parsers.item_mods import resolve_item_class

EssenceVerdict = Literal["compatible", "incompatible", "unknown"]
CheckStatus = Literal["pass", "fail", "unknown"]

LOW_ESSENCE_EFFECT = "升级一件普通物品至稀有物品并且确保一项特定属性。"
HIGH_ESSENCE_EFFECT = "确保一项特定属性, 升级一件普通物品至稀有物品或者重置一件稀有物品的属性."
SUPPORTED_EFFECTS = frozenset({LOW_ESSENCE_EFFECT, HIGH_ESSENCE_EFFECT})
_CLASS_SPLIT_RE = re.compile(r"[,，、]")


@dataclass(frozen=True)
class EssenceItemState:
    rarity: str | None = None
    item_level: int | None = None
    item_class: str = ""
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False
    incomplete_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EssenceCheck:
    key: str
    status: CheckStatus
    summary: str
    source_url: str = ""

    def as_dict(self) -> dict[str, str]:
        out = {"key": self.key, "status": self.status, "summary": self.summary}
        if self.source_url:
            out["source_url"] = self.source_url
        return out


@dataclass
class EssenceOperationResult:
    verdict: EssenceVerdict
    essence: str
    essence_en: str
    metadata_id: str
    tier: int
    base_effect: str
    level_cap: int | None
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    before: EssenceItemState
    after: dict[str, Any]
    guaranteed_modifier: dict[str, str] | None
    checks: list[EssenceCheck] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    boundary: str = (
        "compatible 只表示已提供状态通过精华页面原文中的稀有度、等级上限与装备类别检查；"
        "不等于完整游戏可执行性，也不提供额外词缀、数值结果、成功率、步骤、成本或路线"
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "essence": self.essence,
            "essence_en": self.essence_en,
            "metadata_id": self.metadata_id,
            "tier": self.tier,
            "base_effect": self.base_effect,
            "level_cap": self.level_cap,
            "source": self.source,
            "source_priority": self.source_priority,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "before": self.before.as_dict(),
            "after": self.after,
            "guaranteed_modifier": self.guaranteed_modifier,
            "checks": [check.as_dict() for check in self.checks],
            "unknown_fields": self.unknown_fields,
            "boundary": self.boundary,
        }


def _resolve_essence(conn: sqlite3.Connection, query: str) -> sqlite3.Row | None:
    value = (query or "").strip()
    if not value:
        return None
    rows = conn.execute(
        "SELECT * FROM poedb_essence WHERE name_zh=? OR lower(name_en)=lower(?) "
        "ORDER BY metadata_id",
        (value, value),
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def _known_classes(conn: sqlite3.Connection, metadata_id: str) -> dict[str, str]:
    """返回该精华的装备类别 → 保底词缀；重复类别视为来源结构不唯一。"""
    found: dict[str, str] = {}
    duplicates: set[str] = set()
    rows = conn.execute(
        "SELECT item_classes,text_zh FROM poedb_essence_mod "
        "WHERE metadata_id=? ORDER BY group_index",
        (metadata_id,),
    ).fetchall()
    for row in rows:
        for name in _CLASS_SPLIT_RE.split(str(row["item_classes"])):
            item_class = name.strip()
            if not item_class:
                continue
            if item_class in found:
                duplicates.add(item_class)
            else:
                found[item_class] = str(row["text_zh"])
    for item_class in duplicates:
        found.pop(item_class, None)
    return found


def _canonical_item_class(
    conn: sqlite3.Connection, raw: str, known: dict[str, str]
) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    if value in known:
        return value
    ref = resolve_item_class(conn, value)
    return ref.name_zh if ref.name_zh in known else None


def _rarity_check(effect: str, rarity: str | None, url: str) -> EssenceCheck:
    normalized = normalize_rarity(rarity)
    if normalized is None:
        return EssenceCheck(
            "rarity", "unknown", "需要给出物品稀有度；页面效果只明确普通与稀有物品", url
        )
    allowed = ("普通",) if effect == LOW_ESSENCE_EFFECT else ("普通", "稀有")
    if normalized not in allowed:
        return EssenceCheck(
            "rarity", "fail",
            f"当前是{normalized}物品，但该精华效果原文只描述{'或'.join(allowed)}物品",
            url,
        )
    mode = "升级为稀有" if normalized == "普通" else "重置稀有物品属性"
    return EssenceCheck("rarity", "pass", f"当前是{normalized}物品；页面原文对应操作为{mode}", url)


def evaluate_essence_operation(
    conn: sqlite3.Connection,
    essence: str,
    state: EssenceItemState,
) -> EssenceOperationResult | None:
    row = _resolve_essence(conn, essence)
    if row is None:
        return None

    url = str(row["url"])
    effect = str(row["base_effect"] or "")
    source_ok = effect in SUPPORTED_EFFECTS
    checks = [EssenceCheck(
        "source_contract",
        "pass" if source_ok else "unknown",
        "POEDB 基础效果原文与已审计语义契约逐字一致"
        if source_ok else "POEDB 基础效果原文已变化，拒绝沿用旧状态转移",
        url,
    )]
    unknown_fields: list[str] = []

    if source_ok:
        checks.append(_rarity_check(effect, state.rarity, url))

    level_cap = int(row["level_cap"]) if row["level_cap"] is not None else None
    if level_cap is None:
        checks.append(EssenceCheck("item_level", "pass", "POEDB 页面未给该精华设置物品等级上限", url))
    elif state.item_level is None:
        checks.append(EssenceCheck(
            "item_level", "unknown", f"该精华限制物品等级 {level_cap} 及以下；需要提供物品等级", url
        ))
    elif state.item_level > level_cap:
        checks.append(EssenceCheck(
            "item_level", "fail", f"物品等级 {state.item_level} 超过该精华上限 {level_cap}", url
        ))
    else:
        checks.append(EssenceCheck(
            "item_level", "pass", f"物品等级 {state.item_level} 未超过该精华上限 {level_cap}", url
        ))

    known_classes = _known_classes(conn, str(row["metadata_id"]))
    canonical_class = _canonical_item_class(conn, state.item_class, known_classes)
    guaranteed: dict[str, str] | None = None
    if not state.item_class:
        checks.append(EssenceCheck(
            "item_class", "unknown", "需要提供装备类别，才能从页面逐类别表确定保底词缀", url
        ))
    elif canonical_class is None:
        checks.append(EssenceCheck(
            "item_class", "unknown",
            f"页面逐类别表无法把 {state.item_class!r} 唯一映射到保底词缀；不把缺行推断为不可使用",
            url,
        ))
    else:
        guaranteed = {"item_class": canonical_class, "text": known_classes[canonical_class]}
        checks.append(EssenceCheck(
            "item_class", "pass", f"页面对{canonical_class}明确给出保底词缀：{known_classes[canonical_class]}", url
        ))

    special = []
    if state.corrupted:
        special.append("已腐化")
    if state.mirrored:
        special.append("已镜像")
    if state.fractured:
        special.append("含裂隙词缀")
    if special:
        checks.append(EssenceCheck(
            "special_state", "unknown", "、".join(special) + "的精华操作规则未纳入本 v1 状态机"
        ))
        unknown_fields.append("特殊状态下的完整可执行性与保留/移除语义")
    if state.incomplete_reason:
        checks.append(EssenceCheck("state_completeness", "unknown", state.incomplete_reason))

    if any(check.status == "fail" for check in checks):
        verdict: EssenceVerdict = "incompatible"
    elif any(check.status == "unknown" for check in checks):
        verdict = "unknown"
    else:
        verdict = "compatible"

    normalized = normalize_rarity(state.rarity)
    after = {
        "rarity": "稀有" if source_ok and normalized in {"普通", "稀有"} else normalized,
        "prefix_count": None,
        "suffix_count": None,
        "operation_mode": (
            "upgrade_normal_to_rare" if normalized == "普通"
            else "reroll_rare_properties" if normalized == "稀有" and effect == HIGH_ESSENCE_EFFECT
            else "unknown"
        ),
        "modifier_identities": (
            "generated_with_guaranteed_essence_modifier" if normalized == "普通"
            else "rerolled_with_guaranteed_essence_modifier"
            if normalized == "稀有" and effect == HIGH_ESSENCE_EFFECT
            else "unknown"
        ),
        "guaranteed_modifier": guaranteed,
    }
    unknown_fields.extend((
        "操作后的前后缀数量与除保底项外的具体词缀",
        "保底词缀的最终随机数值",
    ))
    if guaranteed is None:
        unknown_fields.append("当前装备类别对应的保底词缀")
    if not source_ok:
        after = {
            "rarity": normalized,
            "prefix_count": None,
            "suffix_count": None,
            "operation_mode": "unknown",
            "modifier_identities": "unchanged",
            "guaranteed_modifier": None,
        }
        unknown_fields.append("来源原文变化后的状态转移")

    return EssenceOperationResult(
        verdict=verdict,
        essence=str(row["name_zh"]),
        essence_en=str(row["name_en"] or ""),
        metadata_id=str(row["metadata_id"]),
        tier=int(row["tier"]),
        base_effect=effect,
        level_cap=level_cap,
        source=str(row["source"]),
        source_priority=int(row["source_priority"]),
        source_url=url,
        fetched_at=str(row["fetched_at"]),
        before=state,
        after=after,
        guaranteed_modifier=guaranteed,
        checks=checks,
        unknown_fields=list(dict.fromkeys(unknown_fields)),
    )
