"""Build 防御来源拆分：只列装备原文，不冒充完整 PoB breakdown。"""

from __future__ import annotations

import unittest

from parsers.build_sources import build_defence_sources
from parsers.item_text import parse_item
from parsers.pob_build import BuildItem, PobBuild


ITEM = """Rarity: RARE
Guard Shell
Saintly Chainmail
Armour: 500
Energy Shield: 120
Implicits: 0
+80 to maximum Life
+35% to Fire Resistance
+1% to maximum Fire Resistance
15% chance to Suppress Spell Damage
0.4% of Attack Damage Leeched as Life
120% increased Armour and Evasion
+14% to Fire and Cold Resistances {crafted}
Nearby Enemies have -9% to Fire Resistance
"""


class DefenceSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        item = parse_item(ITEM)
        self.sources = build_defence_sources(PobBuild(
            items=[BuildItem(slot="Body Armour", raw=ITEM, parsed=item)],
            passive_nodes=("1", "2"),
            config={"enemyIsBoss": "Pinnacle"},
        ))

    def test_底材属性和词缀原文均保留(self) -> None:
        details = [line.detail for line in self.sources.lines]
        self.assertIn("护甲: 500", details)
        self.assertIn("能量护盾: 120", details)
        self.assertIn("+80 to maximum Life", details)
        self.assertIn("+35% to Fire Resistance", details)

    def test_最大抗性不重复算普通抗性(self) -> None:
        hits = [
            line.category for line in self.sources.lines
            if line.detail == "+1% to maximum Fire Resistance"
        ]
        self.assertEqual(hits, ["max_resistance"])

    def test_同一混合词缀可进入多个防御类别(self) -> None:
        hits = [
            line.category for line in self.sources.lines
            if line.detail == "120% increased Armour and Evasion"
        ]
        self.assertEqual(hits, ["armour_evasion"])
        self.assertTrue(any(
            line.category == "avoidance" and "Suppress" in line.detail
            for line in self.sources.lines
        ))
        self.assertTrue(any(
            line.category == "recovery" and "Leeched" in line.detail
            for line in self.sources.lines
        ))
        crafted = next(line for line in self.sources.lines if "Fire and Cold" in line.detail)
        self.assertEqual(crafted.origin, "工艺词缀")

    def test_敌人减抗不是自身防御来源(self) -> None:
        self.assertFalse(any(
            "Nearby Enemies" in line.detail for line in self.sources.lines
        ))

    def test_未拆分来源明确列出而不是当作零(self) -> None:
        self.assertTrue(any("天赋树" in note for note in self.sources.unresolved))
        self.assertTrue(any("配置与条件" in note for note in self.sources.unresolved))
        self.assertIn("不是 0", self.sources.note)

    def test_空装备仍返回边界说明(self) -> None:
        sources = build_defence_sources(PobBuild())
        self.assertEqual(sources.lines, [])
        self.assertTrue(sources.unresolved)
        self.assertIn("不求和", sources.note)


if __name__ == "__main__":
    unittest.main()
