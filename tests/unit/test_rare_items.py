"""稀有装备可比挂单：底材/词缀映射、保守边界与审计输出。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.economy import (  # noqa: E402
    RareComparableRequest,
    rare_comparables as rare_comparables_endpoint,
)
from economy.rare_items import InvalidRareComparable, RareComparableService  # noqa: E402
from economy.trade_api import TradeApiClient  # noqa: E402
from tests.unit.test_item_mods import fixture_conn, make_item  # noqa: E402
from tests.unit.test_trade_api import STATS, rare_listing  # noqa: E402
from tools.impl.economy import economy_rare_comparables, render_rare_comparables  # noqa: E402


def _insert_base(conn, name_en: str = "Test Base", name_zh: str = "测试底材") -> None:
    conn.execute(
        """INSERT INTO pob_base_item(
               base_id,name_en,name_zh,poedb_slug,category_key,poedb_page,item_class,
               subtype,socket_limit,tags,influence_tags,armour_tag,modifier_page,
               implicit_text,implicit_mod_types,implicit_ids,stats,stats_kind,requirements,
               source_file,source,source_priority,pob_url,poedb_url,source_commit,
               pob_version,game_version,content_hash,updated_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "Metadata/Items/TestBase", name_en, name_zh, "Test_Base", "amulets",
            "Amulets", "Amulet", "", 1, '["amulet","default"]', "[]", "",
            "", None, "[]", "[]", "{}", "none", "{}",
            "Data/Bases/amulet.lua", "Path of Building", 2,
            "https://github.com/example/pob", "https://poedb.tw/cn/Test_Base",
            "a" * 40, "PoB test", "3.29", "hash", "2026-09-22T00:00:00Z",
        ),
    )
    conn.commit()


class RareComparableServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fixture_conn()
        _insert_base(self.conn)
        self.conn.execute(
            "UPDATE pob_mod SET trade_hashes='[3299347043]' "
            "WHERE mod_group='IncreasedLife' AND mod_kind='explicit'"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def _service(self, *, listing_count: int = 5) -> tuple[RareComparableService, list]:
        calls: list[tuple[str, str, dict | None]] = []

        def request(method: str, path: str, payload: dict | None):
            calls.append((method, path, payload))
            if path == "/api/trade/data/stats":
                return STATS
            if path.startswith("/api/leagues"):
                return [{"id": "Standard"}, {"id": "Allflame"}]
            if path.startswith("/api/trade/search/"):
                ids = [f"r{i}" for i in range(listing_count)]
                return {"id": "q", "total": listing_count, "result": ids}
            if path.startswith("/api/trade/fetch/"):
                ids = path.split("/fetch/", 1)[1].split("?", 1)[0].split(",")
                return {"result": [
                    rare_listing(10 + index, base_type="Test Base")
                    for index, _item_id in enumerate(ids)
                ]}
            self.fail(f"unexpected request: {path}")

        client = TradeApiClient(
            request_json=request, requests_per_second=0, minimum_samples=5
        )
        return RareComparableService(
            self.conn,
            client,
            now=lambda: datetime(2026, 9, 22, tzinfo=timezone.utc),
        ), calls

    def test_chinese_item_maps_base_and_trade_hash(self) -> None:
        service, calls = self._service()
        result = service.estimate(make_item("项链", ["+107 最大生命"]))
        self.assertTrue(result.available)
        self.assertEqual("Allflame", result.league)
        self.assertEqual("Test Base", result.base_type_en)
        self.assertEqual("full", result.comparison_quality)
        self.assertEqual(1, result.mapped_mod_count)
        self.assertEqual(("explicit.stat_3299347043",), result.mapped_stats[0].stat_ids)
        self.assertEqual(12, result.quote.median_chaos if result.quote else None)
        search = next(call[2] for call in calls if call[1].startswith("/api/trade/search/"))
        self.assertEqual("Test Base", search["query"]["type"])

    def test_unmapped_mod_is_disclosed_and_marks_partial(self) -> None:
        service, _calls = self._service()
        result = service.estimate(
            make_item("项链", ["+107 最大生命", "+50 最大魔力"]),
            league="Allflame",
        )
        self.assertEqual("partial", result.comparison_quality)
        self.assertEqual(("+50 最大魔力",), result.unmapped_mods)
        self.assertIn("部分可比", result.reason)

    def test_no_mapped_mod_returns_auditable_result_without_search(self) -> None:
        service, calls = self._service()
        result = service.estimate(make_item("项链", ["+50 最大魔力"]))
        self.assertEqual("unmapped", result.status)
        self.assertFalse(result.available)
        self.assertIsNone(result.quote)
        self.assertFalse(any(path.startswith("/api/trade/search/") for _, path, _ in calls))

    def test_insufficient_samples_never_outputs_price(self) -> None:
        service, _calls = self._service(listing_count=2)
        result = service.estimate(make_item("项链", ["+107 最大生命"]))
        self.assertEqual("insufficient_samples", result.status)
        self.assertFalse(result.available)
        self.assertIsNone(result.quote.median_chaos if result.quote else None)

    def test_special_item_state_is_rejected_until_query_contract_exists(self) -> None:
        service, _calls = self._service()
        text = make_item("项链", ["+107 最大生命"]) + "塑界者物品\n"
        with self.assertRaisesRegex(InvalidRareComparable, "影响力"):
            service.estimate(text)
        fractured = make_item("项链", ["+107 最大生命 (fractured)"])
        with self.assertRaisesRegex(InvalidRareComparable, "裂隙"):
            service.estimate(fractured)

    def test_non_rare_and_unknown_base_are_rejected(self) -> None:
        service, _calls = self._service()
        magic = make_item("项链", ["+107 最大生命"]).replace("稀有度: 稀有", "稀有度: 魔法")
        with self.assertRaisesRegex(InvalidRareComparable, "只接受稀有"):
            service.estimate(magic)
        unknown = make_item("项链", ["+107 最大生命"]).replace("测试底材", "未知底材")
        with self.assertRaisesRegex(InvalidRareComparable, "唯一映射"):
            service.estimate(unknown)

    def test_http_and_tool_expose_auditable_result(self) -> None:
        service, _calls = self._service()
        text = make_item("项链", ["+107 最大生命"])
        with patch("api.economy.rare_service_from_config", return_value=service):
            response = rare_comparables_endpoint(
                RareComparableRequest(item_text=text), _caller="test"
            )
        self.assertTrue(response.available)
        self.assertEqual("full", response.comparison_quality)
        self.assertIn("不是成交记录或精确估值", response.answer)

        service2, _calls2 = self._service()
        with patch("tools.impl.economy.rare_service_from_config", return_value=service2):
            result = economy_rare_comparables(text)
        self.assertTrue(result.ok)
        self.assertEqual("live_economy", result.meta["channel"])
        self.assertEqual(1, len(result.citations))
        self.assertIn("只约束词缀存在", render_rare_comparables(service2.estimate(text)))


if __name__ == "__main__":
    unittest.main()
