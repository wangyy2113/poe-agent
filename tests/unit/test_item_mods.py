"""词缀 T 级与数值范围匹配的单元测试（任务 4.2，需求 7.3/7.9）。

用内存 SQLite 打桩，**不依赖 `data/poe.db`**，可离线跑。桩数据不是编的：
每一行的 T 级、物品等级、区间都照抄真实库里的值（见各 fixture 常量上方注释），
这样断言的 T 级也就是真实世界里正确的 T 级。

重点验证需求 7.9：匹配不上时必须标未匹配并说明原因，**绝不猜 T 级**。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from parsers.item_mods import (  # noqa: E402
    TIER_FROM_BRIDGE,
    TIER_FROM_ITEM_TEXT,
    TIER_FROM_POEDB,
    analyze_mods,
    resolve_item_class,
)
from core.db import SCHEMA as _REAL_SCHEMA  # noqa: E402
from parsers.item_text import parse_item  # noqa: E402

#: 桩库直接用 `core.db.SCHEMA`，**不再手抄一份表定义**。
#: 第三十七轮踩到过：手抄的那份没有 `craft_affix_capacity`，于是前后缀容量判定
#: 在单测里根本没被执行，而受影响的用例**照旧通过**——测试为错误的理由变绿，
#: 比测试变红危险得多。用真表就不会再有第二次。
SCHEMA = _REAL_SCHEMA

#: 真实数据（POEDB，2026-08 抓取）：项链的最大生命阶梯 T1..T4 与火/冰抗、智慧、法伤。
#: 数值照抄 `mod_affix`，未做改动。
_AMULET_LIFE = [
    (1, 9, 54, [[115, 129]], "+(115-129) 最大生命"),
    (2, 9, 44, [[100, 114]], "+(100-114) 最大生命"),
    (3, 9, 36, [[85, 99]], "+(85-99) 最大生命"),
    (4, 9, 30, [[70, 84]], "+(70-84) 最大生命"),
]
_AMULET_FIRE_RES = [
    (1, 8, 84, [[46, 48]], "+(46-48)% 火焰抗性"),
    (2, 8, 72, [[42, 45]], "+(42-45)% 火焰抗性"),
    (3, 8, 60, [[36, 41]], "+(36-41)% 火焰抗性"),
    (4, 8, 48, [[30, 35]], "+(30-35)% 火焰抗性"),
]
#: 真实数据：弓的附加物理伤害阶梯（两个可变位）
_BOW_PHYS = [
    (1, 9, 77, [[34, 47], [72, 84]], "该装备附加 (34-47) - (72-84) 基础物理伤害"),
    (2, 9, 65, [[30, 40], [63, 73]], "该装备附加 (30-40) - (63-73) 基础物理伤害"),
    (3, 9, 54, [[25, 33], [52, 61]], "该装备附加 (25-33) - (52-61) 基础物理伤害"),
]


def _insert_affix(
    conn: sqlite3.Connection,
    *,
    page: str,
    code: str,
    zh: str,
    group: str,
    gen_label: str,
    ladder: list,
    category: str = "normal",
    category_label: str = "Base",
) -> None:
    for tier, tier_count, ilvl, ranges, text in ladder:
        strict = text
        loose = text
        for lo, hi in ranges:
            strict = strict.replace(f"({lo}-{hi})", "#", 1)
            loose = loose.replace(f"({lo}-{hi})", "#", 1)
        conn.execute(
            """INSERT INTO mod_affix VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
            [
                f"{page}:{category}:{group}:{tier}", group, page, code, zh, category,
                category_label, "", 1 if gen_label == "前缀" else 2, gen_label,
                json.dumps([group]), group, ilvl, tier, tier_count, "poedb_payload",
                1000, 1.5, text, strict, loose,
                json.dumps([[float(a), float(b)] for a, b in ranges]),
                "[]", "[]", "[]", None, "POEDB", 1,
                f"https://poedb.tw/cn/{page}",
            ],
        )


def _insert_pob(
    conn: sqlite3.Connection, *, group: str, ladder: list, template_en: str
) -> None:
    """PoB 侧阶梯。刻意多塞一档 POEDB 没有的，模拟真实情况下 PoB 是超集。"""
    for tier, _tc, ilvl, ranges, _text in ladder:
        text_en = template_en
        for lo, hi in ranges:
            text_en = text_en.replace("#", f"({lo}-{hi})", 1)
        strict = template_en
        conn.execute(
            """INSERT INTO pob_mod VALUES
               (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))""",
            [
                f"{group}:{tier}", f"{group}{tier}", "explicit", "Data/ModExplicit.lua",
                "Prefix", "", group, ilvl, text_en, strict, strict,
                json.dumps([[float(a), float(b)] for a, b in ranges]),
                "{}", "{}", "[]", "[]", "[]", "[]", None, "[]", "[]",
                "Path of Building", 2, "https://github.com/x/blob/abc/src/Data/ModExplicit.lua",
                "PoB 2.66.2",
            ],
        )


