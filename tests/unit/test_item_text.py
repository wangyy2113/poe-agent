"""装备文本解析器的单元测试（任务 4.1）。

用真实样本作 fixture，来源见 `tests/unit/fixtures/items/README.md`。
纯文本解析，不连数据库，可完全离线跑。

重点不只是「能解析」，还有两类「不该做的事」：
  - 解析不出来必须显式失败并给期望格式，不许部分猜测后继续（需求 7.6）
  - 普通复制拿不到 T 级时 `tier` 必须是 None，不许填猜测值（需求 7.9 的前置条件）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from entities.taxonomy import ModKind  # noqa: E402
from parsers.item_text import (  # noqa: E402
    LINE_SEP,
    ItemParseError,
    Rarity,
    parse_item,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "items"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestRareGlovesEn(unittest.TestCase):
    """标准 Ctrl+C：稀有护甲，隐式 + 显式 + 工艺台 + 腐化。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.item = parse_item(load("rare_gloves_en.txt"))

    def test_头部字段(self):
        self.assertEqual(self.item.item_class, "Gloves")
        self.assertIs(self.item.rarity, Rarity.RARE)
        self.assertEqual(self.item.name, "Havoc Grip")
        self.assertEqual(self.item.base_type, "Fugitive Gloves")
        self.assertEqual(self.item.language, "en")

    def test_属性行(self):
        self.assertEqual(self.item.item_level, 84)
        self.assertEqual(self.item.quality, 20)
        self.assertEqual(self.item.properties["evasion"], "220 (augmented)")

    def test_需求(self):
        self.assertEqual(self.item.requirements.level, 70)
        self.assertEqual(self.item.requirements.dexterity, 51)
        self.assertEqual(self.item.requirements.intelligence, 51)
        self.assertIsNone(self.item.requirements.strength)

    def test_插槽与连接(self):
        self.assertEqual(self.item.sockets.groups, [["R", "G", "B", "B"]])
        self.assertEqual(self.item.sockets.total, 4)
        self.assertEqual(self.item.sockets.max_links, 4)

    def test_隐式按后缀标记归类(self):
        self.assertEqual(len(self.item.implicits), 1)
        self.assertEqual(self.item.implicits[0].text, "+12% to Chaos Resistance")
        self.assertIs(self.item.implicits[0].kind, ModKind.IMPLICIT)

    def test_显式与工艺台词缀(self):
        self.assertEqual(len(self.item.explicits), 5)
        self.assertEqual(len(self.item.crafted), 1)
        self.assertEqual(self.item.crafted[0].text, "+31 to maximum Mana")

    def test_腐化旗标(self):
        self.assertTrue(self.item.flags.corrupted)
        self.assertIn("已腐化", self.item.flags.labels())

    def test_数值抽取(self):
        life = self.item.explicits[0]
        self.assertEqual(life.text, "+45 to maximum Life")
        self.assertEqual(life.values, [45.0])

    def test_普通复制不带T级(self):
        """普通复制文本里没有 T 级信息，必须留空等查库，不许猜。"""
        for mod in self.item.all_mods:
            self.assertIsNone(mod.tier, mod.text)
            self.assertEqual(mod.ranges, [], mod.text)
            self.assertIsNone(mod.roll_pct(), mod.text)

    def test_没有未归类行(self):
        self.assertEqual(self.item.unclassified, [])


