"""实时经济价格查询工具。

结果由代码直接渲染，避免模型把均值改写成保证成交价，或丢掉联盟、时间与置信度。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from functools import lru_cache

from core import db as dbmod
from core.config import get_config
from economy.models import MaterialCostEstimate, MaterialRequest, PriceSearch, PriceTrendSearch
from economy.poe_watch import PoeWatchClient
from economy.trade_api import API_URL as TRADE_API_URL
from economy.trade_api import TradeApiClient
from economy.service import (
    API_URL,
    PUBLIC_CATEGORIES,
    EconomyService,
    EconomyUnavailable,
    InvalidEconomyQuery,
)
from tools.registry import REGISTRY, ToolResult


@lru_cache(maxsize=4)
def _shared_client(timeout_seconds: float, requests_per_second: float) -> PoeWatchClient:
    """跨请求复用限速状态，避免每次建客户端都把令牌桶重置。"""
    return PoeWatchClient(
        timeout_seconds=timeout_seconds,
        requests_per_second=requests_per_second,
    )


@lru_cache(maxsize=4)
def _shared_trade_client(
    timeout_seconds: float,
    requests_per_second: float,
    user_agent: str,
    static_ttl_hours: float,
    minimum_samples: int,
) -> TradeApiClient:
    return TradeApiClient(
        timeout_seconds=timeout_seconds,
        requests_per_second=requests_per_second,
        user_agent=user_agent,
        static_ttl=timedelta(hours=static_ttl_hours),
        minimum_samples=minimum_samples,
    )


def service_from_config() -> EconomyService:
    cfg = get_config()
    if not bool(cfg.get("economy.enabled", True)):
        raise EconomyUnavailable("实时经济价格已由配置关闭")
    fallback_enabled = bool(cfg.get("economy.trade_fallback.enabled", True))
    trade_client = None
    if fallback_enabled:
        trade_client = _shared_trade_client(
            float(cfg.get("economy.trade_fallback.timeout_seconds", 15)),
            float(cfg.get("economy.trade_fallback.requests_per_second", 0.25)),
            str(cfg.get(
                "economy.trade_fallback.user_agent",
                "poe1-agent/0.2 (local economy fallback; "
                "github.com/wangyy2113/poe-agent)",
            )),
            float(cfg.get("economy.trade_fallback.static_ttl_hours", 24)),
            int(cfg.get("economy.trade_fallback.minimum_samples", 5)),
        )
    return EconomyService(
        dbmod.get_conn(),
        client=_shared_client(
            float(cfg.get("economy.timeout_seconds", 15)),
            float(cfg.get("economy.requests_per_second", 0.5)),
        ),
        trade_client=trade_client,
        cache_ttl=timedelta(
            minutes=float(cfg.get("economy.cache_ttl_minutes", 30))
        ),
        stale_after=timedelta(
            hours=float(cfg.get("economy.stale_after_hours", 6))
        ),
        observation_retention=timedelta(
            days=float(cfg.get("economy.history_retention_days", 90))
        ),
        max_trend_points=int(cfg.get("economy.max_trend_points", 200)),
    )


def _price(value: float | None) -> str:
    if value is None:
        return "未提供"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _citation_url(source: str) -> str:
    return TRADE_API_URL if source == "GGG Trade API" else API_URL


def _source_priority(source: str) -> int:
    return 1 if source == "GGG Trade API" else 5


def render_price(result: PriceSearch) -> str:
    """确定性用户文案；数值不经过模型改写。"""
    status = "缓存"
    if not result.from_cache:
        status = "刚刷新"
    if result.stale:
        status += "、已陈旧"
    elif result.fallback:
        status += "、主源失败后使用官方备用源"
    elif result.degraded:
        status += "、上游刷新失败后降级"

    lines = [
        f"### {result.resolved_name} · {result.league} 价格快照",
        "",
    ]
    if not result.prices:
        lines.append("未找到同名条目；没有进行模糊匹配，也没有按同类物品估价。")
    else:
        for item in result.prices:
            if item.group == "quote_unit":
                lines.append("- 计价单位：**1 混沌石 = 1 混沌石**（单位恒等式，非成交统计）。")
                continue
            if item.price_basis == "ggg_listing_median":
                if item.representative_chaos is None:
                    lines.append(
                        f"- **不输出代表价**：过滤后只有 {item.sample_count} 条有效挂单，"
                        f"最低要求 {item.minimum_sample_count} 条。"
                    )
                else:
                    lines += [
                        f"- 在线挂单中位数：**{_price(item.median_chaos)} 混沌石**",
                        f"- 保留样本范围：{_price(item.min_chaos)}–"
                        f"{_price(item.max_chaos)} 混沌石",
                    ]
                lines += [
                    f"- 样本：保留 {item.sample_count} / 原始 {item.raw_listing_count} 条；"
                    f"最低要求 {item.minimum_sample_count} 条",
                    f"- 过滤：跨联盟 {item.cross_league_filtered_count} 条、无效报价 "
                    f"{item.invalid_quote_filtered_count} 条、统计离群 {item.outlier_filtered_count} 条",
                    f"- 挂单时间：{item.earliest_listing_at or '未提供'} → "
                    f"{item.latest_listing_at or '未提供'}",
                    f"- 上游类别：{item.category or '未提供'}",
                ]
            else:
                lines += [
                    f"- poe.watch 均值：**{_price(item.mean_chaos)} 混沌石**",
                    f"- 观测区间：{_price(item.min_chaos)}–{_price(item.max_chaos)} 混沌石",
                    f"- 神圣石口径：{_price(item.divine)} Divine",
                    f"- 上游类别：{item.category or '未提供'}；日样本指标："
                    f"{item.daily if item.daily is not None else '未提供'}",
                ]
            if item.change_percent is not None:
                lines.append(f"- 上游变动指标：{_price(item.change_percent)}%")
            if item.low_confidence and item.price_basis == "poe_watch_mean":
                lines.append("- ⚠️ poe.watch 将该条目标记为**低置信度**。")
    lines += [
        "",
        f"> 数据源：[{result.source}]({result.source_url})；联盟：{result.league}；"
        f"抓取时间：{result.fetched_at}；状态：{status}。",
        "> poe.watch 的 mean/min/max 是市场统计快照；GGG Trade API 的数值是在线挂单中位数"
        "与过滤后范围。两者都不是报价承诺或保证成交价。",
    ]
    if result.warning:
        lines.append(f"> 注意：{result.warning}。")
    return "\n".join(lines)


def render_trend(result: PriceTrendSearch) -> str:
    """确定性趋势文案；只陈述本机已保存的实际观测序列。"""
    status = "缓存" if result.from_cache else "刚刷新"
    if result.stale:
        status += "、已陈旧"
    elif result.fallback:
        status += "、主源失败后使用官方备用源"
    elif result.degraded:
        status += "、上游刷新失败后降级"
    lines = [
        f"### {result.resolved_name} · {result.league} 价格趋势",
        "",
    ]
    if not result.trends:
        lines.append("未找到同名条目；没有进行模糊匹配，也没有按同类物品推断趋势。")
    for trend in result.trends:
        lines.append(f"#### {trend.name}（{trend.category or '未提供类别'}）")
        if trend.status != "ready":
            lines.append(f"- 暂不可判定：{trend.reason}。")
            if trend.last_mean_chaos is not None:
                lines.append(
                    f"- 当前唯一观测：{_price(trend.last_mean_chaos)} 混沌石"
                    f"（{trend.last_observed_at}）。"
                )
            continue
        direction = {"up": "上涨", "down": "下跌", "flat": "持平"}[trend.direction]
        percent = (
            f"{_price(trend.change_percent)}%"
            if trend.change_percent is not None
            else "起点为 0，百分比不可计算"
        )
        lines += [
            f"- 窗口内 {trend.observation_count} 个观测点："
            f"{_price(trend.first_mean_chaos)} → {_price(trend.last_mean_chaos)} 混沌石",
            f"- 变化：{direction} {_price(abs(trend.change_chaos or 0))} 混沌石；{percent}",
            f"- 观测均价范围：{_price(trend.min_mean_chaos)}–"
            f"{_price(trend.max_mean_chaos)} 混沌石",
        ]
        if trend.timestamps_available:
            lines.append(
                f"- 起止时间：{trend.first_observed_at} → {trend.last_observed_at}"
            )
        else:
            lines.append(
                "- 序列口径：poe.watch `sevenDaysHistory`；上游未提供逐点时间戳。"
            )
        if trend.low_confidence_count and trend.basis == "local_observations":
            lines.append(
                f"- ⚠️ 其中 {trend.low_confidence_count} 个观测被 poe.watch 标记为低置信度。"
            )
        elif trend.low_confidence_count:
            lines.append("- ⚠️ poe.watch 将该条目标记为低置信度。")
    lines += [
        "",
        f"> 当前价格数据源：[{result.source}]({result.source_url})；联盟：{result.league}；"
        f"窗口：{result.window_hours} 小时；当前快照：{result.fetched_at}；状态：{status}。",
        "> 趋势优先使用本机保存的带时间戳快照；默认七日窗口点数不足时可使用上游明确的"
        " `sevenDaysHistory` 数列，但不使用单次 change 字段冒充历史。它不是未来价格预测、"
        "利润承诺或 Flip 建议。",
    ]
    if result.warning:
        lines.append(f"> 注意：{result.warning}。")
    return "\n".join(lines)


def render_material_cost(result: MaterialCostEstimate) -> str:
    """确定性材料成本表；任何缺价都不会被静默略过后给出部分总价。"""
    lines = [
        f"### {result.league} 材料清单成本快照",
        "",
        "| 材料 | 数量 | 代表单价（混沌石） | 代表小计（混沌石） | 观测小计区间 | 状态 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    provenance: list[str] = []
    for item in result.materials:
        point = item.unit_price
        name = item.resolved_name or item.query
        status = "已计价"
        if item.status != "priced":
            status = item.reason
        elif point and point.group == "quote_unit":
            status = "已计价；混沌石计价单位恒等于 1"
        elif point and point.price_basis == "ggg_listing_median":
            status = "已计价；官方在线挂单中位数"
        elif item.low_confidence:
            status = "已计价；低置信度"
        if item.stale:
            status += "；陈旧"
        elif item.fallback:
            status += "；官方备用源"
        elif item.degraded:
            status += "；缓存降级"
        lines.append(
            f"| {name} | {_price(item.quantity)} | "
            f"{_price(point.representative_chaos if point else None)} | "
            f"{_price(item.subtotal_representative_chaos)} | "
            f"{_price(item.subtotal_min_chaos)}–{_price(item.subtotal_max_chaos)} | "
            f"{status} |"
        )
        snapshot_status = "缓存" if item.from_cache else "刚刷新"
        if item.stale:
            snapshot_status += "、已陈旧"
        elif item.fallback:
            snapshot_status += "、主源失败后使用官方备用源"
        elif item.degraded:
            snapshot_status += "、上游失败后降级"
        provenance.append(
            f"- {name}：{item.source} / {item.league} / {item.fetched_at} / {snapshot_status}"
        )

    lines.append("")
    if result.calculable:
        total_label = "均值总成本" if result.total_mean_chaos is not None else "代表价总成本"
        lines += [
            f"**{total_label}：{_price(result.total_representative_chaos)} 混沌石**",
            f"观测区间总计：{_price(result.total_min_chaos)}–"
            f"{_price(result.total_max_chaos)} 混沌石。",
        ]
    else:
        lines.append(
            "**总成本不可计算**：至少一项没有唯一可用代表价；未用部分清单冒充完整总价。"
        )
    lines += ["", "逐项快照：", *provenance]
    lines += [
        "",
        f"> 数据源：{result.source}"
        f"{f'（{result.source_url}）' if result.source_url else ''}；联盟：{result.league}；"
        f"最早抓取时间：{result.fetched_at}。每项的抓取时间与缓存状态保留在结构化结果中。",
        "> 单价可能来自 poe.watch mean 市场统计或 GGG 在线挂单中位数，具体口径保留在"
        "逐项结构化结果中；数量乘法与总和由代码计算，不是报价承诺、保证成交价、"
        "制作成功率或利润预测。",
    ]
    if result.warning:
        lines.append(f"> 注意：{result.warning}。")
    return "\n".join(lines)


@REGISTRY.register(
    name="economy_price_lookup",
    description=(
        "查询 POE1 当前或指定联盟市场价格。主源为 poe.watch；主源无缓存且失败时，"
        "通货、圣甲虫、命运卡与碎片会显式降级到 GGG Trade API 在线挂单中位数。"
        "支持唯一装备、通货、圣甲虫、"
        "命运卡与碎片；返回混沌石均值/区间、神圣石口径、抓取时间、缓存/陈旧状态和"
        "低置信度标记。只做精确名称匹配，不提供稀有装备估值、制作成本、趋势或 Flip 建议。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "category": {
                "type": "string",
                "enum": list(PUBLIC_CATEGORIES),
                "default": "auto",
            },
            "league": {
                "type": "string",
                "maxLength": 100,
                "description": "可选；留空时选择 poe.watch 返回的当前普通挑战联盟",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
def economy_price_lookup(
    query: str, category: str = "auto", league: str | None = None
) -> ToolResult:
    try:
        result = service_from_config().price(query, category=category, league=league)
    except (EconomyUnavailable, InvalidEconomyQuery) as exc:
        return ToolResult(ok=False, error=str(exc), meta={"source": "poe.watch"})

    rendered = render_price(result)
    citations = []
    if result.prices:
        citations.append({
            "chunk_id": (
                f"economy:{result.league}:{result.prices[0].category}:"
                f"{result.prices[0].item_id}:{result.fetched_at}"
            ),
            "entity_id": None,
            "title": f"{result.resolved_name} · {result.league} 价格快照",
            "url": _citation_url(result.source),
            "source": result.source,
            "source_priority": _source_priority(result.source),
            "can_support_facts": True,
            "text": rendered,
            "channel": "live_economy",
        })
    return ToolResult(
        ok=True,
        data={**result.as_dict(), "answer": rendered},
        citations=citations,
        meta={
            "channel": "live_economy",
            "source": result.source,
            "from_cache": result.from_cache,
            "stale": result.stale,
            "degraded": result.degraded,
            "fallback": result.fallback,
        },
    )


@REGISTRY.register(
    name="economy_price_trend",
    description=(
        "查询 POE1 当前或指定联盟的历史价格趋势。优先使用本机保存的 poe.watch 快照；"
        "默认七日窗口可回退到上游 sevenDaysHistory 数列，但会明确标注其没有逐点时间戳。"
        "至少两个有效均价点才计算涨跌；不使用单次 change 推算，不预测未来或提供 Flip 建议。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "category": {
                "type": "string",
                "enum": list(PUBLIC_CATEGORIES),
                "default": "auto",
            },
            "league": {"type": "string", "maxLength": 100},
            "window_hours": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2160,
                "default": 168,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
def economy_price_trend(
    query: str,
    category: str = "auto",
    league: str | None = None,
    window_hours: int = 168,
) -> ToolResult:
    try:
        result = service_from_config().trend(
            query,
            category=category,
            league=league,
            window_hours=window_hours,
        )
    except (EconomyUnavailable, InvalidEconomyQuery) as exc:
        return ToolResult(ok=False, error=str(exc), meta={"source": "poe.watch"})

    rendered = render_trend(result)
    identity = "|".join(
        f"{item.item_id}:{item.last_observed_at}:{item.observation_count}"
        for item in result.trends
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    citations = [{
        "chunk_id": f"economy-trend:{result.league}:{digest}",
        "entity_id": None,
        "title": f"{result.resolved_name} · {result.league} 价格趋势",
        "url": _citation_url(result.source),
        "source": result.source,
        "source_priority": _source_priority(result.source),
        "can_support_facts": True,
        "text": rendered,
        "channel": "live_economy",
    }] if any(item.observation_count for item in result.trends) else []
    return ToolResult(
        ok=True,
        data={**result.as_dict(), "answer": rendered},
        citations=citations,
        meta={
            "channel": "live_economy",
            "source": result.source,
            "from_cache": result.from_cache,
            "stale": result.stale,
            "degraded": result.degraded,
            "fallback": result.fallback,
            "analyzable": result.analyzable,
        },
    )


@REGISTRY.register(
    name="economy_material_cost",
    description=(
        "对用户明确给出的 POE1 材料清单做实时成本加总。逐项精确查询代表单价，"
        "优先使用 poe.watch mean，必要时显式降级到 GGG 在线挂单中位数；保留每项联盟、抓取时间、缓存/"
        "陈旧和低置信度状态。缺少任一均价时明确返回不可计算，不生成材料清单、制作步骤、"
        "成功率、利润或 Flip 建议。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "materials": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 200},
                        "quantity": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000000000},
                        "category": {
                            "type": "string",
                            "enum": list(PUBLIC_CATEGORIES),
                            "default": "auto",
                        },
                    },
                    "required": ["query", "quantity"],
                    "additionalProperties": False,
                },
            },
            "league": {
                "type": "string",
                "maxLength": 100,
                "description": "可选；整张清单必须使用同一联盟",
            },
        },
        "required": ["materials"],
        "additionalProperties": False,
    },
)
def economy_material_cost(
    materials: list[dict[str, object]], league: str | None = None
) -> ToolResult:
    try:
        requests = [
            MaterialRequest(
                query=str(item.get("query") or ""),
                quantity=item.get("quantity"),  # type: ignore[arg-type]
                category=str(item.get("category") or "auto"),
            )
            for item in materials
            if isinstance(item, dict)
        ]
        if len(requests) != len(materials):
            raise InvalidEconomyQuery("materials 中每项都必须是对象")
        result = service_from_config().material_cost(requests, league=league)
    except (EconomyUnavailable, InvalidEconomyQuery, TypeError) as exc:
        return ToolResult(ok=False, error=str(exc), meta={"source": "poe.watch"})

    rendered = render_material_cost(result)
    identity = "|".join(
        f"{item.resolved_name}:{item.quantity}:{item.fetched_at}"
        for item in result.materials
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    sources = {item.source for item in result.materials if item.unit_price is not None}
    citations = [{
        "chunk_id": f"economy-cost:{result.league}:{digest}:{source}",
        "entity_id": None,
        "title": f"{result.league} 材料清单成本快照",
        "url": _citation_url(source),
        "source": source,
        "source_priority": _source_priority(source),
        "can_support_facts": True,
        "text": rendered,
        "channel": "live_economy",
    } for source in sorted(sources)]
    return ToolResult(
        ok=True,
        data={**result.as_dict(), "answer": rendered},
        citations=citations,
        meta={
            "channel": "live_economy",
            "source": result.source,
            "from_cache": result.from_cache,
            "stale": result.stale,
            "degraded": result.degraded,
            "fallback": result.fallback,
            "calculable": result.calculable,
        },
    )
