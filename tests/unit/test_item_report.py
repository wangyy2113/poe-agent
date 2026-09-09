"""装备分析报告的单元测试（任务 4.3，需求 7.4/7.5/7.7/7.8）。

复用 `test_item_mods` 的内存桩库，离线可跑。

重点是三条「不许做的事」：
  - 不许给市场价格（经济源未接入，需求 7.8 禁止类推）
  - 不许给裂界/镜像湖/镜子级别的结论（需求 7.5 的输入不具备）
  - 「该底材上最高档位」不许跨来源类别取（工艺台 T1 与掉落 T1 差一个量级）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.item import ItemRequest, analyze_item  # noqa: E402
from craft.capacity import assess_affix_capacity, infer_item_kind  # noqa: E402
from parsers.item_report import build_report, render_report  # noqa: E402
from parsers.item_text import parse_item  # noqa: E402
from tests.unit.test_item_mods import _insert_affix, fixture_conn, make_item  # noqa: E402

#: 真实数据：项链生命组在**工艺工作台**来源下的档位，T1 只有 (41-45)。
#: 掉落来源的 T1 是 (115-129)。两者同属 IncreasedLife 组——正是「只按组查会
#: 把工艺台 T1 当成掉落 T1」的现实来源。
_AMULET_LIFE_BENCH = [
    (1, 6, 81, [[41, 45]], "+(41-45) 最大生命"),
    (2, 6, 60, [[34, 40]], "+(34-40) 最大生命"),
]


class ReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fixture_conn()
        _insert_affix(
            self.conn, page="Amulets", code="Amulet", zh="项链",
            group="IncreasedLife", gen_label="前缀", ladder=_AMULET_LIFE_BENCH,
            category="master", category_label="工艺工作台",
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def report_for(self, mods: list[str], class_line: str = "项链"):
        return build_report(self.conn, parse_item(make_item(class_line, mods)))


class TestBasics(ReportTestCase):
    def test_基础信息来自装备文本(self):
        report = self.report_for(["+107 最大生命"])
        text = render_report(report)
        self.assertIn("来自你粘贴的装备文本本身", text)
        self.assertIn("**类别**：项链", text)
        self.assertIn("**物品等级**：84", text)

    def test_缺失字段标未知而非填估计值(self):
        """需求 8.8 的同一条原则：缺失标未知，不拿估计值冒充。"""
        text = render_report(self.report_for(["+107 最大生命"]))
        self.assertIn("**品质**：未知", text)


class TestModSection(ReportTestCase):
    def test_词缀带档位与引用标记(self):
        text = render_report(self.report_for(["+107 最大生命"]))
        self.assertIn("**T2/9**", text)
        self.assertIn("[[E1]]", text)
        self.assertIn("T 级来源：POEDB 词缀库", text)

    def test_未匹配词缀单独分区且写明原因(self):
        report = self.report_for(["+107 最大生命", "+50 最大魔力"])
        text = render_report(report)
        self.assertIn("未能确定档位的词缀", text)
        self.assertIn("不给推测的 T 级", text)
        self.assertIn("+50 最大魔力", text)

    def test_全部匹配时不出现未匹配分区(self):
        text = render_report(self.report_for(["+107 最大生命"]))
        self.assertNotIn("未能确定档位的词缀", text)


class TestCraftDirections(ReportTestCase):
    def test_最高档位不跨来源类别(self):
        """掉落词缀的升级目标必须是掉落 T1（115-129），
        不能是同组工艺台的 T1（41-45）。"""
        report = self.report_for(["+107 最大生命"])
        direction = next(d for d in report.craft_directions if "最大生命" in d.mod_text)
        assert direction.upgrade is not None
        self.assertEqual(direction.upgrade.to_ranges, [(115.0, 129.0)])
        self.assertEqual(direction.upgrade.required_ilvl, 54)

    def test_已是T1则没有升级方向(self):
        report = self.report_for(["+122 最大生命"])
        upgrades = [d for d in report.craft_directions if d.upgrade is not None]
        self.assertEqual(upgrades, [])

    def test_可制作方向标为建议类(self):
        text = render_report(self.report_for(["+107 最大生命"]))
        self.assertIn("### 可制作方向", text)
        self.assertIn("建议类内容", text)


class TestAffixCapacity(ReportTestCase):
    def test_稀有装备返回上限与剩余名额(self):
        report = self.report_for(["+107 最大生命"])
        capacity = report.capacity
        self.assertEqual(capacity.status, "pass")
        self.assertEqual((capacity.prefix_limit, capacity.suffix_limit), (3, 3))
        self.assertEqual((capacity.prefix_remaining, capacity.suffix_remaining), (2, 3))
        text = render_report(report)
        self.assertIn("可用随机词缀位：前缀 2 条、后缀 3 条", text)
        self.assertIn("前后缀名额规则", text)

    def test_魔法装备使用一加一规则(self):
        raw = make_item("项链", ["+107 最大生命", "+43% 火焰抗性"])
        report = build_report(self.conn, parse_item(raw.replace("稀有度: 稀有", "稀有度: 魔法")))
        self.assertEqual(report.capacity.status, "pass")
        self.assertEqual((report.capacity.prefix_limit, report.capacity.suffix_limit), (1, 1))
        self.assertEqual((report.capacity.prefix_remaining, report.capacity.suffix_remaining), (0, 0))

    def test_未识别显式词缀时不声称剩余名额(self):
        report = self.report_for(["+107 最大生命", "+50 最大魔力"])
        self.assertEqual(report.capacity.status, "unknown")
        self.assertIsNone(report.capacity.prefix_remaining)
        self.assertIn("1 条显式词缀未能识别", report.capacity.missing)
        self.assertNotIn("可用随机词缀位", render_report(report))

    def test_珠宝与已腐化深渊珠宝使用特殊规则(self):
        jewel = infer_item_kind("Jewels")
        abyss = infer_item_kind("Abyss Jewels", corrupted=True)
        self.assertEqual(jewel, "jewel")
        self.assertEqual(abyss, "jewel_abyss_corrupted")
        self.assertEqual(infer_item_kind("Amulets", "Jewel Pendant"), "default")
        normal = assess_affix_capacity(self.conn, "稀有", 2, 2, item_kind=jewel)
        corrupted = assess_affix_capacity(self.conn, "稀有", 2, 2, item_kind=abyss)
        self.assertEqual((normal.prefix_limit, normal.suffix_limit), (2, 2))
        self.assertEqual((corrupted.prefix_limit, corrupted.suffix_limit), (3, 3))

    def test_API返回结构化容量结果(self):
        request = ItemRequest(text=make_item("项链", ["+107 最大生命"]), render=False)
        with patch("api.item.dbmod.get_conn", return_value=self.conn):
            response = analyze_item(request, caller="test")
        self.assertEqual(response.affix_capacity.status, "pass")
        self.assertEqual(response.affix_capacity.prefix_remaining, 2)
        self.assertEqual(response.affix_capacity.url, "https://poedb.tw/cn/Modifiers")
        self.assertIsNone(response.answer)


class TestMarketAndJudgements(ReportTestCase):
    def test_没有经济数据就明说没有(self):
        report = self.report_for(["+107 最大生命"])
        self.assertFalse(report.market.available)
        text = render_report(report)
        self.assertIn("**无价格数据。**", text)
        self.assertIn("不按同类物品类推价格", text)

    def test_三项价值判断都不给结论(self):
        report = self.report_for(["+107 最大生命"])
        topics = [j.topic for j in report.judgements]
        self.assertEqual(len(topics), 3)
        self.assertTrue(any("裂界" in t for t in topics))
        self.assertTrue(any("镜像湖" in t for t in topics))
        self.assertTrue(any("镜子级别" in t for t in topics))
        for judgement in report.judgements:
            self.assertEqual(judgement.verdict, "数据不足，不作判断")
            self.assertTrue(judgement.missing)

    def test_判断分区列出已知事实但不下结论(self):
        text = render_report(self.report_for(["+107 最大生命"]))
        self.assertIn("已知的相关事实", text)
        self.assertIn("物品等级 84", text)
        # 三项判断都必须落在「数据不足」上，不能有任何一项给出结论
        self.assertEqual(text.count("数据不足，不作判断"), 3)
        self.assertIn("目前一项都给不出结论", text)

    def test_未匹配词缀会影响整体评估的表述(self):
        report = self.report_for(["+107 最大生命", "+50 最大魔力"])
        facts = report.judgements[0].known_facts
        self.assertTrue(any("无法据词缀完整评估" in f for f in facts))


class TestCitations(ReportTestCase):
    def test_引用按URL去重并编号(self):
        report = self.report_for(["+107 最大生命", "+43% 火焰抗性"])
        # 两条词缀同属项链页面，再加一条容量规则引用
        self.assertEqual(len(report.citations), 2)
        text = render_report(report)
        self.assertIn("[E1] 来源：POEDB → 项链 词缀表", text)
        self.assertIn("[E2] 来源：POEDB → 前后缀名额规则", text)

    def test_无词缀匹配时只保留容量规则引用(self):
        report = self.report_for(["+50 最大魔力"])
        self.assertEqual(len(report.citations), 1)
        self.assertEqual(report.citations[0].title, "前后缀名额规则")
        self.assertIn("### 引用", render_report(report))


class TestWarningsPassThrough(ReportTestCase):
    def test_解析告警进报告(self):
        text = (
            "物品类别: 项链\n稀有度: 稀有\n测试\n测试底材\n--------\n物品等级: 84\n"
            "--------\n+10 力量\n--------\n+20 敏捷\n--------\n+30 智慧\n"
        )
        report = build_report(self.conn, parse_item(text))
        self.assertTrue(report.warnings)
        self.assertIn("### 解析告警", render_report(report))


if __name__ == "__main__":
    unittest.main(verbosity=2)
