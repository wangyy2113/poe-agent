"""宝石的每级效果文本与属性需求（POEDB 详情页逐级表，第三十二轮）。

全部离线：HTML 在内存里构造，不读 `data/http_cache`、不连网。

这一维**入库前逐级对账**，所以用例分三组：

1. 解析：结构列走白名单、效果文本按列序、属性需求缺就是缺（不补 0）；
2. 对账：需求等级 / 消耗 / 基础伤害三项对不上就拒绝这一颗（实测 `Convocation`
   撞上了——PoB 发的是重做后的 `ConvocationNew`，POEDB 那一页还是旧版）；
3. 出口：卡片与工具都要真的把它写出来（「入库但没有出口」是这个项目踩过三次的坑）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from kb import render_skills  # noqa: E402
from kb.sources.poedb_gem_page import (  # noqa: E402
    GemPageError,
    damage_column,
    parse_gem_page,
    reconcile,
)

# ── 构造详情页 ─────────────────────────────────────────────────


def level_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def gem_detail(table_html: str, *, before: str = "") -> str:
    """真实页面上逐级表前面还有别的表（章节奖励、固定属性），所以默认塞一张进去——
    抽取器必须靠**表头**定位而不是下标。"""
    other = before or "<table><thead><tr><th>章节</th><th>使命</th></tr></thead>" \
                      "<tbody><tr><td>1</td><td>某任务</td></tr></tbody></table>"
    return f"<html><body>{other}{table_html}</body></html>"


ICE_NOVA_TABLE = level_table(
    ["等级", "需求等级", "智慧", "魔力", "基本伤害", "造成 25 - 38 冰霜伤害",
     "基础范围为 2.6 米", "经验值"],
    [
        ["1", "12", "33", "8", "100%, 100%, 100%", "25, 38", "2.6", "118,383"],
        ["20", "70", "155", "23", "100%, 100%, 100%", "1122, 1683", "3", "2,000,000"],
        ["21", "72", "159", "24", "100%, 100%, 100%", "1240, 1860", "3", ""],
    ],
)


def pob_levels(**overrides) -> dict[int, dict]:
    base = {
        1: {"level_requirement": 12, "cost": {"Mana": 8.0}, "base_damage": (25.0, 38.0)},
        20: {"level_requirement": 70, "cost": {"Mana": 23.0}, "base_damage": (1122.0, 1683.0)},
        21: {"level_requirement": 72, "cost": {"Mana": 24.0}, "base_damage": (1240.0, 1860.0)},
    }
    base.update(overrides)
    return base


class ParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.page = parse_gem_page(gem_detail(ICE_NOVA_TABLE), poedb_slug="Ice_Nova")
        assert self.page is not None

    def test_效果文本按列序取出(self) -> None:
        """顺序有意义：技能自己的效果在前。乱序会让人以为是两颗宝石。"""
        self.assertEqual(
            [(0, "造成 25 - 38 冰霜伤害"), (1, "基础范围为 2.6 米")],
            [(line.index, line.text_zh) for line in self.page.stat_lines],
        )

    def test_列头原文不改写(self) -> None:
        """列头里本来就带着 1 级的数值，这正是要给用户看的那句话。
        规整成 `#` 占位等于自己重写上游文本。"""
        self.assertIn("25 - 38", self.page.stat_lines[0].text_zh)

    def test_每级取值按列存(self) -> None:
        row = next(r for r in self.page.levels if r.gem_level == 20)
        self.assertEqual({0: "1122, 1683", 1: "3"}, row.stat_values)

    def test_属性需求逐级取出(self) -> None:
        self.assertEqual({"int": 33}, self.page.levels[0].requirements)
        self.assertEqual({"int": 155},
                         next(r for r in self.page.levels if r.gem_level == 20).requirements)

    def test_页面没给的属性需求是缺而不是零(self) -> None:
        """白色宝石三项都是 0，而超过自然最高级的等级页面根本没这一列——
        补 0 会让「不需要智力」和「没有数据」变成同一句话。"""
        table = level_table(
            ["等级", "需求等级", "基本伤害", "经验值"],
            [["1", "10", "100%, 100%, 100%", ""]],
        )
        page = parse_gem_page(gem_detail(table), poedb_slug="Portal")
        assert page is not None
        self.assertEqual({}, page.levels[0].requirements)
        self.assertIsNone(page.levels[0].req_int)

    def test_零也要如实记下来(self) -> None:
        """白色宝石页面上写的是 0，那就是 0——这与「没有这一列」不同。"""
        table = level_table(
            ["等级", "需求等级", "力量", "智慧", "敏捷", "经验值"],
            [["1", "10", "0", "0", "0", ""]],
        )
        page = parse_gem_page(gem_detail(table), poedb_slug="Portal")
        assert page is not None
        self.assertEqual({"str": 0, "dex": 0, "int": 0}, page.levels[0].requirements)

    def test_按表头定位而不是按下标取表(self) -> None:
        """详情页上逐级表前面还有别的表，下标会随上游加表而错位。"""
        self.assertEqual(3, len(self.page.levels))

    def test_没有逐级表返回None而不是报错(self) -> None:
        """那是数据缺口（POEDB 那一页只有说明），由调用方登记，不是结构错误。"""
        self.assertIsNone(parse_gem_page("<html><body><p>没有表</p></body></html>",
                                         poedb_slug="X"))

    def test_白名单之外的短列头直接报错(self) -> None:
        """上游哪天加一列 `生命`（血魔法消耗），当成效果文本会把消耗写进「效果」，
        当成消耗列又会让一条效果文本消失——两个方向都是静默出错，所以报错。"""
        table = level_table(
            ["等级", "需求等级", "怒火", "经验值"], [["1", "10", "5", ""]],
        )
        with self.assertRaises(GemPageError) as caught:
            parse_gem_page(gem_detail(table), poedb_slug="X")
        self.assertIn("怒火", str(caught.exception))

    def test_带占位符的短列头仍是效果文本(self) -> None:
        """「范围 # 米」实测是这一族里唯一的短列头，靠 `#` 与结构列区分。"""
        table = level_table(
            ["等级", "需求等级", "范围 # 米", "经验值"], [["1", "10", "1.5", ""]],
        )
        page = parse_gem_page(gem_detail(table), poedb_slug="X")
        assert page is not None
        self.assertEqual(["范围 # 米"], [line.text_zh for line in page.stat_lines])

    def test_没有列头就报错(self) -> None:
        table = level_table(["等级", "需求等级", ""], [["1", "10", "x"]])
        with self.assertRaises(GemPageError):
            parse_gem_page(gem_detail(table), poedb_slug="X")


