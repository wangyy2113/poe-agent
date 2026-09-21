"""Harvest 护甲/武器品质附魔状态机工具。"""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.harvest_enchant_operations import (
    ENCHANT_SPECS,
    HarvestEnchantItemState,
    HarvestEnchantResult,
    evaluate_harvest_enchant,
)
from parsers.item_text import ItemParseError, parse_item
from tools.registry import REGISTRY, ToolResult

MAX_ITEM_TEXT_CHARS = 20_000

_BODY_ARMOUR_CLASSES = {"body armour", "body armours", "胸甲", "护甲", "護甲"}
_MELEE_WEAPON_CLASSES = {
    "claw", "claws", "dagger", "daggers", "rune dagger", "rune daggers",
    "one handed axe", "one handed axes", "two handed axe", "two handed axes",
    "one handed mace", "one handed maces", "two handed mace", "two handed maces",
    "one handed sword", "one handed swords", "two handed sword", "two handed swords",
    "sceptre", "sceptres", "staff", "staves", "warstaff", "warstaves",
    "爪", "匕首", "符文匕首", "单手斧", "双手斧", "单手锤", "双手锤",
    "单手剑", "双手剑", "短杖", "长杖", "战杖",
}
_RANGED_WEAPON_CLASSES = {
    "bow", "bows", "wand", "wands", "fishing rod", "fishing rods",
    "弓", "法杖", "鱼竿",
}


def _item_kind(item_class: str) -> str | None:
    normalized = (item_class or "").strip().lower()
    if normalized in _BODY_ARMOUR_CLASSES:
        return "body_armour"
    if normalized in _MELEE_WEAPON_CLASSES:
        return "melee_weapon"
    if normalized in _RANGED_WEAPON_CLASSES:
        return "weapon"
    return None


def _state_from_item_text(text: str) -> HarvestEnchantItemState:
    parsed = parse_item(text)
    reasons = []
    kind = _item_kind(parsed.item_class)
    if kind is None:
        reasons.append(f"物品类别“{parsed.item_class or '未识别'}”无法归入护甲/武器")
    if parsed.flags.unidentified:
        reasons.append("物品未鉴定，显示状态不完整")
    if parsed.unclassified:
        reasons.append(f"{len(parsed.unclassified)} 行内容未归类")
    return HarvestEnchantItemState(
        item_kind=kind,
        quality=parsed.quality,
        corrupted=parsed.flags.corrupted,
        mirrored=parsed.flags.mirrored,
        fractured=parsed.flags.fractured,
        incomplete_reason="；".join(reasons),
    )


def _format_value(value: float | None, unit: str) -> str:
    if value is None:
        return "未知"
    rendered = str(int(value)) if float(value).is_integer() else str(value)
    return rendered + unit


