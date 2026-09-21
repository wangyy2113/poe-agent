"""Harvest 材料转换与牺牲状态机工具。"""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.harvest_exchange_operations import (
    EXCHANGE_SPECS,
    SELECTOR_ALIASES,
    HarvestExchangeResult,
    HarvestExchangeState,
    evaluate_harvest_exchange,
)
from tools.registry import REGISTRY, ToolResult


def _format_costs(result: HarvestExchangeResult) -> str:
    if not result.costs:
        return "未知（费用子项缺失）"
    raw = " + ".join(f"{cost.amount}x {cost.currency_name_zh}" for cost in result.costs)
    if result.after["cost_scaling"] == "proportional_to_stack_size":
        return raw + "（页面原始费用；原文说明随堆叠数量成比例）"
    return raw


def render_harvest_exchange(result: HarvestExchangeResult) -> str:
    labels = {
        "compatible": "已知约束兼容",
        "incompatible": "已知约束不兼容",
        "unknown": "信息不足",
    }
    lines = [
        "### Harvest 转换/牺牲状态转移",
        "",
        f"**结论：{labels[result.verdict]}**",
        f"- POEDB 效果原文：{result.effect_text}",
        f"- 页面费用：{_format_costs(result)}",
        f"- 输入：{result.input_label}",
        f"- 条件输出：{result.output_relation}",
        "",
        "| 检查 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    status_zh = {"pass": "通过", "fail": "不通过", "unknown": "未知"}
    for check in result.checks:
        lines.append(f"| {check.key} | {status_zh[check.status]} | {check.summary} |")

    after = result.after
    if after.get("preserved_map_tier") is not None:
        lines.append(f"- 输出地图位阶：{after['preserved_map_tier']}（与输入相同）")
    if after.get("preserved_rarity"):
        lines.append(f"- 输出地图稀有度：{after['preserved_rarity']}（与输入相同）")
    if after.get("stored_experience") is not None:
        lines.append(f"- 费斯特的透镜储存经验：{after['stored_experience']}")
    if after.get("output_quantity_min") is not None:
        lines.append(
            f"- 输出数量范围：{after['output_quantity_min']}–{after['output_quantity_max']} 张相同命运卡"
        )
    if result.unknown_fields:
        lines += ["", "仍未知：", *[f"- {item}" for item in result.unknown_fields]]
    lines += [
        "",
        f"> 来源：[POEDB]({result.source_url})；抓取时间：{result.fetched_at}。",
        f"> 边界：{result.boundary}。",
    ]
    return "\n".join(lines)


def _citations(result: HarvestExchangeResult, rendered: str) -> list[dict[str, Any]]:
    return [{
        "chunk_id": f"harvest_exchange_operation:{result.craft_id}",
        "entity_id": None,
        "title": f"Harvest 转换/牺牲：{result.effect_text}",
        "url": result.source_url,
        "source": result.source,
        "source_priority": result.source_priority,
        "can_support_facts": True,
        "text": rendered,
        "channel": "structured",
    }]


@REGISTRY.register(
    name="harvest_exchange_state",
    description=(
        "按 POEDB 原文判断 Harvest 剩余 17 条材料转换与牺牲：14 条辅助宝石、材料、碎片、"
        "精华或命运卡随机转换，以及腐化稀有地图、腐化技能石经验、半叠命运卡 3 条牺牲。"
        "保留随机性、同色/同位阶/同类别约束、堆叠比例费用与 0 到翻倍边界；"
        "不生成具体随机结果、概率、市场成本、步骤或路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "完整效果原文或精确别名，如化石转换、腐化地图牺牲、命运卡赌博",
                "minLength": 1,
                "maxLength": 260,
            },
            "quantity": {"type": "integer", "minimum": 1},
            "max_stack_size": {"type": "integer", "minimum": 1},
            "rarity": {"type": "string", "enum": ["普通", "魔法", "稀有", "传奇"]},
            "corrupted": {"type": "boolean"},
            "map_tier": {"type": "integer", "minimum": 1, "maximum": 100},
            "gem_total_experience": {"type": "integer", "minimum": 0},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
)
def harvest_exchange_state(
    operation: str,
    quantity: int | None = None,
    max_stack_size: int | None = None,
    rarity: str | None = None,
    corrupted: bool | None = None,
    map_tier: int | None = None,
    gem_total_experience: int | None = None,
) -> ToolResult:
    if not (operation or "").strip():
        return ToolResult(ok=False, error="operation 不能为空")
    if quantity is not None and quantity < 1:
        return ToolResult(ok=False, error="quantity 必须至少为 1")
    if max_stack_size is not None and max_stack_size < 1:
        return ToolResult(ok=False, error="max_stack_size 必须至少为 1")
    if map_tier is not None and not 1 <= map_tier <= 100:
        return ToolResult(ok=False, error="map_tier 必须在 1 到 100 之间")
    if gem_total_experience is not None and gem_total_experience < 0:
        return ToolResult(ok=False, error="gem_total_experience 不能为负数")

    state = HarvestExchangeState(
        quantity=quantity,
        max_stack_size=max_stack_size,
        rarity=rarity,
        corrupted=corrupted,
        map_tier=map_tier,
        gem_total_experience=gem_total_experience,
    )
    result = evaluate_harvest_exchange(dbmod.get_conn(), operation, state)
    if result is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "operation": operation,
                "supported_operation_count": len(EXCHANGE_SPECS),
                "supported_operations": list(SELECTOR_ALIASES),
                "boundary": "只支持 17 条已审计的 Harvest 材料转换与牺牲",
            },
            citations=[],
            meta={"channel": "structured", "table": "harvest_craft"},
        )
    rendered = render_harvest_exchange(result)
    return ToolResult(
        ok=True,
        data={"found": True, **result.as_dict(), "answer": rendered},
        citations=_citations(result, rendered),
        meta={"channel": "structured", "table": "harvest_craft", "verdict": result.verdict},
    )
