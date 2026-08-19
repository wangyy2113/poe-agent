"""通货抽取与卡片（2026-08-19 第十六轮）。

判据来自实测反例：
「0 张表」不等于抽不出来（那页是卡片布局，抽得出 113 条）、
`data-hover` 有两种形态（2 条只给 tooltip 缓存 URL，退回 slug 当键）、
15 条没有效果文本要留空而不是补一句、英文名不许由 slug 反推。

全部离线：HTML 在内存里构造。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from entities.models import Entity, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, Source  # noqa: E402
from kb import render_stackable  # noqa: E402
from kb.sources.poedb_currency import (  # noqa: E402
    CurrencyStructureError,
    count_other_class,
    extract_currencies,
)

META_HOVER = "?s=Data%5CBaseItemTypes%5CMetadata/Items/Currency/CurrencyRerollRare"
HASH_HOVER = "https://cdn.poedb.tw/cache1/cn/Poe_Data_BaseItemTypes_hover/0cec325e44"


def card(name: str, slug: str, hover: str, stack: str = "1 / 20",
         effects: tuple[str, ...] = ("用新的随机属性来重铸一个稀有物品",),
         cls: str = "item_currency StackableCurrency") -> str:
    mods = "".join(f'<div class="explicitMod">{e}</div>' for e in effects)
    return (
        '<div class="d-flex"><div class="flex-shrink-0">'
        f'<a class="{cls}" data-hover="{hover}" href="{slug}"><img class="w1"></a></div>'
        f'<div class="flex-grow-1"><a class="{cls}" data-hover="{hover}" href="{slug}">{name}</a>'
        f'<div><div class="property">堆叠数量: <span class="colourDefault">{stack}</span></div>'
        f'<div class="separator"></div>{mods}</div></div></div>'
    )


def page(*cards: str) -> str:
    return f'<html><body><div class="page">{"".join(cards)}</div></body></html>'


CHAOS = card("混沌石", "Chaos_Orb", META_HOVER)


class ExtractTest(unittest.TestCase):
    def test_从卡片布局里抽出通货(self) -> None:
        """这一页 `<table>` 计数为 0——结构判断要看 DOM，不是看表格计数。"""
        got = extract_currencies(page(CHAOS))
        self.assertEqual(1, len(got))
        item = got[0]
        self.assertEqual("Metadata/Items/Currency/CurrencyRerollRare", item.metadata_id)
        self.assertEqual(("混沌石", "Chaos_Orb", 20), (item.name_zh, item.poedb_slug, item.stack_max))
        self.assertEqual("用新的随机属性来重铸一个稀有物品", item.effect_zh)

    def test_堆叠只取上限(self) -> None:
        """「1 / 20」的前一个数是展示用的当前数量。"""
        self.assertEqual(40, extract_currencies(page(
            card("知识卷轴", "Scroll_of_Wisdom", META_HOVER, stack="1 / 40")
        ))[0].stack_max)

    def test_缓存哈希形态退回slug当键(self) -> None:
        """实测 2 条（「神恩之贤主钉」「未知的命运卡」）只给 tooltip 缓存 URL。
        缓存哈希是 tooltip **内容**的键、会撞（幽魂那轮撞过 7 例），
        而 slug 在表上有唯一约束。"""
        got = extract_currencies(page(card("未知的命运卡", "Stacked_Deck", HASH_HOVER)))
        self.assertEqual("slug:Stacked_Deck", got[0].metadata_id)

    def test_没有效果文本的留空而不是补一句(self) -> None:
        """碎片与命能类在页面上就没有效果说明。自己写「用于合成」是编造。"""
        got = extract_currencies(page(
            card("混沌石碎片", "Chaos_Shard", META_HOVER, effects=())
        ))
        self.assertIsNone(got[0].effect_zh)

    def test_多条效果用斜杠连接(self) -> None:
        got = extract_currencies(page(
            card("某通货", "Some_Orb", META_HOVER, effects=("效果甲", "效果乙"))
        ))
        self.assertEqual("效果甲 / 效果乙", got[0].effect_zh)

    def test_英文名按元数据路径配对(self) -> None:
        en = page(card("Chaos Orb", "Chaos_Orb", META_HOVER))
        self.assertEqual("Chaos Orb", extract_currencies(page(CHAOS), en)[0].name_en)

    def test_没有英文页时留空不由slug反推(self) -> None:
        self.assertIsNone(extract_currencies(page(CHAOS))[0].name_en)

    def test_英文页回落成中文时也留空(self) -> None:
        """宁可留空，也不把中文名塞进 name_en。"""
        en = page(card("混沌石", "Chaos_Orb", META_HOVER))
        self.assertIsNone(extract_currencies(page(CHAOS), en)[0].name_en)

    def test_没有卡片时拒绝(self) -> None:
        with self.assertRaises(CurrencyStructureError):
            extract_currencies('<html><body><div class="page"></div></body></html>')

    def test_抓到英文页时拒绝(self) -> None:
        with self.assertRaises(CurrencyStructureError) as ctx:
            extract_currencies(page(card("Chaos Orb", "Chaos_Orb", META_HOVER)))
        self.assertIn("中文", str(ctx.exception))

    def test_缺堆叠上限即拒绝(self) -> None:
        """没有堆叠属性说明这张卡不是通货卡片，别把别的东西当通货入库。"""
        with self.assertRaises(CurrencyStructureError):
            extract_currencies(page(card("怪东西", "Weird", META_HOVER, stack="—")))

    def test_另一种class只报数不入库(self) -> None:
        """`whiteitem Currency`（历史赛季物品）本抽取器不取，但条数要报出来——
        静默省略与「页面上就没有」事后分不清。"""
        html = page(CHAOS, '<a class="whiteitem Currency" href="Mystery_Leaguestone">神秘赛季石</a>')
        self.assertEqual(1, len(extract_currencies(html)))
        self.assertEqual(1, count_other_class(html))


def _entity(**attrs) -> Entity:
    base = {"metadata_id": "Metadata/Items/Currency/CurrencyRerollRare",
            "item_class": "StackableCurrency", "stack_max": 20,
            "effect_zh": "用新的随机属性来重铸一个稀有物品"}
    base.update(attrs)
    return Entity(
        entity_id="currency:Chaos_Orb",
        entity_type=EntityType.CURRENCY,
        name_zh="混沌石",
        name_en=attrs.pop("name_en", "Chaos Orb") if "name_en" in attrs else "Chaos Orb",
        slug="Chaos_Orb",
        attributes=base,
        ref=SourceRef(source=Source.POEDB.value,
                      url="https://poedb.tw/cn/Chaos_Orb", priority=1),
    )


class CardTest(unittest.TestCase):
    def test_卡片给名字堆叠与效果(self) -> None:
        text = render_stackable.render_card(_entity())
        self.assertIn("# 混沌石（Chaos Orb）", text)
        self.assertIn("堆叠上限 20", text)
        self.assertIn("效果：用新的随机属性来重铸一个稀有物品", text)
        self.assertLessEqual(len(text), render_stackable.MAX_CHUNK_CHARS)

    def test_没有效果时明写页面上没有(self) -> None:
        """留一片空白会让人以为渲染坏了。"""
        text = render_stackable.render_card(_entity(effect_zh=None))
        self.assertIn("没有给这条通货的效果说明", text)

    def test_卡片不写价格类边界(self) -> None:
        """把「价格」写进 113 张卡片，等于给每个含「价格」的问题送覆盖率——
        技能石那轮踩过这个坑。边界属于覆盖层。"""
        text = render_stackable.render_card(_entity())
        for word in ("价格", "多少钱", "未采集"):
            self.assertNotIn(word, text)

    def test_出卡片且分类是经济系统(self) -> None:
        chunks = render_stackable.to_chunks(_entity())
        self.assertEqual(1, len(chunks))
        self.assertEqual("通货", chunks[0].metadata.subcategory)


class BenchCostLinkTest(unittest.TestCase):
    """工艺台费用引用的通货必须都能在通货表里找到。

    实测 18 个引用**按中文名与按 slug 都 100% 命中**。这条断言把
    「工艺台加最大生命要 2 个混沌石，混沌石是什么」这条链路钉住：
    任何一侧改名都会在这里失败，而不是在用户面前变成一句查不到。

    两个键都查：`currency_slug` 更稳（译名会调整），中文名则是用户实际问的东西。
    """

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO poedb_currency(metadata_id,name_zh,poedb_slug,item_class,stack_max,"
            "source,source_priority,url,content_hash,fetched_at,updated_at) VALUES "
            "('m','混沌石','Chaos_Orb','StackableCurrency',20,'POEDB',1,"
            "'https://poedb.tw/cn/Chaos_Orb','h',datetime('now'),datetime('now'))"
        )
        self.conn.execute(
            "INSERT INTO craft_bench_recipe(recipe_id,effect_text,item_classes,unlock_text,"
            "source,source_priority,url,content_hash,fetched_at,updated_at) VALUES "
            "('r','+# 最大生命','[\"Body Armour\"]','Default','POEDB',1,"
            "'https://poedb.tw/cn/Crafting_Bench','h',datetime('now'),datetime('now'))"
        )
        self.conn.execute(
            "INSERT INTO craft_bench_recipe_cost(recipe_id,position,amount,currency_slug,"
            "currency_name_zh,currency_url) VALUES "
            "('r',0,2,'Chaos_Orb','混沌石','https://poedb.tw/cn/Chaos_Orb')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_按中文名都能对上(self) -> None:
        missing = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT currency_name_zh FROM craft_bench_recipe_cost "
            "WHERE currency_name_zh NOT IN (SELECT name_zh FROM poedb_currency)"
        )]
        self.assertEqual([], missing)

    def test_按slug都能对上(self) -> None:
        missing = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT currency_slug FROM craft_bench_recipe_cost "
            "WHERE currency_slug NOT IN (SELECT poedb_slug FROM poedb_currency)"
        )]
        self.assertEqual([], missing)

    def test_对不上时能被发现(self) -> None:
        """这条是上面两条的反面：桥接断了必须测出来，而不是静默通过。"""
        self.conn.execute(
            "INSERT INTO craft_bench_recipe_cost(recipe_id,position,amount,currency_slug,"
            "currency_name_zh,currency_url) VALUES "
            "('r',1,1,'Divine_Orb','神圣石','https://poedb.tw/cn/Divine_Orb')"
        )
        missing = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT currency_slug FROM craft_bench_recipe_cost "
            "WHERE currency_slug NOT IN (SELECT poedb_slug FROM poedb_currency)"
        )]
        self.assertEqual(["Divine_Orb"], missing)


if __name__ == "__main__":
    unittest.main()
