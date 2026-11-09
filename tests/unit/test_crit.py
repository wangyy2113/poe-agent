"""暴击：上限 100%（不是 95%）、基础加成 150%、平均伤害倍率（第二十八轮）。

这一维为什么现在能做：**平均伤害倍率不需要知道这一击有多大**
（`(1 − 暴击率) + 暴击率 × 暴击加成`），所以它是攻击侧唯一能在
「伤害数值刻意未采集」的前提下如实算出来的一块。它给的是倍率，不是 DPS。

三条钉住的事实：

1. **暴击率上限 100%**——PoB 给 `CritChanceCap` 播的基础值就是 100
   （`CalcSetup.lua:110`），社区常说的 95% 在 3.29 的数据里不成立。
   这个数硬编码在上游代码里、不在常量表，所以对着原文钉。
2. **基础暴击加成 150%**，而 PoB 内部存成 `+50`（`CalcSetup.lua:501` 减了 100）。
   照抄内部值会少一整倍。
3. **「增加的暴击率」乘在基础暴击上**：基础越低，百分比加成越不值。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import constants  # noqa: E402
from calc import crit as ct  # noqa: E402
from core.db import SCHEMA  # noqa: E402
from tests.unit.test_resistance import (  # noqa: E402
    BLOB,
    CONSTANT_SCHEMA,
    POB_ROOT_ENV,
)

POB_URL = f"{BLOB}/src/Data/Skills/act_int.lua"


def constant_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(CONSTANT_SCHEMA)
    conn.execute(
        "INSERT INTO game_constant VALUES (?,?,?,0,'scalar',?,NULL,'Path of Building',2,?,"
        "'PoB 2.66.2 / tree 3.29','3.29',datetime('now'))",
        (
            constants.BASE_CRIT_MULTIPLIER, "characterConstants",
            "Metadata/Characters/Character.ot", 150.0, f"{BLOB}/src/Data/Misc.lua#L62",
        ),
    )
    conn.commit()
    return conn


class CritTestCase(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.conn = constant_conn()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        constants.reset_cache()
        self.conn.close()


class ChanceTest(CritTestCase):
    def test_增加的暴击率乘在基础上(self) -> None:
        """6% 基础 + 300% 增加 = 24%，不是 306%。"""
        self.assertEqual(24.0, ct.crit(6.0, increased_pct=300).chance)

    def test_基础越低百分比加成越不值(self) -> None:
        """这就是「哪种武器适合暴击流」的算术来源。"""
        low = ct.crit(6.0, increased_pct=300).chance
        high = ct.crit(7.5, increased_pct=300).chance
        self.assertEqual((24.0, 30.0), (low, high))

    def test_定值加值先加再乘(self) -> None:
        self.assertEqual(40.0, ct.crit(6.0, flat_chance=4.0, increased_pct=300).chance)

    def test_上限是一百而不是九十五(self) -> None:
        """社区常说的 95% 在 PoB 3.29 的数据里不成立。"""
        result = ct.crit(7.5, increased_pct=1500)
        self.assertEqual(100.0, result.chance)
        self.assertEqual(100.0, result.cap)
        self.assertTrue(result.capped)

    def test_更多的暴击率是连乘(self) -> None:
        self.assertEqual(48.0, ct.crit(6.0, increased_pct=300, more_mult=2.0).chance)


class AverageMultiplierTest(CritTestCase):
    def test_四成暴击三点五倍加成正好翻倍(self) -> None:
        """0.6 + 0.4 × 3.5 = 2.0——平均伤害翻倍。"""
        result = ct.crit(40.0, multi_pct=350)
        self.assertAlmostEqual(2.0, result.average_multiplier, places=6)
        self.assertAlmostEqual(100.0, result.damage_gain_pct, places=6)

    def test_零暴击时倍率为一(self) -> None:
        self.assertEqual(1.0, ct.crit(0.0, multi_pct=350).average_multiplier)

    def test_满暴击时倍率等于暴击加成(self) -> None:
        result = ct.crit(100.0, multi_pct=350)
        self.assertAlmostEqual(3.5, result.average_multiplier, places=6)

    def test_不给加成时用基础一百五(self) -> None:
        """PoB 内部把它存成 +50，显示时才是 150%——照抄内部值会少一整倍。"""
        result = ct.crit(40.0)
        self.assertEqual(150.0, result.multi_pct)
        self.assertAlmostEqual(1.2, result.average_multiplier, places=6)

    def test_额外加成叠在基础之上(self) -> None:
        result = ct.crit(40.0, extra_multi_pct=200)
        self.assertEqual(350.0, result.multi_pct)

    def test_常量缺失时抛异常(self) -> None:
        constants.reset_cache()
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.executescript(CONSTANT_SCHEMA)
        constants.load(empty, force=True)
        with self.assertRaises(constants.MissingConstant):
            ct.crit(6.0)
        empty.close()


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = POB_ROOT_ENV / "src" / "Modules"
        cls.offence = (root / "CalcOffence.lua").read_text(encoding="utf-8")
        cls.setup = (root / "CalcSetup.lua").read_text(encoding="utf-8")

    def test_暴击率公式还在给出的行号范围内(self) -> None:
        block = "\n".join(
            self.offence.splitlines()[ct.CHANCE_LINES[0] - 1: ct.CHANCE_LINES[1]]
        )
        self.assertIn("output.CritChance = (baseCrit + base) * (1 + inc / 100) * more", block)
        self.assertIn('skillModList:Override(nil, "CritChanceCap")', block)

    def test_平均伤害倍率那一行还在(self) -> None:
        line = self.offence.splitlines()[ct.EFFECT_LINE - 1]
        self.assertIn(
            "output.CritEffect = 1 - critChancePercentage + critChancePercentage "
            "* output.CritMultiplier",
            line,
        )

    def test_上限一百是硬编码在这一行(self) -> None:
        """它不在常量表，只能这样钉。"""
        line = self.setup.splitlines()[ct.CAP_LINE - 1]
        self.assertIn(
            f'modDB:NewMod("CritChanceCap", "BASE", {ct.CRIT_CHANCE_CAP:g}, "Base")', line
        )

    def test_基础加成在上游减了一百(self) -> None:
        line = self.setup.splitlines()[ct.BASE_MULTI_LINE - 1]
        self.assertIn(
            'modDB:NewMod("CritMultiplier", "BASE", '
            'data.characterConstants["base_critical_strike_multiplier"] - 100, "Base")',
            line,
        )


def tool_conn() -> sqlite3.Connection:
    """内存库：一颗技能 + 一件武器底材 + 一件非武器。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO pob_skill_gem(gem_id,name_en,name_zh,name_zh_alt,poedb_slug,gem_kind,"
        "attr_color,tags,tags_zh,tag_string,skill_types,natural_max_level,is_vaal,"
        "is_transfigured,source_file,source,source_priority,pob_url,poedb_url,"
        "source_commit,content_hash,updated_at) "
        "VALUES ('IceNova','Ice Nova','冰霜新星','[]','ice_nova','active','int','[]','[]',"
        "'','[]',20,0,0,'Data/Skills/act_int.lua','Path of Building',2,?,"
        "'https://poedb.tw/cn/x',?,'h',datetime('now'))",
        (POB_URL, "d" * 40),
    )
    conn.executemany(
        "INSERT INTO pob_skill_level(gem_id,gem_level,level_requirement,cost,crit_chance) "
        "VALUES ('IceNova',?,?, '{}',?)",
        [(1, 12, 7.5), (20, 70, 7.5), (21, 72, None)],
    )
    conn.executemany(
        "INSERT INTO pob_base_item(base_id,name_en,name_zh,poedb_slug,category_key,"
        "poedb_page,item_class,subtype,socket_limit,tags,influence_tags,armour_tag,"
        "modifier_page,implicit_text,implicit_mod_types,"
        "implicit_ids,stats,stats_kind,requirements,source_file,source,"
        "source_priority,pob_url,poedb_url,source_commit,content_hash,updated_at) "
        "VALUES (?,?,?,?,'claw','Claw','Claw','',0,"
        "'[\"claw\"]','[]','','','','[]','[]',?,?,'{}','Data/Bases/claw.lua',"
        "'Path of Building',2,?,'https://poedb.tw/cn/y',?,?,datetime('now'))",
        [
            ("nailed_fist", "Nailed Fist", "凿钉", "nailed_fist",
             json.dumps({"CritChanceBase": 7.5, "AttackRateBase": 1.55}), "weapon",
             POB_URL, "d" * 40, "h1"),
            ("plate_vest", "Plate Vest", "板甲背心", "plate_vest",
             json.dumps({"Armour": 19}), "armour", POB_URL, "d" * 40, "h2"),
        ],
    )
    conn.commit()
    return conn


