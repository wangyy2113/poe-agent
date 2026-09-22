"""化石标签权重、地渊目录、API 与聊天路由。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from api.craft import FossilOperationRequest, fossil_state
from core.db import SCHEMA
from craft.fossil_operations import (
    FOSSIL_NAMES,
    FossilItemState,
    evaluate_fossil_operation,
)
from orchestrator import loop
from tools.impl.fossil_operation import fossil_operation_state
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for row_index, (name, tag, multiplier) in enumerate((
        ("炽炎化石", "冰霜", 0.0),
        ("炽炎化石", "火焰", 10.0),
        ("以太化石", "攻击", 0.15),
        ("以太化石", "施法", 10.0),
    )):
        conn.execute(
            "INSERT INTO poedb_tag_weight(kind,name_zh,tag,multiplier,row_index,source,"
            "source_priority,url,fetched_at,updated_at) VALUES('化石',?,?,?,?, 'POEDB',1,"
            "'https://poedb.tw/cn/Horticrafting','2026-09-22','2026-09-22')",
            (name, tag, multiplier, row_index),
        )
    for uid, page, mod_code, position, ilvl, tags, fossil_tags, text in (
        ("d1", "Body_Armours_str", "DelveFire", "前缀", 1,
         '["元素", "火焰"]', '["elemental", "fire"]', "火焰伤害提高 (20-30)%"),
        ("d2", "Body_Armours_dex", "DelveFire", "前缀", 1,
         '["元素", "火焰"]', '["elemental", "fire"]', "火焰伤害提高 (20-30)%"),
        ("d3", "Body_Armours_str", "DelveCold", "后缀", 60,
         '["冰霜"]', '["cold"]', "+(20-25)% 冰霜抗性"),
        ("d4", "Body_Armours_str", "DelveLife", "后缀", 80,
         '["生命"]', '["life"]', "+(50-60) 最大生命"),
    ):
        conn.execute(
            "INSERT INTO mod_affix(uid,mod_code,item_class_page,item_class_code,item_class_zh,"
            "category,category_label,name_zh,gen_type,gen_label,correct_group,ilvl,tier,tier_count,"
            "weight,text_zh,tags,fossil_tags,source,source_priority,url,updated_at) "
            "VALUES(?,?,?,'Body Armour','胸甲','delve','地渊','地心的',?,?,?, ?,1,1,2000,"
            "?,?,?,'POEDB',1,?,'2026-09-22')",
            (uid, mod_code, page, 1 if position == "前缀" else 2, position, mod_code,
             ilvl, text, tags, fossil_tags, f"https://poedb.tw/cn/{page}"),
        )
    for column, (guard, outcome, raw) in enumerate((
        ("前缀无法被变更", "no", "NO"),
        ("后缀无法被变更", "no", "NO"),
        ("无法骰出攻击词缀", "not_applicable", "N/A"),
        ("无法骰出法术词缀", "not_applicable", "N/A"),
    )):
        conn.execute(
            "INSERT INTO craft_metamod_interaction(interaction_id,section,operation_text,"
            "guard_text,outcome,raw_value,note_text,row_order,column_order,source,source_priority,"
            "source_url,mirror_url,content_hash,fetched_at,version_scope,updated_at) "
            "VALUES(?,'工艺方式','化石',?,?,?,?,1,?,'Craft of Exile',4,"
            "'https://www.craftofexile.com/metamods','https://poedb.tw/cn/metamods',?,"
            "'2026-09-22','unknown','2026-09-22')",
            (f"meta-{column}", guard, outcome, raw, None, column, f"hash-{column}"),
        )
    conn.commit()
    return conn


class FossilStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def evaluate(self, fossil: str, **state):
        result = evaluate_fossil_operation(self.conn, fossil, FossilItemState(**state))
        self.assertIsNotNone(result)
        return result

    def test_契约列出十八种化石且只接受精确名称(self) -> None:
        self.assertEqual(18, len(FOSSIL_NAMES))
        self.assertIsNotNone(evaluate_fossil_operation(
            self.conn, "炽炎化石", FossilItemState()
        ))
        self.assertIsNone(evaluate_fossil_operation(
            self.conn, "炽炎", FossilItemState()
        ))

    def test_逐标签披露屏蔽与增权而不合并概率(self) -> None:
        result = self.evaluate("炽炎化石")
        self.assertEqual("unknown", result.verdict)
        self.assertEqual([0.0, 10.0], [rule["multiplier"] for rule in result.weight_rules])
        self.assertEqual(["blocked", "increased"], [rule["effect"] for rule in result.weight_rules])
        self.assertIsNone(result.after["prefix_count"])
        self.assertIn("最终生成权重", result.unknown_fields[2])

    def test_地渊目录跨页去重并按物等筛选(self) -> None:
        result = self.evaluate("炽炎化石", item_class="胸甲", item_level=60)
        pool = result.delve_pool
        self.assertIsNotNone(pool)
        self.assertEqual(4, pool["source_rows"])
        self.assertEqual(3, pool["unique_before_level_filter"])
        self.assertEqual(1, pool["excluded_by_item_level"])
        self.assertEqual(2, pool["unique_candidates"])
        fire, cold = pool["candidates"]
        self.assertEqual([{"tag": "火焰", "multiplier": 10.0}], fire["matching_weight_rules"])
        self.assertTrue(cold["blocked_by_explicit_x0"])
        self.assertIn("不是单颗化石的完整可生成池", pool["caveat"])

    def test_未知类别和特殊状态保持未知(self) -> None:
        result = self.evaluate("炽炎化石", item_class="不存在", corrupted=True)
        self.assertEqual("unknown", result.verdict)
        self.assertIsNone(result.delve_pool)
        self.assertEqual("unknown", next(
            check.status for check in result.checks if check.key == "special_state"
        ))

    def test_元词缀矩阵只并列披露原值(self) -> None:
        result = self.evaluate("炽炎化石")
        self.assertEqual(["NO", "NO", "N/A", "N/A"], [
            row["raw_value"] for row in result.metamod_interactions
        ])


class FossilToolApiAndRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patch = patch(
            "tools.impl.fossil_operation.dbmod.get_conn", return_value=self.conn
        )
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_工具输出边界与严格_schema(self) -> None:
        result = fossil_operation_state(
            "炽炎化石", item_class="胸甲", item_level=60, rarity="稀有"
        )
        self.assertTrue(result.ok)
        self.assertEqual("unknown", result.data["verdict"])
        self.assertIn("完整化石操作仍未知", result.data["answer"])
        self.assertFalse(REGISTRY.spec("fossil_operation_state").parameters["additionalProperties"])

    def test_API与工具共用同一结果(self) -> None:
        response = fossil_state(FossilOperationRequest(
            fossil="炽炎化石", item_class="胸甲", item_level=60,
        ), _caller="test")
        self.assertEqual("unknown", response.verdict)
        self.assertEqual(2, response.delve_pool["unique_candidates"])

    def test_具名化石目录问法进入状态机(self) -> None:
        question = "物等60的胸甲用炽炎化石会怎样，有哪些地渊词缀"
        self.assertEqual("fossil_operation_state", loop._preferred_tool_route(question))
        self.assertEqual({
            "fossil": "炽炎化石", "item_class": "胸甲", "item_level": 60,
        }, loop._fossil_operation_args(question))

    def test_权重问法仍由标签权重工具处理(self) -> None:
        for question in ("炽炎化石有什么用", "炽炎化石的权重乘数是多少"):
            self.assertIsNone(loop._fossil_operation_args(question))
            self.assertEqual("tag_weight_lookup", loop._preferred_tool_route(question))

    def test_共鸣器和概率不进入状态机(self) -> None:
        self.assertIsNone(loop._fossil_operation_args("炽炎化石和共鸣器怎么搭配"))
        self.assertIsNone(loop._fossil_operation_args("炽炎化石成功率是多少"))


if __name__ == "__main__":
    unittest.main()
