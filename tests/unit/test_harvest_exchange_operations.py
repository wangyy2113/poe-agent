"""Harvest 材料转换与牺牲状态机：17 条原文、边界、API 与路由。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from api.craft import HarvestExchangeRequest, harvest_exchange_operation_state
from core.db import SCHEMA
from craft.harvest_exchange_operations import (
    EXCHANGE_SPECS,
    SUPPORTED_EXCHANGE_EFFECTS,
    HarvestExchangeState,
    evaluate_harvest_exchange,
)
from orchestrator import loop
from tools.impl.harvest_exchange_operation import harvest_exchange_state
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for index, effect in enumerate(sorted(SUPPORTED_EXCHANGE_EFFECTS)):
        craft_id = f"exchange-{index}"
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
        if effect == "将一个辅助宝石转变为一个相同颜色的随机辅助宝石":
            conn.execute(
                "INSERT INTO harvest_craft_cost(craft_id,position,amount,currency_metadata_id,"
                "currency_slug,currency_name_zh,currency_url) VALUES(?,1,1,"
                "'Metadata/Hatred','Hatred_Crystal','憎恨结晶',"
                "'https://poedb.tw/cn/Hatred_Crystal')",
                (craft_id,),
            )
    conn.commit()
    return conn


class HarvestExchangeStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def evaluate(self, operation: str, **state):
        result = evaluate_harvest_exchange(
            self.conn, operation, HarvestExchangeState(**state)
        )
        self.assertIsNotNone(result)
        return result

    def test_契约恰好覆盖17条原文与18条费用(self) -> None:
        self.assertEqual(17, len(EXCHANGE_SPECS))
        self.assertEqual(17, len(SUPPORTED_EXCHANGE_EFFECTS))
        self.assertEqual(
            18, self.conn.execute("SELECT count(*) FROM harvest_craft_cost").fetchone()[0]
        )
        for spec in EXCHANGE_SPECS:
            self.assertIsNotNone(evaluate_harvest_exchange(
                self.conn, spec.effect_text, HarvestExchangeState()
            ), spec.effect_text)

    def test_辅助宝石只保证相同颜色和随机(self) -> None:
        result = self.evaluate("辅助宝石同色转换")
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("相同颜色的随机辅助宝石", result.output_relation)
        self.assertIn("随机得到的具体结果与概率", result.unknown_fields)

    def test_堆叠转换需要投入数量但不猜费用缩放(self) -> None:
        self.assertEqual("unknown", self.evaluate("催化剂转换").verdict)
        result = self.evaluate("催化剂转换", quantity=10)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("proportional_to_stack_size", result.after["cost_scaling"])
        self.assertIn("完整费用的堆叠缩放基准", result.unknown_fields)

    def test_化石转换保留输出数量可能变小(self) -> None:
        result = self.evaluate("化石转换", quantity=10)
        self.assertEqual("compatible", result.verdict)
        self.assertIn("可能较小", result.output_relation)
        self.assertIn("可能缩小后的输出堆叠数量", result.unknown_fields)

    def test_精华转换只保证位阶相同(self) -> None:
        result = self.evaluate("精华转换", quantity=4)
        self.assertEqual("compatible", result.verdict)
        self.assertIn("位阶相同", result.output_relation)

    def test_腐化稀有地图保留位阶稀有度与腐化(self) -> None:
        result = self.evaluate(
            "腐化地图牺牲", rarity="稀有", corrupted=True, map_tier=16
        )
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(16, result.after["preserved_map_tier"])
        self.assertEqual("稀有", result.after["preserved_rarity"])
        self.assertTrue(result.after["corrupted"])

    def test_地图牺牲拒绝未腐化或非稀有地图(self) -> None:
        self.assertEqual(
            "incompatible",
            self.evaluate("腐化地图牺牲", rarity="稀有", corrupted=False, map_tier=16).verdict,
        )
        self.assertEqual(
            "incompatible",
            self.evaluate("腐化地图牺牲", rarity="魔法", corrupted=True, map_tier=16).verdict,
        )

    def test_腐化技能石经验可精确减半(self) -> None:
        result = self.evaluate(
            "腐化技能石经验", corrupted=True, gem_total_experience=1_000_000
        )
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(500_000, result.after["stored_experience"])

    def test_技能石未腐化不兼容且奇数经验不猜取整(self) -> None:
        self.assertEqual(
            "incompatible",
            self.evaluate(
                "腐化技能石经验", corrupted=False, gem_total_experience=1000
            ).verdict,
        )
        odd = self.evaluate("腐化技能石经验", corrupted=True, gem_total_experience=1001)
        self.assertEqual("unknown", odd.verdict)
        self.assertIsNone(odd.after["stored_experience"])

    def test_命运卡赌博检查半叠并给零到翻倍范围(self) -> None:
        result = self.evaluate("命运卡赌博", quantity=5, max_stack_size=10)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(0, result.after["output_quantity_min"])
        self.assertEqual(10, result.after["output_quantity_max"])
        self.assertEqual(
            "incompatible",
            self.evaluate("命运卡赌博", quantity=6, max_stack_size=10).verdict,
        )
        self.assertEqual("unknown", self.evaluate("命运卡赌博", quantity=5).verdict)

    def test_费用缺失时不能声称完整兼容(self) -> None:
        result = self.evaluate("命运卡随机转换")
        self.conn.execute("DELETE FROM harvest_craft_cost WHERE craft_id=?", (result.craft_id,))
        self.assertEqual("unknown", self.evaluate("命运卡随机转换").verdict)


class HarvestExchangeToolApiAndRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patch = patch(
            "tools.impl.harvest_exchange_operation.dbmod.get_conn", return_value=self.conn
        )
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_工具输出确定性答案与严格schema(self) -> None:
        result = harvest_exchange_state(
            "命运卡赌博", quantity=3, max_stack_size=6
        )
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertIn("输出数量范围：0–6 张", result.data["answer"])
        self.assertFalse(REGISTRY.spec("harvest_exchange_state").parameters["additionalProperties"])

    def test_API与工具共用同一结果(self) -> None:
        response = harvest_exchange_operation_state(HarvestExchangeRequest(
            operation="腐化地图牺牲", rarity="稀有", corrupted=True, map_tier=16
        ), _caller="test")
        self.assertEqual("compatible", response.verdict)
        self.assertEqual(16, response.after["preserved_map_tier"])

    def test_转换牺牲问法直达且费用问法仍走目录(self) -> None:
        fossil = "收获把10个化石转换成另一种化石会怎样"
        self.assertEqual("harvest_exchange_state", loop._preferred_tool_route(fossil))
        self.assertEqual(
            {"operation": "化石转换", "quantity": 10}, loop._harvest_exchange_args(fossil)
        )
        map_question = "收获献祭一张16阶稀有腐化地图会得到什么"
        self.assertEqual("harvest_exchange_state", loop._preferred_tool_route(map_question))
        self.assertEqual(
            {"operation": "腐化地图牺牲", "map_tier": 16,
             "rarity": "稀有", "corrupted": True},
            loop._harvest_exchange_args(map_question),
        )
        cost = "收获转换一堆化石要多少命能"
        self.assertIsNone(loop._harvest_exchange_args(cost))
        self.assertEqual("harvest_craft_catalog", loop._preferred_tool_route(cost))


if __name__ == "__main__":
    unittest.main()
