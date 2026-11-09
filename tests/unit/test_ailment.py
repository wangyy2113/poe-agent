"""持续伤害：单位陷阱、「每秒」口径、叠加与减伤（2026-08-20 第二十五轮）。

这一维最值钱的两条验证：

1. **上游单位不统一**。`BleedPercentBase = 70`（百分数），而 `PoisonPercentBase = 0.30`、
   `IgnitePercentBase = 0.9`（小数）——PoB 对流血 `/ 100`、对另两个不除。
   照抄成同一种单位会差 100 倍，所以三条各自钉住。
2. **「每秒」不是「总共」**。点燃每秒 90%、持续 4 秒 → 总共 360%。这个口径的出处是
   PoB 自己的说明字符串（`x 0.9 (ignite deals 90% per second)`），用例把那句原文钉住。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import ailment as ail  # noqa: E402
from calc import constants  # noqa: E402
from tests.unit.test_resistance import (  # noqa: E402
    BLOB,
    CONSTANT_SCHEMA,
    POB_ROOT_ENV,
)

FIXTURE_ROWS = (
    (constants.IGNITE_PERCENT, "pobMisc", None, 0.9),
    (constants.IGNITE_DURATION, "pobMisc", None, 4.0),
    (constants.POISON_PERCENT, "pobMisc", None, 0.3),
    (constants.POISON_DURATION, "pobMisc", None, 2.0),
    (constants.BLEED_PERCENT, "pobMisc", None, 70.0),
    (constants.BLEED_DURATION, "pobMisc", None, 5.0),
    (constants.DOT_DPS_CAP, "pobMisc", None, 35791394.0),
    (constants.MONSTER_MAX_PHYS_DR, "monsterConstants",
     "Metadata/Monsters/Monster.ot", 75.0),
)


def fixture_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(CONSTANT_SCHEMA)
    conn.executemany(
        "INSERT INTO game_constant VALUES (?,?,?,0,'scalar',?,NULL,'Path of Building',2,?,"
        "'PoB 2.66.2 / tree 3.29','3.29',datetime('now'))",
        [
            (key, group, dat, value, f"{BLOB}/src/Modules/Data.lua#L256")
            for key, group, dat, value in FIXTURE_ROWS
        ],
    )
    conn.commit()
    return conn


class AilmentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.conn = fixture_conn()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        constants.reset_cache()
        self.conn.close()


class UnitTrapTest(AilmentTestCase):
    """上游单位不统一——三条各自钉住。"""

    def test_点燃每秒是九成(self) -> None:
        """`IgnitePercentBase = 0.9`（小数）→ 90%/秒。"""
        result = ail.ailment_dot("点燃", 10000)
        self.assertEqual(90.0, result.percent_per_second)
        self.assertEqual(9000.0, result.dps)

    def test_中毒每秒是三成(self) -> None:
        result = ail.ailment_dot("中毒", 10000)
        self.assertEqual(30.0, result.percent_per_second)
        self.assertEqual(3000.0, result.dps)

    def test_流血写成百分数不是小数(self) -> None:
        """`BleedPercentBase = 70` 要 `/ 100`；当成 0.7 会差 100 倍。"""
        result = ail.ailment_dot("流血", 10000)
        self.assertEqual(70.0, result.percent_per_second)
        self.assertEqual(7000.0, result.dps)


class PerSecondTest(AilmentTestCase):
    """「每秒」不是「总共」。"""

    def test_点燃四秒总共三百六十(self) -> None:
        result = ail.ailment_dot("点燃", 1000)
        self.assertEqual(4.0, result.duration)
        self.assertEqual(3600.0, result.total)

    def test_中毒两秒总共六成(self) -> None:
        self.assertEqual(600.0, ail.ailment_dot("中毒", 1000).total)

    def test_流血五秒总共三百五(self) -> None:
        self.assertEqual(3500.0, ail.ailment_dot("流血", 1000).total)

    def test_更快只挪走时间不改总量(self) -> None:
        """「燃烧更快」抬高每秒、缩短持续，总伤害不变（PoB 的 rateMod 两头都乘）。"""
        base = ail.ailment_dot("点燃", 1000)
        faster = ail.ailment_dot("点燃", 1000, faster_rate_pct=100)
        self.assertEqual(base.dps * 2, faster.dps)
        self.assertEqual(base.duration / 2, faster.duration)
        self.assertAlmostEqual(base.total, faster.total, places=6)


class StackAndMitigationTest(AilmentTestCase):
    def test_层数按倍数叠加(self) -> None:
        self.assertEqual(
            ail.ailment_dot("中毒", 1000).dps * 10,
            ail.ailment_dot("中毒", 1000, stacks=10).dps,
        )

    def test_层数至少一层(self) -> None:
        """0 层是没意义的输入；按 1 层算而不是算出 0。"""
        self.assertEqual(1, ail.ailment_dot("中毒", 1000, stacks=0).stacks)

    def test_抗性按乘法削(self) -> None:
        self.assertEqual(4500.0, ail.ailment_dot("点燃", 10000, mitigation_pct=50).dps)

    def test_持续伤害加成与效果提高分别乘(self) -> None:
        result = ail.ailment_dot(
            "点燃", 1000, dot_multiplier_pct=50, increased_effect_pct=20
        )
        self.assertAlmostEqual(900 * 1.5 * 1.2, result.dps, places=6)

    def test_顶到每秒上限时夹住(self) -> None:
        result = ail.ailment_dot("点燃", 10**9)
        self.assertTrue(result.capped)
        self.assertEqual(constants.get(constants.DOT_DPS_CAP), result.dps_capped)

    def test_认不出的异常直接抛异常(self) -> None:
        """感电与冰冻是门槛型异常，公式完全不同——不能当成第四种 DOT 算。"""
        for name in ("感电", "冰冻", "点火"):
            with self.subTest(name=name):
                with self.assertRaises(ail.UnknownAilment):
                    ail.ailment_dot(name, 1000)

    def test_常量缺失时抛异常(self) -> None:
        constants.reset_cache()
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.executescript(CONSTANT_SCHEMA)
        constants.load(empty, force=True)
        with self.assertRaises(constants.MissingConstant):
            ail.ailment_dot("点燃", 1000)
        empty.close()


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    """把上游原文钉在行号上——尤其是那句「每秒」的说明。"""

    @classmethod
    def setUpClass(cls) -> None:
        root = POB_ROOT_ENV / "src"
        cls.offence = (root / "Modules" / "CalcOffence.lua").read_text(encoding="utf-8")
        cls.setup = (root / "Modules" / "CalcSetup.lua").read_text(encoding="utf-8")
        cls.data = (root / "Modules" / "Data.lua").read_text(encoding="utf-8")

    def _lines(self, text: str, span: tuple[int, int]) -> str:
        return "\n".join(text.splitlines()[span[0] - 1: span[1]])

    def test_每秒这个口径的原文还在给出的行号上(self) -> None:
        line = self.offence.splitlines()[ail.IGNITE_PER_SECOND_LINE - 1]
        self.assertIn("ignite deals 90% per second", line)

    def test_流血除一百而点燃中毒不除(self) -> None:
        """这就是那个单位陷阱，在上游代码里看得最清楚。"""
        bleed = self._lines(self.offence, ail.BLEED_LINES)
        self.assertIn("basePercent / 100", bleed)
        ignite = self._lines(self.offence, ail.IGNITE_LINES)
        self.assertIn("data.misc.IgnitePercentBase", ignite)
        self.assertNotIn("IgnitePercentBase / 100", ignite)

    def test_流血只吃物理减伤不吃护甲(self) -> None:
        block = self._lines(self.offence, ail.BLEED_LINES)
        self.assertIn('enemyDB:Sum("BASE", nil, "PhysicalDamageReduction")', block)
        self.assertIn("data.misc.EnemyPhysicalDamageReductionCap", block)
        self.assertNotIn("Armour", block)

    def test_中毒吃混沌抗点燃吃火抗(self) -> None:
        self.assertIn('local resist = calcResistForType("Chaos", dotCfg)', self.offence)
        self.assertIn('local resist = calcResistForType("Fire", dotCfg)', self.offence)

    def test_流血默认一层(self) -> None:
        line = self.setup.splitlines()[ail.SETUP_STACK_LINE - 1]
        self.assertIn('modDB:NewMod("BleedStacksMax", "BASE", 1, "Base")', line)

    def test_每秒上限的来历还写在注释里(self) -> None:
        self.assertIn("DotDpsCap = 35791394, -- (2 ^ 31 - 1) / 60", self.data)


class AilmentToolTest(AilmentTestCase):
    def setUp(self) -> None:
        super().setUp()
        from tools.impl.ailments import ailment_dot_calc

        self.tool = ailment_dot_calc

    def test_没给伤害时讲规则并把三种并列(self) -> None:
        text = self.tool().citations[0]["text"]
        self.assertEqual("rules", self.tool().data["kind"])
        self.assertIn("| 点燃（Ignite） | 90% | 4 秒 | 360% | 火焰 | 1 层 |", text)
        self.assertIn("| 中毒（Poison） | 30% | 2 秒 | 60% | 混沌 | 无上限 |", text)
        self.assertIn("| 流血（Bleed） | 70% | 5 秒 | 350% | 物理 | 1 层 |", text)

    def test_只给异常名不给伤害时说清缺什么(self) -> None:
        result = self.tool(ailment="点燃")
        self.assertEqual("rules", result.data["kind"])
        self.assertEqual(["这一击的伤害"], result.data["missing_inputs"])
        self.assertIn("没给", result.citations[0]["text"])

    def test_给了伤害就出逐步推导(self) -> None:
        result = self.tool(ailment="点燃", hit_damage=10000)
        self.assertEqual((9000.0, 4.0, 36000.0), (result.data["dps"], result.data["duration"],
                                                  result.data["total"]))
        text = result.citations[0]["text"]
        self.assertIn("| 步骤 | 算式 | 结果 |", text)
        self.assertIn("每秒", text)

    def test_流血证据写明不吃护甲(self) -> None:
        text = self.tool(ailment="流血", hit_damage=8000).citations[0]["text"]
        self.assertIn("不吃护甲", text)

    def test_中毒证据写明无层数上限(self) -> None:
        text = self.tool(ailment="中毒", hit_damage=5000).citations[0]["text"]
        self.assertIn("没有层数上限", text)

    def test_感电与冰冻直接失败并说明原因(self) -> None:
        result = self.tool(ailment="感电", hit_damage=1000)
        self.assertFalse(result.ok)
        self.assertIn("门槛型", result.error)

    def test_边界声明进data由代码附注(self) -> None:
        for kwargs in ({}, {"ailment": "点燃", "hit_damage": 1000}):
            with self.subTest(kwargs=kwargs):
                self.assertIn("感电", self.tool(**kwargs).data["boundary"])

    def test_引用带钉住commit的行号锚点(self) -> None:
        citation = self.tool(ailment="点燃", hit_damage=1000).citations[0]
        self.assertIn("CalcOffence.lua#L", citation["url"])
        self.assertEqual("derived", citation["channel"])

    def test_每秒上限写成千分位而不是科学计数法(self) -> None:
        """`3.57914e+07` 这种写法在答案里没法核对，也会让数值回填对不上。"""
        text = self.tool().citations[0]["text"]
        self.assertIn("35,791,394", text)
        self.assertNotIn("e+07", text)


class AilmentRouteTest(unittest.TestCase):
    """成对写：该抢的抢、不该抢的一条都不许抢。"""

    def route(self, question: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(question)

    def test_持续伤害问法路由到工具(self) -> None:
        for question in (
            "点燃每秒打多少伤害", "中毒能叠几层", "流血持续几秒",
            "一击 10000 火焰伤害的点燃打多少", "持续伤害怎么算",
        ):
            with self.subTest(question=question):
                self.assertEqual("ailment_dot_calc", self.route(question))

    def test_枚举问句仍归stat_sources(self) -> None:
        self.assertEqual("stat_sources", self.route("点燃能从哪些渠道获得"))

    def test_词缀类问法不被抢走(self) -> None:
        """「哪些词缀能造成点燃」不是计算题——裸的「点燃」不能当判据。"""
        self.assertEqual("", self.route("哪些词缀能造成点燃"))

    def test_其它计算器不被抢走(self) -> None:
        self.assertEqual("resistance_calc", self.route("抗性上限是多少"))
        self.assertEqual("recovery_calc", self.route("护盾多久回满"))
        self.assertEqual("reservation_calc", self.route("坚定和优雅一共保留多少魔力"))


class AilmentGateTest(unittest.TestCase):
    def test_持续伤害问法不被判成缺口(self) -> None:
        from core import db as dbmod
        from core.config import get_config
        from kb.coverage import dimension_gap

        dbmod.init_db(get_config().db_path, vector_dim=0)
        conn = dbmod.get_conn()
        for question in ("点燃每秒打多少伤害", "中毒能叠几层", "流血持续几秒"):
            with self.subTest(question=question):
                verdict = dimension_gap(conn, question)
                self.assertFalse(verdict is not None and verdict.is_gap)


if __name__ == "__main__":
    unittest.main()
