"""POEDB 卡片式物品页的**共用解析**（通货、圣甲虫，将来还会有别的）。

## 为什么单独一个模块

通货页与圣甲虫页的卡片结构**逐字相同**，只差两处：链接的 class（`item_currency`
vs `whiteitem MapFragment`）与属性条数（圣甲虫多一条「限制」）。写第二个近似抽取器
就是规约第 3 条点名的那种补丁；所以解析放这一处，各页只提供自己的配置。

    a.<selector>       中文名 + slug + `data-hover` 里的 GGG 元数据路径
    div.property       「堆叠数量: 1 / 20」「限制: 1」——按 `键: 值` 逐条取
    div.explicitMod    效果原文，多条用 ` / ` 连接

## 两种 `data-hover` 形态

多数给 `Metadata/Items/...` 元数据路径，少数只给 tooltip 缓存 URL
（通货 2 条、圣甲虫 4 条）。缓存哈希是 tooltip **内容**的键、会撞（幽魂那轮撞过 7 例），
所以退化键用 slug——两张表上 slug 都有唯一约束。

## 「0 张表」不等于「抽不出来」

这两页 `<table>` 计数分别是 0 与 7，而内容都在卡片里。**结构判断要去看 DOM。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import unquote

from selectolax.parser import HTMLParser

_SPACE_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[一-鿿]")
_META_RE = re.compile(r"(Metadata/Items/[\w/]+)")
#: 「堆叠数量: 1 / 20」→ 20。只取上限：前一个数是展示用的当前数量。
_STACK_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
_PROP_SPLIT_RE = re.compile(r"[:：]", re.UNICODE)

PROP_STACK = "堆叠数量"
PROP_LIMIT = "限制"


class CardStructureError(ValueError):
    """卡片页结构与预期不符。"""


@dataclass(frozen=True)
class ItemCard:
    """一张卡片。`properties` 保留页面原始的 `键: 值`，专用字段由调用方从中取。"""

    key: str
    name: str
    slug: str
    item_class: str
    properties: dict[str, str] = field(default_factory=dict)
    effect: str | None = None

    @property
    def stack_max(self) -> int | None:
        matched = _STACK_RE.search(self.properties.get(PROP_STACK, ""))
        return int(matched.group(2)) if matched else None

    @property
    def device_limit(self) -> int | None:
        """地图装置里的放置上限（圣甲虫才有）。"""
        raw = self.properties.get(PROP_LIMIT, "").strip()
        return int(raw) if raw.isdigit() else None


def text_of(node) -> str:  # noqa: ANN001
    return _SPACE_RE.sub(" ", node.text(deep=True)).strip() if node is not None else ""


def parse_cards(
    html: str, selector: str, *, known_keys_by_slug: Mapping[str, str] | None = None
) -> dict[str, ItemCard]:
    """解析一页卡片，返回 ``键 -> ItemCard``。语言无关，中英文页共用。"""
    tree = HTMLParser(html)
    out: dict[str, ItemCard] = {}
    for link in tree.css(selector):
        name = text_of(link)
        if not name:
            # 每个物品有两个链接：图标（空文本）与名字。空的跳过
            continue
        slug = str(link.attributes.get("href") or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        matched = _META_RE.search(unquote(str(link.attributes.get("data-hover") or "")))
        known_key = (known_keys_by_slug or {}).get(slug)
        if matched and known_key and matched.group(1) != known_key:
            raise CardStructureError(
                f"{slug} 的页面元数据路径与已入库身份不一致，拒绝自动替换"
            )
        # POEDB may replace a metadata hover with an opaque tooltip URL.
        # The slug is stable on the same site, so retain an already verified
        # metadata ID instead of turning the whole snapshot into new items.
        key = matched.group(1) if matched else (known_key or (f"slug:{slug}" if slug else ""))
        if not key or key in out:
            continue
        classes = [
            c for c in str(link.attributes.get("class") or "").split()
            if c not in ("item_currency", "whiteitem")
        ]
        box = link.parent
        properties: dict[str, str] = {}
        for prop in (box.css("div.property") if box is not None else []):
            raw = text_of(prop)
            if _PROP_SPLIT_RE.search(raw):
                label, value = _PROP_SPLIT_RE.split(raw, 1)
                properties[label.strip()] = value.strip()
        effects = [text_of(m) for m in (box.css("div.explicitMod") if box is not None else [])]
        out[key] = ItemCard(
            key=key,
            name=name,
            slug=slug,
            item_class=classes[0] if classes else "",
            properties=properties,
            effect=" / ".join(e for e in effects if e) or None,
        )
    return out


def english_name(en_cards: dict[str, ItemCard], key: str) -> str | None:
    """按**元数据路径**取英文名。取不到或回落成中文时返回 None。

    绝不由 slug 反推（与 `entity.name_en` 同一条规则）：slug 是 URL 标识，
    不是权威英文名。
    """
    card = en_cards.get(key)
    if card is None or not card.name:
        return None
    return card.name if not _CJK_RE.search(card.name) else None


def assert_chinese(names: list[str], *, what: str) -> None:
    """抓到英文页时整批拒绝——入库一堆英文名比失败更难发现。"""
    if names and not _CJK_RE.search(" ".join(names[:20])):
        raise CardStructureError(f"{what}里没有中文，判为抓到了英文页")


def count_links(html: str, selector: str) -> int:
    """某个选择器下带名字的链接去重条数。用于「本抽取器不取但要报数」的那些。"""
    return len({
        str(a.attributes.get("href") or "")
        for a in HTMLParser(html).css(selector)
        if text_of(a)
    })
