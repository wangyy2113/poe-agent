"""PoB 底材 influenceTags 与词缀 spawn_tags 的确定性约束。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from craft.feasibility import analyze_feasibility  # noqa: E402
from craft.influence import resolve_base_profile, resolve_influence  # noqa: E402
from orchestrator import loop  # noqa: E402
from tests.unit.test_craft_feasibility import _add_affix  # noqa: E402
from tests.unit.test_item_mods import fixture_conn  # noqa: E402
from tools.impl.influence import base_influence_profile  # noqa: E402
from tools.impl.craft import check_mod_combination  # noqa: E402


COMMIT = "9e6ea223e5921abd00bab36f246072e353140a58"


def _insert_base(
    conn,
    *,
    name_en: str = "Onyx Amulet",
    name_zh: str = "黑曜护身符",
    item_class: str = "Amulet",
    category_key: str = "amulet",
    poedb_page: str = "Amulets",
    poedb_slug: str = "Onyx_Amulet",
    tags: tuple[str, ...] = ("amulet", "default"),
    influence_tags: dict[str, str] | None = None,
) -> None:
    influence_tags = influence_tags if influence_tags is not None else {
        "shaper": "amulet_shaper",
        "elder": "amulet_elder",
        "adjudicator": "amulet_adjudicator",
        "basilisk": "amulet_basilisk",
        "crusader": "amulet_crusader",
        "eyrie": "amulet_eyrie",
        "cleansing": "amulet_cleansing",
        "tangle": "amulet_tangle",
    }
    conn.execute(
        """
        INSERT INTO pob_base_item(
            base_id,name_en,name_zh,poedb_slug,category_key,poedb_page,item_class,
            subtype,socket_limit,tags,influence_tags,armour_tag,modifier_page,
            implicit_text,implicit_mod_types,implicit_ids,stats,stats_kind,
            requirements,source_file,source,source_priority,pob_url,poedb_url,
            source_commit,pob_version,game_version,content_hash,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        (
            f"base:{name_en}", name_en, name_zh, poedb_slug, category_key,
            poedb_page, item_class, "", 0, json.dumps(tags),
            json.dumps(influence_tags), "", "", None, "[]", "[]", "{}", "none",
            "{}", f"Data/Bases/{category_key}.lua", "Path of Building", 2,
            f"https://github.com/PathOfBuildingCommunity/PathOfBuilding/blob/{COMMIT}/src/Data/Bases/{category_key}.lua",
            f"https://poedb.tw/cn/{poedb_slug}", COMMIT, "2.66.2", "3.29",
            f"hash:{name_en}",
        ),
    )
    conn.commit()


