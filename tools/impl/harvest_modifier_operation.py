"""Harvest 定向词缀转换与新增/移除状态机工具。"""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.harvest_modifier_operations import (
    CONVERSION_EFFECTS,
    REMOVE_ADD_TAGS,
    HarvestModifierItemState,
    HarvestModifierResult,
    evaluate_harvest_modifier_operation,
)
from parsers.item_text import ItemParseError, parse_item
from tools.registry import REGISTRY, ToolResult

MAX_ITEM_TEXT_CHARS = 20_000


def _state_from_item_text(text: str, source_modifier_present: bool | None) -> HarvestModifierItemState:
    parsed = parse_item(text)
    reasons = []
    if parsed.flags.unidentified:
        reasons.append("物品未鉴定，显示状态不完整")
    if parsed.flags.veiled:
        reasons.append("含未解锁隐匿词缀")
    if parsed.unclassified:
        reasons.append(f"{len(parsed.unclassified)} 行内容未归类")
    return HarvestModifierItemState(
        influenced=bool(parsed.flags.influences),
        source_modifier_present=source_modifier_present,
        corrupted=parsed.flags.corrupted,
        mirrored=parsed.flags.mirrored,
        fractured=parsed.flags.fractured,
        incomplete_reason="；".join(reasons),
    )


def render_harvest_modifier_operation(result: HarvestModifierResult) -> str:
    labels = {
        "compatible": "已知约束兼容",
        "incompatible": "已知约束不兼容",
        "unknown": "信息不足",
    }
    state = result.before
    lines = [
        "### Harvest 定向词缀状态转移",
        "",
        f"**结论：{labels[result.verdict]}**",
        f"- POEDB 效果原文：{result.effect_text}",
        "- 原始费用：" + (
            " + ".join(f"{cost.amount}x {cost.currency_name_zh}" for cost in result.costs)
            if result.costs else "未知（费用子项缺失）"
        ),
        "- 输入状态：影响力 "
        + ("有" if state.influenced is True else "无" if state.influenced is False else "未知")
        + "；来源词缀 "
        + ("存在" if state.source_modifier_present is True else "不存在" if state.source_modifier_present is False else "未知"),
        "",
        "| 检查 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    status_zh = {"pass": "通过", "fail": "不通过", "unknown": "未知"}
    for check in result.checks:
        lines.append(f"| {check.key} | {status_zh[check.status]} | {check.summary} |")

    lines += ["", "条件状态转移（仅在操作实际成功执行时）："]
    if result.mode == "convert_elemental_modifier":
        lines += [
            f"- 移除/替换：一个提供{result.source_modifier}的词缀",
            f"- 新词缀：提供{result.target_modifier}，仅保证原文所说的近似位阶",
            "- 词缀数量：一条替换为一条",
        ]
    else:
        lines += [
            f"- 新词缀：一个{result.guaranteed_tag}标签词缀（具体词缀未知）",
            "- 移除：另一个随机词缀（具体是哪条未知）",
            "- 词缀数量：新增一条并移除一条",
        ]
    if result.unknown_fields:
        lines += ["", "仍未知：", *[f"- {item}" for item in result.unknown_fields]]
    lines += [
        "",
        f"> 来源：[POEDB]({result.source_url})；抓取时间：{result.fetched_at}。",
        f"> 边界：{result.boundary}。",
        "> 腐化、镜像、裂隙、Metamod 与原文未描述的影响力交互不自动补全。",
    ]
    return "\n".join(lines)


def _citations(result: HarvestModifierResult, rendered: str) -> list[dict[str, Any]]:
    return [{
        "chunk_id": f"harvest_modifier_operation:{result.craft_id}",
        "entity_id": None,
        "title": f"Harvest 定向词缀：{result.effect_text}",
        "url": result.source_url,
        "source": result.source,
        "source_priority": result.source_priority,
        "can_support_facts": True,
        "text": rendered,
        "channel": "structured",
    }]


@REGISTRY.register(
    name="harvest_modifier_state",
    description=(
        "按 POEDB 原文判断 23 条 Harvest 定向词缀操作：12 条冰霜/火焰/闪电伤害或抗性"
        "近似位阶转换，以及 11 条新增指定标签并从不受影响物品移除另一个随机词缀。"
        "携带原始命能/结晶费用，返回 compatible / incompatible / unknown；不生成具体词缀、"
        "精确位阶、数值、概率、市场成本、步骤或路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": (
                    "完整效果原文，元素转换别名如火焰抗性转冰霜抗性，或新增标签并移除随机词缀"
                ),
                "minLength": 1,
                "maxLength": 180,
            },
            "item_text": {"type": "string", "maxLength": MAX_ITEM_TEXT_CHARS},
            "source_modifier_present": {"type": "boolean"},
            "influenced": {"type": "boolean"},
            "corrupted": {"type": "boolean", "default": False},
            "mirrored": {"type": "boolean", "default": False},
            "fractured": {"type": "boolean", "default": False},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
)
def harvest_modifier_state(
    operation: str,
    item_text: str | None = None,
    source_modifier_present: bool | None = None,
    influenced: bool | None = None,
    corrupted: bool = False,
    mirrored: bool = False,
    fractured: bool = False,
) -> ToolResult:
    if not (operation or "").strip():
        return ToolResult(ok=False, error="operation 不能为空")
    if item_text and influenced is not None:
        return ToolResult(ok=False, error="item_text 与手工 influenced 不能混用")
    if item_text and len(item_text) > MAX_ITEM_TEXT_CHARS:
        return ToolResult(ok=False, error=f"item_text 最多 {MAX_ITEM_TEXT_CHARS} 字符")

    try:
        state = (
            _state_from_item_text(item_text, source_modifier_present)
            if item_text else HarvestModifierItemState(
                influenced=influenced,
                source_modifier_present=source_modifier_present,
                corrupted=bool(corrupted),
                mirrored=bool(mirrored),
                fractured=bool(fractured),
            )
        )
    except ItemParseError as exc:
        return ToolResult(ok=False, error=f"装备文本解析失败：{exc.reason}")

    result = evaluate_harvest_modifier_operation(dbmod.get_conn(), operation, state)
    if result is None:
        examples = [
            "火焰抗性转冰霜抗性",
            *[f"新增{tag}并移除随机词缀" for tag in REMOVE_ADD_TAGS],
        ]
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "operation": operation,
                "supported_operation_count": len(CONVERSION_EFFECTS) + len(REMOVE_ADD_TAGS),
                "supported_examples": examples,
                "boundary": "只支持 23 条已审计的 Harvest 定向词缀操作，不按相似描述猜语义",
            },
            citations=[],
            meta={"channel": "structured", "table": "harvest_craft"},
        )
    rendered = render_harvest_modifier_operation(result)
    return ToolResult(
        ok=True,
        data={"found": True, **result.as_dict(), "answer": rendered},
        citations=_citations(result, rendered),
        meta={"channel": "structured", "table": "harvest_craft", "verdict": result.verdict},
    )