class ReconcileTest(unittest.TestCase):
    """入库前的门槛：两侧逐级对账，对不上就不入这一颗。"""

    def _page(self, table: str = ICE_NOVA_TABLE):  # noqa: ANN202
        page = parse_gem_page(gem_detail(table), poedb_slug="Ice_Nova")
        assert page is not None
        return page

    def test_三项都对得上时没有分歧(self) -> None:
        self.assertEqual([], reconcile(self._page(), pob_levels()))

    def test_需求等级对不上就报出来(self) -> None:
        """实测原型：`Convocation`。PoB 发的是重做后的 `ConvocationNew`
        （各级需求等级恒为 31），POEDB 那一页还是旧版的 24/27/30…
        两边讲的不是同一颗宝石，那就不能把旧版的效果文本挂上去。"""
        broken = pob_levels()
        broken[1] = {"level_requirement": 31, "cost": {"Mana": 8.0},
                     "base_damage": (25.0, 38.0)}
        mismatches = reconcile(self._page(), broken)
        self.assertEqual(1, len(mismatches))
        self.assertIn("需求等级", mismatches[0].render())
        self.assertIn("POEDB 12", mismatches[0].render())
        self.assertIn("PoB 31", mismatches[0].render())

    def test_消耗对不上也报(self) -> None:
        levels = pob_levels()
        levels[20]["cost"] = {"Mana": 40.0}
        self.assertEqual(["Mana 消耗"], [m.field for m in reconcile(self._page(), levels)])

    def test_基础伤害逐级对账(self) -> None:
        """第二十九轮是抽样 20 颗手抠区间文本，这里是同一份数据的逐级表——
        抽样对账因此变成全量对账。"""
        levels = pob_levels()
        levels[20]["base_damage"] = (999.0, 1683.0)
        self.assertEqual(["基础伤害"], [m.field for m in reconcile(self._page(), levels)])

    def test_缺一侧不算分歧(self) -> None:
        """PoB 侧没有基础伤害的技能（辅助宝石大多如此）不该因此被拒。"""
        levels = pob_levels()
        for row in levels.values():
            row["base_damage"] = None
        self.assertEqual([], reconcile(self._page(), levels))

    def test_两列都像伤害时不做伤害对账(self) -> None:
        """实测 10 颗宝石有两列都像伤害（自己的伤害 + 被辅助技能附加的伤害），
        那时不知道该跟哪一列比，就不比——猜一列去比会造出假分歧。"""
        table = level_table(
            ["等级", "需求等级", "造成 5 - 9 火焰伤害", "被辅助技能附加 1 - 2 冰霜伤害"],
            [["1", "12", "5, 9", "1, 2"]],
        )
        page = parse_gem_page(gem_detail(table), poedb_slug="X")
        assert page is not None
        self.assertIsNone(damage_column(page))
        self.assertEqual([], reconcile(page, {1: {"level_requirement": 12, "cost": {},
                                                  "base_damage": (777.0, 888.0)}}))

    def test_库里没有那一级时跳过(self) -> None:
        self.assertEqual([], reconcile(self._page(), {}))


