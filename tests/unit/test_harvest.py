"""收获（古灵庄园）：抽取、入库、覆盖声明与工具边界（2026-08-27 第三十六轮起）。

判据来自实测（`/cn/Horticrafting`，74 条工艺 / 98 条费用 / 24 条双费用 / 5 种通货）：

* 这一页有**三张表，三张都取**——分三轮才取全（第三十六 / 五十八 / 六十五轮），
  所以**按表头定位而不是按下标**；后两张各挂了二十多轮都是因为**名字被写错**
  （表 1 叫成「作物与掉落来源」、表 2 叫成「催化剂等参考表」）；
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
def _badge(text: str, cls: str = "", *, italic: bool = False) -> str:
    """页面的标签是 `<span class="badge …">`，英文那些外面还套一层 `<i>`。"""
    inner = f"<i>{text}</i>" if italic else text
    return f'<span class="badge bg-primary {cls}">{inner}</span>'


#: 标签权重乘数表。**逐字照着实测的 DOM 形状构造**（`/cn/Horticrafting`，2026-09-08）：
#: 化石与工艺工作台的乘数是**跟在 span 后面的文本节点**（` x0, ` / ` x10`），
#: 而催化那两个 span **相邻且中间只有 `<br>`**——没有任何文本分隔符。
#: 后者正是「不能对整格 `text()` 切分」的实例：实测整格取文本会得到
#: `physical_damagechaos_damage` 这一个粘起来的假标签。
MISC_TABLE = (
    '<table><thead><tr><th>Type</th><th>Name</th><th>Tags</th></tr></thead>'
    "<tbody>"
    f"<tr><td>化石</td><td>炽炎化石</td><td>{_badge('冰霜', 'craftingcold')} x0, "
    f"{_badge('火焰', 'craftingfire')} x10</td></tr>"
    f"<tr><td>催化</td><td>有害催化剂</td><td>{_badge('physical_damage', italic=True)}"
    f"<br>{_badge('chaos_damage', italic=True)}</td></tr>"
    f"<tr><td>花园工艺站</td><td>荒野幼熊</td><td>{_badge('施法', 'craftingcaster')}</td></tr>"
    "<tr><td>工艺工作台</td><td>不能附加武器攻击相关属性修正</td>"
    f"<td>{_badge('攻击', 'craftingattack')} x0</td></tr>"
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

    def test_三张表都取完之后没有省略项(self) -> None:
        """`not_taken` **空着才是对的**，不是忘了填。

        这个字段的用途是「有意省略了多少行」，页面全取完之后就该没有条目。
        它的历史正好是这一页的历史：第三十六轮两张没取、第五十八轮种子表入库、
        第六十五轮标签权重表入库——**每一轮都以为剩下的那张属于别的域**。

        断言写成「等于空字典」而不是「不含催化剂那一条」：后者在字段忘了清空时
        照样通过，而那正是第六十五轮实际发生过的事（`_record_harvest_gaps`
        当时 `if not points: return 0` 早退，台账里那条「41 行未入库」留了下来）。
        """
        html = page(
            craft_row("重铸", cost_link("狂野紫晶命能", 50)),
            extra_tables=CROP_TABLE + MISC_TABLE,
        )
        _, stats = extract_harvest_crafts(html)
        self.assertEqual({}, stats.not_taken)

    def test_种子怪物按颜色取出(self) -> None:
        """**这张表第三十六轮起被叫错了名字**：台账里写的是「作物与掉落来源」，
        而它三列其实是 `怪物 ｜ 阶级 ｜ Colour`。照那个名字去想会以为它是
        「哪种作物掉什么」，实际是「哪个怪物属于哪个颜色」——注释里的名字也会骗人。
        """
        from kb.sources.poedb_harvest import parse_seeds

        seeds = parse_seeds(page(
            craft_row("重铸", cost_link("狂野紫晶命能", 50)),
            extra_tables=CROP_TABLE,
        ))
        self.assertTrue(seeds)
        for seed in seeds:
            self.assertTrue(seed.monster_zh)
            self.assertGreater(seed.tier, 0)
            self.assertIn(seed.colour, ("Wild", "Vivid", "Primal"))

    def test_没有种子表时返回空而不是报错(self) -> None:
        from kb.sources.poedb_harvest import parse_seeds

        self.assertEqual((), parse_seeds(page(craft_row("重铸", cost_link("狂野紫晶命能", 50)))))

    def test_阶级不是数字就报错(self) -> None:
        """不猜：阶级用来分档，猜错会把 1 阶怪物说成 4 阶。"""
        from kb.sources.poedb_harvest import HarvestStructureError, parse_seeds

        bad = ('<table><thead><tr><th>怪物</th><th>阶级</th><th>Colour</th></tr></thead>'
               "<tbody><tr><td>荒野幼熊</td><td>甲</td><td>Wild</td></tr></tbody></table>")
        with self.assertRaises(HarvestStructureError):
            parse_seeds(page(craft_row("重铸", cost_link("狂野紫晶命能", 50)),
                             extra_tables=bad))

    # ── 标签权重乘数表（第六十五轮）──

    def _weights(self, extra: str = MISC_TABLE):  # noqa: ANN202
        from kb.sources.poedb_harvest import parse_tag_weights  # noqa: PLC0415

        return parse_tag_weights(page(
            craft_row("重铸", cost_link("狂野紫晶命能", 50)), extra_tables=extra))

    def test_标签逐个span取而不是对整格切分(self) -> None:
        """**这是这张表最容易写错的一处**：催化那两个 span 相邻、中间只有 `<br>`，
        没有任何文本分隔符。实测整格 `text()` 得到的是
        `physical_damagechaos_damage` ——一个粘起来的、库里永远查不到的假标签。

        所以这里断言的是**两个独立标签**，而不只是「解析出了东西」。
        """
        tags = [w.tag for w in self._weights() if w.name_zh == "有害催化剂"]
        self.assertEqual(["physical_damage", "chaos_damage"], tags)

    def test_只有化石与工艺工作台给乘数另两类留空(self) -> None:
        """**留 None 而不是补 1**：页面没给数字与「乘数是 1」不是一回事，
        补 1 会把「页面没说」写成一个看起来很合理的假值。
        """
        by_name = {}
        for w in self._weights():
            by_name.setdefault(w.name_zh, []).append(w)
        self.assertEqual(
            [0.0, 10.0], [w.multiplier for w in by_name["炽炎化石"]])
        self.assertEqual(
            [0.0], [w.multiplier for w in by_name["不能附加武器攻击相关属性修正"]])
        # 催化与花园工艺站：页面只列标签
        for name in ("有害催化剂", "荒野幼熊"):
            self.assertTrue(
                all(w.multiplier is None for w in by_name[name]),
                f"{name} 的乘数该留空",
            )

    def test_x0是屏蔽而留空不算屏蔽(self) -> None:
        """`x0` 是「这一类词缀不会出现」这个**确定的事实**，NULL 是「页面没说」。
        两者在事后分不清，所以 `blocks` 只认 0。
        """
        by_tag = {(w.name_zh, w.tag): w for w in self._weights()}
        self.assertTrue(by_tag[("炽炎化石", "冰霜")].blocks)
        self.assertFalse(by_tag[("炽炎化石", "火焰")].blocks)
        self.assertFalse(by_tag[("有害催化剂", "physical_damage")].blocks)

    def test_标签名中英混排照页面原值存(self) -> None:
        """英文那些在 `<i>` 里、上游没给中文。本库存页面原值、**不自己翻译**——
        翻译等于替上游造一个它没说过的名字。
        """
        tags = {w.tag for w in self._weights()}
        self.assertIn("冰霜", tags)
        self.assertIn("physical_damage", tags)

    def test_没有这张表时返回空而不是报错(self) -> None:
        """与种子表同一条：这一页可能只有主表。"""
        self.assertEqual((), self._weights(extra=""))

    def test_解析不出标签的行整批拒绝(self) -> None:
        """一行有名字却一个标签都取不出来，说明 DOM 变了。

        **不能静默跳过**：这张表的用户问题正是「这个化石屏蔽什么」，
        少一行就是答错而不是答少。
        """
        bad = ('<table><thead><tr><th>Type</th><th>Name</th><th>Tags</th></tr></thead>'
               "<tbody><tr><td>化石</td><td>炽炎化石</td><td>冰霜 x0</td></tr>"
               "</tbody></table>")
        with self.assertRaisesRegex(HarvestStructureError, "炽炎化石"):
            self._weights(extra=bad)

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
        # 第五十八轮起这句说的是「已入库」而不是「未采集」——断言要跟着真值走，
        # 否则它会在措辞反转之后继续通过（同一个词，相反的意思）
        self.assertIn("命能的获取途径已入库", harvest.spec.partial_note)


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
        # **不再断言「获取途径未采集」**：第五十八轮种子怪物入库后那句是假话。
        # 现在这个工具的边界是「掉落数量与概率」，并指向 harvest_seed_lookup
        self.assertIn("掉落数量与概率未采集", result.data["boundary"])
        self.assertIn("harvest_seed_lookup", result.data["boundary"])
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


class TagWeightToolTest(unittest.TestCase):
    """`tag_weight_lookup`（第六十五轮）。用真实注册表调用。"""

    def setUp(self) -> None:
        import tools.impl  # noqa: F401,PLC0415  注册副作用
        from core import db as dbmod  # noqa: PLC0415
        from kb.sources.poedb_harvest import parse_tag_weights  # noqa: PLC0415

        self.conn = _conn()
        weights = parse_tag_weights(page(
            craft_row("重铸", cost_link("狂野紫晶命能", 50)), extra_tables=MISC_TABLE))
        self._insert_weights(weights)
        self._real = dbmod.get_conn
        dbmod.get_conn = lambda: self.conn  # type: ignore[assignment]

    def _insert_weights(self, weights) -> None:  # noqa: ANN001
        self.conn.executemany(
            "INSERT INTO poedb_tag_weight(kind, name_zh, tag, multiplier, row_index,"
            " source, source_priority, url, fetched_at, updated_at)"
            " VALUES (?,?,?,?,?,'POEDB',1,?,'2026-09-08T00:00:00Z', datetime('now'))",
            [
                (w.kind, w.name_zh, w.tag, w.multiplier, i, HORTICRAFTING_URL)
                for i, w in enumerate(weights)
            ],
        )
        self.conn.commit()

    def tearDown(self) -> None:
        from core import db as dbmod  # noqa: PLC0415

        dbmod.get_conn = self._real  # type: ignore[assignment]
        self.conn.close()

    def _call(self, **kwargs):  # noqa: ANN003, ANN201
        from tools.registry import REGISTRY  # noqa: PLC0415

        return REGISTRY.call("tag_weight_lookup", json.dumps(kwargs))

    def test_按名字查出这个化石改哪些标签(self) -> None:
        result = self._call(query="炽炎化石")
        self.assertTrue(result.data["found"])
        self.assertEqual(2, result.data["total"])
        rows = {r["tag"]: r for r in result.data["rows"]}
        self.assertTrue(rows["冰霜"]["blocks"])
        self.assertEqual(10.0, rows["火焰"]["multiplier"])

    def test_可按标签反查哪个化石放大它(self) -> None:
        """「哪个化石能提高火焰词缀」问的是标签那一侧。"""
        result = self._call(query="火焰")
        self.assertTrue(result.data["found"])
        self.assertEqual(["炽炎化石"], [r["name"] for r in result.data["rows"]])

    def test_整数乘数不留小数点(self) -> None:
        """**这条是给出口闸门的**：证据里写 `x10.0` 而页面写 `x10`，
        模型照页面复述时数值回填会因为多出来的那一位找不到，
        于是**一句完全正确的话被自己的闸门拦掉**（第二十轮同型缺陷）。
        """
        text = self._call(query="炽炎化石").citations[0]["text"]
        self.assertIn("x10", text)
        self.assertNotIn("x10.0", text)

    def test_页面没给乘数时写明而不是补1(self) -> None:
        """留空不等于「乘数是 1」，证据里必须把这一点说出来。"""
        result = self._call(query="有害催化剂")
        self.assertTrue(all(r["multiplier"] is None for r in result.data["rows"]))
        self.assertIn("（页面未给）", result.citations[0]["text"])

    def test_查不到时说清是哪一种查不到(self) -> None:
        """「库里没有」不等于「它没有效果」——与幽魂搭配那条同一规矩。"""
        result = self._call(query="不存在的化石")
        self.assertTrue(result.ok)
        self.assertFalse(result.data["found"])
        self.assertIn("这不等于它没有效果", result.data["reason"])

    def test_证据带边界且不承诺成功率(self) -> None:
        """有了权重不等于有了成功率——这一维确实没采集。"""
        result = self._call(query="炽炎化石")
        self.assertIn("成功率", result.data["boundary"])
        self.assertIn("共鸣器", result.data["boundary"])
        self.assertIn("成功率", result.citations[0]["text"])

    def test_截断必须写出还剩多少(self) -> None:
        """静默截断等同于漏答。"""
        from tools.impl.harvest import MAX_TAG_WEIGHT_ROWS  # noqa: PLC0415
        from kb.sources.poedb_harvest import TagWeight  # noqa: PLC0415

        extra = MAX_TAG_WEIGHT_ROWS + 3
        self._insert_weights([
            TagWeight(kind="化石", name_zh="测试化石", tag=f"标签{i}", multiplier=1.0)
            for i in range(extra)
        ])
        result = self._call(query="测试化石")
        self.assertEqual(extra, result.data["total"])
        self.assertTrue(result.data["truncated"])
        self.assertIn(
            f"还有 {extra - MAX_TAG_WEIGHT_ROWS} 行未列出", result.citations[0]["text"])

    def test_通配符按字面量搜索(self) -> None:
        """**这张表最需要转义**：标签名自带下划线（`physical_damage`），
        不转义时用户输入里的 `_` 会变成「任意一个字符」，`%` 直接匹配全部。
        """
        self.assertFalse(self._call(query="physical%damage").data["found"])
        self.assertFalse(self._call(query="physical_dam_ge").data["found"])
        self.assertTrue(self._call(query="physical_damage").data["found"])

    def test_纯通配符与空查询被拒(self) -> None:
        self.assertFalse(self._call(query="%").ok)
        self.assertFalse(self._call(query="  ").ok)


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

    def test_权重乘数问法路由到标签工具(self) -> None:
        for q in ("炽炎化石屏蔽哪些标签", "炽炎化石有什么用",
                  "哪个化石能提高火焰词缀的权重", "化石的权重乘数是多少"):
            self.assertEqual("tag_weight_lookup", self._route(q), q)

    def test_化石的词缀池问法不得被抢走(self) -> None:
        """**裸的「化石」不能进意图词**：化石的词缀池早就入库成 1983 条
        `category='delve'`，「某个化石能开出哪些词缀」是词缀卡与 `mod_tiers` 的活。

        这与「命能」那次是同一个错——放裸词会把另一张表上才有的那一列抢走。
        """
        for q in ("凝结化石能开出哪些词缀", "地心探索的化石词缀有哪些",
                  "炽炎化石能出的词缀等阶"):
            self.assertNotEqual("tag_weight_lookup", self._route(q), q)

    def test_按标签找化石归权重工具而不是词缀池(self) -> None:
        """「**哪个**化石能开出生命词缀」与上一条只差一个疑问词，归属却相反，
        所以这条边界要显式钉住、不能靠「碰巧没匹配上」。

        判据是**问的是哪一侧**：上一条给定化石、问它的词缀池（词缀表 1983 条）；
        这一条给定标签、问哪个化石偏向它——那正是这张权重表的列
        （实测 `原始化石 → 生命 x10`、`致密化石 → 生命 x0`，两条都在库里）。
        """
        for q in ("哪个化石能开出最大生命词缀", "哪个化石可以打出暴击词缀"):
            self.assertEqual("tag_weight_lookup", self._route(q), q)


if __name__ == "__main__":
    unittest.main()
