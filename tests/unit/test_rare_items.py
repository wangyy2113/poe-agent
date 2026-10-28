"""稀有装备可比挂单：底材/词缀映射、保守边界与审计输出。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.economy import (  # noqa: E402
    RareComparableRequest,
    rare_comparables as rare_comparables_endpoint,
)
from economy.rare_items import InvalidRareComparable, RareComparableService  # noqa: E402
from economy.trade_api import TradeApiClient  # noqa: E402
from tests.unit.test_item_mods import (  # noqa: E402
    _insert_affix, _insert_pob, fixture_conn, make_item,
)
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

    def _service(
        self, *, listing_count: int = 5, structured: bool = False,
        stats_payload: dict | None = None,
    ) -> tuple[RareComparableService, list]:
        calls: list[tuple[str, str, dict | None]] = []

        def request(method: str, path: str, payload: dict | None):
            calls.append((method, path, payload))
            if path == "/api/trade/data/stats":
                return stats_payload or STATS
            if path.startswith("/api/leagues"):
                return [{"id": "Standard"}, {"id": "Allflame"}]
            if path.startswith("/api/trade/search/"):
                ids = [f"r{i}" for i in range(listing_count)]
                return {"id": "q", "total": listing_count, "result": ids}
            if path.startswith("/api/trade/fetch/"):
                ids = path.split("/fetch/", 1)[1].split("?", 1)[0].split(",")
                rows = [rare_listing(10 + index, base_type="Test Base")
                        for index, _item_id in enumerate(ids)]
                if structured:
                    for row in rows:
                        row["item"]["properties"] = [
                            {"name": "Quality", "values": [["+20%", 1]]}
                        ]
                        row["item"]["sockets"] = [
                            {"group": group, "sColour": "R"}
                            for group in (0, 0, 1, 1, 1)
                        ]
                return {"result": rows}
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

    def test_plain_copy_mixed_affix_is_one_mapped_trade_stat_group(self) -> None:
        # 真实 Bows normal 阶梯：物理伤害/命中混合 T8，PoB 对应两个 tradeHashes。
        group = "LocalIncreasedPhysicalDamagePercentAndAccuracyRating"
        ladder = [
            (8, 8, 1, [[15, 19], [16, 20]],
             "物理伤害提高 (15-19)% / +(16-20) 命中值"),
        ]
        _insert_affix(
            self.conn, page="Bows", code="Bow", zh="弓", group=group,
            gen_label="前缀", ladder=ladder,
        )
        _insert_pob(
            self.conn, group=group, ladder=ladder,
            template_en="#% increased Physical Damage / +# to Accuracy Rating",
        )
        self.conn.execute(
            "UPDATE pob_mod SET trade_hashes='[691932474,1805374733]' "
            "WHERE mod_group=?", (group,),
        )
        self.conn.execute(
            "UPDATE pob_base_item SET category_key='bow',poedb_page='Bows',"
            "item_class='Bow',tags='[\"bow\",\"weapon\"]' "
            "WHERE name_en='Test Base'"
        )
        stats = {"result": [{"id": "explicit", "entries": [
            {"id": "explicit.stat_691932474", "text": "#% increased Physical Damage",
             "type": "explicit"},
            {"id": "explicit.stat_1805374733", "text": "+# to Accuracy Rating",
             "type": "explicit"},
        ]}]}
        service, calls = self._service(stats_payload=stats)
        raw = make_item("弓", ["物理伤害提高 17%", "+18 命中值"])
        result = service.estimate(raw)
        self.assertEqual(1, result.explicit_mod_count)
        self.assertEqual(1, result.mapped_mod_count)
        self.assertEqual("full", result.comparison_quality)
        self.assertIn(" / ", result.mapped_stats[0].mod_text)
        self.assertEqual(2, len(result.mapped_stats[0].stat_ids))
        search = next(call[2] for call in calls if call[1].startswith("/api/trade/search/"))
        filters = search["query"]["stats"][0]["filters"]
        self.assertEqual(2, len(filters))

    def test_optional_quality_filter_is_exposed_through_http_and_tool(self) -> None:
        raw = make_item("项链", ["+107 最大生命"]).replace(
            "物品等级: 84", "品质: +20%\n物品等级: 84"
        )
        service, _calls = self._service(structured=True)
        result = service.estimate(raw, match_quality=True)
        self.assertTrue(result.available)
        self.assertEqual(20, result.quality_filter)
        self.assertIsNone(result.socket_group_sizes)
        with patch("api.economy.rare_service_from_config", return_value=service):
            response = rare_comparables_endpoint(
                RareComparableRequest(item_text=raw, match_quality=True), _caller="test"
            )
        self.assertEqual(20, response.quality_filter)
        self.assertIn("品质筛选：20%", response.answer)
        with patch("tools.impl.economy.rare_service_from_config", return_value=service):
            tool = economy_rare_comparables(raw, match_quality=True)
        self.assertTrue(tool.ok)
        self.assertEqual(20, tool.data["quality_filter"])

    def test_requested_structure_requires_explicit_copy_fields(self) -> None:
        service, calls = self._service()
        raw = make_item("项链", ["+107 最大生命"])
        with self.assertRaisesRegex(InvalidRareComparable, "明确给出品质"):
            service.estimate(raw, match_quality=True)
        with self.assertRaisesRegex(InvalidRareComparable, "明确给出插槽"):
            service.estimate(raw, match_sockets=True)
        with patch("api.economy.rare_service_from_config", return_value=service):
            with self.assertRaises(HTTPException) as error:
                rare_comparables_endpoint(
                    RareComparableRequest(item_text=raw, match_sockets=True), _caller="test"
                )
        self.assertEqual(422, error.exception.status_code)
        self.assertEqual("invalid_rare_comparable", error.exception.detail["error"])
        with patch("tools.impl.economy.rare_service_from_config", return_value=service):
            tool = economy_rare_comparables(raw, match_sockets=True)
        self.assertFalse(tool.ok)
        self.assertIn("明确给出插槽", tool.error)
        self.assertEqual([], calls)

    def test_bow_quality_and_socket_groups_reach_quote_and_api(self) -> None:
        # PoB LocalPhysicalDamage 的单个 Trade hash 1940865751，英文两数值位。
        self.conn.execute(
            "UPDATE pob_base_item SET category_key='bow',poedb_page='Bows',"
            "item_class='Bow',socket_limit=6,tags='[\"bow\",\"weapon\"]' "
            "WHERE name_en='Test Base'"
        )
        self.conn.execute(
            "UPDATE pob_mod SET trade_hashes='[1940865751]' "
            "WHERE mod_group='LocalPhysicalDamage'"
        )
        stats = {"result": [{"id": "explicit", "entries": [
            {"id": "explicit.stat_1940865751", "text": "Adds # to # Physical Damage",
             "type": "explicit"},
        ]}]}
        service, _calls = self._service(structured=True, stats_payload=stats)
        raw = make_item(
            "Bows", ["Adds 35 to 76 Physical Damage"],
            language="en", base_type="Test Base",
        ).replace(
            "Item Level: 84", "Quality: +20%\nSockets: R-G B-B-B\nItem Level: 84"
        )
        result = service.estimate(raw, match_quality=True, match_sockets=True)
        self.assertTrue(result.available)
        self.assertEqual((2, 3), result.socket_group_sizes)
        self.assertEqual(20, result.quality_filter)
        self.assertEqual(5, result.quote.sample_count)
        self.assertIn("连接组尺寸筛选：[2, 3]", render_rare_comparables(result))
        with patch("api.economy.rare_service_from_config", return_value=service):
            response = rare_comparables_endpoint(
                RareComparableRequest(
                    item_text=raw, match_quality=True, match_sockets=True,
                ), _caller="test",
            )
        self.assertEqual((2, 3), response.socket_group_sizes)
        sockets_only = service.estimate(raw.replace("Quality: +20%\n", ""), match_sockets=True)
        self.assertTrue(sockets_only.available)
        self.assertIsNone(sockets_only.quality_filter)
        self.assertEqual((2, 3), sockets_only.quote.socket_group_sizes)

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
