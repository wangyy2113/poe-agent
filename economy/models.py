"""经济价格领域模型。

所有时间均为带时区的 ISO-8601；价格是 poe.watch 的市场统计快照，不代表保证成交价。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class PricePoint:
    item_id: int | str
    name: str
    category: str
    group: str
    mean_chaos: float | None
    min_chaos: float | None
    max_chaos: float | None
    divine: float | None
    daily: int | None
    change_percent: float | None
    low_confidence: bool
    seven_day_history: tuple[float, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PriceSearch:
    query: str
    resolved_name: str
    requested_category: str
    categories_searched: tuple[str, ...]
    league: str
    source: str
    source_url: str
    fetched_at: str
    from_cache: bool
    stale: bool
    degraded: bool
    warning: str
    prices: tuple[PricePoint, ...] = field(default_factory=tuple)

    @property
    def found(self) -> bool:
        return bool(self.prices)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["found"] = self.found
        out["categories_searched"] = list(self.categories_searched)
        out["prices"] = [p.as_dict() for p in self.prices]
        return out


@dataclass(frozen=True)
class PriceObservation:
    observed_at: str
    mean_chaos: float
    min_chaos: float | None
    max_chaos: float | None
    daily: int | None
    low_confidence: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PriceTrend:
    item_id: int | str
    name: str
    category: str
    group: str
    basis: str
    timestamps_available: bool
    status: str
    reason: str
    observation_count: int
    first_observed_at: str | None
    last_observed_at: str | None
    first_mean_chaos: float | None
    last_mean_chaos: float | None
    change_chaos: float | None
    change_percent: float | None
    min_mean_chaos: float | None
    max_mean_chaos: float | None
    direction: str
    low_confidence_count: int
    series_values: tuple[float, ...] = field(default_factory=tuple)
    observations: tuple[PriceObservation, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["series_values"] = list(self.series_values)
        out["observations"] = [item.as_dict() for item in self.observations]
        return out


@dataclass(frozen=True)
class PriceTrendSearch:
    query: str
    resolved_name: str
    requested_category: str
    categories_searched: tuple[str, ...]
    league: str
    source: str
    source_url: str
    fetched_at: str
    from_cache: bool
    stale: bool
    degraded: bool
    warning: str
    window_hours: int
    minimum_observations: int
    trends: tuple[PriceTrend, ...] = field(default_factory=tuple)

    @property
    def found(self) -> bool:
        return bool(self.trends)

    @property
    def analyzable(self) -> bool:
        return any(item.status == "ready" for item in self.trends)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["found"] = self.found
        out["analyzable"] = self.analyzable
        out["categories_searched"] = list(self.categories_searched)
        out["trends"] = [item.as_dict() for item in self.trends]
        return out


@dataclass(frozen=True)
class MaterialRequest:
    """用户明确给出的一个材料项；不包含任何制作路径推断。"""

    query: str
    quantity: float
    category: str = "auto"


@dataclass(frozen=True)
class MaterialCostLine:
    query: str
    quantity: float
    requested_category: str
    status: str
    reason: str
    resolved_name: str
    categories_searched: tuple[str, ...]
    league: str
    source: str
    source_url: str
    fetched_at: str
    from_cache: bool
    stale: bool
    degraded: bool
    warning: str
    low_confidence: bool
    unit_price: PricePoint | None
    candidate_count: int
    subtotal_mean_chaos: float | None
    subtotal_min_chaos: float | None
    subtotal_max_chaos: float | None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["categories_searched"] = list(self.categories_searched)
        out["unit_price"] = self.unit_price.as_dict() if self.unit_price else None
        return out


@dataclass(frozen=True)
class MaterialCostEstimate:
    """明确材料清单的确定性加总；缺任一单价时不输出不完整总价。"""

    league: str
    source: str
    source_url: str
    fetched_at: str
    from_cache: bool
    stale: bool
    degraded: bool
    warning: str
    calculable: bool
    unresolved_count: int
    total_mean_chaos: float | None
    total_min_chaos: float | None
    total_max_chaos: float | None
    materials: tuple[MaterialCostLine, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["materials"] = [item.as_dict() for item in self.materials]
        return out
