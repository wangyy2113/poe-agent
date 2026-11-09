"""命中率与攻击速度：攻击侧的第二、三块（第三十三轮）。

这一轮的验证方式有三层，和前面几块计算器一致：

1. **复用同一个函数**：命中率与 `calc/mitigation.py` 的闪避是同一个
   `calcs.hitChance`，所以这里直接调它——两处各写一份就是两份会分叉的口径；
2. **公式钉住上游原文**：攻击侧那两行（精准命中率、敌方格挡）与攻速那一段，
   连行号锚点一起断言（数据漂移有 `check_doc_facts.py` 拦，公式漂移只有这个）；
3. **回代**：`accuracy_for_chance` 给出的命中值必须**刚好达标**——它达标、
   比它小 1 就不达标。方向写反、不等式写反，这一条立刻红。

第三条是这一维唯一有力的自洽检查：上游没有可对账的期望值。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import accuracy as acc  # noqa: E402
from calc import attack_speed as spd  # noqa: E402
from calc import constants, mitigation  # noqa: E402
from core.db import SCHEMA  # noqa: E402
from tests.unit.test_resistance import BLOB, CONSTANT_SCHEMA, POB_ROOT_ENV  # noqa: E402

#: 83 级普通怪的闪避（monsterEvasionTable 第 83 项，实测值）
EVASION_83 = 7860.0
EVASION_68 = 1310.0


def constant_conn() -> sqlite3.Connection:
    """内存常量库：只放这一维要用的两张表（闪避表 + 命中表）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(CONSTANT_SCHEMA)
    for key, values in (
        (constants.MONSTER_EVASION_TABLE, {68: EVASION_68, 83: EVASION_83, 84: 8221.0}),
        (constants.MONSTER_ACCURACY_TABLE, {83: 518.0, 85: 559.0}),
    ):
        table = [0.0] * 100
        for level, value in values.items():
            table[level - 1] = value
        conn.execute(
            "INSERT INTO game_constant VALUES (?,?,?,0,'array',NULL,?,'Path of Building',2,?,"
            "'PoB 2.66.2 / tree 3.29','3.29',datetime('now'))",
            (
                key, "monsterTables", "Metadata/Monsters/Monster.ot",
                __import__("json").dumps(table),
                f"{BLOB}/src/Modules/Data.lua#L200",
            ),
        )
    conn.commit()
    return conn


class AccuracyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.conn = constant_conn()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        constants.reset_cache()
        self.conn.close()


class HitChanceTest(AccuracyTestCase):
    def test_命中值与敌方等级算出命中率(self) -> None:
        """1500 命中打 83 级普通怪 = 83%。闪避是查表来的，不是假定的。"""
        result = acc.hit_chance_vs_enemy(1500, enemy_level=83)
        self.assertEqual(83, result.accuracy_hit_chance)
        self.assertEqual(EVASION_83, result.enemy_evasion)

    def test_与受击侧用的是同一个函数(self) -> None:
        """闪避那一侧算的是「怪物打我」，这里是「我打怪物」，公式同一个。
        两处各写一份判据就会分叉——取整与 5% 下限也就跟着不一致。"""
        self.assertEqual(
            mitigation.hit_chance(EVASION_83, 1500),
            acc.hit_chance_vs_enemy(1500, enemy_level=83).accuracy_hit_chance,
        )

    def test_等级越低越好打(self) -> None:
        low = acc.hit_chance_vs_enemy(1500, enemy_level=68).accuracy_hit_chance
        high = acc.hit_chance_vs_enemy(1500, enemy_level=83).accuracy_hit_chance
        self.assertGreater(low, high)

    def test_命中率有下限五(self) -> None:
        """命中值再低也有 5%（与闪避那一侧的上限 95% 对称）。"""
        result = acc.hit_chance_vs_enemy(0, enemy_level=83)
        self.assertEqual(mitigation.HIT_CHANCE_FLOOR, result.accuracy_hit_chance)
        self.assertTrue(result.at_floor)

    def test_敌方格挡是另一层(self) -> None:
        """挡下来的那部分打不出伤害，但它不是「没命中」——两层分开写。"""
        result = acc.hit_chance_vs_enemy(1500, enemy_level=83, enemy_block_pct=25)
        self.assertEqual(83, result.accuracy_hit_chance)
        self.assertAlmostEqual(62.25, result.hit_chance, places=2)

    def test_无法被闪避直接一百(self) -> None:
        result = acc.hit_chance_vs_enemy(10, enemy_level=83, cannot_be_evaded=True)
        self.assertEqual(100, result.accuracy_hit_chance)
        self.assertTrue(result.always_hits)

    def test_不给等级也不给闪避就报错(self) -> None:
        """替用户假定「打 83 级」就是把「打谁」这个前提凭空补上。"""
        with self.assertRaises(ValueError):
            acc.hit_chance_vs_enemy(1500)

    def test_直接给闪避时不写等级(self) -> None:
        result = acc.hit_chance_vs_enemy(1500, enemy_evasion=10000)
        self.assertIsNone(result.enemy_level)
        self.assertEqual(10000.0, result.enemy_evasion)

    def test_等级越界抛异常不外推(self) -> None:
        with self.assertRaises(mitigation.OutOfTableRange):
            acc.hit_chance_vs_enemy(1500, enemy_level=120)

    def test_常量缺失时抛异常(self) -> None:
        constants.reset_cache()
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.executescript(CONSTANT_SCHEMA)
        constants.load(empty, force=True)
        with self.assertRaises((constants.MissingConstant, mitigation.OutOfTableRange)):
            acc.hit_chance_vs_enemy(1500, enemy_level=83)
        empty.close()


