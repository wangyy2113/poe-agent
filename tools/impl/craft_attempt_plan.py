"""普通同侧词缀池的独立尝试分布、材料预算与可选市场成本工具。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core import db as dbmod
from craft.attempt_plan import (
    MAX_ATTEMPTS,
    MAX_MATERIALS,
    PricingDecision,
    parse_materials,
    plan_affix_attempts,
)
from craft.pool import (
    MAX_BASE_CHARS,
    MAX_ITEM_LEVEL,
    MAX_TARGET_CHARS,
    MIN_ITEM_LEVEL,
    analyze_affix_pool,
)
from economy.models import MaterialRequest
from economy.service import PUBLIC_CATEGORIES, EconomyUnavailable, InvalidEconomyQuery
from tools.impl.craft_pool import affix_pool_citations
from tools.impl.economy import (
    material_cost_citations,
    render_material_cost,
    service_from_config,
)
from tools.registry import REGISTRY, ToolResult


def _render_unknown(data: dict[str, Any]) -> str:
    pool = data.get("pool") if isinstance(data.get("pool"), dict) else {}
    gaps = pool.get("gaps") if isinstance(pool.get("gaps"), list) else []
    reasons = [
        str(item.get("message") or "").strip()
        for item in gaps
        if isinstance(item, dict) and str(item.get("message") or "").strip()
    ]
    reason = "；".join(reasons) or "没有形成完整、唯一且新鲜的普通同侧词缀池"
    return (
        f"无法建立独立尝试计划：{reason}。未形成单次概率时不能解释为 0%，"
        "也不能输出期望尝试、材料预算或市场成本。"
    )


def render_attempt_plan(data: dict[str, Any]) -> str:
    """只从结构化字段渲染，避免模型改写分布口径或补制作步骤。"""
    if data.get("status") != "exact":
        return _render_unknown(data)
    pool = data["pool"]
    distribution = data["distribution"]
    base = pool.get("base") or {}
    side = "前缀" if pool.get("generation_side") == "prefix" else "后缀"
    numerator = distribution["single_attempt_probability_numerator"]
    denominator = distribution["single_attempt_probability_denominator"]
    lines = [
        "## 独立尝试计划",
        "",
        (
            f"在底材“{base.get('name_zh') or pool.get('base_type')}”、物品等级 "
            f"{pool.get('item_level')}、normal {side}池、未应用已有 family 排除，"
            f"且每次只进行一次同侧加权选择的条件下，目标组"
            f"“{pool.get('target_group')}”的单次命中概率为 "
            f"{numerator}/{denominator}（{distribution['single_attempt_probability']}）。"
        ),
        (
            "无限重试到首次成功的期望尝试次数为 "
            f"{distribution['expected_attempts_until_success']}；最多尝试 "
            f"{distribution['attempt_cap']} 次时，至少成功一次的概率为 "
            f"{distribution['success_probability_within_cap']}，全部失败概率为 "
            f"{distribution['exhaustion_probability']}，计入提前成功停止后的期望实际尝试数为 "
            f"{distribution['expected_attempts_consumed_with_cap']}。"
        ),
        "",
        "| 目标置信度 | 最少尝试数 | 实际累计成功率 | 是否在上限内 |",
        "|---:|---:|---:|:---:|",
    ]
    for item in distribution["milestones"]:
        lines.append(
            f"| {item['target_probability']} | {item['attempts']} | "
            f"{item['achieved_probability']} | {'是' if item['within_attempt_cap'] else '否'} |"
        )

    lines.extend([
        "",
        "| 每次材料 | 每次用量 | 无限重试期望用量 | 上限内期望用量 | 上限预算 | P50 / P90 / P95 预算 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for item in data["material_budget"]:
        milestones = item["milestones"]
        lines.append(
            f"| {item['query']} | {item['quantity_per_attempt']} | "
            f"{item['expected_until_success']} | {item['expected_with_attempt_cap']} | "
            f"{item['maximum_at_attempt_cap']} | {milestones['p50']} / "
            f"{milestones['p90']} / {milestones['p95']} |"
        )

    pricing = data["pricing"]
    cost = data.get("cost_budget")
    lines.extend(["", "## 市场成本"])
    if cost is not None:
        milestone_costs = cost["milestones_chaos"]
        lines.extend([
            (
                f"价格口径：{pricing['league']}，{pricing['source']}，采样窗口 "
                f"{pricing['snapshot_started_at']} 至 {pricing['snapshot_finished_at']}。"
            ),
            (
                f"每次代表成本 {cost['per_attempt_chaos']} chaos；无限重试期望成本 "
                f"{cost['expected_until_success_chaos']} chaos；在尝试上限内按提前成功停止的"
                f"期望成本 {cost['expected_with_attempt_cap_chaos']} chaos；最坏上限预算 "
                f"{cost['maximum_at_attempt_cap_chaos']} chaos。"
            ),
            (
                "P50 / P90 / P95 材料预算对应成本为 "
                f"{milestone_costs['p50']} / {milestone_costs['p90']} / "
                f"{milestone_costs['p95']} chaos。"
            ),
        ])
        if pricing.get("warning"):
            lines.append(f"价格警告：{pricing['warning']}。")
    elif pricing["status"] == "not_requested":
        lines.append("本次未请求市场计价；只输出尝试次数与材料数量。")
    else:
        lines.append(f"市场总成本不可计算：{pricing['reason']}。没有输出部分总价。")

    lines.extend([
        "",
        "## 模型边界",
        "",
        (
            "这是几何分布的独立单次选择模型：每次必须完整重置、独立同分布、没有保底，"
            "失败状态和中间产物都不复用。它不是任何混沌石、崇高石、精华、化石、Harvest "
            "或其它真实工艺的一次使用成功率，也不是完整多步制作路线。"
        ),
        "材料是调用方显式清单；系统没有从目标反推步骤。市场价只是快照，不保证成交。",
    ])
    return "\n".join(lines)


@REGISTRY.register(
    name="craft_attempt_plan",
    description=(
        "只在完整、新鲜的胸甲 normal 同侧词缀池上，计算每次完整重置且恰好进行一次"
        "加权选择的独立同分布尝试计划。返回单次条件命中概率、几何分布期望、P50/P90/P95 "
        "最少尝试数、尝试上限内成功/耗尽概率，以及用户显式材料清单的期望和分位预算。"
        "可选同联盟实时计价；任一材料缺价、来源或时间口径不一致时不输出部分总成本。"
        "不代表任何具体通货/工艺的一次成功率，不推断步骤，不比较完整路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "base_type": {
                "type": "string", "minLength": 1, "maxLength": MAX_BASE_CHARS,
                "description": "pob_base_item 中逐字中文或英文胸甲底材名。",
            },
            "item_level": {
                "type": "integer", "minimum": MIN_ITEM_LEVEL, "maximum": MAX_ITEM_LEVEL,
            },
            "generation_side": {"type": "string", "enum": ["prefix", "suffix"]},
            "target_query": {
                "type": "string", "minLength": 1, "maxLength": MAX_TARGET_CHARS,
                "description": "唯一命中一个 correct_group 的逐字目标。",
            },
            "materials": {
                "type": "array", "minItems": 1, "maxItems": MAX_MATERIALS,
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 200},
                        "quantity_per_attempt": {
                            "type": "number", "exclusiveMinimum": 0, "maximum": 1000000000,
                        },
                        "category": {
                            "type": "string", "enum": list(PUBLIC_CATEGORIES), "default": "auto",
                        },
                    },
                    "required": ["query", "quantity_per_attempt"],
                    "additionalProperties": False,
                },
            },
            "max_attempts": {
                "type": "integer", "minimum": 1, "maximum": MAX_ATTEMPTS,
                "description": "失败停止上限；同时用于成功率、耗尽率和最坏材料预算。",
            },
            "include_market_prices": {"type": "boolean", "default": False},
            "league": {"type": "string", "maxLength": 100},
        },
        "required": [
            "base_type", "item_level", "generation_side", "target_query", "materials",
            "max_attempts",
        ],
        "additionalProperties": False,
    },
)
def craft_attempt_plan(
    base_type: str,
    item_level: int,
    generation_side: str,
    target_query: str,
    materials: list[dict[str, object]],
    max_attempts: int,
    include_market_prices: bool = False,
    league: str | None = None,
) -> ToolResult:
    try:
        parsed_materials = parse_materials(materials)
        conn = dbmod.get_conn()
        pool = analyze_affix_pool(
            conn,
            base_type=base_type,
            item_level=item_level,
            generation_side=generation_side,  # type: ignore[arg-type]
            target_query=target_query,
        )
        market_cost = None
        pricing_error = ""
        if pool.status == "exact" and include_market_prices:
            try:
                market_cost = service_from_config().material_cost([
                    MaterialRequest(
                        query=item.query,
                        quantity=float(item.quantity_per_attempt),
                        category=item.category,
                    )
                    for item in parsed_materials
                ], league=league)
            except (EconomyUnavailable, InvalidEconomyQuery, TypeError) as exc:
                pricing_error = str(exc)
        report = plan_affix_attempts(
            conn,
            base_type=base_type,
            item_level=item_level,
            generation_side=generation_side,  # type: ignore[arg-type]
            target_query=target_query,
            materials=parsed_materials,
            max_attempts=max_attempts,
            market_cost=market_cost,
            pool_report=pool,
        )
        if include_market_prices and pool.status != "exact":
            report.pricing = PricingDecision(
                "incomplete", "普通同侧词缀池未形成，已跳过无意义的市场请求"
            )
        elif pricing_error:
            report.pricing = PricingDecision(
                "incomplete", f"实时经济服务不可用：{pricing_error}"
            )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))

    data = report.to_dict()
    answer = render_attempt_plan(data)
    citations = affix_pool_citations(pool)
    if report.status == "exact":
        # 分布和预算是本工具从已验证池及显式材料算出的新数值。必须把全部输出
        # 作为确定性计算证据登记，不能只给底材/池三张引用却让派生数字无处回填。
        digest = hashlib.sha256(
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        citations.append({
            "chunk_id": f"craft_attempt_plan:{digest}",
            "entity_id": None,
            "title": "独立尝试分布与材料预算的确定性计算",
            "url": f"https://poedb.tw/cn/{pool.base.modifier_page}" if pool.base else "",
            "source": "POE1-Agent",
            "source_priority": 1,
            "can_support_facts": True,
            "text": answer,
            "channel": "derived",
            "claim_scope": "calculation",
            "claim_data": {
                "kind": "craft_attempt_plan",
                "model": "geometric_iid_single_weighted_selection_v1",
                "pool_digest": pool.pool_digest,
                "max_attempts": max_attempts,
            },
        })
    if market_cost is not None:
        citations.extend(material_cost_citations(market_cost, render_material_cost(market_cost)))
    return ToolResult(
        ok=True,
        data={**data, "answer": answer},
        citations=citations,
        meta={
            "channel": "derived",
            "model": "geometric_iid_single_weighted_selection_v1",
            "pricing_status": report.pricing.status,
        },
    )
