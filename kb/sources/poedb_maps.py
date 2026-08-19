"""POEDB `/cn/Atlas_of_Worlds` 抽取器：地图的等阶、头目、命运卡与图鉴连接。

## 三页里只有这一页有实质内容

`地图与地图词缀` 这个域挂了三页（`Maps` / `Atlas` / `Atlas_of_Worlds`），实测：

===================  =====================================================
`Maps`（180KB）      主表是「地图碎片 → 区域」，区域名是**未翻译的英文 id**
                     （`AnyCorruptedSideArea`）；另有 12 条只有内部 stat id 的
                     地图隐式词缀。价值薄，本模块不取
`Atlas`（85KB）      只有 1 张中文卡片（章节奖励说明）
`Atlas_of_Worlds`    **164 张卡片**，中文齐全：名字 / 等阶 / 额外目标 / 头目 /
（477KB）            命运卡 / 工艺台配方 / **Link（图鉴上连着哪些图）**
===================  =====================================================

**结构要一页一页看。** 同一个域下的三页，内容密度差了两个数量级。

## 名字要从「第一个非 property 的子节点」取

卡片结构是 `div.flex-grow-1 > div(名字) + div.property × N`。用 `css_first('div')`
会匹配到外层包裹节点、把整张卡的文本当成名字（实测踩过），所以按**直接子节点**遍历、
遇到第一个 `property` 就停。

## 等阶可空

实测 159 张卡里只有 **110** 张给了等阶（梦魇/传奇等特殊地图没给）。补 0 会让
「这张图几阶」答成 0 阶，所以留空。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

POEDB_MAPS_PAGE = "Atlas_of_Worlds"
POEDB_MAPS_URL = f"https://poedb.tw/cn/{POEDB_MAPS_PAGE}"

_SPACE_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[一-鿿]")
_KV_RE = re.compile(r"[:：]")
#: 名字长度上限。卡片里偶尔有长段说明文本，超过这个长度的不是地图名。
MAX_NAME_LEN = 18


class MapStructureError(ValueError):
    """地图页结构与预期不符。"""


@dataclass(frozen=True)
class MapEntry:
    """一张地图。`tier` 为 None 表示页面没给等阶（梦魇/传奇等特殊地图）。"""

    name_zh: str
    tier: int | None
    bonus_objective: str | None
    bosses: tuple[str, ...] = ()
    divination_cards: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    bench_recipe: str | None = None
    atlas_points: str | None = None

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [self.name_zh, self.tier, self.bonus_objective, self.bosses,
             self.divination_cards, self.links, self.bench_recipe, self.atlas_points],
            ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class MapExtract:
    maps: list[MapEntry] = field(default_factory=list)
    #: 页面上出现过但被判为「不是地图卡」的卡片数，只报数
    skipped_cards: int = 0


def _text(node) -> str:  # noqa: ANN001
    return _SPACE_RE.sub(" ", node.text(deep=True)).strip() if node is not None else ""


def _card_name(body) -> str:  # noqa: ANN001
    """按**直接子节点**找名字，遇到第一个 property 就停。"""
    for child in body.iter(include_text=False):
        if "property" in str(child.attributes.get("class") or ""):
            break
        text = _text(child)
        if text:
            return text
    return ""


def _link_names(prop, selector: str = "a") -> tuple[str, ...]:  # noqa: ANN001
    return tuple(dict.fromkeys(
        _text(a) for a in prop.css(selector) if _text(a)
    ))


def extract_maps(html: str) -> MapExtract:
    """抽取地图卡片。一条中文地图都没有时整批拒绝。"""
    tree = HTMLParser(html)
    out = MapExtract()
    seen: set[str] = set()
    for card in tree.css("div.d-flex"):
        body = card.css_first("div.flex-grow-1")
        if body is None:
            continue
        props = body.css("div.property")
        if not props:
            continue
        name = _card_name(body)
        if not name or not _CJK_RE.search(name) or len(name) > MAX_NAME_LEN:
            out.skipped_cards += 1
            continue
        if name in seen:
            continue

        tier: int | None = None
        bonus = recipe = points = None
        bosses: tuple[str, ...] = ()
        cards: tuple[str, ...] = ()
        links: tuple[str, ...] = ()
        for prop in props:
            raw = _text(prop)
            if not _KV_RE.search(raw):
                continue
            label, value = (part.strip() for part in _KV_RE.split(raw, 1))
            if label == "等阶":
                tier = int(value) if value.isdigit() else None
            elif label == "额外目标":
                bonus = value or None
            elif label.startswith("Boss"):
                bosses = _link_names(prop)
            elif label == "Card":
                cards = _link_names(prop, "a.divination")
            elif label == "Link":
                links = _link_names(prop)
            elif label == "工艺台配方":
                recipe = value or None
            elif "异界天赋点" in label:
                points = f"{label}：{value}" if value else label
        seen.add(name)
        out.maps.append(
            MapEntry(
                name_zh=name, tier=tier, bonus_objective=bonus, bosses=bosses,
                divination_cards=cards, links=links, bench_recipe=recipe,
                atlas_points=points,
            )
        )

    if not out.maps:
        raise MapStructureError("地图页没有解析到任何中文地图卡片（页面结构可能已改）")
    if not _CJK_RE.search(" ".join(m.name_zh for m in out.maps[:20])):
        raise MapStructureError("地图名里没有中文，判为抓到了英文页")
    return out
