"""POEDB `/cn/Scarab` 抽取器：圣甲虫的效果、堆叠上限与地图装置放置上限。

卡片解析与通货页共用 `kb/sources/poedb_cards.py`——两页结构逐字相同，只差链接 class
与多一条「限制」属性。

## 这一页上有三个物品类，只取圣甲虫

实测 123 张卡片：**118 个 `MapFragment`（圣甲虫本体）**、4 个 `NecropolisPack`
（亡者之城的包）、1 个 `IncubatorStackable`（孵化器）。后两类不是圣甲虫，
**不入库并按类别登记**——把它们塞进圣甲虫表会让 `entity_type='scarab'` 说谎。

## 页面上的 7 张表不是圣甲虫清单

那些是**提到圣甲虫的词缀表**（地图/箱子词缀、工艺台解锁、密教成员阶级），
粒度与来源都与本表不同。本抽取器只取卡片；那几张表要不要入库是另一个决定。

## 「限制」是地图装置里的放置上限

「限制: 1」指同一次开图最多放几个同类圣甲虫，不是堆叠上限（那是「堆叠数量」那条）。
两条都存，字段名分开写清楚——混一起会让「最多能放几个」答成 20。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from kb.sources.poedb_cards import (
    CardStructureError,
    assert_chinese,
    english_name,
    parse_cards,
)

POEDB_SCARAB_PAGE = "Scarab"
POEDB_SCARAB_URL_ZH = f"https://poedb.tw/cn/{POEDB_SCARAB_PAGE}"
POEDB_SCARAB_URL_EN = f"https://poedb.tw/us/{POEDB_SCARAB_PAGE}"

#: 卡片链接选择器。三个物品类共用 `a.whiteitem`，靠 `item_class` 再筛。
SCARAB_SELECTOR = "a.whiteitem"
#: 圣甲虫的物品类。其余类别不入库。
SCARAB_ITEM_CLASS = "MapFragment"

ScarabStructureError = CardStructureError


@dataclass(frozen=True)
class Scarab:
    """一个圣甲虫。`device_limit` 是地图装置里的放置上限，与堆叠上限是两回事。"""

    metadata_id: str
    name_zh: str
    poedb_slug: str
    stack_max: int
    device_limit: int | None
    effect_zh: str | None
    name_en: str | None = None

    @property
    def poedb_url(self) -> str:
        return f"https://poedb.tw/cn/{self.poedb_slug}"

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [self.metadata_id, self.name_zh, self.name_en, self.poedb_slug,
             self.stack_max, self.device_limit, self.effect_zh],
            ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkippedCard:
    """页面上不属于圣甲虫的卡片。调用方登记，不静默丢弃。"""

    name: str
    item_class: str


def extract_scarabs(
    html_zh: str, html_en: str | None = None
) -> tuple[list[Scarab], list[SkippedCard]]:
    """抽取圣甲虫，返回 ``(圣甲虫, 按物品类跳过的卡片)``。"""
    cards_zh = parse_cards(html_zh, SCARAB_SELECTOR)
    if not cards_zh:
        raise CardStructureError(
            "圣甲虫页没有解析到任何 a.whiteitem 卡片（页面结构可能已改）"
        )
    cards_en = parse_cards(html_en, SCARAB_SELECTOR) if html_en else {}

    out: list[Scarab] = []
    skipped: list[SkippedCard] = []
    for key, card in sorted(cards_zh.items()):
        if card.item_class != SCARAB_ITEM_CLASS:
            skipped.append(SkippedCard(name=card.name, item_class=card.item_class))
            continue
        stack = card.stack_max
        if not card.slug or stack is None:
            raise CardStructureError(
                f"圣甲虫 {card.name!r} 缺 slug 或堆叠上限"
                f"（slug={card.slug!r} 属性={card.properties!r}）"
            )
        out.append(
            Scarab(
                metadata_id=key,
                name_zh=card.name,
                name_en=english_name(cards_en, key),
                poedb_slug=card.slug,
                stack_max=stack,
                device_limit=card.device_limit,
                effect_zh=card.effect,
            )
        )
    if not out:
        raise CardStructureError(
            f"圣甲虫页上没有 {SCARAB_ITEM_CLASS} 类卡片（物品类可能已改名）"
        )
    assert_chinese([s.name_zh for s in out], what="圣甲虫名")
    return out, skipped