def render_harvest_enchant(result: HarvestEnchantResult) -> str:
    labels = {
        "compatible": "已知约束兼容",
        "incompatible": "已知约束不兼容",
        "unknown": "信息不足",
    }
    after = result.after
    lines = [
        "### Harvest 品质附魔状态转移",
        "",
        f"**结论：{labels[result.verdict]}**",
        f"- POEDB 效果原文：{result.effect_text}",
        "- 原始费用：" + (
            " + ".join(f"{cost.amount}x {cost.currency_name_zh}" for cost in result.costs)
            if result.costs else "未知（费用子项缺失）"
        ),
        f"- 输入状态：类别 {result.before.item_kind or '未知'}；品质 "
        + (f"{result.before.quality}%" if result.before.quality is not None else "未知"),
        "",
        "| 检查 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    status_zh = {"pass": "通过", "fail": "不通过", "unknown": "未知"}
    for check in result.checks:
        lines.append(f"| {check.key} | {status_zh[check.status]} | {check.summary} |")
    lines += [
        "",
        "条件状态转移（仅在操作实际成功执行时）：",
        f"- 品质不再增加{after['replaced_quality_effect']}",
        f"- 每 {after['quality_step_percent']}% 品质给予 "
        f"{_format_value(after['value_per_step'], after['value_unit'])} {after['granted_stat']}",
        f"- 按当前品质可确定的收益：{_format_value(after['derived_value'], after['value_unit'])}",
    ]
    if result.unknown_fields:
        lines += ["", "仍未知：", *[f"- {item}" for item in result.unknown_fields]]
    lines += [
        "",
        f"> 来源：[POEDB]({result.source_url})；抓取时间：{result.fetched_at}。",
        f"> 边界：{result.boundary}。",
        "> 非整步品质的取整、已有附魔、腐化、镜像、裂隙与 Metamod 交互不自动补全。",
    ]
    return "\n".join(lines)


def _citations(result: HarvestEnchantResult, rendered: str) -> list[dict[str, Any]]:
    return [{
        "chunk_id": f"harvest_enchant_operation:{result.craft_id}",
        "entity_id": None,
        "title": f"Harvest 品质附魔：{result.effect_text}",
        "url": result.source_url,
        "source": result.source,
        "source_priority": result.source_priority,
        "can_support_facts": True,
        "text": rendered,
        "channel": "structured",
    }]


@REGISTRY.register(
    name="harvest_quality_enchant_state",
    description=(
        "按 POEDB 原文判断 14 条 Harvest 品质附魔：8 条护甲附魔与 6 条武器附魔。"
        "检查护甲/武器/近战武器类别，并在品质可被原文步长整除时计算替代收益；"
        "携带原始命能费用，返回 compatible / incompatible / unknown。"
        "不补非整步取整、已有附魔交互、市场成本、步骤或路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "完整效果原文或类别+收益别名，如护甲火焰抗性、武器攻击速度、近战武器范围",
                "minLength": 1,
                "maxLength": 180,
            },
            "item_text": {"type": "string", "maxLength": MAX_ITEM_TEXT_CHARS},
            "item_kind": {
                "type": "string",
                "enum": ["body_armour", "weapon", "melee_weapon", "other"],
            },
            "quality": {"type": "integer", "minimum": 0, "maximum": 100},
            "corrupted": {"type": "boolean", "default": False},
            "mirrored": {"type": "boolean", "default": False},
            "fractured": {"type": "boolean", "default": False},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
)
def harvest_quality_enchant_state(
    operation: str,
    item_text: str | None = None,
    item_kind: str | None = None,
    quality: int | None = None,
    corrupted: bool = False,
    mirrored: bool = False,
    fractured: bool = False,
) -> ToolResult:
    if not (operation or "").strip():
        return ToolResult(ok=False, error="operation 不能为空")
    if item_text and (item_kind is not None or quality is not None):
        return ToolResult(ok=False, error="item_text 与手工 item_kind/quality 不能混用")
    if item_text and len(item_text) > MAX_ITEM_TEXT_CHARS:
        return ToolResult(ok=False, error=f"item_text 最多 {MAX_ITEM_TEXT_CHARS} 字符")
    if quality is not None and not 0 <= quality <= 100:
        return ToolResult(ok=False, error="quality 必须在 0 到 100 之间")

    try:
        state = _state_from_item_text(item_text) if item_text else HarvestEnchantItemState(
            item_kind=item_kind,
            quality=quality,
            corrupted=bool(corrupted),
            mirrored=bool(mirrored),
            fractured=bool(fractured),
        )
    except ItemParseError as exc:
        return ToolResult(ok=False, error=f"装备文本解析失败：{exc.reason}")

    result = evaluate_harvest_enchant(dbmod.get_conn(), operation, state)
    if result is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "operation": operation,
                "supported_operation_count": len(ENCHANT_SPECS),
                "supported_examples": ["护甲火焰抗性", "武器攻击速度", "近战武器范围"],
                "boundary": "只支持 14 条已审计的 Harvest 护甲/武器品质附魔",
            },
            citations=[],
            meta={"channel": "structured", "table": "harvest_craft"},
        )
    rendered = render_harvest_enchant(result)
    return ToolResult(
        ok=True,
        data={"found": True, **result.as_dict(), "answer": rendered},
        citations=_citations(result, rendered),
        meta={"channel": "structured", "table": "harvest_craft", "verdict": result.verdict},
    )
