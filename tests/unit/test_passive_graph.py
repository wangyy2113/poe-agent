"""天赋树的图结构与 `passive_path`（2026-08-19 第十五轮）。

判据几乎每条都来自实测反例：
不排除 root 会把「野蛮人→女巫」算成 2 点（真值 22）、职业名要取自顶层 `classes`
（起点节点自己的名字是「七」「六」这种占位）、同名小点要取最近的一处、
逐位置各跑一次 BFS 会让「力量」慢到 327ms。

全部离线：图在内存里手搓，不读缓存也不连真实库。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from kb.sources.poedb_tree import (  # noqa: E402
    LANG_ZH,
    ROOT_ID,
    TARGET_PASSIVE,
    TreeEndpoint,
    TreeStructureError,
    build_adjacency,
    extract_graph,
    nearest_path,
    shortest_path,
)
from tools.impl.passives import passive_path  # noqa: E402

EP = TreeEndpoint(TARGET_PASSIVE, "3.29", LANG_ZH)


def payload(nodes: dict, classes: list | None = None, points: dict | None = None) -> dict:
    return {
        "nodes": nodes,
        "classes": classes if classes is not None else [{"name": f"职业{i}"} for i in range(7)],
        "points": points if points is not None else {"totalPoints": 123, "ascendancyPoints": 8},
    }


def tiny_tree() -> dict:
    """一棵最小的树。形状（root 只连起点，游戏里不能穿过）：

        职业0 - 力量(a1) - 核心甲(a2) - 核心乙(b2) - 力量(b1) - 职业1
    """
    nodes = {
        ROOT_ID: {"out": ["s1", "s2"]},
        "s1": {"name": "七", "classStartIndex": 0, "out": ["a1"]},
        "s2": {"name": "六", "classStartIndex": 1, "out": ["b1"]},
        "a1": {"name": "力量", "out": ["a2"]},
        "a2": {"name": "核心甲", "isNotable": True, "out": ["b2"]},
        "b2": {"name": "核心乙", "isNotable": True, "out": ["b1"]},
        "b1": {"name": "力量", "out": []},
        "m1": {"name": "大师天赋", "isMastery": True, "out": ["a2"]},
        "asc1": {"name": "升华节点", "ascendancyName": "Juggernaut", "out": ["a2"]},
        "j1": {"name": "珠宝插槽", "isJewelSocket": True, "out": ["a1"]},
    }
    # 7 个职业起点是硬断言，补齐剩下 5 个（各自孤立）
    for i in range(2, 7):
        nodes[f"s{i + 1}"] = {"name": f"起点{i}", "classStartIndex": i, "out": []}
    return nodes


class ExtractGraphTest(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = extract_graph(payload(tiny_tree()), None, EP)

    def test_root不可经过(self) -> None:
        """7 个职业起点都连在 root 上，但游戏里不能从一个职业穿到另一个职业。
        不排除它，实测「野蛮人 → 女巫」会算成 2 点，真值 22。"""
        self.assertNotIn(ROOT_ID, self.graph.passable_ids)

    def test_升华与大师不可经过而珠宝插槽可以(self) -> None:
        passable = self.graph.passable_ids
        self.assertNotIn("asc1", passable)
        self.assertNotIn("m1", passable)
        self.assertIn("j1", passable)

    def test_职业名取自顶层classes(self) -> None:
        """起点节点自己的名字是「七」「六」这类占位名，照搬出去用户看不懂。"""
        starts = self.graph.class_starts
        self.assertEqual("s1", starts["职业0"])
        self.assertNotIn("七", starts)

    def test_边规范化只存一次(self) -> None:
        """JSON 的 in/out 两侧都列了同一条边，不规范化会把连线数算成两倍。"""
        self.assertIn(("a1", "a2"), self.graph.edges)
        self.assertNotIn(("a2", "a1"), self.graph.edges)

    def test_总点数入库(self) -> None:
        self.assertEqual({"totalPoints": 123.0, "ascendancyPoints": 8.0}, self.graph.meta)

    def test_起点数不足七个即拒绝(self) -> None:
        """`classStartIndex` 改名会让路径全算错，那种错必须在入库时暴露。"""
        nodes = tiny_tree()
        for key in list(nodes):
            nodes[key].pop("classStartIndex", None)
        with self.assertRaises(TreeStructureError) as ctx:
            extract_graph(payload(nodes), None, EP)
        self.assertIn("职业起点", str(ctx.exception))

    def test_没有连线即拒绝(self) -> None:
        nodes = {k: {kk: vv for kk, vv in v.items() if kk not in ("in", "out")}
                 for k, v in tiny_tree().items()}
        with self.assertRaises(TreeStructureError):
            extract_graph(payload(nodes), None, EP)


class PathTest(unittest.TestCase):
    def setUp(self) -> None:
        graph = extract_graph(payload(tiny_tree()), None, EP)
        self.edges, self.passable = graph.edges, graph.passable_ids
        self.adj = build_adjacency(self.edges, self.passable)

    def test_不能穿过树心(self) -> None:
        """两个起点之间必须绕树走，而不是经 root 两步到达。"""
        path = shortest_path(self.edges, self.passable, "s1", "s2")
        self.assertEqual(["s1", "a1", "a2", "b2", "b1", "s2"], path)
        self.assertEqual(5, len(path) - 1)

    def test_路径不穿过升华与大师节点(self) -> None:
        for node in ("asc1", "m1"):
            with self.subTest(node=node):
                self.assertIsNone(shortest_path(self.edges, self.passable, "s1", node))

    def test_一次BFS取最近的同名位置(self) -> None:
        """「力量」在这棵树上有两处（a1 与 b1），从 s1 出发最近的是 a1。

        逐位置各跑一次也能得到同样答案，但实测真实树上「力量」有 73 处、
        7 个职业 × 73 次让单次查询要 327ms；BFS 按距离扩展，第一个命中的就是最近的。
        """
        self.assertEqual(["s1", "a1"], nearest_path(self.adj, "s1", {"a1", "b1"}))

    def test_起点即目标时点数为零(self) -> None:
        self.assertEqual(["s1"], nearest_path(self.adj, "s1", {"s1"}))

    def test_孤立起点到不了(self) -> None:
        self.assertIsNone(nearest_path(self.adj, "s3", {"a2"}))


def _tool_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    graph = extract_graph(payload(tiny_tree()), None, EP)
    conn.executemany(
        "INSERT INTO passive_graph_node(node_id,name_zh,name_en,kind,ascendancy_id,"
        "class_start,passable,tree_version,source,source_priority,url,updated_at) "
        "VALUES (?,?,?,?,?,?,?,'3.29','POEDB',1,'https://poedb.tw/data/x.json',datetime('now'))",
        [(n.node_id, n.name_zh, n.name_en, n.kind, n.ascendancy_id, n.class_start,
          int(n.passable)) for n in graph.nodes],
    )
    conn.executemany(
        "INSERT INTO passive_graph_edge(node_a,node_b,tree_version) VALUES (?,?,'3.29')",
        sorted(graph.edges),
    )
    conn.executemany(
        "INSERT INTO passive_tree_meta(key,value_num,tree_version,source,source_priority,"
        "url,updated_at) VALUES (?,?,'3.29','POEDB',1,'https://poedb.tw/data/x.json',"
        "datetime('now'))",
        sorted(graph.meta.items()),
    )
    conn.commit()
    return conn


class PassivePathToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _tool_conn()
        self.patch = patch("tools.impl.passives.dbmod.get_conn", return_value=self.conn)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_按职业分别给点数(self) -> None:
        """同一个节点从不同职业差别很大（真实树上「神威之盾」11 vs 25），
        给一个笼统的数字就是错答案。"""
        result = passive_path("核心甲")
        self.assertTrue(result.data["found"])
        costs = result.data["cost_by_class"]
        self.assertEqual(2, costs["职业0"])
        self.assertEqual(3, costs["职业1"])
        self.assertEqual({"class": "职业0", "points": 2}, result.data["cheapest"])

    def test_只看某个职业(self) -> None:
        result = passive_path("核心甲", class_name="职业1")
        self.assertEqual({"职业1": 3}, result.data["cost_by_class"])

    def test_路径逐节点列出且起点显示职业名(self) -> None:
        text = passive_path("核心甲").citations[0]["text"]
        self.assertIn("职业0 → 力量 → 核心甲", text)
        self.assertNotIn("七 →", text)  # 占位名不能出现

    def test_同名多处写出处数并取最近(self) -> None:
        result = passive_path("力量")
        self.assertEqual(2, result.data["same_name_positions"])
        self.assertIn("同名节点有 2 处", result.citations[0]["text"])
        self.assertEqual(1, result.data["cost_by_class"]["职业0"])

    def test_总点数作为上下文给出(self) -> None:
        self.assertIn("共 123 个天赋点", passive_path("核心甲").citations[0]["text"])

    def test_升华与大师节点说清为什么算不了(self) -> None:
        """不是「查不到」——数据在库里，是这类节点本来就不能用「从起点走过去」描述。"""
        for name, needle in (("升华节点", "升华起点"), ("大师天赋", "选一个")):
            with self.subTest(name=name):
                result = passive_path(name)
                self.assertFalse(result.data["found"])
                self.assertIn(needle, result.data["reason"])
                self.assertEqual([], result.citations)

    def test_树上没有的名字给出换词提示(self) -> None:
        result = passive_path("不存在的节点")
        self.assertFalse(result.data["found"])
        self.assertIn("星团珠宝", result.data["hint"])

    def test_返回里带边界声明(self) -> None:
        from orchestrator.loop import _NOTE_FIELDS

        self.assertIn("不含升华点", passive_path("核心甲").data["boundary"])
        self.assertIn("boundary", _NOTE_FIELDS)

    def test_空名直接失败(self) -> None:
        self.assertFalse(passive_path("  ").ok)


class PassivePathRouteTest(unittest.TestCase):
    """点数在图上算，证据不在任何 chunk 里，所以必须靠意图词路由到达。"""

    def test_点数问法路由到工具(self) -> None:
        from orchestrator.loop import _preferred_tool_route

        for q in ("从起点点到神威之盾要花几个天赋点", "怎么走到坚毅之心"):
            with self.subTest(q=q):
                self.assertEqual("passive_path", _preferred_tool_route(q))

    def test_属性问法不被抢走(self) -> None:
        """「某天赋给什么属性」有卡片可答，不该被点数工具抢路由。"""
        from orchestrator.loop import _preferred_tool_route

        self.assertNotEqual("passive_path", _preferred_tool_route("神威之盾这个天赋给什么属性"))


if __name__ == "__main__":
    unittest.main()
