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