class RoundTripTest(AccuracyTestCase):
    """**回代**：反解出来的命中值必须刚好达标。这一维没有可对账的期望值，
    所以这是唯一有力的自洽检查——方向写反、不等式写反都会立刻红。"""

    CASES = ((90, 83), (75, 83), (100, 83), (50, 68), (95, 84), (6, 83))

    def test_刚好达标(self) -> None:
        for target, level in self.CASES:
            with self.subTest(target=target, level=level):
                needed = acc.accuracy_for_chance(target, enemy_level=level)
                evasion = acc.monster_evasion(level)
                self.assertGreaterEqual(mitigation.hit_chance(evasion, needed.accuracy), target)
                self.assertLess(
                    mitigation.hit_chance(evasion, max(needed.accuracy - 1, 0)), target,
                    "少 1 点命中值就该不达标，否则给的不是最小值",
                )

    def test_反解的方向不能写反(self) -> None:
        """第二十六轮在异常门槛上踩过：照抄上游会得到「需要的命中值 = 当前命中值」
        这种自证式废话。这里钉住方向——目标越高，需要的命中值越多。"""
        low = acc.accuracy_for_chance(70, enemy_level=83).accuracy
        high = acc.accuracy_for_chance(95, enemy_level=83).accuracy
        self.assertGreater(high, low)

    def test_下限以内不需要命中值(self) -> None:
        needed = acc.accuracy_for_chance(5, enemy_level=83)
        self.assertEqual(0.0, needed.accuracy)

    def test_目标越界报错(self) -> None:
        for target in (0, -5, 101):
            with self.subTest(target=target), self.assertRaises(ValueError):
                acc.accuracy_for_chance(target, enemy_level=83)


