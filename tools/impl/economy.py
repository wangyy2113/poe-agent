"""实时经济价格、成本与双边价差工具。

结果由代码直接渲染，避免模型把均值改写成保证成交价，或丢掉联盟、时间与置信度。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from functools import lru_cache

from core import db as dbmod
from core.config import get_config
from economy.models import (
    FlipAnalysis,
    MaterialCostEstimate,
    MaterialRequest,
    PriceSearch,
    PriceTrendSearch,
    TradeFeeInput,
)
from economy.poe_watch import PoeWatchClient
from economy.rare_items import (
    InvalidRareComparable,
    RareComparableEstimate,
    RareComparableService,
)
from economy.trade_api import API_URL as TRADE_API_URL
from economy.trade_api import TradeApiClient, TradeApiError
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
    unique_max_results: int,
) -> TradeApiClient:
    return TradeApiClient(
        timeout_seconds=timeout_seconds,
        requests_per_second=requests_per_second,
        user_agent=user_agent,
        static_ttl=timedelta(hours=static_ttl_hours),
        minimum_samples=minimum_samples,
        unique_max_results=unique_max_results,
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
            float(cfg.get("economy.trade_fallback.requests_per_second", 0.5)),
            str(cfg.get(
                "economy.trade_fallback.user_agent",
                "poe1-agent/0.2 (local economy fallback; "
                "github.com/wangyy2113/poe-agent)",
            )),
            float(cfg.get("economy.trade_fallback.static_ttl_hours", 24)),
            int(cfg.get("economy.trade_fallback.minimum_samples", 5)),
            int(cfg.get("economy.trade_fallback.unique_max_results", 40)),
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


def rare_service_from_config() -> RareComparableService:
    cfg = get_config()
    if not bool(cfg.get("economy.rare_comparables.enabled", True)):
        raise InvalidRareComparable("稀有装备可比挂单查询已由配置关闭")
    return RareComparableService(
        dbmod.get_conn(),
        _shared_trade_client(
            float(cfg.get("economy.trade_fallback.timeout_seconds", 15)),
            float(cfg.get("economy.trade_fallback.requests_per_second", 0.5)),
            str(cfg.get(
                "economy.trade_fallback.user_agent",
                "poe1-agent/0.2 (local economy fallback; "
                "github.com/wangyy2113/poe-agent)",
            )),
            float(cfg.get("economy.trade_fallback.static_ttl_hours", 24)),
            int(cfg.get("economy.trade_fallback.minimum_samples", 5)),
            int(cfg.get("economy.rare_comparables.max_results", 40)),
        ),
    )


def _price(value: float | None) -> str:
    if value is None:
        return "未提供"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _citation_url(source: str, source_url: str = "") -> str:
    if source == "GGG Trade API":
        return source_url or TRADE_API_URL
    return API_URL


def _source_priority(source: str) -> int:
    return 1 if source == "GGG Trade API" else 5


def render_rare_comparables(result: RareComparableEstimate) -> str:
    lines = [
        f"## {result.base_type_input or result.base_type_en} · 稀有装备可比挂单",
        "",
        f"> 数据源：{result.source}；联盟：{result.league or '未查询'}；"
        f"查询时间：{result.fetched_at}。这是在线挂单比较，不是成交记录或精确估值。",
        "",
        f"- 状态：{result.status}（{result.reason}）",
        f"- 精确底材：{result.base_type_en}；物等：{result.item_level}；"
        f"腐化：{'是' if result.corrupted else '否'}",
        f"- 词缀映射：{result.mapped_mod_count}/{result.explicit_mod_count} 条；"
        f"映射完整度：{result.comparison_quality}",
        f"- 品质筛选：{str(result.quality_filter) + '%' if result.quality_filter is not None else '未启用'}；"
        f"连接组尺寸筛选：{list(result.socket_group_sizes) if result.socket_group_sizes is not None else '未启用'}",
    ]
    for mapping in result.mapped_stats:
        labels = " / ".join(
            f"{stat_id}（{text}）"
            for stat_id, text in zip(mapping.stat_ids, mapping.official_texts, strict=True)
        )
        lines.append(f"  - `{mapping.mod_text}` → {labels}")
    if result.unmapped_mods:
        lines.append("- 未映射词缀（未进入筛选，已降低可比性）：")
        lines.extend(f"  - `{text}`" for text in result.unmapped_mods)
    quote = result.quote
    if quote is not None:
        if quote.sufficient:
            lines += [
                f"- 可比挂单中位数：**{_price(quote.median_chaos)} 混沌石**",
                f"- 保留样本范围：{_price(quote.min_chaos)}–{_price(quote.max_chaos)} 混沌石",
            ]
        else:
            lines.append(
                f"- **不输出代表价**：过滤后 {quote.sample_count} 条，"
                f"最低要求 {quote.minimum_sample_count} 条"
            )
        lines += [
            f"- 搜索总命中 {quote.search_total or 0} 条，本次抓取 "
            f"{quote.fetched_listing_count or 0} 条，过滤后保留 {quote.sample_count} 条",
            f"- 过滤：跨联盟 {quote.cross_league_filtered_count}、无效结构/身份 "
            f"{quote.invalid_quote_filtered_count}、无法换算货币 "
            f"{quote.unconverted_currency_filtered_count or 0}、统计离群 "
            f"{quote.outlier_filtered_count}",
            f"- 结构筛选：品质不符 {quote.quality_mismatch_filtered_count}、"
            f"连接组不符 {quote.socket_mismatch_filtered_count}、"
            f"挂单结构字段缺失 {quote.structure_missing_filtered_count}",
            f"- 挂单时间：{quote.earliest_listing_at or '未提供'} → "
            f"{quote.latest_listing_at or '未提供'}",
            f"- 样本边界：{quote.sample_scope}",
        ]
        if quote.divine_listing_count:
            conversion = (
                f"{_price(quote.divine_chaos_rate)} Chaos/Divine，"
                f"{quote.conversion_sample_count or 0} 条换算样本"
                if quote.conversion_status == "converted"
                else f"换算状态 {quote.conversion_status}"
            )
            lines.append(
                f"- 标价构成：Chaos {quote.chaos_listing_count or 0} 条、Divine "
                f"{quote.divine_listing_count or 0} 条；{conversion}"
            )
    lines += [
        "",
        "> 不据此判断保证成交价、制作价值、利润、是否值得分裂/镜像湖或镜子级别。",
    ]
    return "\n".join(lines)


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
                    f"{item.invalid_quote_filtered_count} 条、无法换算货币 "
                    f"{item.unconverted_currency_filtered_count or 0} 条、统计离群 "
                    f"{item.outlier_filtered_count} 条",
                    f"- 挂单时间：{item.earliest_listing_at or '未提供'} → "
                    f"{item.latest_listing_at or '未提供'}",
                    f"- 上游类别：{item.category or '未提供'}",
                ]
                if item.search_total is not None:
                    lines.append(
                        f"- 搜索结果：总命中 {item.search_total} 条，本次抓取 "
                        f"{item.fetched_listing_count} 条"
                    )
                if item.divine_listing_count:
                    if item.conversion_status == "converted":
                        conversion = (
                            f"换算率 {_price(item.divine_chaos_rate)} Chaos/Divine"
                            f"（{item.conversion_sample_count or 0} 条有效交换挂单）"
                        )
                    elif item.conversion_status == "insufficient_samples":
                        conversion = (
                            f"换算样本不足（{item.conversion_sample_count or 0} 条），"
                            "Divine 标价未计入"
                        )
                    else:
                        conversion = "换算源不可用，Divine 标价未计入"
                    lines.append(
                        f"- 标价构成：Chaos {item.chaos_listing_count or 0} 条、Divine "
                        f"{item.divine_listing_count} 条；{conversion}"
                    )
                if item.sample_scope:
                    lines.append(f"- 样本范围：{item.sample_scope}")
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


def render_flip_analysis(result: FlipAnalysis) -> str:
    """确定性双边利润文案；unknown 时不泄漏任何半成品利润数字。"""
    lines = [
        f"### {result.resolved_name} · {result.league} 双边价差",
        "",
        f"> 数据源：[{result.source}]({result.source_url})；查询时间：{result.fetched_at}；"
        f"目标数量：{_price(result.quantity)}；只保留最近 "
        f"{result.max_listing_age_hours} 小时且单条可覆盖全部数量的在线挂单。",
        "",
    ]
    for label, side in (
        ("买入侧（卖家要价）", result.buy_side),
        ("卖出侧（买家承接价）", result.sell_side),
    ):
        unit = (
            f"中位数 {_price(side.unit_chaos)} 混沌石/个；"
            f"范围 {_price(side.min_unit_chaos)}–{_price(side.max_unit_chaos)}"
            if side.status == "ready"
            else "代表价 unknown"
        )
        lines += [
            f"- {label}：{unit}",
            f"  - 样本：保留 {side.sample_count}/{side.raw_listing_count} 条；"
            f"最低要求 {side.minimum_sample_count} 条",
            f"  - 过滤：跨联盟 {side.cross_league_filtered_count}、无效报价 "
            f"{side.invalid_quote_filtered_count}、数量不足 "
            f"{side.insufficient_stock_filtered_count}、超时 "
            f"{side.stale_listing_filtered_count}、统计离群 {side.outlier_filtered_count}",
            f"  - 挂单时间：{side.earliest_listing_at or '未提供'} → "
            f"{side.latest_listing_at or '未提供'}",
        ]
    lines += [
        "",
        f"- 显式费用：买入固定 {_price(result.fees.buy_fixed_chaos)} C + "
        f"{_price(result.fees.buy_percent)}%；卖出固定 "
        f"{_price(result.fees.sell_fixed_chaos)} C + {_price(result.fees.sell_percent)}%",
        f"- 判定阈值：净利润至少 {_price(result.minimum_net_profit_chaos)} C，"
        f"利润率至少 {_price(result.minimum_margin_percent)}%",
    ]
    if not result.calculable:
        lines += [
            f"- **状态：unknown**（{result.reason}）",
            "- 净利润、利润率与 Flip 判断均不计算。",
        ]
    else:
        verdict = (
            "满足阈值，可列为 Flip 候选"
            if result.verdict == "flip_candidate"
            else "不满足 Flip 阈值"
        )
        lines += [
            f"- 买入：毛成本 {_price(result.gross_buy_cost_chaos)} C + 费用 "
            f"{_price(result.buy_fee_chaos)} C = **{_price(result.total_buy_cost_chaos)} C**",
            f"- 卖出：毛收入 {_price(result.gross_sell_revenue_chaos)} C - 费用 "
            f"{_price(result.sell_fee_chaos)} C = **{_price(result.net_sell_revenue_chaos)} C**",
            f"- 净利润：**{_price(result.net_profit_chaos)} C**；利润率："
            f"**{_price(result.margin_percent)}%**",
            f"- 判定：**{verdict}**。",
        ]
    lines += [
        "",
        "> 这是两个方向在线挂单中位数的瞬时可执行性筛查，不是成交记录或收益保证；"
        "没有计入未显式填写的时间成本、议价、价格滑点、重复账号或行情变化。",
    ]
    return "\n".join(lines)


def material_cost_citations(
    result: MaterialCostEstimate, rendered: str
) -> list[dict[str, object]]:
    """把逐项价格来源折叠成按来源去重的实时经济证据。"""
    identity = "|".join(
        f"{item.resolved_name}:{item.quantity}:{item.fetched_at}"
        for item in result.materials
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    source_urls = {
        item.source: item.source_url
        for item in result.materials
        if item.unit_price is not None
    }
    return [{
        "chunk_id": f"economy-cost:{result.league}:{digest}:{source}",
        "entity_id": None,
        "title": f"{result.league} 材料清单成本快照",
        "url": _citation_url(source, source_url),
        "source": source,
        "source_priority": _source_priority(source),
        "can_support_facts": True,
        "text": rendered,
        "channel": "live_economy",
    } for source, source_url in sorted(source_urls.items())]


@REGISTRY.register(
    name="economy_flip_analysis",
    description=(
        "对用户明确指定的 POE1 exchange 标准条目和数量做双边价差分析。分别查询买入"
        "卖家要价与卖出买家承接价，只保留时限内且单条可覆盖全部数量的在线挂单，并要求"
        "两侧都达到最低样本数。调用方必须显式填写买卖固定/比例费用；证据完整时才计算"
        "净利润、利润率与 Flip 候选判断，任一侧样本、深度、时效或费用缺失则返回 unknown。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "quantity": {
                "type": "number", "exclusiveMinimum": 0, "maximum": 1000000000,
            },
            "fees": {
                "type": "object",
                "properties": {
                    "buy_fixed_chaos": {"type": "number", "minimum": 0},
                    "buy_percent": {"type": "number", "minimum": 0, "exclusiveMaximum": 100},
                    "sell_fixed_chaos": {"type": "number", "minimum": 0},
                    "sell_percent": {"type": "number", "minimum": 0, "exclusiveMaximum": 100},
                },
                "required": [
                    "buy_fixed_chaos", "buy_percent", "sell_fixed_chaos", "sell_percent",
                ],
                "additionalProperties": False,
            },
            "category": {
                "type": "string",
                "enum": ["auto", "currency", "scarab", "card", "fragment"],
                "default": "auto",
            },
            "league": {"type": "string", "maxLength": 100},
            "max_listing_age_hours": {
                "type": "integer", "minimum": 1, "maximum": 168, "default": 24,
            },
            "minimum_net_profit_chaos": {"type": "number", "minimum": 0, "default": 0},
            "minimum_margin_percent": {"type": "number", "minimum": 0, "default": 0},
        },
        "required": ["query", "quantity", "fees"],
        "additionalProperties": False,
    },
)
def economy_flip_analysis(
    query: str,
    quantity: float,
    fees: dict[str, object],
    category: str = "auto",
    league: str | None = None,
    max_listing_age_hours: int = 24,
    minimum_net_profit_chaos: float = 0,
    minimum_margin_percent: float = 0,
) -> ToolResult:
    try:
        required_fees = (
            "buy_fixed_chaos", "buy_percent", "sell_fixed_chaos", "sell_percent",
        )
        if not isinstance(fees, dict) or any(name not in fees for name in required_fees):
            raise InvalidEconomyQuery("fees 必须显式包含买卖两侧的固定费用和比例费用")
        fee_input = TradeFeeInput(**{
            name: fees[name] for name in required_fees
        })  # type: ignore[arg-type]
        result = service_from_config().flip_analysis(
            query,
            quantity=quantity,
            fees=fee_input,
            category=category,
            league=league,
            max_listing_age_hours=max_listing_age_hours,
            minimum_net_profit_chaos=minimum_net_profit_chaos,
            minimum_margin_percent=minimum_margin_percent,
        )
    except (EconomyUnavailable, InvalidEconomyQuery, TradeApiError, TypeError) as exc:
        return ToolResult(ok=False, error=str(exc), meta={"source": "GGG Trade API"})

    rendered = render_flip_analysis(result)
    identity = (
        f"{result.league}:{result.resolved_name}:{result.quantity}:"
        f"{result.buy_side.latest_listing_at}:{result.sell_side.latest_listing_at}"
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return ToolResult(
        ok=True,
        data={**result.as_dict(), "answer": rendered},
        citations=[{
            "chunk_id": f"economy-flip:{digest}",
            "entity_id": None,
            "title": f"{result.resolved_name} · {result.league} 双边价差",
            "url": result.source_url,
            "source": result.source,
            "source_priority": 1,
            "can_support_facts": True,
            "text": rendered,
            "channel": "live_economy",
        }],
        meta={
            "channel": "live_economy",
            "source": result.source,
            "calculable": result.calculable,
            "verdict": result.verdict,
        },
    )


@REGISTRY.register(
    name="economy_price_lookup",
    description=(
        "查询 POE1 当前或指定联盟市场价格。主源为 poe.watch；主源无缓存且失败时，"
        "通货、圣甲虫、命运卡与碎片会显式降级到 GGG Trade API 在线挂单中位数。"
        "唯一装备使用精确名称+底材的未腐化搜索，其他标准条目使用 exchange。支持唯一装备、通货、圣甲虫、"
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
            "url": _citation_url(result.source, result.source_url),
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
    name="economy_rare_comparables",
    description=(
        "把 POE1 稀有装备复制文本映射到 GGG 官方 Trade stat，查询同联盟、精确底材、"
        "相同物等和腐化状态、同时具备已映射显式词缀的在线可比挂单。返回中位数、范围、"
        "样本/过滤审计和未映射词缀。可选按明确复制的品质、连接组尺寸再过滤；"
        "它不是精确成交价、制作价值、利润或是否值得分裂/"
        "镜像湖/镜子级别的判断；样本不足时不输出代表价。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "item_text": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20000,
                "description": "游戏内 Ctrl+C 或 Ctrl+Alt+C 得到的完整稀有装备文本",
            },
            "league": {
                "type": "string",
                "maxLength": 100,
                "description": "可选；留空时选择当前普通挑战联盟",
            },
            "match_quality": {
                "type": "boolean",
                "description": "可选；要求复制文本与挂单都有明确品质，并精确匹配百分比",
            },
            "match_sockets": {
                "type": "boolean",
                "description": "可选；按连接组尺寸精确匹配，不比较颜色或位置",
            },
        },
        "required": ["item_text"],
        "additionalProperties": False,
    },
)
def economy_rare_comparables(
    item_text: str, league: str | None = None,
    match_quality: bool = False, match_sockets: bool = False,
) -> ToolResult:
    try:
        result = rare_service_from_config().estimate(
            item_text, league=league,
            match_quality=match_quality, match_sockets=match_sockets,
        )
    except (InvalidRareComparable, TradeApiError) as exc:
        return ToolResult(ok=False, error=str(exc), meta={"source": "GGG Trade API"})
    except ValueError as exc:
        # ItemParseError 是 ValueError 子类；工具层保持统一失败契约。
        return ToolResult(ok=False, error=str(exc), meta={"source": "item_text"})

    rendered = render_rare_comparables(result)
    citations = []
    if result.quote is not None:
        citations.append({
            "chunk_id": (
                f"rare-comparable:{result.league}:{result.quote.item_id}:"
                f"{result.fetched_at}"
            ),
            "entity_id": None,
            "title": f"{result.base_type_en} · {result.league} 稀有装备可比挂单",
            "url": result.source_url,
            "source": result.source,
            "source_priority": 1,
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
            "status": result.status,
            "comparison_quality": result.comparison_quality,
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
        "url": _citation_url(result.source, result.source_url),
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
    citations = material_cost_citations(result, rendered)
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
