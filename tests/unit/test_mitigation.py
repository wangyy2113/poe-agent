"""受击结算：与 PoB 对账、公式钉住上游、工具边界与路由（2026-08-19 第二十一轮）。

沿用抗性那一轮定下的两组互补验证（判据见 `tests/unit/test_resistance.py` 的模块 docstring）：

1. `ReconcileWithPoB`：拿 **PoB 自己写下的期望值**对账——
   `spec/System/TestDefence_spec.lua:573` 断言「100% 法术格挡」→ `SpellBlockChance == 75`、
   `SpellBlockChanceOverCap == 25`。
2. `FormulaPinnedToUpstream`：断言我建模的那几段原文仍在、且引用锚点确实指向它们。
   上一轮第一次跑就抓到锚点差一行，这类断言的价值已经验证过了。

另外有一组是这一维特有的：**「不给伤害就没有答案」**。护甲减伤依赖这一击有多大，
「10000 护甲减多少伤害」在不给伤害数值时**不能给百分比**——工具必须说缺什么，
而不是替用户假定一个伤害值。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import constants  # noqa: E402
from calc import mitigation as mit  # noqa: E402
from calc.derive import join_terms, pob_url  # noqa: E402
from tests.unit.test_resistance import (  # noqa: E402
    BLOB,
    CONSTANT_SCHEMA,
    POB_ROOT_ENV,
)

#: 受击结算用到的常量。与真实库同值，但**用内存库打桩**——单测不连 data/poe.db。
FIXTURE_ROWS = (
    (constants.MAX_BLOCK, "characterConstants", "Metadata/Characters/Character.ot", 75.0),
    (constants.MAX_SPELL_BLOCK, "characterConstants", "Metadata/Characters/Character.ot", 75.0),
    (constants.MAX_PHYS_DAMAGE_REDUCTION, "characterConstants",
     "Metadata/Characters/Character.ot", 90.0),
    (constants.EVADE_CHANCE_CAP, "pobMisc", None, 95.0),
    (constants.BLOCK_CHANCE_CAP, "pobMisc", None, 90.0),
    (constants.SUPPRESSION_CHANCE_CAP, "pobMisc", None, 100.0),
    (constants.SUPPRESSION_EFFECT, "pobMisc", None, 40.0),
    (constants.MAX_RESIST_CAP, "pobMisc", None, 90.0),
)

#: 怪物命中值等级表的前 100 项里，用例只需要几个真值。
#: 85 级 = 559 取自本机库（`DefaultMonsterStats.dat`），其余填 0 占位。
ACCURACY_TABLE = [0.0] * 100
ACCURACY_TABLE[82] = 502.0  # 83 级
ACCURACY_TABLE[84] = 559.0  # 85 级


def fixture_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(CONSTANT_SCHEMA)
    conn.executemany(
        "INSERT INTO game_constant VALUES (?,?,?,0,'scalar',?,NULL,'Path of Building',2,?,"
        "'PoB 2.66.2 / tree 3.29','3.29',datetime('now'))",
        [
            (key, group, dat, value, f"{BLOB}/src/Modules/Data.lua#L240")
            for key, group, dat, value in FIXTURE_ROWS
        ],
    )
    import json

    conn.execute(
        "INSERT INTO game_constant VALUES (?,?,?,1,'level_table',NULL,?,'Path of Building',2,?,"
        "'PoB 2.66.2 / tree 3.29','3.29',datetime('now'))",
        (
            constants.MONSTER_ACCURACY_TABLE, "monsterAccuracyTable",
            "DefaultMonsterStats.dat", json.dumps(ACCURACY_TABLE),
            f"{BLOB}/src/Data/Misc.lua#L6",
        ),
    )
    conn.commit()
    return conn


class MitigationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.conn = fixture_conn()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        constants.reset_cache()
        self.conn.close()


class ReconcileWithPoB(MitigationTestCase):
    def test_对账pob用例的法术格挡与超出部分(self) -> None:
        """`TestDefence_spec.lua:573`：100% 法术格挡 → 生效 75%、超出 25%。"""
        result = mit.block(100, spell=True)
        self.assertEqual((75, 25.0), (result.chance, result.over_cap))

    def test_最大格挡加成后仍受硬上限约束(self) -> None:
        """基础 75 + 加成 20 = 95，但硬上限 90。"""
        result = mit.block(100, extra_max_block=20)
        self.assertEqual(90, result.max_block)
        self.assertEqual(90, result.chance)

    def test_护甲减伤是取整后的百分比(self) -> None:
        """10000 护甲对 1000 的打击：10000/(10000+5000) = 66.67% → 取整 67%。"""
        result = mit.armour(10000, 1000)
        self.assertEqual(67, result.reduction)
        self.assertAlmostEqual(330.0, result.damage_after, places=2)

    def test_同样的护甲对越大的打击减得越少(self) -> None:
        """这是「护甲不是固定百分比减伤」的可判形式。"""
        small = mit.armour(10000, 200).reduction
        large = mit.armour(10000, 5000).reduction
        self.assertGreater(small, large)
        # 200 的打击：90.9% → 取整 91%，再被 90% 上限夹住；5000 的打击只剩 29%
        self.assertEqual((90, 29), (small, large))

    def test_护甲减伤夹在物理减伤上限内(self) -> None:
        self.assertEqual(90, mit.armour(10**7, 100).reduction)
        self.assertEqual(90, mit.armour(10000, 1000, flat_reduction=40).reduction)

    def test_零护甲零伤害不报错且减伤为零(self) -> None:
        """PoB 对 `armour == 0 and raw == 0` 有专门的分支，照抄。"""
        self.assertEqual(0, mit.armour(0, 0).reduction)

    def test_命中率有下限闪避率有上限(self) -> None:
        """闪避堆到无穷也躲不掉全部攻击：命中率下限 5%、闪避率上限 95%。"""
        self.assertEqual(mit.HIT_CHANCE_FLOOR, mit.hit_chance(10**9, 559))
        result = mit.evade(10**9, accuracy=559)
        self.assertEqual(95.0, result.evade_chance)
        self.assertTrue(result.capped)

    def test_没有闪避就是必被命中(self) -> None:
        """`0 / 0` 在上游是未定义行为，这里刻意取「必被命中」。"""
        self.assertEqual(100, mit.hit_chance(0, 0))
        self.assertEqual(100, mit.hit_chance(0, 559))

    def test_命中值为负直接取下限(self) -> None:
        self.assertEqual(mit.HIT_CHANCE_FLOOR, mit.hit_chance(1000, -1))

    def test_闪避率按怪物等级表查命中值(self) -> None:
        """85 级怪物命中值 559 → 10000 闪避时命中率 47%、闪避率 53%。"""
        result = mit.evade(10000, monster_level=85)
        self.assertEqual(559.0, result.accuracy)
        self.assertEqual((47, 53.0), (result.hit_chance, result.evade_chance))

    def test_等级表越界抛异常而不是外推(self) -> None:
        with self.assertRaises(mit.OutOfTableRange):
            mit.monster_accuracy(120)
        with self.assertRaises(mit.OutOfTableRange):
            mit.monster_accuracy(0)

    def test_既没有命中值也没有等级就拒绝计算(self) -> None:
        """替用户假定一个 85 级，就是给结论装一个编造的前提。"""
        with self.assertRaises(ValueError):
            mit.evade(10000)

    def test_法术压制减的是四十而不是五十(self) -> None:
        result = mit.suppression(100)
        self.assertEqual(40.0, result.effect)
        self.assertEqual(40.0, result.average_reduction)

    def test_压制几率夹在上限内(self) -> None:
        self.assertEqual(100.0, mit.suppression(150).chance)

    def test_常量缺失时抛异常而不是给默认值(self) -> None:
        constants.reset_cache()
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.executescript(CONSTANT_SCHEMA)
        constants.load(empty, force=True)
        with self.assertRaises(constants.MissingConstant):
            mit.armour(10000, 1000)
        with self.assertRaises(constants.MissingConstant):
            mit.block(60)
        empty.close()


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    """把上游公式原文钉在行号上：改了或挪了，这里立刻红。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.defence = (POB_ROOT_ENV / "src" / "Modules" / "CalcDefence.lua").read_text(
            encoding="utf-8"
        )

    def _lines(self, span: tuple[int, int]) -> str:
        return "\n".join(self.defence.splitlines()[span[0] - 1: span[1]])

    def test_命中率公式还在给出的行号范围内(self) -> None:
        block = self._lines(mit.HIT_CHANCE_LINES)
        self.assertIn("function calcs.hitChance(evasion, accuracy)", block)
        self.assertIn("accuracy / (accuracy + (evasion / 5) ^ 0.9) * 125", block)
        self.assertIn(f"m_max(m_min(round(rawChance), 100), {mit.HIT_CHANCE_FLOOR})", block)

    def test_护甲减伤公式还在给出的行号范围内(self) -> None:
        block = self._lines(mit.ARMOUR_LINES)
        self.assertIn("(armour / (armour + raw * 5) * 100)", block)
        self.assertIn("function calcs.armourReduction(armour, raw)", block)

    def test_闪避率上限还在给出的行号范围内(self) -> None:
        block = self._lines(mit.EVADE_LINES)
        self.assertIn("data.misc.EvadeChanceCap", block)
        self.assertIn("calcs.hitChance(meleeEvadeStat, enemyAccuracy)", block)

    def test_格挡上限还在给出的行号范围内(self) -> None:
        block = self._lines(mit.BLOCK_LINES)
        self.assertIn('m_min(modDB:Sum("BASE", nil, "BlockChanceMax"), data.misc.BlockChanceCap)',
                      block)
        self.assertIn("output.BlockChanceOverCap = m_max(0, totalBlockChance - output.BlockChanceMax)",
                      block)

    def test_物理减伤上限取的是角色常量而不是另一份副本(self) -> None:
        """上游 `data.misc.DamageReductionCap` 只是 `characterConstants` 那条的引用，
        所以白名单**故意不收**它——收了就是多一份出处更差的副本。"""
        self.assertIn(
            'DamageReductionCap = data.characterConstants["maximum_physical_damage_reduction_%"]',
            (POB_ROOT_ENV / "src" / "Modules" / "Data.lua").read_text(encoding="utf-8"),
        )

    def test_怪物命中值就是等级表那一项(self) -> None:
        setup = (POB_ROOT_ENV / "src" / "Modules" / "CalcSetup.lua").read_text(encoding="utf-8")
        self.assertIn(
            'enemyDB:NewMod("Accuracy", "BASE", env.data.monsterAccuracyTable[env.enemyLevel], "Base")',
            setup,
        )