#: 前后缀名额规则。数值照抄库里真实入库的那七条（POEDB 词缀页 × PoB Item.lua）。
#: **桩库必须有这张表且有数据**：表空时容量判定会退回 unknown，于是「补了容量规则」
#: 这件事在单测里完全不被执行，而受影响的用例照旧变绿（第三十七轮实测踩到）。
_AFFIX_CAPACITY = [
    ("魔法", "default", 1, 1, "both"),
    ("魔法", "flask", 1, 1, "poedb_only"),
    ("稀有", "default", 3, 3, "both"),
    ("稀有", "jewel", 2, 2, "pob_only"),
    ("稀有", "graft", 2, 2, "pob_only"),
    # 反直觉的一条：已腐化的深渊珠宝回到 3+3
    ("稀有", "jewel_abyss_corrupted", 3, 3, "pob_only"),
    ("传奇", "default", None, None, "poedb_only"),  # None = 不受限制
]


def seed_affix_capacity(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO craft_affix_capacity(rarity, item_kind, prefix_limit, suffix_limit,"
        " agreement, poedb_text, pob_text, source, source_priority, url, updated_at)"
        " VALUES (?,?,?,?,?,?,NULL,'POEDB',1,'https://poedb.tw/cn/Modifiers',datetime('now'))",
        [
            (rarity, kind, pre, suf, agreement, f"{rarity}物品名额规则")
            for rarity, kind, pre, suf, agreement in _AFFIX_CAPACITY
        ],
    )


def fixture_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _insert_affix(conn, page="Amulets", code="Amulet", zh="项链",
                  group="IncreasedLife", gen_label="前缀", ladder=_AMULET_LIFE)
    _insert_affix(conn, page="Amulets", code="Amulet", zh="项链",
                  group="FireResistance", gen_label="后缀", ladder=_AMULET_FIRE_RES)
    _insert_affix(conn, page="Bows", code="Bow", zh="弓",
                  group="PhysicalDamage", gen_label="前缀", ladder=_BOW_PHYS)
    # 护甲类：同一中文类别拆成多个页面。两个页面阶梯一致（真实情况），
    # 第三个页面刻意给不同阶梯，用来验证「页面间不一致就不给 T 级」
    for page in ("Gloves_str", "Gloves_int"):
        _insert_affix(conn, page=page, code="Gloves", zh="手套",
                      group="IncreasedLife", gen_label="前缀", ladder=_AMULET_LIFE)
    _insert_affix(conn, page="Gloves_dex", code="Gloves", zh="手套",
                  group="IncreasedLife", gen_label="前缀",
                  ladder=[(t, tc, il, r, txt) for t, tc, il, r, txt in _AMULET_LIFE])
    seed_affix_capacity(conn)
    # 英文桥用：PoB 侧的同名阶梯
    _insert_pob(conn, group="LocalPhysicalDamage", ladder=_BOW_PHYS,
                template_en="Adds # to # Physical Damage")
    _insert_pob(conn, group="IncreasedLife", ladder=_AMULET_LIFE,
                template_en="+# to maximum Life")
    conn.commit()
    return conn


def make_item(class_line: str, mods: list[str], *, language: str = "zh", ilvl: int = 84) -> str:
    header = "物品类别" if language == "zh" else "Item Class"
    rarity = "稀有度: 稀有" if language == "zh" else "Rarity: Rare"
    lvl = "物品等级" if language == "zh" else "Item Level"
    name = "测试物品\n测试底材" if language == "zh" else "Test Item\nTest Base"
    body = "\n".join(mods)
    return f"{header}: {class_line}\n{rarity}\n{name}\n--------\n{lvl}: {ilvl}\n--------\n{body}\n"


class ModsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fixture_conn()

    def tearDown(self) -> None:
        self.conn.close()


class TestResolveItemClass(ModsTestCase):
    def test_页面slug直接命中(self):
        ref = resolve_item_class(self.conn, "Bows")
        self.assertEqual(ref.matched_by, "page")
        self.assertEqual(ref.pages, ["Bows"])
        self.assertEqual(ref.name_zh, "弓")

    def test_类别码命中多个页面(self):
        ref = resolve_item_class(self.conn, "Gloves")
        self.assertEqual(ref.matched_by, "code")
        self.assertEqual(len(ref.pages), 3)

    def test_复数去尾s后按类别码命中(self):
        """装备文本给复数（Body Armours），而类别码是单数（Body Armour）。"""
        ref = resolve_item_class(self.conn, "Amulets")
        self.assertTrue(ref.ok)
        ref2 = resolve_item_class(self.conn, "Bow")  # 单数也应能命中
        self.assertTrue(ref2.ok)

    def test_中文类别名命中(self):
        ref = resolve_item_class(self.conn, "项链")
        self.assertEqual(ref.matched_by, "zh")
        self.assertEqual(ref.pages, ["Amulets"])

    def test_未知类别不猜且给出原因(self):
        ref = resolve_item_class(self.conn, "Maps")
        self.assertFalse(ref.ok)
        self.assertIn("没有对应页面", ref.reason)

    def test_空类别名(self):
        ref = resolve_item_class(self.conn, "")
        self.assertFalse(ref.ok)
        self.assertIn("没有类别行", ref.reason)


class TestChineseTierLookup(ModsTestCase):
    """中文文本查 POEDB 词缀库——主路径。"""

    def test_按数值定档(self):
        item = parse_item(make_item("项链", ["+107 最大生命"]))
        report = analyze_mods(self.conn, item)
        a = report.mods[0]
        self.assertEqual(a.tier, 2)
        self.assertEqual(a.tier_count, 9)
        self.assertEqual(a.tier_source, TIER_FROM_POEDB)
        self.assertEqual(a.tier_label, "T2/9")
        self.assertEqual(a.affix_type, "前缀")
        self.assertEqual(a.ilvl, 44)

    def test_不同数值落不同档(self):
        item = parse_item(make_item("项链", ["+122 最大生命", "+92 最大生命"]))
        report = analyze_mods(self.conn, item)
        self.assertEqual([m.tier for m in report.mods], [1, 3])

    def test_roll百分比按库里区间算(self):
        item = parse_item(make_item("项链", ["+114 最大生命"]))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertAlmostEqual(a.roll_pct or -1, 1.0, places=6)

    def test_两个可变位的词缀(self):
        item = parse_item(make_item("弓", ["该装备附加 40 - 78 基础物理伤害"]))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertEqual(a.tier, 1)
        self.assertEqual(a.ranges, [(34.0, 47.0), (72.0, 84.0)])

    def test_数值超出全部档位则未匹配(self):
        """需求 7.9：匹配不上要说清楚，不许挑个最近的档位当答案。"""
        item = parse_item(make_item("项链", ["+999 最大生命"]))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertIsNone(a.tier)
        self.assertEqual(a.tier_label, "未匹配")
        self.assertIn("区间", a.reason)

    def test_库里没有的词缀则未匹配(self):
        item = parse_item(make_item("项链", ["+50 最大魔力"]))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertIsNone(a.tier)
        self.assertIn("没有匹配模板", a.reason)

    def test_类别未知时不给T级(self):
        item = parse_item(make_item("地图", ["+107 最大生命"]))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertIsNone(a.tier)

    def test_前后缀计数(self):
        item = parse_item(make_item("项链", ["+107 最大生命", "+43% 火焰抗性"]))
        report = analyze_mods(self.conn, item)
        self.assertEqual(report.prefix_count, 1)
        self.assertEqual(report.suffix_count, 1)
        self.assertEqual(report.matched_count, 2)


class TestImplicitAndCrafted(ModsTestCase):
    def test_隐式词缀说明来自底材而非猜T级(self):
        text = (
            "物品类别: 项链\n稀有度: 稀有\n测试\n测试底材\n--------\n物品等级: 84\n"
            "--------\n+16 全属性\n--------\n+107 最大生命\n"
        )
        item = parse_item(text)
        report = analyze_mods(self.conn, item)
        implicit = report.mods[0]
        self.assertIsNone(implicit.tier)
        self.assertIn("底材", implicit.reason)

    def test_工艺台Rank不当作掉落T级(self):
        text = (
            'Item Class: Amulets\nRarity: Rare\nTest\nTest Base\n--------\n'
            "Item Level: 84\n--------\n"
            '{ Master Crafted Suffix Modifier "of Craft" (Rank: 3) }\n'
            "+35% to Cold Resistance\n"
        )
        item = parse_item(text)
        a = analyze_mods(self.conn, item).mods[0]
        self.assertIsNone(a.tier)
        self.assertIn("Rank 3", a.reason)
        self.assertIn("不属掉落词缀的 T 级体系", a.reason)


class TestAdvancedCopyTier(ModsTestCase):
    """进阶复制：T 级由游戏给出，优先于查库。"""

    def test_采信文本自带的T级(self):
        text = (
            'Item Class: Bows\nRarity: Rare\nTest\nTest Base\n--------\n'
            "Item Level: 84\n--------\n"
            '{ Prefix Modifier "Fecund" (Tier: 1) — Life }\n'
            "+142(130-144) to maximum Life\n"
        )
        item = parse_item(text)
        a = analyze_mods(self.conn, item).mods[0]
        self.assertEqual(a.tier, 1)
        self.assertEqual(a.tier_source, TIER_FROM_ITEM_TEXT)
        self.assertEqual(a.ranges, [(130.0, 144.0)])
        self.assertAlmostEqual(a.roll_pct or -1, 12 / 14, places=4)
        self.assertIn("未经库查询", a.reason)

    def test_文本自带T级时不受语言限制(self):
        """英文客户端用 Ctrl+Alt+C 一样能拿到 T 级——这是英文路径的正解。"""
        text = (
            'Item Class: Gloves\nRarity: Rare\nTest\nTest Base\n--------\n'
            "Item Level: 84\n--------\n"
            '{ Suffix Modifier "of the Newt" (Tier: 4) }\n'
            "+9(8-10) Life gained on Kill\n"
        )
        item = parse_item(text)
        a = analyze_mods(self.conn, item).mods[0]
        self.assertEqual(a.tier, 4)
        self.assertEqual(a.tier_source, TIER_FROM_ITEM_TEXT)


class TestEnglishBridge(ModsTestCase):
    """英文普通复制走「PoB 阶梯 ⊇ POEDB 阶梯」的指纹桥。"""

    def test_桥接命中并给出T级(self):
        item = parse_item(make_item("Bows", ["Adds 40 to 78 Physical Damage"],
                                    language="en"))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertEqual(a.tier, 1)
        self.assertEqual(a.tier_count, 9)
        self.assertEqual(a.tier_source, TIER_FROM_BRIDGE)
        self.assertEqual(a.ranges, [(34.0, 47.0), (72.0, 84.0)])

    def test_桥接结果与中文路径一致(self):
        """同一件装备的同一条词缀，两种语言必须得到同一个 T 级。"""
        en = analyze_mods(
            self.conn,
            parse_item(make_item("Bows", ["Adds 30 to 65 Physical Damage"], language="en")),
        ).mods[0]
        zh = analyze_mods(
            self.conn,
            parse_item(make_item("弓", ["该装备附加 30 - 65 基础物理伤害"])),
        ).mods[0]
        self.assertEqual(en.tier, zh.tier)
        self.assertEqual(en.ranges, zh.ranges)

    def test_PoB侧没有该词缀则未匹配(self):
        item = parse_item(make_item("Bows", ["+50 to Strength"], language="en"))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertIsNone(a.tier)
        self.assertIn("PoB", a.reason)

    def test_未匹配时提示用进阶复制(self):
        item = parse_item(make_item("Bows", ["+50 to Strength"], language="en"))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertIn("Ctrl+Alt+C", a.reason)

    def test_数值不在任何档位则未匹配(self):
        item = parse_item(make_item("Bows", ["Adds 999 to 999 Physical Damage"],
                                    language="en"))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertIsNone(a.tier)


class TestPageInconsistency(ModsTestCase):
    """同一类别下不同底材页面给出不同 T 级时，不许取其一。"""

    def test_页面间阶梯不一致则不给T级(self):
        # 手动让某个页面的同一档位区间不同，制造页面间冲突
        self.conn.execute(
            """UPDATE mod_affix SET ranges = ?, text_zh = ?, tier = ?
               WHERE item_class_page = 'Gloves_dex' AND tier = 2""",
            [json.dumps([[100.0, 114.0]]), "+(100-114) 最大生命", 7],
        )
        self.conn.commit()
        item = parse_item(make_item("手套", ["+107 最大生命"]))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertIsNone(a.tier)
        self.assertIn("取决于具体底材", a.reason)

    def test_页面间一致时正常给T级(self):
        item = parse_item(make_item("手套", ["+107 最大生命"]))
        a = analyze_mods(self.conn, item).mods[0]
        self.assertEqual(a.tier, 2)
        self.assertIn("底材页面上档位一致", a.reason)


class TestReport(ModsTestCase):
    def test_未匹配清单与引用(self):
        item = parse_item(make_item("项链", ["+107 最大生命", "+50 最大魔力"]))
        report = analyze_mods(self.conn, item)
        self.assertEqual(len(report.unmatched), 1)
        self.assertEqual(report.matched_count, 1)
        self.assertEqual(len(report.citations()), 1)
        self.assertTrue(report.citations()[0][1].startswith("https://"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