class CritToolTest(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.const = constant_conn()
        constants.load(self.const, force=True)
        self.conn = tool_conn()
        self.patch = patch("tools.impl.crit.dbmod.get_conn", return_value=self.conn)
        self.patch.start()
        from tools.impl.crit import crit_calc

        self.tool = crit_calc

    def tearDown(self) -> None:
        self.patch.stop()
        constants.reset_cache()
        self.conn.close()
        self.const.close()

    def test_没给数值也没给名字时讲规则(self) -> None:
        result = self.tool()
        self.assertEqual("rules", result.data["kind"])
        text = result.citations[0]["text"]
        self.assertIn("不是社区常说的 95%", text)
        self.assertIn("150%", text)

    def test_按技能名查基础暴击并写明等级(self) -> None:
        result = self.tool(skill="冰霜新星", increased_pct=300, multi_pct=350)
        self.assertEqual(7.5, result.data["base_chance"])
        self.assertEqual(30.0, result.data["chance"])
        self.assertIn("20 级", result.citations[0]["text"])

    def test_按武器底材名查基础暴击(self) -> None:
        result = self.tool(weapon="凿钉", increased_pct=300)
        self.assertEqual(7.5, result.data["base_chance"])
        self.assertIn("凿钉", result.data["source"])

    def test_非武器底材明确说没有基础暴击(self) -> None:
        result = self.tool(weapon="板甲背心")
        self.assertFalse(result.data["found"])
        self.assertIn("不是武器", result.citations[0]["text"])

    def test_库里没有那一级时退到有数据的一级并写明(self) -> None:
        result = self.tool(skill="冰霜新星", gem_level=21)
        self.assertIn("按 20 级算", result.citations[0]["text"])

    def test_平均倍率与提升幅度一起给(self) -> None:
        result = self.tool(base_chance=40, multi_pct=350)
        self.assertAlmostEqual(2.0, result.data["average_multiplier"], places=4)
        self.assertAlmostEqual(100.0, result.data["damage_gain_pct"], places=2)

    def test_封顶时说明上限是一百(self) -> None:
        result = self.tool(base_chance=7.5, increased_pct=1500)
        self.assertTrue(result.data["capped"])
        self.assertIn("上限是 100% 而不是 95%", result.citations[0]["text"])

    def test_没给加成时提醒用的是基础值(self) -> None:
        text = self.tool(base_chance=40).citations[0]["text"]
        self.assertIn("用的是基础值 150%", text)

    def test_边界写明不给DPS(self) -> None:
        for kwargs in ({}, {"base_chance": 40}):
            with self.subTest(kwargs=kwargs):
                self.assertIn("不算 DPS", self.tool(**kwargs).data["boundary"])

    def test_名字有歧义时列候选而不猜(self) -> None:
        result = self.tool(skill="新星")
        self.assertTrue(result.data["found"] or "对应多个" in result.citations[0]["text"])

    def test_引用带钉住commit的行号锚点(self) -> None:
        citation = self.tool(base_chance=40).citations[0]
        self.assertIn("CalcOffence.lua#L", citation["url"])
        self.assertEqual("derived", citation["channel"])


class CritRouteTest(unittest.TestCase):
    def route(self, question: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(question)

    def test_暴击结算问法路由到暴击计算器(self) -> None:
        for question in (
            "暴击率上限是多少", "暴击怎么算", "冰霜新星的基础暴击是多少",
            "我 40% 暴击率 350% 暴击伤害，平均伤害提升多少",
        ):
            with self.subTest(question=question):
                self.assertEqual("crit_calc", self.route(question))

    def test_档位与枚举问法不被抢走(self) -> None:
        self.assertEqual("mod_tiers", self.route("+30% 暴击率这条词缀有几档"))
        self.assertEqual("stat_sources", self.route("暴击率能从哪些渠道获得"))

    def test_裸的暴击不构成判据(self) -> None:
        self.assertEqual("", self.route("哪些词缀给暴击"))

    def test_其它计算器不被抢走(self) -> None:
        self.assertEqual("max_hit_calc", self.route("一发最多能挨多少"))
        self.assertEqual("resistance_calc", self.route("抗性上限是多少"))


if __name__ == "__main__":
    unittest.main()
