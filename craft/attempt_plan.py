"""已验证普通词缀池上的独立尝试分布与材料预算。

本模块刻意只做一个窄模型：每次尝试都从完全相同的初始条件开始，每次恰好进行
一次 :mod:`craft.pool` 已证明完整的同侧 ``normal`` 加权选择，命中目标组即成功。
因此可以使用几何分布；任何会保留中间状态、改变词缀池、一次生成多个词缀或带保底的
真实制作操作都不属于这个模型。

材料清单必须由调用方逐项给出。本模块不会从通货名、制作目标或市场价格反推工艺步骤。
价格只是可选快照：任一材料没有唯一代表价、联盟/来源不一致、快照陈旧或采样时间跨度
超过五分钟时，仍返回尝试次数和材料数量，但不输出部分总成本。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any, Literal

from craft.pool import GenerationSide, PoolOddsReport, analyze_affix_pool
from economy.models import MaterialCostEstimate

PlanStatus = Literal["exact", "unknown"]
PricingStatus = Literal[
    "not_requested", "complete", "incomplete", "inconsistent_snapshot",
]

MAX_MATERIALS = 20
MAX_ATTEMPTS = 1_000_000
MAX_PRICE_SPAN_SECONDS = 300

_MILESTONES: tuple[tuple[str, Decimal], ...] = (
    ("p50", Decimal("0.50")),
    ("p90", Decimal("0.90")),
    ("p95", Decimal("0.95")),
)


@dataclass(frozen=True)
class AttemptMaterial:
    """一次独立尝试明确消耗的材料；数量不是整条路线的总量。"""

    query: str
    quantity_per_attempt: Decimal
    category: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "quantity_per_attempt": _decimal_text(self.quantity_per_attempt),
            "category": self.category,
        }


@dataclass(frozen=True)
class PricingDecision:
    status: PricingStatus
    reason: str
    estimate: MaterialCostEstimate | None = None
    snapshot_started_at: str = ""
    snapshot_finished_at: str = ""

    @property
    def per_attempt_chaos(self) -> Decimal | None:
        if self.status != "complete" or self.estimate is None:
            return None
        value = self.estimate.total_representative_chaos
        return Decimal(str(value)) if value is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "league": self.estimate.league if self.estimate else "",
            "source": self.estimate.source if self.estimate else "",
            "snapshot_started_at": self.snapshot_started_at,
            "snapshot_finished_at": self.snapshot_finished_at,
            "per_attempt_representative_chaos": (
                _decimal_text(self.per_attempt_chaos)
                if self.per_attempt_chaos is not None else None
            ),
            "warning": self.estimate.warning if self.estimate else "",
            "materials": (
                [line.as_dict() for line in self.estimate.materials]
                if self.estimate else []
            ),
        }


@dataclass
class AttemptPlanReport:
    status: PlanStatus
    pool: PoolOddsReport
    max_attempts: int
    materials: tuple[AttemptMaterial, ...]
    pricing: PricingDecision
    distribution: dict[str, Any] | None = None
    material_budget: list[dict[str, Any]] = field(default_factory=list)
    cost_budget: dict[str, Any] | None = None
    assumptions: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    gaps: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pool": self.pool.to_dict(),
            "max_attempts": self.max_attempts,
            "materials_per_attempt": [item.to_dict() for item in self.materials],
            "distribution": self.distribution,
            "material_budget": list(self.material_budget),
            "pricing": self.pricing.to_dict(),
            "cost_budget": self.cost_budget,
            "assumptions": list(self.assumptions),
            "exclusions": list(self.exclusions),
            "gaps": list(self.gaps),
        }


def parse_materials(raw: list[dict[str, object]]) -> tuple[AttemptMaterial, ...]:
    """校验 API/工具输入，并保留用户给出的十进制数量。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("materials 必须包含至少一项明确材料")
    if len(raw) > MAX_MATERIALS:
        raise ValueError(f"materials 最多包含 {MAX_MATERIALS} 项")

    result: list[AttemptMaterial] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"materials 第 {index} 项必须是对象")
        unknown = set(item) - {"query", "quantity_per_attempt", "category"}
        if unknown:
            raise ValueError(f"materials 第 {index} 项含未知字段：{', '.join(sorted(unknown))}")
        query = str(item.get("query") or "").strip()
        category = str(item.get("category") or "auto").strip()
        if not query or len(query) > 200:
            raise ValueError(f"materials 第 {index} 项 query 必须为 1..200 个字符")
        if not category or len(category) > 80:
            raise ValueError(f"materials 第 {index} 项 category 无效")
        raw_quantity = item.get("quantity_per_attempt")
        if isinstance(raw_quantity, bool):
            raise ValueError(f"materials 第 {index} 项数量必须是有限正数")
        try:
            quantity = Decimal(str(raw_quantity))
        except Exception as exc:  # noqa: BLE001 - Decimal 的具体异常对调用方没有帮助
            raise ValueError(f"materials 第 {index} 项数量必须是有限正数") from exc
        if not quantity.is_finite() or quantity <= 0 or quantity > Decimal("1000000000"):
            raise ValueError(f"materials 第 {index} 项数量必须在 0..1,000,000,000 之间")
        identity = (query, category)
        if identity in seen:
            raise ValueError(f"材料 {query!r}（{category}）重复；请先合并每次用量")
        seen.add(identity)
        result.append(AttemptMaterial(query, quantity, category))
    return tuple(result)


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    text = format(value, ".18g")
    if "e" not in text.lower() and "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def validate_pricing_snapshot(
    materials: tuple[AttemptMaterial, ...],
    estimate: MaterialCostEstimate | None,
) -> PricingDecision:
    """只放行完整、同联盟、同来源且时间跨度受限的每次尝试成本。"""
    if estimate is None:
        return PricingDecision("not_requested", "调用方未请求市场计价")
    if len(estimate.materials) != len(materials):
        return PricingDecision("incomplete", "价格结果与材料清单行数不一致", estimate)
    if not estimate.calculable or estimate.unresolved_count:
        return PricingDecision(
            "incomplete", "至少一项材料没有唯一可用代表价；不输出部分总成本", estimate
        )
    if estimate.stale:
        return PricingDecision(
            "inconsistent_snapshot", "至少一项材料价格快照已陈旧；不输出当前成本", estimate
        )

    timestamps: list[datetime] = []
    sources: set[str] = set()
    leagues: set[str] = set()
    for material, line in zip(materials, estimate.materials, strict=True):
        if (
            line.query != material.query
            or Decimal(str(line.quantity)) != material.quantity_per_attempt
            or line.requested_category != material.category
            or line.status != "priced"
            or line.unit_price is None
            or line.unit_price.representative_chaos is None
        ):
            return PricingDecision(
                "incomplete", "价格行与显式材料、数量或代表价不一致", estimate
            )
        parsed = _timestamp(line.fetched_at)
        if parsed is None:
            return PricingDecision(
                "inconsistent_snapshot", "至少一项价格缺少带时区抓取时间", estimate
            )
        timestamps.append(parsed)
        sources.add(line.source)
        leagues.add(line.league)

    started, finished = min(timestamps), max(timestamps)
    kwargs = {
        "snapshot_started_at": started.isoformat(timespec="seconds"),
        "snapshot_finished_at": finished.isoformat(timespec="seconds"),
    }
    if len(leagues) != 1 or not estimate.league or estimate.league not in leagues:
        return PricingDecision(
            "inconsistent_snapshot", "材料价格不属于同一联盟", estimate, **kwargs
        )
    if len(sources) != 1 or estimate.source not in sources:
        return PricingDecision(
            "inconsistent_snapshot", "材料价格混用了不同统计来源", estimate, **kwargs
        )
    if (finished - started).total_seconds() > MAX_PRICE_SPAN_SECONDS:
        return PricingDecision(
            "inconsistent_snapshot",
            f"材料价格采样跨度超过 {MAX_PRICE_SPAN_SECONDS} 秒",
            estimate,
            **kwargs,
        )
    return PricingDecision("complete", "全部材料具有同口径代表价", estimate, **kwargs)


