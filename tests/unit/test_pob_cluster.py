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

from core.db import SCHEMA, _migrate_cluster_skill_weight_tag  # noqa: E402
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

    def test_表键与权重标签分开保留(self) -> None:
        body = CLUSTER_LUA.replace(
            'tag = "affliction_maximum_life"', 'tag = "shared_weight_tag"', 1
        )
        skill = next(s for s in extract_cluster_data(pob_root(body)).skills if s.name_en == "Life")
        self.assertEqual("affliction_maximum_life", skill.skill_id)
        self.assertEqual("shared_weight_tag", skill.weight_tag)

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


class MigrationTest(unittest.TestCase):
    def test_旧表补真实权重标签且幂等(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE pob_cluster_skill(size TEXT,tag TEXT,name_en TEXT,"
            "stats_en TEXT,enchant_en TEXT,PRIMARY KEY(size,tag))"
        )
        conn.executemany(
            "INSERT INTO pob_cluster_skill VALUES (?,?,?,?,?)",
            [
                ("Small", "affliction_chance_to_block_attack_damage", "Attack Block", "[]", "[]"),
                ("Large", "affliction_axe_and_sword_damage", "Axe and Sword", "[]", "[]"),
            ],
        )
        _migrate_cluster_skill_weight_tag(conn)
        _migrate_cluster_skill_weight_tag(conn)
        rows = dict(conn.execute("SELECT tag,weight_tag FROM pob_cluster_skill"))
        self.assertEqual("affliction_chance_to_block", rows["affliction_chance_to_block_attack_damage"])
        self.assertEqual("affliction_axe_and_sword_damage", rows["affliction_axe_and_sword_damage"])
        conn.close()


def _tool_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    commit = "a" * 40
    url = f"https://github.com/x/blob/{commit}/src/Data/ClusterJewels.lua"
    mod_url = f"https://github.com/x/blob/{commit}/src/Data/ModJewelCluster.lua"
    conn.executemany(
        "INSERT INTO pob_cluster_jewel(size,min_nodes,max_nodes,notable_slots,socket_slots,"
        "source,source_priority,pob_url,source_commit,updated_at) "
        "VALUES (?,?,?,?,?,'Path of Building',2,?,?,datetime('now'))",
        [("Small", 2, 3, 1, 1, url, commit), ("Medium", 4, 6, 4, 1, url, commit),
         ("Large", 8, 12, 5, 3, url, commit)],
    )
    conn.executemany(
        "INSERT INTO pob_cluster_skill(size,tag,weight_tag,name_en,stats_en,enchant_en) "
        "VALUES (?,?,?,?,?,?)",
        [("Large", "affliction_axe", "affliction_axe", "Axe and Sword Damage",
          '["Axe Attacks deal 12% increased Damage"]', '["Added Small Passive Skills grant: x"]')],
    )
    conn.executemany(
        "INSERT INTO pob_cluster_notable(name_en,name_zh,entity_id,is_keystone,sort_order,"
        "source,source_priority,pob_url,source_commit,updated_at) "
        "VALUES (?,?,?,?,?,'Path of Building',2,?,?,datetime('now'))",
        [("Hollow Palm Technique", "空明之掌", "passive:HPT", 1, 11999, url, commit),
         ("Prodigious Defence", "恢宏壁垒", "passive:PD", 0, 11281, url, commit),
         ("Martial Prowess", "武艺精通", "passive:MP", 0, 11282, url, commit)],
    )
    conn.execute(
        "INSERT INTO entity(entity_id,entity_type,name_zh,name_en,attributes,source,"
        "source_priority,url,content_hash,fetched_at,updated_at) "
        "VALUES ('passive:AXE','passive','斧剑伤害','Axe and Sword Damage','{}',"
        "'POEDB',1,'https://poedb.tw/cn/passive-skill-tree/','hash',"
        "datetime('now'),datetime('now'))"
    )
    conn.executemany(
        "INSERT INTO pob_mod(uid,mod_id,mod_kind,source_file,affix_type,affix_name,ilvl,"
        "text_en,weights,weight_mults,source,source_priority,url,updated_at) "
        "VALUES (?,?, 'jewel_cluster','Data/ModJewelCluster.lua',?,?,?,?,?,?,"
        "'Path of Building',2,?,datetime('now'))",
        [
            ("jewel_cluster:PD", "PD", "Prefix", "Notable", 1,
             "1 Added Passive Skill is Prodigious Defence", '{"affliction_axe":600}',
             '{"has_affliction_notable":0}', mod_url),
            # 后缀的 affix_name 不是 Notable；识别必须看文本，不能漏掉整类后缀。
            ("jewel_cluster:MP", "MP", "Suffix", "of Significance", 68,
             "1 Added Passive Skill is Martial Prowess", '{"affliction_axe":300}',
             '{"has_affliction_notable2":0}', mod_url),
        ],
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

    def test_边界不把基础权重冒充概率(self) -> None:
        for query in ("大型星团珠宝", "恢宏壁垒", "星团珠宝"):
            with self.subTest(query=query):
                self.assertIn("不是单次概率", cluster_jewel_info(query).data["boundary"])

    def test_类型中文名来自天赋树桥接(self) -> None:
        text = cluster_jewel_info("大型星团珠宝").citations[0]["text"]
        self.assertIn("斧剑伤害", text)
        self.assertIn("小天赋效果文本仍按 PoB 英文原文", text)

    def test_按中文小天赋类型列核心天赋池且不漏后缀(self) -> None:
        result = cluster_jewel_info("大星团珠宝的斧剑伤害能开出哪些核心天赋")
        self.assertEqual("notable_pool", result.data["kind"])
        self.assertEqual(2, result.data["count"])
        self.assertEqual(
            {"恢宏壁垒", "武艺精通"},
            {row["name_zh"] for row in result.data["notables"]},
        )
        self.assertIn("后缀", result.data["answer"])
        self.assertEqual(2, len(result.citations))

    def test_核心天赋池按问题里的物等过滤(self) -> None:
        result = cluster_jewel_info("物等50的大星团斧剑伤害能出什么")
        self.assertEqual(1, result.data["count"])
        self.assertEqual(1, result.data["excluded_by_item_level"])
        self.assertEqual("恢宏壁垒", result.data["notables"][0]["name_zh"])

    def test_核心天赋反查可出现的小天赋类型(self) -> None:
        result = cluster_jewel_info("恢宏壁垒能在哪些星团类型出现")
        self.assertEqual("notable", result.data["kind"])
        self.assertEqual("affliction_axe", result.data["skill_types"][0]["tag"])
        self.assertIn("斧剑伤害", result.data["answer"])

    def test_没有正权重映射的基石不硬塞进随机池(self) -> None:
        result = cluster_jewel_info("空明之掌")
        self.assertEqual([], result.data["skill_types"])
        self.assertIn("没有为它记录任何正权重", result.data["answer"])

    def test_物等参数校验(self) -> None:
        self.assertFalse(cluster_jewel_info("斧剑伤害", item_level=0).ok)
        self.assertFalse(cluster_jewel_info("物等999的斧剑伤害").ok)

    def test_两个PoB来源版本不一致时拒绝拼接(self) -> None:
        other = "b" * 40
        self.conn.execute(
            "UPDATE pob_mod SET url=replace(url, ?, ?)", ("a" * 40, other)
        )
        result = cluster_jewel_info("大星团斧剑伤害能出什么")
        self.assertFalse(result.data["found"])
        self.assertEqual("source_version_mismatch", result.data["kind"])
        self.conn.execute("DELETE FROM pob_mod")
        missing = cluster_jewel_info("大星团斧剑伤害能出什么")
        self.assertFalse(missing.data["found"])
        self.assertEqual("mapping_data_missing", missing.data["kind"])

    def test_查不到名字时给总览(self) -> None:
        self.assertEqual("overview", cluster_jewel_info("星团珠宝怎么用").data["kind"])
        self.assertEqual("overview", cluster_jewel_info("星团珠宝有哪些小天赋类型").data["kind"])

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
