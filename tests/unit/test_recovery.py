"""回复：重复键的自洽核对、公式钉住上游、两层上限与工具边界（2026-08-20 第二十三轮）。

这一维没有 PoB 自己写下的期望值可对账（`spec/` 里没有回充/偷取的断言），
所以换了一种同样有力的验证：**内部自洽核对**。

`Modules/Data.lua` 里 `EnergyShieldRechargeBase` 写了两次——先是
`characterConstants["energy_shield_recharge_rate_per_minute_%"] / 60 / 100`（表达式），
紧接着一行字面量 `0.33`。Lua 后者覆盖前者，解析器同样取后者。
`test_重复键的两种写法相差正好百分之一` 拿库里那条 characterConstants 反算，
结果是字面量比常量算出来的**低正好 1%**——所以 PoB 显示的回充速度也低 1%。
我们复算的是 PoB，所以照它取 0.33，但这条差异写进了证据，
而不是让用户以为 33% 就是游戏的精确值。**这件事要被证明而不是被相信。**

同一类的还有 `MaxEnergyShieldLeechRate`：`CalcSetup.lua:51` 硬编码 `10`，
而常量表里是 `600 %/分钟`（÷60 = 10）。两处一致，用例把它钉住。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import constants  # noqa: E402
from calc import recovery as rec  # noqa: E402
from tests.unit.test_resistance import (  # noqa: E402
    BLOB,
    CONSTANT_SCHEMA,
    POB_ROOT_ENV,
)

#: 回复用到的常量。值与真实库一致，但用内存库打桩——单测不连 data/poe.db。
FIXTURE_ROWS = (
    (constants.ES_RECHARGE_BASE, "pobMisc", None, 0.33),
    (constants.ES_RECHARGE_DELAY, "pobMisc", None, 2.0),
    (constants.LEECH_RATE_BASE, "pobMisc", None, 0.02),
    (constants.MAX_LIFE_LEECH_RATE, "characterConstants",
     "Metadata/Characters/Character.ot", 1200.0),
    (constants.MAX_MANA_LEECH_RATE, "characterConstants",
     "Metadata/Characters/Character.ot", 1200.0),
    (constants.MAX_ES_LEECH_RATE, "characterConstants",
     "Metadata/Characters/Character.ot", 600.0),
    (constants.MAX_LIFE_LEECH_INSTANCE, "characterConstants",
     "Metadata/Characters/Character.ot", 10.0),
    (constants.MAX_MANA_LEECH_INSTANCE, "characterConstants",
     "Metadata/Characters/Character.ot", 10.0),
    (constants.MAX_ES_LEECH_INSTANCE, "characterConstants",
     "Metadata/Characters/Character.ot", 10.0),
    (constants.MANA_REGEN_RATE_PER_MINUTE, "characterConstants",
     "Metadata/Characters/Character.ot", 105.0),
    # 回充基数的「另一种写法」：表达式那一侧引用的就是它
    ("characterConstants.energy_shield_recharge_rate_per_minute_%", "characterConstants",
     "Metadata/Characters/Character.ot", 2000.0),
)


def fixture_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(CONSTANT_SCHEMA)
    conn.executemany(
        "INSERT INTO game_constant VALUES (?,?,?,0,'scalar',?,NULL,'Path of Building',2,?,"
        "'PoB 2.66.2 / tree 3.29','3.29',datetime('now'))",
        [
            (key, group, dat, value, f"{BLOB}/src/Modules/Data.lua#L249")
            for key, group, dat, value in FIXTURE_ROWS
        ],
    )
    conn.commit()
    return conn


class RecoveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.conn = fixture_conn()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        constants.reset_cache()
        self.conn.close()


class ConstantsAgreeTest(RecoveryTestCase):
    """重复键与硬编码值的自洽核对。"""

    def test_重复键的两种写法相差正好百分之一(self) -> None:
        """字面量 `0.33` 比 `2000 / 60 / 100 = 0.3333…` **低正好 1%**。

        这条用例的意义不在数值，而在把差异钉住：那张表里同一个键写了两次，
        Lua 取后写的字面量，所以 **PoB 显示的回充速度比 GGG 常量算出来的低约 1%**
        （5000 护盾：1650/秒 vs 1666.7/秒）。我们复算的是 PoB，所以用 0.33；
        但这条差异要在证据里说出来，而不是让用户以为 33% 就是游戏的精确值。
        """
        literal = constants.get(constants.ES_RECHARGE_BASE)
        derived = constants.get(
            "characterConstants.energy_shield_recharge_rate_per_minute_%"
        ) / 60 / 100
        self.assertAlmostEqual(0.99, literal / derived, places=6)

    def test_护盾偷取速率上限两处一致(self) -> None:
        """`CalcSetup.lua:51` 硬编码 10，常量表是 600%/分钟 ÷ 60 = 10。"""
        self.assertEqual(10.0, constants.get(constants.MAX_ES_LEECH_RATE) / 60)

    def test_三种池的上限不是同一个数(self) -> None:
        """生命/魔力每秒 20%，护盾每秒 10%。合并成一个数就是错的。"""
        per_second = {
            pool: constants.get(key) / 60
            for pool, key in (
                ("生命", constants.MAX_LIFE_LEECH_RATE),
                ("魔力", constants.MAX_MANA_LEECH_RATE),
                ("能量护盾", constants.MAX_ES_LEECH_RATE),
            )
        }
        self.assertEqual({"生命": 20.0, "魔力": 20.0, "能量护盾": 10.0}, per_second)


class RechargeTest(RecoveryTestCase):
    def test_每秒回充是最大护盾的三分之一(self) -> None:
        result = rec.energy_shield_recharge(5000)
        self.assertEqual(1650, result.per_second)

    def test_回满耗时与护盾大小无关(self) -> None:
        """每秒回复的是**百分比**，所以 1000 与 10000 护盾回满耗时相同。"""
        small = rec.energy_shield_recharge(1000)
        large = rec.energy_shield_recharge(10000)
        self.assertAlmostEqual(small.seconds_to_full, large.seconds_to_full, places=2)
        self.assertAlmostEqual(3.03, small.seconds_to_full, places=2)

    def test_延迟要算进总时长(self) -> None:
        """只说「每秒回 33%」会让人以为受伤后立刻开始回。"""
        result = rec.energy_shield_recharge(5000)
        self.assertEqual(2.0, result.delay_seconds)
        self.assertAlmostEqual(result.total_seconds, 2.0 + result.seconds_to_full, places=4)

    def test_更快开始回充缩短延迟(self) -> None:
        result = rec.energy_shield_recharge(5000, faster_start_pct=100)
        self.assertEqual(1.0, result.delay_seconds)

    def test_增加与更多分别乘上去(self) -> None:
        result = rec.energy_shield_recharge(
            5000, increased_recharge_pct=50, more_recharge_mult=1.2
        )
        self.assertEqual(round(5000 * 0.33 * 1.5 * 1.2), result.per_second)

    def test_常量缺失时抛异常(self) -> None:
        constants.reset_cache()
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.executescript(CONSTANT_SCHEMA)
        constants.load(empty, force=True)
        with self.assertRaises(constants.MissingConstant):
            rec.energy_shield_recharge(5000)
        empty.close()


class LeechTest(RecoveryTestCase):
    def test_一个实例每秒回复池的百分之二(self) -> None:
        result = rec.leech(4000, pool_name="生命")
        self.assertEqual(80.0, result.instance_rate)

    def test_速率上限按池的百分比(self) -> None:
        """生命每秒 20% → 4000 生命的速率上限是 800。"""
        result = rec.leech(4000, pool_name="生命", instances=20)
        self.assertEqual(800.0, result.rate_cap)
        self.assertEqual(800.0, result.total_rate)
        self.assertTrue(result.rate_capped)

    def test_护盾的速率上限只有一半(self) -> None:
        life = rec.leech(4000, pool_name="生命", instances=20).rate_cap
        shield = rec.leech(4000, pool_name="能量护盾", instances=20).rate_cap
        self.assertEqual(life / 2, shield)

    def test_单次实例上限限量不限速(self) -> None:
        """一击偷到 5000，但单次上限是池的 10% = 400；速率仍是 2%/秒。"""
        result = rec.leech(4000, pool_name="生命", leech_pct=100, hit_damage=5000)
        self.assertEqual(400.0, result.instance_cap)
        self.assertEqual(400.0, result.effective_amount)
        self.assertTrue(result.amount_capped)
        self.assertEqual(80.0, result.instance_rate)

    def test_实例持续时间等于量除以每秒回复(self) -> None:
        """60 的量、每秒 80 → 0.75 秒。"""
        result = rec.leech(4000, pool_name="生命", leech_pct=2, hit_damage=3000)
        self.assertAlmostEqual(0.75, result.duration, places=4)

    def test_偷取速度加成同时抬高实例速率(self) -> None:
        result = rec.leech(4000, pool_name="生命", increased_leech_rate_pct=50)
        self.assertEqual(120.0, result.instance_rate)

    def test_认不出的池直接抛异常(self) -> None:
        """三种池的上限不同，猜错就是错数字。"""
        with self.assertRaises(rec.UnknownPool):
            rec.leech(4000, pool_name="怒气")


class ManaRegenTest(RecoveryTestCase):
    def test_基数是每分钟而不是每秒(self) -> None:
        """105%/分钟 = 每秒 1.75%。拿 105 当每秒用会差 60 倍。"""
        result = rec.mana_regen(1000)
        self.assertAlmostEqual(17.5, result.per_second, places=4)

    def test_定值再生先加再乘增幅(self) -> None:
        result = rec.mana_regen(1000, flat_regen=10, increased_regen_pct=100)
        self.assertAlmostEqual((17.5 + 10) * 2, result.per_second, places=4)


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    """把上游公式原文钉在行号上。"""

    @classmethod
    def setUpClass(cls) -> None:
        root = POB_ROOT_ENV / "src"
        cls.defence = (root / "Modules" / "CalcDefence.lua").read_text(encoding="utf-8")
        cls.offence = (root / "Modules" / "CalcOffence.lua").read_text(encoding="utf-8")
        cls.setup = (root / "Modules" / "CalcSetup.lua").read_text(encoding="utf-8")
        cls.data = (root / "Modules" / "Data.lua").read_text(encoding="utf-8")

    def _lines(self, text: str, span: tuple[int, int]) -> str:
        return "\n".join(text.splitlines()[span[0] - 1: span[1]])

    def test_回充公式还在给出的行号范围内(self) -> None:
        block = self._lines(self.defence, rec.RECHARGE_LINES)
        self.assertIn("data.misc.EnergyShieldRechargeBase", block)
        self.assertIn("output.EnergyShield * base * (1 + inc/100) * more", block)

    def test_偷取速率与实例上限还在给出的行号范围内(self) -> None:
        rate = self._lines(self.offence, rec.LEECH_RATE_LINES)
        self.assertIn("output.Life * data.misc.LeechRateBase", rate)
        self.assertIn("m_min(output.LifeLeechRate, output.MaxLifeLeechRate)", rate)
        duration = self._lines(self.offence, rec.LEECH_DURATION_LINES)
        self.assertIn("amount / total / data.misc.LeechRateBase", duration)

    def test_两层上限都由常量表播种(self) -> None:
        block = self._lines(self.setup, rec.LEECH_CAP_LINES)
        self.assertIn(
            'modDB:NewMod("MaxLifeLeechRate", "BASE", '
            'data.characterConstants["maximum_life_leech_rate_%_per_minute"] / 60, "Base")',
            block,
        )
        self.assertIn('modDB:NewMod("MaxEnergyShieldLeechRate", "BASE", 10, "Base")', block)
        self.assertIn("MaxLifeLeechInstance", block)

    def test_魔力再生基数还在给出的行号上(self) -> None:
        line = self.setup.splitlines()[rec.MANA_REGEN_LINE - 1]
        self.assertIn('modDB:NewMod("ManaRegen", "BASE", env.data.misc.ManaRegenBase', line)

    def test_回充基数在上游确实写了两次且后者是字面量(self) -> None:
        """白名单里唯一一个重复键。行号锚点必须指向**后写的那一行**。"""
        lines = [
            i + 1 for i, line in enumerate(self.data.splitlines())
            if line.strip().startswith("EnergyShieldRechargeBase =")
        ]
        self.assertEqual(2, len(lines), f"上游写法变了：{lines}")
        first, second = lines
        self.assertIn("characterConstants", self.data.splitlines()[first - 1])
        self.assertIn("0.33", self.data.splitlines()[second - 1])

    def test_行号锚点取的是最后一次赋值(self) -> None:
        from kb.sources.pob_misc import extract_module_misc

        values, problems = extract_module_misc(POB_ROOT_ENV)
        self.assertEqual([], problems)
        _, line = values["EnergyShieldRechargeBase"]
        self.assertIn("0.33", self.data.splitlines()[line - 1])


class RecoveryToolTest(RecoveryTestCase):
    def setUp(self) -> None:
        super().setUp()
        from tools.impl.recovery import recovery_calc

        self.tool = recovery_calc

    def test_没给池大小时讲规则(self) -> None:
        result = self.tool()
        self.assertEqual("rules", result.data["kind"])
        text = result.citations[0]["text"]
        self.assertIn("2 秒", text)      # 延迟
        self.assertIn("| 生命 | 20% | 10% |", text)  # 两层上限
        # 与 GGG 常量的 1% 差异要说出来，不能让 33% 看起来像精确值
        self.assertIn("33.33", text)

    def test_回充给逐步推导与总时长(self) -> None:
        result = self.tool(energy_shield=5000)
        self.assertEqual(1650, result.data["recharge"]["per_second"])
        text = result.citations[0]["text"]
        self.assertIn("| 步骤 | 算式 | 结果 |", text)
        self.assertIn("回满耗时与护盾大小无关", text)

    def test_只问偷取但没给池时说清缺什么(self) -> None:
        """与 `mitigation_calc` 的护甲同一条纪律：没有池就没有速率。"""
        result = self.tool(leech_pct=2, hit_damage=3000)
        self.assertEqual("rules", result.data["kind"])
        self.assertIn("没给池大小", result.citations[0]["text"])

    def test_偷取给两层上限(self) -> None:
        result = self.tool(pool=4000, pool_name="生命", leech_pct=2, hit_damage=3000)
        leech = result.data["leech"]
        self.assertEqual((80.0, 800.0, 400.0), (leech["instance_rate"], leech["rate_cap"],
                                                leech["instance_cap"]))

    def test_速率封顶时写明再叠不会更快(self) -> None:
        result = self.tool(pool=4000, pool_name="生命", instances=20)
        self.assertIn("速率已封顶", result.citations[0]["text"])

    def test_三段一起给时逐段列出(self) -> None:
        result = self.tool(energy_shield=5000, pool=4000, leech_pct=2, hit_damage=3000,
                           mana=1000)
        text = result.citations[0]["text"]
        for section in ("### 能量护盾回充", "### 生命偷取", "### 魔力再生"):
            self.assertIn(section, text)

    def test_认不出的池直接失败(self) -> None:
        self.assertFalse(self.tool(pool=4000, pool_name="怒气").ok)

    def test_边界声明进data由代码附注(self) -> None:
        for kwargs in ({}, {"energy_shield": 5000}):
            with self.subTest(kwargs=kwargs):
                self.assertIn("Mind over Matter", self.tool(**kwargs).data["boundary"])

    def test_证据带钉住commit的行号锚点且未静默截断(self) -> None:
        from tools.impl.recovery import MAX_EVIDENCE_CHARS

        citation = self.tool(energy_shield=5000, pool=4000, leech_pct=2, hit_damage=3000,
                             mana=1000, instances=3).citations[0]
        self.assertIn("CalcDefence.lua#L", citation["url"])
        self.assertEqual("derived", citation["channel"])
        self.assertLessEqual(len(citation["text"]), MAX_EVIDENCE_CHARS)
        self.assertNotIn("已截断", citation["text"])
        self.assertIn("来源：", citation["text"])

    def test_常量没入库时报错(self) -> None:
        constants.reset_cache()
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.executescript(CONSTANT_SCHEMA)
        constants.load(empty, force=True)
        self.assertFalse(self.tool(energy_shield=5000).ok)
        empty.close()


class RecoveryRouteTest(unittest.TestCase):
    """成对写：该抢的抢、不该抢的一条都不许抢。"""

    def route(self, question: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(question)

    def test_回复问法路由到回复计算器(self) -> None:
        for question in (
            "5000 护盾多久回满", "偷取上限是多少", "偷取速度上限", "魔力再生怎么算",
            "护盾回充要等几秒",
        ):
            with self.subTest(question=question):
                self.assertEqual("recovery_calc", self.route(question))

    def test_枚举问句仍归stat_sources(self) -> None:
        for stat in ("生命偷取", "护盾回充", "魔力再生"):
            with self.subTest(stat=stat):
                self.assertEqual("stat_sources", self.route(f"{stat}能从哪些渠道获得"))

    def test_另外两个计算器不被抢走(self) -> None:
        self.assertEqual("resistance_calc", self.route("抗性上限是多少"))
        self.assertEqual("mitigation_calc", self.route("格挡上限是多少"))

    def test_底材属性问法不抢(self) -> None:
        """裸的「护盾」是底材属性名，「哪些底材护盾最高」有卡片可答。"""
        self.assertEqual("", self.route("哪些底材护盾最高"))

    def test_档位问法不被抢走(self) -> None:
        self.assertEqual("mod_tiers", self.route("+50 最大能量护盾这条词缀有几档"))


class RecoveryGateTest(unittest.TestCase):
    def test_回复问法不被判成缺口(self) -> None:
        from core import db as dbmod
        from core.config import get_config
        from kb.coverage import dimension_gap

        dbmod.init_db(get_config().db_path, vector_dim=0)
        conn = dbmod.get_conn()
        for question in ("护盾多久回满", "偷取上限是多少", "魔力再生怎么算"):
            with self.subTest(question=question):
                verdict = dimension_gap(conn, question)
                self.assertFalse(verdict is not None and verdict.is_gap)


if __name__ == "__main__":
    unittest.main()
