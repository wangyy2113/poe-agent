"""保留：取整规则、补上的定值列、名字歧义与路由（2026-08-20 第二十四轮）。

这一轮有两半：**先补数据，再算**。

数据那半的实测：`pob_skill_level` 先前只收 `manaReservationPercent`，
于是清晰 / 精准 / 活力这类**定值保留**的技能在库里看起来「不保留任何魔力」——
而 PoB 侧有 400 条 `manaReservationFlat`。把「保留 279 点魔力」答成「不保留」，
比查不到更糟。

计算那半的全部难点是**取整**，三条都成对写了用例：

1. 百分比各自 round 到 2 位 → 求和 → **只 ceil 一次**（不是每个光环各自 ceil）。
2. 保留效率是**除法**（提高 25% 让 50% 变 40%，不是 25%）。
3. `更多 <= 0` 或 `增加 <= -100` 时保留为 0（PoB 的显式分支）。
"""

from __future__ import annotations

import math
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import reservation as res  # noqa: E402
from core.db import SCHEMA  # noqa: E402
from tests.unit.test_resistance import POB_ROOT_ENV  # noqa: E402

POB_URL = (
    "https://github.com/x/PathOfBuilding/blob/" + "d" * 40 + "/src/Data/Skills/act_str.lua"
)


