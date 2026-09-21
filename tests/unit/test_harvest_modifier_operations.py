"""Harvest 定向词缀状态机：23 条原文、条件、费用、API 与聊天路由。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from api.craft import HarvestModifierRequest, harvest_modifier_operation_state
from core.db import SCHEMA
from craft.harvest_modifier_operations import (
    CONVERSION_EFFECTS,
    REMOVE_ADD_EFFECTS,
    SUPPORTED_MODIFIER_EFFECTS,
    HarvestModifierItemState,
    evaluate_harvest_modifier_operation,
)
from orchestrator import loop
from tools.impl.harvest_modifier_operation import harvest_modifier_state
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conversion_values = set(CONVERSION_EFFECTS.values())
    for index, effect in enumerate(sorted(SUPPORTED_MODIFIER_EFFECTS)):
        craft_id = f"modifier-{index}"
        conn.execute(
            "INSERT INTO harvest_craft(craft_id,effect_text,source,source_priority,url,"
            "content_hash,fetched_at,updated_at) VALUES(?,?,'POEDB',1,"
            "'https://poedb.tw/cn/Horticrafting',?,'2026-09-21','2026-09-21')",
            (craft_id, effect, f"hash-{index}"),
        )
        conn.execute(
            "INSERT INTO harvest_craft_cost(craft_id,position,amount,currency_metadata_id,"
            "currency_slug,currency_name_zh,currency_url) VALUES(?,0,500,"
            "'Metadata/Wild','Wild_Crystallised_Lifeforce','狂野紫晶命能',"
            "'https://poedb.tw/cn/Wild_Crystallised_Lifeforce')",
            (craft_id,),
        )
        if effect not in conversion_values or "伤害" in effect:
            second_name = "憎恨结晶" if effect in conversion_values else "神圣白晶命能"
            conn.execute(
                "INSERT INTO harvest_craft_cost(craft_id,position,amount,currency_metadata_id,"
                "currency_slug,currency_name_zh,currency_url) VALUES(?,1,1,"
                "'Metadata/Second','Second_Currency',?,"
                "'https://poedb.tw/cn/Second_Currency')",
                (craft_id, second_name),
            )
    conn.execute(
        "INSERT INTO harvest_craft(craft_id,effect_text,source,source_priority,url,content_hash,"
        "fetched_at,updated_at) VALUES('unsupported','将一堆化石变为另一种类型的化石',"
        "'POEDB',1,'https://poedb.tw/cn/Horticrafting','other','2026-09-21','2026-09-21')"
    )
    conn.commit()
    return conn


class HarvestModifierOperationStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def evaluate(self, operation: str, **state):
        result = evaluate_harvest_modifier_operation(
            self.conn, operation, HarvestModifierItemState(**state)
        )
        self.assertIsNotNone(result)
        return result

    def test_契约恰好覆盖23条原文和40条费用子项(self) -> None:
        self.assertEqual(12, len(CONVERSION_EFFECTS))
        self.assertEqual(11, len(REMOVE_ADD_EFFECTS))
        self.assertEqual(23, len(SUPPORTED_MODIFIER_EFFECTS))
        count = self.conn.execute("SELECT count(*) FROM harvest_craft_cost").fetchone()[0]
        self.assertEqual(40, count)
        for effect in SUPPORTED_MODIFIER_EFFECTS:
            self.assertIsNotNone(evaluate_harvest_modifier_operation(
                self.conn,
                effect,
                HarvestModifierItemState(influenced=False, source_modifier_present=True),
            ), effect)

    def test_元素抗性转换替换一条并保留近似位阶措辞(self) -> None:
        result = self.evaluate("火焰抗性转冰霜抗性", source_modifier_present=True)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("火焰抗性", result.source_modifier)
        self.assertEqual("冰霜抗性", result.target_modifier)
        self.assertEqual("approximately_same_tier", result.after["tier_relation"])
        self.assertIn("精确映射", result.unknown_fields[0])

    def test_元素转换缺来源词缀时不兼容(self) -> None:
        result = self.evaluate("冰霜伤害转闪电伤害", source_modifier_present=False)
        self.assertEqual("incompatible", result.verdict)

    def test_元素转换未说明来源词缀时保持unknown(self) -> None:
        self.assertEqual("unknown", self.evaluate("冰霜伤害转火焰伤害").verdict)

    def test_新增移除要求不受影响物品(self) -> None:
        result = self.evaluate("新增火焰并移除随机词缀", influenced=False)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("火焰", result.guaranteed_tag)
        self.assertEqual("one_random_other_modifier", result.after["removed_modifier"])
        self.assertEqual("one_added_one_removed", result.after["modifier_count_change"])

    def test_新增移除拒绝受影响物品(self) -> None:
        self.assertEqual(
            "incompatible",
            self.evaluate("新增生命并移除随机词缀", influenced=True).verdict,
        )

    def test_新增移除缺影响状态时保持unknown(self) -> None:
        self.assertEqual("unknown", self.evaluate("新增防御并移除随机词缀").verdict)

    def test_特殊装备状态与缺费用保持unknown(self) -> None:
        special = self.evaluate(
            "闪电抗性转火焰抗性", source_modifier_present=True, fractured=True
        )
        self.assertEqual("unknown", special.verdict)
        normal = self.evaluate("新增攻击并移除随机词缀", influenced=False)
        self.conn.execute("DELETE FROM harvest_craft_cost WHERE craft_id=?", (normal.craft_id,))
        self.assertEqual(
            "unknown", self.evaluate("新增攻击并移除随机词缀", influenced=False).verdict
        )

    def test_目录中的其它Harvest操作不被猜入状态机(self) -> None:
        self.assertIsNone(evaluate_harvest_modifier_operation(
            self.conn,
            "将一堆化石变为另一种类型的化石",
            HarvestModifierItemState(),
        ))


class HarvestModifierToolApiAndRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patch = patch(
            "tools.impl.harvest_modifier_operation.dbmod.get_conn", return_value=self.conn
        )
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_工具输出确定性答案与严格schema(self) -> None:
        result = harvest_modifier_state(
            "火焰伤害转冰霜伤害", source_modifier_present=True
        )
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertIn("近似位阶", result.data["answer"])
        self.assertIn("500x 狂野紫晶命能", result.data["answer"])
        self.assertFalse(REGISTRY.spec("harvest_modifier_state").parameters["additionalProperties"])

    def test_工具从装备文本识别无影响力但不猜来源词缀(self) -> None:
        text = (
            "Item Class: Amulets\nRarity: Rare\nTest Eye\nOnyx Amulet\n--------\n"
            "Item Level: 84\n--------\n+20 to Strength"
        )
        result = harvest_modifier_state("新增暴击并移除随机词缀", item_text=text)
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertFalse(result.data["before"]["influenced"])

    def test_API与工具共用同一结果(self) -> None:
        response = harvest_modifier_operation_state(HarvestModifierRequest(
            operation="闪电抗性转火焰抗性", source_modifier_present=True
        ), _caller="test")
        self.assertEqual("compatible", response.verdict)
        self.assertEqual("火焰抗性", response.target_modifier)

    def test_两类状态问法直达且费用问法仍走目录(self) -> None:
        conversion = "收获能把火焰抗性转成冰霜抗性吗"
        self.assertEqual("harvest_modifier_state", loop._preferred_tool_route(conversion))
        self.assertEqual(
            {"operation": "火焰抗性转冰霜抗性"},
            loop._harvest_modifier_args(conversion),
        )
        remove_add = "收获新增火焰词缀并移除随机词缀能用于无影响力物品吗"
        self.assertEqual("harvest_modifier_state", loop._preferred_tool_route(remove_add))
        self.assertEqual(
            {"operation": "新增火焰并移除随机词缀", "influenced": False},
            loop._harvest_modifier_args(remove_add),
        )
        cost = "收获把火焰抗性转成冰霜抗性要多少命能"
        self.assertIsNone(loop._harvest_modifier_args(cost))
        self.assertEqual("harvest_craft_catalog", loop._preferred_tool_route(cost))


if __name__ == "__main__":
    unittest.main()