# ── 出口：卡片与工具 ────────────────────────────────────────────


class CardTest(unittest.TestCase):
    """卡片必须真的写出这两维——「入库但没有出口」是踩过三次的坑。"""

    def _card(self, levels: list[dict], stat_lines: list[dict]) -> str:
        from tests.unit.test_pob_skills import _entity as build

        entity = build(levels, stat_lines=stat_lines)
        return render_skills.render_card(entity)

    def test_属性需求写在等级行上(self) -> None:
        card = self._card(
            [{"gem_level": 1, "level_requirement": 12, "req_int": 33},
             {"gem_level": 20, "level_requirement": 70, "req_int": 155}],
            [],
        )
        self.assertIn("属性需求 智慧 33", card)
        self.assertIn("属性需求 智慧 155", card)

    def test_效果文本带两个关键等级的取值(self) -> None:
        card = self._card(
            [{"gem_level": 1, "stat_values": {"0": "25, 38"}},
             {"gem_level": 20, "stat_values": {"0": "1122, 1683"}}],
            [{"index": 0, "text_zh": "造成 25 - 38 冰霜伤害"}],
        )
        self.assertIn("**效果文本（POEDB 逐级表）**", card)
        self.assertIn("造成 25 - 38 冰霜伤害", card)
        self.assertIn("20 级 1122, 1683", card)

    def test_没有效果文本时不留空标题(self) -> None:
        card = self._card([{"gem_level": 1, "level_requirement": 12}], [])
        self.assertNotIn("效果文本", card)

    def test_超过上限要写出还剩多少(self) -> None:
        """静默截断等同于漏答（与 `mod_tiers` 同一条约定）。"""
        lines = [{"index": i, "text_zh": f"效果 {i}"} for i in range(9)]
        card = self._card([{"gem_level": 1}], lines)
        self.assertIn(f"另有 {9 - render_skills.MAX_CARD_STAT_LINES} 条效果文本", card)

    def test_卡片不超过切片上限(self) -> None:
        lines = [{"index": i, "text_zh": f"效果文本第 {i} 条，写得长一些占字数" * 2}
                 for i in range(6)]
        card = self._card([{"gem_level": 1}, {"gem_level": 20}], lines)
        self.assertLessEqual(len(card), render_skills.MAX_CHUNK_CHARS)