def _milestone_attempts(probability: Decimal, target: Decimal) -> int:
    """返回首次达到目标累计概率的最小正整数尝试数。"""
    if probability >= 1:
        return 1
    estimate = max(
        1,
        math.ceil(math.log1p(-float(target)) / math.log1p(-float(probability))),
    )
    with localcontext() as ctx:
        ctx.prec = 70

        def reached(attempts: int) -> bool:
            return Decimal(1) - (Decimal(1) - probability) ** attempts >= target

        while estimate > 1 and reached(estimate - 1):
            estimate -= 1
        while not reached(estimate):
            estimate += 1
    return estimate


def _scaled_budget(
    materials: tuple[AttemptMaterial, ...],
    expected_attempts: Decimal,
    milestones: list[dict[str, Any]],
    max_attempts: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for material in materials:
        result.append({
            **material.to_dict(),
            "expected_until_success": _decimal_text(
                material.quantity_per_attempt * expected_attempts
            ),
            "expected_with_attempt_cap": None,  # 由调用处填入，避免重复求分布
            "maximum_at_attempt_cap": _decimal_text(
                material.quantity_per_attempt * Decimal(max_attempts)
            ),
            "milestones": {
                item["label"]: _decimal_text(
                    material.quantity_per_attempt * Decimal(item["attempts"])
                )
                for item in milestones
            },
        })
    return result


def plan_affix_attempts(
    conn: sqlite3.Connection,
    *,
    base_type: str,
    item_level: int,
    generation_side: GenerationSide,
    target_query: str,
    materials: tuple[AttemptMaterial, ...],
    max_attempts: int,
    market_cost: MaterialCostEstimate | None = None,
    pool_report: PoolOddsReport | None = None,
) -> AttemptPlanReport:
    """把已验证词缀池提升为独立尝试分布、材料预算和可选成本快照。"""
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError("max_attempts 必须是整数")
    if not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise ValueError(f"max_attempts 必须在 1..{MAX_ATTEMPTS} 范围内")
    if not materials or len(materials) > MAX_MATERIALS:
        raise ValueError(f"materials 必须包含 1..{MAX_MATERIALS} 项")

    pool = pool_report or analyze_affix_pool(
        conn,
        base_type=base_type,
        item_level=item_level,
        generation_side=generation_side,
        target_query=target_query,
    )
    pricing = validate_pricing_snapshot(materials, market_cost)
    common = dict(
        pool=pool,
        max_attempts=max_attempts,
        materials=materials,
        pricing=pricing,
        assumptions=[
            "每次尝试都从完全相同的初始条件重新开始",
            "每次尝试恰好进行一次已验证 normal 同侧加权选择",
            "各次尝试独立同分布，命中目标 correct_group 即停止",
            "没有保底、批量生成、状态继承、失败返还或中间产物复用",
            "材料数量由调用方逐项明确给出，本模块没有推断制作步骤",
        ],
        exclusions=[
            "不表示混沌石、崇高石、精华、化石、Harvest 或其它真实工艺的一次成功率",
            "不处理一次操作生成多个词缀、已有 family 排除、前后缀容量或装备当前状态",
            "不比较多步路线、机会成本、交易摩擦、出售回收、最快或最经济方案",
            "市场价格只是同联盟短时间快照，不是报价承诺或保证成交价",
        ],
    )
    if pool.status != "exact":
        return AttemptPlanReport(
            status="unknown",
            gaps=[gap.to_dict() for gap in pool.gaps],
            **common,
        )

    assert pool.probability_numerator is not None
    assert pool.probability_denominator is not None
    with localcontext() as ctx:
        ctx.prec = 70
        probability = (
            Decimal(pool.probability_numerator) / Decimal(pool.probability_denominator)
        )
        failure_cap = (Decimal(1) - probability) ** max_attempts
        success_cap = Decimal(1) - failure_cap
        expected_attempts = Decimal(1) / probability
        expected_with_cap = success_cap / probability

        milestones: list[dict[str, Any]] = []
        for label, target in _MILESTONES:
            attempts = _milestone_attempts(probability, target)
            achieved = Decimal(1) - (Decimal(1) - probability) ** attempts
            milestones.append({
                "label": label,
                "target_probability": _decimal_text(target),
                "attempts": attempts,
                "achieved_probability": _decimal_text(achieved),
                "within_attempt_cap": attempts <= max_attempts,
            })

        distribution = {
            "model": "geometric_iid_single_weighted_selection_v1",
            "single_attempt_probability_numerator": pool.probability_numerator,
            "single_attempt_probability_denominator": pool.probability_denominator,
            "single_attempt_probability": _decimal_text(probability),
            "expected_attempts_until_success": _decimal_text(expected_attempts),
            "attempt_cap": max_attempts,
            "success_probability_within_cap": _decimal_text(success_cap),
            "exhaustion_probability": _decimal_text(failure_cap),
            "expected_attempts_consumed_with_cap": _decimal_text(expected_with_cap),
            "milestones": milestones,
        }

        material_budget = _scaled_budget(
            materials, expected_attempts, milestones, max_attempts
        )
        for material, budget in zip(materials, material_budget, strict=True):
            budget["expected_with_attempt_cap"] = _decimal_text(
                material.quantity_per_attempt * expected_with_cap
            )

        cost_budget: dict[str, Any] | None = None
        per_attempt = pricing.per_attempt_chaos
        if per_attempt is not None:
            cost_budget = {
                "basis": "representative_chaos_snapshot",
                "per_attempt_chaos": _decimal_text(per_attempt),
                "expected_until_success_chaos": _decimal_text(
                    per_attempt * expected_attempts
                ),
                "expected_with_attempt_cap_chaos": _decimal_text(
                    per_attempt * expected_with_cap
                ),
                "maximum_at_attempt_cap_chaos": _decimal_text(
                    per_attempt * Decimal(max_attempts)
                ),
                "milestones_chaos": {
                    item["label"]: _decimal_text(
                        per_attempt * Decimal(item["attempts"])
                    )
                    for item in milestones
                },
            }

    return AttemptPlanReport(
        status="exact",
        distribution=distribution,
        material_budget=material_budget,
        cost_budget=cost_budget,
        **common,
    )