class AttackSpeedTest(unittest.TestCase):
    """攻速不依赖常量，但两个取整/顺序细节要钉住。"""

    def test_技能倍率乘在武器攻速上(self) -> None:
        """PoB 存的是增量：-30 表示「基础攻速的 70%」。"""
        result = spd.attack_rate(1.55, skill_multiplier=-30)
        self.assertAlmostEqual(1.085, result.weapon_rate, places=4)
        # `rate` 是给人看的两位小数；真值从间隔取倒数（1.085 次/秒）
        self.assertAlmostEqual(1.085, 1 / result.interval, places=3)
        self.assertAlmostEqual(1.08, result.rate, places=2)

    def test_攻速修正先取整到两位再除(self) -> None:
        """`round((1 + inc/100) × more, 2)` 在**分母**上。照直算不取整会差出一次攻击。"""
        rounded = spd.attack_rate(1.0, increased_pct=33.333)
        self.assertAlmostEqual(1.33, rounded.rate, places=2)

    def test_更多攻速是连乘(self) -> None:
        result = spd.attack_rate(1.0, increased_pct=50, more_mult=1.2)
        self.assertAlmostEqual(1.8, result.rate, places=2)

    def test_间隔与每秒次数互为倒数(self) -> None:
        result = spd.attack_rate(1.55, increased_pct=60)
        self.assertAlmostEqual(1.0, result.rate * result.interval, places=2)

    def test_没有武器就没有这一维(self) -> None:
        with self.assertRaises(ValueError):
            spd.attack_rate(0)

    def test_修正归零时报错而不是给无穷(self) -> None:
        with self.assertRaises(ValueError):
            spd.attack_rate(1.55, increased_pct=-100)

    def test_非引导技能受服务器tick与重复次数上限约束(self) -> None:
        capped = spd.attack_rate(10, increased_pct=900)
        self.assertTrue(capped.server_limited)
        self.assertAlmostEqual(30.3, capped.rate, places=1)
        repeated = spd.attack_rate(10, increased_pct=900, repeats=2)
        self.assertAlmostEqual(60.61, repeated.rate, places=2)
        channelled = spd.attack_rate(10, increased_pct=900, channelled=True)
        self.assertEqual(100, channelled.rate)


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = POB_ROOT_ENV / "src" / "Modules"
        cls.offence = (root / "CalcOffence.lua").read_text(encoding="utf-8")
        cls.config = (root / "ConfigOptions.lua").read_text(encoding="utf-8")

    def _lines(self, span: tuple[int, int], text: str) -> str:
        return "\n".join(text.splitlines()[span[0] - 1: span[1]])

    def test_精准命中率那两行还在(self) -> None:
        block = self._lines(acc.HIT_CHANCE_LINES, self.offence)
        self.assertIn('output.AccuracyHitChance = 100', block)
        self.assertIn(
            "output.AccuracyHitChance = calcs.hitChance(enemyEvasion, accuracyVsEnemy)", block
        )

    def test_敌方格挡那一行还在(self) -> None:
        line = self.offence.splitlines()[acc.BLOCK_LINE - 1]
        self.assertIn(
            "output.HitChance = output.AccuracyHitChance * (1 - output.enemyBlockChance / 100)",
            line,
        )

    def test_敌方闪避默认来自等级表(self) -> None:
        """「按等级查表」不是我们的选择，是上游配置页的默认值。"""
        line = self.config.splitlines()[acc.EVASION_DEFAULT_LINE - 1]
        self.assertIn("data.monsterEvasionTable[defaultLevel]", line)

    def test_攻速那一段还在(self) -> None:
        block = self._lines(spd.BASE_TIME_LINES, self.offence)
        self.assertIn(
            "source.AttackRate = source.AttackRate * (1 + skillData.attackSpeedMultiplier / 100)",
            block,
        )
        self.assertIn("baseTime = 1 / ( source.AttackRate or 1 )", block)

    def test_每秒次数那一行还在且取整在分母里(self) -> None:
        line = self.offence.splitlines()[spd.SPEED_LINE - 1]
        self.assertIn("output.Speed = 1 / (baseTime / round((1 + inc/100) * more, 2)", line)

    def test_非引导技能服务器tick上限仍在(self) -> None:
        line = self.offence.splitlines()[spd.SERVER_CAP_LINE - 1]
        self.assertIn("output.Speed = m_min(output.Speed, data.misc.ServerTickRate", line)


