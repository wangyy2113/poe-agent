"""基础通货操作状态机：来源契约、名额、三态与 API/工具一致性。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from api.craft import CraftOperationRequest, operation_state
from core.db import SCHEMA
from craft.operations import ItemState, OPERATION_SPECS, evaluate_operation
from orchestrator import loop
from tools.impl.craft_operation import craft_operation_state
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for spec in OPERATION_SPECS:
        conn.execute(
            "INSERT INTO poedb_currency(metadata_id,name_zh,name_en,poedb_slug,item_class,"
            "stack_max,effect_zh,source,source_priority,url,content_hash,fetched_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                spec.metadata_id, spec.name_zh, spec.name_en, spec.name_en.replace(" ", "_"),
                "StackableCurrency", 20, spec.expected_effect, "POEDB", 1,
                f"https://poedb.tw/cn/{spec.name_en.replace(' ', '_')}", "hash",
                "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z",
            ),
        )
    for rarity, prefix, suffix in (("魔法", 1, 1), ("稀有", 3, 3)):
        conn.execute(
            "INSERT INTO craft_affix_capacity(rarity,item_kind,prefix_limit,suffix_limit,"
            "agreement,poedb_text,pob_text,source,source_priority,url,pob_url,source_commit,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rarity, "default", prefix, suffix, "both", f"{rarity}名额规则", "PoB rule",
                "POEDB", 1, "https://poedb.tw/cn/Modifiers", None, None,
                "2026-09-21T00:00:00Z",
            ),
        )
    for interaction_id, operation, guard, outcome, raw in (
        ("metamod:divine-prefix", "神圣石", "前缀无法被变更", "yes", "YES"),
        ("metamod:annul-suffix", "无效石", "后缀无法被变更", "no", "NO"),
    ):
        conn.execute(
            "INSERT INTO craft_metamod_interaction(interaction_id,section,operation_text,"
            "guard_text,outcome,raw_value,note_text,row_order,column_order,source,"
            "source_priority,source_url,mirror_url,content_hash,fetched_at,version_scope,"
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                interaction_id, "工艺方式", operation, guard, outcome, raw, None, 1, 0,
                "Craft of Exile", 4, "https://www.craftofexile.com/metamods",
                "https://poedb.tw/cn/metamods", "matrix-hash",
                "2026-09-21T00:00:00Z", "unknown", "2026-09-21T00:00:00Z",
            ),
        )
    conn.commit()
    return conn


class CraftOperationStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def evaluate(self, operation: str, **state):
        result = evaluate_operation(self.conn, operation, ItemState(**state))
        self.assertIsNotNone(result)
        return result

    def test_点金石普通转稀有(self) -> None:
        result = self.evaluate("点金石", rarity="普通", prefix_count=0, suffix_count=0)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("稀有", result.after["rarity"])
        self.assertIsNone(result.after["prefix_count"])
        self.assertIn("升级后的前后缀数量", result.unknown_fields[0])

    def test_点金石对稀有物品明确不兼容(self) -> None:
        result = self.evaluate("Orb of Alchemy", rarity="稀有")
        self.assertEqual("incompatible", result.verdict)
        self.assertEqual("fail", next(c for c in result.checks if c.key == "rarity").status)

    def test_崇高石有单侧空位时只保留一个后状态(self) -> None:
        result = self.evaluate("崇高石", rarity="稀有", prefix_count=2, suffix_count=3)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(
            [{"prefix_count": 3, "suffix_count": 3}],
            result.after["possible_affix_counts"],
        )
        self.assertEqual("pass", next(c for c in result.checks if c.key == "affix_capacity").status)

    def test_崇高石三前三后明确没有空位(self) -> None:
        result = self.evaluate("崇高石", rarity="稀有", prefix_count=3, suffix_count=3)
        self.assertEqual("incompatible", result.verdict)
        self.assertIn("没有可新增", next(c for c in result.checks if c.key == "affix_capacity").summary)

    def test_增幅石缺计数保持unknown(self) -> None:
        result = self.evaluate("增幅石", rarity="魔法")
        self.assertEqual("unknown", result.verdict)
        self.assertEqual("unknown", next(c for c in result.checks if c.key == "affix_capacity").status)

    def test_机会石保留三个可能稀有度而不猜结果(self) -> None:
        result = self.evaluate("机会石", rarity="普通")
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(["魔法", "稀有", "传奇"], result.after["possible_rarities"])
        self.assertIn("随机升级后的稀有度", result.unknown_fields)

    def test_重铸石清空计数并变普通(self) -> None:
        result = self.evaluate("重铸石", rarity="稀有", prefix_count=2, suffix_count=2)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(("普通", 0, 0), (
            result.after["rarity"], result.after["prefix_count"], result.after["suffix_count"],
        ))

    def test_神圣石只改数值不改计数(self) -> None:
        result = self.evaluate("神圣石", rarity="稀有", prefix_count=2, suffix_count=1)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual((2, 1), (result.after["prefix_count"], result.after["suffix_count"]))
        self.assertEqual("unchanged", result.after["modifier_identities"])
        self.assertEqual("random_modifier_values_rerolled", result.after["modifier_values"])

    def test_剥离石列出前后缀两种可能但不挑结果(self) -> None:
        result = self.evaluate("剥离石", rarity="稀有", prefix_count=2, suffix_count=1)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual([
            {"prefix_count": 1, "suffix_count": 1},
            {"prefix_count": 2, "suffix_count": 0},
        ], result.after["possible_affix_counts"])

    def test_来源原文漂移立即停止旧语义(self) -> None:
        self.conn.execute(
            "UPDATE poedb_currency SET effect_zh='上游已变化' WHERE name_zh='混沌石'"
        )
        result = self.evaluate("混沌石", rarity="稀有")
        self.assertEqual("unknown", result.verdict)
        self.assertEqual("unchanged", result.after["modifier_identities"])
        self.assertIn("来源原文变化后的状态转移", result.unknown_fields)

    def test_特殊状态不擅自套普通语义(self) -> None:
        result = self.evaluate("混沌石", rarity="稀有", corrupted=True)
        self.assertEqual("unknown", result.verdict)
        self.assertEqual("unknown", next(c for c in result.checks if c.key == "special_state").status)

    def test_不支持的相似名称不猜(self) -> None:
        self.assertIsNone(evaluate_operation(self.conn, "超级崇高石", ItemState(rarity="稀有")))


class CraftOperationToolAndApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patch = patch("tools.impl.craft_operation.dbmod.get_conn", return_value=self.conn)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_工具结构严格且答案确定性渲染(self) -> None:
        result = craft_operation_state(
            "崇高石", rarity="稀有", prefix_count=2, suffix_count=3
        )
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertIn("3 前缀 + 3 后缀", result.data["answer"])
        self.assertIn("只表示已提供状态", result.data["boundary"])
        self.assertEqual(2, len(result.citations))
        spec = REGISTRY.spec("craft_operation_state")
        self.assertIsNotNone(spec)
        self.assertFalse(spec.parameters["additionalProperties"])

    def test_工具可直接解析普通装备文本(self) -> None:
        text = "Item Class: Amulets\nRarity: Normal\nAmber Amulet\n--------\nItem Level: 80"
        result = craft_operation_state("点金石", item_text=text)
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertEqual("普通", result.data["before"]["rarity"])

    def test_基础通货可并列返回逐字Metamod矩阵原值(self) -> None:
        result = craft_operation_state(
            "神圣石", rarity="稀有", prefix_count=2, suffix_count=1,
            metamod_guard="前缀无法被变更",
        )
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertEqual("compatible", result.data["operation_verdict"])
        self.assertEqual("recorded", result.data["metamod_interaction"]["status"])
        self.assertEqual("YES", result.data["metamod_interaction"]["raw_value"])
        self.assertEqual("unknown", result.data["metamod_interaction"]["version_scope"])
        self.assertIn("不解释 YES/NO", result.data["answer"])
        self.assertEqual(2, len(result.citations))
        self.assertNotIn("Metamod 交互", result.citations[0]["text"])
        self.assertEqual("metamod_matrix", result.citations[1]["claim_scope"])
        self.assertEqual(
            "metamod_interaction:metamod:divine-prefix",
            result.citations[-1]["chunk_id"],
        )

    def test_剥离石只通过显式桥接查询矩阵旧译名(self) -> None:
        result = craft_operation_state(
            "剥离石", rarity="稀有", prefix_count=2, suffix_count=1,
            metamod_guard="后缀无法被变更",
        )
        self.assertEqual("无效石", result.data["metamod_interaction"]["operation"])
        self.assertEqual("NO", result.data["metamod_interaction"]["raw_value"])

    def test_矩阵没有记录时不拿基础状态冒充完整结论(self) -> None:
        result = craft_operation_state(
            "机会石", rarity="普通", metamod_guard="前缀无法被变更",
        )
        self.assertTrue(result.ok)
        self.assertEqual("unknown", result.data["verdict"])
        self.assertEqual("compatible", result.data["operation_verdict"])
        self.assertEqual("not_recorded", result.data["metamod_interaction"]["status"])
        self.assertIn("明确 Metamod 矩阵结果", result.data["unknown_fields"][-1])
        self.assertEqual(1, len(result.citations))

    def test_自然元词缀问法代码直达合流工具(self) -> None:
        for question in (
            "神圣石在前缀无法被变更下的结果是什么",
            "神圣石与前缀无法被变更怎么互动",
        ):
            with self.subTest(question=question):
                self.assertEqual(
                    "craft_operation_state", loop._preferred_tool_route(question)
                )
                self.assertEqual({
                    "operation": "神圣石", "metamod_guard": "前缀无法被变更",
                }, loop._craft_operation_args(question))
        excluded = "不要用元词缀矩阵，神圣石与前缀无法被变更怎么互动"
        self.assertIsNone(loop._craft_operation_args(excluded))

    def test_API与工具共用同一结果(self) -> None:
        response = operation_state(CraftOperationRequest(
            operation="神圣石", rarity="稀有", prefix_count=2, suffix_count=1,
            metamod_guard="前缀无法被变更",
        ), _caller="test")
        self.assertEqual("compatible", response.verdict)
        self.assertEqual("YES", response.metamod_interaction["raw_value"])


if __name__ == "__main__":
    unittest.main()
