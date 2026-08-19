"""POEDB `/cn/Currency` 抽取器：通货的名称、堆叠上限与效果原文。

## 「0 张表」不等于「抽不出来」

这一页 `<table>` 计数为 **0**，先前因此被记成「已抓取未入库」并且读起来像是抽不出内容。
实测它是**卡片布局**，内容在三个 class 里，抽出 113 条毫无问题：

    a.item_currency      中文名 + slug + `data-hover` 里的 GGG 元数据路径
    div.property         「堆叠数量: 1 / 20」
    div.explicitMod      效果原文（「用新的随机属性来重铸一个稀有物品」）

这是同一类错误的第六次（「需联网」其实缓存里就有、「缺人工核对」其实判据错、
「已排除整列」其实函数从未生效、「PoB 侧有值可回填」方向正相反、
「技能石有阻塞」中文页早就在缓存里）。**结构判断要去看 DOM，不要看表格计数。**

## 稳定键取元数据路径

`data-hover` 给的是 `Metadata/Items/Currency/CurrencyRerollRare` 这种 GGG 内部路径。
不用中文名（译名会调整）也不用 slug（那是 URL 标识）当键。

## 15 条没有效果文本，留空而不是补一句

碎片（卷轴碎片、混沌石碎片…）与命能/结晶在页面上没有效果文本。自己写一句「用于合成」
就是编造——留 NULL，并让覆盖声明说清有多少条。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import unquote

from selectolax.parser import HTMLParser

POEDB_CURRENCY_PAGE = "Currency"
POEDB_CURRENCY_URL_ZH = f"https://poedb.tw/cn/{POEDB_CURRENCY_PAGE}"
POEDB_CURRENCY_URL_EN = f"https://poedb.tw/us/{POEDB_CURRENCY_PAGE}"

#: 「堆叠数量: 1 / 20」→ 20。只取上限：前一个数是展示用的「当前数量」。
_STACK_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
_SPACE_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[一-鿿]")
#: 元数据路径。页面上偶尔带前缀（`Data\BaseItemTypes\` 等），只保留 Metadata 之后的部分。
_META_RE = re.compile(r"(Metadata/Items/[\w/]+)")

#: 另一种 class：`whiteitem Currency`（实测 1 条「神秘赛季石」，历史赛季物品）。
#: 它的卡片结构与主清单不同，本抽取器**不取**，但调用方要把条数记进 note——
#: 静默省略与「页面上就没有」在事后是分不清的。
OTHER_CLASS_SELECTOR = "div.page a.whiteitem.Currency"


class CurrencyStructureError(ValueError):
    """通货页结构与预期不符。"""


@dataclass(frozen=True)
class Currency:
    """一条通货。`effect_zh` 为 None 表示页面上没有效果文本（碎片类）。"""

    metadata_id: str
    name_zh: str
    poedb_slug: str
    item_class: str
    stack_max: int
    effect_zh: str | None
    name_en: str | None = None

    @property
    def poedb_url(self) -> str:
        return f"https://poedb.tw/cn/{self.poedb_slug}"

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [self.metadata_id, self.name_zh, self.name_en, self.poedb_slug,
             self.item_class, self.stack_max, self.effect_zh],
            ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(node) -> str:  # noqa: ANN001
    return _SPACE_RE.sub(" ", node.text(deep=True)).strip() if node is not None else ""


def _parse_cards(html: str) -> dict[str, dict[str, str | int | None]]:
    """解析一页，返回 ``元数据路径 -> 字段``。语言无关，中英文页共用。"""
    tree = HTMLParser(html)
    out: dict[str, dict[str, str | int | None]] = {}
    for link in tree.css("a.item_currency"):
        name = _text(link)
        if not name:
            # 每条通货有两个链接：图标（空文本）与名字。空的跳过
            continue
        slug = str(link.attributes.get("href") or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        # `data-hover` 有两种形态（与幽魂表同一个坑）：GGG 元数据路径，
        # 或 tooltip 缓存 URL。实测 111 条给路径、2 条只给缓存 URL
        # （「神恩之贤主钉」「未知的命运卡」）。后者退回 slug 当键——
        # 表上 slug 有唯一约束，而缓存哈希是 tooltip **内容**的键、会撞。
        matched = _META_RE.search(unquote(str(link.attributes.get("data-hover") or "")))
        metadata_id = matched.group(1) if matched else (f"slug:{slug}" if slug else "")
        if not metadata_id or metadata_id in out:
            continue
        classes = [
            c for c in str(link.attributes.get("class") or "").split()
            if c not in ("item_currency", "whiteitem")
        ]
        box = link.parent
        stack_node = box.css_first("div.property") if box is not None else None
        stack = _STACK_RE.search(_text(stack_node)) if stack_node is not None else None
        effects = [_text(m) for m in (box.css("div.explicitMod") if box is not None else [])]
        out[metadata_id] = {
            "name": name,
            "slug": slug,
            "item_class": classes[0] if classes else "",
            "stack_max": int(stack.group(2)) if stack else None,
            "effect": " / ".join(e for e in effects if e) or None,
        }
    return out


def count_other_class(html: str) -> int:
    """页面上另一种 class 的通货卡片条数（本抽取器不取，只报数）。"""
    return len({
        str(a.attributes.get("href") or "")
        for a in HTMLParser(html).css(OTHER_CLASS_SELECTOR)
        if _text(a)
    })


def extract_currencies(html_zh: str, html_en: str | None = None) -> list[Currency]:
    """抽取通货。英文页可选——取不到时 `name_en` 留 None，不由 slug 反推。

    两侧按**元数据路径**配对，而不是按名字或 slug：那是唯一在两种语言下都不变的键。
    """
    cards_zh = _parse_cards(html_zh)
    if not cards_zh:
        raise CurrencyStructureError(
            "通货页没有解析到任何 a.item_currency 卡片（页面结构可能已改）"
        )
    cards_en = _parse_cards(html_en) if html_en else {}

    out: list[Currency] = []
    for metadata_id, card in sorted(cards_zh.items()):
        name = str(card["name"])
        slug = str(card["slug"])
        stack = card["stack_max"]
        if not slug or not isinstance(stack, int):
            # 缺 slug 就没有可引用的页面，缺堆叠上限说明这张卡不是通货卡片
            raise CurrencyStructureError(
                f"通货 {name!r} 缺 slug 或堆叠上限（slug={slug!r} stack={stack!r}）"
            )
        name_en = None
        en_card = cards_en.get(metadata_id)
        if en_card:
            candidate = str(en_card["name"])
            # 英文页偶尔回落成中文名；含中日韩字符就不是英文名，宁可留空
            name_en = candidate if candidate and not _CJK_RE.search(candidate) else None
        out.append(
            Currency(
                metadata_id=metadata_id,
                name_zh=name,
                name_en=name_en,
                poedb_slug=slug,
                item_class=str(card["item_class"]),
                stack_max=stack,
                effect_zh=str(card["effect"]) if card["effect"] else None,
            )
        )
    if not _CJK_RE.search(" ".join(c.name_zh for c in out[:20])):
        raise CurrencyStructureError("通货名里没有中文，判为抓到了英文页")
    return out
