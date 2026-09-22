"""Influence eligibility derived from pinned Path of Building base tags.

PoB stores two pieces required for a deterministic check:

* every base item has ``tags`` and, when influence is supported, an
  ``influenceTags`` mapping;
* every modifier has an ordered spawn-weight tag list.  ``mod_affix`` keeps
  the positive tags from that list as ``spawn_tags``.

The mapping below only translates PoB's public display names to its internal
keys.  It does not describe acquisition, Awakener's Orb behaviour, influence
limits, or crafting steps.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InfluenceSpec:
    key: str
    category: str
    name_zh: str
    name_en: str
    aliases: tuple[str, ...]


INFLUENCES: tuple[InfluenceSpec, ...] = (
    InfluenceSpec("shaper", "shaper", "塑界者", "Shaper", ("塑界",)),
    InfluenceSpec("elder", "elder", "裂界者", "Elder", ("裂界",)),
    InfluenceSpec("adjudicator", "warlord", "督军", "Warlord", ("督軍",)),
    InfluenceSpec("basilisk", "hunter", "狩猎者", "Hunter", ("猎人", "獵人", "狩獵者")),
    InfluenceSpec("crusader", "crusader", "圣战者", "Crusader", ("聖戰者",)),
    InfluenceSpec("eyrie", "redeemer", "救赎者", "Redeemer", ("救贖者",)),
)

_INFLUENCE_BY_ALIAS: dict[str, InfluenceSpec] = {}
for _spec in INFLUENCES:
    for _alias in (
        _spec.key,
        _spec.category,
        _spec.name_zh,
        _spec.name_en,
        *_spec.aliases,
    ):
        _INFLUENCE_BY_ALIAS[_alias.casefold()] = _spec

# Eldritch implicits have separate slot and dominance rules.  Keep them in the
# broad source set so feasibility still refuses to treat them as ordinary
# sources, but do not expose their raw base-table tags as legacy influence
# eligibility.  ``eldritch_operation_state`` is the authoritative path.
INFLUENCE_CATEGORIES = frozenset(
    {*(spec.category for spec in INFLUENCES), "eater", "searing"}
)


@dataclass(frozen=True)
class BaseInfluenceProfile:
    base_id: str
    name_en: str
    name_zh: str
    poedb_slug: str
    category_key: str
    poedb_page: str
    item_class: str
    modifier_page: str
    tags: tuple[str, ...]
    influence_tags: dict[str, str]
    pob_url: str
    poedb_url: str
    source_commit: str

    def generation_tag(self, influence: InfluenceSpec) -> str:
        return self.influence_tags.get(influence.key, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_id": self.base_id,
            "name_en": self.name_en,
            "name_zh": self.name_zh,
            "poedb_slug": self.poedb_slug,
            "category": self.category_key,
            "item_class": self.item_class,
            "base_tags": list(self.tags),
            "supported_influences": [
                {
                    "key": spec.key,
                    "category": spec.category,
                    "name_zh": spec.name_zh,
                    "name_en": spec.name_en,
                    "generation_tag": self.generation_tag(spec),
                }
                for spec in INFLUENCES
                if self.generation_tag(spec)
            ],
        }


def resolve_influence(value: str) -> InfluenceSpec | None:
    return _INFLUENCE_BY_ALIAS.get((value or "").strip().casefold())


def _json_strings(value: Any) -> tuple[str, ...] | None:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or any(not isinstance(v, str) for v in parsed):
        return None
    return tuple(parsed)


def _json_string_map(value: Any) -> dict[str, str] | None:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or not v
        for k, v in parsed.items()
    ):
        return None
    return parsed


def resolve_base_profile(
    conn: sqlite3.Connection, base_type: str
) -> tuple[BaseInfluenceProfile | None, str]:
    """Resolve one exact Chinese or English base name; ambiguity is an error."""
    value = (base_type or "").strip()
    if not value:
        return None, "base_type 不能为空"
    rows = conn.execute(
        "SELECT * FROM pob_base_item WHERE name_zh=? OR name_en=? COLLATE NOCASE "
        "ORDER BY base_id",
        (value, value),
    ).fetchall()
    unique = {str(row["base_id"]): row for row in rows}
    if not unique:
        return None, f"底材 {value!r} 没有逐字匹配的注册记录"
    if len(unique) != 1:
        return None, f"底材 {value!r} 命中 {len(unique)} 条记录，无法唯一确定"
    row = next(iter(unique.values()))
    tags = _json_strings(row["tags"])
    influence_tags = _json_string_map(row["influence_tags"])
    if tags is None or not tags or influence_tags is None:
        return None, f"底材 {value!r} 的标签记录损坏，不能验证影响力适用性"
    required = (
        row["name_en"], row["name_zh"], row["poedb_slug"], row["item_class"],
        row["pob_url"], row["poedb_url"], row["source_commit"],
    )
    if any(not item for item in required):
        return None, f"底材 {value!r} 的来源记录不完整，不能验证影响力适用性"
    return BaseInfluenceProfile(
        base_id=str(row["base_id"]),
        name_en=str(row["name_en"]),
        name_zh=str(row["name_zh"]),
        poedb_slug=str(row["poedb_slug"]),
        category_key=str(row["category_key"]),
        poedb_page=str(row["poedb_page"]),
        item_class=str(row["item_class"]),
        modifier_page=str(row["modifier_page"] or ""),
        tags=tags,
        influence_tags=influence_tags,
        pob_url=str(row["pob_url"]),
        poedb_url=str(row["poedb_url"]),
        source_commit=str(row["source_commit"]),
    ), ""


def influence_info_url(commit: str) -> str:
    return (
        "https://github.com/PathOfBuildingCommunity/PathOfBuilding/blob/"
        f"{commit}/src/Modules/ItemTools.lua#L13-L25"
    )


def spawn_rule_url(commit: str) -> str:
    return (
        "https://github.com/PathOfBuildingCommunity/PathOfBuilding/blob/"
        f"{commit}/src/Classes/Item.lua#L1650-L1691"
    )


def preferred_modifier_pages(
    profile: BaseInfluenceProfile, available_pages: list[str]
) -> list[str]:
    """Narrow a broad ItemClass page set with exact base metadata when possible."""
    for candidate in (
        profile.modifier_page,
        profile.poedb_slug,
        profile.poedb_page,
    ):
        if candidate and candidate in available_pages:
            return [candidate]
    return available_pages


def row_matches_base(
    category: str,
    spawn_tags: tuple[str, ...],
    profile: BaseInfluenceProfile,
    requested: tuple[InfluenceSpec, ...],
) -> bool:
    """Apply the subset of PoB spawn-weight matching represented in our DB.

    ``spawn_tags`` contains only tags whose source weight is positive.  Influence
    rows must match the exact influence generation tag; their trailing ``default``
    tag must not make an uninfluenced base appear eligible.
    """
    if category in INFLUENCE_CATEGORIES:
        matching = [spec for spec in requested if spec.category == category]
        return any(profile.generation_tag(spec) in spawn_tags for spec in matching)
    if category == "normal":
        return bool(set(profile.tags) & set(spawn_tags))
    return True
