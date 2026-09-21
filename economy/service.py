"""实时价格服务：名称解析、TTL 缓存、陈旧降级和结果规范化。"""

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
    PricePoint,
    PriceSearch,
)
from economy.poe_watch import PoeWatchClient, PoeWatchError
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


class EconomyService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        client: PoeWatchClient,
        cache_ttl: timedelta = timedelta(minutes=30),
        stale_after: timedelta = timedelta(hours=6),
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.conn = conn
        self.client = client
        self.cache_ttl = cache_ttl
        self.stale_after = stale_after
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

        leagues, league_snapshot = self._leagues()
        selected_league = self._select_league(league, leagues)
        resolved_name, inferred, inferred_upstream = self._resolve_name(raw_query)
        logical_category = inferred if requested == "auto" and inferred else requested
        upstream_categories = (
            inferred_upstream
            if inferred_upstream and logical_category == "unique"
            else CATEGORY_MAP[logical_category]
            if logical_category != "auto"
            else tuple(c for values in CATEGORY_MAP.values() for c in values)
        )

        snapshots = [self._prices(selected_league, c) for c in upstream_categories]
        wanted = normalize(resolved_name)
        prices: list[PricePoint] = []
        for snapshot in snapshots:
            for row in snapshot.payload:
                name = str(row.get("name") or "").strip()
                row_name = normalize(name)
                # ``query`` 既可来自结构化 API（通常就是名字），也可来自聊天整句。
                # 只接受完整名称相等或完整名称作为问句子串，不做编辑距离/模糊猜测。
                if name and (row_name == wanted or (len(row_name) >= 4 and row_name in wanted)):
                    prices.append(self._price_point(row))

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
                mean_chaos=1.0,
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
            warning=warning,
            prices=tuple(prices),
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
            elif point is not None and point.mean_chaos is None:
                status, reason = "missing_price", "上游未提供混沌石均价"
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
                warning=result.warning,
                low_confidence=bool(point and point.low_confidence),
                unit_price=point,
                candidate_count=len(matches),
                subtotal_mean_chaos=subtotal(point.mean_chaos if point else None),
                subtotal_min_chaos=subtotal(point.min_chaos if point else None),
                subtotal_max_chaos=subtotal(point.max_chaos if point else None),
            ))

        def total(field: str) -> float | None:
            values = [getattr(line, field) for line in lines]
            if any(value is None for value in values):
                return None
            return float(sum((Decimal(str(value)) for value in values), Decimal("0")))

        mean_total = total("subtotal_mean_chaos")
        unresolved = sum(line.status != "priced" for line in lines)
        warnings: list[str] = []
        if unresolved:
            warnings.append(f"{unresolved} 项未取得唯一可用均价，总成本不可计算")
        if any(line.stale for line in lines):
            warnings.append("至少一个材料使用陈旧快照")
        elif any(line.degraded for line in lines):
            warnings.append("至少一个材料在上游刷新失败后使用缓存")
        if any(line.low_confidence for line in lines):
            warnings.append("至少一个材料被 poe.watch 标记为低置信度")

        return MaterialCostEstimate(
            league=selected_league,
            source=SOURCE,
            source_url=SOURCE_URL,
            fetched_at=min(line.fetched_at for line in lines),
            from_cache=all(line.from_cache for line in lines),
            stale=any(line.stale for line in lines),
            degraded=any(line.degraded for line in lines),
            warning="；".join(warnings),
            calculable=mean_total is not None,
            unresolved_count=unresolved,
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
    def _select_league(requested: str | None, leagues: list[dict[str, Any]]) -> str:
        names = [str(row.get("name") or "").strip() for row in leagues]
        names = [name for name in names if name]
        if not names:
            raise EconomyUnavailable("poe.watch 没有返回可用联盟")
        if requested and requested.strip():
            wanted = requested.strip()
            exact = next((name for name in names if name.casefold() == wanted.casefold()), None)
            if exact is None:
                raise InvalidEconomyQuery(
                    f"poe.watch 当前没有联盟 {wanted!r}；可用：{', '.join(names)}"
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
            mean_chaos=_number(row.get("mean")),
            min_chaos=_number(row.get("min")),
            max_chaos=_number(row.get("max")),
            divine=_number(row.get("divine")),
            daily=daily_value,
            change_percent=_number(row.get("change")),
            low_confidence=bool(row.get("lowConfidence", False)),
        )
