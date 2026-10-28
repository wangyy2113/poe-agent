"""底材注册表泛化到全部装备类别（2026-08-18）。

先前 `kb/sources/pob_bases.py` 只认胸甲：`subType` 必填、`socketLimit` 必须为正、
数值表必须是非空的 `armour`。这些都是照胸甲写的，而腰带没有 subType、戒指没有插槽、
首饰连数值表都没有。泛化时逐条放开，但**不能顺手把真检查也放开**——
「胸甲 socketLimit=0」仍必须整批拒绝，那是脏数据。

这些用例用内存构造的 lua 与 HTML，不读 `data/http_cache`，也不连网。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kb.sources.pob_bases import (
    CATEGORIES,
    CATEGORY_BY_KEY,
    SKIP_NO_PAGE,
    SKIP_PSEUDO,
    SKIP_VARIANT,
    BaseCategory,
    BaseStructureError,
    PoedbPage,
    extract_category_bases,
    parse_poedb_base_names,
    parse_poedb_base_implicits,
)


def page_html(entries: list[tuple[str, str]], cls: str) -> str:
    """按 POEDB 的真实结构生成页面：每个底材两个链接，一个空文本一个带名字。"""
    links = "".join(
        f'<a class="whiteitem {cls}" href="/cn/{slug}"></a>'
        f'<a class="whiteitem {cls}" href="/cn/{slug}">{name}</a>'
        for slug, name in entries
    )
    return f"<html><body>{links}</body></html>"


def implicit_card(slug: str, name: str, text: str, cls: str = "Belt") -> str:
    """POEDB 类别页的具名链接与固有隐式同在 flex-grow-1 卡片中。"""
    return (
        f'<div class="flex-grow-1 ms-2"><a class="whiteitem {cls}" '
        f'href="{slug}">{name}</a><div><div class="separator"></div>'
        f'<div class="implicitMod">{text}</div></div></div>'
    )


def lua_file(body: str) -> Path:
    root = Path(tempfile.mkdtemp())
    target = root / "src" / "Data" / "Bases" / "belt.lua"
    target.parent.mkdir(parents=True)
    target.write_text(body, encoding="utf-8")
    return root


BELT_CATEGORY = BaseCategory(
    "belt", "belt.lua", ("Belt",),
    (PoedbPage("Belts", ("Belt",)),), requires_sockets=False,
)

BELT_LUA = """
itemBases["Rustic Sash"] = {
	type = "Belt",
	tags = { belt = true, default = true, },
	implicit = "(12-24)% increased Global Physical Damage",
	req = { },
}
"""


def _extract(lua: str, cn: list[tuple[str, str]], en: list[tuple[str, str]],
             category: BaseCategory = BELT_CATEGORY):
    root = lua_file(lua)
    pages = {"Belts": (page_html(cn, "Belt"), page_html(en, "Belt"))}
    return extract_category_bases(category, root, pages)


class SocketlessCategoryTest(unittest.TestCase):
    """腰带没有 subType、没有插槽、也没有数值表——三者都不该导致拒绝。"""

    def test_belt_without_subtype_socket_or_stats(self) -> None:
        bases, skipped = _extract(
            BELT_LUA, [("Rustic_Sash", "素布腰带")], [("Rustic_Sash", "Rustic Sash")]
        )
        self.assertEqual([], skipped)
        self.assertEqual(1, len(bases))
        base = bases[0]
        self.assertEqual(("Rustic Sash", "素布腰带"), (base.name_en, base.name_zh))
        self.assertEqual("", base.subtype)
        self.assertEqual(0, base.socket_limit)
        self.assertEqual("none", base.stats_kind)
        self.assertEqual({}, base.stats)
        self.assertEqual("", base.armour_tag, "非护甲类不该有属性护甲标签")
        self.assertEqual("belt", base.category_key)

    def test_socket_requirement_still_enforced_where_it_applies(self) -> None:
        """需要插槽的类别遇到 socketLimit=0 仍必须整批拒绝。"""
        armour_like = BaseCategory(
            "belt", "belt.lua", ("Belt",), (PoedbPage("Belts", ("Belt",)),),
            requires_sockets=True,
        )
        with self.assertRaisesRegex(BaseStructureError, "socketLimit"):
            _extract(
                BELT_LUA, [("Rustic_Sash", "素布腰带")],
                [("Rustic_Sash", "Rustic Sash")], armour_like,
            )


class SkipFamiliesTest(unittest.TestCase):
    def test_pseudo_entry_excluded_without_becoming_a_gap(self) -> None:
        """PoB 伪条目按规则排除：POEDB 没有它们是正确的，不该记成采集缺口。"""
        lua = BELT_LUA + """