class TestBaseInfluenceProfile(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fixture_conn()
        _insert_base(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_中英文别名映射到PoB内部键(self) -> None:
        self.assertEqual(resolve_influence("猎人").key, "basilisk")
        self.assertEqual(resolve_influence("Hunter").category, "hunter")
        self.assertEqual(resolve_influence("督军").key, "adjudicator")

    def test_逐字底材返回六种传统影响生成标签和三条来源(self) -> None:
        with patch("tools.impl.influence.dbmod.get_conn", return_value=self.conn):
            result = base_influence_profile("黑曜护身符", "猎人")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["found"])
        self.assertTrue(result.data["requested_influence"]["supported"])
        self.assertEqual(
            result.data["requested_influence"]["generation_tag"], "amulet_basilisk"
        )
        self.assertEqual(len(result.data["supported_influences"]), 6)
        self.assertEqual(len(result.citations), 3)
        self.assertTrue(all(citation["url"] for citation in result.citations))

    def test_无影响标签的底材明确返回不支持(self) -> None:
        _insert_base(
            self.conn,
            name_en="Small Life Flask",
            name_zh="小型生命药剂",
            item_class="Flask",
            category_key="flask",
            poedb_page="Flasks",
            poedb_slug="Small_Life_Flask",
            tags=("default", "flask", "life_flask"),
            influence_tags={},
        )
        with patch("tools.impl.influence.dbmod.get_conn", return_value=self.conn):
            result = base_influence_profile("Small Life Flask", "塑界者")
        self.assertFalse(result.data["requested_influence"]["supported"])
        self.assertEqual(result.data["supported_influences"], [])

    def test_聊天路由可从库内逐字抽出底材和影响(self) -> None:
        question = "黑曜护身符能有猎人影响吗"
        with patch("core.db.get_conn", return_value=self.conn):
            self.assertEqual(
                loop._base_influence_args(question),
                {"base_type": "黑曜护身符", "influence": "狩猎者"},
            )
        self.assertEqual(loop._preferred_tool_route(question), "base_influence_profile")

    def test_底材名歧义时拒绝任选一条(self) -> None:
        _insert_base(
            self.conn,
            name_en="Other Amulet",
            name_zh="黑曜护身符",
            poedb_slug="Other_Amulet",
        )
        profile, reason = resolve_base_profile(self.conn, "黑曜护身符")
        self.assertIsNone(profile)
        self.assertIn("命中 2 条", reason)


class TestInfluenceAwareFeasibility(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fixture_conn()
        _insert_base(self.conn)
        self.conn.execute(
            "UPDATE mod_affix SET spawn_tags=? WHERE item_class_page='Amulets' "
            "AND category='normal'",
            (json.dumps(["amulet", "default"]),),
        )
        _add_affix(
            self.conn,
            page="Amulets",
            group="HunterChaosSkill",
            text="+(1-2) 混沌技能石等级",
            category="hunter",
            category_label="狩猎者",
        )
        self.conn.execute(
            "UPDATE mod_affix SET ilvl=68, spawn_tags=? "
            "WHERE correct_group='HunterChaosSkill'",
            (json.dumps(["amulet_basilisk", "default"]),),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_具体底材影响与物等闭环后可以判possible(self) -> None:
        report = analyze_feasibility(
            self.conn,
            "项链",
            [
                {"query": "最大生命"},
                {"query": "混沌技能石等级", "source": "hunter"},
            ],
            rarity="稀有",
            influences=["猎人"],
            base_type="黑曜护身符",
            item_level=84,
        )
        self.assertEqual(report.verdict, "possible")
        self.assertEqual(report.pages, ["Amulets"])
        self.assertFalse(report.gaps)
        influence = next(
            check
            for check in report.page_analyses[0].checks
            if check.key == "influence_requirement"
        )
        self.assertEqual(influence.status, "pass")
        self.assertIn("amulet_basilisk", influence.summary)
        self.assertFalse(
            any(check.key == "source_requirement" for check in report.page_analyses[0].checks)
        )

    def test_工具证据同时包含底材规则与确定性求解(self) -> None:
        with patch("tools.impl.craft.dbmod.get_conn", return_value=self.conn):
            result = check_mod_combination(
                targets=[
                    {"query": "最大生命"},
                    {"query": "混沌技能石等级", "source": "hunter"},
                ],
                rarity="稀有",
                influences=["猎人"],
                base_type="黑曜护身符",
                item_level=84,
            )
        chunk_ids = [citation["chunk_id"] for citation in result.citations]
        self.assertTrue(any(value.startswith("craft_base:") for value in chunk_ids))
        self.assertTrue(
            any(value.startswith("craft_influence_rule:") for value in chunk_ids)
        )
        self.assertTrue(any(value.startswith("craft_solver:") for value in chunk_ids))
        self.assertEqual(result.data["item_class"], "Amulets")
        precision = loop._precision_citations("check_mod_combination", result)
        self.assertTrue(
            any(citation["chunk_id"].startswith("craft_base:") for citation in precision)
        )

    def test_未给具体底材时影响规则仍保持unknown(self) -> None:
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "最大生命"}, {"query": "火焰抗性"}],
            rarity="稀有",
            influences=["猎人"],
        )
        self.assertEqual(report.verdict, "unknown")
        self.assertTrue(any(gap.code == "influence_requirement" for gap in report.gaps))

    def test_物等过滤不会把高等级候选冒充可用(self) -> None:
        report = analyze_feasibility(
            self.conn,
            "项链",
            [
                {"query": "最大生命"},
                {"query": "混沌技能石等级", "source": "hunter"},
            ],
            rarity="稀有",
            influences=["猎人"],
            base_type="Onyx Amulet",
            item_level=67,
        )
        self.assertEqual(report.verdict, "unknown")
        self.assertTrue(any(gap.code == "target_not_resolved" for gap in report.gaps))

    def test_底材与类别矛盾时不跨类别套用(self) -> None:
        report = analyze_feasibility(
            self.conn,
            "弓",
            [{"query": "物理伤害"}, {"query": "最大生命"}],
            rarity="稀有",
            base_type="黑曜护身符",
        )
        self.assertEqual(report.verdict, "unknown")
        self.assertTrue(any(gap.code == "base_item_class" for gap in report.gaps))


if __name__ == "__main__":
    unittest.main(verbosity=2)
