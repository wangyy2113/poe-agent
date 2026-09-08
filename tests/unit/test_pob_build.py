"""PoB 分享码解析（第四十八轮）。

夹具用 PoB 检出里的 `spec/TestBuilds/3.13/*.xml`（5 个真实 build、100 件装备）。
**检出不在时整类跳过而不是失败**：`POB_PATH` 不一定存在，
`scripts/probe_pob.py` 早有这个先例。

这一轮的核心回归是**PoB 存档方言**：那种格式不带 `--------`，改用 `Implicits: N`
标边界，不认它的话 100 件里有 99 件词缀全丢——名字/底材/稀有度都对，
偏偏丢的是后面 T 级匹配与 build 分析要用的那一半。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from parsers.item_text import parse_item  # noqa: E402
from parsers.pob_build import (  # noqa: E402
    PobParseError,
    decode_share_code,
    encode_share_code,
    parse_build,
)

_FIXTURES = Path("../PathOfBuilding/spec/TestBuilds/3.13")


def _fixtures() -> list[Path]:
    base = (Path(__file__).resolve().parents[2] / _FIXTURES).resolve()
    return sorted(base.glob("*.xml")) if base.is_dir() else []


class ShareCodeTest(unittest.TestCase):
    """编码方式取自 PoB `ImportTab.lua:504/631`：zlib deflate + URL-safe base64。"""

    def test_往返一致(self) -> None:
        xml = "<PathOfBuilding><Build level='90' className='Witch'/></PathOfBuilding>"
        self.assertEqual(decode_share_code(encode_share_code(xml)), xml)

    def test_url_safe_字符能解(self) -> None:
        """PoB 把 `+`/`/` 换成 `-`/`_`，解码要换回来。"""
        xml = "<PathOfBuilding><Build level='1'/></PathOfBuilding>" * 20
        code = encode_share_code(xml)
        self.assertNotIn("+", code)
        self.assertNotIn("/", code)
        self.assertEqual(decode_share_code(code), xml)

    def test_链接给出明确指引而不是_base64_报错(self) -> None:
        with self.assertRaises(PobParseError) as ctx:
            decode_share_code("https://pobb.in/abcdef")
        self.assertIn("链接", str(ctx.exception))

    def test_三种失败各给各的原因(self) -> None:
        """「解析失败」四个字对用户没有任何帮助。"""
        with self.assertRaises(PobParseError) as empty:
            decode_share_code("   ")
        self.assertIn("空", str(empty.exception))

        with self.assertRaises(PobParseError) as bad_char:
            decode_share_code("不是base64的中文")
        self.assertIn("base64", str(bad_char.exception))

        with self.assertRaises(PobParseError) as not_zlib:
            decode_share_code("aGVsbG8gd29ybGQ")  # 合法 base64，但不是 zlib
        self.assertIn("zlib", str(not_zlib.exception))

    def test_不是_pob_的_xml_要说清楚(self) -> None:
        with self.assertRaises(PobParseError) as ctx:
            parse_build("<Foo><Bar/></Foo>")
        self.assertIn("PathOfBuilding", str(ctx.exception))

    def test_主技能组与计算技能组不能混为一谈(self) -> None:
        """`mainSocketGroup` 是 Build 主技能，`Calcs.skill_number` 才绑定 PlayerStat。"""
        xml = (
            "<PathOfBuilding><Build level='90' className='Witch' mainSocketGroup='2'>"
            "<PlayerStat stat='CombinedDPS' value='123'/></Build>"
            "<Calcs><Input name='skill_number' number='1'/></Calcs><Skills>"
            "<Skill mainActiveSkill='1'><Gem nameSpec='Arc' skillId='Arc'/></Skill>"
            "<Skill mainActiveSkill='1'><Gem nameSpec='Fireball' skillId='Fireball'/></Skill>"
            "</Skills></PathOfBuilding>"
        )
        build = parse_build(xml)
        self.assertEqual(build.main_group.active_gem.name_en, "Fireball")
        self.assertEqual(build.calcs_group.active_gem.name_en, "Arc")
        self.assertFalse(build.skill_groups[0].is_main)
        self.assertTrue(build.skill_groups[1].is_main)

    def test_主动技能下标跳过辅助宝石(self) -> None:
        xml = (
            "<PathOfBuilding><Build level='90' className='Witch' mainSocketGroup='1'/>"
            "<Skills><Skill mainActiveSkill='2'>"
            "<Gem nameSpec='Added Cold' skillId='SupportAddedCold' "
            "gemId='Metadata/Items/Gems/SupportGemAddedColdDamage'/>"
            "<Gem nameSpec='Arc' skillId='Arc'/><Gem nameSpec='Vaal Arc' skillId='VaalArc'/>"
            "</Skill></Skills></PathOfBuilding>"
        )
        group = parse_build(xml).main_group
        self.assertEqual(group.active_gem.name_en, "Vaal Arc")

    def test_计算页主动技能下标优先(self) -> None:
        xml = (
            "<PathOfBuilding><Build level='90' className='Witch' mainSocketGroup='1'/>"
            "<Calcs><Input name='skill_number' number='1'/></Calcs><Skills>"
            "<Skill mainActiveSkill='1' mainActiveSkillCalcs='2'>"
            "<Gem nameSpec='Arc' skillId='Arc'/><Gem nameSpec='Vaal Arc' skillId='VaalArc'/>"
            "</Skill></Skills></PathOfBuilding>"
        )
        group = parse_build(xml).calcs_group
        self.assertEqual(group.active_gem.name_en, "Arc")
        self.assertEqual(group.calcs_active_gem.name_en, "Vaal Arc")

    def test_新版技能集只读取当前激活的一套(self) -> None:
        xml = (
            "<PathOfBuilding><Build level='90' className='Witch' mainSocketGroup='1'/>"
            "<Skills activeSkillSet='2'>"
            "<SkillSet id='1'><Skill mainActiveSkill='1'>"
            "<Gem nameSpec='Arc' skillId='Arc'/></Skill></SkillSet>"
            "<SkillSet id='2'><Skill mainActiveSkill='1'>"
            "<Gem nameSpec='Fireball' skillId='Fireball'/></Skill></SkillSet>"
            "</Skills></PathOfBuilding>"
        )
        build = parse_build(xml)
        self.assertEqual(len(build.skill_groups), 1)
        self.assertEqual(build.main_group.active_gem.name_en, "Fireball")


class PobItemDialectTest(unittest.TestCase):
    """PoB 存档方言：没有 `--------`，用 `Implicits: N` 标边界。"""

    BODY = (
        "Rarity: RARE\n"
        "Loath Sanctuary\n"
        "Vaal Spirit Shield\n"
        "Item Level: 83\n"
        "Sockets: B-B-B\n"
        "LevelReq: 70\n"
        "Implicits: 1\n"
        "9% increased Spell Damage\n"
        "+54 to Intelligence\n"
        "+87 to maximum Life\n"
        "Corrupted\n"
    )

    def test_隐式与显式按计数切开(self) -> None:
        item = parse_item(self.BODY)
        self.assertEqual([m.text for m in item.implicits], ["9% increased Spell Damage"])
        self.assertEqual(
            [m.text for m in item.explicits],
            ["+54 to Intelligence", "+87 to maximum Life"],
        )

    def test_头部属性不再被吞掉(self) -> None:
        """PoB 把 `Item Level` / `Sockets` 和名字挤在同一段，
        而 `parse_item` 只解析 `sections[1:]` 的属性——不把头部再切一刀就全留空。"""
        item = parse_item(self.BODY)
        self.assertEqual(item.item_level, 83)
        self.assertTrue(item.sockets.raw)
        self.assertEqual(item.requirements.level, 70, "LevelReq 是 PoB 对同一概念的键名")

    def test_旗标仍然识别(self) -> None:
        self.assertTrue(parse_item(self.BODY).flags.corrupted)

    def test_零隐式(self) -> None:
        body = self.BODY.replace("Implicits: 1\n9% increased Spell Damage\n", "Implicits: 0\n")
        item = parse_item(body)
        self.assertEqual(item.implicits, [])
        self.assertEqual(len(item.explicits), 2)

    def test_游戏原文格式不受影响(self) -> None:
        """带 `--------` 的照旧走老路径——同一个解析器认两种方言，不是换掉一种。"""
        game = (
            "Item Class: Gloves\nRarity: RARE\nFoo Bar\nDragonscale Gauntlets\n"
            "--------\nItem Level: 84\n--------\n+42 to maximum Life\n"
        )
        item = parse_item(game)
        self.assertEqual(item.item_class, "Gloves")
        self.assertEqual(item.item_level, 84)
        self.assertEqual([m.text for m in item.explicits], ["+42 to maximum Life"])


class FixtureTest(unittest.TestCase):
    """拿 PoB 自己的 5 个 build 夹具做端到端验证。"""

    def setUp(self) -> None:
        self.files = _fixtures()
        if not self.files:
            self.skipTest("PoB 检出不可用（POB_PATH 未配置），跳过夹具用例")

    def test_五个夹具都能往返并解析(self) -> None:
        for path in self.files:
            with self.subTest(build=path.stem):
                xml = path.read_text(encoding="utf-8")
                self.assertEqual(decode_share_code(encode_share_code(xml)), xml)
                build = parse_build(xml)
                self.assertTrue(build.class_name, "职业必须解析出来")
                self.assertTrue(build.skill_groups, "技能组不能为空")
                self.assertTrue(build.items, "装备不能为空")

    def test_一百件装备全部解析出词缀(self) -> None:
        """**这一轮的核心回归**：改之前 100 件里有 99 件一条词缀都取不到。"""
        total = without_mods = 0
        for path in self.files:
            for item in parse_build(path.read_text(encoding="utf-8")).items:
                total += 1
                self.assertIsNotNone(item.parsed, f"{item.slot} 解析失败：{item.error}")
                if item.parsed is not None and not item.parsed.all_mods:
                    without_mods += 1
        self.assertGreaterEqual(total, 100)
        self.assertEqual(without_mods, 0, "PoB 存档方言下不该有装备丢掉全部词缀")

    def test_playerstat_被原样收下(self) -> None:
        """它是 PoB 自己算的快照——收下但要标清来源（见 `api/build.py`）。"""
        build = parse_build(self.files[0].read_text(encoding="utf-8"))
        self.assertTrue(build.player_stats, "夹具里带着 PlayerStat")
        for value in build.player_stats.values():
            self.assertIsInstance(value, float)


class BridgeTest(unittest.TestCase):
    """宝石桥接：桥不上**留原文并记 warning**，不猜。"""

    def test_桥不上的留原文并告警(self) -> None:
        import sqlite3

        from parsers.pob_build import bridge_gems

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE pob_skill_gem(gem_id TEXT, name_en TEXT, name_zh TEXT)")
        conn.execute("INSERT INTO pob_skill_gem VALUES('IceNova','Ice Nova','冰霜新星')")
        xml = (
            "<PathOfBuilding><Build level='90' className='Witch'/><Skills>"
            "<Skill mainActiveSkill='1'>"
            "<Gem nameSpec='Ice Nova' level='20' quality='20'/>"
            "<Gem nameSpec='Nonexistent Gem' level='1'/>"
            "</Skill></Skills></PathOfBuilding>"
        )
        build = bridge_gems(conn, parse_build(xml))
        gems = build.skill_groups[0].gems
        self.assertEqual(gems[0].gem_id, "IceNova")
        self.assertEqual(gems[0].name_zh, "冰霜新星")
        self.assertIsNone(gems[1].gem_id, "桥不上就是 None")
        self.assertEqual(gems[1].name_en, "Nonexistent Gem", "原文要留着")
        self.assertTrue(any("未能与本库桥接" in w for w in build.warnings))


if __name__ == "__main__":
    unittest.main()


# ── 需求 8.4 体检报告（第四十九轮）────────────────────────────


class RequirementCoverageTest(unittest.TestCase):
    """「检查并报告下列全部项目，**不得遗漏**」是需求原文，靠代码保证。"""

    def test_十六项一个不缺(self) -> None:
        from parsers.build_report import REQUIREMENT_8_4_ITEMS, uncovered_requirements

        self.assertEqual(len(REQUIREMENT_8_4_ITEMS), 16, "需求 8.4 点名 16 项")
        self.assertEqual(uncovered_requirements(), (), "有需求项没有任何检查覆盖")

    def test_判据落在需求那一侧(self) -> None:
        """**不能写成 `REQUIRED = len(_CHECKS)`**——那等于「要求的项数 = 我实现了几项」，
        恒等于自己、永远不会失败。第一版就是这么写的，实测它报 18 而需求是 16。"""
        import inspect

        from parsers import build_report

        src = inspect.getsource(build_report)
        self.assertNotIn("= len(_CHECKS)", src)

    def test_需求_8_5_四类防御补充一个不缺(self) -> None:
        from parsers.build_report import (
            REQUIREMENT_8_5_ITEMS,
            uncovered_defence_requirements,
        )

        self.assertEqual(REQUIREMENT_8_5_ITEMS, ("最大抗性", "吸纳", "偷取", "回复"))
        self.assertEqual(uncovered_defence_requirements(), ())


class DefenceExpansionTest(unittest.TestCase):
    """需求 8.5：只消费 PoB 快照里确实存在的值，缺失时不反推。"""

    def setUp(self) -> None:
        import sqlite3

        from calc import constants
        from core.db import SCHEMA

        constants.reset_cache()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.executemany(
            "INSERT INTO game_constant VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            [
                (key, "pobMisc", None, 0, "scalar", value, None,
                 "Path of Building", 2, "https://example/blob/Data.lua",
                 "PoB test", "test")
                for key, value in (
                    (constants.MAX_RESIST_CAP, 90.0),
                    (constants.BLOCK_CHANCE_CAP, 90.0),
                    (constants.SUPPRESSION_CHANCE_CAP, 100.0),
                )
            ],
        )
        self.conn.commit()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        from calc import constants

        constants.reset_cache()
        self.conn.close()

    def _report(self, stats: dict[str, float]):
        from parsers.build_report import build_health_report
        from parsers.pob_build import PobBuild

        return build_health_report(self.conn, PobBuild(player_stats=stats))

    def test_超抗为正时才能从快照反推最大抗性(self) -> None:
        report = self._report({
            "FireResist": 76, "FireResistOverCap": 12,
            "ColdResist": 75, "ColdResistOverCap": 0,
        })
        fire = report.by_key("fire_max_resist")
        cold = report.by_key("cold_max_resist")
        self.assertTrue(fire.known)
        self.assertEqual(fire.value, "76%")
        self.assertIn("已被上限夹住", fire.verdict)
        self.assertFalse(cold.known, "OverCap=0 时不能猜最大抗性就是当前抗性")
        self.assertEqual(cold.value, "")

    def test_显式最大抗性字段优先(self) -> None:
        item = self._report({"FireResist": 70, "FireResistMax": 79}).by_key(
            "fire_max_resist"
        )
        self.assertTrue(item.known)
        self.assertEqual(item.value, "79%")
        self.assertIn("FireResistMax", item.basis)

    def test_新版法压字段优先于旧字段(self) -> None:
        item = self._report({
            "EffectiveSpellSuppressionChance": 92,
            "SpellSuppressionChance": 40,
        }).by_key("spell_suppression")
        self.assertEqual(item.value, "92%")
        self.assertIn("EffectiveSpellSuppressionChance", item.basis)

    def test_格挡同时展示攻击与法术并兼容新旧字段(self) -> None:
        item = self._report({
            "EffectiveBlockChance": 70,
            "SpellBlockChance": 55,
        }).by_key("block")
        self.assertEqual(item.value, "攻击 70%；法术 55%")
        self.assertIn("EffectiveBlockChance", item.basis)
        self.assertIn("SpellBlockChance", item.basis)

    def test_max_hit_逐伤害类型展示(self) -> None:
        item = self._report({
            "PhysicalMaximumHitTaken": 12000,
            "FireMaximumHitTaken": 30000,
            "ChaosMaximumHitTaken": 8000,
        }).by_key("max_hit")
        self.assertEqual(item.value, "物理 12000；火焰 30000；混沌 8000")
        self.assertNotIn("冰霜", item.value, "缺失类型不能补零或类推")

    def test_偷取字段保留_leech_与_on_hit_合并口径(self) -> None:
        item = self._report({
            "LifeLeechGainRate": 120.5,
            "ManaLeechGainRate": 0,
            "EnergyShieldLeechGainRate": 8,
        }).by_key("leech")
        self.assertTrue(item.known)
        self.assertIn("生命 120.5/秒", item.value)
        self.assertIn("Leech 与 On Hit", item.basis)

    def test_回复优先净值并兼容旧版再生字段(self) -> None:
        item = self._report({
            "NetLifeRegen": -20,
            "LifeRegenRecovery": 300,
            "ManaRegen": 45,
        }).by_key("recovery")
        self.assertIn("生命 -20/秒", item.value)
        self.assertNotIn("生命 300/秒", item.value)
        self.assertIn("魔力 45/秒", item.value)
        self.assertIn("旧版 build", item.basis)

    def test_吸纳字段缺失时保持未知(self) -> None:
        missing = self._report({"LifeRegen": 10}).by_key("recoup")
        self.assertFalse(missing.known)
        self.assertEqual(missing.value, "")

        known = self._report({"LifeRecoup": 20, "EnergyShieldRecoup": 10}).by_key("recoup")
        self.assertTrue(known.known)
        self.assertEqual(known.value, "生命 20%；能量护盾 10%")


class OutputAnalysisTest(unittest.TestCase):
    """需求 8.6：三种场景分开，且不把一个配置快照冒充三份结果。"""

    def _items(
        self,
        stats: dict[str, float],
        *,
        config: dict[str, str] | None = None,
        stat_text: dict[str, str] | None = None,
    ):
        import sqlite3

        from parsers.build_report import build_health_report
        from parsers.pob_build import BuildGem, PobBuild, SkillGroup

        build = PobBuild(
            player_stats=stats,
            player_stat_text=stat_text or {key: str(value) for key, value in stats.items()},
            config=config or {},
            main_socket_group=1,
            calcs_skill_group=1,
            skill_groups=[SkillGroup(
                is_main=True,
                active_skill_index=1,
                gems=(BuildGem(name_en="Arc", name_zh="电弧", skill_id="Arc"),),
            )],
        )
        conn = sqlite3.connect(":memory:")
        try:
            return build_health_report(conn, build).output_items
        finally:
            conn.close()

    @staticmethod
    def _by_key(items, key: str):  # noqa: ANN001
        return next(item for item in items if item.key == key)

    def test_三种输出场景固定齐全(self) -> None:
        from parsers.build_report import REQUIREMENT_8_6_ITEMS, uncovered_output_requirements

        self.assertEqual(REQUIREMENT_8_6_ITEMS, ("单体", "清图", "Boss"))
        self.assertEqual(uncovered_output_requirements(), ())
        items = self._items({"CombinedDPS": 123})
        self.assertEqual(
            [item.key for item in items],
            ["output_single_target", "output_clear", "output_boss"],
        )

    def test_单体绑定当前计算技能并优先_full_dps(self) -> None:
        items = self._items({"FullDPS": 456, "CombinedDPS": 123})
        item = self._by_key(items, "output_single_target")
        self.assertEqual(item.value, "电弧；456（FullDPS）")
        self.assertIn("当前保存", item.verdict)

    def test_dps_为零仍是已知值(self) -> None:
        item = self._by_key(self._items({"TotalDPS": 0}), "output_single_target")
        self.assertTrue(item.known)
        self.assertIn("0（TotalDPS）", item.value)

    def test_boss_只有显式配置时才给值(self) -> None:
        missing = self._by_key(self._items({"CombinedDPS": 123}), "output_boss")
        self.assertFalse(missing.known)
        self.assertEqual(missing.value, "")
        self.assertIn("enemyIsBoss", missing.missing_reason)

        ordinary = self._by_key(
            self._items({"CombinedDPS": 123}, config={"enemyIsBoss": "None"}),
            "output_boss",
        )
        self.assertFalse(ordinary.known)

        pinnacle = self._by_key(
            self._items({"CombinedDPS": 123}, config={"enemyIsBoss": "Pinnacle"}),
            "output_boss",
        )
        self.assertTrue(pinnacle.known)
        self.assertIn("守卫者/巅峰 Boss", pinnacle.value)
        self.assertIn("enemyIsBoss=Pinnacle", pinnacle.basis)

        legacy = self._by_key(
            self._items({"CombinedDPS": 123}, config={"enemyIsBoss": "Shaper"}),
            "output_boss",
        )
        self.assertTrue(legacy.known)
        self.assertIn("旧版 Shaper", legacy.value)

    def test_清图只列覆盖指标且不拿_dps_猜(self) -> None:
        missing = self._by_key(self._items({"CombinedDPS": 999}), "output_clear")
        self.assertFalse(missing.known)
        self.assertIn("不能只凭单体 DPS", missing.missing_reason)

        known = self._by_key(
            self._items(
                {"CombinedDPS": 999, "AreaOfEffectRadiusMetres": 2.4},
                stat_text={
                    "CombinedDPS": "999",
                    "AreaOfEffectRadiusMetres": "2.4",
                    "ChainMaxString": "All",
                },
            ),
            "output_clear",
        )
        self.assertTrue(known.known)
        self.assertIn("范围半径 2.4 米", known.value)
        self.assertIn("连锁 All", known.value)
        self.assertIn("不据此给强/弱评级", known.verdict)


class HealthReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.files = _fixtures()
        if not self.files:
            self.skipTest("PoB 检出不可用，跳过夹具用例")
        import sqlite3

        from calc import constants
        from core.db import SCHEMA

        constants.reset_cache()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # 判据用的三个上限——与 `test_resistance.fixture_conn` 同一套打桩方式
        self.conn.executemany(
            "INSERT INTO game_constant VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            [
                (key, "pobMisc", None, 0, "scalar", value, None,
                 "Path of Building", 2, "https://example/blob/Data.lua",
                 "PoB 2.66.2 / tree 3.29", "3.29")
                for key, value in (
                    (constants.MAX_RESIST_CAP, 90.0),
                    (constants.BLOCK_CHANCE_CAP, 90.0),
                    (constants.SUPPRESSION_CHANCE_CAP, 100.0),
                )
            ],
        )
        self.conn.commit()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        from calc import constants

        constants.reset_cache()
        self.conn.close()

    def _report(self, path: Path):
        from parsers.build_report import build_health_report
        from parsers.pob_build import parse_build

        return build_health_report(self.conn, parse_build(path.read_text(encoding="utf-8")))

    def test_缺失项标未知且不填估计值(self) -> None:
        """8.4.7 的原文：缺失 SHALL 标注为「未知」**而非填入估计值**。

        3.13 夹具里法术压制/EHP/MaxHit/保留都没有（压制是 3.16 才加的），
        正好是这条路径的天然样本。
        """
        report = self._report(self.files[-1])
        for item in report.items:
            if item.known:
                continue
            with self.subTest(item=item.key):
                self.assertEqual(item.value, "", "未知项必须留空，不许填估计值")
                self.assertTrue(item.missing_reason, "未知必须说清缺的是哪一项")

    def test_法术压制在_313_夹具上是未知(self) -> None:
        item = self._report(self.files[-1]).by_key("spell_suppression")
        self.assertIsNotNone(item)
        self.assertFalse(item.known)
        self.assertIn("SpellSuppressionChance", item.missing_reason)

    def test_超抗要写进抗性(self) -> None:
        """超抗顶得住减抗与诅咒、顶不住穿透，是 `calc/resistance` 的既有结论，
        报告里不能把它吞掉。"""
        item = self._report(self.files[-1]).by_key("fire_resist")
        self.assertIn("超抗", item.value)

    def test_护甲不给减伤百分比(self) -> None:
        """护甲不是固定百分比减伤，不给这一击多大就没有减伤可言——
        与 `num_armour_reduction` / `gap_armour_without_damage` 那对用例同一条规矩。"""
        item = self._report(self.files[-1]).by_key("armour")
        self.assertNotIn("%", item.value)
        self.assertIn("这一击", item.basis)

    def test_dps_只复述不计算(self) -> None:
        item = self._report(self.files[-1]).by_key("dps")
        self.assertIn("不计算 DPS", item.basis)

    def test_珠宝按树上插槽而不是装备栏(self) -> None:
        """实测 OccVortex：装备 Slot 里一件珠宝都没有，而树上插着 3 颗。
        按 Slot 名找会得出「没有珠宝」这个错结论。"""
        occ = next((p for p in self.files if p.stem == "OccVortex"), None)
        if occ is None:
            self.skipTest("没有 OccVortex 夹具")
        item = self._report(occ).by_key("jewels")
        self.assertIn("3", item.value)

    def test_万神殿没开不等于未知(self) -> None:
        item = self._report(self.files[-1]).by_key("pantheon")
        self.assertTrue(item.known, "没开是一个确定的事实，不是「未知」")
        self.assertIn("未启用", item.value)

    def test_五个夹具都能出完整报告(self) -> None:
        from parsers.build_report import uncovered_requirements

        for path in self.files:
            with self.subTest(build=path.stem):
                report = self._report(path)
                self.assertEqual(uncovered_requirements(), ())
                self.assertTrue(report.notes, "报告必须带来源说明")
                self.assertTrue(
                    any("没有重算" in n for n in report.notes),
                    "必须说清本库没有重算任何一项",
                )


# ── 分享链接（第五十轮）─────────────────────────────────────


class BuildLinkTest(unittest.TestCase):
    """站点表逐条抄自 PoB `Modules/BuildSiteTools.lua`，不凭记忆。"""

    def resolve(self, link: str) -> str:
        from parsers.pob_link import resolve_download_url

        return resolve_download_url(link)

    def test_pobb_in_的下载地址不是_raw_结尾(self) -> None:
        """**照记忆写会错**：pobb.in 的下载地址是 `/pob/<id>`，不是 `/<id>/raw`。
        这条正是「能钉住上游原文就别凭印象」的现成例子。"""
        self.assertEqual(
            self.resolve("https://pobb.in/abc123"), "https://pobb.in/pob/abc123")

    def test_各站点按_pob_的表解析(self) -> None:
        cases = {
            "https://pastebin.com/AbC123": "https://pastebin.com/raw/AbC123",
            "https://pob.codes/b/xyz": "https://api.pob.codes/xyz/raw",
            "https://rentry.co/abc": "https://rentry.co/paste/abc/raw",
            "https://maxroll.gg/poe/pob/deadbeef": "https://maxroll.gg/poe/api/pob/deadbeef",
            "https://poedb.tw/pob/zzz": "https://poedb.tw/pob/zzz/raw",
        }
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(self.resolve(link), expected)

    def test_没有协议头也能认(self) -> None:
        self.assertEqual(self.resolve("pobb.in/abc"), "https://pobb.in/pob/abc")

    def test_poe_ninja_只认_poe1_路径(self) -> None:
        """poe.ninja 两款游戏都服务。POE1/POE2 边界顺带落在这张表上：
        `poe2` 那一段匹配不上，自然被挡在外面。"""
        from parsers.pob_link import PobLinkError

        self.assertEqual(
            self.resolve("https://poe.ninja/poe1/pob/abc123"),
            "https://poe.ninja/poe1/pob/raw/abc123")
        with self.assertRaises(PobLinkError):
            self.resolve("https://poe.ninja/poe2/pob/abc123")

    def test_白名单之外一律拒绝(self) -> None:
        """**这是白名单不是黑名单**：URL 由用户给、服务端拿它去发请求，
        放开等于允许指使本服务访问任意地址（内网、云元数据端点都在射程内）。"""
        from parsers.pob_link import PobLinkError

        for bad in (
            "https://evil.example.com/x",
            "http://169.254.169.254/latest/meta-data/",
            "https://localhost:8787/admin",
            "file:///etc/passwd",
        ):
            with self.subTest(url=bad), self.assertRaises(PobLinkError):
                self.resolve(bad)

    def test_拒绝时列出支持的站点(self) -> None:
        from parsers.pob_link import PobLinkError

        with self.assertRaises(PobLinkError) as ctx:
            self.resolve("https://evil.example.com/x")
        self.assertIn("pobb.in", str(ctx.exception))

    def test_分享码不会被当成链接(self) -> None:
        from parsers.pob_link import looks_like_link

        self.assertFalse(looks_like_link("eJy9XVtzm8i2fh7_CspV-7yMbPcdOifZu"))
        self.assertTrue(looks_like_link("https://pobb.in/abc"))
        self.assertTrue(looks_like_link("pobb.in/abc"))