def tool_conn() -> sqlite3.Connection:
    """内存库：一把武器底材 + 一件非武器 + 一颗有攻速倍率的技能。"""
    import json

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO pob_base_item(base_id,name_en,name_zh,poedb_slug,category_key,"
        "poedb_page,item_class,subtype,socket_limit,tags,influence_tags,armour_tag,"
        "modifier_page,implicit_text,implicit_mod_types,implicit_ids,stats,stats_kind,"
        "requirements,source_file,source,source_priority,pob_url,poedb_url,source_commit,"
        "content_hash,updated_at) VALUES (?,?,?,?,'claw','Claw','Claw','',0,"
        "'[\"claw\"]','[]','','','','[]','[]',?,'weapon','{}','Data/Bases/claw.lua',"
        "'Path of Building',2,?,'https://poedb.tw/cn/x',?,?,datetime('now'))",
        [
            ("awl", "Awl", "凿钉", "awl",
             json.dumps({"AttackRateBase": 1.55, "CritChanceBase": 7.5}),
             f"{BLOB}/src/Data/Bases/claw.lua", "d" * 40, "h1"),
            ("plate", "Plate Vest", "板甲背心", "plate_vest",
             json.dumps({"Armour": 19}), f"{BLOB}/src/Data/Bases/armour.lua", "d" * 40, "h2"),
        ],
    )
    conn.execute(
        "INSERT INTO pob_skill_gem(gem_id,name_en,name_zh,name_zh_alt,poedb_slug,gem_kind,"
        "attr_color,tags,tags_zh,tag_string,skill_types,natural_max_level,is_vaal,"
        "is_transfigured,source_file,source,source_priority,pob_url,poedb_url,"
        "source_commit,content_hash,updated_at) VALUES "
        "('SiegeBallista','Siege Ballista','攻城炮台','[]','Siege_Ballista','active','dex',"
        "'[]','[]','','[]',20,0,0,'Data/Skills/act_dex.lua','Path of Building',2,?,"
        "'https://poedb.tw/cn/x',?,?,datetime('now'))",
        (f"{BLOB}/src/Data/Skills/act_dex.lua", "d" * 40, "h3"),
    )
    conn.executemany(
        "INSERT INTO pob_skill_level(gem_id,gem_level,level_requirement,cost,"
        "attack_speed_multiplier) VALUES ('SiegeBallista',?,?,'{}',?)",
        [(1, 12, -50.0), (20, 70, -50.0)],
    )
    conn.commit()
    return conn


class AccuracyToolTest(AccuracyTestCase):
    def setUp(self) -> None:
        super().setUp()
        from tools.impl.attack import accuracy_calc

        self.tool = accuracy_calc

    def test_没给数字时讲规则(self) -> None:
        result = self.tool()
        self.assertEqual("rules", result.data["kind"])
        text = result.citations[0]["text"]
        self.assertIn("5%..100%", text)
        # 千分位由 `calc.derive.fmt` 在 ≥10,000 时才加（第二十一轮定的口径），
        # 而出口的数值回填本来就把 `7,860` 与 `7860` 归一成同一个数（`binding._norm_num`）
        self.assertIn("7860", text)

    def test_没给敌方等级时说清缺什么而不是假定(self) -> None:
        result = self.tool(accuracy=1500)
        self.assertEqual(["敌方等级或敌方闪避值"], result.data["missing_inputs"])
        self.assertIn("替你假定", result.data["reason"])

    def test_给了命中值与等级就给推导(self) -> None:
        result = self.tool(accuracy=1500, enemy_level=83)
        self.assertEqual(83, result.data["accuracy_hit_chance"])
        text = result.citations[0]["text"]
        self.assertIn("83 级怪物的闪避", text)
        self.assertIn("7860", text)

    def test_反解给出最小达标命中值(self) -> None:
        result = self.tool(target_chance=90, enemy_level=83)
        self.assertEqual(1899.0, result.data["accuracy_needed"])
        self.assertGreaterEqual(result.data["achieved_chance"], 90)

    def test_两问一起答(self) -> None:
        """「我 1500 命中够不够 90%」——现状与目标都要给。"""
        result = self.tool(accuracy=1500, target_chance=90, enemy_level=83)
        self.assertEqual(83, result.data["accuracy_hit_chance"])
        self.assertEqual(1899.0, result.data["accuracy_needed"])

    def test_边界写明不给DPS也不算头目闪避(self) -> None:
        for kwargs in ({}, {"accuracy": 1500, "enemy_level": 83}):
            with self.subTest(kwargs=kwargs):
                boundary = self.tool(**kwargs).data["boundary"]
                self.assertIn("DPS", boundary)
                self.assertIn("头目", boundary)

    def test_引用带钉住commit的行号锚点(self) -> None:
        citation = self.tool(accuracy=1500, enemy_level=83).citations[0]
        self.assertIn("CalcOffence.lua#L", citation["url"])
        self.assertEqual("derived", citation["channel"])

    def test_等级越界给错误而不是外推(self) -> None:
        self.assertFalse(self.tool(accuracy=1500, enemy_level=150).ok)


