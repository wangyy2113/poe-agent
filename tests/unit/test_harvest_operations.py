"""Harvest 重铸状态机：原文分类、稀有度、影响状态、费用与确定性出口。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from api.craft import HarvestReforgeRequest, harvest_state
from core.db import SCHEMA
from craft.harvest_operations import (
    INFLUENCE_EFFECT,
    RANDOMIZE_INFLUENCE_EFFECT,
    SAME_LESS_EFFECT,
    SAME_MORE_EFFECT,
    SUPPORTED_EFFECTS,
    HarvestItemState,
    evaluate_harvest_reforge,
)
from orchestrator import loop
from tools.impl.harvest_operation import harvest_reforge_state
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for index, effect in enumerate(sorted(SUPPORTED_EFFECTS)):
        craft_id = f"craft-{index}"
        conn.execute(
            "INSERT INTO harvest_craft(craft_id,effect_text,source,source_priority,url,"
            "content_hash,fetched_at,updated_at) VALUES(?,?,'POEDB',1,"
            "'https://poedb.tw/cn/Horticrafting',?,'2026-09-22','2026-09-22')",
            (craft_id, effect, f"hash-{index}"),
        )
        conn.execute(
            "INSERT INTO harvest_craft_cost(craft_id,position,amount,currency_metadata_id,"
            "currency_slug,currency_name_zh,currency_url) VALUES(?,0,50,"
            "'Metadata/Wild','Wild_Crystallised_Lifeforce','狂野紫晶命能',"
            "'https://poedb.tw/cn/Wild_Crystallised_Lifeforce')",
            (craft_id,),
        )
    conn.execute(
        "INSERT INTO harvest_craft(craft_id,effect_text,source,source_priority,url,content_hash,"
        "fetched_at,updated_at) VALUES('unsupported','将一堆化石变为另一种类型的化石',"
        "'POEDB',1,'https://poedb.tw/cn/Horticrafting','other','2026-09-22','2026-09-22')"
    )
    conn.commit()
    return conn


class HarvestOperationStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def evaluate(self, operation: str, **state):
        result = evaluate_harvest_reforge(self.conn, operation, HarvestItemState(**state))
        self.assertIsNotNone(result)
        return result

    def test_契约恰好覆盖20条重铸原文(self) -> None:
        self.assertEqual(20, len(SUPPORTED_EFFECTS))
        for effect in SUPPORTED_EFFECTS:
            self.assertIsNotNone(evaluate_harvest_reforge(
                self.conn, effect, HarvestItemState(rarity="稀有", influenced=True)
            ), effect)

    def test_火焰重铸稀有物品兼容并保底标签(self) -> None:
        result = self.evaluate("火焰", rarity="稀有")
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("reforge_guaranteed_tag", result.mode)
        self.assertEqual("火焰", result.guaranteed_tag)
        self.assertEqual("火焰", result.after["guaranteed_tag"])
        self.assertEqual("狂野紫晶命能", result.costs[0].currency_name_zh)

    def test_标签重铸对普通物品不兼容(self) -> None:
        result = self.evaluate("生命", rarity="普通")
        self.assertEqual("incompatible", result.verdict)
        self.assertEqual("fail", next(c for c in result.checks if c.key == "rarity").status)

    def test_标签重铸缺稀有度保持unknown(self) -> None:
        self.assertEqual("unknown", self.evaluate("冰霜").verdict)

    def test_影响词缀重铸要求受影响稀有物品(self) -> None:
        result = self.evaluate("影响词缀", rarity="稀有", influenced=True)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("影响", result.guaranteed_tag)
        self.assertEqual(INFLUENCE_EFFECT, result.effect_text)

    def test_影响词缀重铸对无影响力物品不兼容(self) -> None:
        result = self.evaluate("影响词缀", rarity="稀有", influenced=False)
        self.assertEqual("incompatible", result.verdict)

    def test_影响词缀重铸缺影响状态保持unknown(self) -> None:
        result = self.evaluate("影响词缀", rarity="稀有")
        self.assertEqual("unknown", result.verdict)

    def test_影响类型随机化接受受影响魔法物品(self) -> None:
        result = self.evaluate("随机影响类型", rarity="魔法", influenced=True)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(RANDOMIZE_INFLUENCE_EFFECT, result.effect_text)
        self.assertEqual("randomized", result.after["influence_state"])
        self.assertEqual("魔法", result.after["rarity"])

    def test_影响类型随机化拒绝传奇物品(self) -> None:
        result = self.evaluate("随机影响类型", rarity="传奇", influenced=True)
        self.assertEqual("incompatible", result.verdict)

    def test_同类倾向只保留定性原文不发明概率(self) -> None:
        more = self.evaluate("大概率相同", rarity="稀有")
        less = self.evaluate("大概率不同", rarity="稀有")
        self.assertEqual(SAME_MORE_EFFECT, more.effect_text)
        self.assertEqual(SAME_LESS_EFFECT, less.effect_text)
        self.assertEqual("same_modifier_types_more_likely", more.after["selection_bias"])
        self.assertIn("具体概率", more.unknown_fields[0])

    def test_费用子项缺失时不能声称完整兼容(self) -> None:
        result = self.evaluate("火焰", rarity="稀有")
        self.conn.execute("DELETE FROM harvest_craft_cost WHERE craft_id=?", (result.craft_id,))
        result = self.evaluate("火焰", rarity="稀有")
        self.assertEqual("unknown", result.verdict)

    def test_特殊装备状态保持unknown(self) -> None:
        result = self.evaluate("火焰", rarity="稀有", fractured=True)
        self.assertEqual("unknown", result.verdict)

    def test_转换类Harvest不被猜成重铸(self) -> None:
        self.assertIsNone(evaluate_harvest_reforge(
            self.conn, "将一堆化石变为另一种类型的化石", HarvestItemState(rarity="稀有")
        ))


class HarvestOperationToolApiAndRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patch = patch("tools.impl.harvest_operation.dbmod.get_conn", return_value=self.conn)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_工具输出确定性答案与严格schema(self) -> None:
        result = harvest_reforge_state("火焰", rarity="稀有")
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertIn("保底标签：火焰", result.data["answer"])
        self.assertIn("50x 狂野紫晶命能", result.data["answer"])
        self.assertFalse(REGISTRY.spec("harvest_reforge_state").parameters["additionalProperties"])

    def test_工具从装备文本识别影响力(self) -> None:
        text = (
            "Item Class: Amulets\nRarity: Rare\nTest Eye\nOnyx Amulet\n--------\n"
            "Item Level: 84\n--------\nShaper Item\n--------\n+20 to Strength"
        )
        result = harvest_reforge_state("影响词缀", item_text=text)
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertTrue(result.data["before"]["influenced"])

    def test_API与工具共用同一结果(self) -> None:
        response = harvest_state(HarvestReforgeRequest(
            operation="随机影响类型", rarity="普通", influenced=True
        ), _caller="test")
        self.assertEqual("compatible", response.verdict)
        self.assertEqual("randomized", response.after["influence_state"])

    def test_状态问法直达且费用问法仍走目录(self) -> None:
        question = "收获火焰重铸能用于稀有装备吗"
        self.assertEqual("harvest_reforge_state", loop._preferred_tool_route(question))
        self.assertEqual({"operation": "火焰", "rarity": "稀有"}, loop._harvest_reforge_args(question))
        cost_question = "收获里重铸一件带火焰词缀的稀有物品要多少命能"
        self.assertIsNone(loop._harvest_reforge_args(cost_question))
        self.assertEqual("harvest_craft_catalog", loop._preferred_tool_route(cost_question))


if __name__ == "__main__":
    unittest.main()
