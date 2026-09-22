"""GGG 官方 Trade API 报价适配器。

覆盖标准条目 exchange、唯一装备 search/fetch，以及稀有装备的同底材词缀可比挂单。
返回值是当前在线挂单的稳健中位数，不是成交记录；调用方必须保留样本量、过滤计数、
挂单时间范围和搜索边界。
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx


SOURCE = "GGG Trade API"
SOURCE_URL = "https://www.pathofexile.com/trade"
API_URL = "https://www.pathofexile.com/api/trade"


class TradeApiError(RuntimeError):
    """官方 Trade API 不可用或响应不符合已验证契约。"""


JsonRequester = Callable[[str, str, dict[str, Any] | None], Any]


@dataclass(frozen=True)
class TradeQuote:
    item_id: str
    name: str
    category: str
    median_chaos: float | None
    min_chaos: float | None
    max_chaos: float | None
    sample_count: int
    minimum_sample_count: int
    raw_listing_count: int
    cross_league_filtered_count: int
    invalid_quote_filtered_count: int
    outlier_filtered_count: int
    earliest_listing_at: str | None
    latest_listing_at: str | None
    search_total: int | None = None
    fetched_listing_count: int | None = None
    chaos_listing_count: int | None = None
    divine_listing_count: int | None = None
    unconverted_currency_filtered_count: int | None = None
    divine_chaos_rate: float | None = None
    conversion_sample_count: int | None = None
    conversion_status: str = ""
    sample_scope: str = ""
    source_url: str = SOURCE_URL

    @property
    def sufficient(self) -> bool:
        return self.median_chaos is not None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _category(label: str, name: str) -> str | None:
    if label == "Currency":
        return "currency"
    if label == "Cards":
        return "card"
    if label == "Fragments, Scarabs & Mapping":
        return "scarab" if "scarab" in name.casefold() else "fragment"
    return None


def _without_outliers(values: list[float]) -> list[float]:
    """用 Tukey 1.5×IQR 围栏剔除明显离群挂单；少于五个样本不做统计过滤。"""
    if len(values) < 5:
        return list(values)
    ordered = sorted(values)
    q1, _, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    iqr = q3 - q1
    if iqr == 0:
        return [value for value in ordered if q1 <= value <= q3]
    low = q1 - 1.5 * iqr
    high = q3 + 1.5 * iqr
    return [value for value in ordered if low <= value <= high]


def _keep_candidates(
    candidates: list[tuple[float, datetime]],
) -> list[tuple[float, datetime]]:
    """把数值过滤结果映射回原挂单，保留重复报价各自的时间戳。"""
    remaining = _without_outliers([value for value, _ in candidates])
    kept: list[tuple[float, datetime]] = []
    for candidate in candidates:
        try:
            index = remaining.index(candidate[0])
        except ValueError:
            continue
        kept.append(candidate)
        remaining.pop(index)
    return kept


class TradeApiClient:
    def __init__(
        self,
        *,
        base_url: str = "https://www.pathofexile.com",
        timeout_seconds: float = 15,
        requests_per_second: float = 0.5,
        user_agent: str = (
            "poe1-agent/0.2 (local economy fallback; "
            "github.com/wangyy2113/poe-agent)"
        ),
        static_ttl: timedelta = timedelta(hours=24),
        league_ttl: timedelta = timedelta(minutes=30),
        minimum_samples: int = 5,
        unique_max_results: int = 40,
        request_json: JsonRequester | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(float(timeout_seconds), 0.1)
        self.requests_per_second = max(float(requests_per_second), 0.0)
        self.user_agent = user_agent.strip()
        self.static_ttl = static_ttl
        self.league_ttl = league_ttl
        self.minimum_samples = max(2, int(minimum_samples))
        self.unique_max_results = max(1, min(int(unique_max_results), 100))
        self._request_json = request_json
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._last_request = 0.0
        self._static_fetched_at: datetime | None = None
        self._entries: tuple[tuple[str, str, str], ...] = ()
        self._stats_fetched_at: datetime | None = None
        self._stats: dict[str, str] = {}
        self._league_fetched_at: datetime | None = None
        self._league_names: tuple[str, ...] = ()

    def leagues(self) -> list[str]:
        with self._lock:
            now = self._now()
            if (
                self._league_names
                and self._league_fetched_at is not None
                and now - self._league_fetched_at <= self.league_ttl
            ):
                return list(self._league_names)
            payload = self._request(
                "GET", "/api/leagues?type=main&compact=1&limit=50", None
            )
            if not isinstance(payload, list):
                raise TradeApiError("GGG /api/leagues 返回的不是数组")
            names: list[str] = []
            for row in payload:
                if not isinstance(row, dict):
                    raise TradeApiError("GGG /api/leagues 包含非对象条目")
                name = str(row.get("id") or row.get("name") or "").strip()
                if name:
                    names.append(name)
            if not names:
                raise TradeApiError("GGG /api/leagues 没有返回可用联盟")
            self._league_names = tuple(names)
            self._league_fetched_at = now
            return names

    def quote(self, league: str, item_name: str, category: str) -> TradeQuote:
        item_id, canonical_name, resolved_category = self._resolve_item(
            item_name, category
        )
        payload = self._request(
            "POST",
            f"/api/trade/exchange/{quote(league, safe='')}",
            {
                "exchange": {
                    "status": {"option": "online"},
                    "have": ["chaos"],
                    "want": [item_id],
                }
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise TradeApiError("GGG exchange 返回缺少 result 对象")

        results = payload["result"]
        raw_count = len(results)
        cross_league = 0
        invalid = 0
        candidates: list[tuple[float, datetime]] = []
        for row in results.values():
            if not isinstance(row, dict):
                invalid += 1
                continue
            listing = row.get("listing")
            if not isinstance(listing, dict):
                invalid += 1
                continue
            account = listing.get("account")
            online = account.get("online") if isinstance(account, dict) else None
            online_league = online.get("league") if isinstance(online, dict) else None
            if not isinstance(online_league, str) or (
                online_league.casefold() != league.casefold()
            ):
                cross_league += 1
                continue
            indexed = _parse_time(listing.get("indexed"))
            offers = listing.get("offers")
            if indexed is None or not isinstance(offers, list):
                invalid += 1
                continue

            accepted = False
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                exchange = offer.get("exchange")
                item = offer.get("item")
                if not isinstance(exchange, dict) or not isinstance(item, dict):
                    continue
                if exchange.get("currency") != "chaos" or item.get("currency") != item_id:
                    continue
                have_amount = _number(exchange.get("amount"))
                want_amount = _number(item.get("amount"))
                stock = _number(item.get("stock"))
                if have_amount is None or want_amount is None:
                    continue
                if item.get("stock") is not None and stock is None:
                    continue
                if stock is not None and stock < want_amount:
                    continue
                rate = have_amount / want_amount
                if not math.isfinite(rate) or rate <= 0:
                    continue
                candidates.append((rate, indexed))
                accepted = True
                break
            if not accepted:
                invalid += 1

        kept = _keep_candidates(candidates)

        sample_count = len(kept)
        sufficient = sample_count >= self.minimum_samples
        values = [value for value, _ in kept]
        times = [indexed for _, indexed in kept]
        return TradeQuote(
            item_id=item_id,
            name=canonical_name,
            category=resolved_category,
            median_chaos=float(statistics.median(values)) if sufficient else None,
            min_chaos=min(values) if sufficient else None,
            max_chaos=max(values) if sufficient else None,
            sample_count=sample_count,
            minimum_sample_count=self.minimum_samples,
            raw_listing_count=raw_count,
            cross_league_filtered_count=cross_league,
            invalid_quote_filtered_count=invalid,
            outlier_filtered_count=len(candidates) - sample_count,
            earliest_listing_at=_iso(min(times)) if times else None,
            latest_listing_at=_iso(max(times)) if times else None,
        )

    def unique_quote(
        self,
        league: str,
        item_name: str,
        base_type: str,
    ) -> TradeQuote:
        """查询未腐化唯一装备，并对 chaos/divine 标价统一换算为 chaos。"""
        name = item_name.strip()
        item_type = base_type.strip()
        if not name or not item_type:
            raise TradeApiError("唯一装备备用估值需要精确英文名称和底材")
        search_payload = self._request(
            "POST",
            f"/api/trade/search/{quote(league, safe='')}",
            {
                "query": {
                    "status": {"option": "online"},
                    "name": name,
                    "type": item_type,
                    "stats": [{"type": "and", "filters": []}],
                    "filters": {
                        "misc_filters": {
                            "filters": {"corrupted": {"option": "false"}}
                        }
                    },
                },
                "sort": {"price": "asc"},
            },
        )
        if not isinstance(search_payload, dict):
            raise TradeApiError("GGG unique search 返回的不是对象")
        search_id = search_payload.get("id")
        result_ids = search_payload.get("result")
        if not isinstance(search_id, str) or not search_id:
            raise TradeApiError("GGG unique search 缺少查询 id")
        if not isinstance(result_ids, list) or not all(
            isinstance(value, str) and value for value in result_ids
        ):
            raise TradeApiError("GGG unique search 的 result 不是 id 数组")
        total_value = search_payload.get("total")
        search_total = (
            int(total_value)
            if isinstance(total_value, int) and not isinstance(total_value, bool)
            else len(result_ids)
        )
        selected_ids = list(dict.fromkeys(result_ids))[:self.unique_max_results]
        fetched: list[dict[str, Any]] = []
        for offset in range(0, len(selected_ids), 10):
            ids = selected_ids[offset:offset + 10]
            encoded_ids = ",".join(quote(item_id, safe="") for item_id in ids)
            payload = self._request(
                "GET",
                f"/api/trade/fetch/{encoded_ids}?query={quote(search_id, safe='')}",
                None,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
                raise TradeApiError("GGG unique fetch 返回缺少 result 数组")
            if not all(isinstance(row, dict) for row in payload["result"]):
                raise TradeApiError("GGG unique fetch 的 result 包含非对象条目")
            fetched.extend(payload["result"])

        cross_league = 0
        invalid = 0
        chaos_rows: list[tuple[float, datetime]] = []
        divine_rows: list[tuple[float, datetime]] = []
        unsupported_currency = 0
        for row in fetched:
            listing = row.get("listing")
            item = row.get("item")
            if not isinstance(listing, dict) or not isinstance(item, dict):
                invalid += 1
                continue
            account = listing.get("account")
            online = account.get("online") if isinstance(account, dict) else None
            online_league = online.get("league") if isinstance(online, dict) else None
            if not isinstance(online_league, str) or (
                online_league.casefold() != league.casefold()
            ):
                cross_league += 1
                continue
            if (
                str(item.get("name") or "").strip().casefold() != name.casefold()
                or str(item.get("typeLine") or "").strip().casefold()
                != item_type.casefold()
                or item.get("corrupted") is True
            ):
                invalid += 1
                continue
            indexed = _parse_time(listing.get("indexed"))
            price_data = listing.get("price")
            if indexed is None or not isinstance(price_data, dict):
                invalid += 1
                continue
            if price_data.get("type") not in {"~price", "~b/o"}:
                invalid += 1
                continue
            amount = _number(price_data.get("amount"))
            currency = str(price_data.get("currency") or "").casefold()
            if amount is None:
                invalid += 1
            elif currency == "chaos":
                chaos_rows.append((amount, indexed))
            elif currency == "divine":
                divine_rows.append((amount, indexed))
            else:
                unsupported_currency += 1

        divine_rate: float | None = None
        conversion_samples: int | None = None
        conversion_status = "not_needed"
        if divine_rows:
            try:
                conversion = self.quote(league, "Divine Orb", "currency")
            except TradeApiError:
                conversion_status = "unavailable"
            else:
                divine_rate = conversion.median_chaos
                conversion_samples = conversion.sample_count
                conversion_status = (
                    "converted" if divine_rate is not None else "insufficient_samples"
                )

        candidates = list(chaos_rows)
        unconverted = unsupported_currency
        if divine_rate is None:
            unconverted += len(divine_rows)
        else:
            candidates.extend(
                (amount * divine_rate, indexed) for amount, indexed in divine_rows
            )
        kept = _keep_candidates(candidates)
        sample_count = len(kept)
        sufficient = sample_count >= self.minimum_samples
        values = [value for value, _ in kept]
        times = [indexed for _, indexed in kept]
        return TradeQuote(
            item_id=f"unique:{search_id}",
            name=name,
            category="unique",
            median_chaos=float(statistics.median(values)) if sufficient else None,
            min_chaos=min(values) if sufficient else None,
            max_chaos=max(values) if sufficient else None,
            sample_count=sample_count,
            minimum_sample_count=self.minimum_samples,
            raw_listing_count=search_total,
            cross_league_filtered_count=cross_league,
            invalid_quote_filtered_count=invalid,
            outlier_filtered_count=len(candidates) - sample_count,
            earliest_listing_at=_iso(min(times)) if times else None,
            latest_listing_at=_iso(max(times)) if times else None,
            search_total=search_total,
            fetched_listing_count=len(fetched),
            chaos_listing_count=len(chaos_rows),
            divine_listing_count=len(divine_rows),
            unconverted_currency_filtered_count=unconverted,
            divine_chaos_rate=divine_rate,
            conversion_sample_count=conversion_samples,
            conversion_status=conversion_status,
            sample_scope=(
                f"未腐化、精确名称与底材；按标价升序最多抓取前 "
                f"{self.unique_max_results} 条，未归一化词缀数值、插槽或链接"
            ),
            source_url=f"https://www.pathofexile.com/trade/search/{quote(league, safe='')}/{search_id}",
        )

    def explicit_stats(self) -> dict[str, str]:
        """返回官方当前可搜索的显式 stat 字典，按配置 TTL 缓存。

        PoB 的 ``tradeHashes`` 只保存数字 hash；真正提交搜索前仍要用官方字典确认
        ``explicit.stat_<hash>`` 当前存在，避免把过期或其他类型的 hash 发给搜索接口。
        """
        with self._lock:
            now = self._now()
            if (
                self._stats
                and self._stats_fetched_at is not None
                and now - self._stats_fetched_at <= self.static_ttl
            ):
                return dict(self._stats)
            payload = self._request("GET", "/api/trade/data/stats", None)
            if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
                raise TradeApiError("GGG stats 返回缺少 result 数组")
            entries: dict[str, str] = {}
            for group in payload["result"]:
                if not isinstance(group, dict):
                    raise TradeApiError("GGG stats result 包含非对象分组")
                rows = group.get("entries")
                if not isinstance(rows, list):
                    raise TradeApiError("GGG stats 分组缺少 entries 数组")
                for row in rows:
                    if not isinstance(row, dict):
                        raise TradeApiError("GGG stats entries 包含非对象条目")
                    stat_id = str(row.get("id") or "").strip()
                    stat_type = str(row.get("type") or "").strip()
                    text = str(row.get("text") or "").strip()
                    if stat_id.startswith("explicit.stat_") and stat_type == "explicit" and text:
                        entries[stat_id] = text
            if not entries:
                raise TradeApiError("GGG stats 字典没有可用的 explicit stat")
            self._stats = entries
            self._stats_fetched_at = now
            return dict(entries)

    def rare_quote(
        self,
        league: str,
        base_type: str,
        item_level: int,
        corrupted: bool,
        stat_ids: list[str],
    ) -> TradeQuote:
        """查询同底材、同物等/腐化状态、同时具备指定显式 stat 的稀有挂单。

        stat 只约束“存在”，不约束 roll；具体边界由上层连同映射覆盖率一起披露。
        """
        item_type = base_type.strip()
        unique_stats = list(dict.fromkeys(
            value.strip() for value in stat_ids if value.strip()
        ))
        if not item_type or not 1 <= int(item_level) <= 100:
            raise TradeApiError("稀有装备可比查询需要精确英文底材和 1–100 物品等级")
        if not unique_stats:
            raise TradeApiError("稀有装备可比查询至少需要一个官方显式 stat")
        official_stats = self.explicit_stats()
        unknown = [stat_id for stat_id in unique_stats if stat_id not in official_stats]
        if unknown:
            raise TradeApiError(f"GGG stats 字典中没有显式 stat：{', '.join(unknown)}")

        search_payload = self._request(
            "POST",
            f"/api/trade/search/{quote(league, safe='')}",
            {
                "query": {
                    "status": {"option": "online"},
                    "type": item_type,
                    "stats": [{
                        "type": "and",
                        "filters": [{"id": stat_id} for stat_id in unique_stats],
                    }],
                    "filters": {
                        "type_filters": {
                            "filters": {"rarity": {"option": "rare"}}
                        },
                        "misc_filters": {
                            "filters": {
                                "ilvl": {"min": int(item_level), "max": int(item_level)},
                                "corrupted": {"option": "true" if corrupted else "false"},
                            }
                        },
                    },
                },
                "sort": {"price": "asc"},
            },
        )
        if not isinstance(search_payload, dict):
            raise TradeApiError("GGG rare search 返回的不是对象")
        search_id = search_payload.get("id")
        result_ids = search_payload.get("result")
        if not isinstance(search_id, str) or not search_id:
            raise TradeApiError("GGG rare search 缺少查询 id")
        if not isinstance(result_ids, list) or not all(
            isinstance(value, str) and value for value in result_ids
        ):
            raise TradeApiError("GGG rare search 的 result 不是 id 数组")
        total_value = search_payload.get("total")
        search_total = (
            int(total_value)
            if isinstance(total_value, int) and not isinstance(total_value, bool)
            else len(result_ids)
        )
        selected_ids = list(dict.fromkeys(result_ids))[:self.unique_max_results]
        fetched: list[dict[str, Any]] = []
        for offset in range(0, len(selected_ids), 10):
            ids = selected_ids[offset:offset + 10]
            encoded_ids = ",".join(quote(item_id, safe="") for item_id in ids)
            payload = self._request(
                "GET",
                f"/api/trade/fetch/{encoded_ids}?query={quote(search_id, safe='')}",
                None,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
                raise TradeApiError("GGG rare fetch 返回缺少 result 数组")
            if not all(isinstance(row, dict) for row in payload["result"]):
                raise TradeApiError("GGG rare fetch 的 result 包含非对象条目")
            fetched.extend(payload["result"])

        cross_league = 0
        invalid = 0
        unsupported_currency = 0
        chaos_rows: list[tuple[float, datetime]] = []
        divine_rows: list[tuple[float, datetime]] = []
        for row in fetched:
            listing = row.get("listing")
            item = row.get("item")
            if not isinstance(listing, dict) or not isinstance(item, dict):
                invalid += 1
                continue
            account = listing.get("account")
            online = account.get("online") if isinstance(account, dict) else None
            online_league = online.get("league") if isinstance(online, dict) else None
            if not isinstance(online_league, str) or (
                online_league.casefold() != league.casefold()
            ):
                cross_league += 1
                continue
            if (
                str(item.get("typeLine") or "").strip().casefold() != item_type.casefold()
                or item.get("frameType") != 2
                or item.get("ilvl") != int(item_level)
                or bool(item.get("corrupted", False)) != corrupted
            ):
                invalid += 1
                continue
            indexed = _parse_time(listing.get("indexed"))
            price_data = listing.get("price")
            if indexed is None or not isinstance(price_data, dict):
                invalid += 1
                continue
            if price_data.get("type") not in {"~price", "~b/o"}:
                invalid += 1
                continue
            amount = _number(price_data.get("amount"))
            currency = str(price_data.get("currency") or "").casefold()
            if amount is None:
                invalid += 1
            elif currency == "chaos":
                chaos_rows.append((amount, indexed))
            elif currency == "divine":
                divine_rows.append((amount, indexed))
            else:
                unsupported_currency += 1

        divine_rate: float | None = None
        conversion_samples: int | None = None
        conversion_status = "not_needed"
        if divine_rows:
            try:
                conversion = self.quote(league, "Divine Orb", "currency")
            except TradeApiError:
                conversion_status = "unavailable"
            else:
                divine_rate = conversion.median_chaos
                conversion_samples = conversion.sample_count
                conversion_status = (
                    "converted" if divine_rate is not None else "insufficient_samples"
                )

        candidates = list(chaos_rows)
        unconverted = unsupported_currency
        if divine_rate is None:
            unconverted += len(divine_rows)
        else:
            candidates.extend(
                (amount * divine_rate, indexed) for amount, indexed in divine_rows
            )
        kept = _keep_candidates(candidates)
        sample_count = len(kept)
        sufficient = sample_count >= self.minimum_samples
        values = [value for value, _ in kept]
        times = [indexed for _, indexed in kept]
        return TradeQuote(
            item_id=f"rare:{search_id}",
            name=item_type,
            category="rare",
            median_chaos=float(statistics.median(values)) if sufficient else None,
            min_chaos=min(values) if sufficient else None,
            max_chaos=max(values) if sufficient else None,
            sample_count=sample_count,
            minimum_sample_count=self.minimum_samples,
            raw_listing_count=search_total,
            cross_league_filtered_count=cross_league,
            invalid_quote_filtered_count=invalid,
            outlier_filtered_count=len(candidates) - sample_count,
            earliest_listing_at=_iso(min(times)) if times else None,
            latest_listing_at=_iso(max(times)) if times else None,
            search_total=search_total,
            fetched_listing_count=len(fetched),
            chaos_listing_count=len(chaos_rows),
            divine_listing_count=len(divine_rows),
            unconverted_currency_filtered_count=unconverted,
            divine_chaos_rate=divine_rate,
            conversion_sample_count=conversion_samples,
            conversion_status=conversion_status,
            sample_scope=(
                f"稀有、精确底材、物等 {int(item_level)}、"
                f"{'已腐化' if corrupted else '未腐化'}、同时具备 {len(unique_stats)} 个"
                "已映射显式 stat；只约束词缀存在，不归一化词缀数值、隐式/附魔、"
                "品质、插槽或链接"
            ),
            source_url=f"https://www.pathofexile.com/trade/search/{quote(league, safe='')}/{search_id}",
        )

    def _resolve_item(self, name: str, requested_category: str) -> tuple[str, str, str]:
        wanted = name.strip().casefold()
        matches = [
            entry for entry in self._static_entries()
            if entry[1].casefold() == wanted
            and (requested_category == "auto" or entry[2] == requested_category)
        ]
        if not matches:
            raise TradeApiError(
                f"GGG exchange 字典中没有受支持的精确条目 {name!r}"
            )
        if len(matches) != 1:
            raise TradeApiError(f"GGG exchange 字典中条目 {name!r} 不唯一")
        return matches[0]

    def _static_entries(self) -> tuple[tuple[str, str, str], ...]:
        with self._lock:
            now = self._now()
            if (
                self._entries
                and self._static_fetched_at is not None
                and now - self._static_fetched_at <= self.static_ttl
            ):
                return self._entries
            payload = self._request("GET", "/api/trade/data/static", None)
            if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
                raise TradeApiError("GGG static 返回缺少 result 数组")
            entries: list[tuple[str, str, str]] = []
            for group in payload["result"]:
                if not isinstance(group, dict):
                    raise TradeApiError("GGG static result 包含非对象分组")
                label = str(group.get("label") or "")
                rows = group.get("entries")
                if not isinstance(rows, list):
                    raise TradeApiError("GGG static 分组缺少 entries 数组")
                for row in rows:
                    if not isinstance(row, dict):
                        raise TradeApiError("GGG static entries 包含非对象条目")
                    item_id = str(row.get("id") or "").strip()
                    item_name = str(row.get("text") or "").strip()
                    category = _category(label, item_name)
                    if item_id and item_name and category:
                        entries.append((item_id, item_name, category))
            if not any(item_id == "chaos" for item_id, _, _ in entries):
                raise TradeApiError("GGG static 字典缺少 chaos 计价单位")
            self._entries = tuple(entries)
            self._static_fetched_at = now
            return self._entries

    def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> Any:
        self._pace()
        try:
            if self._request_json is not None:
                return self._request_json(method, path, payload)
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                headers={"User-Agent": self.user_agent},
            ) as client:
                response = client.request(method, path, json=payload)
                response.raise_for_status()
                return response.json()
        except TradeApiError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise TradeApiError(
                f"GGG Trade API 请求失败：{type(exc).__name__}: {exc}"
            ) from exc

    def _pace(self) -> None:
        if self.requests_per_second <= 0:
            return
        interval = 1.0 / self.requests_per_second
        with self._lock:
            remaining = interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()