class AttackSpeedToolTest(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.const = constant_conn()
        constants.load(self.const, force=True)
        self.conn = tool_conn()
        self.patch = patch("tools.impl.attack.dbmod.get_conn", return_value=self.conn)
        self.patch.start()
        from tools.impl.attack import attack_speed_calc

        self.tool = attack_speed_calc

    def tearDown(self) -> None:
        self.patch.stop()
        constants.reset_cache()
        self.conn.close()
        self.const.close()

    def test_没给武器时讲规则并说缺什么(self) -> None:
        result = self.tool()
        self.assertEqual("rules", result.data["kind"])
        self.assertIn("武器基础攻速（给 base_rate 或武器名）", result.data["missing_inputs"])

    def test_按武器名查基础攻速(self) -> None:
        result = self.tool(weapon="凿钉", increased_pct=60)
        self.assertEqual(1.55, result.data["base_rate"])
        self.assertAlmostEqual(2.48, result.data["rate"], places=2)
        self.assertIn("凿钉", result.citations[0]["text"])

    def test_非武器底材明确说没有攻速(self) -> None:
        result = self.tool(weapon="板甲背心")
        self.assertFalse(result.data["found"])
        self.assertIn("板甲背心", result.data["reason"])

    def test_按技能名查攻速倍率并写明等级(self) -> None:
        result = self.tool(weapon="凿钉", skill="攻城炮台")
        self.assertEqual(-50.0, result.data["skill_multiplier"])
        self.assertIn("按 20 级", result.citations[0]["text"])

    def test_查不到技能时按百分之百算并写出来(self) -> None:
        result = self.tool(weapon="凿钉", skill="不存在的技能")
        self.assertEqual(0.0, result.data["skill_multiplier"])
        self.assertIn("技能倍率按 100% 算", " ".join(result.data["sources"]))

    def test_边界写明不含双持与DPS(self) -> None:
        boundary = self.tool(weapon="凿钉").data["boundary"]
        self.assertIn("双持", boundary)
        self.assertIn("DPS", boundary)

    def test_服务器tick上限写进证据(self) -> None:
        result = self.tool(base_rate=10, increased_pct=900)
        self.assertTrue(result.data["server_limited"])
        self.assertIn("服务器 tick 上限", result.citations[0]["text"])


class RouteTest(unittest.TestCase):
    def route(self, question: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(question)

    def test_命中问法路由到命中计算器(self) -> None:
        for question in (
            "我 1500 命中打 83 级怪物命中率多少",
            "要多少命中才能 90% 命中率",
            "命中率怎么算",
            "我的命中值够不够",
            "会不会 miss",
        ):
            with self.subTest(question=question):
                self.assertEqual("accuracy_calc", self.route(question))

    def test_防守侧问法仍归受击计算器(self) -> None:
        """成对点：「命中率」这个词让给了攻击侧，防守侧的说法必须还在原处。
        实测判据：闪避率 / 躲开 / 被命中。"""
        for question in (
            "我有 10000 闪避，85 级怪物打我能躲开多少",
            "闪避率怎么算",
            "多少闪避才能躲开 85 级怪物",
            "护甲能减多少伤害",
        ):
            with self.subTest(question=question):
                self.assertEqual("mitigation_calc", self.route(question))

    def test_攻速问法路由到攻速计算器(self) -> None:
        for question in ("每秒攻击几次", "攻击间隔怎么算", "攻速 1.55 加 60% 每秒几下"):
            with self.subTest(question=question):
                self.assertEqual("attack_speed_calc", self.route(question))

    def test_枚举问句永远不是计算题(self) -> None:
        for stat in ("命中值", "命中率", "攻击速度"):
            with self.subTest(stat=stat):
                self.assertEqual("stat_sources", self.route(f"{stat}能从哪些渠道获得"))

    def test_档位问法不被抢走(self) -> None:
        self.assertEqual("mod_tiers", self.route("+300 命中值这条词缀有几档"))
        self.assertEqual("mod_tiers", self.route("攻击速度加快这条词缀有几档"))

    def test_其它计算器不被抢走(self) -> None:
        self.assertEqual("crit_calc", self.route("暴击率上限是多少"))
        self.assertEqual("max_hit_calc", self.route("一发最多能挨多少"))


if __name__ == "__main__":
    unittest.main()