itemBases["Random Belt"] = { type = "Belt", tags = { default = true, }, req = { }, }
itemBases["Hidden Belt"] = {
	type = "Belt", hidden = true, tags = { belt = true, default = true, }, req = { },
}
"""
        bases, skipped = _extract(
            lua, [("Rustic_Sash", "素布腰带")], [("Rustic_Sash", "Rustic Sash")]
        )
        self.assertEqual(1, len(bases))
        self.assertEqual({SKIP_PSEUDO}, {s.reason for s in skipped})
        self.assertEqual({"Random Belt", "Hidden Belt"}, {s.name_en for s in skipped})

    def test_variant_expansion_is_labelled_as_such(self) -> None:
        """PoB 把一个 POEDB 底材展开成多个变体时，跳过原因必须是 variant。"""
        lua = BELT_LUA + """
itemBases["Rustic Sash (Fire/Cold)"] = {
	type = "Belt", tags = { belt = true, default = true, }, req = { },
}
"""
        _, skipped = _extract(
            lua, [("Rustic_Sash", "素布腰带")], [("Rustic_Sash", "Rustic Sash")]
        )
        self.assertEqual([SKIP_VARIANT], [s.reason for s in skipped])

    def test_experimental_base_recorded_as_missing_page(self) -> None:
        lua = BELT_LUA + """
itemBases["Mechalarm Belt"] = {
	type = "Belt",
	tags = { belt = true, default = true, experimental_base = true, },
	req = { },
}
"""
        _, skipped = _extract(
            lua, [("Rustic_Sash", "素布腰带")], [("Rustic_Sash", "Rustic Sash")]
        )
        self.assertEqual([SKIP_NO_PAGE], [s.reason for s in skipped])
        self.assertIn("experimental_base", skipped[0].detail)

    def test_ambiguous_slug_variant_is_not_mislabelled(self) -> None:
        """中文页一个 slug 三个变体名时，该 slug 被剔出桥接池。

        此时 PoB 的变体仍必须归为 variant 而不是「POEDB 没这一页」——
        实测异色鞋就是这样被误标的。
        """
        lua = BELT_LUA + """
itemBases["Rustic Sash (Armour/Evasion)"] = {
	type = "Belt", tags = { belt = true, default = true, }, req = { },
}
"""
        # 中文页给 Rustic_Sash 两个变体名 → 无法唯一命名；另留一条正常底材，
        # 否则整页为空会走「页面没有可识别卡片」那条错误路径
        lua += """