class DeriveHelperTest(unittest.TestCase):
    """两个计算器共用的推导件（第二十一轮从 `calc/resistance.py` 抽出来）。"""

    def test_负数写成减号而不是加负号(self) -> None:
        self.assertEqual("120% - 60%", join_terms([120, -60]))
        self.assertEqual("120% + 48% - 60%", join_terms([120, 48, -60]))

    def test_引用推到同一个commit(self) -> None:
        url = pob_url(f"{BLOB}/src/Modules/Data.lua#L239", mit.DEFENCE_FILE, lines=(57, 63))
        self.assertEqual(f"{BLOB}/src/Modules/CalcDefence.lua#L57-L63", url)

    def test_推不出引用时返回原地址(self) -> None:
        self.assertEqual("不是 blob 地址", pob_url("不是 blob 地址", mit.DEFENCE_FILE))


class MitigationToolTest(MitigationTestCase):
    def setUp(self) -> None:
        super().setUp()
        from tools.impl.defence import mitigation_calc

        self.tool = mitigation_calc

    def test_没给数值时讲规则与顺序(self) -> None:
        result = self.tool()
        self.assertEqual("rules", result.data["kind"])
        text = result.citations[0]["text"]
        self.assertIn("结算顺序", text)
        self.assertIn("40%", text)  # 压制效果，不是 50%

    def test_只给护甲不给伤害时说清缺什么(self) -> None:
        """这是这一维最重要的一条：没有伤害数值就没有减伤百分比。
        替用户假定一个伤害值，等于给结论装一个编造的前提。"""
        result = self.tool(armour=10000)
        self.assertEqual("rules", result.data["kind"])
        self.assertNotIn("armour", result.data)
        text = result.citations[0]["text"]
        self.assertIn("没给这一击的伤害", text)

    def test_只给闪避不给命中值或等级时说清缺什么(self) -> None:
        result = self.tool(evasion=10000)
        self.assertEqual("rules", result.data["kind"])
        self.assertIn("闪避率算不出来", result.citations[0]["text"])

    def test_护甲减伤给逐步推导(self) -> None:
        result = self.tool(armour=10000, hit_damage=1000)
        self.assertEqual(67, result.data["armour"]["reduction"])
        text = result.citations[0]["text"]
        self.assertIn("| 步骤 | 算式 | 结果 |", text)
        self.assertIn("10000 / (10000 + 5 × 1000) × 100", text)
        self.assertIn("护甲不是固定百分比减伤", text)

    def test_闪避按怪物等级算并写明用了哪一级(self) -> None:
        result = self.tool(evasion=10000, monster_level=85)
        self.assertEqual((47, 53.0), (result.data["evade"]["hit_chance"],
                                      result.data["evade"]["evade_chance"]))
        self.assertIn("85 级怪物的命中值", result.citations[0]["text"])

    def test_多段一起给时逐段列出(self) -> None:
        result = self.tool(armour=10000, hit_damage=1000, evasion=6000, monster_level=83,
                           block_chance=60, suppression_chance=100)
        text = result.citations[0]["text"]
        for section in ("### 闪避与命中率", "### 护甲减伤", "### 攻击格挡", "### 法术压制"):
            self.assertIn(section, text)
        self.assertIn("结算顺序", text)

    def test_压制结果区分几率与减伤比例(self) -> None:
        result = self.tool(suppression_chance=100)
        text = result.citations[0]["text"]
        self.assertIn("压制几率 100%", text)
        self.assertIn("每次触发减 40%", text)
        self.assertIn("不等于免疫", text)

    def test_只给最大格挡加成也答上限本身(self) -> None:
        result = self.tool(extra_max_block=5)
        self.assertEqual(80, result.data["block"]["max_block"])

    def test_超出上限的格挡写明要先提高最大格挡(self) -> None:
        result = self.tool(block_chance=85)
        self.assertEqual(10.0, result.data["block"]["over_cap"])
        self.assertIn("先提高**最大**格挡", result.citations[0]["text"])

    def test_等级越界直接失败而不是外推(self) -> None:
        self.assertFalse(self.tool(evasion=1000, monster_level=200).ok)

    def test_边界声明进data由代码附注(self) -> None:
        for kwargs in ({}, {"armour": 10000, "hit_damage": 1000}):
            with self.subTest(kwargs=kwargs):
                self.assertIn("头目", self.tool(**kwargs).data["boundary"])

    def test_证据带钉住commit的行号锚点(self) -> None:
        citation = self.tool(armour=10000, hit_damage=1000).citations[0]
        self.assertIn("CalcDefence.lua#L", citation["url"])
        self.assertEqual("derived", citation["channel"])

    def test_常量没入库时报错而不是给默认值(self) -> None:
        constants.reset_cache()
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.executescript(CONSTANT_SCHEMA)
        constants.load(empty, force=True)
        self.assertFalse(self.tool(armour=10000, hit_damage=1000).ok)
        empty.close()

    def test_证据没有被静默截断(self) -> None:
        """推导表的末尾是来源清单，悄悄截掉它等于引用消失。"""
        from tools.impl.defence import MAX_EVIDENCE_CHARS

        text = self.tool(
            armour=10000, hit_damage=1000, evasion=6000, monster_level=83,
            block_chance=60, spell_block_chance=40, suppression_chance=100,
            flat_physical_reduction=20, extra_max_block=5,
        ).citations[0]["text"]
        self.assertLessEqual(len(text), MAX_EVIDENCE_CHARS)
        self.assertNotIn("已截断", text)
        self.assertIn("来源：", text)


