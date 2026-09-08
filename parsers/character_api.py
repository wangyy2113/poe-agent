"""GGG 官方 PoE1 角色 API → 部分 ``PobBuild``。

官方端点 ``GET https://api.pathofexile.com/character/<name>`` 需要 OAuth
``account:characters`` scope，一次返回角色身份、已装备物品、插槽宝石、珠宝和天赋。
访问地址由本模块固定拼出，不接受调用方提供 URL；Bearer token 只进请求头，不写缓存、
不写日志，也不返回前端。User-Agent 必须符合 GGG 的
``OAuth clientId/version (contact: contact)`` 格式。

角色 API 不是 PoB：它没有 PlayerStat、PoB 配置或“当前主技能”。因此转换后只把响应里
明确存在的字段接入既有报告，缺失维度继续显示未知，不从装备和节点局部重算面板。

官方契约：
https://www.pathofexile.com/developer/docs/reference#characters
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from entities.taxonomy import ModKind
from parsers.item_text import (
    ItemFlags,
    ParsedItem,
    ParsedMod,
    Rarity,
    Requirements,
    SocketLayout,
)
from parsers.pob_build import BuildGem, BuildItem, PobBuild, SkillGroup


API_BASE = "https://api.pathofexile.com"
SOURCE = "https://www.pathofexile.com/developer/docs/reference#characters"
MAX_RESPONSE_BYTES = 5_000_000


class CharacterApiError(RuntimeError):
    def __init__(self, code: str, reason: str, status_code: int = 502) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.status_code = status_code


@dataclass(frozen=True)
class CharacterImport:
    name: str
    league: str = ""
    realm: str = "pc"
    version: str = ""
    alternate_ascendancy: str = ""
    mastery_effects: tuple[tuple[str, str], ...] = ()


_BASE_CLASSES = frozenset({
    "Scion", "Marauder", "Ranger", "Witch", "Duelist", "Templar", "Shadow",
})
_ASCENDANCY_BASE = {
    "Ascendant": "Scion",
    "Juggernaut": "Marauder", "Berserker": "Marauder", "Chieftain": "Marauder",
    "Deadeye": "Ranger", "Raider": "Ranger", "Pathfinder": "Ranger", "Warden": "Ranger",
    "Necromancer": "Witch", "Elementalist": "Witch", "Occultist": "Witch",
    "Slayer": "Duelist", "Gladiator": "Duelist", "Champion": "Duelist",
    "Inquisitor": "Templar", "Hierophant": "Templar", "Guardian": "Templar",
    "Assassin": "Shadow", "Saboteur": "Shadow", "Trickster": "Shadow",
}
_RARITY = {
    "normal": Rarity.NORMAL,
    "magic": Rarity.MAGIC,
    "rare": Rarity.RARE,
    "unique": Rarity.UNIQUE,
}
_PROPERTY_KEYS = {
    "armour": "armour",
    "evasion rating": "evasion",
    "energy shield": "energy_shield",
    "ward": "ward",
    "physical damage": "physical_damage",
    "elemental damage": "elemental_damage",
    "chaos damage": "chaos_damage",
    "critical strike chance": "crit_chance",
    "attacks per second": "attack_speed",
    "chance to block": "block_chance",
    "block chance": "block_chance",
}
_SOCKET_COLOUR = {"S": "R", "D": "G", "I": "B", "G": "W"}
_INFLUENCE_KEYS = {
    "shaper": "shaper", "elder": "elder", "warlord": "warlord", "hunter": "hunter",
    "crusader": "crusader", "redeemer": "redeemer",
    "searing": "searing_exarch", "tangled": "eater_of_worlds",
}
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)")


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str:
    return str(value or "").strip()


def _first_property_value(prop: object) -> str:
    if not isinstance(prop, dict):
        return ""
    values = _as_list(prop.get("values"))
    rendered: list[str] = []
    for value in values:
        if isinstance(value, list) and value:
            rendered.append(_text(value[0]))
        elif isinstance(value, str):
            rendered.append(value.strip())
    return ", ".join(value for value in rendered if value)


def _property_map(raw: object) -> dict[str, str]:
    out: dict[str, str] = {}
    for prop in _as_list(raw):
        if not isinstance(prop, dict):
            continue
        name = _text(prop.get("name"))
        value = _first_property_value(prop)
        if not name or not value:
            continue
        out[_PROPERTY_KEYS.get(name.casefold(), name)] = value
    return out


def _integer_property(raw: object, *names: str) -> int | None:
    wanted = {name.casefold() for name in names}
    for prop in _as_list(raw):
        if not isinstance(prop, dict) or _text(prop.get("name")).casefold() not in wanted:
            continue
        matched = _NUMBER_RE.search(_first_property_value(prop))
        if matched:
            return int(float(matched.group()))
    return None


def _mod(entry: object, default_kind: ModKind) -> ParsedMod | None:
    flags: dict[str, Any] = {}
    if isinstance(entry, str):
        raw = entry.strip()
    elif isinstance(entry, dict):
        raw = _text(entry.get("description"))
        flags = entry.get("flags") if isinstance(entry.get("flags"), dict) else {}
    else:
        return None
    if not raw:
        return None
    kind = default_kind
    if flags.get("crafted"):
        kind = ModKind.CRAFTED
    elif flags.get("fractured"):
        kind = ModKind.FRACTURED
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    text = " / ".join(lines)
    return ParsedMod(
        kind=kind,
        text=text,
        raw=raw,
        values=[float(match.group()) for match in _NUMBER_RE.finditer(text)],
        flags=[key for key, enabled in flags.items() if enabled],
        lines=lines,
    )


def _mods(item: dict[str, Any], key: str, kind: ModKind) -> list[ParsedMod]:
    return [mod for entry in _as_list(item.get(key)) if (mod := _mod(entry, kind))]


def _sockets(raw: object) -> SocketLayout:
    groups: dict[int, list[str]] = {}
    for socket in _as_list(raw):
        if not isinstance(socket, dict):
            continue
        try:
            group = int(socket.get("group", 0))
        except (TypeError, ValueError):
            group = 0
        colour = _text(socket.get("sColour")) or _SOCKET_COLOUR.get(
            _text(socket.get("attr")), _text(socket.get("attr"))
        )
        groups.setdefault(group, []).append(colour or "?")
    linked = [groups[key] for key in sorted(groups)]
    return SocketLayout(raw=" ".join("-".join(group) for group in linked), groups=linked)


def _requirements(raw: object) -> Requirements:
    return Requirements(
        level=_integer_property(raw, "Level"),
        strength=_integer_property(raw, "Str", "Strength"),
        dexterity=_integer_property(raw, "Dex", "Dexterity"),
        intelligence=_integer_property(raw, "Int", "Intelligence"),
    )


def _parsed_item(item: dict[str, Any]) -> ParsedItem:
    rarity = _RARITY.get(_text(item.get("rarity")).casefold(), Rarity.UNKNOWN)
    implicits = _mods(item, "implicitMods", ModKind.IMPLICIT)
    explicits = _mods(item, "explicitMods", ModKind.EXPLICIT)
    # 兼容仍返回字符串数组的旧字段；当前官方 ItemMod 会把 crafted/fractured 放 flags。
    explicits.extend(_mods(item, "craftedMods", ModKind.CRAFTED))
    explicits.extend(_mods(item, "fracturedMods", ModKind.FRACTURED))
    raw_influences = item.get("influences") if isinstance(item.get("influences"), dict) else {}
    influence_keys = [key for key, enabled in raw_influences.items() if enabled]
    influence_keys.extend(key for key in _INFLUENCE_KEYS if item.get(key))
    influences = list(dict.fromkeys(
        _INFLUENCE_KEYS.get(_text(key).casefold(), _text(key).casefold())
        for key in influence_keys if _text(key)
    ))
    flags = ItemFlags(
        corrupted=bool(item.get("corrupted")),
        mirrored=bool(item.get("mirrored")),
        split=bool(item.get("split")),
        unidentified=item.get("identified") is False,
        synthesised=bool(item.get("synthesised")),
        fractured=bool(item.get("fractured")),
        veiled=bool(item.get("veiled")),
        influences=influences,
    )
    return ParsedItem(
        rarity=rarity,
        name=_text(item.get("name")),
        base_type=_text(item.get("baseType")) or _text(item.get("typeLine")),
        item_level=(item.get("ilvl") if isinstance(item.get("ilvl"), int) else None),
        quality=_integer_property(item.get("properties"), "Quality"),
        sockets=_sockets(item.get("sockets")),
        requirements=_requirements(item.get("requirements")),
        flags=flags,
        implicits=implicits,
        explicits=explicits,
        enchants=_mods(item, "enchantMods", ModKind.ENCHANT),
        properties=_property_map(item.get("properties")),
    )


def _build_item(item: object, fallback_slot: str) -> BuildItem | None:
    if not isinstance(item, dict):
        return None
    slot = _text(item.get("inventoryId")) or fallback_slot
    try:
        parsed = _parsed_item(item)
    except (TypeError, ValueError) as exc:
        return BuildItem(slot=slot, raw="", error=f"官方角色 API 装备字段无法解析：{exc}")
    return BuildItem(
        slot=slot,
        raw=json.dumps(item, ensure_ascii=False, sort_keys=True),
        parsed=parsed,
    )


def _gem(item: object) -> BuildGem | None:
    if not isinstance(item, dict):
        return None
    name = _text(item.get("typeLine")) or _text(item.get("baseType"))
    if not name:
        return None
    props = item.get("properties")
    return BuildGem(
        name_en=name,
        level=_integer_property(props, "Level"),
        quality=_integer_property(props, "Quality"),
        enabled=True,
        is_support=bool(item.get("support")),
    )


def _skill_groups(equipment: list[Any]) -> list[SkillGroup]:
    out: list[SkillGroup] = []
    for parent in equipment:
        if not isinstance(parent, dict):
            continue
        sockets = _as_list(parent.get("sockets"))
        socket_groups: dict[int, int] = {}
        for index, socket in enumerate(sockets):
            if isinstance(socket, dict):
                try:
                    socket_groups[index] = int(socket.get("group", 0))
                except (TypeError, ValueError):
                    socket_groups[index] = 0
        groups: dict[int, list[tuple[int, BuildGem]]] = {}
        for child in _as_list(parent.get("socketedItems")):
            gem = _gem(child)
            if gem is None or not isinstance(child, dict):
                continue
            try:
                socket_index = int(child.get("socket", 0))
            except (TypeError, ValueError):
                socket_index = 0
            group = socket_groups.get(socket_index, 0)
            groups.setdefault(group, []).append((socket_index, gem))
        slot = _text(parent.get("inventoryId")) or "未知部位"
        for group, gems in sorted(groups.items()):
            out.append(SkillGroup(
                slot=f"{slot} · 连接组 {group + 1}",
                gems=tuple(gem for _index, gem in sorted(gems, key=lambda pair: pair[0])),
            ))
    return out


def _normalise_version(value: object) -> str:
    matched = _VERSION_RE.match(_text(value).removeprefix("v"))
    return f"{matched.group(1)}.{matched.group(2)}" if matched else ""


def character_build(payload: object) -> tuple[PobBuild, CharacterImport]:
    """把官方 ``Get Character`` 响应转换为现有报告可消费的部分 Build。"""
    if isinstance(payload, dict) and "character" in payload and payload["character"] is None:
        raise CharacterApiError("character_not_found", "GGG 角色 API 未找到该角色", 404)
    if not isinstance(payload, dict) or not isinstance(payload.get("character"), dict):
        raise CharacterApiError(
            "character_api_invalid_response", "官方角色 API 响应缺少 character 对象"
        )
    character: dict[str, Any] = payload["character"]
    name = _text(character.get("name"))
    if not name:
        raise CharacterApiError(
            "character_api_invalid_response", "官方角色 API 响应缺少角色名"
        )
    realm = _text(character.get("realm")) or "pc"
    if realm != "pc":
        raise CharacterApiError(
            "poe2_character_rejected" if realm == "poe2" else "unsupported_realm",
            f"只支持 PoE1 PC 角色，收到 realm={realm!r}",
            422,
        )

    raw_class = _text(character.get("class"))
    if raw_class in _BASE_CLASSES:
        class_name, ascendancy = raw_class, ""
    else:
        class_name, ascendancy = _ASCENDANCY_BASE.get(raw_class, ""), raw_class

    equipment = _as_list(character.get("equipment"))
    jewels = _as_list(character.get("jewels"))
    parsed_items = [
        built for index, item in enumerate(equipment, start=1)
        if (built := _build_item(item, f"Equipment {index}")) is not None
    ]
    parsed_items.extend(
        built for index, item in enumerate(jewels, start=1)
        if (built := _build_item(item, f"Jewel {index}")) is not None
    )
    passives = character.get("passives") if isinstance(character.get("passives"), dict) else {}
    hashes = [*_as_list(passives.get("hashes")), *_as_list(passives.get("hashes_ex"))]
    passive_nodes = tuple(str(value) for value in hashes if isinstance(value, (int, str)))
    raw_masteries = passives.get("mastery_effects")
    mastery_effects = tuple(
        (str(node), str(effect)) for node, effect in raw_masteries.items()
    ) if isinstance(raw_masteries, dict) else ()
    metadata = character.get("metadata") if isinstance(character.get("metadata"), dict) else {}
    alternate = _text(passives.get("alternate_ascendancy"))
    version = _normalise_version(metadata.get("version"))

    warnings = [
        "官方角色 API 不提供 PoB PlayerStat 或配置快照；DPS、EHP、Max Hit 与抗性等面板项保持未知。",
        "角色 API 不标记主技能；已保留装备上的技能连接组，但不会猜哪一组是主技能。",
        "角色背包与 rucksack 不属于已装备 Build，本次导入未纳入体检。",
    ]
    if raw_class and not class_name:
        warnings.append(f"角色职业字段为 {raw_class!r}，本地无法确定其基础职业，未作猜测。")
    if alternate:
        warnings.append(f"角色 API 给出的副升华/Bloodline 为 {alternate}。")

    build = PobBuild(
        level=character.get("level") if isinstance(character.get("level"), int) else None,
        class_name=class_name,
        ascend_class_name=ascendancy,
        bandit=_text(passives.get("bandit_choice")),
        pantheon_major=_text(passives.get("pantheon_major")),
        pantheon_minor=_text(passives.get("pantheon_minor")),
        skill_groups=_skill_groups(equipment),
        items=parsed_items,
        passive_nodes=passive_nodes,
        tree_jewels=len(jewels),
        tree_version=version,
        tree_version_basis="GGG 角色 API 的 character.metadata.version",
        passive_nodes_basis="GGG 角色 API 的 character.passives.hashes / hashes_ex",
        warnings=warnings,
    )
    return build, CharacterImport(
        name=name,
        league=_text(character.get("league")),
        realm=realm,
        version=version,
        alternate_ascendancy=alternate,
        mastery_effects=mastery_effects,
    )


class CharacterApiClient:
    def __init__(self, token: str, user_agent: str, timeout: float = 30.0) -> None:
        self.token = token.strip()
        self.user_agent = user_agent.strip()
        self.timeout = timeout

    def _get(self, path: str) -> dict[str, Any]:
        if not self.token:
            raise CharacterApiError(
                "character_api_not_configured",
                "服务端未配置 POE_OAUTH_TOKEN；需要带 account:characters scope 的 GGG OAuth token",
                503,
            )
        if not self.user_agent.startswith("OAuth "):
            raise CharacterApiError(
                "character_api_not_configured",
                "服务端未配置合规的 POE_OAUTH_USER_AGENT；GGG 要求格式为 "
                "OAuth clientId/version (contact: contact)",
                503,
            )
        url = f"{API_BASE}{path}"
        try:
            response = httpx.get(
                url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": self.user_agent,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise CharacterApiError(
                "character_api_unavailable", f"连接 GGG 角色 API 失败：{exc}", 502
            ) from exc
        if response.status_code in {401, 403}:
            raise CharacterApiError(
                "character_api_auth_failed",
                "GGG 拒绝了 OAuth token；请检查 token 是否有效并包含 account:characters scope",
                502,
            )
        if response.status_code == 404:
            raise CharacterApiError("character_not_found", "GGG 角色 API 未找到该角色", 404)
        if response.status_code == 429:
            raise CharacterApiError(
                "character_api_rate_limited", "GGG 角色 API 正在限流，请稍后重试", 503
            )
        if response.status_code >= 400:
            raise CharacterApiError(
                "character_api_failed",
                f"GGG 角色 API 返回 HTTP {response.status_code}",
                502,
            )
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise CharacterApiError(
                "character_api_invalid_response", "GGG 角色 API 响应超过 5 MB 上限", 502
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CharacterApiError(
                "character_api_invalid_response", "GGG 角色 API 返回的不是合法 JSON", 502
            ) from exc
        if not isinstance(payload, dict):
            raise CharacterApiError(
                "character_api_invalid_response", "GGG 角色 API 顶层响应不是对象", 502
            )
        return payload

    def list_characters(self) -> list[dict[str, Any]]:
        payload = self._get("/character")
        characters = payload.get("characters")
        if not isinstance(characters, list):
            raise CharacterApiError(
                "character_api_invalid_response", "GGG 角色列表响应缺少 characters 数组", 502
            )
        return [entry for entry in characters if isinstance(entry, dict)]

    def get_character(self, name: str) -> dict[str, Any]:
        clean = name.strip()
        if not clean:
            raise CharacterApiError("character_name_required", "角色名不能为空", 422)
        if len(clean) > 100:
            raise CharacterApiError("character_name_too_long", "角色名不能超过 100 个字符", 422)
        return self._get(f"/character/{quote(clean, safe='')}")
