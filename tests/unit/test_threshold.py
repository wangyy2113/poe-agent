"""门槛型异常：门槛怎么算、强度公式、反函数，以及一批「取不出来的常量」（第二十六轮）。

这一轮有三件事值得钉住：

1. **一个入库了却取不出来的常量类**。`mapLevelLifeMult.66..90` 这类稀疏表条目原先
   落成 `kind='other'` + `value_num=NULL` + 数字塞在 `value_json` 里，于是
   `constants.get()` 抛 `ConstantTypeError`、`table()` 又不是数组——**46 行死数据**。
   门槛计算正好需要它们，这一轮把 `flatten()` 改成数值型 other 也落 `value_num`。
2. **头目倍率 7.68 是上游硬编码的字面量**，不在常量表，所以直接对着原文钉住。
3. **反函数方向容易写反**：PoB 的 `thresh` 返回「某伤害下达到 X 所需的**门槛**」，
   而用户问的是「要达到 X 需要多大**伤害**」。照抄会得到「需要的伤害 = 当前伤害」
   这种自证式废话（实测踩过一次）。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import constants  # noqa: E402
from calc import threshold as th  # noqa: E402
from calc.derive import fmt  # noqa: E402
from tests.unit.test_resistance import (  # noqa: E402
    BLOB,
    CONSTANT_SCHEMA,
    POB_ROOT_ENV,
)

#: 门槛表只放用例需要的几级（1、60、83、85），其余填 0 占位。
THRESHOLD_TABLE = [0.0] * 100
THRESHOLD_TABLE[0] = 22.0
THRESHOLD_TABLE[59] = 3698.0
THRESHOLD_TABLE[82] = 15319.0
THRESHOLD_TABLE[84] = 17268.0
BOSS_LIFE_TABLE = [0.0] * 90
BOSS_LIFE_TABLE[82] = 19.64
BOSS_AILMENT_TABLE = [0.0] * 90
BOSS_AILMENT_TABLE[82] = 0.47

AILMENT_FIELDS = {
    "Chill": {"default": 10, "min": 5, "max": 30, "precision": 0, "duration": 2},
    "Freeze": {"min": 0.3, "max": 3, "precision": 2},
    "Shock": {"default": 15, "min": 5, "max": 50, "precision": 0, "duration": 2},
    "Scorch": {"default": 10, "min": 0, "max": 30, "precision": 0, "duration": 4},
    "Brittle": {"default": 2, "min": 0, "max": 6, "precision": 2, "duration": 4},
    "Sap": {"default": 6, "min": 0, "max": 20, "precision": 0, "duration": 4},
}


def fixture_conn() -> sqlite3.Connection:
    import json

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(CONSTANT_SCHEMA)
    url = f"{BLOB}/src/Data/Misc.lua#L14"
    conn.executemany(
        "INSERT INTO game_constant VALUES (?,?,?,1,'level_table',NULL,?,'Path of Building',"
        "2,?,'PoB 2.66.2 / tree 3.29','3.29',datetime('now'))",
        [
            (constants.MONSTER_AILMENT_THRESHOLD_TABLE, "monsterAilmentThresholdTable",
             "DefaultMonsterStats.dat", json.dumps(THRESHOLD_TABLE), url),
            ("mapLevelBossLifeMult", "mapLevelBossLifeMult", None,
             json.dumps(BOSS_LIFE_TABLE), url),
            ("mapLevelBossAilmentMult", "mapLevelBossAilmentMult", None,
             json.dumps(BOSS_AILMENT_TABLE), url),
        ],
    )
    rows = [("mapLevelLifeMult.83", "mapLevelLifeMult", 2.09)]
    for ailment, fields in AILMENT_FIELDS.items():
        for field, value in fields.items():
            rows.append(
                (f"pobMisc.nonDamagingAilment.{ailment}.{field}", "pobMisc", float(value))
            )
    conn.executemany(
        "INSERT INTO game_constant VALUES (?,?,NULL,0,'scalar',?,NULL,'Path of Building',"
        "2,?,'PoB 2.66.2 / tree 3.29','3.29',datetime('now'))",
        [(key, group, value, url) for key, group, value in rows],
    )
    conn.commit()
    return conn


class ThresholdTestCase(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.conn = fixture_conn()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        constants.reset_cache()
        self.conn.close()


class EnemyThresholdTest(ThresholdTestCase):
    def test_低于六十六级不乘地图倍率(self) -> None:
        """地图倍率表从 66 级才开始；60 级怪物的门槛就是等级表那一项。"""
        result = th.enemy_threshold(60)
        self.assertEqual(3698.0, result.threshold)
        self.assertEqual(1.0, result.map_mult)

    def test_满级地图普通怪(self) -> None:
        """83 级：15319 × 2.09 = 32016.71。"""
        result = th.enemy_threshold(83)
        self.assertAlmostEqual(32016.71, result.threshold, places=2)

    def test_头目要乘七点六八与头目地图倍率(self) -> None:
        """15319 × 7.68 × (19.64 × 0.47) ≈ 1,086,003——头目门槛比小怪高 34 倍。"""
        result = th.enemy_threshold(83, is_boss=True)
        self.assertAlmostEqual(1086002.88, result.threshold, places=2)
        self.assertEqual(th.BOSS_TYPE_MULT, result.boss_mult)
        normal = th.enemy_threshold(83).threshold
        self.assertGreater(result.threshold / normal, 30)

    def test_等级越界抛异常而不是外推(self) -> None:
        for level in (0, 120):
            with self.subTest(level=level):
                with self.assertRaises(th.OutOfTableRange):
                    th.enemy_threshold(level)


class ReachableConstantsTest(ThresholdTestCase):
    """稀疏表条目必须能取出来——这一轮修的正是它取不出来。"""

    def test_稀疏表条目是标量而不是json(self) -> None:
        """`mapLevelLifeMult.83` 原先 value_num 为 NULL、数字塞在 value_json 里，
        于是 `get()` 抛 ConstantTypeError——库里有值却没人能读到。"""
        self.assertEqual(2.09, constants.get("mapLevelLifeMult.83"))

    def test_异常上下限也是标量(self) -> None:
        self.assertEqual(50.0, constants.get("pobMisc.nonDamagingAilment.Shock.max"))
        self.assertEqual(0.3, constants.get("pobMisc.nonDamagingAilment.Freeze.min"))


class EffectTest(ThresholdTestCase):
    def test_比值为一时正好是五十(self) -> None:
        """一条能记住的标尺：这一击等于门槛 ⇒ 感电 50 ×1^0.4 = 50%（也就是上限）。"""
        result = th.ailment_effect("感电", 32016.71, level=83)
        self.assertEqual(50.0, result.effect)
        self.assertTrue(result.capped)

    def test_打头目同一击弱得多(self) -> None:
        """同样 10 万伤害：打小怪满感电，打 83 级头目只有 19%。"""
        boss = th.ailment_effect("感电", 100000, level=83, is_boss=True)
        normal = th.ailment_effect("感电", 100000, level=83)
        self.assertEqual(19.0, boss.effect)
        self.assertEqual(50.0, normal.effect)

    def test_按precision取整(self) -> None:
        """感电 precision=0 → 整数；脆弱 precision=2 → 两位小数。
        不按它取整，答案里会出现 19.2592% 这种游戏里不存在的精度。"""
        shock = th.ailment_effect("感电", 100000, level=83, is_boss=True)
        self.assertEqual(shock.effect, round(shock.effect))
        brittle = th.ailment_effect("脆弱", 100000, level=83, is_boss=True)
        self.assertEqual(round(brittle.raw_effect, 2), brittle.effect)

    def test_夹在上下限之间(self) -> None:
        weak = th.ailment_effect("感电", 1, level=83, is_boss=True)
        self.assertEqual(5.0, weak.effect)  # 下限
        self.assertTrue(weak.below_min)
        strong = th.ailment_effect("感电", 10**9, level=83, is_boss=True)
        self.assertEqual(50.0, strong.effect)  # 上限

    def test_五种异常的基数不同(self) -> None:
        """脆弱基数 10、干枯 100/3——用同一个 50 去算另两种就是错数字。"""
        bases = {name: th.AILMENTS[name]["base"] for name in th.AILMENTS}
        self.assertEqual(50.0, bases["感电"])
        self.assertEqual(10.0, bases["脆弱"])
        self.assertAlmostEqual(100 / 3, float(bases["干枯"]))

    def test_效果加成乘在强度上(self) -> None:
        base = th.ailment_effect("脆弱", 100000, level=83, is_boss=True)
        boosted = th.ailment_effect(
            "脆弱", 100000, level=83, is_boss=True, effect_mod_pct=50
        )
        self.assertAlmostEqual(base.raw_effect * 1.5, boosted.raw_effect, places=6)

    def test_直接给门槛就不查表(self) -> None:
        result = th.ailment_effect("感电", 1000, threshold=1000)
        self.assertEqual(1000.0, result.threshold)
        self.assertEqual(50.0, result.effect)

    def test_伤害性异常与冰冻都不在这里(self) -> None:
        for name in ("点燃", "中毒", "流血", "冰冻"):
            with self.subTest(name=name):
                with self.assertRaises(th.UnknownAilment):
                    th.ailment_effect(name, 1000, threshold=1000)


class InverseTest(ThresholdTestCase):
    """反函数方向写反过一次，这里把方向钉住。"""

    def test_要感电到上限就得打出一整条门槛(self) -> None:
        needed = th.damage_for_effect("感电", 50, 1086002.88)
        self.assertAlmostEqual(1086002.88, needed, places=2)

    def test_三十个百分点只需要不到三成伤害(self) -> None:
        """(30/50)^2.5 ≈ 0.2788——曲线是 0.4 次方，所以低强度便宜得多。"""
        needed = th.damage_for_effect("感电", 30, 1086002.88)
        self.assertAlmostEqual(302837.12, needed, places=2)

    def test_结果卡里的所需伤害不等于当前伤害(self) -> None:
        """照抄 PoB 的 `thresh` 会得到「需要的伤害 = 当前伤害」这种自证式废话。"""
        result = th.ailment_effect("感电", 100000, level=83, is_boss=True)
        self.assertNotAlmostEqual(result.hit_damage, result.damage_for_max, places=2)
        self.assertAlmostEqual(result.threshold, result.damage_for_max, places=2)

    def test_目标强度非正直接报错(self) -> None:
        with self.assertRaises(ValueError):
            th.damage_for_effect("感电", 0, 1000)


class FreezeNoteTest(ThresholdTestCase):
    def test_只给能钉住的那条门槛判据(self) -> None:
        low, high, max_threshold = th.freeze_threshold_note(100000)
        self.assertEqual((0.3, 3.0), (low, high))
        self.assertEqual(2000000, max_threshold)


class NumberFormatTest(unittest.TestCase):
    """大数不能写成科学计数法——那种写法没法核对，也会让数值回填对不上。"""

    def test_大数用千分位(self) -> None:
        self.assertEqual("1,086,003", fmt(1086003))
        self.assertEqual("35,791,394", fmt(35791394))

    def test_小数与小整数照旧(self) -> None:
        self.assertEqual("0.4", fmt(0.4))
        self.assertEqual("19", fmt(19))
        self.assertEqual("9.2308", fmt(9.2308))


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = POB_ROOT_ENV / "src"
        cls.offence = (root / "Modules" / "CalcOffence.lua").read_text(encoding="utf-8")
        cls.data = (root / "Modules" / "Data.lua").read_text(encoding="utf-8")

    def _lines(self, span: tuple[int, int]) -> str:
        return "\n".join(self.offence.splitlines()[span[0] - 1: span[1]])

    def test_头目倍率七点六八还写在原文里(self) -> None:
        """它是硬编码字面量、不在常量表，所以只能这样钉。"""
        block = self._lines(th.THRESHOLD_LINES)
        self.assertIn(f"local enemyTypeMult = isBoss and {th.BOSS_TYPE_MULT} or 1", block)

    def test_门槛的乘法链还在(self) -> None:
        block = self._lines(th.THRESHOLD_LINES)
        self.assertIn("data.monsterAilmentThresholdTable[env.enemyLevel]", block)
        self.assertIn("enemyAilmentThreshold * enemyTypeMult * enemyMapLifeMult", block)
        self.assertIn("data.mapLevelBossAilmentMult[env.enemyLevel]", block)

    def test_强度公式与它的反函数还在(self) -> None:
        block = self._lines(th.EFFECT_LINES)
        self.assertIn("return 50 * ((damage / enemyThreshold) ^ 0.4) * effectMod", block)
        self.assertIn("return damage * ((50 * effectMod / value) ^ 2.5)", block)
        self.assertIn("return 10 * ((damage / enemyThreshold) ^ 0.4) * effectMod", block)
        self.assertIn("return (100 / 3) * ((damage / enemyThreshold) ^ 0.4) * effectMod", block)

    def test_冰冻那句门槛判据还在给出的行号上(self) -> None:
        line = self.offence.splitlines()[th.FREEZE_NOTE_LINE - 1]
        self.assertIn("For freeze to apply for the minimum of 0.3 seconds", line)
        self.assertIn("baseVal * 20", line)

    def test_六种异常的上下限还在数据表里(self) -> None:
        for ailment, fields in AILMENT_FIELDS.items():
            with self.subTest(ailment=ailment):
                self.assertIn(f'["{ailment}"] = {{ associatedType', self.data)
                self.assertIn(f"max = {fields['max']:g}", self.data)


class ThresholdToolTest(ThresholdTestCase):
    def setUp(self) -> None:
        super().setUp()
        from tools.impl.thresholds import ailment_threshold_calc

        self.tool = ailment_threshold_calc

    def test_没给伤害时讲规则并列出五种(self) -> None:
        result = self.tool()
        self.assertEqual("rules", result.data["kind"])
        text = result.citations[0]["text"]
        self.assertIn("| 感电（Shock） | 50 | 5–50% | 2 秒 | 闪电 |", text)
        self.assertIn("| 脆弱（Brittle） | 10 | 0–6% | 4 秒 | 冰霜 |", text)

    def test_给了伤害但没说打谁时说清缺什么(self) -> None:
        """强度取决于门槛，而门槛随等级与是否头目差几十倍——不能替用户假定 83 级。"""
        result = self.tool(ailment="感电", hit_damage=100000)
        self.assertEqual("rules", result.data["kind"])
        self.assertIn("没说打**谁**", result.citations[0]["text"])

    def test_给全了就出逐步推导(self) -> None:
        result = self.tool(ailment="感电", hit_damage=100000, monster_level=83, is_boss=True)
        self.assertEqual(19.0, result.data["effect"])
        text = result.citations[0]["text"]
        self.assertIn("| 步骤 | 算式 | 结果 |", text)
        self.assertIn("7.68", text)

    def test_顶到上限时说明再高也没用(self) -> None:
        result = self.tool(ailment="感电", hit_damage=100000, monster_level=83)
        self.assertTrue(result.data["capped"])
        self.assertIn("再高的伤害不会让感电更强", result.citations[0]["text"])

    def test_反问要多大伤害(self) -> None:
        result = self.tool(ailment="感电", target_effect=30, monster_level=83, is_boss=True)
        self.assertEqual("inverse", result.data["kind"])
        self.assertAlmostEqual(302837.12, result.data["damage_needed"], places=2)

    def test_冰冻给半个答案而不是报错(self) -> None:
        """第一次实测：工具对冰冻直接报错，模型连着调三次、把预算烧光，
        一个字都没答出来——契约（该拒答）过了、机制是坏的。
        能答的那一半（门槛判据）必须以证据形式给出去。"""
        result = self.tool(ailment="冰冻", hit_damage=100000, monster_level=83, is_boss=True)
        self.assertTrue(result.ok)
        self.assertEqual("freeze", result.data["kind"])
        self.assertTrue(result.data["can_freeze_minimum"])
        text = result.citations[0]["text"]
        self.assertIn("2,000,000", text)          # 20 倍门槛上限
        self.assertIn("具体几秒仍然算不出来", text)  # 不编的那一半

    def test_冰冻没给伤害时只给上下限与判据(self) -> None:
        result = self.tool(ailment="冰冻")
        self.assertEqual("freeze", result.data["kind"])
        self.assertNotIn("max_threshold_for_min_freeze", result.data)
        self.assertIn("0.3–3 秒", result.citations[0]["text"])

    def test_伤害性异常引导到另一个工具(self) -> None:
        result = self.tool(ailment="点燃", hit_damage=100000, monster_level=83)
        self.assertFalse(result.ok)
        self.assertIn("ailment_dot_calc", result.error)

    def test_边界声明进data由代码附注(self) -> None:
        for kwargs in ({}, {"ailment": "感电", "hit_damage": 100, "monster_level": 83}):
            with self.subTest(kwargs=kwargs):
                self.assertIn("冰冻", self.tool(**kwargs).data["boundary"])

    def test_大数不写成科学计数法(self) -> None:
        text = self.tool(
            ailment="感电", hit_damage=100000, monster_level=83, is_boss=True
        ).citations[0]["text"]
        self.assertIn("1,086,003", text)
        self.assertNotIn("e+06", text)

    def test_引用带钉住commit的行号锚点(self) -> None:
        citation = self.tool(
            ailment="感电", hit_damage=100, monster_level=83
        ).citations[0]
        self.assertIn("CalcOffence.lua#L", citation["url"])
        self.assertEqual("derived", citation["channel"])


class ThresholdRouteTest(unittest.TestCase):
    def route(self, question: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(question)

    def test_门槛型问法路由到门槛计算器(self) -> None:
        for question in (
            "感电上限是多少", "打 83 级头目能感电多少", "异常门槛是什么",
            "多少伤害能冻住 boss", "冰缓上限是多少",
        ):
            with self.subTest(question=question):
                self.assertEqual("ailment_threshold_calc", self.route(question))

    def test_伤害性异常仍归另一个工具(self) -> None:
        """两套公式完全不同，判据不能互相渗透。"""
        self.assertEqual("ailment_dot_calc", self.route("点燃每秒打多少伤害"))
        self.assertEqual("ailment_dot_calc", self.route("中毒能叠几层"))

    def test_枚举问句仍归stat_sources(self) -> None:
        self.assertEqual("stat_sources", self.route("感电能从哪些渠道获得"))

    def test_词缀类问法不被抢走(self) -> None:
        self.assertEqual("", self.route("哪些词缀能造成感电"))


if __name__ == "__main__":
    unittest.main()
