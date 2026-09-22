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


class TradeApiClientTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