def tool_conn() -> sqlite3.Connection:
    """两颗宝石：一颗有 POEDB 两维，一颗没有（对账没过的那种）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO pob_skill_gem(gem_id,name_en,name_zh,name_zh_alt,poedb_slug,gem_kind,"
        "attr_color,tags,tags_zh,tag_string,skill_types,natural_max_level,is_vaal,"
        "is_transfigured,source_file,source,source_priority,pob_url,poedb_url,"
        "source_commit,content_hash,updated_at) VALUES "
        "(?,?,?,'[]',?,'active','int','[]','[]','','[]',20,0,0,'Data/Skills/act_int.lua',"
        "'Path of Building',2,'https://github.com/x/blob/abc/f.lua',"
        "'https://poedb.tw/cn/x',?,?,datetime('now'))",
        [
            ("IceNova", "Ice Nova", "冰霜新星", "Ice_Nova", "d" * 40, "h1"),
            ("Convocation", "Convocation", "号召", "Convocation", "d" * 40, "h2"),
        ],
    )
    conn.executemany(
        "INSERT INTO pob_skill_level(gem_id,gem_level,level_requirement,cost) "
        "VALUES (?,?,?,'{\"Mana\": 8}')",
        [("IceNova", 1, 12), ("IceNova", 20, 70), ("Convocation", 1, 31)],
    )
    conn.executemany(
        "INSERT INTO poedb_gem_stat(gem_id,stat_index,text_zh,source,source_priority,url,"
        "fetched_at,updated_at) VALUES (?,?,?,'POEDB',1,'https://poedb.tw/cn/Ice_Nova',"
        "datetime('now'),datetime('now'))",
        [("IceNova", 0, "造成 25 - 38 冰霜伤害"), ("IceNova", 1, "基础范围为 2.6 米")],
    )
    conn.executemany(
        "INSERT INTO poedb_gem_stat_level(gem_id,gem_level,req_str,req_dex,req_int,"
        "values_json,source,source_priority,url,updated_at) "
        "VALUES (?,?,NULL,NULL,?,?,'POEDB',1,'https://poedb.tw/cn/Ice_Nova',datetime('now'))",
        [
            ("IceNova", 1, 33, json.dumps({"0": "25, 38", "1": "2.6"})),
            ("IceNova", 20, 155, json.dumps({"0": "1122, 1683", "1": "3"})),
        ],
    )
    conn.commit()
    return conn


class ToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = tool_conn()
        self.patch = patch("tools.impl.gems.dbmod.get_conn", return_value=self.conn)
        self.patch.start()
        from tools.impl.gems import skill_gem_levels

        self.tool = skill_gem_levels

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_效果文本进证据正文(self) -> None:
        text = self.tool("冰霜新星").citations[0]["text"]
        self.assertIn("造成 25 - 38 冰霜伤害", text)
        self.assertIn("20 级 1122, 1683", text)

    def test_属性需求写在等级行上(self) -> None:
        text = self.tool("冰霜新星", level=20).citations[0]["text"]
        self.assertIn("属性需求 智慧 155", text)

    def test_结构化字段也给出这两维(self) -> None:
        data = self.tool("冰霜新星").data
        self.assertEqual(["造成 25 - 38 冰霜伤害", "基础范围为 2.6 米"],
                         data["gems"][0]["stat_lines"])
        self.assertEqual({"req_int": 155}, data["gems"][0]["requirements_by_level"]["20"])

    def test_边界声明只留DPS与获取途径(self) -> None:
        boundary = self.tool("冰霜新星").data["boundary"]
        self.assertIn("DPS", boundary)
        self.assertIn("获取途径", boundary)
        self.assertNotIn("效果描述文本", boundary)

    def test_没入库的那一颗逐颗说真话(self) -> None:
        """成对点：819 颗有效果文本，`号召` 没有。笼统说「本库未采集效果文本」
        会在另外 819 颗上变成假话，笼统说「有」会在这一颗上变成假话。"""
        text = self.tool("号召").citations[0]["text"]
        self.assertIn("本库没有这颗宝石的效果文本与属性需求", text)
        self.assertNotIn("造成", text)


if __name__ == "__main__":
    unittest.main()


# ── 固定效果（第四十四轮）─────────────────────────────────────


def fixed_table(rows: list[str], *, active: bool = True) -> str:
    """一张「固定」表，包在 tab-pane 里（真实页面就是这个结构）。"""
    body = "".join(f"<tr><td>{r}</td></tr>" for r in rows)
    cls = "tab-pane fade show active" if active else "tab-pane fade"
    return (
        f'<div class="{cls}"><table><thead><tr><th>固定</th></tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


class FixedStatsParseTest(unittest.TestCase):
    """`固定` 表：与等级无关的技能机制。"""

    def _parse(self, html: str, slug: str = "Ice_Nova"):
        from kb.sources.poedb_gem_page import parse_gem_fixed

        return parse_gem_fixed(html, poedb_slug=slug)

    def test_中文文本与内部字段分流(self) -> None:
        """`CooldownTime: 5000` 不是内容，是对账口径。把内部字段当中文答案输出
        就是拿调试字段冒充游戏文本（怪物词缀那一轮定下的禁忌）。"""
        got = self._parse(fixed_table([
            "CooldownTime: 5000", "有 25% 的几率对敌人造成冻结状态", "Mana: 50%",
        ]))
        self.assertEqual(got.prose, ("有 25% 的几率对敌人造成冻结状态",))
        self.assertEqual(got.reconcile_kv, {"CooldownTime": "5000", "Mana": "50%"})

    def test_字面反斜杠n换成空格且不拆行(self) -> None:
        """实测 126 条含它，而它**有时夹在句子中间**——拆开会得到两个读不通的碎片。
        一个单元格是上游的一个断言单位。"""
        got = self._parse(fixed_table(["对召唤生物伤害的增幅与减益\\n有 250% 也作用于该技能的伤害"]))
        self.assertEqual(len(got.prose), 1)
        self.assertNotIn("\\n", got.prose[0])
        self.assertIn("增幅与减益 有 250%", got.prose[0])

    def test_数值两侧的空格要保留(self) -> None:
        """数字在 `<span class="mod-value">` 里。`text(strip=True)` 会得到
        `增助攻击的暴击率+25%`，而页面上是 `增助攻击的暴击率 +25%`。"""
        got = self._parse(fixed_table(
            ['增助攻击的暴击率 <span class="mod-value">+25</span>%']))
        self.assertEqual(got.prose, ("增助攻击的暴击率 +25%",))

    def test_两张表时取选中的那一张(self) -> None:
        """实测 280 张页有两张固定表，其中 **98 张内容不一样**
        （静滞之天雷之珠 2.8 米 vs 1.8 米）。取第一张就是在两个互相矛盾的说法里
        无依据地挑一个。"""
        html = fixed_table(["基础范围为 1.8 米"], active=False) + fixed_table(
            ["基础范围为 2.8 米"], active=True)
        self.assertEqual(self._parse(html).prose, ("基础范围为 2.8 米",))

    def test_只有一张表时即使没选中也用它(self) -> None:
        """没有可挑的就没有挑错的风险（实测 5 颗如此）。"""
        got = self._parse(fixed_table(["最多储存 50 次使用次数"], active=False))
        self.assertEqual(got.prose, ("最多储存 50 次使用次数",))

    def test_多张表且都没选中要报错(self) -> None:
        html = (fixed_table(["A"], active=False) + fixed_table(["B"], active=False))
        with self.assertRaises(GemPageError):
            self._parse(html)

    def test_没有固定表返回空(self) -> None:
        got = self._parse("<table><thead><tr><th>等级</th></tr></thead></table>")
        self.assertEqual(got.prose, ())
        self.assertEqual(got.reconcile_kv, {})


class FixedReconcileTest(unittest.TestCase):
    """那 444 条内部字段是这一维**唯一**的对账口径。"""

    def _reconcile(self, kv: dict, pob: dict | None):
        from kb.sources.poedb_gem_page import GemFixedStats, reconcile_fixed

        return reconcile_fixed(GemFixedStats(prose=(), reconcile_kv=kv), pob)

    def test_冷却毫秒要换算成秒(self) -> None:
        """POEDB 写毫秒、PoB 写秒，差 1000 倍。不换算会把全对报成全错。"""
        self.assertEqual(self._reconcile({"CooldownTime": "5000"}, {"cooldown": 5.0}), [])
        self.assertTrue(self._reconcile({"CooldownTime": "5000"}, {"cooldown": 8.0}))

    def test_带百分号的魔力走保留而不是消耗(self) -> None:
        """`Mana: 50%` 是光环的保留百分比。跟定值消耗比会把光环全判成分歧。"""
        self.assertEqual(
            self._reconcile({"Mana": "50%"}, {"reservation_percent": 50.0, "cost": {}}), [])

    def test_不带百分号的魔力走消耗(self) -> None:
        """实测抓到一颗真分歧：破坏之冰锥地雷 POEDB 15 / PoB 3。
        它的姊妹技能破坏之火屑地雷早就因同样的 5 倍差被拒，而它一直漏网——
        因为逐级表只能比**存在的列**，而那一页根本没有魔力列。"""
        self.assertTrue(self._reconcile({"Mana": "15"}, {"cost": {"Mana": 3.0}}))
        self.assertEqual(self._reconcile({"Mana": "15"}, {"cost": {"Mana": 15.0}}), [])

    def test_缺一侧不算分歧(self) -> None:
        self.assertEqual(self._reconcile({"CooldownTime": "5000"}, {}), [])
        self.assertEqual(self._reconcile({}, {"cooldown": 5.0}), [])


class FixedCardTest(unittest.TestCase):
    def _card(self, fixed: list[str]) -> str:
        from tests.unit.test_pob_skills import _entity as build

        return render_skills.render_card(build(
            [{"gem_level": 1, "level_requirement": 12}], fixed_stats=fixed))

    def test_固定效果写在卡上(self) -> None:
        card = self._card(["有 25% 的几率对敌人造成冻结状态"])
        self.assertIn("**固定效果（与等级无关）**", card)
        self.assertIn("有 25% 的几率对敌人造成冻结状态", card)

    def test_没有固定效果时不出区块也不写未采集(self) -> None:
        """实测 21 颗页面上没有这张表——那是「这个技能没有与等级无关的机制」，
        **不是采集缺口**。写「未采集」就是一句假话。
        这与获取途径那 152 颗不同：那些是页面确实没给这一维。"""
        card = self._card([])
        self.assertNotIn("固定效果", card)
        self.assertNotIn("未采集该宝石的固定", card)

    def test_超过上限要写出还剩多少(self) -> None:
        """实测单颗最多 16 条，这个上限会真的触发。"""
        many = [f"固定效果第 {i} 条" for i in range(render_skills.MAX_CARD_FIXED_LINES + 4)]
        self.assertIn("另有 4 条固定效果", self._card(many))
