"""实时经济价格：精确匹配、缓存、新鲜度、降级与确定性呈现。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from api.economy import (
    MaterialCostItemRequest,
    MaterialCostRequest,
    PriceRequest,
    TrendRequest,
    material_cost as material_cost_endpoint,
    price as price_endpoint,
    trend as trend_endpoint,
)
from core import db as dbmod
from economy.models import MaterialRequest
from economy.poe_watch import PoeWatchClient, PoeWatchError
from economy.service import EconomyService, EconomyUnavailable, InvalidEconomyQuery
from economy.trade_api import TradeApiClient, TradeApiError
from kb.coverage import probe
from tools.impl.economy import (
    economy_price_lookup,
    economy_price_trend,
    render_material_cost,
    render_price,
    render_trend,
)


SCHEMA = """
CREATE TABLE economy_snapshot(
 source TEXT NOT NULL, league TEXT NOT NULL, category TEXT NOT NULL,
 fetched_at TEXT NOT NULL, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
 PRIMARY KEY(source,league,category));
CREATE TABLE economy_observation(
 source TEXT NOT NULL, league TEXT NOT NULL, category TEXT NOT NULL,
 item_key TEXT NOT NULL, item_id TEXT NOT NULL, item_name TEXT NOT NULL,
 item_group TEXT NOT NULL, observed_at TEXT NOT NULL, mean_chaos REAL,
 min_chaos REAL, max_chaos REAL, divine REAL, daily INTEGER,
 low_confidence INTEGER NOT NULL,
 PRIMARY KEY(source,league,category,item_key,observed_at));
CREATE TABLE entity(entity_id TEXT PRIMARY KEY, name_en TEXT, attributes TEXT NOT NULL);
CREATE TABLE alias(
 alias TEXT NOT NULL, alias_norm TEXT NOT NULL, entity_id TEXT NOT NULL, kind TEXT NOT NULL,
 PRIMARY KEY(alias_norm,entity_id));
