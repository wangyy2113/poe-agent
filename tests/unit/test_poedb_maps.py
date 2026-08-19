"""地图抽取与卡片（2026-08-19 第十九轮）。

判据来自实测：
同一个域挂的三页内容密度差两个数量级（只有 Atlas_of_Worlds 有实质内容）；
名字要按**直接子节点**取（`css_first('div')` 会把整张卡的文本当成名字，踩过）；
等阶只有 110/159 张有，可空——补 0 会把「这张图几阶」答成 0 阶。

全部离线：HTML 在内存里构造。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from entities.models import Entity, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, Source  # noqa: E402
from kb import render_maps  # noqa: E402
from kb.sources.poedb_maps import MapStructureError, extract_maps  # noqa: E402


def card(name: str, props: str) -> str:
    return (
        '<div class="d-flex"><div class="flex-shrink-0"><img></div>'
        f'<div class="flex-grow-1 ms-2"><div class="">{name}</div>{props}</div></div>'
    )


def prop(label: str, value: str) -> str:
    return f'<div class="property">{label}:<span class="colourDefault">{value}</span></div>'


def links(label: str, names: list[str], cls: str = "") -> str:
    body = ", ".join(f'<a class="{cls}" href="/cn/{n}">{n}</a>' for n in names)
    return f'<div class="property">{label}: <span class="colourDefault">{body}</span></div>'


SHRINE = card("奇术秘殿", "".join([
    prop("等阶", "5"),
    prop("额外目标", "在 魔法 或更稀有版本的该地图内击败头目"),
    links("Boss Fights", ["天雷之使派蒂", "天雷之使派蒂"]),
    links("Card", ["薇妮雅的信物", "大奇术师"], cls="divination DivinationCard"),
    links("Link", ["中转码头", "沉沦之城"]),
    prop("工艺台配方", "阻灵术 - 等级 1"),
]))


def page(*cards: str) -> str:
    return f'<html><body><div class="page">{"".join(cards)}</div></body></html>'


class ExtractTest(unittest.TestCase):
    def test_抽出等阶头目命运卡与连接(self) -> None:
        result = extract_maps(page(SHRINE))
        self.assertEqual(1, len(result.maps))
        item = result.maps[0]
        self.assertEqual(("奇术秘殿", 5), (item.name_zh, item.tier))
        self.assertEqual(("天雷之使派蒂",), item.bosses)  # 重复的头目去重
        self.assertEqual(("薇妮雅的信物", "大奇术师"), item.divination_cards)
        self.assertEqual(("中转码头", "沉沦之城"), item.links)
        self.assertEqual("阻灵术 - 等级 1", item.bench_recipe)
        self.assertIn("击败头目", item.bonus_objective or "")

    def test_没有等阶时留空而不是补零(self) -> None:
        """实测 159 张里只有 110 张有等阶。补 0 会把「这张图几阶」答成 0 阶。"""
        item = extract_maps(page(card("梦魇之地", links("Link", ["某图"])))).maps[0]
        self.assertIsNone(item.tier)

    def test_命运卡只收divination链接(self) -> None:
        """Card 那一格里可能混着别的链接，只有 `a.divination` 是命运卡。"""
        html = page(card("某图", links("Card", ["不是卡"]) + links("Link", ["某图2"])))
        self.assertEqual((), extract_maps(html).maps[0].divination_cards)

    def test_名字按直接子节点取(self) -> None:
        """用 `css_first('div')` 会匹配到外层包裹节点、把整张卡的文本当成名字。"""
        item = extract_maps(page(SHRINE)).maps[0]
        self.assertEqual("奇术秘殿", item.name_zh)
        self.assertNotIn("等阶", item.name_zh)

    def test_长文本卡片不当地图并报数(self) -> None:
        long_name = "这是一段很长的说明文本不该被当成地图名字"
        result = extract_maps(page(SHRINE, card(long_name, prop("等阶", "3"))))
        self.assertEqual(["奇术秘殿"], [m.name_zh for m in result.maps])
        self.assertEqual(1, result.skipped_cards)

    def test_同名卡片只留一张(self) -> None:
        self.assertEqual(1, len(extract_maps(page(SHRINE, SHRINE)).maps))

    def test_没有中文地图卡时拒绝(self) -> None:
        with self.assertRaises(MapStructureError):
            extract_maps(page(card("Shrine", prop("等阶", "5"))))

    def test_一张卡都没有时拒绝(self) -> None:
        with self.assertRaises(MapStructureError):
            extract_maps('<html><body><div class="page"></div></body></html>')


def _entity(**attrs) -> Entity:
    base = {"tier": 5, "bonus_objective": "在 魔法 或更稀有版本的该地图内击败头目",
            "bosses": ["天雷之使派蒂"], "divination_cards": ["薇妮雅的信物"],
            "links": ["中转码头", "沉沦之城"], "bench_recipe": "阻灵术 - 等级 1",
            "atlas_points": None}
    base.update(attrs)
    return Entity(
        entity_id="map:奇术秘殿",
        entity_type=EntityType.MAP,
        name_zh="奇术秘殿",
        attributes=base,
        ref=SourceRef(source=Source.POEDB.value,
                      url="https://poedb.tw/cn/Atlas_of_Worlds", priority=1),
    )


class CardTest(unittest.TestCase):
    def test_卡片给等阶头目与连接(self) -> None:
        text = render_maps.render_card(_entity())
        self.assertIn("# 奇术秘殿（地图）", text)
        self.assertIn("等阶：5", text)
        self.assertIn("头目：天雷之使派蒂", text)
        self.assertIn("异界图鉴上相邻的地图：中转码头、沉沦之城", text)
        self.assertLessEqual(len(text), render_maps.MAX_CHUNK_CHARS)

    def test_没有等阶时写明页面未给(self) -> None:
        """空着会让人以为渲染坏了。"""
        self.assertIn("页面未给", render_maps.render_card(_entity(tier=None)))

    def test_长清单写出还剩多少(self) -> None:
        many = [f"图{i}" for i in range(12)]
        text = render_maps.render_card(_entity(links=many))
        self.assertIn(f"共 {len(many)} 个", text)

    def test_卡片不写词缀类边界(self) -> None:
        """把「词缀」写进 159 张卡片，等于给每个含「词缀」的问题送覆盖率。"""
        text = render_maps.render_card(_entity())
        for word in ("词缀", "未采集"):
            self.assertNotIn(word, text)

    def test_出卡片且分类是地图系统(self) -> None:
        chunks = render_maps.to_chunks(_entity())
        self.assertEqual(1, len(chunks))
        self.assertEqual("地图", chunks[0].metadata.subcategory)


if __name__ == "__main__":
    unittest.main()
