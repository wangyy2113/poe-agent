"""需求 8.9：Build 与本地数据版本不一致时明确提示。"""

from __future__ import annotations

import sqlite3
import unittest

from parsers.build_version import check_build_version
from parsers.pob_build import PobBuild, parse_build


def _conn(*, tree_version: str = "", game_version: str = "") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE passive_graph_node(tree_version TEXT)")
    conn.execute("CREATE TABLE game_constant(game_version TEXT)")
    if tree_version:
        conn.executemany(
            "INSERT INTO passive_graph_node VALUES (?)",
            [(tree_version,), (tree_version,)],
        )
    if game_version:
        conn.execute("INSERT INTO game_constant VALUES (?)", (game_version,))
    return conn


class BuildVersionTest(unittest.TestCase):
    def test_读取当前激活天赋方案而不是第一套(self) -> None:
        build = parse_build(
            "<PathOfBuilding><Build level='90'/><Tree activeSpec='2'>"
            "<Spec treeVersion='3_20' nodes='1'/><Spec treeVersion='3_29' nodes='2,3'/>"
            "</Tree></PathOfBuilding>"
        )
        self.assertEqual(build.tree_version, "3.29")
        self.assertEqual(build.passive_nodes, ("2", "3"))

    def test_同版明确通过(self) -> None:
        conn = _conn(tree_version="3.29")
        try:
            result = check_build_version(conn, PobBuild(tree_version="3_29"))
        finally:
            conn.close()
        self.assertTrue(result.known)
        self.assertTrue(result.matches)
        self.assertIn("一致", result.message)

    def test_跨版明确提示影响边界(self) -> None:
        conn = _conn(tree_version="3.29")
        try:
            result = check_build_version(conn, PobBuild(tree_version="3.13"))
        finally:
            conn.close()
        self.assertTrue(result.known)
        self.assertFalse(result.matches)
        self.assertIn("3.13", result.message)
        self.assertIn("3.29", result.message)
        self.assertIn("PlayerStat 仍是原 PoB 的快照", result.message)
        self.assertIn("可能存在版本偏差", result.message)

    def test_本地天赋图缺失时回退常量版本(self) -> None:
        conn = _conn(game_version="3.29")
        try:
            result = check_build_version(conn, PobBuild(tree_version="3.29"))
        finally:
            conn.close()
        self.assertTrue(result.matches)
        self.assertIn("game_constant.game_version", result.basis)

    def test_任一侧版本缺失都不猜(self) -> None:
        conn = _conn(tree_version="3.29")
        try:
            no_build = check_build_version(conn, PobBuild())
        finally:
            conn.close()
        self.assertFalse(no_build.known)
        self.assertIsNone(no_build.matches)
        self.assertIn("无法判断", no_build.message)

        conn = _conn()
        try:
            no_system = check_build_version(conn, PobBuild(tree_version="3.29"))
        finally:
            conn.close()
        self.assertFalse(no_system.known)
        self.assertIsNone(no_system.matches)
        self.assertIn("无法与 Build 版本比对", no_system.message)


if __name__ == "__main__":
    unittest.main()
