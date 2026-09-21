"""古灵通货状态机：12 条原文、名额、API、元词缀披露与聊天路由。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from api.craft import EldritchOperationRequest, eldritch_state
from core.db import SCHEMA
from craft.eldritch_operations import (
    ELDRITCH_SPECS,
    EldritchItemState,
    evaluate_eldritch_operation,
)
from orchestrator import loop
from tools.impl.eldritch_operation import eldritch_operation_state
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for index, spec in enumerate(ELDRITCH_SPECS):
        conn.execute(
            "INSERT INTO poedb_currency(metadata_id,name_zh,name_en,poedb_slug,item_class,"
            "stack_max,effect_zh,source,source_priority,url,content_hash,fetched_at,updated_at) "
            "VALUES(?,?,?,?,'StackableCurrency',10,?,'POEDB',1,?,?,"
            "'2026-09-21','2026-09-21')",
            (spec.metadata_id, spec.name_zh, spec.name_en, f"eldritch-{index}",
             spec.expected_effect, f"https://poedb.tw/cn/{index}", f"hash-{index}"),
        )
    conn.execute(
        "INSERT INTO craft_affix_capacity(rarity,item_kind,prefix_limit,suffix_limit,"
        "agreement,poedb_text,pob_text,source,source_priority,url,pob_url,source_commit,updated_at) "
        "VALUES('稀有','default',3,3,'both','稀有物品最多有三个前缀和三个后缀',"
        "'rareLimit=3','POEDB + Path of Building',1,'https://poedb.tw/cn/Modifiers',"
        "'https://github.com/PathOfBuildingCommunity/PathOfBuilding','abc','2026-09-21')"
    )
    for column, (guard, outcome, raw) in enumerate((
        ("前缀无法被变更", "not_applicable", "N/A"),
        ("后缀无法被变更", "not_applicable", "N/A"),
        ("无法骰出攻击词缀", "yes", "YES"),
        ("无法骰出法术词缀", "yes", "YES"),
    )):
        conn.execute(
            "INSERT INTO craft_metamod_interaction(interaction_id,section,operation_text,"
            "guard_text,outcome,raw_value,note_text,row_order,column_order,source,source_priority,"
            "source_url,mirror_url,content_hash,fetched_at,version_scope,updated_at) "
            "VALUES(?, '异能工艺','异能崇高石',?,?,?,?,32,?,'Craft of Exile',4,"
            "'https://www.craftofexile.com/metamods','https://poedb.tw/cn/metamods',?,"
            "'2026-09-21','unknown','2026-09-21')",
            (f"meta-{column}", guard, outcome, raw, None, column, f"meta-hash-{column}"),
        )
    conn.commit()
    return conn


class EldritchStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def evaluate(self, operation: str, **state):
        result = evaluate_eldritch_operation(
            self.conn, operation, EldritchItemState(**state)
        )
        self.assertIsNotNone(result)
        return result

    def test_契约恰好覆盖12种通货(self) -> None:
        self.assertEqual(12, len(ELDRITCH_SPECS))
        for spec in ELDRITCH_SPECS:
            self.assertIsNotNone(evaluate_eldritch_operation(
                self.conn, spec.name_zh, EldritchItemState()
            ), spec.name_zh)

    def test_余烬只允许四个装备部位并保留随机边界(self) -> None:
        result = self.evaluate("次级古灵余烬", item_slot="helmet")
        self.assertEqual("compatible", result.verdict)
        self.assertTrue(result.after["has_searing_implicit"])
        self.assertEqual("次级", result.after["searing_implicit_tier"])
        self.assertIn("新增的具体焚界者基底词缀", result.unknown_fields)
        self.assertEqual(
            "incompatible", self.evaluate("次级古灵余烬", item_slot="other").verdict
        )
        self.assertEqual("unknown", self.evaluate("次级古灵余烬").verdict)

    def test_溶液替换边界不会误删焚界者基底(self) -> None:
        result = self.evaluate(
            "卓越古灵溶液", item_slot="body_armour", has_searing_implicit=True
        )
        self.assertEqual("compatible", result.verdict)
        self.assertTrue(result.after["has_searing_implicit"])
        self.assertTrue(result.after["has_eater_implicit"])
        self.assertEqual("卓越", result.after["eater_implicit_tier"])

    def test_古灵混沌石按首要方重置单侧(self) -> None:
        result = self.evaluate(
            "古灵混沌石", dominance="焚界者", prefix_count=2, suffix_count=3
        )
        self.assertEqual("compatible", result.verdict)
        self.assertIsNone(result.after["prefix_count"])
        self.assertEqual(3, result.after["suffix_count"])
        self.assertEqual("prefix_modifiers_rerolled", result.after["explicit_modifier_change"])
        self.assertEqual("unknown", self.evaluate("古灵混沌石").verdict)
        self.assertEqual("incompatible", self.evaluate(
            "古灵混沌石", dominance="平衡"
        ).verdict)

    def test_古灵崇高石检查目标侧名额并披露矩阵(self) -> None:
        result = self.evaluate(
            "古灵崇高石", rarity="稀有", dominance="灭界者",
            prefix_count=2, suffix_count=2,
        )
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(3, result.after["suffix_count"])
        self.assertEqual(4, len(result.metamod_interactions))
        self.assertEqual("YES", result.metamod_interactions[2]["raw_value"])
        full = self.evaluate(
            "古灵崇高石", rarity="稀有", dominance="灭界者",
            prefix_count=2, suffix_count=3,
        )
        self.assertEqual("incompatible", full.verdict)

    def test_古灵剥离石只减少首要方对应的一侧(self) -> None:
        result = self.evaluate(
            "异能无效石", dominance="焚界者", prefix_count=2, suffix_count=3
        )
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(1, result.after["prefix_count"])
        self.assertEqual(3, result.after["suffix_count"])
        empty = self.evaluate(
            "古灵剥离石", dominance="灭界者", prefix_count=2, suffix_count=0
        )
        self.assertEqual("incompatible", empty.verdict)

    def test_冲突石要求双基底且不捏造升降档级(self) -> None:
        result = self.evaluate(
            "冲突石", has_searing_implicit=True, has_eater_implicit=True
        )
        self.assertEqual("compatible", result.verdict)
        self.assertTrue(result.after["random_outcome"])
        self.assertIn("升降后的确切档级", result.unknown_fields)
        self.assertEqual("incompatible", self.evaluate(
            "冲突石", has_searing_implicit=True, has_eater_implicit=False
        ).verdict)
        self.assertEqual("unknown", self.evaluate("冲突石").verdict)

    def test_来源漂移与特殊状态保持未知(self) -> None:
        self.conn.execute(
            "UPDATE poedb_currency SET effect_zh='上游变化' WHERE name_zh='次级古灵余烬'"
        )
        self.assertEqual("unknown", self.evaluate(
            "次级古灵余烬", item_slot="helmet"
        ).verdict)
        self.assertEqual("unknown", self.evaluate(
            "高级古灵溶液", item_slot="boots", corrupted=True
        ).verdict)


class EldritchToolApiAndRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patch = patch(
            "tools.impl.eldritch_operation.dbmod.get_conn", return_value=self.conn
        )
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_工具输出确定性答案与严格schema(self) -> None:
        result = eldritch_operation_state(
            "古灵崇高石", rarity="稀有", dominance="焚界者",
            prefix_count=2, suffix_count=3,
        )
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertIn("添加一个随机前缀", result.data["answer"])
        self.assertFalse(
            REGISTRY.spec("eldritch_operation_state").parameters["additionalProperties"]
        )

    def test_API与工具共用同一结果(self) -> None:
        response = eldritch_state(EldritchOperationRequest(
            operation="卓越古灵余烬", item_slot="helmet"
        ), _caller="test")
        self.assertEqual("compatible", response.verdict)
        self.assertEqual("卓越", response.after["searing_implicit_tier"])

    def test_古灵问法优先于基础崇高石子串并提取状态(self) -> None:
        question = "焚界者首要的稀有胸甲有2条前缀3条后缀，古灵崇高石能用吗"
        self.assertEqual("eldritch_operation_state", loop._preferred_tool_route(question))
        self.assertEqual({
            "operation": "古灵崇高石", "item_slot": "body_armour",
            "dominance": "焚界者", "prefix_count": 2, "suffix_count": 3,
            "rarity": "稀有",
        }, loop._eldritch_operation_args(question))

    def test_价格问法不进入状态机(self) -> None:
        self.assertIsNone(loop._eldritch_operation_args("古灵崇高石多少钱"))


if __name__ == "__main__":
    unittest.main()