class TestNebulisAdvancedEn(unittest.TestCase):
    """真实进阶复制（Ctrl+Alt+C）：游戏自带 T 级与区间。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.item = parse_item(load("unique_nebulis_advanced_en.txt"))

    def test_识别为进阶复制(self):
        self.assertTrue(self.item.advanced_copy)

    def test_头部(self):
        self.assertEqual(self.item.item_class, "Sceptres")
        self.assertIs(self.item.rarity, Rarity.UNIQUE)
        self.assertEqual(self.item.name, "Nebulis")
        # 底材名前缀 Synthesised 不属于底材本身
        self.assertEqual(self.item.base_type, "Void Sceptre")

    def test_属性段裸行不当词缀(self):
        """武器段开头的「Sceptre」是类型说明行，当成词缀会污染整条分析。"""
        self.assertIn("Sceptre", self.item.info_lines)
        for mod in self.item.all_mods:
            self.assertNotEqual(mod.text, "Sceptre")

    def test_插槽含空格分组(self):
        self.assertEqual(self.item.sockets.groups, [["B"], ["R"]])
        self.assertEqual(self.item.sockets.max_links, 1)

    def test_附魔按后缀归类(self):
        texts = [m.text for m in self.item.enchants]
        self.assertIn("Has 1 White Socket", texts)
        self.assertIn("8% increased Explicit Cold Modifier magnitudes", texts)

    def test_提示文本被跳过(self):
        """`(Armour, Evasion Rating and Energy Shield are the standard Defences)`
        是提示文本，不是词缀。"""
        for mod in self.item.all_mods:
            self.assertNotIn("standard Defences", mod.text)
            self.assertNotIn("come from an item's type", mod.text)

    def test_进阶复制带出T级与词缀名(self):
        freezing = next(m for m in self.item.explicits if "Cold Damage to Spells" in m.text)
        self.assertEqual(freezing.tier, 5)
        self.assertEqual(freezing.affix_name, "Freezing")
        self.assertEqual(freezing.affix_type, "Prefix")
        self.assertIn("Cold", freezing.tags)

    def test_进阶复制的区间与实际值分离(self):
        freezing = next(m for m in self.item.explicits if "Cold Damage to Spells" in m.text)
        # 文本里的 `17(16-20) to 35(30-36)` → 实际值 17/35，区间 (16,20)/(30,36)
        self.assertEqual(freezing.text, "Adds 17 to 35 Cold Damage to Spells")
        self.assertEqual(freezing.values, [17.0, 35.0])
        self.assertEqual(freezing.ranges, [(16.0, 20.0), (30.0, 36.0)])

    def test_roll百分比可算(self):
        freezing = next(m for m in self.item.explicits if "Cold Damage to Spells" in m.text)
        pct = freezing.roll_pct()
        self.assertIsNotNone(pct)
        # (17-16)/(20-16)=0.25，(35-30)/(36-30)≈0.833，均值≈0.542
        assert pct is not None
        self.assertAlmostEqual(pct, (0.25 + 5 / 6) / 2, places=3)

    def test_满值roll是1(self):
        cold_res = next(m for m in self.item.explicits if "to Cold Resistance" in m.text)
        self.assertEqual(cold_res.values, [35.0])
        self.assertEqual(cold_res.ranges, [(29.0, 35.0)])
        self.assertAlmostEqual(cold_res.roll_pct() or -1, 1.0, places=6)

    def test_一个词缀头下的多行合成一条(self):
        """混合词缀在装备上占两行，库里存成一条（用 LINE_SEP 拼接）。
        拆成两条的话两半都匹配不上。"""
        beetle = next(m for m in self.item.explicits if m.affix_name == "Beetle's")
        self.assertTrue(beetle.is_multiline)
        self.assertEqual(len(beetle.lines), 2)
        self.assertIn(LINE_SEP, beetle.lookup_text)
        self.assertIn("increased Armour", beetle.lookup_text)
        self.assertIn("Stun and Block Recovery", beetle.lookup_text)

    def test_工艺台词缀识别为crafted(self):
        crafted = [m for m in self.item.explicits if m.kind is ModKind.CRAFTED]
        names = {m.affix_name for m in crafted}
        self.assertIn("Upgraded", names)
        self.assertIn("of Craft", names)

    def test_工艺台词缀用Rank而非Tier(self):
        of_craft = next(m for m in self.item.explicits if m.affix_name == "of Craft")
        self.assertEqual(of_craft.tier_rank, 3)
        self.assertIsNone(of_craft.tier)

    def test_裂隙词缀识别(self):
        fractured = [m for m in self.item.all_mods if m.kind is ModKind.FRACTURED]
        self.assertEqual(len(fractured), 1)
        self.assertEqual(fractured[0].affix_name, "Thorny")

    def test_隐匿占位行不当词缀(self):
        self.assertTrue(self.item.flags.veiled)
        for mod in self.item.all_mods:
            self.assertNotEqual(mod.text.lower(), "veiled prefix")

    def test_影响词(self):
        self.assertIn("searing_exarch", self.item.flags.influences)
        self.assertIn("焚界者", self.item.flags.labels())

    def test_腐化(self):
        self.assertTrue(self.item.flags.corrupted)


class TestBowFracturedEn(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.item = parse_item(load("rare_bow_fractured_en.txt"))

    def test_多组插槽(self):
        self.assertEqual(self.item.sockets.groups, [["G", "G", "G"], ["G", "B"], ["B"]])
        self.assertEqual(self.item.sockets.total, 6)
        self.assertEqual(self.item.sockets.max_links, 3)

    def test_裂隙词缀与整件裂隙旗标(self):
        self.assertTrue(self.item.flags.fractured)
        self.assertEqual(len(self.item.fractured), 1)
        self.assertEqual(self.item.fractured[0].text, "Adds 40 to 78 Physical Damage")

    def test_影响词(self):
        self.assertEqual(self.item.flags.influences, ["hunter"])

    def test_显式词缀数量(self):
        # 裂隙那条也算显式（它就是显式词缀，只是被裂界固定了）
        self.assertEqual(len(self.item.explicits), 5)

    def test_两值词缀的数值(self):
        phys = self.item.fractured[0]
        self.assertEqual(phys.values, [40.0, 78.0])


class TestMagicRingEn(unittest.TestCase):
    def test_魔法物品只有一行名称(self):
        item = parse_item(load("magic_ring_en.txt"))
        self.assertIs(item.rarity, Rarity.MAGIC)
        self.assertEqual(item.name, "Sapphire Ring of Success")
        # 魔法物品的名称行里含词缀前后缀，底材要靠底材库反查，这里退化为同一行
        self.assertEqual(item.base_type, "Sapphire Ring of Success")
        self.assertEqual(len(item.implicits), 1)
        self.assertEqual(len(item.explicits), 2)


class TestAmuletZh(unittest.TestCase):
    """中文样本。表头标签待真机核实，见 fixtures/items/README.md。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.item = parse_item(load("rare_amulet_zh.txt"))

    def test_语言判定(self):
        self.assertEqual(self.item.language, "zh")

    def test_头部(self):
        self.assertEqual(self.item.item_class, "项链")
        self.assertIs(self.item.rarity, Rarity.RARE)
        self.assertEqual(self.item.name, "恐惧之瞳")
        self.assertEqual(self.item.base_type, "玛瑙护身符")

    def test_属性与需求(self):
        self.assertEqual(self.item.item_level, 84)
        self.assertEqual(self.item.requirements.level, 60)
        self.assertEqual(self.item.requirements.intelligence, 41)

    def test_无后缀标记时按段落位置推隐式(self):
        """中文客户端的隐式后缀标签未知，只能靠「隐式段在显式段之前」推断。
        这条推断本身必须被测到——它是中文路径能用的前提。"""
        self.assertEqual(len(self.item.implicits), 1)
        self.assertEqual(self.item.implicits[0].text, "+16 全属性")
        self.assertEqual(len(self.item.explicits), 5)

    def test_中文词缀文本原样保留(self):
        texts = [m.text for m in self.item.explicits]
        self.assertIn("+107 最大生命", texts)
        self.assertIn("法术伤害提高 20%", texts)
        self.assertIn("+43% 火焰抗性", texts)

    def test_中文腐化旗标(self):
        self.assertTrue(self.item.flags.corrupted)