def fixture_conn() -> sqlite3.Connection:
    """内存库：几个技能，覆盖百分比 / 定值 / 无保留 / 同名前缀。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    gems = [
        ("Determination", "Determination", "坚定"),
        ("Grace", "Grace", "优雅"),
        ("Clarity", "Clarity", "清晰"),
        ("PurityOfFire", "Purity of Fire", "火焰净化"),
        ("PurityOfIce", "Purity of Ice", "冰霜净化"),
        ("Fireball", "Fireball", "火球"),
    ]
    conn.executemany(
        "INSERT INTO pob_skill_gem(gem_id,name_en,name_zh,name_zh_alt,poedb_slug,gem_kind,"
        "attr_color,tags,tags_zh,tag_string,skill_types,natural_max_level,is_vaal,"
        "is_transfigured,source_file,source,source_priority,pob_url,poedb_url,"
        "source_commit,content_hash,updated_at) "
        "VALUES (?,?,?,'[]',?,'active','str','[]','[]','','[]',20,0,0,"
        "'Data/Skills/act_str.lua','Path of Building',2,?,'https://poedb.tw/cn/x',"
        "?,?,datetime('now'))",
        [(gid, en, zh, gid.lower(), POB_URL, "d" * 40, gid) for gid, en, zh in gems],
    )
    conn.executemany(
        "INSERT INTO pob_skill_level(gem_id,gem_level,level_requirement,cost,"
        "reservation_percent,reservation_flat) VALUES (?,?,70,'{}',?,?)",
        [
            ("Determination", 20, 50.0, None),
            ("Determination", 21, 50.0, None),
            ("Grace", 20, 50.0, None),
            ("Clarity", 20, None, 279.0),
            ("PurityOfFire", 20, 35.0, None),
            ("PurityOfIce", 20, 35.0, None),
            ("Fireball", 20, None, None),
        ],
    )
    conn.commit()
    return conn


class RoundingTest(unittest.TestCase):
    """取整：这一维唯一真正会算错的地方。"""

    def test_百分比合计之后只向上取整一次(self) -> None:
        """每个光环各自 ceil 再相加会多算——1001 魔力 × 两个 25% 就差 1 点。

        一次 ceil：ceil(1001 × 50%) = 501；各自 ceil：ceil(250.25) × 2 = 502。
        """
        result = res.reserve(1001, [("甲", 25, 0), ("乙", 25, 0)])
        self.assertEqual(501, result.reserved)
        self.assertEqual(502, math.ceil(1001 * 0.25) * 2)

    def test_保留效率是除法不是减法(self) -> None:
        """效率提高 25%：50% → 40%（÷1.25），不是 25%。"""
        result = res.reserve(4000, [("坚定", 50, 0)], efficiency_pct=25)
        self.assertEqual(40.0, result.auras[0].percent)
        self.assertEqual(1600, result.reserved)

    def test_效率不会低于负一百(self) -> None:
        """PoB 对效率取 `m_max(..., -100)`；再低会让除数变成 0 或负数。"""
        result = res.reserve(4000, [("坚定", 50, 0)], efficiency_pct=-500)
        self.assertEqual(4000, result.reserved)

    def test_增加的保留按百分比乘上去(self) -> None:
        result = res.reserve(4000, [("坚定", 50, 0)], increased_pct=20)
        self.assertEqual(60.0, result.auras[0].percent)

    def test_更多小于等于零时保留为零(self) -> None:
        """PoB 的显式分支：`values.more > 0` 才算。"""
        result = res.reserve(4000, [("坚定", 50, 0)], more_mult=0)
        self.assertEqual(0.0, result.auras[0].percent)
        self.assertEqual(0, result.reserved)

    def test_减少一百以上时保留为零(self) -> None:
        result = res.reserve(4000, [("坚定", 50, 0)], increased_pct=-100)
        self.assertEqual(0, result.reserved)

    def test_百分比各自round到两位(self) -> None:
        """35% ÷ 1.07 = 32.710…% → 32.71%，两个合起来 65.42%，ceil(654.2) = 655。"""
        result = res.reserve(1000, [("甲", 35, 0), ("乙", 35, 0)], efficiency_pct=7)
        self.assertEqual(32.71, result.auras[0].percent)
        self.assertEqual(65.42, result.total_percent)
        self.assertEqual(655, result.reserved)

    def test_定值与百分比一起算(self) -> None:
        """清晰 279 点 + 憎恨 50%，1000 魔力 → 279 + 500 = 779。"""
        result = res.reserve(1000, [("清晰", 0, 279), ("憎恨", 50, 0)])
        self.assertEqual(779, result.reserved)
        self.assertEqual(221, result.unreserved)

    def test_定值取整到个位(self) -> None:
        """定值那一侧 PoB 用 `round(..., 0)`：279 ÷ 1.25 = 223.2 → 223。"""
        result = res.reserve(1000, [("清晰", 0, 279)], efficiency_pct=25)
        self.assertEqual(223.0, result.auras[0].flat)

    def test_保留超过池时夹住并标记开不起(self) -> None:
        result = res.reserve(1000, [("甲", 60, 0), ("乙", 60, 0)])
        self.assertEqual(1000, result.reserved)
        self.assertEqual(0, result.unreserved)
        self.assertTrue(result.over_reserved)

    def test_刚好用完不算开不起(self) -> None:
        result = res.reserve(4000, [("坚定", 50, 0), ("优雅", 50, 0)])
        self.assertEqual(4000, result.reserved)
        self.assertFalse(result.over_reserved)


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    """把上游的取整规则钉在行号上——它就是这一维的全部难点。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.perform = (POB_ROOT_ENV / "src" / "Modules" / "CalcPerform.lua").read_text(
            encoding="utf-8"
        )

    def _lines(self, span: tuple[int, int]) -> str:
        return "\n".join(self.perform.splitlines()[span[0] - 1: span[1]])

    def test_单技能的取整与效率写法还在(self) -> None:
        block = self._lines(res.RESERVE_LINES)
        self.assertIn(
            "values.reservedPercent = m_max(round(basePercentVal * (100 + values.inc) / 100 "
            "* values.more / (1 + values.efficiency / 100) / values.efficiencyMore, 2), 0)",
            block,
        )
        self.assertIn("values.more > 0 and values.inc > -100", block)
        self.assertIn(
            'values.efficiency = m_max(skillModList:Sum("INC", skillCfg, '
            'name.."ReservationEfficiency", "ReservationEfficiency"), -100)',
            block,
        )

    def test_合计只向上取整一次的写法还在(self) -> None:
        block = self._lines(res.TOTAL_LINES)
        self.assertIn(
            'reserved = (actor["reserved_"..pool.."Base"] or 0) + '
            'm_ceil(max * (actor["reserved_"..pool.."Percent"] or 0) / 100)',
            block,
        )
        self.assertIn('output[pool.."Unreserved"] = max - reserved', block)

    def test_定值那一侧取整到个位(self) -> None:
        self.assertIn("values.efficiencyMore, 0), 0)", self._lines(res.RESERVE_LINES))


