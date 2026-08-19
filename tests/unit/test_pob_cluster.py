"""星团珠宝：抽取、中文名桥接与工具边界（2026-08-19 第十八轮）。

这一轮补的是**分类**不是数据：300 个星团核心天赋的属性早就随天赋树入库了，
缺的是「它们只能从星团珠宝拿、不在主树上」这个事实。所以用例的重点是：
清单在库里 ≠ 那个映射在库里、以及 `passive_path` 的理由从「可能」变成事实。

全部离线：Lua 在临时目录里构造。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from kb.sources.pob_cluster import (  # noqa: E402
    ClusterStructureError,
    bridge_chinese,
    extract_cluster_data,
)
from tools.impl.cluster import cluster_jewel_info, cluster_notable_row  # noqa: E402

CLUSTER_LUA = """
return {
	jewels = {
		["Small Cluster Jewel"] = {
			size = "Small", sizeIndex = 0, minNodes = 2, maxNodes = 3,
			notableIndicies = { 1 }, socketIndicies = { 2 }, totalIndicies = 6,
			skills = {
				affliction_maximum_life = {
					name = "Life", tag = "affliction_maximum_life",
					stats = { "5% increased maximum Life" },
					enchant = { "Added Small Passive Skills grant: 5% increased maximum Life" },
				},
			},
		},
		["Medium Cluster Jewel"] = {
			size = "Medium", minNodes = 4, maxNodes = 6,
			notableIndicies = { 1, 2, 3, 4 }, socketIndicies = { 5 },
			skills = { affliction_x = { name = "X", tag = "affliction_x", stats = { "x" }, enchant = { "x" } } },
		},
		["Large Cluster Jewel"] = {
			size = "Large", minNodes = 8, maxNodes = 12,
			notableIndicies = { 1, 2, 3, 4, 5 }, socketIndicies = { 6, 7, 8 },
			skills = { affliction_y = { name = "Y", tag = "affliction_y", stats = { "y" }, enchant = { "y" } } },
		},
	},
	notableSortOrder = {
		["Prodigious Defence"] = 11281,
		["Hollow Palm Technique"] = 11999,
	},
	keystones = { "Hollow Palm Technique", "Pitfighter" },
}
"""


def pob_root(body: str = CLUSTER_LUA) -> Path:
    root = Path(mkdtemp())
    target = root / "src" / "Data" / "ClusterJewels.lua"
    target.parent.mkdir(parents=True)
    target.write_text(body, encoding="utf-8")
    return root


class ExtractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data = extract_cluster_data(pob_root())

    def test_三个尺寸与小天赋数量(self) -> None:
        by_size = {s.size: s for s in self.data.sizes}
        self.assertEqual((2, 3, 1, 1),
                         (by_size["Small"].min_nodes, by_size["Small"].max_nodes,
                          by_size["Small"].notable_slots, by_size["Small"].socket_slots))
        self.assertEqual((8, 12, 5, 3),
                         (by_size["Large"].min_nodes, by_size["Large"].max_nodes,
                          by_size["Large"].notable_slots, by_size["Large"].socket_slots))

    def test_小天赋类型带原文(self) -> None:
        small = [s for s in self.data.skills if s.size == "Small"]
        self.assertEqual("Life", small[0].name_en)
        self.assertEqual(("5% increased maximum Life",), small[0].stats_en)

    def test_基石标出来且补齐不在排序表里的(self) -> None:
        """`Pitfighter` 只在 keystones 里、不在 notableSortOrder 里。
        漏掉它就答不了「哪些基石只能靠星团珠宝拿」。"""
        keystones = {n.name_en for n in self.data.notables if n.is_keystone}
        self.assertEqual({"Hollow Palm Technique", "Pitfighter"}, keystones)
        self.assertIn("Prodigious Defence", {n.name_en for n in self.data.notables})

    def test_尺寸不足三个即拒绝(self) -> None:
        """上游少一个尺寸会让「能加几个小点」少一档，那种错要在入库时暴露。"""
        body = CLUSTER_LUA.replace('["Medium Cluster Jewel"]', '["Unknown Jewel"]')
        with self.assertRaises(ClusterStructureError):
            extract_cluster_data(pob_root(body))

    def test_缺清单即拒绝(self) -> None:
        body = CLUSTER_LUA.replace("notableSortOrder", "somethingElse")
        with self.assertRaises(ClusterStructureError):
            extract_cluster_data(pob_root(body))


class BridgeTest(unittest.TestCase):
    """中文名从**已入库的天赋卡**桥接，桥不上留空。"""

    def test_按英文名取中文名与实体id(self) -> None:
        data = extract_cluster_data(pob_root())
        bridged = bridge_chinese(data.notables, {"Prodigious Defence": ("恢宏壁垒", "passive:x")})
        by_en = {n.name_en: n for n in bridged}
        self.assertEqual(("恢宏壁垒", "passive:x"),
                         (by_en["Prodigious Defence"].name_zh, by_en["Prodigious Defence"].entity_id))

    def test_桥不上时留空而不是硬翻(self) -> None:
        data = extract_cluster_data(pob_root())
        bridged = bridge_chinese(data.notables, {})
        self.assertTrue(all(n.name_zh is None for n in bridged))


def _tool_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    commit = "a" * 40
    url = "https://github.com/x/blob/abc/src/Data/ClusterJewels.lua"
    conn.executemany(
        "INSERT INTO pob_cluster_jewel(size,min_nodes,max_nodes,notable_slots,socket_slots,"
        "source,source_priority,pob_url,source_commit,updated_at) "
        "VALUES (?,?,?,?,?,'Path of Building',2,?,?,datetime('now'))",
        [("Small", 2, 3, 1, 1, url, commit), ("Medium", 4, 6, 4, 1, url, commit),
         ("Large", 8, 12, 5, 3, url, commit)],
    )
    conn.executemany(
        "INSERT INTO pob_cluster_skill(size,tag,name_en,stats_en,enchant_en) VALUES (?,?,?,?,?)",
        [("Large", "affliction_axe", "Axe and Sword Damage",
          '["Axe Attacks deal 12% increased Damage"]', '["Added Small Passive Skills grant: x"]')],
    )
    conn.executemany(
        "INSERT INTO pob_cluster_notable(name_en,name_zh,entity_id,is_keystone,sort_order,"
        "source,source_priority,pob_url,source_commit,updated_at) "
        "VALUES (?,?,?,?,?,'Path of Building',2,?,?,datetime('now'))",
        [("Hollow Palm Technique", "空明之掌", "passive:HPT", 1, 11999, url, commit),
         ("Prodigious Defence", "恢宏壁垒", "passive:PD", 0, 11281, url, commit)],
    )
    conn.commit()
    return conn


class ClusterToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _tool_conn()
        self.patch = patch("tools.impl.cluster.dbmod.get_conn", return_value=self.conn)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_按尺寸给小天赋数与类型(self) -> None:
        result = cluster_jewel_info("大型星团珠宝")
        self.assertEqual(("size", 8, 12), (result.data["kind"], result.data["min_nodes"],
                                           result.data["max_nodes"]))
        self.assertIn("8–12 个小天赋", result.citations[0]["text"])

    def test_基石清单(self) -> None:
        result = cluster_jewel_info("星团珠宝基石有哪些")
        self.assertEqual("keystones", result.data["kind"])
        self.assertIn("空明之掌", result.citations[0]["text"])

    def test_具体核心天赋说清不在主树上(self) -> None:
        """这是把 `passive_path` 那句「可能」变成事实的那条路。"""
        result = cluster_jewel_info("恢宏壁垒")
        self.assertEqual("notable", result.data["kind"])
        self.assertFalse(result.data["on_main_tree"])
        self.assertFalse(result.data["is_keystone"])
        self.assertIn("不在主天赋树上", result.citations[0]["text"])

    def test_边界写明映射未采集(self) -> None:
        """308 条清单在手上，最容易犯的错是拿一部分去凑一个「看起来对」的答案。"""
        for query in ("大型星团珠宝", "恢宏壁垒", "星团珠宝"):
            with self.subTest(query=query):
                self.assertIn("哪种小天赋类型上未采集",
                              cluster_jewel_info(query).data["boundary"])

    def test_类型名标明是英文原文(self) -> None:
        text = cluster_jewel_info("大型星团珠宝").citations[0]["text"]
        self.assertIn("只有英文", text)

    def test_查不到名字时给总览(self) -> None:
        self.assertEqual("overview", cluster_jewel_info("星团珠宝怎么用").data["kind"])

    def test_空查询直接失败(self) -> None:
        self.assertFalse(cluster_jewel_info("  ").ok)

    def test_供passive_path复用的查询函数(self) -> None:
        row = cluster_notable_row(self.conn, "空明之掌")
        self.assertIsNotNone(row)
        self.assertEqual(1, row["is_keystone"])
        self.assertIsNone(cluster_notable_row(self.conn, "神威之盾"))


class ClusterRouteTest(unittest.TestCase):
    def test_星团问法路由到工具(self) -> None:
        from orchestrator.loop import _preferred_tool_route

        self.assertEqual("cluster_jewel_info", _preferred_tool_route("大型星团珠宝能加几个小天赋"))

    def test_珠宝词缀问法不被抢走(self) -> None:
        """单独的「珠宝」不该抢路由——「珠宝上的词缀」有卡片可答。"""
        from orchestrator.loop import _preferred_tool_route

        self.assertNotEqual("cluster_jewel_info", _preferred_tool_route("珠宝上有哪些词缀"))


if __name__ == "__main__":
    unittest.main()