class TestFailureContract(unittest.TestCase):
    """需求 7.6：认不出就明确失败，给原因与期望格式，不猜测续行。"""

    def test_空输入(self):
        with self.assertRaises(ItemParseError):
            parse_item("   \n  \n")

    def test_不是装备文本(self):
        with self.assertRaises(ItemParseError) as ctx:
            parse_item(load("garbage.txt"))
        self.assertIn("Rarity", ctx.exception.reason)

    def test_失败带期望格式样例(self):
        try:
            parse_item(load("garbage.txt"))
        except ItemParseError as exc:
            self.assertIn("Item Class:", exc.expected_sample)
            self.assertIn("Ctrl+C", exc.hint)

    def test_只有表头没有名称也算失败(self):
        with self.assertRaises(ItemParseError):
            parse_item("Item Class: Gloves\nRarity: Rare\n")

    def test_稀有度取值异常不静默采信(self):
        item = parse_item("Item Class: Gloves\nRarity: Legendary\nName\nIron Gauntlets")
        self.assertIs(item.rarity, Rarity.UNKNOWN)
        self.assertTrue(any("稀有度" in w for w in item.warnings))


class TestSectionAmbiguity(unittest.TestCase):
    """无标记词缀段超过两段时不硬猜（宁可不归类，也不给错 T 级）。"""

    def test_三段无标记词缀会记告警且多余段不参与分析(self):
        text = (
            "Item Class: Gloves\nRarity: Rare\nName\nIron Gauntlets\n"
            "--------\nItem Level: 84\n"
            "--------\n+10 to Strength\n"
            "--------\n+20 to Dexterity\n"
            "--------\n+30 to Intelligence\n"
        )
        item = parse_item(text)
        self.assertEqual(len(item.unclassified), 1)
        self.assertIn("+30 to Intelligence", item.unclassified[0])
        self.assertTrue(any("未归类" in w for w in item.warnings))
        self.assertEqual([m.text for m in item.implicits], ["+10 to Strength"])
        self.assertEqual([m.text for m in item.explicits], ["+20 to Dexterity"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
