"""GGG 官方 Trade API 交换报价适配器。

这里只覆盖可通过 ``/api/trade/exchange`` 精确交换的标准条目。返回值是当前在线挂单的
稳健中位数，不是成交记录；调用方必须保留样本量、过滤计数和挂单时间范围。
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


class TradeApiClient:
    def __init__(
        self,
        *,
        base_url: str = "https://www.pathofexile.com",
        timeout_seconds: float = 15,
        requests_per_second: float = 0.25,
        user_agent: str = (
            "poe1-agent/0.2 (local economy fallback; "
            "github.com/wangyy2113/poe-agent)"
        ),
        static_ttl: timedelta = timedelta(hours=24),
        league_ttl: timedelta = timedelta(minutes=30),
        minimum_samples: int = 5,
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
        self._request_json = request_json
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._last_request = 0.0
        self._static_fetched_at: datetime | None = None
        self._entries: tuple[tuple[str, str, str], ...] = ()
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

        raw_values = [value for value, _ in candidates]
        kept_values = _without_outliers(raw_values)
        # 按数值多重集保留对应时间；相同报价的重复挂单仍是独立样本。
        remaining = list(kept_values)
        kept: list[tuple[float, datetime]] = []
        for candidate in candidates:
            try:
                index = remaining.index(candidate[0])
            except ValueError:
                continue
            kept.append(candidate)
            remaining.pop(index)

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
