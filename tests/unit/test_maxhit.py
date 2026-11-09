"""Max Hit：自洽回代、二次方程的两个边界、以及那条「只做单类型」的边界（第二十七轮）。

上游没有可对账的期望值，所以这一维的核心验证是**回代**：
把算出来的 Max Hit 重新走一遍正向链路（抗性 → 护甲 → 池），结果必须等于池子。
公式抄错、系数写反、上下限夹反，回代都会立刻对不上——`RoundTripTest` 就是这件事。

另一半是**边界**：PoB 的完整 Max Hit 含伤害转化、守护技能、Ward 与 Mind over Matter，
它自己的注释写着完整解「可能要解一个六次多项式」，所以那些情形它用多轮平滑逼近。
本模块只做**单一伤害类型、100% 不转化**的闭式那一支，用例把这条边界也钉住。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import constants  # noqa: E402
from calc import maxhit as mh  # noqa: E402
from tests.unit.test_resistance import (  # noqa: E402
    BLOB,
    CONSTANT_SCHEMA,
    POB_ROOT_ENV,
    fixture_conn as resistance_fixture_conn,
)


def fixture_conn() -> sqlite3.Connection:
    conn = resistance_fixture_conn()
    conn.execute(
        "INSERT INTO game_constant VALUES (?,?,?,0,'scalar',?,NULL,'Path of Building',2,?,"
        "'PoB 2.66.2 / tree 3.29','3.29',datetime('now'))",
        (
            constants.MAX_PHYS_DAMAGE_REDUCTION, "characterConstants",
            "Metadata/Characters/Character.ot", 90.0,
            f"{BLOB}/src/Data/Misc.lua#L62",
        ),
    )
    conn.commit()
    return conn


class MaxHitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.conn = fixture_conn()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        constants.reset_cache()
        self.conn.close()


class SimpleCaseTest(MaxHitTestCase):
    """没有护甲时是一步除法。"""

    def test_抗性把一发砍到四分之一(self) -> None:
        """75% 抗性、5000 池 ⇒ 能挨 20000。"""
        self.assertEqual(20000.0, mh.max_hit(5000, resist_pct=75).max_hit)

    def test_零抗性时就是池子本身(self) -> None:
        self.assertEqual(5000.0, mh.max_hit(5000).max_hit)

    def test_承受倍率修正也算进去(self) -> None:
        """「受到的伤害减少 20%」⇒ 能挨的原始伤害多 25%。"""
        self.assertEqual(6250.0, mh.max_hit(5000, taken_mult=0.8).max_hit)

    def test_只有定值减伤时仍是除法(self) -> None:
        """没有护甲、只有 20% 物理减伤：5000 / 0.8 = 6250。"""
        self.assertAlmostEqual(6250.0, mh.max_hit(5000, flat_reduction_pct=20).max_hit, places=2)


class QuadraticTest(MaxHitTestCase):
    """有护甲时要解二次方程，并夹在两个边界之间。"""

    def test_护甲让能挨的伤害变多但不成比例(self) -> None:
        """10000 护甲、5000 池、0 抗：6531（不是 5000 也不是 10000）。"""
        result = mh.max_hit(5000, armour=10000)
        self.assertEqual(6531.0, result.max_hit)
        self.assertGreater(result.max_hit, result.without_armour)

    def test_结果永远不低于不算护甲的下界(self) -> None:
        for armour in (0, 100, 10000, 10**6):
            with self.subTest(armour=armour):
                result = mh.max_hit(5000, resist_pct=50, armour=armour)
                self.assertGreaterEqual(result.max_hit, result.without_armour - 1)

    def test_结果不超过减伤上限那条上界(self) -> None:
        """护甲再高也只能吃到 90% 减伤——上界是「不算护甲」÷ 0.1。"""
        result = mh.max_hit(5000, armour=10**9)
        self.assertLessEqual(result.max_hit, result.with_max_reduction)
        self.assertAlmostEqual(50000.0, result.with_max_reduction, places=2)

    def test_同一堆护甲对更大的池挡得更少(self) -> None:
        """护甲减伤依赖伤害大小，所以池子越大、护甲的边际收益越低。
        这就是「护甲挡不住大招」的算术来源。"""
        small = mh.max_hit(1000, armour=10000)
        large = mh.max_hit(20000, armour=10000)
        self.assertGreater(
            small.effective_multiplier, large.effective_multiplier
        )

    def test_压制削掉护甲的收益(self) -> None:
        base = mh.max_hit(5000, armour=10000).max_hit
        overwhelmed = mh.max_hit(5000, armour=10000, overwhelm_pct=20).max_hit
        self.assertLess(overwhelmed, base)


class RoundTripTest(MaxHitTestCase):
    """**自洽回代**：算出来的 Max Hit 走一遍正向链路必须正好打满池子。

    这是这一维唯一有力的验证方式（上游没有可对账的期望值）。
    公式抄错、系数写反、上下限夹反，这里都会立刻红。
    """

    CASES = (
        dict(pool=5000, resist_pct=75),
        dict(pool=5000, resist_pct=0, armour=10000),
        dict(pool=5000, resist_pct=75, armour=10000, flat_reduction_pct=10),
        dict(pool=8000, resist_pct=0, armour=30000, taken_mult=0.8),
        dict(pool=5000, resist_pct=0, armour=10000, overwhelm_pct=20),
        dict(pool=12345, resist_pct=40, armour=7777, flat_reduction_pct=5),
        dict(pool=1000, resist_pct=90, armour=500),
    )

    def test_回代正好打满池子(self) -> None:
        for case in self.CASES:
            with self.subTest(**case):
                pool = case["pool"]
                params = {k: v for k, v in case.items() if k != "pool"}
                result = mh.max_hit(pool, **params)
                taken = mh.taken_from(result.max_hit, **params)
                # 取整只会让结果略小于池子，误差不超过 0.1%
                self.assertLessEqual(taken, pool * 1.0001)
                self.assertGreater(taken, pool * 0.999)

    def test_比max_hit大一点的一发就会超过池子(self) -> None:
        """上界性质：再多打 1% 就撑不住了——否则「最多」这两个字不成立。"""
        for case in self.CASES:
            with self.subTest(**case):
                pool = case["pool"]
                params = {k: v for k, v in case.items() if k != "pool"}
                result = mh.max_hit(pool, **params)
                self.assertGreater(
                    mh.taken_from(result.max_hit * 1.01, **params), pool
                )


class BoundaryTest(MaxHitTestCase):
    def test_池子为零时不报错也不给数(self) -> None:
        self.assertEqual(0.0, mh.max_hit(0, resist_pct=75).max_hit)

    def test_unattainable_effective_resistance_is_rejected(self) -> None:
        for value in (91, 99, 100, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mh.max_hit(5000, resist_pct=value)

    def test_常量缺失时抛异常(self) -> None:
        constants.reset_cache()
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.executescript(CONSTANT_SCHEMA)
        constants.load(empty, force=True)
        with self.assertRaises(constants.MissingConstant):
            mh.max_hit(5000, resist_pct=75)
        empty.close()


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.defence = (POB_ROOT_ENV / "src" / "Modules" / "CalcDefence.lua").read_text(
            encoding="utf-8"
        )

    def _lines(self, span: tuple[int, int]) -> str:
        return "\n".join(self.defence.splitlines()[span[0] - 1: span[1]])

    def test_无护甲那条闭式还在(self) -> None:
        block = self._lines(mh.SIMPLE_LINES)
        self.assertIn(
            "hitTaken = m_max(totalHitPool / damageConvertedMulti / drMulti - takenFlat, 0)"
            " / totalTakenMulti",
            block,
        )

    def test_二次方程的三个系数还在(self) -> None:
        block = self._lines(mh.QUADRATIC_LINES)
        self.assertIn(
            "local a = 5 * (1 - flatDR + enemyOverwhelmPercent / 100) * totalTakenMulti "
            "* resistXConvert * resistXConvert",
            block,
        )
        self.assertIn("local c = -effectiveAppliedArmour * (totalHitPool", block)
        self.assertIn("(m_sqrt(m_max(b * b - 4 * a * c, 0)) - b) / (2 * a)", block)

    def test_两个边界与向下取整还在(self) -> None:
        block = self._lines(mh.QUADRATIC_LINES)
        self.assertIn("local noDRMaxHit = totalHitPool / damageConvertedMulti", block)
        self.assertIn(
            "local maxDRMaxHit = noDRMaxHit / (1 - (output.DamageReductionMax "
            "- enemyOverwhelmPercent) / 100)",
            block,
        )
        self.assertIn("hitTaken = m_floor(m_max(m_min(RAW, maxDRMaxHit), noDRMaxHit))", block)

    def test_上游自己说完整解要解六次多项式(self) -> None:
        """这就是「只做单类型」这条边界的依据——不是我偷懒。"""
        line = self.defence.splitlines()[mh.COMMENT_LINE - 1]
        self.assertIn("polynomial equation of 6th degree", line)


class MaxHitToolTest(MaxHitTestCase):
    def setUp(self) -> None:
        super().setUp()
        from tools.impl.maxhit import max_hit_calc

        self.tool = max_hit_calc

    def test_total_resistance_is_capped_before_max_hit(self) -> None:
        cases = [({}, 75, 20000), ({"maximum_resist_pct": 80}, 80, 25000),
                 ({"maximum_resist_pct": 90}, 90, 50000),
                 ({"extra_max_resist_pct": 5}, 80, 25000)]
        for extra, effective, threshold in cases:
            with self.subTest(extra=extra):
                result = self.tool(life=5000, total_resist_pct=99, damage_type="火焰", **extra)
                self.assertEqual("result", result.data["kind"])
                self.assertEqual(effective, result.data["effective_resist_pct"])
                self.assertAlmostEqual(threshold, result.data["max_hit"])
                text = result.citations[0]["text"]
                self.assertIn("临界值", text)
                self.assertIn("耗尽池子", text)
                if not extra:
                    self.assertIn("未提供最大抗性加成", text)
                    self.assertIn("硬上限为 90%", text)

    def test_penetration_follows_cap_and_cannot_be_offset_by_overcap(self) -> None:
        for total in (99, 120):
            result = self.tool(life=5000, total_resist_pct=total,
                               enemy_penetration=25, damage_type="火焰")
            self.assertEqual(50, result.data["effective_resist_pct"])
            self.assertEqual(10000, result.data["max_hit"])
        result = self.tool(life=5000, resist_pct=90, enemy_penetration=25, damage_type="火焰")
        self.assertEqual(65, result.data["effective_resist_pct"])

    def test_invalid_or_unsupported_premises_produce_cited_clarification(self) -> None:
        cases = [
            {"resist_pct": 99}, {"resist_pct": 100}, {"resist_pct": float("nan")},
            {"total_resist_pct": 99, "maximum_resist_pct": 95},
            {"total_resist_pct": 99, "resist_pct": 75},
            {"resist_pct": 75, "armour": 10000},
            {"resist_pct": 75, "flat_reduction_pct": 10},
            {"resist_pct": 75, "enemy_penetration": -25},
            {"resist_pct": 75, "unsupported_conditions": ["伤害转化", "Mind over Matter"]},
            {"resist_pct": 75, "unsupported_conditions": ["Mind over Matter 分流42%伤害"]},
            {"damage_type": "物理", "total_resist_pct": 0},
            {"damage_type": "物理", "resist_pct": 75},
            {"damage_type": "物理", "flat_reduction_pct": -10},
            {"damage_type": "混沌", "energy_shield": 3000, "resist_pct": 75},
        ]
        for case in cases:
            with self.subTest(case=case):
                result = self.tool(**{"life": 5000, "damage_type": "火焰", **case})
                self.assertEqual("clarification", result.data["kind"])
                self.assertNotIn("max_hit", result.data)
                self.assertTrue(result.citations)
                self.assertIn("本轮不提供承伤数值", result.citations[0]["text"])
                self.assertNotIn("42%", result.citations[0]["text"])

    def test_missing_resistance_is_not_zero(self) -> None:
        result = self.tool(life=5000, damage_type="火焰")
        self.assertEqual("rules", result.data["kind"])
        self.assertIn("该伤害类型的总抗性或明确的生效抗性", result.data["missing_inputs"])
        self.assertNotIn("max_hit", result.data)

    def test_没给池子时讲规则(self) -> None:
        result = self.tool()
        self.assertEqual("rules", result.data["kind"])
        text = result.citations[0]["text"]
        self.assertIn("抗性 → 护甲与物理减伤 → 池子", text)
        self.assertIn("二次方程", text)

    def test_给了抗性但没给池子时说清缺什么(self) -> None:
        result = self.tool(resist_pct=75)
        self.assertEqual(["生命 + 能量护盾"], result.data["missing_inputs"])
        self.assertIn("没给池子", result.citations[0]["text"])

    def test_生命与护盾相加成池(self) -> None:
        result = self.tool(life=3000, energy_shield=2000, resist_pct=75)
        self.assertEqual(5000, result.data["pool"])
        self.assertEqual(20000.0, result.data["max_hit"])

    def test_护甲那一支给出实际挡掉多少(self) -> None:
        result = self.tool(life=5000, armour=10000, damage_type="物理")
        self.assertEqual(6531.0, result.data["max_hit"])
        self.assertGreater(result.data["armour_reduction_pct"], 0)
        self.assertIn("护甲减伤依赖伤害大小", result.citations[0]["text"])

    def test_边界声明写明不含转化与MoM(self) -> None:
        for kwargs in ({}, {"life": 5000, "resist_pct": 75}):
            with self.subTest(kwargs=kwargs):
                boundary = self.tool(**kwargs).data["boundary"]
                self.assertIn("不转化", boundary)
                self.assertIn("Mind over Matter", boundary)

    def test_大数不写成科学计数法(self) -> None:
        text = self.tool(life=100000, resist_pct=75).citations[0]["text"]
        self.assertIn("400,000", text)
        self.assertNotIn("e+05", text)

    def test_引用带钉住commit的行号锚点(self) -> None:
        citation = self.tool(life=5000, resist_pct=75).citations[0]
        self.assertIn("CalcDefence.lua#L", citation["url"])
        self.assertEqual("derived", citation["channel"])


class MaxHitRouteTest(unittest.TestCase):
    def route(self, question: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(question)

    def test_一发能挨多少路由到maxhit(self) -> None:
        for question in (
            "我 5000 血 75 火抗，一发最多能挨多少火焰伤害",
            "10000 护甲 5000 血会被 20000 物理秒吗",
            "一发最多能挨多少",
            "5000生命，总火抗99%，最大抗性90%，一发最多能挨多少火焰伤害",
            "5000生命，总火抗99%，没有最大抗性加成，敌人25%火焰穿透，一发最多能挨多少",
            "5000生命、护甲10000，护甲减伤后最多能承受多大的物理击中",
        ):
            with self.subTest(question=question):
                self.assertEqual("max_hit_calc", self.route(question))

    def test_单维度问法仍归各自的计算器(self) -> None:
        """Max Hit 是六块的合流，但不能把单维度问题也吸走。"""
        self.assertEqual("mitigation_calc", self.route("护甲能减多少伤害"))
        self.assertEqual("resistance_calc", self.route("抗性上限是多少"))
        self.assertEqual("recovery_calc", self.route("护盾多久回满"))

    def test_堆生命的问法不抢(self) -> None:
        """「有效生命怎么堆」是取向问题，不是计算。"""
        self.assertEqual("", self.route("有效生命怎么堆"))


if __name__ == "__main__":
    unittest.main()
