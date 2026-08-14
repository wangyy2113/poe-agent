"""PoB 胸甲底材注册表抽取器。

PoB ``Data/Bases/body.lua`` 提供底材的英文名、类型与生成标签；POEDB 的中英文
``Body_Armours`` 页面提供同一 slug 下的中英文名称桥接。两侧必须逐条精确对应，
任何缺失或多解都整条拒绝，不能由 slug 猜英文名，也不能把英文名冒充中文名。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from selectolax.parser import HTMLParser

from kb.sources.pob_lua import LuaOpaque, load_lua_file, parse_assignments

BODY_BASE_FILE = "Data/Bases/body.lua"
POEDB_BODY_CN_URL = "https://poedb.tw/cn/Body_Armours"
POEDB_BODY_EN_URL = "https://poedb.tw/us/Body_Armours"

ARMOUR_TAG_TO_PAGE: dict[str, str] = {
    "str_armour": "Body_Armours_str",
    "dex_armour": "Body_Armours_dex",
    "int_armour": "Body_Armours_int",
    "str_dex_armour": "Body_Armours_str_dex",
    "str_int_armour": "Body_Armours_str_int",
    "dex_int_armour": "Body_Armours_dex_int",
    "str_dex_int_armour": "Body_Armours_str_dex_int",
}


class BaseStructureError(ValueError):
    """底材源结构不完整或跨来源无法唯一对应。"""


@dataclass(frozen=True)
class BodyArmourBase:
    base_id: str
    name_en: str
    name_zh: str
    poedb_slug: str
    item_class: str
    subtype: str
    socket_limit: int
    tags: tuple[str, ...]
    influence_tags: dict[str, str]
    armour_tag: str
    modifier_page: str
    implicit_text: str | None
    implicit_mod_types: list[Any]
    implicit_ids: tuple[str, ...]
    defences: dict[str, Any]
    requirements: dict[str, Any]
    source_file: str = BODY_BASE_FILE

    @property
    def poedb_url(self) -> str:
        return f"https://poedb.tw/cn/{self.poedb_slug}"

    @property
    def content_hash(self) -> str:
        payload = {
            "name_en": self.name_en,
            "name_zh": self.name_zh,
            "poedb_slug": self.poedb_slug,
            "item_class": self.item_class,
            "subtype": self.subtype,
            "socket_limit": self.socket_limit,
            "tags": self.tags,
            "influence_tags": self.influence_tags,
            "armour_tag": self.armour_tag,
            "modifier_page": self.modifier_page,
            "implicit_text": self.implicit_text,
            "implicit_mod_types": self.implicit_mod_types,
            "implicit_ids": self.implicit_ids,
            "defences": self.defences,
            "requirements": self.requirements,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_SPACE_RE = re.compile(r"\s+")


def _text(node) -> str:  # noqa: ANN001
    return _SPACE_RE.sub(" ", node.text(deep=True)).strip() if node is not None else ""


def parse_poedb_body_names(html: str) -> dict[str, str]:
    """返回 ``slug -> 页面显示名``；同 slug 异名或空页面直接拒绝。"""
    tree = HTMLParser(html)
    out: dict[str, str] = {}
    for link in tree.css("a.whiteitem.BodyArmour"):
        name = _text(link)
        href = str(link.attributes.get("href") or "").strip()
        if not name or not href:
            continue
        slug = href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if not slug:
            continue
        previous = out.get(slug)
        if previous is not None and previous != name:
            raise BaseStructureError(f"POEDB slug {slug!r} 同时对应 {previous!r} 与 {name!r}")
        out[slug] = name
    if not out:
        raise BaseStructureError("POEDB Body_Armours 页面没有可识别的普通底材卡片")
    return out


def _jsonable(value: Any, *, path: str) -> Any:
    if isinstance(value, LuaOpaque):
        raise BaseStructureError(f"{path} 含未求值 Lua 表达式")
    if isinstance(value, dict):
        int_keys = sorted(k for k in value if isinstance(k, int))
        other_keys = [k for k in value if not isinstance(k, int)]
        if int_keys and not other_keys and int_keys == list(range(1, len(int_keys) + 1)):
            return [_jsonable(value[k], path=f"{path}[{k}]") for k in int_keys]
        return {
            str(k): _jsonable(v, path=f"{path}.{k}")
            for k, v in value.items()
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise BaseStructureError(f"{path} 含不支持的值类型 {type(value).__name__}")


def _string_set(value: Any, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, dict):
        raise BaseStructureError(f"{path} 必须是集合表")
    names = sorted(str(k) for k, enabled in value.items() if isinstance(k, str) and enabled is True)
    if not names:
        raise BaseStructureError(f"{path} 不能为空")
    return tuple(names)


def _string_map(value: Any, *, path: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise BaseStructureError(f"{path} 必须是映射表")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item:
            raise BaseStructureError(f"{path} 含非字符串键值")
        out[key] = item
    return dict(sorted(out.items()))


def _string_array(value: Any, *, path: str) -> tuple[str, ...]:
    converted = _jsonable(value or {}, path=path)
    if converted == {}:
        return ()
    if not isinstance(converted, list) or any(not isinstance(item, str) for item in converted):
        raise BaseStructureError(f"{path} 必须是字符串数组")
    return tuple(converted)


def extract_body_armour_bases(
    pob_root: Path,
    poedb_cn_html: str,
    poedb_en_html: str,
) -> list[BodyArmourBase]:
    """抽取全部 PoB 胸甲并与 POEDB 中英文名称逐条桥接。"""
    path = pob_root / "src" / Path(BODY_BASE_FILE)
    if not path.is_file():
        raise BaseStructureError(f"找不到 {BODY_BASE_FILE}")

    raw_bases = parse_assignments(load_lua_file(path))
    if not raw_bases:
        raise BaseStructureError(f"{BODY_BASE_FILE} 没有可识别底材")

    cn_by_slug = parse_poedb_body_names(poedb_cn_html)
    en_by_slug = parse_poedb_body_names(poedb_en_html)
    if set(cn_by_slug) != set(en_by_slug):
        missing_cn = sorted(set(en_by_slug) - set(cn_by_slug))
        missing_en = sorted(set(cn_by_slug) - set(en_by_slug))
        raise BaseStructureError(
            f"POEDB 中英文胸甲清单 slug 不一致：缺中文={missing_cn[:5]}，缺英文={missing_en[:5]}"
        )

    slugs_by_en: dict[str, list[str]] = {}
    for slug, name in en_by_slug.items():
        slugs_by_en.setdefault(name, []).append(slug)

    out: list[BodyArmourBase] = []
    seen_ids: set[str] = set()
    for name_en, raw in raw_bases.items():
        if not isinstance(raw, dict):
            raise BaseStructureError(f"底材 {name_en!r} 不是表")
        matches = slugs_by_en.get(str(name_en), [])
        if len(matches) != 1:
            raise BaseStructureError(
                f"PoB 底材 {name_en!r} 在 POEDB 英文页命中 {len(matches)} 个 slug"
            )
        slug = matches[0]
        name_zh = cn_by_slug.get(slug, "")
        if not name_zh:
            raise BaseStructureError(f"POEDB 底材 {slug!r} 缺中文名")

        item_class = raw.get("type")
        subtype = raw.get("subType")
        socket_limit = raw.get("socketLimit")
        if item_class != "Body Armour":
            raise BaseStructureError(f"{name_en!r}.type 不是 Body Armour")
        if not isinstance(subtype, str) or not subtype:
            raise BaseStructureError(f"{name_en!r}.subType 缺失")
        if not isinstance(socket_limit, int) or socket_limit <= 0:
            raise BaseStructureError(f"{name_en!r}.socketLimit 非正整数")

        tags = _string_set(raw.get("tags"), path=f"{name_en}.tags")
        armour_tags = [tag for tag in ARMOUR_TAG_TO_PAGE if tag in tags]
        if len(armour_tags) != 1:
            raise BaseStructureError(
                f"{name_en!r} 必须且只能有一个属性护甲标签，实际 {armour_tags}"
            )
        armour_tag = armour_tags[0]
        influence_tags = _string_map(
            raw.get("influenceTags"), path=f"{name_en}.influenceTags"
        )
        defences = _jsonable(raw.get("armour"), path=f"{name_en}.armour")
        requirements = _jsonable(raw.get("req"), path=f"{name_en}.req")
        if not isinstance(defences, dict) or not defences:
            raise BaseStructureError(f"{name_en!r}.armour 缺失")
        if not isinstance(requirements, dict) or not requirements:
            raise BaseStructureError(f"{name_en!r}.req 缺失")

        seed = json.dumps([BODY_BASE_FILE, str(name_en)], ensure_ascii=False, separators=(",", ":"))
        base_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        if base_id in seen_ids:
            raise BaseStructureError(f"底材稳定键重复：{name_en!r}")
        seen_ids.add(base_id)

        implicit_raw = raw.get("implicit")
        if implicit_raw is not None and not isinstance(implicit_raw, str):
            raise BaseStructureError(f"{name_en!r}.implicit 不是字符串")
        implicit_mod_types = _jsonable(
            raw.get("implicitModTypes") or {}, path=f"{name_en}.implicitModTypes"
        )
        if implicit_mod_types == {}:
            implicit_mod_types = []
        if not isinstance(implicit_mod_types, list):
            raise BaseStructureError(f"{name_en!r}.implicitModTypes 不是数组")

        out.append(
            BodyArmourBase(
                base_id=base_id,
                name_en=str(name_en),
                name_zh=name_zh,
                poedb_slug=slug,
                item_class=item_class,
                subtype=subtype,
                socket_limit=socket_limit,
                tags=tags,
                influence_tags=influence_tags,
                armour_tag=armour_tag,
                modifier_page=ARMOUR_TAG_TO_PAGE[armour_tag],
                implicit_text=implicit_raw,
                implicit_mod_types=implicit_mod_types,
                implicit_ids=_string_array(
                    raw.get("implicitIds"), path=f"{name_en}.implicitIds"
                ),
                defences=defences,
                requirements=requirements,
            )
        )

    out.sort(key=lambda base: (base.armour_tag, base.name_en))
    return out
