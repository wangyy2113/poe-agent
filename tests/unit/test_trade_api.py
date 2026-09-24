"""GGG Trade API 备用源：契约断言、过滤、稳健中位数与样本门槛。"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from economy.trade_api import TradeApiClient, TradeApiError


STATIC = {
    "result": [
        {
            "label": "Currency",
            "entries": [
                {"id": "chaos", "text": "Chaos Orb"},
                {"id": "divine", "text": "Divine Orb"},
            ],
        },
        {
            "label": "Fragments, Scarabs & Mapping",
            "entries": [
                {"id": "test-scarab", "text": "Test Scarab"},
                {"id": "dusk", "text": "Sacrifice at Dusk"},
            ],
        },
        {
            "label": "Cards",
            "entries": [{"id": "doctor", "text": "The Doctor"}],
        },
    ]
}


def listing(
    price: float,
    *,
    league: str = "Allflame",
    item_id: str = "divine",
    indexed: str = "2026-09-22T08:00:00+00:00",
) -> dict:
    return {
        "listing": {
            "indexed": indexed,
            "account": {"online": {"league": league}},
            "offers": [{
                "exchange": {"currency": "chaos", "amount": price},
                "item": {"currency": item_id, "amount": 1, "stock": 100},
            }],
        }
    }


def unique_listing(
    price: float,
    *,
    currency: str = "chaos",
    league: str = "Allflame",
    name: str = "Goldrim",
    base_type: str = "Leather Cap",
    corrupted: bool | None = None,
) -> dict:
    return {
        "listing": {
            "indexed": "2026-09-22T08:00:00Z",
            "account": {"online": {"league": league}},
            "price": {"type": "~price", "amount": price, "currency": currency},
        },
        "item": {
            "name": name,
            "typeLine": base_type,
            "corrupted": corrupted,
        },
    }


def rare_listing(
    price: float,
    *,
    currency: str = "chaos",
    league: str = "Allflame",
    base_type: str = "Amber Amulet",
    item_level: int = 84,
    corrupted: bool = False,
    frame_type: int = 2,
) -> dict:
    return {
        "listing": {
            "indexed": "2026-09-22T08:00:00Z",
            "account": {"online": {"league": league}},
            "price": {"type": "~price", "amount": price, "currency": currency},
        },
        "item": {
            "name": "Dread Eye",
            "typeLine": base_type,
            "frameType": frame_type,
            "ilvl": item_level,
            "corrupted": corrupted,
        },
    }


STATS = {
    "result": [{
        "id": "explicit",
        "label": "Explicit",
        "entries": [
            {"id": "explicit.stat_4080418644", "text": "+# to Strength", "type": "explicit"},
            {"id": "explicit.stat_3299347043", "text": "+# to maximum Life", "type": "explicit"},
        ],
    }]
}


class TradeApiClientTest(unittest.TestCase):
    def test_sell_side_reverses_exchange_and_requires_fresh_full_quantity_depth(self) -> None:
        now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
        calls: list[tuple[str, str, dict | None]] = []

        def bid_listing(
            rate: float, *, stock: float = 1_000, indexed: str = "2026-09-24T11:00:00Z"
        ) -> dict:
            return {"listing": {
                "indexed": indexed,
                "account": {"online": {"league": "Allflame"}},
                "offers": [{
                    "exchange": {"currency": "divine", "amount": 1},
                    "item": {"currency": "chaos", "amount": rate, "stock": stock},
                }],
            }}

        def request(method: str, path: str, payload: dict | None):
            calls.append((method, path, payload))
            if path == "/api/trade/data/static":
                return STATIC
            return {"result": {
                "a": bid_listing(350),
                "b": bid_listing(355),
                "c": bid_listing(360),
                "d": bid_listing(365),
                "e": bid_listing(370),
                "too-small": bid_listing(360, stock=100),
                "too-old": bid_listing(360, indexed="2026-09-20T11:00:00Z"),
            }}

        quote = TradeApiClient(
            request_json=request,
            requests_per_second=0,
            minimum_samples=5,
            now=lambda: now,
        ).quote_side(
            "Allflame",
            "Divine Orb",
            "currency",
            side="sell",
            quantity=2,
            max_listing_age=timedelta(hours=24),
        )

        self.assertEqual(360, quote.median_chaos)
        self.assertEqual("sell", quote.side)
        self.assertEqual(2, quote.requested_quantity)
        self.assertEqual(1, quote.insufficient_stock_filtered_count)
        self.assertEqual(1, quote.stale_listing_filtered_count)
        exchange = calls[-1][2]["exchange"]
        self.assertEqual(["divine"], exchange["have"])
        self.assertEqual(["chaos"], exchange["want"])

    def test_quote_side_rejects_unknown_direction_and_nonpositive_quantity(self) -> None:
        client = TradeApiClient(
            request_json=lambda _m, _p, _b: STATIC,
            requests_per_second=0,
        )
        with self.assertRaisesRegex(TradeApiError, "side"):
            client.quote_side(
                "Allflame", "Divine Orb", "currency", side="hold", quantity=1
            )
        with self.assertRaisesRegex(TradeApiError, "quantity"):
            client.quote_side(
                "Allflame", "Divine Orb", "currency", side="buy", quantity=0
            )

    def test_filters_cross_league_invalid_and_outlier_before_median(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []

        def request(method: str, path: str, payload: dict | None):
            calls.append((method, path, payload))
            if path == "/api/trade/data/static":
                return STATIC
            return {"result": {
                "a": listing(350, indexed="2026-09-22T08:00:00+00:00"),
                "b": listing(355, indexed="2026-09-22T08:01:00+00:00"),
                "c": listing(360, indexed="2026-09-22T08:02:00+00:00"),
                "d": listing(365, indexed="2026-09-22T08:03:00+00:00"),
                "e": listing(370, indexed="2026-09-22T08:04:00+00:00"),
                "bait": listing(5),
                "wrong-league": listing(1, league="Standard"),
                "broken": {"listing": {"account": {"online": {"league": "Allflame"}}}},
            }}

        client = TradeApiClient(
            request_json=request, requests_per_second=0, minimum_samples=5
        )
        quote = client.quote("Allflame", "Divine Orb", "currency")

        self.assertEqual(360, quote.median_chaos)
        self.assertEqual(350, quote.min_chaos)
        self.assertEqual(370, quote.max_chaos)
        self.assertEqual(5, quote.sample_count)
        self.assertEqual(8, quote.raw_listing_count)
        self.assertEqual(1, quote.cross_league_filtered_count)
        self.assertEqual(1, quote.invalid_quote_filtered_count)
        self.assertEqual(1, quote.outlier_filtered_count)
        self.assertEqual("2026-09-22T08:00:00Z", quote.earliest_listing_at)
        self.assertEqual("2026-09-22T08:04:00Z", quote.latest_listing_at)
        self.assertEqual("POST", calls[-1][0])
        self.assertEqual("/api/trade/exchange/Allflame", calls[-1][1])
        self.assertEqual(["chaos"], calls[-1][2]["exchange"]["have"])
        self.assertEqual(["divine"], calls[-1][2]["exchange"]["want"])

    def test_insufficient_samples_never_become_a_market_price(self) -> None:
        def request(_method: str, path: str, _payload: dict | None):
            if path == "/api/trade/data/static":
                return STATIC
            return {"result": {"a": listing(350), "b": listing(360)}}

        quote = TradeApiClient(
            request_json=request, requests_per_second=0, minimum_samples=3
        ).quote("Allflame", "Divine Orb", "currency")
        self.assertFalse(quote.sufficient)
        self.assertIsNone(quote.median_chaos)
        self.assertIsNone(quote.min_chaos)
        self.assertEqual(2, quote.sample_count)

    def test_static_dictionary_is_cached_for_configured_ttl(self) -> None:
        now = datetime(2026, 9, 22, tzinfo=timezone.utc)
        static_calls = 0

        def request(_method: str, path: str, _payload: dict | None):
            nonlocal static_calls
            if path == "/api/trade/data/static":
                static_calls += 1
                return STATIC
            return {"result": {
                str(i): listing(350 + i) for i in range(5)
            }}

        client = TradeApiClient(
            request_json=request,
            requests_per_second=0,
            static_ttl=timedelta(hours=1),
            now=lambda: now,
        )
        client.quote("Allflame", "Divine Orb", "currency")
        client.quote("Allflame", "Divine Orb", "currency")
        self.assertEqual(1, static_calls)

    def test_exact_name_and_category_are_required(self) -> None:
        client = TradeApiClient(
            request_json=lambda _m, _p, _b: STATIC,
            requests_per_second=0,
        )
        with self.assertRaises(TradeApiError):
            client.quote("Allflame", "Divine", "currency")
        with self.assertRaises(TradeApiError):
            client.quote("Allflame", "Divine Orb", "card")

    def test_supported_static_groups_map_to_public_categories(self) -> None:
        requested: list[dict | None] = []

        def request(_method: str, path: str, payload: dict | None):
            if path == "/api/trade/data/static":
                return STATIC
            requested.append(payload)
            item_id = payload["exchange"]["want"][0]
            return {"result": {
                str(i): listing(10 + i, item_id=item_id) for i in range(5)
            }}

        client = TradeApiClient(request_json=request, requests_per_second=0)
        self.assertEqual("scarab", client.quote("Allflame", "Test Scarab", "auto").category)
        self.assertEqual("fragment", client.quote("Allflame", "Sacrifice at Dusk", "fragment").category)
        self.assertEqual("card", client.quote("Allflame", "The Doctor", "card").category)
        self.assertEqual(3, len(requested))

    def test_malformed_static_and_exchange_responses_are_rejected(self) -> None:
        broken_static = TradeApiClient(
            request_json=lambda _m, _p, _b: {}, requests_per_second=0
        )
        with self.assertRaises(TradeApiError):
            broken_static.quote("Allflame", "Divine Orb", "currency")

        def request(_method: str, path: str, _payload: dict | None):
            return STATIC if path == "/api/trade/data/static" else {"result": []}

        with self.assertRaises(TradeApiError):
            TradeApiClient(request_json=request, requests_per_second=0).quote(
                "Allflame", "Divine Orb", "currency"
            )

    def test_league_contract_accepts_official_id_field(self) -> None:
        client = TradeApiClient(
            request_json=lambda _m, _p, _b: [{"id": "Allflame"}, {"id": "Standard"}],
            requests_per_second=0,
        )
        self.assertEqual(["Allflame", "Standard"], client.leagues())

    def test_unique_search_fetches_batches_and_normalizes_divine_prices(self) -> None:
        ids = [f"id-{index}" for index in range(12)]
        rows = {
            "id-0": unique_listing(340),
            "id-1": unique_listing(350),
            "id-2": unique_listing(360),
            "id-3": unique_listing(1, currency="divine"),
            "id-4": unique_listing(1.02, currency="divine"),
            "id-5": unique_listing(0.98, currency="divine"),
            "id-6": unique_listing(5),
            "id-7": unique_listing(1, league="Standard"),
            "id-8": unique_listing(1, corrupted=True),
            "id-9": unique_listing(1, currency="exalted"),
            "id-10": {"listing": {"account": {"online": {"league": "Allflame"}}}},
            "id-11": unique_listing(1, name="Not Goldrim"),
        }
        calls: list[tuple[str, str, dict | None]] = []

        def request(method: str, path: str, payload: dict | None):
            calls.append((method, path, payload))
            if path.startswith("/api/trade/search/"):
                return {"id": "search-token", "total": 100, "result": ids}
            if path.startswith("/api/trade/fetch/"):
                requested_ids = path.split("/fetch/", 1)[1].split("?", 1)[0].split(",")
                return {"result": [rows[item_id] for item_id in requested_ids]}
            if path == "/api/trade/data/static":
                return STATIC
            return {"result": {
                str(index): listing(value)
                for index, value in enumerate((350, 355, 360, 365, 370))
            }}

        quote = TradeApiClient(
            request_json=request,
            requests_per_second=0,
            minimum_samples=5,
            unique_max_results=12,
        ).unique_quote("Allflame", "Goldrim", "Leather Cap")

        self.assertAlmostEqual(356.4, quote.median_chaos)
        self.assertEqual(100, quote.search_total)
        self.assertEqual(12, quote.fetched_listing_count)
        self.assertEqual(4, quote.chaos_listing_count)
        self.assertEqual(3, quote.divine_listing_count)
        self.assertEqual(1, quote.cross_league_filtered_count)
        self.assertEqual(3, quote.invalid_quote_filtered_count)
        self.assertEqual(1, quote.unconverted_currency_filtered_count)
        self.assertEqual(1, quote.outlier_filtered_count)
        self.assertEqual(360, quote.divine_chaos_rate)
        self.assertEqual(5, quote.conversion_sample_count)
        self.assertEqual("converted", quote.conversion_status)
        self.assertIn("/trade/search/Allflame/search-token", quote.source_url)
        fetch_calls = [call for call in calls if call[1].startswith("/api/trade/fetch/")]
        self.assertEqual(2, len(fetch_calls))
        search_body = next(call[2] for call in calls if call[1].startswith("/api/trade/search/"))
        self.assertEqual("Goldrim", search_body["query"]["name"])
        self.assertEqual("Leather Cap", search_body["query"]["type"])
        self.assertEqual("false", search_body["query"]["filters"]["misc_filters"]["filters"]["corrupted"]["option"])

    def test_unique_divine_listings_are_ignored_when_conversion_sample_is_too_small(self) -> None:
        def request(_method: str, path: str, _payload: dict | None):
            if path.startswith("/api/trade/search/"):
                return {"id": "q", "total": 10, "result": [f"u{i}" for i in range(10)]}
            if path.startswith("/api/trade/fetch/"):
                return {"result": [
                    *[unique_listing(10 + index) for index in range(5)],
                    *[unique_listing(1, currency="divine") for _ in range(5)],
                ]}
            if path == "/api/trade/data/static":
                return STATIC
            return {"result": {"a": listing(350), "b": listing(360)}}

        quote = TradeApiClient(
            request_json=request, requests_per_second=0, minimum_samples=5
        ).unique_quote("Allflame", "Goldrim", "Leather Cap")
        self.assertEqual(12, quote.median_chaos)
        self.assertEqual(5, quote.sample_count)
        self.assertEqual(5, quote.unconverted_currency_filtered_count)
        self.assertEqual(2, quote.conversion_sample_count)
        self.assertEqual("insufficient_samples", quote.conversion_status)

    def test_unique_empty_search_is_auditable_insufficient_result(self) -> None:
        def request(_method: str, path: str, _payload: dict | None):
            if path.startswith("/api/trade/search/"):
                return {"id": "empty", "total": 0, "result": []}
            self.fail(f"unexpected request: {path}")

        quote = TradeApiClient(
            request_json=request, requests_per_second=0
        ).unique_quote("Allflame", "Goldrim", "Leather Cap")
        self.assertFalse(quote.sufficient)
        self.assertEqual(0, quote.search_total)
        self.assertEqual(0, quote.fetched_listing_count)

    def test_unique_search_and_fetch_contracts_are_asserted(self) -> None:
        broken_search = TradeApiClient(
            request_json=lambda _m, _p, _b: {"result": []}, requests_per_second=0
        )
        with self.assertRaisesRegex(TradeApiError, "查询 id"):
            broken_search.unique_quote("Allflame", "Goldrim", "Leather Cap")

        def request(_method: str, path: str, _payload: dict | None):
            if path.startswith("/api/trade/search/"):
                return {"id": "q", "total": 1, "result": ["u1"]}
            return {"result": {}}

        with self.assertRaisesRegex(TradeApiError, "unique fetch"):
            TradeApiClient(request_json=request, requests_per_second=0).unique_quote(
                "Allflame", "Goldrim", "Leather Cap"
            )

    def test_rare_search_uses_exact_metadata_and_presence_stats(self) -> None:
        ids = [f"r{i}" for i in range(8)]
        rows = {
            "r0": rare_listing(10),
            "r1": rare_listing(11),
            "r2": rare_listing(12),
            "r3": rare_listing(13),
            "r4": rare_listing(14),
            "r5": rare_listing(1, league="Standard"),
            "r6": rare_listing(1, item_level=83),
            "r7": rare_listing(1, frame_type=3),
        }
        calls: list[tuple[str, str, dict | None]] = []

        def request(method: str, path: str, payload: dict | None):
            calls.append((method, path, payload))
            if path == "/api/trade/data/stats":
                return STATS
            if path.startswith("/api/trade/search/"):
                return {"id": "rare-query", "total": len(ids), "result": ids}
            if path.startswith("/api/trade/fetch/"):
                requested = path.split("/fetch/", 1)[1].split("?", 1)[0].split(",")
                return {"result": [rows[item_id] for item_id in requested]}
            self.fail(f"unexpected request: {path}")

        quote = TradeApiClient(
            request_json=request, requests_per_second=0, minimum_samples=5
        ).rare_quote(
            "Allflame", "Amber Amulet", 84, False,
            ["explicit.stat_4080418644", "explicit.stat_3299347043"],
        )
        self.assertEqual(12, quote.median_chaos)
        self.assertEqual(5, quote.sample_count)
        self.assertEqual(1, quote.cross_league_filtered_count)
        self.assertEqual(2, quote.invalid_quote_filtered_count)
        self.assertIn("只约束词缀存在", quote.sample_scope)
        search = next(call[2] for call in calls if call[1].startswith("/api/trade/search/"))
        self.assertEqual("Amber Amulet", search["query"]["type"])
        self.assertEqual(
            "rare",
            search["query"]["filters"]["type_filters"]["filters"]["rarity"]["option"],
        )
        self.assertEqual(
            {"min": 84, "max": 84},
            search["query"]["filters"]["misc_filters"]["filters"]["ilvl"],
        )
        self.assertEqual(
            [{"id": "explicit.stat_4080418644"}, {"id": "explicit.stat_3299347043"}],
            search["query"]["stats"][0]["filters"],
        )

    def test_rare_rejects_hash_missing_from_official_dictionary(self) -> None:
        client = TradeApiClient(
            request_json=lambda _m, path, _p: STATS if path.endswith("/stats") else {},
            requests_per_second=0,
        )
        with self.assertRaisesRegex(TradeApiError, "没有显式 stat"):
            client.rare_quote(
                "Allflame", "Amber Amulet", 84, False, ["explicit.stat_999"]
            )

    def test_rare_divine_prices_use_audited_exchange_rate(self) -> None:
        ids = [f"r{i}" for i in range(8)]

        def request(_method: str, path: str, _payload: dict | None):
            if path == "/api/trade/data/stats":
                return STATS
            if path.startswith("/api/trade/search/"):
                return {"id": "rare-divine", "total": 8, "result": ids}
            if path.startswith("/api/trade/fetch/"):
                return {"result": [
                    *[rare_listing(value) for value in (340, 350, 360, 370, 380)],
                    *[rare_listing(1, currency="divine") for _ in range(3)],
                ]}
            if path == "/api/trade/data/static":
                return STATIC
            return {"result": {
                str(index): listing(value)
                for index, value in enumerate((350, 355, 360, 365, 370))
            }}

        quote = TradeApiClient(
            request_json=request, requests_per_second=0, minimum_samples=5
        ).rare_quote(
            "Allflame", "Amber Amulet", 84, False,
            ["explicit.stat_3299347043"],
        )
        self.assertEqual(360, quote.median_chaos)
        self.assertEqual(3, quote.divine_listing_count)
        self.assertEqual(360, quote.divine_chaos_rate)
        self.assertEqual("converted", quote.conversion_status)

    def test_rare_search_contract_is_asserted(self) -> None:
        def request(_method: str, path: str, _payload: dict | None):
            if path == "/api/trade/data/stats":
                return STATS
            return {"result": []}

        with self.assertRaisesRegex(TradeApiError, "查询 id"):
            TradeApiClient(request_json=request, requests_per_second=0).rare_quote(
                "Allflame", "Amber Amulet", 84, False,
                ["explicit.stat_3299347043"],
            )

    def test_stats_dictionary_contract_and_cache(self) -> None:
        calls = 0

        def request(_method: str, _path: str, _payload: dict | None):
            nonlocal calls
            calls += 1
            return STATS

        client = TradeApiClient(request_json=request, requests_per_second=0)
        self.assertEqual("+# to Strength", client.explicit_stats()["explicit.stat_4080418644"])
        client.explicit_stats()
        self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