"""


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class EconomyServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.clock = _Clock()
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fail = False
        self.mean = 370.25
        self.minimum = 365
        self.maximum = 375
        self.seven_day_history: list[float] = []

        def fetch(path: str, params: dict[str, str]):
            self.calls.append((path, params))
            if self.fail:
                raise PoeWatchError("upstream down")
            if path == "/leagues":
                return [{"name": "Allflame"}, {"name": "Standard"}]
            return [{
                "id": 1, "name": "Divine Orb", "category": "currency", "group": "currency",
                "mean": self.mean, "min": self.minimum, "max": self.maximum, "divine": 1,
                "daily": 42, "change": -2, "lowConfidence": True,
                "sevenDaysHistory": self.seven_day_history,
            }]

        self.service = EconomyService(
            self.conn,
            client=PoeWatchClient(fetch_json=fetch, requests_per_second=0),
            cache_ttl=timedelta(minutes=30),
            stale_after=timedelta(hours=6),
            now=self.clock,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def trade_client(
        self,
        calls: list[tuple[str, str, dict | None]],
        *,
        fail: bool = False,
        prices: tuple[float, ...] = (350, 355, 360, 365, 370, 5),
    ) -> TradeApiClient:
        def request(method: str, path: str, payload: dict | None):
            calls.append((method, path, payload))
            if fail:
                raise TradeApiError("trade down")
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
            rows = {}
            for index, value in enumerate(prices):
                rows[str(index)] = {"listing": {
                    "indexed": f"2026-09-22T08:0{index}:00+00:00",
                    "account": {"online": {"league": "Allflame"}},
                    "offers": [{
                        "exchange": {"currency": "chaos", "amount": value},
                        "item": {"currency": "divine", "amount": 1, "stock": 100},
                    }],
                }}
            rows["wrong-league"] = {"listing": {
                "indexed": "2026-09-22T08:09:00+00:00",
                "account": {"online": {"league": "Standard"}},
                "offers": [{
                    "exchange": {"currency": "chaos", "amount": 1},
                    "item": {"currency": "divine", "amount": 1, "stock": 100},
                }],
            }}
            return {"result": rows}

        return TradeApiClient(
            request_json=request,
            requests_per_second=0,
            minimum_samples=5,
            now=self.clock,
        )

    def test_fetches_current_league_and_returns_all_provenance(self) -> None:
        result = self.service.price("Divine Orb", category="currency")
        self.assertTrue(result.found)
        self.assertEqual("Allflame", result.league)
        self.assertEqual(370.25, result.prices[0].mean_chaos)
        self.assertTrue(result.prices[0].low_confidence)
        self.assertFalse(result.from_cache)
        self.assertFalse(result.stale)
        self.assertIn("低置信度", result.warning)
        self.assertEqual(["/leagues", "/get"], [path for path, _ in self.calls])

    def test_fresh_snapshot_avoids_network(self) -> None:
        self.service.price("Divine Orb", category="currency")
        self.calls.clear()
        result = self.service.price("Divine Orb", category="currency")
        self.assertTrue(result.from_cache)
        self.assertEqual([], self.calls)
        self.assertEqual(
            1,
            self.conn.execute("SELECT count(*) FROM economy_observation").fetchone()[0],
        )

    def test_expired_snapshot_refresh_failure_degrades_to_non_stale_cache(self) -> None:
        self.service.price("Divine Orb", category="currency")
        self.clock.value += timedelta(hours=1)
        self.fail = True
        result = self.service.price("Divine Orb", category="currency")
        self.assertTrue(result.from_cache)
        self.assertTrue(result.degraded)
        self.assertFalse(result.stale)
        self.assertIn("上游刷新失败", result.warning)

    def test_cache_older_than_stale_threshold_is_explicitly_marked(self) -> None:
        self.service.price("Divine Orb", category="currency")
        self.clock.value += timedelta(hours=7)
        self.fail = True
        result = self.service.price("Divine Orb", category="currency")
        self.assertTrue(result.stale)
        self.assertIn("陈旧快照", result.warning)
        self.assertIn("已陈旧", render_price(result))

    def test_no_cache_and_upstream_failure_is_unavailable(self) -> None:
        self.fail = True
        with self.assertRaises(EconomyUnavailable):
            self.service.price("Divine Orb", category="currency")

    def test_no_cache_primary_failure_uses_disclosed_trade_fallback(self) -> None:
        trade_calls: list[tuple[str, str, dict | None]] = []
        self.service.trade_client = self.trade_client(trade_calls)
        self.fail = True

        result = self.service.price("Divine Orb", category="currency")

        self.assertTrue(result.fallback)
        self.assertTrue(result.degraded)
        self.assertEqual("GGG Trade API", result.source)
        self.assertEqual(360, result.prices[0].median_chaos)
        self.assertEqual(360, result.prices[0].representative_chaos)
        self.assertIsNone(result.prices[0].mean_chaos)
        self.assertEqual(5, result.prices[0].sample_count)
        self.assertEqual(1, result.prices[0].cross_league_filtered_count)
        self.assertEqual(1, result.prices[0].outlier_filtered_count)
        rendered = render_price(result)
        self.assertIn("在线挂单中位数", rendered)
        self.assertIn("主源失败后使用官方备用源", rendered)
        self.assertIn("不是报价承诺或保证成交价", rendered)
        with patch("api.economy.service_from_config", return_value=self.service):
            response = price_endpoint(
                PriceRequest(query="Divine Orb", category="currency"), _caller="test"
            )
        self.assertTrue(response.fallback)
        self.assertEqual("poe.watch 无可用缓存且刷新失败", response.fallback_reason)
        with patch("tools.impl.economy.service_from_config", return_value=self.service):
            tool_result = economy_price_lookup("Divine Orb", category="currency")
        self.assertTrue(tool_result.ok)
        self.assertEqual("GGG Trade API", tool_result.citations[0]["source"])
        self.assertEqual(1, tool_result.citations[0]["source_priority"])

    def test_existing_primary_cache_wins_over_trade_fallback(self) -> None:
        self.service.price("Divine Orb", category="currency")
        self.clock.value += timedelta(hours=7)
        self.fail = True
        trade_calls: list[tuple[str, str, dict | None]] = []
        self.service.trade_client = self.trade_client(trade_calls)

        result = self.service.price("Divine Orb", category="currency")

        self.assertEqual("poe.watch", result.source)
        self.assertFalse(result.fallback)
        self.assertTrue(result.stale)
        self.assertEqual([], trade_calls)

    def test_unique_item_uses_exact_name_and_base_type_fallback(self) -> None:
        self.conn.execute(
            "INSERT INTO entity(entity_id,name_en,attributes) VALUES(?,?,?)",
            ("unique_item:Headhunter", "Headhunter", json.dumps({"base_type_en": "Leather Belt"})),
        )
        self.conn.execute(
            "INSERT INTO alias(alias,alias_norm,entity_id,kind) VALUES(?,?,?,?)",
            ("猎首", "猎首", "unique_item:Headhunter", "name_zh"),
        )
        trade_calls: list[tuple[str, str, dict | None]] = []

        def request(method: str, path: str, payload: dict | None):
            trade_calls.append((method, path, payload))
            if path.startswith("/api/leagues"):
                return [{"id": "Allflame"}]
            if path.startswith("/api/trade/search/"):
                self.assertEqual("Headhunter", payload["query"]["name"])
                self.assertEqual("Leather Belt", payload["query"]["type"])
                return {"id": "query-id", "total": 5, "result": [f"u{i}" for i in range(5)]}
            if path.startswith("/api/trade/fetch/"):
                return {"result": [{
                    "listing": {
                        "indexed": "2026-09-22T08:00:00Z",
                        "account": {"online": {"league": "Allflame"}},
                        "price": {"type": "~price", "amount": 100 + index, "currency": "chaos"},
                    },
                    "item": {"name": "Headhunter", "typeLine": "Leather Belt"},
                } for index in range(5)]}
            self.fail(f"unexpected request: {path}")

        self.service.trade_client = TradeApiClient(
            request_json=request,
            requests_per_second=0,
            minimum_samples=5,
            now=self.clock,
        )
        self.fail = True

        result = self.service.price("猎首")
        self.assertEqual("Headhunter", result.resolved_name)
        self.assertEqual("unique", result.prices[0].category)
        self.assertEqual("unique_listing", result.prices[0].group)
        self.assertEqual(102, result.prices[0].median_chaos)
        self.assertIn("未腐化", result.prices[0].sample_scope)
        self.assertIn("/trade/search/Allflame/query-id", result.source_url)
        rendered = render_price(result)
        self.assertIn("搜索结果：总命中 5 条，本次抓取 5 条", rendered)
        self.assertIn("未归一化词缀数值、插槽或链接", rendered)
        self.assertIn("不是报价承诺或保证成交价", rendered)
        with patch("api.economy.service_from_config", return_value=self.service):
            response = price_endpoint(PriceRequest(query="猎首"), _caller="test")
        self.assertTrue(response.fallback)
        self.assertEqual(102, response.prices[0]["representative_chaos"])
        with patch("tools.impl.economy.service_from_config", return_value=self.service):
            tool_result = economy_price_lookup("猎首")
        self.assertTrue(tool_result.ok)
        self.assertIn("/trade/search/Allflame/query-id", tool_result.citations[0]["url"])

    def test_unique_fallback_requires_authoritative_base_type(self) -> None:
        trade_calls: list[tuple[str, str, dict | None]] = []
        self.service.trade_client = self.trade_client(trade_calls)
        self.fail = True
        with self.assertRaisesRegex(EconomyUnavailable, "缺少唯一装备底材"):
            self.service.price("Unknown Unique", category="unique")
        self.assertEqual(1, len(trade_calls))  # 只读取联盟，没有发起模糊装备搜索。

    def test_both_sources_failing_remains_unavailable(self) -> None:
        trade_calls: list[tuple[str, str, dict | None]] = []
        self.service.trade_client = self.trade_client(trade_calls, fail=True)
        self.fail = True
        with self.assertRaisesRegex(EconomyUnavailable, "备用查询也不可用"):
            self.service.price("Divine Orb", category="currency")

    def test_insufficient_trade_samples_are_disclosed_without_price(self) -> None:
        trade_calls: list[tuple[str, str, dict | None]] = []
        self.service.trade_client = self.trade_client(trade_calls, prices=(350, 360))
        self.fail = True
        result = self.service.price("Divine Orb", category="currency")
        point = result.prices[0]
        self.assertIsNone(point.representative_chaos)
        self.assertTrue(point.low_confidence)
        self.assertIn("低于最低样本数 5", result.warning)
        self.assertIn("不输出代表价", render_price(result))

    def test_material_cost_can_use_trade_median_without_calling_it_a_mean(self) -> None:
        trade_calls: list[tuple[str, str, dict | None]] = []
        self.service.trade_client = self.trade_client(trade_calls)
        self.fail = True
        result = self.service.material_cost([
            MaterialRequest("Divine Orb", 2, "currency"),
            MaterialRequest("Chaos Orb", 100, "currency"),
        ])
        self.assertTrue(result.calculable)
        self.assertTrue(result.fallback)
        self.assertEqual(820, result.total_representative_chaos)
        self.assertIsNone(result.total_mean_chaos)
        rendered = render_material_cost(result)
        self.assertIn("代表价总成本：820 混沌石", rendered)
        self.assertIn("官方在线挂单中位数", rendered)

    def test_invalid_league_is_rejected_instead_of_silently_switching(self) -> None:
        with self.assertRaises(InvalidEconomyQuery):
            self.service.price("Divine Orb", category="currency", league="No Such League")

    def test_not_found_does_not_fuzzy_guess(self) -> None:
        result = self.service.price("Divine Or", category="currency")
        self.assertFalse(result.found)
        self.assertIn("没有进行模糊匹配", render_price(result))

    def test_chinese_unique_alias_uses_only_the_accessory_partition(self) -> None:
        self.conn.execute(
            "INSERT INTO entity(entity_id,name_en,attributes) VALUES(?,?,?)",
            ("unique_item:Headhunter", "Headhunter", json.dumps({"base_type_en": "Leather Belt"})),
        )
        self.conn.execute(
            "INSERT INTO alias(alias,alias_norm,entity_id,kind) VALUES(?,?,?,?)",
            ("猎首", "猎首", "unique_item:Headhunter", "name_zh"),
        )
        self.conn.execute(
            "INSERT INTO entity(entity_id,name_en,attributes) VALUES(?,?,?)",
            ("currency:Chaos_Orb", "Chaos Orb", "{}"),
        )
        self.conn.execute(
            "INSERT INTO alias(alias,alias_norm,entity_id,kind) VALUES(?,?,?,?)",
            ("混沌石", "混沌石", "currency:Chaos_Orb", "name_zh"),
        )

        def fetch(path: str, params: dict[str, str]):
            if path == "/leagues":
                return [{"name": "Allflame"}]
            self.assertEqual("accessory", params["category"])
            return [{
                "id": 2, "name": "Headhunter", "category": "accessory", "group": "unique",
                "mean": 100, "min": 90, "max": 110, "divine": 0.3,
                "daily": 5, "change": 0, "lowConfidence": False,
            }]

        service = EconomyService(
            self.conn,
            client=PoeWatchClient(fetch_json=fetch, requests_per_second=0),
            now=self.clock,
        )
        result = service.price("猎首现在值多少混沌石")
        self.assertEqual("Headhunter", result.resolved_name)
        self.assertEqual(("accessory",), result.categories_searched)
        self.assertTrue(result.found)

    def test_render_never_calls_mean_a_guaranteed_trade_price(self) -> None:
        text = render_price(self.service.price("Divine Orb", category="currency"))
        self.assertIn("市场统计快照", text)
        self.assertIn("不是报价承诺或保证成交价", text)
        self.assertIn("Allflame", text)
        self.assertIn("2026-09-21T04:00:00Z", text)

    def test_coverage_declares_live_price_capability_without_cached_rows(self) -> None:
        status = next(item for item in probe(self.conn) if item.key == "economy_price")
        self.assertEqual("partial", status.status)
        self.assertEqual(0, status.item_count)
        self.assertIn("economy_price_lookup", status.spec.tools)
        self.assertIn("economy_price_trend", status.spec.tools)
        self.assertIn("economy_material_cost", status.spec.tools)

    def test_http_handler_exposes_structured_freshness_fields(self) -> None:
        with patch("api.economy.service_from_config", return_value=self.service):
            response = price_endpoint(
                PriceRequest(query="Divine Orb", category="currency"), _caller="test"
            )
        self.assertTrue(response.found)
        self.assertEqual("poe.watch", response.source)
        self.assertEqual("2026-09-21T04:00:00Z", response.fetched_at)
        self.assertFalse(response.stale)

    def test_trend_uses_two_saved_snapshots_and_not_upstream_change(self) -> None:
        self.service.price("Divine Orb", category="currency")
        self.clock.value += timedelta(minutes=31)
        self.mean = 400
        self.minimum = 395
        self.maximum = 405

        result = self.service.trend(
            "Divine Orb", category="currency", window_hours=24
        )

        self.assertTrue(result.analyzable)
        trend = result.trends[0]
        self.assertEqual("ready", trend.status)
        self.assertEqual(2, trend.observation_count)
        self.assertEqual(370.25, trend.first_mean_chaos)
        self.assertEqual(400, trend.last_mean_chaos)
        self.assertEqual(29.75, trend.change_chaos)
        self.assertEqual("up", trend.direction)
        self.assertAlmostEqual(8.035, trend.change_percent, places=3)
        self.assertIn("本机保存的带时间戳快照", render_trend(result))
        self.assertNotIn("-2%", render_trend(result))

    def test_trend_refuses_to_infer_from_one_observation(self) -> None:
        result = self.service.trend("Divine Orb", category="currency")
        self.assertFalse(result.analyzable)
        self.assertEqual("insufficient_data", result.trends[0].status)
        self.assertEqual(1, result.trends[0].observation_count)
        self.assertIsNone(result.trends[0].change_percent)
        self.assertIn("暂不可判定", render_trend(result))

    def test_default_window_can_use_explicit_upstream_seven_day_series(self) -> None:
        self.seven_day_history = [300, 310, 320, 330, 340, 360, 370.25]
        result = self.service.trend("Divine Orb", category="currency")
        trend = result.trends[0]
        self.assertTrue(result.analyzable)
        self.assertEqual("poe_watch_seven_day_history", trend.basis)
        self.assertFalse(trend.timestamps_available)
        self.assertEqual(7, trend.observation_count)
        self.assertEqual(70.25, trend.change_chaos)
        self.assertEqual(tuple(self.seven_day_history), trend.series_values)
        self.assertIn("上游未提供逐点时间戳", render_trend(result))

    def test_trend_window_excludes_old_observations(self) -> None:
        self.service.price("Divine Orb", category="currency")
        self.clock.value += timedelta(days=2)
        self.mean = 400
        result = self.service.trend(
            "Divine Orb", category="currency", window_hours=24
        )
        self.assertFalse(result.analyzable)
        self.assertEqual(1, result.trends[0].observation_count)

    def test_trend_rejects_invalid_window(self) -> None:
        for value in (0, 2161, True, 1.5):
            with self.subTest(value=value), self.assertRaises(InvalidEconomyQuery):
                self.service.trend(
                    "Divine Orb", category="currency", window_hours=value  # type: ignore[arg-type]
                )

    def test_observation_retention_prunes_old_points(self) -> None:
        self.service.price("Divine Orb", category="currency")
        self.clock.value += timedelta(days=91)
        self.mean = 400
        self.service.price("Divine Orb", category="currency")
        rows = self.conn.execute(
            "SELECT observed_at FROM economy_observation ORDER BY observed_at"
        ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual("2026-12-21T04:00:00Z", rows[0]["observed_at"])

    def test_trend_calculates_all_points_but_caps_returned_series(self) -> None:
        self.service.max_trend_points = 3
        for index, mean in enumerate((100, 110, 120, 130, 140)):
            self.mean = mean
            self.service.price("Divine Orb", category="currency")
            if index < 4:
                self.clock.value += timedelta(minutes=31)
        trend = self.service.trend(
            "Divine Orb", category="currency", window_hours=24
        ).trends[0]
        self.assertEqual(5, trend.observation_count)
        self.assertEqual((100, 120, 140), trend.series_values)
        self.assertEqual(3, len(trend.observations))
        self.assertEqual(40, trend.change_chaos)

    def test_trend_http_handler_exposes_observations(self) -> None:
        self.service.price("Divine Orb", category="currency")
        self.clock.value += timedelta(minutes=31)
        self.mean = 400
        with patch("api.economy.service_from_config", return_value=self.service):
            response = trend_endpoint(
                TrendRequest(query="Divine Orb", category="currency", window_hours=24),
                _caller="test",
            )
        self.assertTrue(response.analyzable)
        self.assertEqual(2, response.trends[0]["observation_count"])
        self.assertEqual("poe.watch", response.source)

    def test_trend_tool_returns_deterministic_answer_and_citation(self) -> None:
        self.service.price("Divine Orb", category="currency")
        self.clock.value += timedelta(minutes=31)
        self.mean = 400
        with patch("tools.impl.economy.service_from_config", return_value=self.service):
            result = economy_price_trend(
                "Divine Orb", category="currency", window_hours=24
            )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["analyzable"])
        self.assertIn("上涨 29.75 混沌石", result.data["answer"])
        self.assertEqual("live_economy", result.citations[0]["channel"])

    def test_production_schema_contains_observation_contract(self) -> None:
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(dbmod.SCHEMA)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(economy_observation)")
        }
        self.assertEqual(
            {
                "source", "league", "category", "item_key", "item_id", "item_name",
                "item_group", "observed_at", "mean_chaos", "min_chaos", "max_chaos",
                "divine", "daily", "low_confidence",
            },
            columns,
        )

    def test_material_cost_batches_shared_snapshots_and_sums_all_axes(self) -> None:
        result = self.service.material_cost([
            MaterialRequest("Divine Orb", 2, "currency"),
            MaterialRequest("Chaos Orb", 100, "currency"),
        ])
        self.assertTrue(result.calculable)
        self.assertEqual(840.5, result.total_mean_chaos)
        self.assertEqual(830, result.total_min_chaos)
        self.assertEqual(850, result.total_max_chaos)
        self.assertEqual(0, result.unresolved_count)
        self.assertEqual(["/leagues", "/get"], [path for path, _ in self.calls])
        self.assertEqual("priced", result.materials[0].status)
        self.assertEqual(740.5, result.materials[0].subtotal_mean_chaos)
        self.assertTrue(result.materials[0].low_confidence)
        self.assertEqual("quote_unit", result.materials[1].unit_price.group)

        rendered = render_material_cost(result)
        self.assertIn("均值总成本：840.5 混沌石", rendered)
        self.assertIn("数量乘法与总和由代码计算", rendered)
        self.assertIn("不是报价承诺", rendered)

    def test_chaos_orb_is_explicit_quote_unit_when_upstream_omits_it(self) -> None:
        result = self.service.price("Chaos Orb", category="currency")
        self.assertTrue(result.found)
        self.assertEqual(1, result.prices[0].mean_chaos)
        self.assertEqual("quote_unit", result.prices[0].group)
        self.assertIn("单位恒等式", render_price(result))

    def test_material_cost_never_presents_partial_list_as_total(self) -> None:
        result = self.service.material_cost([
            MaterialRequest("Divine Orb", 1, "currency"),
            MaterialRequest("Definitely Missing", 3, "currency"),
        ])
        self.assertFalse(result.calculable)
        self.assertIsNone(result.total_mean_chaos)
        self.assertEqual(1, result.unresolved_count)
        self.assertEqual("not_found", result.materials[1].status)
        self.assertIn("总成本不可计算", render_material_cost(result))
        self.assertNotIn("均值总成本：370.25", render_material_cost(result))

    def test_material_cost_rejects_invalid_or_empty_inputs(self) -> None:
        with self.assertRaises(InvalidEconomyQuery):
            self.service.material_cost([])
        with self.assertRaises(InvalidEconomyQuery):
            self.service.material_cost([MaterialRequest("Divine Orb", float("nan"))])
        with self.assertRaises(InvalidEconomyQuery):
            self.service.material_cost([MaterialRequest("Divine Orb", 0)])

    def test_material_cost_http_handler_returns_structured_line_provenance(self) -> None:
        request = MaterialCostRequest(materials=[
            MaterialCostItemRequest(query="Divine Orb", quantity=2, category="currency"),
            MaterialCostItemRequest(query="Chaos Orb", quantity=100, category="currency"),
        ])
        with patch("api.economy.service_from_config", return_value=self.service):
            response = material_cost_endpoint(request, _caller="test")
        self.assertTrue(response.calculable)
        self.assertEqual(840.5, response.total_mean_chaos)
        self.assertEqual("2026-09-21T04:00:00Z", response.materials[0]["fetched_at"])
        self.assertEqual("poe.watch", response.materials[1]["source"])


if __name__ == "__main__":
    unittest.main()
