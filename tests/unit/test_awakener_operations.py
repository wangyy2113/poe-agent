"""Awakener's Orb: two-item source contract, tool/API, and deterministic routing."""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from api.craft import AwakenerOrbRequest, awakener_state
from core.db import SCHEMA
from craft.awakener_operations import (
    EXPECTED_EFFECT,
    METADATA_ID,
    AwakenerItemState,
    evaluate_awakener_orb,
)
from orchestrator import loop
from tools.impl.awakener_operation import awakener_orb_state
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO poedb_currency(metadata_id,name_zh,name_en,poedb_slug,item_class,"
        "stack_max,effect_zh,source,source_priority,url,content_hash,fetched_at,updated_at) "
        "VALUES(?,?,?,'Awakeners_Orb','StackableCurrency',10,?,'POEDB',1,"
        "'https://poedb.tw/cn/Awakeners_Orb','hash','2026-09-22','2026-09-22')",
        (METADATA_ID, "觉醒者之石", "Awakener's Orb", EXPECTED_EFFECT),
    )
    conn.commit()
    return conn


def _state(item_class: str = "Amulets", influence: str = "shaper", **kwargs):
    return AwakenerItemState(item_class=item_class, influences=(influence,), **kwargs)


class AwakenerOperationStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_同类且不同传统影响给出有界条件后状态(self) -> None:
        result = evaluate_awakener_orb(
            self.conn, _state(influence="shaper"), _state(influence="hunter")
        )
        self.assertIsNotNone(result)
        self.assertEqual("compatible", result.verdict)
        self.assertTrue(result.after["donor_destroyed"])
        self.assertEqual("稀有", result.after["target_rarity"])
        self.assertEqual(["狩猎者", "塑界者"], result.after["target_influences"])
        self.assertEqual("reforged_with_new_modifiers", result.after["target_modifier_identities"])
        self.assertIn("来源原文未说明会保留", result.unknown_fields[1])

    def test_不同类别明确不满足同类物品(self) -> None:
        result = evaluate_awakener_orb(
            self.conn,
            _state(item_class="Amulets", influence="elder"),
            _state(item_class="Rings", influence="warlord"),
        )
        self.assertEqual("incompatible", result.verdict)
        check = next(item for item in result.checks if item.key == "same_item_class")
        self.assertEqual("fail", check.status)

    def test_缺状态同影响和多影响都不猜(self) -> None:
        missing = evaluate_awakener_orb(
            self.conn, AwakenerItemState(), AwakenerItemState()
        )
        self.assertEqual("unknown", missing.verdict)
        same = evaluate_awakener_orb(
            self.conn, _state(influence="elder"), _state(influence="elder")
        )
        self.assertEqual("unknown", same.verdict)
        multiple = evaluate_awakener_orb(
            self.conn,
            AwakenerItemState(item_class="Amulets", influences=("elder", "shaper")),
            _state(influence="hunter"),
        )
        self.assertEqual("unknown", multiple.verdict)

    def test_古灵或特殊状态不冒充传统双影响规则(self) -> None:
        eldritch = evaluate_awakener_orb(
            self.conn, _state(influence="searing_exarch"), _state(influence="hunter")
        )
        self.assertEqual("unknown", eldritch.verdict)
        corrupted = evaluate_awakener_orb(
            self.conn, _state(influence="shaper", corrupted=True), _state(influence="hunter")
        )
        self.assertEqual("unknown", corrupted.verdict)

    def test_来源原文漂移立即停止旧状态转移(self) -> None:
        self.conn.execute(
            "UPDATE poedb_currency SET effect_zh='上游原文变化' WHERE metadata_id=?",
            (METADATA_ID,),
        )
        result = evaluate_awakener_orb(
            self.conn, _state(influence="shaper"), _state(influence="hunter")
        )
        self.assertEqual("unknown", result.verdict)
        self.assertIsNone(result.after["donor_destroyed"])
        self.assertIn("来源原文变化后的状态转移", result.unknown_fields)


class AwakenerToolApiAndRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patch = patch(
            "tools.impl.awakener_operation.dbmod.get_conn", return_value=self.conn
        )
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_工具可解析两段装备文本且不会承诺保留词缀(self) -> None:
        donor = (
            "Item Class: Amulets\nRarity: Rare\nDonor\nAmber Amulet\n--------\n"
            "Item Level: 84\n--------\nShaper Item"
        )
        target = (
            "Item Class: Amulets\nRarity: Rare\nTarget\nOnyx Amulet\n--------\n"
            "Item Level: 84\n--------\nHunter Item"
        )
        result = awakener_orb_state(donor_item_text=donor, target_item_text=target)
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertIn("供体：被摧毁", result.data["answer"])
        self.assertIn("具体结果未知", result.data["answer"])
        self.assertNotIn("各保留一条", result.data["answer"])
        self.assertEqual(1, len(result.citations))
        self.assertFalse(REGISTRY.spec("awakener_orb_state").parameters["additionalProperties"])

    def test_API与工具共用同一结果(self) -> None:
        response = awakener_state(AwakenerOrbRequest(
            donor_item_class="项链",
            target_item_class="项链",
            donor_influences=["塑界者"],
            target_influences=["猎人"],
        ), _caller="test")
        self.assertEqual("compatible", response.verdict)
        self.assertEqual(["狩猎者", "塑界者"], response.after["target_influences"])

    def test_无状态问法直达工具但价格和双装备文本交给其他路径(self) -> None:
        question = "觉醒者之石如何约束制作操作"
        self.assertEqual({}, loop._awakener_orb_args(question))
        self.assertEqual("awakener_orb_state", loop._preferred_tool_route(question))
        self.assertIsNone(loop._awakener_orb_args("觉醒者之石多少钱"))
        pasted = "觉醒者之石能用吗\nItem Class: Amulets\n...\nItem Class: Amulets"
        self.assertIsNone(loop._awakener_orb_args(pasted))


if __name__ == "__main__":
    unittest.main()
