"""Harvest 品质附魔状态机：14 条原文、类别、品质、费用、API 与路由。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from api.craft import HarvestQualityEnchantRequest, harvest_quality_enchant_operation_state
from core.db import SCHEMA
from craft.harvest_enchant_operations import (
    ENCHANT_SPECS,
    SUPPORTED_ENCHANT_EFFECTS,
    HarvestEnchantItemState,
    evaluate_harvest_enchant,
)
from orchestrator import loop
from tools.impl.harvest_enchant_operation import harvest_quality_enchant_state
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for index, effect in enumerate(sorted(SUPPORTED_ENCHANT_EFFECTS)):
        craft_id = f"enchant-{index}"
        conn.execute(
            "INSERT INTO harvest_craft(craft_id,effect_text,source,source_priority,url,"
            "content_hash,fetched_at,updated_at) VALUES(?,?,'POEDB',1,"
            "'https://poedb.tw/cn/Horticrafting',?,'2026-09-21','2026-09-21')",
            (craft_id, effect, f"hash-{index}"),
        )
        amount = 3250 if "护甲" in effect else 5000
        conn.execute(
            "INSERT INTO harvest_craft_cost(craft_id,position,amount,currency_metadata_id,"
            "currency_slug,currency_name_zh,currency_url) VALUES(?,0,?,"
            "'Metadata/Wild','Wild_Crystallised_Lifeforce','狂野紫晶命能',"
            "'https://poedb.tw/cn/Wild_Crystallised_Lifeforce')",
            (craft_id, amount),
        )
    conn.commit()
    return conn


class HarvestEnchantStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def evaluate(self, operation: str, **state):
        result = evaluate_harvest_enchant(
            self.conn, operation, HarvestEnchantItemState(**state)
        )
        self.assertIsNotNone(result)
        return result

    def test_契约恰好覆盖14条附魔原文与14条费用(self) -> None:
        self.assertEqual(14, len(ENCHANT_SPECS))
        self.assertEqual(14, len(SUPPORTED_ENCHANT_EFFECTS))
        self.assertEqual(
            14, self.conn.execute("SELECT count(*) FROM harvest_craft_cost").fetchone()[0]
        )
        for spec in ENCHANT_SPECS:
            kind = "melee_weapon" if spec.item_kind != "body_armour" else "body_armour"
            self.assertIsNotNone(evaluate_harvest_enchant(
                self.conn, spec.effect_text, HarvestEnchantItemState(item_kind=kind, quality=40)
            ), spec.effect_text)

    def test_二十品质护甲火抗附魔确定为十火抗(self) -> None:
        result = self.evaluate("护甲火焰抗性", item_kind="body_armour", quality=20)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("防御", result.after["replaced_quality_effect"])
        self.assertEqual("火焰抗性", result.after["granted_stat"])
        self.assertEqual(10, result.after["derived_value"])
        self.assertEqual(3250, result.costs[0].amount)

    def test_护甲附魔不能用于武器(self) -> None:
        result = self.evaluate("护甲力量", item_kind="weapon", quality=20)
        self.assertEqual("incompatible", result.verdict)

    def test_通用武器附魔接受近战武器(self) -> None:
        result = self.evaluate("武器攻击速度", item_kind="melee_weapon", quality=24)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(3, result.after["derived_value"])
        self.assertEqual("物理伤害", result.after["replaced_quality_effect"])

    def test_近战范围附魔拒绝未确认近战的通用武器(self) -> None:
        result = self.evaluate("近战武器范围", item_kind="weapon", quality=20)
        self.assertEqual("incompatible", result.verdict)

    def test_近战范围按十品质增加零点一米(self) -> None:
        result = self.evaluate("近战武器范围", item_kind="melee_weapon", quality=30)
        self.assertEqual("compatible", result.verdict)
        self.assertEqual(0.3, result.after["derived_value"])

    def test_非整步品质不猜取整(self) -> None:
        result = self.evaluate("武器暴击率", item_kind="weapon", quality=21)
        self.assertEqual("unknown", result.verdict)
        self.assertIsNone(result.after["derived_value"])
        self.assertTrue(any("取整" in item for item in result.unknown_fields))

    def test_缺类别或品质保持unknown(self) -> None:
        self.assertEqual("unknown", self.evaluate("武器精度", quality=20).verdict)
        self.assertEqual("unknown", self.evaluate("武器精度", item_kind="weapon").verdict)

    def test_特殊状态与缺费用保持unknown(self) -> None:
        special = self.evaluate("护甲生命", item_kind="body_armour", quality=20, mirrored=True)
        self.assertEqual("unknown", special.verdict)
        normal = self.evaluate("武器元素伤害", item_kind="weapon", quality=20)
        self.conn.execute("DELETE FROM harvest_craft_cost WHERE craft_id=?", (normal.craft_id,))
        self.assertEqual(
            "unknown", self.evaluate("武器元素伤害", item_kind="weapon", quality=20).verdict
        )


class HarvestEnchantToolApiAndRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patch = patch(
            "tools.impl.harvest_enchant_operation.dbmod.get_conn", return_value=self.conn
        )
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_工具输出确定性答案与严格schema(self) -> None:
        result = harvest_quality_enchant_state(
            "武器攻击速度", item_kind="weapon", quality=24
        )
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertIn("按当前品质可确定的收益：3%", result.data["answer"])
        self.assertIn("5000x 狂野紫晶命能", result.data["answer"])
        self.assertFalse(
            REGISTRY.spec("harvest_quality_enchant_state").parameters["additionalProperties"]
        )

    def test_工具从装备文本识别胸甲与品质(self) -> None:
        text = (
            "Item Class: Body Armours\nRarity: Rare\nTest Shell\nAstral Plate\n--------\n"
            "Quality: +20% (augmented)\nArmour: 1000\n--------\nItem Level: 84"
        )
        result = harvest_quality_enchant_state("护甲火焰抗性", item_text=text)
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertEqual("body_armour", result.data["before"]["item_kind"])
        self.assertEqual(20, result.data["before"]["quality"])

    def test_API与工具共用同一结果(self) -> None:
        response = harvest_quality_enchant_operation_state(HarvestQualityEnchantRequest(
            operation="近战武器范围", item_kind="melee_weapon", quality=20
        ), _caller="test")
        self.assertEqual("compatible", response.verdict)
        self.assertEqual(0.2, response.after["derived_value"])

    def test_状态问法直达且费用问法仍走目录(self) -> None:
        armour = "收获护甲品质20%附魔成火焰抗性能加多少"
        self.assertEqual("harvest_quality_enchant_state", loop._preferred_tool_route(armour))
        self.assertEqual(
            {"operation": "护甲火焰抗性", "item_kind": "body_armour", "quality": 20},
            loop._harvest_quality_enchant_args(armour),
        )
        melee = "收获近战武器品质 30% 的武器范围附魔会怎样"
        self.assertEqual("harvest_quality_enchant_state", loop._preferred_tool_route(melee))
        cost = "收获武器攻击速度品质附魔要多少命能"
        self.assertIsNone(loop._harvest_quality_enchant_args(cost))
        self.assertEqual("harvest_craft_catalog", loop._preferred_tool_route(cost))


if __name__ == "__main__":
    unittest.main()
