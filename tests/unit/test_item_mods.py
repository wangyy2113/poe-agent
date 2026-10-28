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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from parsers.item_mods import (  # noqa: E402
    TIER_FROM_BRIDGE,
    TIER_FROM_ITEM_TEXT,
    TIER_FROM_POEDB,
    analyze_mods,
    refine_item_class_with_base,
    resolve_item_class,
)
from core.db import SCHEMA as _REAL_SCHEMA  # noqa: E402
from kb.sources.poedb_mods import loose_templatize  # noqa: E402
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
#: 真实数据：POEDB 项链工艺台「所有元素抗性 / 召唤生物元素抗性」
#: （2026-10 本地库）。两个数值位必须同时落在同一档区间。
_AMULET_MIXED_RES = [
    (1, 2, 60, [[7, 9], [9, 12]],
     "+(7-9)% 所有元素抗性 / 召唤生物获得 +(9-12)% 元素抗性"),
    (2, 2, 30, [[4, 6], [5, 8]],
     "+(4-6)% 所有元素抗性 / 召唤生物获得 +(5-8)% 元素抗性"),
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
        # 宽松模板还要占位固定数字；真实导入器也这样处理。
        loose = loose_templatize(loose)
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


def _insert_base(
    conn: sqlite3.Connection, *, name_en: str, name_zh: str,
    category: str, poedb_page: str, item_class: str,
    tags: list[str], armour_tag: str = "", modifier_page: str = "",
    implicit_text: str | None = None,
) -> None:
    """用真 schema 放一条双来源底材桥，模拟已入库的逐字名称与属性标签。"""
    slug = name_en.replace(" ", "_")
    values = {
        "base_id": f"test:{slug}",
        "name_en": name_en,
        "name_zh": name_zh,
        "poedb_slug": slug,
        "category_key": category,
        "poedb_page": poedb_page,
        "item_class": item_class,
        "subtype": "",
        "socket_limit": 4,
        "tags": json.dumps(tags),
        "influence_tags": "{}",
        "armour_tag": armour_tag,
        "modifier_page": modifier_page,
        "implicit_text": implicit_text,
        "implicit_mod_types": "[]",
        "implicit_ids": "[]",
        "stats": "{}",
        "stats_kind": "none",
        "requirements": "{}",
        "source_file": "Data/Bases/test.lua",
        "source": "Path of Building",
        "source_priority": 2,
        "pob_url": f"https://github.com/PathOfBuildingCommunity/PathOfBuilding/blob/test/{slug}",
        "poedb_url": f"https://poedb.tw/cn/{slug}",
        "source_commit": "a" * 40,
        "pob_version": "test",
        "game_version": "test",
        "content_hash": "test-hash",
        "updated_at": "2026-10-28",
    }
    columns = ",".join(values)
    params = ",".join("?" for _ in values)
    conn.execute(f"INSERT INTO pob_base_item({columns}) VALUES ({params})", list(values.values()))


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


def make_item(class_line: str, mods: list[str], *, language: str = "zh", ilvl: int = 84,
              base_type: str = "测试底材") -> str:
    header = "物品类别" if language == "zh" else "Item Class"
    rarity = "稀有度: 稀有" if language == "zh" else "Rarity: Rare"
    lvl = "物品等级" if language == "zh" else "Item Level"
    name = (f"测试物品\n{base_type}" if language == "zh"
            else f"Test Item\n{base_type}")
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


class TestBasePageResolution(ModsTestCase):
    """具体底材只在逐字桥与类别页双重一致时，缩窄词缀查询页面。"""

    def setUp(self) -> None:
        super().setUp()
        _insert_base(
            self.conn, name_en="Vaal Gauntlets", name_zh="瓦尔护手",
            category="gloves", poedb_page="Gloves", item_class="Gloves",
            tags=["armour", "gloves", "str_armour"],
        )

    def test_具体手套底材把跨页歧义缩到力量页(self):
        # 同一生命数值在敏捷页被改为 T7，类别查询会拒绝；力量底材能定 T2。
        self.conn.execute(
            "UPDATE mod_affix SET tier=7 WHERE item_class_page='Gloves_dex' AND tier=2"
        )
        broad = analyze_mods(self.conn, parse_item(make_item("手套", ["+107 最大生命"])))
        self.assertIsNone(broad.mods[0].tier)
        item = parse_item(make_item("手套", ["+107 最大生命"], base_type="瓦尔护手"))
        report = analyze_mods(self.conn, item)
        self.assertEqual(report.item_class.pages, ["Gloves_str"])
        self.assertEqual(report.item_class.matched_by, "base")
        self.assertEqual(report.mods[0].tier, 2)
        self.assertEqual(report.mods[0].url, "https://poedb.tw/cn/Gloves_str")
        self.assertIn("从 3 个类别页面缩到 1 个", report.warnings[0])

    def test_英文底材名同样逐字命中(self):
        ref = refine_item_class_with_base(
            self.conn, resolve_item_class(self.conn, "Gloves"), "vaal gauntlets"
        )
        self.assertEqual(ref.pages, ["Gloves_str"])
        self.assertEqual(ref.base_evidence[0].name_en, "Vaal Gauntlets")

    def test_重名底材若页面相同可定页面但不选择某一件(self):
        for name in ("Silk Gloves", "Fingerless Silk Gloves"):
            _insert_base(
                self.conn, name_en=name, name_zh="丝绸手套",
                category="gloves", poedb_page="Gloves", item_class="Gloves",
                tags=["armour", "gloves", "int_armour"],
            )
        ref = refine_item_class_with_base(
            self.conn, resolve_item_class(self.conn, "手套"), "丝绸手套"
        )
        self.assertEqual(ref.pages, ["Gloves_int"])
        self.assertEqual(len(ref.base_evidence), 2)
        self.assertIn("没有擅自选择某一件", ref.base_note)

    def test_重名底材指向不同页面时保留候选页不选一个(self):
        for name, tag in (("Twin A", "str_armour"), ("Twin B", "dex_armour")):
            _insert_base(
                self.conn, name_en=name, name_zh="同名护手",
                category="gloves", poedb_page="Gloves", item_class="Gloves",
                tags=["armour", "gloves", tag],
            )
        ref = refine_item_class_with_base(
            self.conn, resolve_item_class(self.conn, "手套"), "同名护手"
        )
        self.assertEqual(ref.pages, ["Gloves_dex", "Gloves_str"])
        self.assertEqual(ref.matched_by, "base_candidates")

    def test_类别与已识别底材矛盾时不给T级(self):
        item = parse_item(make_item("项链", ["+107 最大生命"], base_type="瓦尔护手"))
        report = analyze_mods(self.conn, item)
        self.assertEqual(report.item_class.pages, [])
        self.assertEqual(report.item_class.matched_by, "base_conflict")
        self.assertIsNone(report.mods[0].tier)
        self.assertIn("不属于装备文本类别", report.warnings[0])

    def test_显式页面与底材属性页冲突时不给T级(self):
        ref = refine_item_class_with_base(
            self.conn, resolve_item_class(self.conn, "Gloves_dex"), "Vaal Gauntlets"
        )
        self.assertFalse(ref.ok)
        self.assertEqual(ref.matched_by, "base_conflict")

    def test_武器两源类别拼法不同但实际词缀页相同可核实(self):
        _insert_affix(
            self.conn, page="One_Hand_Axes", code="One Hand Axe", zh="单手斧",
            group="WeaponLife", gen_label="前缀", ladder=_AMULET_LIFE,
        )
        _insert_base(
            self.conn, name_en="Arming Axe", name_zh="武装斧",
            category="axe", poedb_page="One_Hand_Axes", item_class="One Handed Axe",
            tags=["weapon", "axe", "one_hand_weapon"],
        )
        ref = refine_item_class_with_base(
            self.conn, resolve_item_class(self.conn, "One Hand Axes"), "Arming Axe"
        )
        self.assertEqual(ref.pages, ["One_Hand_Axes"])
        self.assertEqual(ref.matched_by, "base")

    def test_药剂没有逐底材页面桥时保留现有类别查询(self):
        _insert_affix(
            self.conn, page="Life_Flasks", code="LifeFlask", zh="生命药剂",
            group="FlaskLife", gen_label="前缀", ladder=_AMULET_LIFE,
        )
        _insert_base(
            self.conn, name_en="Divine Life Flask", name_zh="不朽生命药剂",
            category="flask", poedb_page="Flasks", item_class="Flask",
            tags=["flask", "life_flask"],
        )
        ref = refine_item_class_with_base(
            self.conn, resolve_item_class(self.conn, "Life Flasks"), "Divine Life Flask"
        )
        self.assertEqual(ref.pages, ["Life_Flasks"])
        self.assertEqual(ref.matched_by, "page")

    def test_已识别的Ward底材没有可验证页面时不退回跨页(self):
        _insert_base(
            self.conn, name_en="Runic Gloves", name_zh="符文手套",
            category="gloves", poedb_page="Gloves", item_class="Gloves",
            tags=["armour", "gloves", "ward_armour"],
        )
        item = parse_item(make_item("手套", ["+107 最大生命"], base_type="符文手套"))
        report = analyze_mods(self.conn, item)
        self.assertFalse(report.item_class.ok)
        self.assertIsNone(report.mods[0].tier)
        self.assertIn("无法唯一映射", report.item_class.reason)

    def test_未入库底材维持类别查询而不虚构映射(self):
        ref = refine_item_class_with_base(
            self.conn, resolve_item_class(self.conn, "手套"), "不存在的手套"
        )
        self.assertEqual(len(ref.pages), 3)
        self.assertEqual(ref.base_evidence, [])
        self.assertEqual(ref.matched_by, "zh")

    def test_胸甲既有modifier_page必须与标签推导一致(self):
        _insert_affix(
            self.conn, page="Body_Armours_str", code="Body Armour", zh="胸甲",
            group="BodyLife", gen_label="前缀", ladder=_AMULET_LIFE,
        )
        _insert_base(
            self.conn, name_en="Glorious Plate", name_zh="荣耀战铠",
            category="body", poedb_page="Body_Armours", item_class="Body Armour",
            tags=["armour", "body_armour", "str_armour"],
            armour_tag="str_armour", modifier_page="Body_Armours_str",
        )
        ref = refine_item_class_with_base(
            self.conn, resolve_item_class(self.conn, "胸甲"), "荣耀战铠"
        )
        self.assertEqual(ref.pages, ["Body_Armours_str"])
        self.conn.execute(
            "UPDATE pob_base_item SET modifier_page='Body_Armours_dex' "
            "WHERE name_en='Glorious Plate'"
        )
        bad = refine_item_class_with_base(
            self.conn, resolve_item_class(self.conn, "胸甲"), "荣耀战铠"
        )
        self.assertFalse(bad.ok)


class TestBaseImplicitEvidence(ModsTestCase):
    """PoB 只提供底材固有隐式原文与区间，不提供隐式 T 级。"""

    def setUp(self) -> None:
        super().setUp()
        _insert_affix(
            self.conn, page="Body_Armours_str", code="Body Armour", zh="胸甲",
            group="BodyLife", gen_label="前缀", ladder=_AMULET_LIFE,
        )
        _insert_base(
            self.conn, name_en="Astral Plate", name_zh="星芒战铠",
            category="body", poedb_page="Body_Armours", item_class="Body Armour",
            tags=["armour", "body_armour", "str_armour"],
            armour_tag="str_armour", modifier_page="Body_Armours_str",
            implicit_text="+(8-12)% to all Elemental Resistances",
        )

    def analyze_implicit(self, line: str, *, base_type: str = "Astral Plate"):
        item = parse_item(make_item(
            "Body Armours", [line], language="en", base_type=base_type
        ))
        self.assertEqual(len(item.implicits), 1)
        return analyze_mods(self.conn, item).mods[0]

    def test_英文底材固有隐式可核实区间和roll但不给T级(self):
        mod = self.analyze_implicit("+10% to all Elemental Resistances (implicit)")
        self.assertIsNone(mod.tier)
        self.assertEqual(mod.ranges, [(8.0, 12.0)])
        self.assertAlmostEqual(mod.roll_pct or -1, 0.5)
        self.assertEqual(mod.source_label, "PoB 底材固有隐式")
        self.assertIn("Astral_Plate", mod.url)
        self.assertIn("PoB 未给隐式 T 级", mod.reason)

    def test_隐式数值超出底材区间不报告来源区间(self):
        mod = self.analyze_implicit("+99% to all Elemental Resistances (implicit)")
        self.assertIsNone(mod.tier)
        self.assertEqual(mod.ranges, [])
        self.assertEqual(mod.url, "")

    def test_底材不在注册表时不借其它底材的隐式(self):
        mod = self.analyze_implicit(
            "+10% to all Elemental Resistances (implicit)", base_type="Unknown Plate"
        )
        self.assertEqual(mod.ranges, [])
        self.assertEqual(mod.url, "")

    def test_固定值隐式逐字一致时来源可核但没有roll(self):
        _insert_base(
            self.conn, name_en="Assassin's Garb", name_zh="暗影之装",
            category="body", poedb_page="Body_Armours", item_class="Body Armour",
            tags=["armour", "body_armour", "dex_armour"],
            armour_tag="dex_armour", modifier_page="Body_Armours_dex",
            implicit_text="3% increased Movement Speed",
        )
        _insert_affix(
            self.conn, page="Body_Armours_dex", code="Body Armour", zh="胸甲",
            group="BodyDex", gen_label="前缀", ladder=_AMULET_LIFE,
        )
        mod = self.analyze_implicit(
            "3% increased Movement Speed (implicit)", base_type="Assassin's Garb"
        )
        self.assertIsNone(mod.tier)
        self.assertEqual(mod.ranges, [])
        self.assertIsNone(mod.roll_pct)
        self.assertTrue(mod.url)
        self.assertIn("逐字吻合", mod.reason)

    def test_多行底材定义不靠普通复制的第一行局部匹配(self):
        self.conn.execute(
            "UPDATE pob_base_item SET implicit_text=? WHERE name_en='Astral Plate'",
            ["+(8-12)% to all Elemental Resistances\nYour Hits have 10% increased Damage"],
        )
        mod = self.analyze_implicit("+10% to all Elemental Resistances (implicit)")
        self.assertEqual(mod.ranges, [])
        self.assertEqual(mod.url, "")

    def test_同名底材隐式原文不一致时不任选一个(self):
        _insert_base(
            self.conn, name_en="Other Plate", name_zh="星芒战铠",
            category="body", poedb_page="Body_Armours", item_class="Body Armour",
            tags=["armour", "body_armour", "str_armour"],
            armour_tag="str_armour", modifier_page="Body_Armours_str",
            implicit_text="+(20-25)% to all Elemental Resistances",
        )
        mod = self.analyze_implicit(
            "+10% to all Elemental Resistances (implicit)", base_type="星芒战铠"
        )
        self.assertEqual(mod.ranges, [])
        self.assertEqual(mod.url, "")

    def test_类别底材矛盾时隐式原因保留冲突而不报范围(self):
        item = parse_item(make_item(
            "Amulets", ["+10% to all Elemental Resistances (implicit)"],
            language="en", base_type="Astral Plate",
        ))
        analysis = analyze_mods(self.conn, item).mods[0]
        self.assertIsNone(analysis.tier)
        self.assertEqual(analysis.ranges, [])
        self.assertIn("类别与底材矛盾", analysis.reason)


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

    def test_无Tier头下多行整体失配不能借首行给T级(self):
        text = make_item("项链", [
            '{ Prefix Modifier "Unknown Mixed" }',
            "+107(100-114) 最大生命",
            "未知的第二行",
        ])
        item = parse_item(text)
        self.assertTrue(item.advanced_copy)
        self.assertEqual(len(item.all_mods), 1)
        self.assertTrue(item.all_mods[0].is_multiline)
        analysis = analyze_mods(self.conn, item).mods[0]
        self.assertIsNone(analysis.tier)
        self.assertEqual(analysis.tier_label, "未匹配")
        self.assertEqual(analysis.affix_type, "前缀")  # 方向来自游戏头，仍可信
        self.assertIn("没有匹配模板", analysis.reason)


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


class TestPlainCopyMixedAffix(ModsTestCase):
    """普通复制只在完整、唯一的库证据下合并相邻混合词缀行。"""

    def setUp(self) -> None:
        super().setUp()
        _insert_affix(
            self.conn, page="Amulets", code="Amulet", zh="项链",
            group="AllResistances", gen_label="后缀", ladder=_AMULET_MIXED_RES,
            category="master", category_label="工艺工作台",
        )
        self.conn.commit()

    @staticmethod
    def mixed_lines(first: str = "+5% 所有元素抗性",
                    second: str = "召唤生物获得 +7% 元素抗性") -> list[str]:
        return [first, second]

    def test_真实工艺台双行按完整区间合成一条(self):
        item = parse_item(make_item("项链", self.mixed_lines()))
        original = [(m.text, m.raw, list(m.values)) for m in item.all_mods]
        report = analyze_mods(self.conn, item)
        self.assertEqual(len(report.mods), 1)
        mod = report.mods[0]
        self.assertEqual(mod.mod.lookup_text,
                         "+5% 所有元素抗性 / 召唤生物获得 +7% 元素抗性")
        self.assertEqual(mod.mod.values, [5.0, 7.0])
        self.assertEqual(mod.mod.raw,
                         "+5% 所有元素抗性\n召唤生物获得 +7% 元素抗性")
        self.assertEqual(mod.tier, 2)
        self.assertEqual(mod.tier_source, TIER_FROM_POEDB)
        self.assertEqual(mod.affix_type, "后缀")
        self.assertEqual(report.suffix_count, 1)
        self.assertAlmostEqual(mod.roll_pct or -1, (1 / 2 + 2 / 3) / 2)
        self.assertIn("合并分析", report.warnings[0])
        # 查库分析不得偷偷改写无库的纯文本解析结果。
        self.assertEqual([(m.text, m.raw, m.values) for m in item.all_mods], original)

    def test_第一行数值换档时必须两行同档(self):
        item = parse_item(make_item("项链", self.mixed_lines(
            "+8% 所有元素抗性", "召唤生物获得 +11% 元素抗性")))
        mod = analyze_mods(self.conn, item).mods[0]
        self.assertEqual(mod.tier, 1)
        self.assertEqual(mod.ranges, [(7.0, 9.0), (9.0, 12.0)])

    def test_两组互不重叠的混合词缀分别合并(self):
        # POEDB 项链工艺台另一个真实定义：生命 / 固定每秒回魔。
        _insert_affix(
            self.conn, page="Amulets", code="Amulet", zh="项链",
            group="LifeManaRegen", gen_label="前缀",
            ladder=[(4, 6, 30, [[28, 33]], "+(28-33) 最大生命 / 每秒再生 2 魔力")],
            category="master", category_label="工艺工作台",
        )
        report = analyze_mods(self.conn, parse_item(make_item("项链", [
            "+30 最大生命", "每秒再生 2 魔力", *self.mixed_lines(),
        ])))
        self.assertEqual(len(report.mods), 2)
        self.assertEqual([m.tier for m in report.mods], [4, 2])
        self.assertEqual((report.prefix_count, report.suffix_count), (1, 1))
        self.assertEqual(sum("合并分析" in w for w in report.warnings), 2)

    def test_任一数值超出完整区间则保留两行且不猜T级(self):
        item = parse_item(make_item("项链", self.mixed_lines(
            "+5% 所有元素抗性", "召唤生物获得 +99% 元素抗性")))
        report = analyze_mods(self.conn, item)
        self.assertEqual(len(report.mods), 2)
        self.assertTrue(all(m.tier is None for m in report.mods))
        self.assertFalse(any("合并分析" in w for w in report.warnings))

    def test_相邻两行都能独立成立则不擅自合并(self):
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
        report = analyze_mods(self.conn, parse_item(make_item("项链", self.mixed_lines())))
        self.assertEqual(len(report.mods), 2)
        self.assertEqual([m.mod.text for m in report.mods], self.mixed_lines())
        self.assertTrue(all(m.tier is None and m.ambiguous for m in report.mods))
        self.assertEqual(report.suffix_count, 0)
        self.assertIn("无法唯一确认", report.warnings[0])

    def test_拼接结果跨来源组有歧义则不合并(self):
        _insert_affix(
            self.conn, page="Amulets", code="Amulet", zh="项链",
            group="OtherMixedRes", gen_label="后缀", ladder=_AMULET_MIXED_RES,
            category="essence", category_label="精华",
        )
        report = analyze_mods(self.conn, parse_item(make_item("项链", self.mixed_lines())))
        self.assertEqual(len(report.mods), 2)
        self.assertTrue(all(m.tier is None for m in report.mods))
        self.assertFalse(any("合并分析" in w for w in report.warnings))

    def test_其他方向的隐式候选不能拿来合并显式行(self):
        # POEDB 项链真实腐化隐式模板；普通显式段没有隐式旗标时不可借它定档。
        _insert_affix(
            self.conn, page="Amulets", code="Amulet", zh="项链",
            group="CorruptedAttributes", gen_label="其他",
            ladder=[(1, 1, 1, [[4, 6], [4, 6]],
                     "力量提高 (4-6)% / 敏捷提高 (4-6)%")],
            category="corrupted", category_label="腐化",
        )
        report = analyze_mods(self.conn, parse_item(make_item("项链", [
            "力量提高 5%", "敏捷提高 5%",
        ])))
        self.assertEqual(len(report.mods), 2)
        self.assertTrue(all(m.tier is None for m in report.mods))
        self.assertFalse(any("合并分析" in w for w in report.warnings))

    def test_重叠的两种分组都可行时不选择一边(self):
        for group, text in (
            ("FirstPair", "第 (1-3) 行 / 第 (4-6) 行"),
            ("SecondPair", "第 (4-6) 行 / 第 (7-9) 行"),
        ):
            _insert_affix(
                self.conn, page="Amulets", code="Amulet", zh="项链",
                group=group, gen_label="前缀",
                ladder=[(1, 1, 1, [[1, 3], [4, 6]] if group == "FirstPair"
                         else [[4, 6], [7, 9]], text)],
            )
        report = analyze_mods(self.conn, parse_item(make_item(
            "项链", ["第 2 行", "第 5 行", "第 8 行"])))
        self.assertEqual(len(report.mods), 3)
        self.assertTrue(all(m.tier is None for m in report.mods))
        self.assertFalse(any("合并分析" in w for w in report.warnings))

    def test_真实三行词缀也只占一个位置(self):
        # POEDB Convoking_Wand 的真实三行定义，两个固定数字不计入可变区间。
        _insert_affix(
            self.conn, page="Convoking_Wand", code="Wand", zh="召集法杖",
            group="WeaponCasterDamagePrefix", gen_label="前缀",
            ladder=[(1, 1, 50, [[70, 74]],
                     "使用技能时触发一个插入的法术，它有 4 秒冷却时间 / "
                     "通过这种方式触发的法术消耗总增 150% / 法术伤害提高 (70-74)%")],
            category="incursion", category_label="神庙",
        )
        lines = [
            "使用技能时触发一个插入的法术，它有 4 秒冷却时间",
            "通过这种方式触发的法术消耗总增 150%",
            "法术伤害提高 72%",
        ]
        report = analyze_mods(self.conn, parse_item(make_item("召集法杖", lines)))
        self.assertEqual(len(report.mods), 1)
        self.assertEqual(report.mods[0].tier, 1)
        self.assertEqual(report.mods[0].mod.values, [4.0, 150.0, 72.0])
        self.assertEqual(report.prefix_count, 1)

    def test_三行与后两行都能组成词缀时不猜分组(self):
        # 真实库里这三行本身是一条隐匿词缀，但后两行也有独立词缀定义。
        # 无词缀头的普通复制不能断言哪种分组正确。
        _insert_affix(
            self.conn, page="Helmets_dex", code="Helmet", zh="头部",
            group="MinionLifeAndLimits", gen_label="前缀",
            ladder=[(1, 1, 1, [[8, 10]],
                     "召唤生物的最大生命提高 (8-10)% / +1 魔卫复苏数量上限 / +1 魔侍数量上限")],
            category="veiled", category_label="隐匿",
        )
        _insert_affix(
            self.conn, page="Helmets_dex", code="Helmet", zh="头部",
            group="MinionLimits", gen_label="前缀",
            ladder=[(1, 1, 1, [], "+1 魔卫复苏数量上限 / +1 魔侍数量上限")],
            category="veiled", category_label="隐匿",
        )
        lines = ["召唤生物的最大生命提高 9%", "+1 魔卫复苏数量上限", "+1 魔侍数量上限"]
        report = analyze_mods(self.conn, parse_item(make_item("头部", lines)))
        self.assertEqual(len(report.mods), 3)
        self.assertTrue(all(m.tier is None for m in report.mods))
        self.assertFalse(any("合并分析" in w for w in report.warnings))

    def test_多底材页面档位冲突不能靠拼接选定T级(self):
        for page, tier in (("Gloves_str", 1), ("Gloves_dex", 2)):
            _insert_affix(
                self.conn, page=page, code="Gloves", zh="手套",
                group="AllResistances", gen_label="后缀",
                ladder=[(tier, 2, 30, [[4, 6], [5, 8]],
                         "+(4-6)% 所有元素抗性 / 召唤生物获得 +(5-8)% 元素抗性")],
                category="master", category_label="工艺工作台",
            )
        report = analyze_mods(self.conn, parse_item(make_item("手套", self.mixed_lines())))
        self.assertEqual(len(report.mods), 2)
        self.assertTrue(all(m.tier is None for m in report.mods))
        self.assertFalse(any("合并分析" in w for w in report.warnings))

    def test_多底材页面同档但前后缀方向不同也不能合并(self):
        # T 级一致不代表容量方向一致；lookup_mod 的同档去重不会检查方向。
        for page, direction in (("Gloves_str", "前缀"), ("Gloves_dex", "后缀")):
            _insert_affix(
                self.conn, page=page, code="Gloves", zh="手套",
                group="AllResistances", gen_label=direction,
                ladder=[(2, 2, 30, [[4, 6], [5, 8]],
                         "+(4-6)% 所有元素抗性 / 召唤生物获得 +(5-8)% 元素抗性")],
                category="master", category_label="工艺工作台",
            )
        report = analyze_mods(self.conn, parse_item(make_item("手套", self.mixed_lines())))
        self.assertEqual(len(report.mods), 2)
        self.assertTrue(all(m.tier is None for m in report.mods))
        self.assertEqual((report.prefix_count, report.suffix_count), (0, 0))

    def test_多底材页面同档但门槛或权重不同不借某一页的数据(self):
        for page, ilvl in (("Gloves_str", 30), ("Gloves_dex", 60)):
            _insert_affix(
                self.conn, page=page, code="Gloves", zh="手套",
                group="AllResistances", gen_label="后缀",
                ladder=[(2, 2, ilvl, [[4, 6], [5, 8]],
                         "+(4-6)% 所有元素抗性 / 召唤生物获得 +(5-8)% 元素抗性")],
                category="master", category_label="工艺工作台",
            )
        report = analyze_mods(self.conn, parse_item(make_item("手套", self.mixed_lines())))
        self.assertEqual(len(report.mods), 2)
        self.assertTrue(all(m.tier is None and m.ilvl is None for m in report.mods))

    def test_跨段及不同旗标不拼接(self):
        separated = make_item("项链", [self.mixed_lines()[0],
                                        "--------", self.mixed_lines()[1]])
        self.assertEqual(len(analyze_mods(self.conn, parse_item(separated)).mods), 2)
        flagged = self.mixed_lines()
        flagged[0] += " (crafted)"
        self.assertEqual(len(analyze_mods(
            self.conn, parse_item(make_item("项链", flagged))).mods), 2)

    def test_进阶复制和英文普通复制不走这条推断路径(self):
        advanced = make_item("项链", [
            '{ Suffix Modifier "One" (Tier: 2) }',
            "+5(4-6)% 所有元素抗性",
            '{ Suffix Modifier "Two" (Tier: 2) }',
            "召唤生物获得 +7(5-8)% 元素抗性",
        ])
        report = analyze_mods(self.conn, parse_item(advanced))
        self.assertEqual(len(report.mods), 2)
        self.assertFalse(any("合并分析" in w for w in report.warnings))
        english = parse_item(make_item("Amulets", [
            "+5% to all Elemental Resistances",
            "Minions have +7% to Elemental Resistances",
        ], language="en"))
        self.assertEqual(len(analyze_mods(self.conn, english).mods), 2)

    def test_三种通货状态入口共享合并后的名额计数(self):
        # 这三个入口直接调用 analyze_mods，不经过 build_report。
        from tools.impl.craft_operation import _state_from_item_text as craft_state
        from tools.impl.eldritch_operation import _state_from_item_text as eldritch_state
        from tools.impl.veiled_operation import _state_from_item_text as veiled_state

        raw = make_item("项链", ["+107 最大生命", *self.mixed_lines()])
        for state_fn, db_path in (
            (craft_state, "tools.impl.craft_operation.dbmod.get_conn"),
            (eldritch_state, "tools.impl.eldritch_operation.dbmod.get_conn"),
            (veiled_state, "tools.impl.veiled_operation.dbmod.get_conn"),
        ):
            with self.subTest(state_fn=state_fn.__module__), patch(
                db_path, return_value=self.conn
            ):
                state = state_fn(raw)
                self.assertEqual((state.prefix_count, state.suffix_count), (1, 1))
                self.assertEqual(state.incomplete_reason, "")


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
