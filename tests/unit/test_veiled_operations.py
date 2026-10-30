"""加密通货状态机：两条原文、隐匿候选目录、API 与聊天路由。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from api.craft import VeiledOperationRequest, veiled_catalog, veiled_state
from core.db import SCHEMA
from craft.veiled_operations import (
    VEILED_SPECS,
    VeiledItemState,
    evaluate_veiled_operation,
)
from orchestrator import loop
from tools.impl.veiled_operation import veiled_operation_state
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for index, spec in enumerate(VEILED_SPECS):
        conn.execute(
            "INSERT INTO poedb_currency(metadata_id,name_zh,name_en,poedb_slug,item_class,"
            "stack_max,effect_zh,source,source_priority,url,content_hash,fetched_at,updated_at) "
            "VALUES(?,?,?,?,'StackableCurrency',10,?,'POEDB',1,?,?,"
            "'2026-09-21','2026-09-21')",
            (spec.metadata_id, spec.name_zh, spec.name_en, f"veiled-{index}",
             spec.expected_effect, f"https://poedb.tw/cn/veiled-{index}", f"hash-{index}"),
        )
    for uid, page, mod_code, position, text in (
        ("v1", "Body_Armours_str", "VeiledLife", "前缀", "+(50-55) 最大生命"),
        ("v2", "Body_Armours_dex", "VeiledLife", "前缀", "+(50-55) 最大生命"),
        ("v3", "Body_Armours_str", "VeiledRes", "后缀", "+(16-20)% 火焰与混沌抗性"),
    ):
        conn.execute(
            "INSERT INTO mod_affix(uid,mod_code,item_class_page,item_class_code,item_class_zh,"
            "category,category_label,name_zh,gen_type,gen_label,correct_group,ilvl,tier,tier_count,"
            "weight,text_zh,source,source_priority,url,updated_at) VALUES(?,?,?,'Body Armour',"
            "'胸甲','veiled','隐匿','天选的',?,?,?,60,1,1,1000,?,'POEDB',1,?, '2026-09-21')",
            (uid, mod_code, page, 1 if position == "前缀" else 2, position,
             mod_code, text, f"https://poedb.tw/cn/{page}"),
        )
    conn.execute(
        "INSERT INTO craft_affix_capacity(rarity,item_kind,prefix_limit,suffix_limit,"
        "agreement,poedb_text,pob_text,source,source_priority,url,pob_url,source_commit,updated_at) "
        "VALUES('稀有','default',3,3,'both','稀有物品最多有三个前缀和三个后缀',"
        "'rareLimit=3','POEDB + Path of Building',1,'https://poedb.tw/cn/Modifiers',"
        "'https://github.com/PathOfBuildingCommunity/PathOfBuilding','abc','2026-09-21')"
    )
    for column, (guard, outcome, raw) in enumerate((
        ("前缀无法被变更", "yes", "YES"),
        ("后缀无法被变更", "yes", "YES"),
        ("无法骰出攻击词缀", "no", "NO"),
        ("无法骰出法术词缀", "no", "NO"),
    )):
        conn.execute(
            "INSERT INTO craft_metamod_interaction(interaction_id,section,operation_text,"
            "guard_text,outcome,raw_value,note_text,row_order,column_order,source,source_priority,"
            "source_url,mirror_url,content_hash,fetched_at,version_scope,updated_at) "
            "VALUES(?, '工艺方式','隐匿混沌石',?,?,?,?,6,?,'Craft of Exile',4,"
            "'https://www.craftofexile.com/metamods','https://poedb.tw/cn/metamods',?,"
            "'2026-09-21','unknown','2026-09-21')",
            (f"meta-{column}", guard, outcome, raw, None, column, f"meta-hash-{column}"),
        )
    conn.commit()
    return conn


class VeiledStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def evaluate(self, operation: str, **state):
        result = evaluate_veiled_operation(
            self.conn, operation, VeiledItemState(**state)
        )
        self.assertIsNotNone(result)
        return result

    def test_契约恰好覆盖两种通货(self) -> None:
        self.assertEqual(2, len(VEILED_SPECS))
        for spec in VEILED_SPECS:
            self.assertIsNotNone(evaluate_veiled_operation(
                self.conn, spec.name_zh, VeiledItemState()
            ))

    def test_两种通货都要求稀有物品(self) -> None:
        for operation in ("加密崇高石", "加密混沌石"):
            self.assertEqual("unknown", self.evaluate(operation).verdict)
            self.assertEqual("incompatible", self.evaluate(
                operation, rarity="魔法", prefix_count=1, suffix_count=1
            ).verdict)

    def test_加密混沌石重置全部并保底一个加密词缀(self) -> None:
        result = self.evaluate(
            "隐匿混沌石", rarity="稀有", item_class="胸甲",
            prefix_count=2, suffix_count=3,
        )
        self.assertEqual("compatible", result.verdict)
        self.assertIsNone(result.after["prefix_count"])
        self.assertIsNone(result.after["suffix_count"])
        self.assertTrue(result.after["has_veiled_modifier"])
        self.assertEqual(4, len(result.metamod_interactions))

    def test_加密崇高石移一加一并枚举计数边界(self) -> None:
        result = self.evaluate(
            "加密崇高石", rarity="稀有", item_class="胸甲",
            prefix_count=2, suffix_count=2,
        )
        self.assertEqual("compatible", result.verdict)
        self.assertEqual([
            {"prefix_count": 1, "suffix_count": 3},
            {"prefix_count": 2, "suffix_count": 2},
            {"prefix_count": 3, "suffix_count": 1},
        ], result.after["possible_affix_counts"])
        self.assertIn("随机移除的具体词缀", result.unknown_fields)

    def test_加密崇高石没有可移除词缀时不兼容(self) -> None:
        result = self.evaluate(
            "加密崇高石", rarity="稀有", prefix_count=0, suffix_count=0
        )
        self.assertEqual("incompatible", result.verdict)
        self.assertEqual("unknown", self.evaluate(
            "加密崇高石", rarity="稀有"
        ).verdict)

    def test_类别目录跨页去重且不冒充揭露概率(self) -> None:
        result = self.evaluate("加密混沌石", rarity="稀有", item_class="胸甲")
        pool = result.veiled_pool
        self.assertIsNotNone(pool)
        self.assertEqual(3, pool["source_rows"])
        self.assertEqual(2, pool["unique_candidates"])
        self.assertEqual(1, pool["prefix_candidates"])
        self.assertEqual(1, pool["suffix_candidates"])
        self.assertIn("不等于加密通货的结果概率", pool["caveat"])

    def test_未知类别与特殊状态保持未知(self) -> None:
        self.assertEqual("unknown", self.evaluate(
            "加密混沌石", rarity="稀有", item_class="不存在的类别"
        ).verdict)
        self.assertEqual("unknown", self.evaluate(
            "加密混沌石", rarity="稀有", corrupted=True
        ).verdict)

    def test_来源漂移拒绝沿用状态转移(self) -> None:
        for operation in ("加密混沌石", "加密崇高石"):
            self.conn.execute(
                "UPDATE poedb_currency SET effect_zh='上游变化' WHERE name_zh=?",
                (operation,),
            )
            result = self.evaluate(
                operation, rarity="稀有", item_class="胸甲",
                prefix_count=2, suffix_count=2,
            )
            self.assertEqual("unknown", result.verdict)
            self.assertEqual("unknown", result.after["modifier_change"])
            self.assertIsNone(result.after["rarity"])
            self.assertIsNone(result.after["prefix_count"])
            self.assertIsNone(result.after["suffix_count"])
            self.assertIsNone(result.after["has_veiled_modifier"])
            self.assertIsNone(result.after["random_outcome"])
            self.assertEqual([], result.after["veiled_modifier_position"])
            self.assertEqual([], result.after["possible_affix_counts"])
            self.assertIn("来源原文变化后的状态转移", result.unknown_fields)
            self.assertFalse(any(check.key == "rarity" for check in result.checks))


class VeiledToolApiAndRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patch = patch(
            "tools.impl.veiled_operation.dbmod.get_conn", return_value=self.conn
        )
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_工具输出候选目录与严格schema(self) -> None:
        result = veiled_operation_state(
            "加密混沌石", rarity="稀有", item_class="胸甲"
        )
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertIn("去重后 2 条", result.data["answer"])
        self.assertFalse(REGISTRY.spec("veiled_operation_state").parameters["additionalProperties"])

    def test_API与工具共用同一结果(self) -> None:
        response = veiled_state(VeiledOperationRequest(
            operation="加密崇高石", rarity="稀有", item_class="胸甲",
            prefix_count=2, suffix_count=2,
        ), _caller="test")
        self.assertEqual("compatible", response.verdict)
        self.assertEqual(2, response.veiled_pool["unique_candidates"])

    def test_清单只列当前入库操作并标记来源漂移(self) -> None:
        catalog = veiled_catalog(_caller="test")
        self.assertEqual(2, catalog.total)
        self.assertEqual(["加密崇高石", "加密混沌石"], [x.name_zh for x in catalog.items])
        self.assertTrue(all(x.source_contract_ok for x in catalog.items))
        self.conn.execute(
            "UPDATE poedb_currency SET effect_zh='上游变化' WHERE name_zh='加密崇高石'"
        )
        self.assertFalse(veiled_catalog(_caller="test").items[0].source_contract_ok)
        self.conn.execute(
            "DELETE FROM poedb_currency WHERE name_zh='加密混沌石'"
        )
        self.assertEqual(1, veiled_catalog(_caller="test").total)

    def test_来源漂移的确定性答案不复述旧操作语义(self) -> None:
        self.conn.execute(
            "UPDATE poedb_currency SET effect_zh='上游变化' WHERE name_zh='加密混沌石'"
        )
        result = veiled_operation_state("加密混沌石", rarity="稀有", item_class="胸甲")
        self.assertEqual("unknown", result.data["verdict"])
        self.assertIn("未知（来源效果原文已变化）", result.data["answer"])
        self.assertNotIn("重置全部词缀，并包含一个随机加密词缀", result.data["answer"])

    def test_加密通货优先于基础通货子串(self) -> None:
        question = "稀有胸甲有2条前缀2条后缀，加密崇高石会怎样"
        self.assertEqual("veiled_operation_state", loop._preferred_tool_route(question))
        self.assertEqual({
            "operation": "加密崇高石", "item_class": "胸甲", "rarity": "稀有",
            "prefix_count": 2, "suffix_count": 2,
        }, loop._veiled_operation_args(question))

    def test_爱斯林和价格问法不进入状态机(self) -> None:
        self.assertIsNone(loop._veiled_operation_args("爱斯林隐匿工艺会怎样"))
        self.assertIsNone(loop._veiled_operation_args("加密混沌石多少钱"))


if __name__ == "__main__":
    unittest.main()
