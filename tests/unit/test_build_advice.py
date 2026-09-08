"""需求 8.7：只有已知短板才给低/高预算双档建议。"""

from __future__ import annotations

import unittest

from parsers.build_advice import build_suggestions
from parsers.pob_build import PobBuild


class BuildAdviceTest(unittest.TestCase):
    def test_元素抗性短板给成对建议和可复核收益(self) -> None:
        items = build_suggestions(PobBuild(player_stats={
            "FireResist": 50,
            "ColdResist": 75,
            "LightningResist": 70,
        }))
        elemental = [item for item in items if item.weakness.startswith("元素抗性未满")]
        self.assertEqual([item.tier for item in elemental], ["低预算", "高预算"])
        self.assertTrue(all("火焰 50%" in item.weakness for item in elemental))
        self.assertTrue(all("冰霜" not in item.weakness for item in elemental))
        self.assertIn("火焰至少 +25%", elemental[0].action)
        self.assertIn("承受倍率降低约 50%", elemental[0].expected_gain)

    def test_混沌抗性分阶段目标(self) -> None:
        items = build_suggestions(PobBuild(player_stats={"ChaosResist": -20}))
        self.assertEqual(len(items), 2)
        self.assertIn("补到 10%", items[0].action)
        self.assertIn("补到 75%", items[1].action)
        self.assertTrue(all("无穿透" in item.assumptions for item in items))

    def test_法压只承诺触发率不猜减伤(self) -> None:
        items = build_suggestions(PobBuild(player_stats={
            "EffectiveSpellSuppressionChance": 80,
        }))
        self.assertEqual(len(items), 2)
        self.assertTrue(all("80% → 100%" in item.expected_gain for item in items))
        self.assertTrue(all("不假定固定减伤比例" in item.assumptions for item in items))

    def test_每个短板都有低高两档和成本边界(self) -> None:
        items = build_suggestions(PobBuild(player_stats={
            "FireResist": 60,
            "ChaosResist": 0,
            "SpellSuppressionChance": 90,
        }))
        weaknesses = {item.weakness for item in items}
        for weakness in weaknesses:
            pair = [item for item in items if item.weakness == weakness]
            self.assertEqual({item.tier for item in pair}, {"低预算", "高预算"})
            self.assertTrue(all(item.expected_gain for item in pair))
            self.assertTrue(all("未接赛季经济数据" in item.estimated_cost for item in pair))

    def test_未知项不猜成短板(self) -> None:
        self.assertEqual(build_suggestions(PobBuild()), [])

    def test_已满指标不生成建议(self) -> None:
        self.assertEqual(build_suggestions(PobBuild(player_stats={
            "FireResist": 75,
            "ColdResist": 80,
            "LightningResist": 75,
            "ChaosResist": 75,
            "EffectiveSpellSuppressionChance": 100,
        })), [])


if __name__ == "__main__":
    unittest.main()
