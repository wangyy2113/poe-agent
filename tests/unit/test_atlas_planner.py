"""Atlas 原始拓扑与有来源候选规划的边界。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.deps import require_user
from core import db as dbmod
from kb import pipeline
from kb.sources import poedb_tree
from main import create_app
from planner.atlas import plan_atlas, plan_atlas_route
from tools.impl.atlas import atlas_candidates, atlas_route


def payload(version: str = "3.29") -> dict:
    nodes = {"root": {"name": "", "out": ["1"], "in": []}}
    for index in range(1, 600):
        nodes[str(index)] = {
            "name": "精华核心" if index == 10 else f"节点{index}",
            "stats": ["地图内有额外精华"] if index == 10 else ["测试文本"],
            "out": [str(index + 1)] if index < 599 else [],
            "in": [str(index - 1)] if index > 1 else ["root"],
            "skill": index,
        }
    return {"nodes": nodes, "points": {"totalPoints": 138}}


class AtlasPlannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(dbmod.SCHEMA)
        self.conn.executescript(dbmod.FTS_SCHEMA)
        self.ep = poedb_tree.TreeEndpoint(poedb_tree.TARGET_ATLAS, "3.29", "cn")

    def tearDown(self) -> None:
        self.conn.close()

    def _insert_target(self, version: str = "3.29", *, node_id: int = 10) -> None:
        attrs = {"node_kind": "notable", "skill_ids": [node_id],
                 "data_url": self.ep.json_url,
                 "mods": [{"kind": "passive", "text": "地图内有额外精华"}]}
        self.conn.execute(
            "INSERT INTO entity(entity_id,entity_type,name_zh,name_en,attributes,"
            "source,source_priority,url,version,content_hash,fetched_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (f"atlas_passive:test:{node_id}", "atlas_passive", "精华核心", "Essence Notable",
             json.dumps(attrs, ensure_ascii=False), "POEDB", 1, self.ep.page_url,
             version, "hash", "2026-01-01"),
        )
        self.conn.commit()

    def test_extract_publish_and_plan_source_path(self) -> None:
        graph = poedb_tree.extract_atlas_graph(payload(), None, self.ep)
        self.assertEqual(600, len(graph.nodes))
        self.assertEqual(599, len(graph.edges))
        self.assertEqual(138, graph.meta["totalPoints"])
        pipeline._replace_atlas_graph(self.conn, graph, self.ep, "3.29")
        self._insert_target()
        plan = plan_atlas(self.conn, goal="特定材料", focus="精华")
        self.assertEqual("candidates", plan.status)
        self.assertEqual("精华核心", plan.candidates[0].name_zh)
        self.assertEqual(["root", *map(str, range(1, 11))],
                         [node.node_id for node in plan.candidates[0].path])
        self.assertEqual(self.ep.page_url, plan.candidates[0].source_url)
        self.assertIn("精华", plan.candidates[0].matched_lines[0])
        self.assertIn("未验证", plan.missing[0])
        self.assertEqual(10, plan.candidates[0].path_allocatable_nodes)

    def test_multi_target_route_shares_nodes_without_claiming_points(self) -> None:
        graph = poedb_tree.extract_atlas_graph(payload(), None, self.ep)
        pipeline._replace_atlas_graph(self.conn, graph, self.ep, "3.29")
        self._insert_target(node_id=10)
        self._insert_target(node_id=15)
        result = plan_atlas_route(
            self.conn, entity_ids=["atlas_passive:test:15", "atlas_passive:test:10"]
        )
        self.assertEqual("connected", result["status"])
        self.assertEqual(15, result["new_node_count"])
        self.assertEqual([10, 5], [route["new_node_count"] for route in result["routes"]])
        self.assertEqual(["atlas_passive:test:10", "atlas_passive:test:15"],
                         [route["entity_id"] for route in result["routes"]])
        self.assertIn("不是经游戏规则核验的 Atlas 点数", result["boundary"])

    def test_route_rejects_disconnected_allocation_and_version_mismatch(self) -> None:
        graph = poedb_tree.extract_atlas_graph(payload(), None, self.ep)
        pipeline._replace_atlas_graph(self.conn, graph, self.ep, "3.29")
        self._insert_target(version="3.28")
        result = plan_atlas_route(self.conn, entity_ids=["atlas_passive:test:10"])
        self.assertEqual("unknown", result["status"])
        self.assertEqual([], result["routes"])
        self.assertIn("版本不一致", result["unresolved"][0]["reason"])
        result = plan_atlas_route(
            self.conn, entity_ids=["atlas_passive:test:10"],
            allocated_node_ids=["10"],
        )
        self.assertEqual("unknown", result["status"])
        with self.assertRaises(ValueError):
            plan_atlas_route(
                self.conn, entity_ids=["atlas_passive:test:10"],
                allocated_node_ids=["unknown"],
            )

    def test_tools_keep_effect_and_graph_citations_separate_from_point_claim(self) -> None:
        graph = poedb_tree.extract_atlas_graph(payload(), None, self.ep)
        pipeline._replace_atlas_graph(self.conn, graph, self.ep, "3.29")
        self._insert_target()
        with patch.object(dbmod, "get_conn", return_value=self.conn):
            candidate = atlas_candidates("特定材料", "精华")
            route = atlas_route(["atlas_passive:test:10"])
        self.assertTrue(candidate.ok)
        self.assertEqual(1, len(candidate.citations))
        self.assertEqual(self.ep.json_url, candidate.citations[0]["url"])
        self.assertIn("地图内有额外精华", candidate.citations[0]["text"])
        self.assertTrue(route.ok)
        self.assertEqual(1, len(route.citations))
        self.assertIn("新增图节点 10 个", route.citations[0]["text"])
        self.assertIn("不是经游戏规则核验", route.data["boundary"])

    def test_version_mismatch_and_missing_graph_do_not_claim_path(self) -> None:
        self._insert_target(version="3.28")
        self.assertEqual("partial", plan_atlas(
            self.conn, goal="特定材料", focus="精华").status)
        graph = poedb_tree.extract_atlas_graph(payload(), None, self.ep)
        pipeline._replace_atlas_graph(self.conn, graph, self.ep, "3.29")
        result = plan_atlas(self.conn, goal="特定材料", focus="精华")
        self.assertEqual("partial", result.status)
        self.assertEqual((), result.candidates[0].path)

    def test_missing_evidence_and_bad_graph_fail_closed(self) -> None:
        self.assertEqual("unknown", plan_atlas(
            self.conn, goal="刷图速度").status)
        with self.assertRaises(poedb_tree.TreeStructureError):
            poedb_tree.extract_atlas_graph({"nodes": {}}, None, self.ep)
        wrong = payload()
        del wrong["nodes"]["root"]
        with self.assertRaises(poedb_tree.TreeStructureError):
            poedb_tree.extract_atlas_graph(wrong, None, self.ep)
        with self.assertRaises(ValueError):
            plan_atlas(self.conn, goal="特定材料", focus="")

    def test_api_does_not_bypass_user_auth(self) -> None:
        self._insert_target()
        app = create_app()
        with patch.object(dbmod, "get_conn", return_value=self.conn), patch(
            "api.deps.user_keys", return_value=["atlas-key"]
        ):
            with TestClient(app) as client:
                self.assertEqual(401, client.post(
                    "/poe_agent/planner/atlas", json={"goal": "特定材料", "focus": "精华"}
                ).status_code)
            app.dependency_overrides[require_user] = lambda: "test"
            with TestClient(app) as client:
                response = client.post(
                    "/poe_agent/planner/atlas", json={"goal": "特定材料", "focus": "精华"}
                )
                self.assertEqual(200, response.status_code)
                self.assertEqual("partial", response.json()["status"])
                response = client.post(
                    "/poe_agent/planner/atlas-route",
                    json={"entity_ids": ["atlas_passive:test:10"]},
                )
                self.assertEqual(200, response.status_code)
                self.assertEqual("unknown", response.json()["status"])


if __name__ == "__main__":
    unittest.main()
