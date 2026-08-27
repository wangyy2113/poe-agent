"""收获（古灵庄园）定向工艺：抽取、入库、覆盖声明与工具边界（2026-08-27 第三十六轮）。

判据来自实测（`/cn/Horticrafting`，74 条工艺 / 98 条费用 / 24 条双费用 / 5 种通货）：

* 这一页有**三张表**，另外两张（51 行作物与掉落、41 行催化/化石混排）有意不取，
  所以**按表头定位而不是按下标**；
* 费用的写法与工艺台**相反**（`<a>通货</a> x50`，数量在后），另写解析器；
* `crafting*` span class 是**配色不是语义**，不得进入产物；
* 费用通货按 `data-hover` 里的元数据路径与 `poedb_currency` 对账，实测 5/5。

这个文件不连网：HTML 是内联构造的最小样例，库用内存 SQLite + 真实 SCHEMA。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from kb.coverage import probe  # noqa: E402
from kb.sources.poedb_harvest import (  # noqa: E402
    HORTICRAFTING_URL,
    HarvestStructureError,
    extract_harvest_crafts,
)

_META = "?s=Data%5CBaseItemTypes%2FMetadata%2FItems%2FCurrency%2FHarvestSeedRed"
_META2 = "?s=Data%5CBaseItemTypes%2FMetadata%2FItems%2FCurrency%2FHarvestSeedBoss"


def cost_link(name: str, amount: int, hover: str = _META, href: str | None = None) -> str:
    slug = href or name
    return (
        f'<a class="item_currency StackableCurrency" data-hover="{hover}" '
        f'href="{slug}"><img alt="x">{name}</a> x{amount}'
    )


def craft_row(desc_html: str, cost_html: str) -> str:
    return f"<tr><td>{desc_html}</td><td>{cost_html}</td></tr>"


def page(rows: str, *, extra_tables: str = "") -> str:
    return (
        "<html><body>"
        '<table><thead><tr><th>描述</th><th>消耗</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>{extra_tables}"
        "</body></html>"
    )


CROP_TABLE = (
    '<table><thead><tr><th>怪物</th><th>阶级</th><th>Colour</th></tr></thead>'
    "<tbody><tr><td>荒野幼熊</td><td>1</td><td>Wild</td></tr>"
    "<tr><td>荒野幼狼</td><td>1</td><td>Wild</td></tr></tbody></table>"
)
MISC_TABLE = (
    '<table><thead><tr><th>Type</th><th>Name</th><th>Tags</th></tr></thead>'
    "<tbody><tr><td>催化</td><td>猛烈催化剂</td><td>elemental_damage</td></tr>"
    "</tbody></table>"
)

_SIMPLE = page(
    craft_row(
        '<span class="white">重铸</span>一件带随机词缀的稀有物品，'
        '其中包括一个<span class="craftingfire">火焰</span>词缀',
        cost_link("狂野紫晶命能", 50, href="Wild_Crystallised_Lifeforce"),
    )
)


class ExtractTest(unittest.TestCase):
    def test_抽出描述与费用(self) -> None:
        crafts, stats = extract_harvest_crafts(_SIMPLE)
        self.assertEqual(1, len(crafts))
        craft = crafts[0]
        self.assertEqual(
            "重铸一件带随机词缀的稀有物品，其中包括一个火焰词缀", craft.effect_text
        )
        self.assertEqual(1, len(craft.costs))
        cost = craft.costs[0]
        self.assertEqual(50, cost.amount)
        self.assertEqual("狂野紫晶命能", cost.currency_name_zh)
        self.assertEqual(
            "Metadata/Items/Currency/HarvestSeedRed", cost.currency_metadata_id
        )
        self.assertEqual(
            "https://poedb.tw/cn/Wild_Crystallised_Lifeforce", cost.currency_url
        )
        self.assertEqual(1, stats.total)

    def test_配色class不得进入产物(self) -> None:
        """`craftingfire` 是配色不是语义：「召唤生物词缀」也标 craftingcaster，
        「元素词缀」则一个 class 都没有。存了它，按分类查就会既错又漏。
        """
        craft = extract_harvest_crafts(_SIMPLE)[0][0]
        self.assertNotIn("crafting", craft.effect_text)
        self.assertNotIn("span", craft.effect_text)
        # 分类词本来就在描述原文里，不需要另建分类列
        self.assertIn("火焰", craft.effect_text)

    def test_双组件费用按位置排序(self) -> None:
        html = page(
            craft_row(
                "重铸一件带随机词缀的受影响的稀有物品",
                cost_link("原始蓝晶命能", 5000, href="Primal_Crystallised_Lifeforce")
                + "<br>"
                + cost_link("神圣白晶命能", 1, _META2, href="Sacred_Crystallised_Lifeforce"),
            )
        )
        crafts, stats = extract_harvest_crafts(html)
        costs = crafts[0].costs
        self.assertEqual([0, 1], [c.position for c in costs])
        self.assertEqual([5000, 1], [c.amount for c in costs])
        self.assertEqual(1, stats.multi_cost_crafts)
        self.assertEqual(2, stats.costs)

    def test_按表头定位而不是按下标(self) -> None:
        """同页另有两张表；主表在最后也必须找得到。"""
        html = (
            "<html><body>"
            + CROP_TABLE
            + MISC_TABLE
            + '<table><thead><tr><th>描述</th><th>消耗</th></tr></thead><tbody>'
            + craft_row("重铸", cost_link("狂野紫晶命能", 50))
            + "</tbody></table></body></html>"
        )
        crafts, _ = extract_harvest_crafts(html)
        self.assertEqual(1, len(crafts))

    def test_未取的两张表按行数留痕(self) -> None:
        """静默省略与「页面上就没有」事后分不清，所以条数要报出来。"""
        html = page(
            craft_row("重铸", cost_link("狂野紫晶命能", 50)),
            extra_tables=CROP_TABLE + MISC_TABLE,
        )
        _, stats = extract_harvest_crafts(html)
        self.assertEqual({"作物与掉落来源": 2, "相关物品": 1}, stats.not_taken)

    def test_缺数量整批拒绝(self) -> None:
        html = page(
            craft_row(
                "重铸",
                '<a class="item_currency" data-hover="' + _META + '" href="x">狂野紫晶命能</a>',
            )
        )
        with self.assertRaisesRegex(HarvestStructureError, "没有对应数量"):
            extract_harvest_crafts(html)

    def test_数量没有对应通货整批拒绝(self) -> None:
        html = page(craft_row("重铸", " x50"))
        with self.assertRaisesRegex(HarvestStructureError, "没有通货链接"):
            extract_harvest_crafts(html)

    def test_缺元数据路径整批拒绝(self) -> None:
        """元数据路径是与 poedb_currency 对账的键，缺了就核对不了。"""
        html = page(
            craft_row(
                "重铸",
                '<a class="item_currency" data-hover="?s=whatever" href="x">狂野紫晶命能</a> x50',
            )
        )
        with self.assertRaisesRegex(HarvestStructureError, "元数据路径"):
            extract_harvest_crafts(html)

    def test_找不到主表时报出观测到的表头(self) -> None:
        with self.assertRaisesRegex(HarvestStructureError, "观测到的表头"):
            extract_harvest_crafts("<html><body>" + CROP_TABLE + "</body></html>")

    def test_完整重复的行拒绝静默去重(self) -> None:
        row = craft_row("重铸", cost_link("狂野紫晶命能", 50))
        with self.assertRaisesRegex(HarvestStructureError, "完全重复"):
            extract_harvest_crafts(page(row + row))

    def test_描述相同但费用不同不算重复(self) -> None:
        """id 是整行的哈希，不是描述的哈希——否则会把两条真记录压成一条。"""
        html = page(
            craft_row("重铸", cost_link("狂野紫晶命能", 50))
            + craft_row("重铸", cost_link("狂野紫晶命能", 100))
        )
        crafts, _ = extract_harvest_crafts(html)
        self.assertEqual(2, len(crafts))
        self.assertNotEqual(crafts[0].craft_id, crafts[1].craft_id)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _insert(conn: sqlite3.Connection, crafts) -> None:  # noqa: ANN001
    for craft in crafts:
        conn.execute(
            "INSERT INTO harvest_craft(craft_id, effect_text, source, source_priority,"
            " url, content_hash, fetched_at, updated_at)"
            " VALUES (?,?,'POEDB',1,?,?, '2026-08-27T00:00:00Z', datetime('now'))",
            (craft.craft_id, craft.effect_text, craft.url, craft.content_hash),
        )
        for cost in craft.costs:
            conn.execute(
                "INSERT INTO harvest_craft_cost(craft_id, position, amount,"
                " currency_metadata_id, currency_slug, currency_name_zh, currency_url)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    craft.craft_id, cost.position, cost.amount,
                    cost.currency_metadata_id, cost.currency_slug,
                    cost.currency_name_zh, cost.currency_url,
                ),
            )
    conn.commit()


class StorageAndCoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        crafts, _ = extract_harvest_crafts(_SIMPLE)
        _insert(self.conn, crafts)

    def tearDown(self) -> None:
        self.conn.close()

    def test_删工艺会级联删费用(self) -> None:
        self.conn.execute("DELETE FROM harvest_craft")
        self.conn.commit()
        self.assertEqual(
            0, self.conn.execute("SELECT count(*) FROM harvest_craft_cost").fetchone()[0]
        )

    def test_费用金额必须为正(self) -> None:
        craft_id = self.conn.execute("SELECT craft_id FROM harvest_craft").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO harvest_craft_cost(craft_id, position, amount,"
                " currency_metadata_id, currency_slug, currency_name_zh, currency_url)"
                " VALUES (?,9,0,'m','s','n','https://x')",
                (craft_id,),
            )

    def test_覆盖声明由真实表探测(self) -> None:
        statuses = {s.key: s for s in probe(self.conn)}
        harvest = statuses["harvest"]
        self.assertEqual("partial", harvest.status)
        self.assertEqual(1, harvest.item_count)
        self.assertEqual(["狂野紫晶命能"], harvest.channels)
        self.assertIn("harvest_craft_catalog", harvest.spec.tools)
        # 这个域没有卡片，chunk 数为 0 是预期而不是缺陷
        self.assertEqual(0, harvest.chunk_count)
        self.assertIn("获取途径", harvest.spec.partial_note)


class CatalogToolTest(unittest.TestCase):
    """工具行为。用真实注册表调用，避免只测到一个平行实现。"""

    def setUp(self) -> None:
        import tools.impl  # noqa: F401,PLC0415  注册副作用
        from core import db as dbmod  # noqa: PLC0415

        self.conn = _conn()
        rows = "".join(
            craft_row(f"重铸词缀 {i}", cost_link("狂野紫晶命能", 50 + i))
            for i in range(5)
        )
        crafts, _ = extract_harvest_crafts(page(rows))
        _insert(self.conn, crafts)
        self._real = dbmod.get_conn
        dbmod.get_conn = lambda: self.conn  # type: ignore[assignment]

    def tearDown(self) -> None:
        from core import db as dbmod  # noqa: PLC0415

        dbmod.get_conn = self._real  # type: ignore[assignment]
        self.conn.close()

    def _call(self, **kwargs):  # noqa: ANN003, ANN201
        from tools.registry import REGISTRY  # noqa: PLC0415

        return REGISTRY.call("harvest_craft_catalog", json.dumps(kwargs))

    def test_截断必须写出还剩多少(self) -> None:
        """静默截断等同于漏答。"""
        result = self._call(query="重铸", limit=2)
        self.assertTrue(result.ok)
        self.assertEqual(5, result.data["total"])
        self.assertEqual(2, result.data["returned"])
        self.assertTrue(result.data["truncated"])
        self.assertIn("还有 3 条未列出", result.data["truncation_note"])

    def test_没截断时不写截断说明(self) -> None:
        result = self._call(query="重铸", limit=20)
        self.assertFalse(result.data["truncated"])
        self.assertNotIn("truncation_note", result.data)

    def test_可按费用通货名反查(self) -> None:
        """「狂野紫晶命能有什么用」问的是费用那一侧。"""
        result = self._call(query="狂野紫晶命能")
        self.assertTrue(result.data["found"])
        self.assertEqual(5, result.data["total"])

    def test_证据带边界且不泄露越界字段(self) -> None:
        result = self._call(query="重铸", limit=1)
        text = result.citations[0]["text"]
        self.assertIn("不证明", text)
        self.assertIn("50x 狂野紫晶命能", text)
        self.assertIn("成功率", result.data["boundary"])
        self.assertIn("获取途径", result.data["boundary"])
        self.assertNotIn("steps", result.data)
        self.assertNotIn("probability", result.data)

    def test_纯通配符与空查询被拒(self) -> None:
        self.assertFalse(self._call(query="%").ok)
        self.assertFalse(self._call(query="  ").ok)

    def test_通配符按字面量搜索(self) -> None:
        """参数绑定不会转义 % 与 _，必须自己转义，否则 `%` 会匹配全部。"""
        self.assertEqual(0, self._call(query="重铸%3").data["total"])

    def test_limit越界被拒(self) -> None:
        self.assertFalse(self._call(query="重铸", limit=0).ok)
        self.assertFalse(self._call(query="重铸", limit=999).ok)


class RoutingTest(unittest.TestCase):
    """意图词是**问法**不是话题词——这条判据坏过好几次。"""

    def _route(self, question: str) -> str:
        import tools.impl  # noqa: F401,PLC0415
        from orchestrator.loop import _preferred_tool_route  # noqa: PLC0415

        return _preferred_tool_route(question)

    def test_收获问法路由到目录工具(self) -> None:
        for q in ("收获有哪些能改火焰词缀的工艺", "古灵庄园能改词缀吗",
                  "狂野紫晶命能有什么用", "神圣白晶命能能做什么"):
            self.assertEqual("harvest_craft_catalog", self._route(q), q)

    def test_通货属性问法不得被抢走(self) -> None:
        """实测：先前放了裸的「命能」，「狂野紫晶命能的堆叠上限是多少」当场被抢到
        目录工具上——而堆叠上限在通货卡里，目录表根本没有这一列。
        """
        for q in ("狂野紫晶命能的堆叠上限是多少", "狂野紫晶命能最多堆叠几个"):
            self.assertNotEqual("harvest_craft_catalog", self._route(q), q)

    def test_工艺台问题不得被抢走(self) -> None:
        self.assertNotEqual(
            "harvest_craft_catalog", self._route("工艺台有没有最大生命的选项")
        )


if __name__ == "__main__":
    unittest.main()
