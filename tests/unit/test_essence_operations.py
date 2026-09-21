"""精华状态机：来源原文、等级上限、装备类别、三态与 API/工具一致性。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from api.craft import EssenceOperationRequest, essence_state
from core.db import SCHEMA
from craft.essence_operations import (
    HIGH_ESSENCE_EFFECT,
    LOW_ESSENCE_EFFECT,
    EssenceItemState,
    evaluate_essence_operation,
)
from orchestrator import loop
from tools.impl.essence_operation import essence_operation_state
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    rows = [
        ("low", "憎恨之低语精华", "Whispering Essence of Hatred", 1, 35, LOW_ESSENCE_EFFECT),
        ("high", "憎恨之破空精华", "Deafening Essence of Hatred", 7, None, HIGH_ESSENCE_EFFECT),
        # 真实数据里的 tier 0 例外：语义必须读效果原文，不能按 tier 推断。
        ("special", "哀恸精华", "Essence of Desolation", 0, 15, HIGH_ESSENCE_EFFECT),
    ]
    conn.executemany(
        "INSERT INTO poedb_essence(metadata_id,name_zh,name_en,slug,tier,stack_max,"
        "level_cap,base_effect,source,source_priority,url,content_hash,fetched_at,updated_at) "
        "VALUES(?,?,?,?,?,10,?,?,'POEDB',1,'https://poedb.tw/cn/Essence','hash','2026-09-21','2026-09-21')",
        [(mid, zh, en, mid, tier, cap, effect) for mid, zh, en, tier, cap, effect in rows],
    )
    conn.executemany(
        "INSERT INTO poedb_essence_mod(metadata_id,group_index,item_classes,text_zh) "
        "VALUES(?,?,?,?)",
        [
            ("low", 0, "项链、戒指", "冰霜伤害提高 (6—10)%"),
            ("high", 0, "项链、戒指", "冰霜伤害提高 (31—34)%"),
            ("special", 0, "项链", "你近期内未施加过的异常状态类型，其持续时间延长 (40—60)%"),
        ],
    )
    conn.commit()
    return conn


class EssenceOperationStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()

    def tearDown(self) -> None:
        self.conn.close()

    def evaluate(self, essence: str, **state):
        result = evaluate_essence_operation(self.conn, essence, EssenceItemState(**state))
        self.assertIsNotNone(result)
        return result

    def test_低阶精华升级合规白装并给出类别保底(self) -> None:
        result = self.evaluate(
            "憎恨之低语精华", rarity="普通", item_level=35, item_class="项链"
        )
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("upgrade_normal_to_rare", result.after["operation_mode"])
        self.assertEqual("稀有", result.after["rarity"])
        self.assertEqual("冰霜伤害提高 (6—10)%", result.guaranteed_modifier["text"])

    def test_低阶精华不能重置稀有物品(self) -> None:
        result = self.evaluate(
            "Whispering Essence of Hatred", rarity="稀有", item_level=30, item_class="项链"
        )
        self.assertEqual("incompatible", result.verdict)
        self.assertEqual("fail", next(c for c in result.checks if c.key == "rarity").status)

    def test_低阶精华物等超过上限明确不兼容(self) -> None:
        result = self.evaluate(
            "憎恨之低语精华", rarity="普通", item_level=36, item_class="戒指"
        )
        self.assertEqual("incompatible", result.verdict)
        self.assertIn("超过", next(c for c in result.checks if c.key == "item_level").summary)

    def test_有等级上限但缺物等保持unknown(self) -> None:
        result = self.evaluate("憎恨之低语精华", rarity="普通", item_class="项链")
        self.assertEqual("unknown", result.verdict)

    def test_高阶精华可重置稀有物品且无等级上限(self) -> None:
        result = self.evaluate(
            "憎恨之破空精华", rarity="稀有", item_level=84, item_class="项链"
        )
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("reroll_rare_properties", result.after["operation_mode"])
        self.assertIn("未给该精华设置物品等级上限", next(
            c for c in result.checks if c.key == "item_level"
        ).summary)

    def test_高阶精华也可把白装升级为稀有(self) -> None:
        result = self.evaluate(
            "憎恨之破空精华", rarity="普通", item_level=84, item_class="戒指"
        )
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("upgrade_normal_to_rare", result.after["operation_mode"])

    def test_高阶精华对魔法物品不兼容(self) -> None:
        result = self.evaluate(
            "憎恨之破空精华", rarity="魔法", item_level=84, item_class="戒指"
        )
        self.assertEqual("incompatible", result.verdict)

    def test_tier0例外按效果原文而不是等阶推断(self) -> None:
        result = self.evaluate("哀恸精华", rarity="稀有", item_level=15, item_class="项链")
        self.assertEqual("compatible", result.verdict)
        self.assertEqual("reroll_rare_properties", result.after["operation_mode"])

    def test_未知类别不把页面缺行推断为不可使用(self) -> None:
        result = self.evaluate(
            "憎恨之破空精华", rarity="稀有", item_level=84, item_class="地图"
        )
        self.assertEqual("unknown", result.verdict)
        self.assertIsNone(result.guaranteed_modifier)

    def test_来源原文漂移立即停止旧语义(self) -> None:
        self.conn.execute("UPDATE poedb_essence SET base_effect='上游已变化' WHERE metadata_id='high'")
        result = self.evaluate(
            "憎恨之破空精华", rarity="稀有", item_level=84, item_class="项链"
        )
        self.assertEqual("unknown", result.verdict)
        self.assertEqual("unchanged", result.after["modifier_identities"])
        self.assertIn("来源原文变化后的状态转移", result.unknown_fields)

    def test_特殊状态保持unknown(self) -> None:
        result = self.evaluate(
            "憎恨之破空精华", rarity="稀有", item_level=84, item_class="项链", corrupted=True
        )
        self.assertEqual("unknown", result.verdict)

    def test_不支持的相似名称不猜(self) -> None:
        self.assertIsNone(evaluate_essence_operation(
            self.conn, "超级憎恨精华", EssenceItemState(rarity="普通")
        ))


class EssenceOperationToolApiAndRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patch = patch("tools.impl.essence_operation.dbmod.get_conn", return_value=self.conn)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_工具输出确定性答案与严格schema(self) -> None:
        result = essence_operation_state(
            "憎恨之破空精华", rarity="稀有", item_level=84, item_class="项链"
        )
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertIn("冰霜伤害提高 (31—34)%", result.data["answer"])
        self.assertIn("不等于完整游戏可执行性", result.data["boundary"])
        self.assertEqual(1, len(result.citations))
        self.assertFalse(REGISTRY.spec("essence_operation_state").parameters["additionalProperties"])

    def test_工具可直接解析装备文本(self) -> None:
        text = (
            "物品类别: 项链\n稀有度: 稀有\n测试项链\n玛瑙护身符\n--------\n"
            "物品等级: 84\n--------\n+20 力量"
        )
        result = essence_operation_state("憎恨之破空精华", item_text=text)
        self.assertTrue(result.ok)
        self.assertEqual("compatible", result.data["verdict"])
        self.assertEqual(84, result.data["before"]["item_level"])

    def test_API与工具共用同一结果(self) -> None:
        response = essence_state(EssenceOperationRequest(
            essence="憎恨之低语精华", rarity="普通", item_level=35, item_class="项链"
        ), _caller="test")
        self.assertEqual("compatible", response.verdict)
        self.assertEqual("稀有", response.after["rarity"])

    def test_明确状态问法路由且价格与泛问不被抢(self) -> None:
        question = "物等84的稀有项链能用憎恨之破空精华吗"
        self.assertEqual("essence_operation_state", loop._preferred_tool_route(question))
        self.assertEqual({
            "essence": "憎恨之破空精华",
            "rarity": "稀有",
            "item_level": 84,
            "item_class": "项链",
        }, loop._essence_operation_args(question))
        self.assertIsNone(loop._essence_operation_args("憎恨之破空精华多少钱"))
        self.assertEqual("", loop._preferred_tool_route("憎恨之低语精华有什么用"))


if __name__ == "__main__":
    unittest.main()
