"""装备分析报告的单元测试（任务 4.3，需求 7.4/7.5/7.7/7.8）。

复用 `test_item_mods` 的内存桩库，离线可跑。

重点是三条「不许做的事」：
  - 本地装备报告不许把另一个接口的同类挂单冒充为精确市场估值
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
from tests.unit.test_item_mods import (  # noqa: E402
    _AMULET_MIXED_RES,
    _insert_affix,
    _insert_base,
    fixture_conn,
    make_item,
)

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

    def test_混合词缀报告条数容量引用与API一致(self):
        _insert_affix(
            self.conn, page="Amulets", code="Amulet", zh="项链",
            group="AllResistances", gen_label="后缀", ladder=_AMULET_MIXED_RES,
            category="master", category_label="工艺工作台",
        )
        raw = make_item("项链", [
            "+107 最大生命", "+5% 所有元素抗性", "召唤生物获得 +7% 元素抗性",
        ])
        report = build_report(self.conn, parse_item(raw))
        self.assertEqual(len(report.parsed.all_mods), 3)  # 原始文本保持可审计
        self.assertEqual(len(report.mods.mods), 2)
        self.assertEqual(report.mods.matched_count, 2)
        self.assertEqual((report.mods.prefix_count, report.mods.suffix_count), (1, 1))
        self.assertEqual((report.capacity.prefix_remaining,
                          report.capacity.suffix_remaining), (2, 2))
        self.assertEqual(len(report.citations), 2)  # 词缀页 + 容量规则
        rendered = render_report(report)
        self.assertIn("已确定档位 2 条 / 共 2 条；前缀 1 条、后缀 1 条", rendered)
        self.assertIn("+5% 所有元素抗性 / 召唤生物获得 +7% 元素抗性", rendered)
        request = ItemRequest(text=raw, render=True)
        with patch("api.item.dbmod.get_conn", return_value=self.conn):
            response = analyze_item(request, caller="test")
        self.assertEqual(len(response.mods), 2)
        self.assertEqual((response.prefix_count, response.suffix_count), (1, 1))
        self.assertEqual(response.mods[1].tier, 2)
        self.assertTrue(any("合并分析" in w for w in response.warnings))

    def test_可合并也可独立时报告与容量都保持未知(self):
        _insert_affix(
            self.conn, page="Amulets", code="Amulet", zh="项链",
            group="AllResistances", gen_label="后缀", ladder=_AMULET_MIXED_RES,
            category="master", category_label="工艺工作台",
        )
        _insert_affix(
            self.conn, page="Amulets", code="Amulet", zh="项链",
            group="StandaloneAllRes", gen_label="后缀",
            ladder=[(1, 1, 1, [[4, 6]], "+(4-6)% 所有元素抗性")],
        )
        _insert_affix(
            self.conn, page="Amulets", code="Amulet", zh="项链",
            group="StandaloneMinionRes", gen_label="后缀",
            ladder=[(1, 1, 1, [[5, 8]], "召唤生物获得 +(5-8)% 元素抗性")],
        )
        report = self.report_for(["+5% 所有元素抗性", "召唤生物获得 +7% 元素抗性"])
        self.assertEqual(len(report.mods.mods), 2)
        self.assertEqual(report.mods.matched_count, 0)
        self.assertEqual(report.capacity.status, "unknown")
        self.assertIn("无法唯一确认的词缀分组", render_report(report))

    def test_具体底材页面出处在报告与API中可核对(self):
        _insert_base(
            self.conn, name_en="Vaal Gauntlets", name_zh="瓦尔护手",
            category="gloves", poedb_page="Gloves", item_class="Gloves",
            tags=["armour", "gloves", "str_armour"],
        )
        raw = make_item("手套", ["+107 最大生命"], base_type="瓦尔护手")
        report = build_report(self.conn, parse_item(raw))
        self.assertEqual(report.mods.item_class.pages, ["Gloves_str"])
        self.assertEqual(report.mods.item_class.matched_by, "base")
        urls = {citation.url for citation in report.citations}
        self.assertIn("https://poedb.tw/cn/Vaal_Gauntlets", urls)
        self.assertIn(
            "https://github.com/PathOfBuildingCommunity/PathOfBuilding/blob/test/Vaal_Gauntlets",
            urls,
        )
        rendered = render_report(report)
        self.assertIn("词缀查询页：Gloves_str", rendered)
        self.assertIn("PoB 标签与 POEDB 名称桥", rendered)
        request = ItemRequest(text=raw, render=False)
        with patch("api.item.dbmod.get_conn", return_value=self.conn):
            response = analyze_item(request, caller="test")
        self.assertEqual(response.item_class_pages, ["Gloves_str"])
        self.assertEqual(response.page_resolution, "base")
        self.assertTrue(any("Vaal_Gauntlets" in c.url for c in response.citations))

    def test_隐式区间有据时报告展示但T级仍未知(self):
        _insert_affix(
            self.conn, page="Body_Armours_str", code="Body Armour", zh="胸甲",
            group="BodyLife", gen_label="前缀",
            ladder=[(1, 1, 1, [[115, 129]], "+(115-129) 最大生命")],
        )
        _insert_base(
            self.conn, name_en="Astral Plate", name_zh="星芒战铠",
            category="body", poedb_page="Body_Armours", item_class="Body Armour",
            tags=["armour", "body_armour", "str_armour"],
            armour_tag="str_armour", modifier_page="Body_Armours_str",
            implicit_text="+(8-12)% to all Elemental Resistances",
        )
        raw = make_item(
            "Body Armours", ["+10% to all Elemental Resistances (implicit)"],
            language="en", base_type="Astral Plate",
        )
        report = build_report(self.conn, parse_item(raw))
        self.assertEqual(report.mods.matched_count, 0)
        implicit_citation = next(
            citation for citation in report.citations if "Astral_Plate" in citation.url
            and "github.com" in citation.url
        )
        self.assertEqual(implicit_citation.source, "Path of Building")
        self.assertEqual(implicit_citation.priority, 2)
        rendered = render_report(report)
        self.assertIn("**未匹配**", rendered)
        self.assertIn("PoB 底材固有隐式；区间 8-12；roll 50%", rendered)
        self.assertIn("PoB 未给隐式 T 级", rendered)
        with patch("api.item.dbmod.get_conn", return_value=self.conn):
            response = analyze_item(ItemRequest(text=raw, render=False), caller="test")
        self.assertIsNone(response.mods[0].tier)
        self.assertEqual(response.mods[0].ranges, [(8.0, 12.0)])
        self.assertEqual(response.mods[0].roll_pct, 0.5)


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
    def test_报告不自动联网并指向可比挂单工具(self):
        report = self.report_for(["+107 最大生命"])
        self.assertFalse(report.market.available)
        text = render_report(report)
        self.assertIn("**无价格数据。**", text)
        self.assertIn("economy_rare_comparables", text)
        self.assertIn("不是这件装备的精确成交价", text)

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
