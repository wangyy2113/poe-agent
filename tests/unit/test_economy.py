"""实时经济价格：精确匹配、缓存、新鲜度、降级与确定性呈现。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from api.economy import PriceRequest, price as price_endpoint
from economy.poe_watch import PoeWatchClient, PoeWatchError
from economy.service import EconomyService, EconomyUnavailable, InvalidEconomyQuery
from kb.coverage import probe
from tools.impl.economy import render_price


SCHEMA = """
CREATE TABLE economy_snapshot(
 source TEXT NOT NULL, league TEXT NOT NULL, category TEXT NOT NULL,
 fetched_at TEXT NOT NULL, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
 PRIMARY KEY(source,league,category));
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

        def fetch(path: str, params: dict[str, str]):
            self.calls.append((path, params))
            if self.fail:
                raise PoeWatchError("upstream down")
            if path == "/leagues":
                return [{"name": "Allflame"}, {"name": "Standard"}]
            return [{
                "id": 1, "name": "Divine Orb", "category": "currency", "group": "currency",
                "mean": 370.25, "min": 365, "max": 375, "divine": 1,
                "daily": 42, "change": -2, "lowConfidence": True,
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

    def test_http_handler_exposes_structured_freshness_fields(self) -> None:
        with patch("api.economy.service_from_config", return_value=self.service):
            response = price_endpoint(
                PriceRequest(query="Divine Orb", category="currency"), _caller="test"
            )
        self.assertTrue(response.found)
        self.assertEqual("poe.watch", response.source)
        self.assertEqual("2026-09-21T04:00:00Z", response.fetched_at)
        self.assertFalse(response.stale)


if __name__ == "__main__":
    unittest.main()
