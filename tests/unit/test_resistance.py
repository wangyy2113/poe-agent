"""抗性计算器：与 PoB 对账、公式钉住上游、工具边界（2026-08-19 第二十轮）。

## 这一轮引入了一种新的验证方式：把上游**公式原文**钉住

前十八轮验证的都是「数据对不对」，靠的是与来源逐条对账、`check_doc_facts.py` 拦数字
漂移。计算器带来一类新的失效：**公式漂移**。常量值变了会被现有断言抓到，而
`final = max(min(total, max), min)` 的**顺序**、`m_modf` 的**截断**、
`resistancePenalty or -60` 的**默认档**这些都是代码里的行为，此前没有任何东西能拦。

所以这里有两组互补的用例：

1. `ReconcileWithPoB`：拿 **PoB 自己写下的期望值**对账
   （`spec/System/TestDefence_spec.lua:386-387` 断言 `LightningResistTotal == 90`、
   `LightningResistOverCap == 15`）。这是「我实现的公式与 PoB 一致」的直接证据。
2. `FormulaPinnedToUpstream`：直接读本地 PoB 检出，断言我建模的那几处**原文仍在**，
   并且引用里的行号锚点**确实指向那几行**。上游改了公式或挪了行，这里立刻红——
   引用点开是别的代码，比数字错更难发现。

第 2 组依赖本地检出，没有就跳过（其余用例全部离线，用内存 SQLite 打桩常量）。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import constants  # noqa: E402
from calc import resistance as res  # noqa: E402
from kb.sources.pob_misc import (  # noqa: E402
    MODULE_MISC_WHITELIST,
    extract_module_misc,
)

CONSTANT_SCHEMA = """
CREATE TABLE game_constant (
    key TEXT PRIMARY KEY, group_name TEXT NOT NULL, dat_source TEXT,
    dat_source_inherited INTEGER DEFAULT 0, kind TEXT NOT NULL,
    value_num REAL, value_json TEXT, source TEXT NOT NULL,
    source_priority INTEGER NOT NULL, url TEXT NOT NULL,
    pob_version TEXT, game_version TEXT, updated_at TEXT NOT NULL
);
"""

BLOB = "https://github.com/x/PathOfBuilding/blob/" + "c" * 40
POB_ROOT_ENV = Path(__file__).resolve().parents[3] / "PathOfBuilding"


def fixture_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(CONSTANT_SCHEMA)
    conn.executemany(
        "INSERT INTO game_constant VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        [
            (
                constants.MAX_ALL_RESISTANCES, "characterConstants",
                "Metadata/Characters/Character.ot", 0, "scalar", 75.0, None,
                "Path of Building", 2, f"{BLOB}/src/Data/Misc.lua#L62",
                "PoB 2.66.2 / tree 3.29", "3.29",
            ),
            (
                constants.MAX_RESIST_CAP, "pobMisc", None, 0, "scalar", 90.0, None,
                "Path of Building", 2, f"{BLOB}/src/Modules/Data.lua#L239",
                "PoB 2.66.2 / tree 3.29", "3.29",
            ),
            (
                constants.RESIST_FLOOR, "pobMisc", None, 0, "scalar", -200.0, None,
                "Path of Building", 2, f"{BLOB}/src/Modules/Data.lua#L238",
                "PoB 2.66.2 / tree 3.29", "3.29",
            ),
        ],
    )
    conn.commit()
    return conn


class ResistanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.conn = fixture_conn()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        constants.reset_cache()
        self.conn.close()


class ReconcileWithPoB(ResistanceTestCase):
    """与 PoB 自己写下的期望值对账。"""

    def test_对账pob用例的总抗与超抗(self) -> None:
        """`spec/System/TestDefence_spec.lua:386-387`：`+300 火抗` 经
        「火抗词缀以 50% 的值也作用于冰与电」转化给闪电 = `+150`，
        加上 PoB 默认的剧情抗性惩罚 `-60` → 总抗 90、超抗 15。

        这条用例值钱的地方是那个 `-60`：只看抗性那段代码会算出 150，
        而 PoB 断言的是 90——差额正是 `CalcSetup.lua:503` 建角色时就加的惩罚。
        """
        result = res.resistance("闪电", base_specific=150, campaign_penalty=-60)
        self.assertEqual((90, 15), (result.total, result.over_cap))
        self.assertEqual(75, result.final)

    def test_小数截断而不是四舍五入(self) -> None:
        """PoB 的注释就写着 `Fractional resistances are truncated`。
        四舍五入会让 75.9 变 76，超抗多算 1%。"""
        self.assertEqual(75, res.resistance("火焰", base_specific=75.9).total)
        self.assertEqual(-60, res.resistance("火焰", base_specific=-60.9).total)

    def test_夹在上下限之间(self) -> None:
        self.assertEqual(75, res.resistance("火焰", base_specific=120).final)
        self.assertEqual(-200, res.resistance("火焰", base_specific=-500).final)

    def test_最大抗性不超过硬上限(self) -> None:
        """基础 75 + 加成 30 = 105，但硬上限 90。"""
        capped = res.resistance("火焰", base_specific=200, extra_max_specific=30)
        self.assertEqual(90, capped.max_resist)
        self.assertEqual(90, capped.final)

    def test_覆写最大抗性可以突破硬上限(self) -> None:
        """`Melding of the Flesh` 那类 OVERRIDE 在 `m_min(MaxResistCap, ...)` 之前短路。"""
        result = res.resistance("火焰", base_specific=200, max_override=95)
        self.assertEqual(95, result.max_resist)

    def test_混沌不吃元素类加成(self) -> None:
        """把「+48% 全部元素抗性」「+4% 最大元素抗性」算到混沌上，是这一块最常见的错。"""
        chaos = res.resistance(
            "混沌", base_specific=40, base_elemental=48, extra_max_elemental=4
        )
        self.assertEqual((40, 75), (chaos.total, chaos.max_resist))
        fire = res.resistance(
            "火焰", base_specific=40, base_elemental=48, extra_max_elemental=4
        )
        self.assertEqual((88, 79), (fire.total, fire.max_resist))

    def test_混沌不吃元素加成也要写进推导(self) -> None:
        """静默丢掉那一项，用户会以为工具漏算了；必须显式写出「对混沌不生效」。"""
        chaos = res.resistance("混沌", base_specific=40, base_elemental=48)
        self.assertTrue(
            any("对混沌不生效" in step.label for step in chaos.steps),
            [step.label for step in chaos.steps],
        )

    def test_增加的抗性整体不小于零(self) -> None:
        """`m_max(calcLib.mod(...), 0)`：−150% 增加不会把正抗性翻成负的。"""
        self.assertEqual(0, res.resistance("火焰", base_specific=76, increased_pct=-150).total)

    def test_承受倍率与穿透的先后顺序(self) -> None:
        """超抗顶不住穿透——这是「先夹上限、后减穿透」的直接后果。

        总抗 120 与总抗 75 在有 25% 穿透时承受倍率**相同**（都是 0.5）：
        如果顺序反了，前者会算成 0.25，那是一条反向的结论。
        """
        overcapped = res.resistance("火焰", base_specific=120, enemy_penetration=25)
        exact = res.resistance("火焰", base_specific=75, enemy_penetration=25)
        self.assertAlmostEqual(0.5, overcapped.taken_mult)
        self.assertAlmostEqual(0.5, exact.taken_mult)
        self.assertEqual(45, overcapped.over_cap)

    def test_超抗顶得住减抗(self) -> None:
        """减抗进的是第 1 步（夹上限之前），所以 45% 超抗能吃下 −45% 的减抗。"""
        cursed = res.resistance("火焰", base_specific=120 - 45)
        self.assertEqual(75, cursed.final)

    def test_减抗单独一项而不是让调用方先减(self) -> None:
        """`base_specific=120, resist_penalty=-60` 与 `base_specific=60` 结果相同，
        但只有前者能让 120 与 −60 都出现在推导里。

        为什么这很重要（第二十轮实测）：模型自己先减，证据里就只剩 60，
        于是它写的「120% − 60% = 60%」被出口闸门以「120 未出现在证据中」拦掉——
        一句完全正确的话，因为工具没给它表达输入的地方。
        """
        split = res.resistance("火焰", base_specific=120, resist_penalty=-60)
        presummed = res.resistance("火焰", base_specific=60)
        self.assertEqual(presummed.final, split.final)
        exprs = " ".join(step.expr for step in split.steps)
        self.assertIn("120", exprs)
        self.assertIn("60", exprs)

    def test_距上限还差多少(self) -> None:
        self.assertEqual(59, res.resistance("混沌", base_specific=16).missing)
        self.assertEqual(135, res.resistance("混沌", base_specific=-60).missing)

    def test_认不出的抗性类型直接抛异常(self) -> None:
        """「物理抗性」在 POE1 不存在；猜成火抗就是一条编造的结论。"""
        with self.assertRaises(res.UnknownElement):
            res.resistance("物理")

    def test_常量缺失时抛异常而不是用默认值(self) -> None:
        constants.reset_cache()
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.executescript(CONSTANT_SCHEMA)
        constants.load(empty, force=True)
        with self.assertRaises(constants.MissingConstant):
            res.resistance("火焰", base_specific=75)
        empty.close()

    def test_引用推到同一个commit(self) -> None:
        """公式引用必须与常量入库那一刻的 commit 一致，否则点开是另一份代码。"""
        url = constants.provenance(constants.MAX_RESIST_CAP).url
        formula = res.pob_url(url, res.FORMULA_FILE, lines=res.FORMULA_LINES)
        self.assertTrue(formula.startswith(BLOB + "/src/Modules/CalcDefence.lua"))
        self.assertTrue(formula.endswith("#L578-L615"))

    def test_推不出引用时返回原地址而不编一个(self) -> None:
        self.assertEqual("不是一个 blob 地址",
                         res.pob_url("不是一个 blob 地址", res.FORMULA_FILE))


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    """把上游**公式原文**钉住：改了就红。

    钉的是行为而不是数值：常量漂移有别的断言拦，公式与默认档漂移此前没有任何东西拦。
    每条断言都同时检查「原文还在」与「我给的行号锚点确实指向它」。
    """

    @classmethod
    def setUpClass(cls) -> None:
        root = POB_ROOT_ENV / "src"
        cls.defence = (root / "Modules" / "CalcDefence.lua").read_text(encoding="utf-8")
        cls.setup = (root / "Modules" / "CalcSetup.lua").read_text(encoding="utf-8")
        cls.config = (root / "Modules" / "ConfigOptions.lua").read_text(encoding="utf-8")

    def _lines(self, text: str, span: tuple[int, int]) -> str:
        return "\n".join(text.splitlines()[span[0] - 1: span[1]])

    def test_抗性结算段落还在给出的行号范围内(self) -> None:
        block = self._lines(self.defence, res.FORMULA_LINES)
        self.assertIn("local final = m_max(m_min(total, max), min)", block)
        self.assertIn("Fractional resistances are truncated", block)
        self.assertIn("total = m_modf(total)", block)
        self.assertIn("m_min(data.misc.MaxResistCap", block)
        self.assertIn("data.misc.ResistFloor", block)
        self.assertIn('m_max(0, total - max)', block)

    def test_承受倍率那一行还在给出的行号上(self) -> None:
        line = self.defence.splitlines()[res.TAKEN_LINE - 1]
        self.assertIn("resMult = 1 - (resist - enemyPen) / 100", line)

    def test_剧情抗性惩罚默认档还是负六十(self) -> None:
        block = self._lines(self.setup, res.PENALTY_LINES)
        for element in ("Fire", "Cold", "Lightning", "Chaos"):
            self.assertIn(
                f'modDB:NewMod("{element}Resist", "BASE", '
                'env.configInput.resistancePenalty or -60, "Base")',
                block,
            )

    def test_三档惩罚与默认项还在(self) -> None:
        line = self.config.splitlines()[res.CONFIG_PENALTY_LINE - 1]
        self.assertIn('var = "resistancePenalty"', line)
        for value in res.CAMPAIGN_PENALTIES:
            self.assertIn(f"val={value}", line)
        self.assertIn("defaultIndex = 3", line)

    def test_只有三种抗性算元素抗性(self) -> None:
        self.assertIn(
            "local isElemental = { Fire = true, Cold = true, Lightning = true }",
            self.defence,
        )
        self.assertEqual(("Fire", "Cold", "Lightning"), res.ELEMENTAL)

    def test_白名单常量确实能从上游取到(self) -> None:
        values, problems = extract_module_misc(POB_ROOT_ENV)
        self.assertEqual([], problems)
        self.assertEqual(set(MODULE_MISC_WHITELIST), set(values))
        self.assertEqual(-200.0, values["ResistFloor"][0])
        self.assertEqual(90.0, values["MaxResistCap"][0])
        # 行号是引用锚点的来源，取不到就是引用没有锚点
        self.assertTrue(all(line for _, line in values.values()))


class ModuleMiscExtractTest(unittest.TestCase):
    """`Modules/Data.lua` 的白名单抽取：离线，Lua 在临时目录里构造。"""

    def _root(self, body: str) -> Path:
        root = Path(mkdtemp())
        target = root / "src" / "Modules" / "Data.lua"
        target.parent.mkdir(parents=True)
        target.write_text(body, encoding="utf-8")
        return root

    def _full_table(self, extra: str = "") -> Path:
        """把**当前白名单的每一项**都写进 Lua，行号从第 3 行开始。

        用白名单生成而不是写死两项：白名单会长（第二十一轮加了 4 个受击上限），
        写死的用例会在下一次扩容时假失败。
        """
        lines = [f"\t{key} = {i},"for i, key in enumerate(MODULE_MISC_WHITELIST, start=1)]
        body = "data = { }\ndata.misc = {\n" + "\n".join(lines) + f"\n{extra}}}\n"
        return self._root(body)

    def test_取白名单里的数值并给出行号(self) -> None:
        values, problems = extract_module_misc(self._full_table())
        self.assertEqual([], problems)
        self.assertEqual(set(MODULE_MISC_WHITELIST), set(values))
        # 第 i 项写的值是 i、行号是 i+2（前两行是 `data = { }` 与 `data.misc = {`）
        for i, key in enumerate(MODULE_MISC_WHITELIST, start=1):
            self.assertEqual((float(i), i + 2), values[key], key)

    def test_不在白名单里的一律不取(self) -> None:
        """那张表混着 PoB 建模取值、表达式与重复键，整表入库会抹掉 provenance 的差别。"""
        values, _ = extract_module_misc(
            self._full_table("\tServerTickTime = 0.033,\n\tEnergyShieldRechargeBase = 0.33,\n")
        )
        self.assertEqual(set(MODULE_MISC_WHITELIST), set(values))

    def test_白名单项变成非数值时报问题而不是静默跳过(self) -> None:
        """上游把它改成表达式的话，计算器会拿不到常量并炸掉——那要在导入期就看见。"""
        root = self._root(
            "data = { }\ndata.misc = {\n\tResistFloor = -200,\n"
            "\tMaxResistCap = data.foo / 100,\n}\n"
        )
        values, problems = extract_module_misc(root)
        self.assertNotIn("MaxResistCap", values)
        self.assertTrue(any("MaxResistCap" in p for p in problems), problems)

    def test_缺misc表时报问题(self) -> None:
        root = self._root("data = { }\ndata.other = { ResistFloor = -200 }\n")
        values, problems = extract_module_misc(root)
        self.assertEqual({}, values)
        self.assertTrue(problems)


class ResistanceToolTest(ResistanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        from tools.impl.defence import resistance_calc

        self.tool = resistance_calc

    def test_没给数值时只讲规则不编一个零抗(self) -> None:
        """输出一份「0% 抗性」的推导，等于把「你没告诉我」呈现成「你的抗性是 0」。"""
        result = self.tool()
        self.assertEqual("rules", result.data["kind"])
        text = result.citations[0]["text"]
        self.assertIn("最大抗性", text)
        self.assertIn("截断", text)

    def test_规则卡写明超抗防减抗不防穿透(self) -> None:
        text = self.tool().citations[0]["text"]
        self.assertIn("超抗", text)
        self.assertIn("穿透", text)

    def test_给了数值就出逐步推导(self) -> None:
        result = self.tool(element="火焰", base_specific=120)
        self.assertEqual(("result", 120, 75, 45), (
            result.data["kind"], result.data["total"],
            result.data["final"], result.data["over_cap"],
        ))
        text = result.citations[0]["text"]
        self.assertIn("| 步骤 | 算式 | 结果 |", text)
        self.assertIn("max(min(120%, 75%), -200%)", text)

    def test_减抗与抗性分开回显(self) -> None:
        text = self.tool(
            element="火焰", base_specific=120, resist_penalty=-60
        ).citations[0]["text"]
        self.assertIn("该抗性的「+X% 抗性」之和：120%", text)
        self.assertIn("减抗 / 诅咒等负值：-60%", text)
        self.assertIn("120% - 60%", text)

    def test_只回显给了的输入(self) -> None:
        """把一串 0 也列出来，读起来像在断言「你的最大抗性加成是 0」。"""
        text = self.tool(element="火焰", base_specific=120).citations[0]["text"]
        self.assertIn("该抗性的「+X% 抗性」之和：120%", text)
        self.assertNotIn("敌方穿透：0%", text)

    def test_推导里的每个数字都在证据里(self) -> None:
        """出口闸门要把答案里的数值回填到证据原文比对。结论数字不在证据里，
        用户看到的就是一句被拦掉的正确话。"""
        result = self.tool(element="火焰", base_specific=120, enemy_penetration=25)
        text = result.citations[0]["text"]
        for value in ("120", "75", "45", "-200", "0.50"):
            self.assertIn(value, text)

    def test_边界声明进data由代码附注(self) -> None:
        for kwargs in ({}, {"element": "火焰", "base_specific": 120}):
            with self.subTest(kwargs=kwargs):
                self.assertIn("图腾抗性", self.tool(**kwargs).data["boundary"])

    def test_认不出的抗性类型直接失败而不是猜(self) -> None:
        result = self.tool(element="物理", base_specific=30)
        self.assertFalse(result.ok)

    def test_证据带钉住commit的行号锚点(self) -> None:
        citation = self.tool(element="火焰", base_specific=75).citations[0]
        self.assertIn("#L578-L615", citation["url"])
        self.assertIn(f"#L{res.TAKEN_LINE}", citation["text"])
        self.assertEqual("derived", citation["channel"])

    def test_常量没入库时报错而不是给默认值(self) -> None:
        constants.reset_cache()
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        empty.executescript(CONSTANT_SCHEMA)
        constants.load(empty, force=True)
        self.assertFalse(self.tool(element="火焰", base_specific=75).ok)
        empty.close()


class ResistanceRouteTest(unittest.TestCase):
    """成对写：该抢的抢、不该抢的一条都不许抢。"""

    def route(self, question: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(question)

    def test_结算问法路由到计算器(self) -> None:
        for question in (
            "抗性上限是多少", "最大抗性能堆到多少", "超抗有什么用",
            "元素穿透和降低抗性有什么区别", "抗性怎么算的",
        ):
            with self.subTest(question=question):
                self.assertEqual("resistance_calc", self.route(question))

    def test_带数字的抗性问法也路由到计算器(self) -> None:
        """「火抗 120 是不是浪费了」这类问法没有固定说法，靠「抗性词 + 数字」判。"""
        for question in (
            "火抗 120 是不是浪费了", "地图 -60% 元素抗性之后我还剩多少火抗",
            "我火抗 75 冰抗 60，还差多少",
        ):
            with self.subTest(question=question):
                self.assertEqual("resistance_calc", self.route(question))

    def test_档位与来源问法不被抢走(self) -> None:
        """判据顺序就是判据的一部分：挪到意图词表前面会立刻抢走这两类。"""
        self.assertEqual("mod_tiers", self.route("+40% 火焰抗性这条词缀有几档"))
        self.assertEqual("stat_sources", self.route("火焰抗性能从哪些渠道获得"))

    def test_没有数字的泛抗性问法不抢(self) -> None:
        self.assertEqual("", self.route("哪些唯一装备能给全部元素抗性"))

    def test_其它防御维度不冒充抗性(self) -> None:
        """护甲/闪避还没有计算器，路由过去只会让工具答非所问。"""
        self.assertEqual("", self.route("护甲的减伤公式是什么"))


class ResistanceGateTest(unittest.TestCase):
    """抗性问法不得被维度闸门拦掉——常量域已入库，且现在有工具。"""

    def test_抗性结算问法不被判成缺口(self) -> None:
        from core import db as dbmod
        from core.config import get_config
        from kb.coverage import dimension_gap

        dbmod.init_db(get_config().db_path, vector_dim=0)
        conn = dbmod.get_conn()
        for question in ("抗性上限是多少", "超抗有什么用", "火抗 120 是不是浪费了"):
            with self.subTest(question=question):
                verdict = dimension_gap(conn, question)
                self.assertFalse(verdict is not None and verdict.is_gap)


if __name__ == "__main__":
    unittest.main()
