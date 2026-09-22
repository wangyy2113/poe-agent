"""实时价格服务：名称解析、TTL 缓存、陈旧缓存与官方 Trade API 备用降级。"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from economy.models import (
    MaterialCostEstimate,
    MaterialCostLine,
    MaterialRequest,
    PriceObservation,
    PricePoint,
    PriceSearch,
    PriceTrend,
    PriceTrendSearch,
)
from economy.poe_watch import PoeWatchClient, PoeWatchError
from economy.trade_api import (
    SOURCE as TRADE_SOURCE,
    SOURCE_URL as TRADE_SOURCE_URL,
    TradeApiClient,
    TradeApiError,
)
from retrieval.alias import lookup, normalize

SOURCE = "poe.watch"
SOURCE_URL = "https://poe.watch"
API_URL = "https://api.poe.watch"

CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    "currency": ("currency",),
    "scarab": ("scarab",),
    "card": ("card",),
    "fragment": ("fragment",),
    "unique": ("accessory", "armour", "weapon", "jewel", "flask", "uniqueMap"),
}
PUBLIC_CATEGORIES = ("auto", *CATEGORY_MAP)
_SNAPSHOT_LOCK = threading.RLock()


class EconomyUnavailable(RuntimeError):
    """上游不可用且没有可降级的缓存。"""


class InvalidEconomyQuery(ValueError):
    """联盟、类别或查询参数不符合支持范围。"""


@dataclass(frozen=True)
class _Snapshot:
    payload: list[dict[str, Any]]
    fetched_at: datetime
    from_cache: bool
    degraded: bool = False
    upstream_error: str = ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(raw: str) -> datetime:
    value = raw.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _number_series(value: Any) -> tuple[float, ...]:
    if not isinstance(value, list):
        return ()
    numbers = [_number(item) for item in value]
    return tuple(item for item in numbers if item is not None and math.isfinite(item))


class EconomyService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        client: PoeWatchClient,
        trade_client: TradeApiClient | None = None,
        cache_ttl: timedelta = timedelta(minutes=30),
        stale_after: timedelta = timedelta(hours=6),
        observation_retention: timedelta = timedelta(days=90),
        max_trend_points: int = 200,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.conn = conn
        self.client = client
        self.trade_client = trade_client
        self.cache_ttl = cache_ttl
        self.stale_after = stale_after
        self.observation_retention = observation_retention
        self.max_trend_points = max(2, int(max_trend_points))
        self.now = now

    def price(
        self,
        query: str,
        *,
        category: str = "auto",
        league: str | None = None,
    ) -> PriceSearch:
        raw_query = (query or "").strip()
        requested = (category or "auto").strip()
        if not raw_query:
            raise InvalidEconomyQuery("query 不能为空")
        if requested not in PUBLIC_CATEGORIES:
            raise InvalidEconomyQuery(
                f"不支持的 category={requested!r}；可用：{', '.join(PUBLIC_CATEGORIES)}"
            )

        resolved_name, inferred, inferred_upstream = self._resolve_name(raw_query)
        logical_category = inferred if requested == "auto" and inferred else requested
        upstream_categories = (
            inferred_upstream
            if inferred_upstream and logical_category == "unique"
            else CATEGORY_MAP[logical_category]
            if logical_category != "auto"
            else tuple(c for values in CATEGORY_MAP.values() for c in values)
        )

        selected_league: str | None = None
        try:
            leagues, league_snapshot = self._leagues()
            selected_league = self._select_league(league, leagues)
            snapshots = [self._prices(selected_league, c) for c in upstream_categories]
        except EconomyUnavailable as exc:
            return self._fallback_price(
                raw_query=raw_query,
                requested_category=requested,
                resolved_name=resolved_name,
                logical_category=logical_category,
                requested_league=league or selected_league,
                primary_error=exc,
            )
        wanted = normalize(resolved_name)
        prices: list[PricePoint] = []
        observed_at_by_item: dict[tuple[str, str], datetime] = {}
        for snapshot in snapshots:
            for row in snapshot.payload:
                name = str(row.get("name") or "").strip()
                row_name = normalize(name)
                # ``query`` 既可来自结构化 API（通常就是名字），也可来自聊天整句。
                # 只接受完整名称相等或完整名称作为问句子串，不做编辑距离/模糊猜测。
                if name and (row_name == wanted or (len(row_name) >= 4 and row_name in wanted)):
                    point = self._price_point(row)
                    prices.append(point)
                    observed_at_by_item[(str(point.item_id), point.category)] = snapshot.fetched_at

        if prices:
            longest = max(len(normalize(item.name)) for item in prices)
            prices = [item for item in prices if len(normalize(item.name)) == longest]

        # 同一条目偶尔会由多个上游类别返回；按 (id, category) 去重，保持类别顺序。
        unique: dict[tuple[str, str], PricePoint] = {}
        for item in prices:
            unique[(str(item.item_id), item.category)] = item
        prices = list(unique.values())

        # poe.watch 的所有 mean/min/max 都以 Chaos Orb 为计价单位，因此上游列表可能
        # 不返回 Chaos Orb 自身。这里是单位恒等式（1 chaos = 1 chaos），不是市场估价；
        # 仍沿用 currency 快照的时间戳，明确它所依附的报价口径。
        if (
            not prices
            and logical_category in {"auto", "currency"}
            and normalize(resolved_name) == normalize("Chaos Orb")
        ):
            prices = [PricePoint(
                item_id="chaos-quote-unit",
                name="Chaos Orb",
                category="currency",
                group="quote_unit",
                representative_chaos=1.0,
                price_basis="quote_unit_identity",
                mean_chaos=1.0,
                median_chaos=None,
                min_chaos=1.0,
                max_chaos=1.0,
                divine=None,
                daily=None,
                change_percent=None,
                low_confidence=False,
            )]

        # 价格新鲜度只取价格快照；联盟目录可以比价格缓存更早，不应把刚刷新的价格
        # 错标成陈旧。联盟请求是否降级仍合并到 degraded 状态。
        fetched_at = min(s.fetched_at for s in snapshots)
        age = self.now() - fetched_at
        all_snapshots = [league_snapshot, *snapshots]
        degraded = any(s.degraded for s in all_snapshots)
        errors = [s.upstream_error for s in all_snapshots if s.upstream_error]
        stale = age > self.stale_after
        warning = ""
        if stale:
            warning = "缓存已超过允许的新鲜度，只能作为陈旧快照参考，不能视为当前行情"
        elif degraded:
            warning = "上游刷新失败，本次使用尚未超过陈旧阈值的缓存快照"
        if any(p.low_confidence for p in prices):
            confidence = "poe.watch 将该价格标记为低置信度"
            warning = f"{warning}；{confidence}" if warning else confidence
        if errors:
            # 不把完整异常塞进用户答案，只保留稳定状态；详情进服务日志/工具 error 即可。
            warning = warning or "poe.watch 刷新失败，已降级使用缓存"

        self._record_observations(
            selected_league,
            [
                (
                    point,
                    observed_at_by_item.get((str(point.item_id), point.category), fetched_at),
                )
                for point in prices
            ],
        )

        return PriceSearch(
            query=raw_query,
            resolved_name=resolved_name,
            requested_category=requested,
            categories_searched=upstream_categories,
            league=selected_league,
            source=SOURCE,
            source_url=SOURCE_URL,
            fetched_at=_iso(fetched_at),
            from_cache=all(s.from_cache for s in snapshots),
            stale=stale,
            degraded=degraded,
            fallback=False,
            fallback_reason="",
            warning=warning,
            prices=tuple(prices),
        )

    def _fallback_price(
        self,
        *,
        raw_query: str,
        requested_category: str,
        resolved_name: str,
        logical_category: str,
        requested_league: str | None,
        primary_error: EconomyUnavailable,
    ) -> PriceSearch:
        """主源没有任何可用缓存时，才查询官方在线交换挂单。"""
        if self.trade_client is None:
            raise primary_error
        if logical_category == "unique":
            raise EconomyUnavailable(
                f"{primary_error}；GGG Trade API 备用源不支持唯一装备估值"
            ) from primary_error
        if logical_category not in {"auto", "currency", "scarab", "card", "fragment"}:
            raise primary_error

        try:
            official_leagues = [{"name": name} for name in self.trade_client.leagues()]
            selected_league = self._select_league(
                requested_league, official_leagues, source=TRADE_SOURCE
            )
            if (
                logical_category in {"auto", "currency"}
                and normalize(resolved_name) == normalize("Chaos Orb")
            ):
                point = PricePoint(
                    item_id="chaos-quote-unit",
                    name="Chaos Orb",
                    category="currency",
                    group="quote_unit",
                    representative_chaos=1.0,
                    price_basis="quote_unit_identity",
                    mean_chaos=1.0,
                    median_chaos=None,
                    min_chaos=1.0,
                    max_chaos=1.0,
                    divine=None,
                    daily=None,
                    change_percent=None,
                    low_confidence=False,
                )
                searched = ("currency",)
                warning = (
                    "poe.watch 无可用缓存且刷新失败，已显式降级；混沌石价格为计价单位恒等式"
                )
            else:
                quote = self.trade_client.quote(
                    selected_league, resolved_name, logical_category
                )
                point = PricePoint(
                    item_id=f"trade:{quote.item_id}",
                    name=quote.name,
                    category=quote.category,
                    group="exchange_listing",
                    representative_chaos=quote.median_chaos,
                    price_basis="ggg_listing_median",
                    mean_chaos=None,
                    median_chaos=quote.median_chaos,
                    min_chaos=quote.min_chaos,
                    max_chaos=quote.max_chaos,
                    divine=None,
                    daily=None,
                    change_percent=None,
                    low_confidence=not quote.sufficient,
                    sample_count=quote.sample_count,
                    minimum_sample_count=quote.minimum_sample_count,
                    raw_listing_count=quote.raw_listing_count,
                    cross_league_filtered_count=quote.cross_league_filtered_count,
                    invalid_quote_filtered_count=quote.invalid_quote_filtered_count,
                    outlier_filtered_count=quote.outlier_filtered_count,
                    earliest_listing_at=quote.earliest_listing_at,
                    latest_listing_at=quote.latest_listing_at,
                )
                searched = (quote.category,)
                warning = (
                    "poe.watch 无可用缓存且刷新失败，已显式降级到 GGG Trade API 在线挂单"
                )
                if not quote.sufficient:
                    warning += (
                        f"；过滤后只有 {quote.sample_count} 条有效挂单，低于最低样本数 "
                        f"{quote.minimum_sample_count}，未输出代表价"
                    )
        except TradeApiError as exc:
            raise EconomyUnavailable(
                f"{primary_error}；官方 Trade API 备用查询也不可用：{exc}"
            ) from exc

        return PriceSearch(
            query=raw_query,
            resolved_name=point.name,
            requested_category=requested_category,
            categories_searched=searched,
            league=selected_league,
            source=TRADE_SOURCE,
            source_url=TRADE_SOURCE_URL,
            fetched_at=_iso(self.now()),
            from_cache=False,
            stale=False,
            degraded=True,
            fallback=True,
            fallback_reason="poe.watch 无可用缓存且刷新失败",
            warning=warning,
            prices=(point,),
        )

    def trend(
        self,
        query: str,
        *,
        category: str = "auto",
        league: str | None = None,
        window_hours: int = 24 * 7,
        minimum_observations: int = 2,
    ) -> PriceTrendSearch:
        """按本机实际保存的多个快照计算趋势；绝不把单次 ``change`` 当历史。"""
        if isinstance(window_hours, bool) or not isinstance(window_hours, int):
            raise InvalidEconomyQuery("window_hours 必须是整数")
        max_hours = max(1, int(self.observation_retention.total_seconds() // 3600))
        if not 1 <= window_hours <= max_hours:
            raise InvalidEconomyQuery(f"window_hours 必须在 1 到 {max_hours} 之间")
        if (
            isinstance(minimum_observations, bool)
            or not isinstance(minimum_observations, int)
            or not 2 <= minimum_observations <= 100
        ):
            raise InvalidEconomyQuery("minimum_observations 必须是 2 到 100 之间的整数")

        current = self.price(query, category=category, league=league)
        cutoff = _iso(self.now() - timedelta(hours=window_hours))
        trends = tuple(
            self._trend_for_point(
                current.league,
                point,
                cutoff=cutoff,
                window_hours=window_hours,
                minimum_observations=minimum_observations,
            )
            for point in current.prices
            if point.group != "quote_unit"
        )
        warning = current.warning
        if trends and not any(item.status == "ready" for item in trends):
            note = f"至少需要 {minimum_observations} 个不同时间的有效均价观测点才能判断趋势"
            warning = f"{warning}；{note}" if warning else note
        return PriceTrendSearch(
            query=current.query,
            resolved_name=current.resolved_name,
            requested_category=current.requested_category,
            categories_searched=current.categories_searched,
            league=current.league,
            source=current.source,
            source_url=current.source_url,
            fetched_at=current.fetched_at,
            from_cache=current.from_cache,
            stale=current.stale,
            degraded=current.degraded,
            fallback=current.fallback,
            fallback_reason=current.fallback_reason,
            warning=warning,
            window_hours=window_hours,
            minimum_observations=minimum_observations,
            trends=trends,
        )

    def material_cost(
        self,
        materials: list[MaterialRequest],
        *,
        league: str | None = None,
    ) -> MaterialCostEstimate:
        """给明确材料清单逐项查价并加总，不补材料、不推断制作步骤。"""
        if not materials:
            raise InvalidEconomyQuery("materials 不能为空")
        if len(materials) > 50:
            raise InvalidEconomyQuery("materials 最多包含 50 项")

        lines: list[MaterialCostLine] = []
        selected_league = ""
        for material in materials:
            query = (material.query or "").strip()
            quantity = material.quantity
            if not query:
                raise InvalidEconomyQuery("材料 query 不能为空")
            if (
                isinstance(quantity, bool)
                or not isinstance(quantity, (int, float))
                or not math.isfinite(float(quantity))
                or float(quantity) <= 0
                or float(quantity) > 1_000_000_000
            ):
                raise InvalidEconomyQuery("材料 quantity 必须是 0 到 1,000,000,000 之间的有限数")

            result = self.price(query, category=material.category, league=league)
            if selected_league and result.league != selected_league:
                raise EconomyUnavailable("同一材料清单返回了不同联盟，拒绝混合计价")
            selected_league = result.league
            matches = result.prices
            point = matches[0] if len(matches) == 1 else None
            if not matches:
                status, reason = "not_found", "未找到同名条目；未做模糊匹配"
            elif len(matches) > 1:
                status, reason = "ambiguous", f"同名匹配到 {len(matches)} 个条目，未擅自选价"
            elif point is not None and point.representative_chaos is None:
                if (
                    point.minimum_sample_count is not None
                    and point.sample_count is not None
                    and point.sample_count < point.minimum_sample_count
                ):
                    status, reason = (
                        "insufficient_samples",
                        f"有效挂单 {point.sample_count} 条，低于最低样本数 "
                        f"{point.minimum_sample_count}",
                    )
                else:
                    status, reason = "missing_price", "上游未提供可用混沌石代表价"
            else:
                status, reason = "priced", ""

            def subtotal(value: float | None) -> float | None:
                if point is None or value is None:
                    return None
                # 从十进制文本构造，避免二进制浮点乘法产生 0.30000000000000004。
                return float(Decimal(str(value)) * Decimal(str(quantity)))

            lines.append(MaterialCostLine(
                query=query,
                quantity=float(quantity),
                requested_category=material.category,
                status=status,
                reason=reason,
                resolved_name=result.resolved_name,
                categories_searched=result.categories_searched,
                league=result.league,
                source=result.source,
                source_url=result.source_url,
                fetched_at=result.fetched_at,
                from_cache=result.from_cache,
                stale=result.stale,
                degraded=result.degraded,
                fallback=result.fallback,
                fallback_reason=result.fallback_reason,
                warning=result.warning,
                low_confidence=bool(point and point.low_confidence),
                unit_price=point,
                candidate_count=len(matches),
                subtotal_representative_chaos=subtotal(
                    point.representative_chaos if point else None
                ),
                subtotal_mean_chaos=subtotal(point.mean_chaos if point else None),
                subtotal_min_chaos=subtotal(point.min_chaos if point else None),
                subtotal_max_chaos=subtotal(point.max_chaos if point else None),
            ))

        def total(field: str) -> float | None:
            values = [getattr(line, field) for line in lines]
            if any(value is None for value in values):
                return None
            return float(sum((Decimal(str(value)) for value in values), Decimal("0")))

        representative_total = total("subtotal_representative_chaos")
        mean_total = total("subtotal_mean_chaos")
        unresolved = sum(line.status != "priced" for line in lines)
        warnings: list[str] = []
        if unresolved:
            warnings.append(f"{unresolved} 项未取得唯一可用代表价，总成本不可计算")
        if any(line.stale for line in lines):
            warnings.append("至少一个材料使用陈旧快照")
        elif any(line.fallback for line in lines):
            warnings.append("至少一个材料在主源失败后使用 GGG Trade API 在线挂单")
        elif any(line.degraded for line in lines):
            warnings.append("至少一个材料在上游刷新失败后使用缓存")
        if any(line.low_confidence for line in lines):
            warnings.append("至少一个材料价格置信度不足")

        sources = list(dict.fromkeys(line.source for line in lines))
        source_urls = list(dict.fromkeys(line.source_url for line in lines))

        return MaterialCostEstimate(
            league=selected_league,
            source=sources[0] if len(sources) == 1 else "mixed",
            source_url=source_urls[0] if len(source_urls) == 1 else "",
            fetched_at=min(line.fetched_at for line in lines),
            from_cache=all(line.from_cache for line in lines),
            stale=any(line.stale for line in lines),
            degraded=any(line.degraded for line in lines),
            fallback=any(line.fallback for line in lines),
            warning="；".join(warnings),
            calculable=representative_total is not None,
            unresolved_count=unresolved,
            total_representative_chaos=representative_total,
            total_mean_chaos=mean_total,
            total_min_chaos=total("subtotal_min_chaos"),
            total_max_chaos=total("subtotal_max_chaos"),
            materials=tuple(lines),
        )

    def _leagues(self) -> tuple[list[dict[str, Any]], _Snapshot]:
        snap = self._load_or_fetch("__global__", "__leagues__", self.client.leagues)
        return snap.payload, snap

    def _prices(self, league: str, category: str) -> _Snapshot:
        return self._load_or_fetch(
            league, category, lambda: self.client.prices(league, category)
        )

    def _load_or_fetch(
        self, league: str, category: str, fetch: Callable[[], list[dict[str, Any]]]
    ) -> _Snapshot:
        # 应用使用一个 check_same_thread=False 的共享连接；缓存读写与刷新串行化，
        # 避免两个并发 miss 在同一连接上交错 commit，也避免对公共 API 形成惊群。
        with _SNAPSHOT_LOCK:
            return self._load_or_fetch_locked(league, category, fetch)

    def _load_or_fetch_locked(
        self, league: str, category: str, fetch: Callable[[], list[dict[str, Any]]]
    ) -> _Snapshot:
        cached = self.conn.execute(
            "SELECT fetched_at,payload FROM economy_snapshot "
            "WHERE source=? AND league=? AND category=?",
            (SOURCE, league, category),
        ).fetchone()
        now = self.now()
        if cached is not None:
            fetched_at = _parse_time(str(cached["fetched_at"]))
            if now - fetched_at <= self.cache_ttl:
                return _Snapshot(json.loads(cached["payload"]), fetched_at, True)
        try:
            payload = fetch()
        except PoeWatchError as exc:
            if cached is None:
                raise EconomyUnavailable(str(exc)) from exc
            return _Snapshot(
                json.loads(cached["payload"]),
                _parse_time(str(cached["fetched_at"])),
                True,
                degraded=True,
                upstream_error=str(exc),
            )

        fetched_at = now
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        self.conn.execute(
            """
            INSERT INTO economy_snapshot(source,league,category,fetched_at,payload,content_hash)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(source,league,category) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                payload=excluded.payload,
                content_hash=excluded.content_hash
            """,
            (SOURCE, league, category, _iso(fetched_at), encoded, digest),
        )
        self.conn.commit()
        return _Snapshot(payload, fetched_at, False)

    @staticmethod
    def _observation_key(point: PricePoint) -> str:
        item_id = str(point.item_id).strip()
        if item_id:
            return f"id:{item_id}"
        identity = json.dumps(
            [normalize(point.name), point.category, point.group],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"name:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"

    def _record_observations(
        self,
        league: str,
        prices: list[tuple[PricePoint, datetime]],
    ) -> None:
        rows = [
            (
                SOURCE,
                league,
                point.category,
                self._observation_key(point),
                str(point.item_id),
                point.name,
                point.group,
                _iso(observed_at),
                point.mean_chaos,
                point.min_chaos,
                point.max_chaos,
                point.divine,
                point.daily,
                int(point.low_confidence),
            )
            for point, observed_at in prices
            if point.group != "quote_unit"
        ]
        if not rows:
            return
        with _SNAPSHOT_LOCK:
            self.conn.executemany(
                "INSERT OR IGNORE INTO economy_observation("
                "source,league,category,item_key,item_id,item_name,item_group,observed_at,"
                "mean_chaos,min_chaos,max_chaos,divine,daily,low_confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            cutoff = _iso(self.now() - self.observation_retention)
            self.conn.execute(
                "DELETE FROM economy_observation WHERE source=? AND observed_at < ?",
                (SOURCE, cutoff),
            )
            self.conn.commit()

    def _trend_for_point(
        self,
        league: str,
        point: PricePoint,
        *,
        cutoff: str,
        window_hours: int,
        minimum_observations: int,
    ) -> PriceTrend:
        rows = list(self.conn.execute(
            "SELECT observed_at,mean_chaos,min_chaos,max_chaos,daily,low_confidence "
            "FROM economy_observation WHERE source=? AND league=? AND category=? "
            "AND item_key=? AND observed_at>=? AND mean_chaos IS NOT NULL "
            "ORDER BY observed_at",
            (SOURCE, league, point.category, self._observation_key(point), cutoff),
        ))
        observations = [
            PriceObservation(
                observed_at=str(row["observed_at"]),
                mean_chaos=float(row["mean_chaos"]),
                min_chaos=_number(row["min_chaos"]),
                max_chaos=_number(row["max_chaos"]),
                daily=(int(row["daily"]) if row["daily"] is not None else None),
                low_confidence=bool(row["low_confidence"]),
            )
            for row in rows
        ]
        local_ready = len(observations) >= minimum_observations and (
            observations[0].observed_at != observations[-1].observed_at
        )
        upstream_values = point.seven_day_history if window_hours == 24 * 7 else ()
        upstream_ready = not local_ready and len(upstream_values) >= minimum_observations
        basis = (
            "poe_watch_seven_day_history" if upstream_ready else "local_observations"
        )
        values = (
            [item.mean_chaos for item in observations]
            if local_ready
            else list(upstream_values)
            if upstream_ready
            else [item.mean_chaos for item in observations]
        )
        count = len(values)
        ready = local_ready or upstream_ready
        first = observations[0] if observations else None
        last = observations[-1] if observations else None
        change = None
        change_percent = None
        direction = "unknown"
        reason = ""
        if ready:
            change_decimal = Decimal(str(values[-1])) - Decimal(str(values[0]))
            change = float(change_decimal)
            if values[0] != 0:
                change_percent = float(
                    change_decimal / abs(Decimal(str(values[0]))) * Decimal("100")
                )
            direction = "up" if change > 0 else "down" if change < 0 else "flat"
        else:
            reason = (
                f"窗口内只有 {count} 个有效均价观测点；至少需要 "
                f"{minimum_observations} 个不同时间的观测点"
            )

        sampled = self._sample_observations(observations)
        returned_values = (
            tuple(upstream_values)
            if upstream_ready
            else tuple(item.mean_chaos for item in sampled)
        )
        return PriceTrend(
            item_id=point.item_id,
            name=point.name,
            category=point.category,
            group=point.group,
            basis=basis,
            timestamps_available=not upstream_ready,
            status="ready" if ready else "insufficient_data",
            reason=reason,
            observation_count=count,
            first_observed_at=first.observed_at if local_ready and first else None,
            last_observed_at=last.observed_at if local_ready and last else None,
            first_mean_chaos=values[0] if values else None,
            last_mean_chaos=values[-1] if values else None,
            change_chaos=change,
            change_percent=change_percent,
            min_mean_chaos=min(values) if values else None,
            max_mean_chaos=max(values) if values else None,
            direction=direction,
            low_confidence_count=(
                sum(item.low_confidence for item in observations)
                if local_ready
                else int(point.low_confidence)
            ),
            series_values=returned_values,
            observations=tuple(sampled),
        )

    def _sample_observations(
        self, observations: list[PriceObservation]
    ) -> list[PriceObservation]:
        if len(observations) <= self.max_trend_points:
            return observations
        last = len(observations) - 1
        indexes = {
            round(index * last / (self.max_trend_points - 1))
            for index in range(self.max_trend_points)
        }
        return [observations[index] for index in sorted(indexes)]

    @staticmethod
    def _select_league(
        requested: str | None,
        leagues: list[dict[str, Any]],
        *,
        source: str = SOURCE,
    ) -> str:
        names = [str(row.get("name") or "").strip() for row in leagues]
        names = [name for name in names if name]
        if not names:
            raise EconomyUnavailable(f"{source} 没有返回可用联盟")
        if requested and requested.strip():
            wanted = requested.strip()
            exact = next((name for name in names if name.casefold() == wanted.casefold()), None)
            if exact is None:
                raise InvalidEconomyQuery(
                    f"{source} 当前没有联盟 {wanted!r}；可用：{', '.join(names)}"
                )
            return exact
        # API 按当前挑战联盟在前、永久区在后返回；排除 HC/Ruthless/SSF 与永久区。
        preferred = [
            name for name in names
            if name not in {"Standard", "Hardcore", "Solo Self-Found"}
            and not re.search(r"Hardcore|Ruthless|SSF", name, re.I)
        ]
        return preferred[0] if preferred else names[0]

    def _resolve_name(self, query: str) -> tuple[str, str | None, tuple[str, ...] | None]:
        try:
            entity_ids = lookup(self.conn, query)
        except sqlite3.Error:
            entity_ids = []
        category_by_prefix = {
            "unique_item:": "unique",
            "currency:": "currency",
            "scarab:": "scarab",
        }
        candidates: list[tuple[str, str, tuple[str, ...] | None]] = []
        for entity_id in entity_ids:
            category = next(
                (value for prefix, value in category_by_prefix.items() if entity_id.startswith(prefix)),
                None,
            )
            if category is None:
                continue
            row = self.conn.execute(
                "SELECT name_en,attributes FROM entity WHERE entity_id=?", (entity_id,)
            ).fetchone()
            if row is not None and row["name_en"]:
                upstream = None
                if category == "unique":
                    try:
                        attrs = json.loads(row["attributes"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        attrs = {}
                    upstream = self._unique_category(str(attrs.get("base_type_en") or ""))
                candidates.append((str(row["name_en"]), category, upstream))
        distinct = list(dict.fromkeys(candidates))
        if len(distinct) == 1:
            return distinct[0]
        # 「猎首值多少混沌石」会同时命中猎首与计价单位混沌石。只在恰好有一个
        # 非通货主体时把句末通货视为单位；多个非通货主体仍保持歧义，不猜目标。
        non_currency = [candidate for candidate in distinct if candidate[1] != "currency"]
        if len(non_currency) == 1 and any(
            marker in query
            for marker in (
                "价格", "多少钱", "多少C", "多少c", "值多少", "行情", "市价",
            )
        ):
            return non_currency[0]
        return query.strip(), None, None

    @staticmethod
    def _unique_category(base_type: str) -> tuple[str, ...] | None:
        """用权威英文底材只缩小 poe.watch 分区，不推断价格或物品名称。"""
        value = base_type.casefold()
        groups = (
            ("uniqueMap", (" map", "map ", "map")),
            ("flask", ("flask",)),
            ("jewel", ("jewel",)),
            ("accessory", ("ring", "amulet", "belt", "quiver")),
            ("armour", ("helmet", "gloves", "boots", "body armour", "shield")),
            (
                "weapon",
                (
                    "axe", "sword", "mace", "bow", "staff", "wand", "dagger",
                    "sceptre", "claw",
                ),
            ),
        )
        for category, markers in groups:
            if any(marker in value for marker in markers):
                return (category,)
        return None

    @staticmethod
    def _price_point(row: dict[str, Any]) -> PricePoint:
        daily = row.get("daily")
        try:
            daily_value = int(daily) if daily is not None and not isinstance(daily, bool) else None
        except (TypeError, ValueError):
            daily_value = None
        return PricePoint(
            item_id=row.get("id", ""),
            name=str(row.get("name") or ""),
            category=str(row.get("category") or ""),
            group=str(row.get("group") or ""),
            representative_chaos=_number(row.get("mean")),
            price_basis="poe_watch_mean",
            mean_chaos=_number(row.get("mean")),
            median_chaos=None,
            min_chaos=_number(row.get("min")),
            max_chaos=_number(row.get("max")),
            divine=_number(row.get("divine")),
            daily=daily_value,
            change_percent=_number(row.get("change")),
            low_confidence=bool(row.get("lowConfidence", False)),
            seven_day_history=_number_series(row.get("sevenDaysHistory")),
        )
