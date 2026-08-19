"""圣甲虫抽取与共用卡片解析（2026-08-19 第十七轮）。

判据来自实测：
这一页有**三个物品类**（118 圣甲虫 + 4 亡者之城包 + 1 孵化器），只收圣甲虫；
「限制」是地图装置放置上限，与堆叠上限是两回事（混一起会把「最多放几个」答成 20）；
4 条只给 tooltip 缓存 URL 的退回 slug 当键。

卡片解析与通货页共用 `kb/sources/poedb_cards.py`——两页结构逐字相同。
这个文件里也直接测那个共用模块，避免它只被间接覆盖。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from entities.models import Entity, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, Source  # noqa: E402
from kb import render_stackable  # noqa: E402
from kb.sources.poedb_cards import (  # noqa: E402
    CardStructureError,
    english_name,
    parse_cards,
)
from kb.sources.poedb_scarab import SCARAB_SELECTOR, extract_scarabs  # noqa: E402

META = "?s=Data%5CBaseItemTypes%2FMetadata%2FItems%2FScarabs%2FScarabBreachNew1"
META2 = "?s=Data%5CBaseItemTypes%2FMetadata%2FItems%2FScarabs%2FScarabBreachNew3"
HASH_HOVER = "https://cdn.poedb.tw/cache1/cn/Poe_Data_BaseItemTypes_hover/abc12345"


def card(name: str, slug: str, hover: str, *, cls: str = "whiteitem MapFragment",
         stack: str = "1 / 20", limit: str | None = "1",
         effects: tuple[str, ...] = ("该区域包含一个裂隙巢穴",)) -> str:
    props = f'<div class="property">堆叠数量: <span>{stack}</span></div>'
    if limit is not None:
        props += f'<div class="property">限制: <span>{limit}</span></div>'
    mods = "".join(f'<div class="explicitMod">{e}</div>' for e in effects)
    return (
        '<div class="d-flex"><div class="flex-shrink-0">'
        f'<a class="{cls}" data-hover="{hover}" href="{slug}"><img></a></div>'
        f'<div class="flex-grow-1"><a class="{cls}" data-hover="{hover}" href="{slug}">{name}</a>'
        f'<div>{props}<div class="separator"></div>{mods}</div></div></div>'
    )


def page(*cards: str) -> str:
    return f'<html><body><div class="page">{"".join(cards)}</div></body></html>'


HIVE = card("虫群之裂隙圣甲虫", "Breach_Scarab_of_the_Hive", META)


class SharedCardParserTest(unittest.TestCase):
    """共用解析：属性按「键: 值」逐条取，双形态键，效果多条连接。"""

    def test_属性逐条取且堆叠与限制分开(self) -> None:
        cards = parse_cards(page(card("甲", "A", META, stack="1 / 10", limit="3")),
                            SCARAB_SELECTOR)
        item = next(iter(cards.values()))
        self.assertEqual({"堆叠数量": "1 / 10", "限制": "3"}, item.properties)
        self.assertEqual(10, item.stack_max)
        self.assertEqual(3, item.device_limit)

    def test_没有限制属性时为None而不是0(self) -> None:
        """0 会被读成「一个都不能放」，而真实含义是「这一页没给这个字段」。"""
        cards = parse_cards(page(card("甲", "A", META, limit=None)), SCARAB_SELECTOR)
        self.assertIsNone(next(iter(cards.values())).device_limit)

    def test_缓存哈希形态退回slug(self) -> None:
        cards = parse_cards(page(card("甲", "Some_Slug", HASH_HOVER)), SCARAB_SELECTOR)
        self.assertIn("slug:Some_Slug", cards)

    def test_多条效果用斜杠连接(self) -> None:
        cards = parse_cards(page(card("甲", "A", META, effects=("效果甲", "效果乙"))),
                            SCARAB_SELECTOR)
        self.assertEqual("效果甲 / 效果乙", next(iter(cards.values())).effect)

    def test_英文名按键配对且不由slug反推(self) -> None:
        zh = parse_cards(page(HIVE), SCARAB_SELECTOR)
        en = parse_cards(page(card("Breach Scarab of the Hive",
                                   "Breach_Scarab_of_the_Hive", META)), SCARAB_SELECTOR)
        key = next(iter(zh))
        self.assertEqual("Breach Scarab of the Hive", english_name(en, key))
        self.assertIsNone(english_name({}, key))

    def test_英文页回落成中文时留空(self) -> None:
        zh = parse_cards(page(HIVE), SCARAB_SELECTOR)
        en = parse_cards(page(HIVE), SCARAB_SELECTOR)
        self.assertIsNone(english_name(en, next(iter(zh))))


class ScarabExtractTest(unittest.TestCase):
    def test_只收MapFragment其余按物品类跳过(self) -> None:
        """实测页面上还有 4 条亡者之城包与 1 条孵化器。把它们塞进圣甲虫表
        会让 `entity_type='scarab'` 说谎。"""
        html = page(
            HIVE,
            card("迷踪孕育石", "Stasis_Prison", META2, cls="whiteitem IncubatorStackable"),
            card("辛迪加刺客之不灭余烬", "Ember", HASH_HOVER, cls="whiteitem NecropolisPack"),
        )
        scarabs, skipped = extract_scarabs(html)
        self.assertEqual(["虫群之裂隙圣甲虫"], [s.name_zh for s in scarabs])
        self.assertEqual(
            {("迷踪孕育石", "IncubatorStackable"), ("辛迪加刺客之不灭余烬", "NecropolisPack")},
            {(s.name, s.item_class) for s in skipped},
        )

    def test_放置上限与堆叠上限分开存(self) -> None:
        scarabs, _ = extract_scarabs(page(card("深渊圣甲虫", "Abyss_Scarab", META,
                                               stack="1 / 20", limit="5")))
        self.assertEqual((20, 5), (scarabs[0].stack_max, scarabs[0].device_limit))

    def test_没有圣甲虫类卡片时拒绝(self) -> None:
        """物品类改名会让这张表悄悄变空，那种错必须在入库时暴露。"""
        html = page(card("孵化器", "Inc", META, cls="whiteitem IncubatorStackable"))
        with self.assertRaises(CardStructureError) as ctx:
            extract_scarabs(html)
        self.assertIn("MapFragment", str(ctx.exception))

    def test_抓到英文页时拒绝(self) -> None:
        with self.assertRaises(CardStructureError):
            extract_scarabs(page(card("Abyss Scarab", "Abyss_Scarab", META)))

    def test_缺堆叠上限即拒绝(self) -> None:
        with self.assertRaises(CardStructureError):
            extract_scarabs(page(card("怪东西", "Weird", META, stack="—")))


def _entity(entity_type: EntityType, **attrs) -> Entity:
    base = {"metadata_id": "Metadata/Items/Scarabs/ScarabAbyss1",
            "stack_max": 20, "device_limit": 5,
            "effect_zh": "区域内包含一个额外的深渊"}
    base.update(attrs)
    return Entity(
        entity_id=f"{entity_type.value}:Abyss_Scarab",
        entity_type=entity_type,
        name_zh="深渊圣甲虫",
        name_en="Abyss Scarab",
        slug="Abyss_Scarab",
        attributes=base,
        ref=SourceRef(source=Source.POEDB.value,
                      url="https://poedb.tw/cn/Abyss_Scarab", priority=1),
    )


class ScarabCardTest(unittest.TestCase):
    """卡片渲染与通货共用一处（措辞必须一致，否则出口的数值回填会两头对不上）。"""

    def test_卡片写出类型堆叠与放置上限(self) -> None:
        text = render_stackable.render_card(_entity(EntityType.SCARAB))
        self.assertIn("类型：圣甲虫", text)
        self.assertIn("堆叠上限 20", text)
        self.assertIn("地图装置放置上限 5", text)
        self.assertIn("效果：区域内包含一个额外的深渊", text)

    def test_通货卡片不出现放置上限(self) -> None:
        """通货没有这个字段，渲染里也不该凭空多一行。"""
        text = render_stackable.render_card(
            _entity(EntityType.CURRENCY, device_limit=None)
        )
        self.assertIn("类型：通货", text)
        self.assertNotIn("放置上限", text)

    def test_二级分类按实体类型分开(self) -> None:
        self.assertEqual(
            "圣甲虫",
            render_stackable.build_metadata(_entity(EntityType.SCARAB)).subcategory,
        )
        self.assertEqual(
            "通货",
            render_stackable.build_metadata(_entity(EntityType.CURRENCY)).subcategory,
        )


if __name__ == "__main__":
    unittest.main()
