"""POEDB `/cn/Currency` 抽取器：通货的名称、堆叠上限与效果原文。

卡片解析在 `kb/sources/poedb_cards.py`（与圣甲虫页共用一处实现，两页结构逐字相同）。
本模块只提供通货这一页的配置与字段口径。

## 「0 张表」不等于「抽不出来」

这一页 `<table>` 计数为 **0**，先前因此被记成「已抓取未入库」并且读起来像是抽不出内容。
实测它是**卡片布局**，抽出 113 条毫无问题。这是同一类错误的第六次（「需联网」其实缓存里
就有、「缺人工核对」其实判据错、「已排除整列」其实函数从未生效、「PoB 侧有值可回填」
方向正相反、「技能石有阻塞」中文页早就在缓存里）。**结构判断要去看 DOM，不要看表格计数。**

## 15 条没有效果文本，留空而不是补一句

碎片（卷轴碎片、混沌石碎片…）与命能/结晶在页面上没有效果文本。自己写一句「用于合成」
就是编造——留 NULL，并让覆盖声明说清有多少条。
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping
from dataclasses import dataclass

from kb.sources.poedb_cards import (
    CardStructureError,
    assert_chinese,
    count_links,
    english_name,
    parse_cards,
)

POEDB_CURRENCY_PAGE = "Currency"
POEDB_CURRENCY_URL_ZH = f"https://poedb.tw/cn/{POEDB_CURRENCY_PAGE}"
POEDB_CURRENCY_URL_EN = f"https://poedb.tw/us/{POEDB_CURRENCY_PAGE}"

#: 通货卡片的链接选择器。
CURRENCY_SELECTOR = "a.item_currency"
#: 另一种 class：`whiteitem Currency`（实测 1 条「神秘赛季石」，历史赛季物品）。
#: 它的卡片结构与主清单不同，本抽取器**不取**，但调用方要把条数记进 note——
#: 静默省略与「页面上就没有」在事后是分不清的。
OTHER_CLASS_SELECTOR = "div.page a.whiteitem.Currency"

#: 兼容旧引用：结构错误类型在共用模块里定义。
CurrencyStructureError = CardStructureError


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


def count_other_class(html: str) -> int:
    """页面上另一种 class 的通货卡片条数（本抽取器不取，只报数）。"""
    return count_links(html, OTHER_CLASS_SELECTOR)


def extract_currencies(
    html_zh: str, html_en: str | None = None, *,
    known_keys_by_slug: Mapping[str, str] | None = None,
) -> list[Currency]:
    """抽取通货。英文页可选——取不到时 `name_en` 留 None，不由 slug 反推。"""
    cards_zh = parse_cards(html_zh, CURRENCY_SELECTOR, known_keys_by_slug=known_keys_by_slug)
    if not cards_zh:
        raise CardStructureError(
            "通货页没有解析到任何 a.item_currency 卡片（页面结构可能已改）"
        )
    cards_en = parse_cards(
        html_en, CURRENCY_SELECTOR, known_keys_by_slug=known_keys_by_slug
    ) if html_en else {}

    out: list[Currency] = []
    for key, card in sorted(cards_zh.items()):
        stack = card.stack_max
        if not card.slug or stack is None:
            # 缺 slug 就没有可引用的页面，缺堆叠上限说明这张卡不是通货卡片
            raise CardStructureError(
                f"通货 {card.name!r} 缺 slug 或堆叠上限"
                f"（slug={card.slug!r} 属性={card.properties!r}）"
            )
        out.append(
            Currency(
                metadata_id=key,
                name_zh=card.name,
                name_en=english_name(cards_en, key),
                poedb_slug=card.slug,
                item_class=card.item_class,
                stack_max=stack,
                effect_zh=card.effect,
            )
        )
    assert_chinese([c.name_zh for c in out], what="通货名")
    return out