itemBases["Chain Belt"] = { type = "Belt", tags = { belt = true, default = true, }, req = { }, }
"""
        bases, skipped = _extract(
            lua,
            [("Rustic_Sash", "素布腰带 (火冰)"), ("Rustic_Sash", "素布腰带 (冰闪)"),
             ("Chain_Belt", "扣链腰带")],
            [("Rustic_Sash", "Rustic Sash"), ("Chain_Belt", "Chain Belt")],
        )
        self.assertEqual(["Chain Belt"], [b.name_en for b in bases],
                         "无法唯一命名的 slug 不得入库")
        reasons = {s.name_en: s.reason for s in skipped}
        self.assertEqual(SKIP_VARIANT, reasons["Rustic Sash (Armour/Evasion)"])


class PoedbPageParsingTest(unittest.TestCase):
    def test_implicit_card_keeps_range_and_line_order(self) -> None:
        html = implicit_card(
            "Rustic_Sash", "素布腰带",
            '全局物理伤害提高 <span class="mod-value">(12<span class="ndash">—</span>24)</span>%'
        )
        self.assertEqual(
            {"Rustic_Sash": "全局物理伤害提高 (12-24)%"},
            parse_poedb_base_implicits(html, ("Belt",)),
        )

    def test_conflicting_implicit_cards_are_not_selected(self) -> None:
        html = (
            implicit_card("Rustic_Sash", "素布腰带", "伤害提高 (12-24)%")
            + implicit_card("Rustic_Sash", "素布腰带", "伤害提高 (20-30)%")
        )
        self.assertEqual({}, parse_poedb_base_implicits(html, ("Belt",)))

    def test_chinese_implicit_requires_same_base_and_numeric_fingerprint(self) -> None:
        root = lua_file(BELT_LUA)
        en = page_html([("Rustic_Sash", "Rustic Sash")], "Belt")
        cn = implicit_card("Rustic_Sash", "素布腰带", "全局物理伤害提高 (12-24)%")
        bases, _ = extract_category_bases(BELT_CATEGORY, root, {"Belts": (cn, en)})
        self.assertEqual("全局物理伤害提高 (12-24)%", bases[0].implicit_zh)
        changed = implicit_card("Rustic_Sash", "素布腰带", "全局物理伤害提高 (20-30)%")
        bases, _ = extract_category_bases(BELT_CATEGORY, root, {"Belts": (changed, en)})
        self.assertIsNone(bases[0].implicit_zh)

    def test_royale_slugs_excluded(self) -> None:
        """Royale 底材的英文名与常规底材完全相同，不剔掉会让常规底材命中 2 个 slug。"""
        html = page_html(
            [("Rustic_Sash", "Rustic Sash"), ("Royale_Rustic_Sash", "Rustic Sash")],
            "Belt",
        )
        names, ambiguous = parse_poedb_base_names(html, ("Belt",), page="Belts")
        self.assertEqual({"Rustic_Sash": "Rustic Sash"}, names)
        self.assertEqual(set(), ambiguous)

    def test_cjk_name_wins_over_english_fallback(self) -> None:
        """中文页对个别 slug 同时给中英标签（实测 Unset_Ring）——取中文那个。"""
        html = page_html(
            [("Unset_Ring", "Unset Ring"), ("Unset_Ring", "潜能之戒")], "Ring"
        )
        names, ambiguous = parse_poedb_base_names(html, ("Ring",), page="Rings")
        self.assertEqual({"Unset_Ring": "潜能之戒"}, names)
        self.assertEqual(set(), ambiguous)

    def test_multiple_cjk_names_are_ambiguous(self) -> None:
        """多个中文变体名无法唯一确定，记为 ambiguous 而不是挑一个。"""
        html = page_html(
            [("Two-Toned_Boots", "异色鞋 (火冰)"), ("Two-Toned_Boots", "异色鞋(冰闪)"),
             ("Iron_Greaves", "铁靴")],
            "Boots",
        )
        names, ambiguous = parse_poedb_base_names(html, ("Boots",), page="Boots")
        self.assertEqual({"Iron_Greaves": "铁靴"}, names)
        self.assertEqual({"Two-Toned_Boots"}, ambiguous)

    def test_page_with_only_ambiguous_slugs_says_so(self) -> None:
        """两种「空」要能分开：抓不到卡片 vs 全部无法唯一命名，排查方向不同。"""
        html = page_html(
            [("Two-Toned_Boots", "异色鞋 (火冰)"), ("Two-Toned_Boots", "异色鞋(冰闪)")],
            "Boots",
        )
        with self.assertRaisesRegex(BaseStructureError, "无法唯一命名"):
            parse_poedb_base_names(html, ("Boots",), page="Boots")

    def test_empty_page_rejected(self) -> None:
        with self.assertRaisesRegex(BaseStructureError, "没有可识别"):
            parse_poedb_base_names("<html></html>", ("Belt",), page="Belts")


class CategoryConfigTest(unittest.TestCase):
    def test_keys_and_pages_unique(self) -> None:
        keys = [c.key for c in CATEGORIES]
        self.assertEqual(len(keys), len(set(keys)))
        pages = [p.slug for c in CATEGORIES for p in c.pages]
        self.assertEqual(len(pages), len(set(pages)), "同一个 POEDB 页面不该挂两个类别")

    def test_only_body_has_armour_tag_pages(self) -> None:
        """按属性分的词缀页目前只有胸甲有——`craft/pool.py` 的模型依赖这一点。"""
        with_pages = [c.key for c in CATEGORIES if c.armour_tag_pages]
        self.assertEqual(["body"], with_pages)

    def test_every_category_declares_item_classes(self) -> None:
        for c in CATEGORIES:
            with self.subTest(category=c.key):
                self.assertTrue(c.item_classes)
                self.assertTrue(c.pages)
                self.assertTrue(c.source_file.startswith("Data/Bases/"))

    def test_body_category_still_requires_sockets(self) -> None:
        self.assertTrue(CATEGORY_BY_KEY["body"].requires_sockets)


class BaseTableMigrationTest(unittest.TestCase):
    """底材表迁移必须幂等 —— 每次启动都重建一遍表不是迁移，是 bug。"""

    def _old_body_only_table(self, conn) -> None:  # noqa: ANN001
        """建一张 body-only 旧结构表（含 name_zh 唯一约束）。"""
        conn.executescript(
            """
            CREATE TABLE pob_base_item (
                base_id TEXT PRIMARY KEY,
                name_en TEXT NOT NULL, name_zh TEXT NOT NULL, poedb_slug TEXT NOT NULL,
                item_class TEXT NOT NULL CHECK(item_class = 'Body Armour'),
                subtype TEXT NOT NULL, socket_limit INTEGER NOT NULL,
                tags TEXT NOT NULL, influence_tags TEXT NOT NULL,
                armour_tag TEXT NOT NULL, modifier_page TEXT NOT NULL,
                implicit_text TEXT, implicit_mod_types TEXT NOT NULL,
                implicit_ids TEXT NOT NULL, defences TEXT NOT NULL,
                requirements TEXT NOT NULL, source_file TEXT NOT NULL,
                source TEXT NOT NULL, source_priority INTEGER NOT NULL,
                pob_url TEXT NOT NULL, poedb_url TEXT NOT NULL,
                source_commit TEXT NOT NULL, pob_version TEXT, game_version TEXT,
                content_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(name_en), UNIQUE(name_zh), UNIQUE(poedb_slug)
            );
            INSERT INTO pob_base_item VALUES(
                'id1','Plate Vest','铁制背心','Plate_Vest','Body Armour','Armour',6,
                '["armour"]','{}','str_armour','Body_Armours_str',NULL,'[]','[]',
                '{"ArmourBaseMin":19}','{"str":12}','Data/Bases/body.lua',
                'Path of Building',2,'https://pob','https://poedb',
                '0123456789012345678901234567890123456789','v','3.29','h','2026-08-18'
            );
            """
        )

    def test_migration_is_idempotent_and_keeps_rows(self) -> None:
        import sqlite3

        from core import db as dbmod

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self._old_body_only_table(conn)
        self.assertTrue(
            dbmod._has_single_column_unique(conn, "pob_base_item", "name_zh")
        )

        dbmod._migrate_base_item_all_categories(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pob_base_item)")}
        self.assertIn("stats", cols)
        self.assertIn("category_key", cols)
        self.assertFalse(
            dbmod._has_single_column_unique(conn, "pob_base_item", "name_zh"),
            "中文名唯一约束必须去掉：POEDB 会把两个底材译成同一个中文名",
        )
        row = conn.execute("SELECT * FROM pob_base_item").fetchone()
        self.assertEqual(("body", "Body_Armours", "armour"),
                         (row["category_key"], row["poedb_page"], row["stats_kind"]))
        self.assertEqual('{"ArmourBaseMin":19}', row["stats"], "defences 应原样搬进 stats")

        # 再跑两次：不得再改动表，也不得丢行
        before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='pob_base_item'"
        ).fetchone()[0]
        for _ in range(2):
            dbmod._migrate_base_item_all_categories(conn)
        after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='pob_base_item'"
        ).fetchone()[0]
        self.assertEqual(before, after, "幂等：第二次调用不该重建表")
        self.assertEqual(1, conn.execute("SELECT count(*) FROM pob_base_item").fetchone()[0])
        self.assertEqual(
            [], [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'pob_base_item_%'")],
            "不该残留中间表",
        )
        conn.close()

    def test_detection_does_not_match_sql_comments(self) -> None:
        """按真实索引判而不是匹配建表语句文本。

        SQLite 把注释一起存进 sqlite_master，而本表的注释里就写着那个约束名——
        按文本匹配会永远命中，于是每次启动都重建一次表（实测踩到）。
        """
        import sqlite3

        from core import db as dbmod

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE t (\n"
            "  a TEXT, b TEXT\n"
            "  -- 这里故意写上 UNIQUE(b) 这串字面文本\n"
            ")"
        )
        self.assertFalse(dbmod._has_single_column_unique(conn, "t", "b"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
