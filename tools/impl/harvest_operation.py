"""Harvest 重铸状态机工具；结果完全由代码装配。"""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.harvest_operations import (
    GUARANTEED_TAGS,
    HarvestItemState,
    HarvestOperationResult,
    evaluate_harvest_reforge,
)
from parsers.item_text import ItemParseError, parse_item
from tools.registry import REGISTRY, ToolResult

MAX_ITEM_TEXT_CHARS = 20_000
SELECTOR_ALIASES = (
    *GUARANTEED_TAGS,
    "影响词缀", "大概率相同", "大概率不同", "随机影响类型",
)


def _state_from_item_text(text: str) -> HarvestItemState:
    parsed = parse_item(text)
    reasons = []
    if parsed.flags.unidentified:
        reasons.append("物品未鉴定，显示状态不完整")
    if parsed.flags.veiled:
        reasons.append("含未解锁隐匿词缀")
    if parsed.unclassified:
        reasons.append(f"{len(parsed.unclassified)} 行内容未归类")
    return HarvestItemState(
        rarity=parsed.rarity.value,
        influenced=bool(parsed.flags.influences),
        corrupted=parsed.flags.corrupted,
        mirrored=parsed.flags.mirrored,
        fractured=parsed.flags.fractured,
        incomplete_reason="；".join(reasons),
    )


def render_harvest_reforge(result: HarvestOperationResult) -> str:
    state = result.before
    labels = {
        "compatible": "已知约束兼容",
        "incompatible": "已知约束不兼容",
        "unknown": "信息不足",
    }
    lines = [
        "### Harvest 重铸状态转移",
        "",
        f"**结论：{labels[result.verdict]}**",
        f"- POEDB 效果原文：{result.effect_text}",
        "- 原始费用：" + (
            " + ".join(f"{cost.amount}x {cost.currency_name_zh}" for cost in result.costs)
            if result.costs else "未知（费用子项缺失）"
        ),
        f"- 输入状态：稀有度 {state.rarity or '未提供'}；影响力 "
        + ("有" if state.influenced is True else "无" if state.influenced is False else "未知"),
        "",
        "| 检查 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    status_zh = {"pass": "通过", "fail": "不通过", "unknown": "未知"}
    for check in result.checks:
        lines.append(f"| {check.key} | {status_zh[check.status]} | {check.summary} |")

    after = result.after
    mode_labels = {
        "reforge_guaranteed_tag": "重铸稀有物品，并保底一个指定标签词缀",
        "reforge_guaranteed_influence": "重铸受影响稀有物品，并保底一个影响词缀",
        "reforge_same_types_more_likely": "重铸稀有物品，原文称大概率得到相同词缀类型",
        "reforge_same_types_less_likely": "重铸稀有物品，原文称大概率不会得到相同词缀类型",
        "randomize_influence_and_reforge": "随机化影响类型，并用新随机词缀重铸物品",
    }
    lines += [
        "",
        "条件状态转移（仅在操作实际成功执行时）：",
        f"- 操作模式：{mode_labels[result.mode]}",
        f"- 稀有度：{after.get('rarity') or '未知'}",
        "- 前后缀计数：未知（效果原文未给生成数量）",
    ]
    if result.guaranteed_tag:
        lines.append(f"- 保底标签：{result.guaranteed_tag}（具体词缀与数值未知）")
    if after.get("selection_bias"):
        lines.append("- 词缀类型倾向：保留页面的“大概率”定性原文，不换算成概率")
    lines.append(
        "- 影响状态：" + (
            "影响类型会被随机化" if after["influence_state"] == "randomized"
            else "来源原文未说明是否变化"
        )
    )
    if result.unknown_fields:
        lines += ["", "仍未知：", *[f"- {item}" for item in result.unknown_fields]]
    lines += [
        "",
        f"> 来源：[POEDB]({result.source_url})；抓取时间：{result.fetched_at}。",
        f"> 边界：{result.boundary}。",
        "> 腐化、镜像、裂隙、Metamod 与未被效果原文明确描述的影响力交互不自动补全。",
    ]
    return "\n".join(lines)


def _citations(result: HarvestOperationResult, rendered: str) -> list[dict[str, Any]]:
    return [{
        "chunk_id": f"harvest_operation:{result.craft_id}",
        "entity_id": None,
        "title": f"Harvest 重铸：{result.effect_text}",
        "url": result.source_url,
        "source": result.source,
        "source_priority": result.source_priority,
        "can_support_facts": True,
        "text": rendered,
        "channel": "structured",
    }]


@REGISTRY.register(
    name="harvest_reforge_state",
    description=(
        "按 POEDB 原文判断 20 条 Harvest 装备重铸的保守状态转移：16 类保底标签、"
        "影响词缀保底、相同词缀类型更可能/更不可能、影响类型随机化。携带原始命能费用，"
        "可接收装备复制文本或手工稀有度/影响状态。返回 compatible / incompatible / unknown；"
        "不生成具体结果、概率、市场成本、步骤或路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "完整效果原文，或标签/别名：" + "、".join(SELECTOR_ALIASES),
                "minLength": 1,
                "maxLength": 160,
            },
            "item_text": {"type": "string", "maxLength": MAX_ITEM_TEXT_CHARS},
            "rarity": {"type": "string", "enum": ["普通", "魔法", "稀有", "传奇"]},
            "influenced": {"type": "boolean"},
            "corrupted": {"type": "boolean", "default": False},
            "mirrored": {"type": "boolean", "default": False},
            "fractured": {"type": "boolean", "default": False},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
)
def harvest_reforge_state(
    operation: str,
    item_text: str | None = None,
    rarity: str | None = None,
    influenced: bool | None = None,
    corrupted: bool = False,
    mirrored: bool = False,
    fractured: bool = False,
) -> ToolResult:
    if not (operation or "").strip():
        return ToolResult(ok=False, error="operation 不能为空")
    if item_text and (rarity is not None or influenced is not None):
        return ToolResult(ok=False, error="item_text 与手工 rarity/influenced 不能混用")
    if item_text and len(item_text) > MAX_ITEM_TEXT_CHARS:
        return ToolResult(ok=False, error=f"item_text 最多 {MAX_ITEM_TEXT_CHARS} 字符")

    try:
        state = _state_from_item_text(item_text) if item_text else HarvestItemState(
            rarity=rarity,
            influenced=influenced,
            corrupted=bool(corrupted),
            mirrored=bool(mirrored),
            fractured=bool(fractured),
        )
    except ItemParseError as exc:
        return ToolResult(ok=False, error=f"装备文本解析失败：{exc.reason}")

    result = evaluate_harvest_reforge(dbmod.get_conn(), operation, state)
    if result is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "operation": operation,
                "supported_operations": list(SELECTOR_ALIASES),
                "boundary": "只支持 20 条已审计的 Harvest 装备重铸，不把转换、牺牲或附魔猜成重铸",
            },
            citations=[],
            meta={"channel": "structured", "table": "harvest_craft"},
        )
    rendered = render_harvest_reforge(result)
    return ToolResult(
        ok=True,
        data={"found": True, **result.as_dict(), "answer": rendered},
        citations=_citations(result, rendered),
        meta={"channel": "structured", "table": "harvest_craft", "verdict": result.verdict},
    )
