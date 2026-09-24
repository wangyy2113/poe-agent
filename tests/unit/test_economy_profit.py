"""双边市场利润：深度、时效、费用与 unknown 闸门。"""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from agents.knowledge import ALLOWED_TOOLS
from api.economy import (
    FlipAnalysisRequest,
    TradeFeeRequest,
    flip_analysis as flip_analysis_endpoint,
)
from economy.models import TradeFeeInput
from economy.poe_watch import PoeWatchClient
from economy.service import EconomyService, InvalidEconomyQuery
from economy.trade_api import TradeApiClient
from orchestrator import loop
from tools.impl.economy import economy_flip_analysis, render_flip_analysis


SCHEMA = """
CREATE TABLE entity(entity_id TEXT PRIMARY KEY, name_en TEXT, attributes TEXT NOT NULL);
CREATE TABLE alias(
 alias TEXT NOT NULL, alias_norm TEXT NOT NULL, entity_id TEXT NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(alias_norm,entity_id));
"""


class EconomyProfitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO entity(entity_id,name_en,attributes) VALUES(?,?,?)",
            ("currency:Divine_Orb", "Divine Orb", "{}"),
        )
        self.conn.execute(
            "INSERT INTO alias(alias,alias_norm,entity_id,kind) VALUES(?,?,?,?)",
            ("神圣石", "神圣石", "currency:Divine_Orb", "name_zh"),
        )
        self.ask_prices = [10, 11, 12, 13, 14]
        self.bid_prices = [15, 16, 17, 18, 19]
        self.bid_stock = 1_000.0
        self.calls: list[dict] = []

        def request(_method: str, path: str, payload: dict | None):
            if path.startswith("/api/leagues"):
                return [{"id": "Allflame"}, {"id": "Standard"}]
            if path == "/api/trade/data/static":
                return {"result": [{
                    "label": "Currency",
                    "entries": [
                        {"id": "chaos", "text": "Chaos Orb"},
                        {"id": "divine", "text": "Divine Orb"},
                    ],
                }]}
            assert payload is not None
            exchange = payload["exchange"]
            self.calls.append(exchange)
            buy = exchange["have"] == ["chaos"]
            prices = self.ask_prices if buy else self.bid_prices
            rows = {}
            for index, price in enumerate(prices):
                if buy:
                    offer = {
                        "exchange": {"currency": "chaos", "amount": price},
                        "item": {"currency": "divine", "amount": 1, "stock": 100},
                    }
                else:
                    offer = {
                        "exchange": {"currency": "divine", "amount": 1},
                        "item": {
                            "currency": "chaos", "amount": price,
                            "stock": self.bid_stock,
                        },
                    }
                rows[str(index)] = {"listing": {
                    "indexed": f"2026-09-24T11:0{index}:00Z",
                    "account": {"online": {"league": "Allflame"}},
                    "offers": [offer],
                }}
            return {"result": rows}

        trade_client = TradeApiClient(
            request_json=request,
            requests_per_second=0,
            minimum_samples=5,
            now=lambda: self.now,
        )
        self.service = EconomyService(
            self.conn,
            client=PoeWatchClient(fetch_json=lambda _p, _q: [], requests_per_second=0),
            trade_client=trade_client,
            now=lambda: self.now,
        )
        self.fees = TradeFeeInput(
            buy_fixed_chaos=2,
            buy_percent=1,
            sell_fixed_chaos=3,
            sell_percent=2,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_complete_two_sided_evidence_calculates_profit_and_threshold_verdict(self) -> None:
        result = self.service.flip_analysis(
            "神圣石",
            quantity=10,
            fees=self.fees,
            minimum_net_profit_chaos=40,
            minimum_margin_percent=30,
        )

        self.assertTrue(result.calculable)
        self.assertEqual("ready", result.status)
        self.assertEqual("flip_candidate", result.verdict)
        self.assertEqual("Divine Orb", result.resolved_name)
        self.assertEqual(12, result.buy_side.unit_chaos)
        self.assertEqual(17, result.sell_side.unit_chaos)
        self.assertEqual(120, result.gross_buy_cost_chaos)
        self.assertEqual(3.2, result.buy_fee_chaos)
        self.assertEqual(123.2, result.total_buy_cost_chaos)
        self.assertEqual(170, result.gross_sell_revenue_chaos)
        self.assertEqual(6.4, result.sell_fee_chaos)
        self.assertAlmostEqual(40.4, result.net_profit_chaos)
        self.assertAlmostEqual(32.79220779, result.margin_percent)
        self.assertEqual(["chaos"], self.calls[0]["have"])
        self.assertEqual(["divine"], self.calls[1]["have"])
        rendered = render_flip_analysis(result)
        self.assertIn("可列为 Flip 候选", rendered)
        self.assertIn("不是成交记录或收益保证", rendered)

    def test_below_user_threshold_is_not_profitable_even_with_positive_spread(self) -> None:
        result = self.service.flip_analysis(
            "Divine Orb",
            quantity=10,
            fees=self.fees,
            category="currency",
            minimum_net_profit_chaos=50,
        )
        self.assertTrue(result.calculable)
        self.assertGreater(result.net_profit_chaos or 0, 0)
        self.assertEqual("not_profitable", result.verdict)

    def test_missing_sell_depth_returns_unknown_without_partial_profit(self) -> None:
        self.bid_stock = 10  # 17 chaos stock只能承接不足 1 个 Divine。
        result = self.service.flip_analysis(
            "Divine Orb", quantity=10, fees=self.fees, category="currency"
        )

        self.assertFalse(result.calculable)
        self.assertEqual("unknown", result.status)
        self.assertEqual("unknown", result.verdict)
        self.assertEqual("insufficient_liquidity", result.sell_side.status)
        self.assertEqual(5, result.sell_side.insufficient_stock_filtered_count)
        self.assertIsNone(result.gross_buy_cost_chaos)
        self.assertIsNone(result.net_profit_chaos)
        self.assertIsNone(result.margin_percent)
        rendered = render_flip_analysis(result)
        self.assertIn("净利润、利润率与 Flip 判断均不计算", rendered)
        self.assertNotIn("毛成本 120", rendered)

    def test_api_and_tool_preserve_structured_evidence(self) -> None:
        request = FlipAnalysisRequest(
            query="Divine Orb",
            quantity=10,
            category="currency",
            fees=TradeFeeRequest(
                buy_fixed_chaos=2,
                buy_percent=1,
                sell_fixed_chaos=3,
                sell_percent=2,
            ),
        )
        with patch("api.economy.service_from_config", return_value=self.service):
            response = flip_analysis_endpoint(request, _caller="test")
        self.assertTrue(response.calculable)
        self.assertEqual(5, response.buy_side["sample_count"])
        self.assertEqual("sell", response.sell_side["side"])

        with patch("tools.impl.economy.service_from_config", return_value=self.service):
            tool_result = economy_flip_analysis(
                "Divine Orb",
                10,
                {
                    "buy_fixed_chaos": 2,
                    "buy_percent": 1,
                    "sell_fixed_chaos": 3,
                    "sell_percent": 2,
                },
                category="currency",
            )
        self.assertTrue(tool_result.ok)
        self.assertEqual("live_economy", tool_result.citations[0]["channel"])
        self.assertEqual("flip_candidate", tool_result.meta["verdict"])

    def test_invalid_fees_age_and_unique_category_are_rejected(self) -> None:
        with self.assertRaises(InvalidEconomyQuery):
            self.service.flip_analysis(
                "Divine Orb",
                quantity=1,
                fees=TradeFeeInput(0, 100, 0, 0),
                category="currency",
            )
        with self.assertRaises(InvalidEconomyQuery):
            self.service.flip_analysis(
                "Divine Orb",
                quantity=1,
                fees=TradeFeeInput(0, 0, 0, 0),
                category="currency",
                max_listing_age_hours=0,
            )
        with self.assertRaises(InvalidEconomyQuery):
            self.service.flip_analysis(
                "Headhunter",
                quantity=1,
                fees=TradeFeeInput(0, 0, 0, 0),
                category="unique",  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(InvalidEconomyQuery, "计价单位"):
            self.service.flip_analysis(
                "Chaos Orb",
                quantity=1,
                fees=TradeFeeInput(0, 0, 0, 0),
                category="currency",
            )

    def test_profit_intent_is_exposed_and_routes_before_plain_price(self) -> None:
        self.assertIn("economy_flip_analysis", ALLOWED_TOOLS)
        self.assertIn("economy_flip_analysis", loop._DETERMINISTIC_DIRECT_TOOLS)
        self.assertEqual(
            "economy_flip_analysis",
            loop._preferred_tool_route("神圣石倒卖利润是多少"),
        )


if __name__ == "__main__":
    unittest.main()