class FlatReservationColumnTest(unittest.TestCase):
    """补上的那一列，以及**故意没有**的那两列。"""

    def setUp(self) -> None:
        self.conn = fixture_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_表里有定值保留列(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(pob_skill_level)")}
        self.assertIn("reservation_flat", cols)

    def test_没有生命保留列(self) -> None:
        """上游只有 3 个**未桥接**技能有 `lifeReservationPercent`
        （BoneArmour / TentacleSmash / ThirstForBlood），`sup_str.lua` 那两处是
        辅助宝石的 mod 声明。加一列当前管线永远填不上的空列，
        正是这张表注释开头警告的坑。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(pob_skill_level)")}
        self.assertNotIn("life_reservation_percent", cols)
        self.assertNotIn("life_reservation_flat", cols)

    def test_卡片把定值写成点数(self) -> None:
        """漏了这一行，清晰的卡片上会一个保留字样都没有。"""
        from kb.render_skills import level_line

        line = level_line({"gem_level": 20, "reservation_flat": 279.0})
        self.assertIn("保留 279 点魔力", line)


class ReservationToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fixture_conn()
        self.patch = patch("tools.impl.reservation.dbmod.get_conn", return_value=self.conn)
        self.patch.start()
        from tools.impl.reservation import reservation_calc

        self.tool = reservation_calc

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_按名字查库并写明用了哪一级(self) -> None:
        """「20 级坚定保留 50%」里的「20 级」是查出来的前提，不是假定的。"""
        result = self.tool(auras=["坚定", "优雅"], pool=4000)
        self.assertEqual(4000, result.data["reserved"])
        self.assertIn("坚定（Determination，20 级）", result.citations[0]["text"])

    def test_英文名也能查(self) -> None:
        self.assertEqual(2000, self.tool(auras=["Determination"], pool=4000).data["reserved"])

    def test_缺池时只给百分比并说清缺什么(self) -> None:
        result = self.tool(auras=["坚定", "优雅"])
        self.assertEqual("percent_only", result.data["kind"])
        self.assertEqual(100.0, result.data["total_percent"])
        self.assertIn("剩多少点算不出来", result.citations[0]["text"])

    def test_名字有歧义时列候选而不猜(self) -> None:
        """「净化」对应多个技能，挑一个去算是在编数字。"""
        result = self.tool(auras=["净化"], pool=4000)
        self.assertFalse(result.data["found"])
        self.assertIn("对应多个技能", result.citations[0]["text"])

    def test_没有保留数据的技能明确说出来(self) -> None:
        result = self.tool(auras=["火球"], pool=4000)
        self.assertFalse(result.data["found"])
        self.assertIn("没有保留数据", result.citations[0]["text"])

    def test_部分命中时算命中的并列出没命中的(self) -> None:
        result = self.tool(auras=["坚定", "火球"], pool=4000)
        self.assertEqual(2000, result.data["reserved"])
        self.assertTrue(any("火球" in n for n in result.data["missing_inputs"]))

    def test_库里没有那一级时退到最高级并写明(self) -> None:
        result = self.tool(auras=["清晰"], pool=1000, gem_level=99)
        self.assertIn("按 20 级算", result.citations[0]["text"])

    def test_开不起时明确说开不起(self) -> None:
        result = self.tool(auras=["坚定", "优雅", "清晰"], pool=1000)
        self.assertTrue(result.data["over_reserved"])
        self.assertIn("开不起", result.citations[0]["text"])

    def test_证据写明取整口径(self) -> None:
        text = self.tool(auras=["坚定"], pool=4000).citations[0]["text"]
        self.assertIn("只向上取整一次", text)
        self.assertIn("除法", text)

    def test_边界声明进data由代码附注(self) -> None:
        self.assertIn("血魔法", self.tool(auras=["坚定"], pool=4000).data["boundary"])

    def test_引用带钉住commit的行号锚点(self) -> None:
        citation = self.tool(auras=["坚定"], pool=4000).citations[0]
        self.assertIn("CalcPerform.lua#L", citation["url"])
        self.assertIn("d" * 40, citation["url"])
        self.assertEqual("derived", citation["channel"])

    def test_空技能列表直接失败(self) -> None:
        self.assertFalse(self.tool(auras=[]).ok)


class RouteFixtureMixin(unittest.TestCase):
    """路由与抽参数都要查库里的技能名，所以把 `get_conn` 打桩到内存库。

    单测不连 `data/poe.db`——那样用例会随真实数据变动而红。
    """

    def setUp(self) -> None:
        self.conn = fixture_conn()
        self.patch = patch("core.db.get_conn", return_value=self.conn)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()


class ReservationRouteTest(RouteFixtureMixin):
    """成对写：该抢的抢、不该抢的一条都不许抢。"""

    def route(self, question: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(question)

    def test_保留问法路由到保留计算器(self) -> None:
        for question in (
            "坚定和优雅一共保留多少魔力", "4000 魔力开得起憎恨吗",
            "我 4000 魔力，开坚定和优雅还剩多少",
        ):
            with self.subTest(question=question):
                self.assertEqual("reservation_calc", self.route(question))

    def test_宝石消耗问法不被抢走(self) -> None:
        """「20 级憎恨的魔力消耗」是 skill_gem_levels 的活：
        光环名 + 魔力还不够，必须有**保留这件事的信号**。"""
        self.assertNotEqual("reservation_calc", self.route("20 级憎恨的魔力消耗是多少"))

    def test_裸的开字不构成保留信号(self) -> None:
        self.assertEqual("", self.route("开图的时候憎恨还有用吗"))

    def test_抗性问法不被还剩多少抢走(self) -> None:
        """「还剩多少」是很多计算题的共同问法，所以**不能**当保留的意图词
        （实测它会把「地图 −60% 抗性之后我还剩多少火抗」抢走）。"""
        self.assertEqual("resistance_calc", self.route("地图 -60% 元素抗性之后我还剩多少火抗"))

    def test_枚举问句仍归stat_sources(self) -> None:
        self.assertEqual("stat_sources", self.route("保留效率能从哪些渠道获得"))

    def test_没有技能名的保留问法不抢(self) -> None:
        """「哪些词缀能减少保留」没有技能名，工具拿不到 auras 只能空手而归。"""
        self.assertEqual("", self.route("哪些词缀能减少保留"))


class ReservationArgsTest(RouteFixtureMixin):
    """代码抽参数：技能名必须真的在库里（与 entity_lookup 同一条纪律）。"""

    def args(self, question: str):  # noqa: ANN201
        from orchestrator.loop import _reservation_args

        return _reservation_args(question)

    def test_抽出技能名与池(self) -> None:
        self.assertEqual(
            {"auras": ["坚定", "优雅"], "pool": 4000.0},
            self.args("我 4000 魔力，开坚定和优雅还剩多少"),
        )

    def test_英文名大小写不敏感(self) -> None:
        args = self.args("开 determination 和 grace 之后 4000 魔力还剩多少")
        self.assertEqual(["Determination", "Grace"], args["auras"])

    def test_没有保留信号就不算保留题(self) -> None:
        self.assertIsNone(self.args("20 级憎恨的魔力消耗是多少"))

    def test_认不出技能名时返回None(self) -> None:
        self.assertIsNone(self.args("哪些词缀能减少保留"))

    def test_池抽不到唯一值时只给技能名(self) -> None:
        args = self.args("4000 魔力和 5000 魔力分别开坚定还剩多少")
        self.assertNotIn("pool", args)
        self.assertEqual(["坚定"], args["auras"])


if __name__ == "__main__":
    unittest.main()