class MitigationRouteTest(unittest.TestCase):
    """成对写：该抢的抢、不该抢的一条都不许抢。"""

    def route(self, question: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(question)

    def test_结算问法路由到受击计算器(self) -> None:
        for question in (
            "护甲有什么用", "10000 护甲能减多少物理伤害", "格挡上限是多少",
            "最大格挡怎么提高", "法术压制减多少伤害", "多少闪避才能躲开 85 级怪物",
            "物理减伤上限是多少",
        ):
            with self.subTest(question=question):
                self.assertEqual("mitigation_calc", self.route(question))

    def test_抗性问法仍归抗性计算器(self) -> None:
        """两个计算器是一条链的两段，判据不能互相渗透。"""
        self.assertEqual("resistance_calc", self.route("抗性上限是多少"))
        self.assertEqual("resistance_calc", self.route("我火抗 120 是不是浪费了"))
        self.assertEqual("resistance_calc", self.route("火抗 120、护甲 10000 的图我还剩多少抗"))

    def test_枚举问句永远不是计算题(self) -> None:
        """计算器的话题同时也是属性名，所以遇到枚举意图词必须让路
        （否则「法术压制能从哪些渠道获得」会被抢走——实测发生过）。"""
        for stat in ("护甲", "闪避", "闪避率", "格挡", "最大格挡", "法术压制",
                     "物理减伤", "抗性上限"):
            with self.subTest(stat=stat):
                self.assertEqual("stat_sources", self.route(f"{stat}能从哪些渠道获得"))

    def test_档位问法不被抢走(self) -> None:
        self.assertEqual("mod_tiers", self.route("+400 护甲这条词缀有几档"))

    def test_没有专指结算的问法不抢(self) -> None:
        self.assertEqual("", self.route("哪些底材护甲最高"))


class MitigationGateTest(unittest.TestCase):
    """受击问法不得被维度闸门拦掉——常量域已入库，且现在有工具。"""

    def test_受击问法不被判成缺口(self) -> None:
        from kb.coverage import dimension_gap

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE game_constant(group_name TEXT)")
        conn.execute("INSERT INTO game_constant VALUES ('defence')")
        conn.execute("CREATE TABLE chunk(entity_id TEXT)")
        try:
            for question in ("护甲能减多少伤害", "格挡上限是多少", "法术压制减多少伤害"):
                with self.subTest(question=question):
                    verdict = dimension_gap(conn, question)
                    self.assertFalse(verdict is not None and verdict.is_gap)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
