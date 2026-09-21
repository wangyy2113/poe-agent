"""实时经济价格查询工具。

结果由代码直接渲染，避免模型把均值改写成保证成交价，或丢掉联盟、时间与置信度。
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from core import db as dbmod
from core.config import get_config
from economy.models import PriceSearch
from economy.poe_watch import PoeWatchClient
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


def service_from_config() -> EconomyService:
    cfg = get_config()
    if not bool(cfg.get("economy.enabled", True)):
        raise EconomyUnavailable("实时经济价格已由配置关闭")
    return EconomyService(
        dbmod.get_conn(),
        client=_shared_client(
            float(cfg.get("economy.timeout_seconds", 15)),
            float(cfg.get("economy.requests_per_second", 0.5)),
        ),
        cache_ttl=timedelta(
            minutes=float(cfg.get("economy.cache_ttl_minutes", 30))
        ),
        stale_after=timedelta(
            hours=float(cfg.get("economy.stale_after_hours", 6))
        ),
    )


def _price(value: float | None) -> str:
    if value is None:
        return "未提供"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def render_price(result: PriceSearch) -> str:
    """确定性用户文案；数值不经过模型改写。"""
    status = "缓存"
    if not result.from_cache:
        status = "刚刷新"
    if result.stale:
        status += "、已陈旧"
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
            lines += [
                f"- poe.watch 均值：**{_price(item.mean_chaos)} 混沌石**",
                f"- 观测区间：{_price(item.min_chaos)}–{_price(item.max_chaos)} 混沌石",
                f"- 神圣石口径：{_price(item.divine)} Divine",
                f"- 上游类别：{item.category or '未提供'}；日样本指标："
                f"{item.daily if item.daily is not None else '未提供'}",
            ]
            if item.change_percent is not None:
                lines.append(f"- 上游变动指标：{_price(item.change_percent)}%")
            if item.low_confidence:
                lines.append("- ⚠️ poe.watch 将该条目标记为**低置信度**。")
    lines += [
        "",
        f"> 数据源：[poe.watch]({result.source_url})；联盟：{result.league}；"
        f"抓取时间：{result.fetched_at}；状态：{status}。",
        "> mean/min/max 是市场统计快照，不是报价承诺或保证成交价。",
    ]
    if result.warning:
        lines.append(f"> 注意：{result.warning}。")
    return "\n".join(lines)


@REGISTRY.register(
    name="economy_price_lookup",
    description=(
        "查询 poe.watch 的 POE1 当前或指定联盟市场价格。支持唯一装备、通货、圣甲虫、"
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
            "url": API_URL,
            "source": result.source,
            "source_priority": 5,
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
        },
    )
