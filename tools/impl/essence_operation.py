"""精华操作状态机工具；结果完全由代码装配，不经模型改写。"""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.essence_operations import (
    EssenceItemState,
    EssenceOperationResult,
    evaluate_essence_operation,
)
from parsers.item_text import ItemParseError, parse_item
from tools.registry import REGISTRY, ToolResult

MAX_ITEM_TEXT_CHARS = 20_000


def _state_from_item_text(text: str) -> EssenceItemState:
    parsed = parse_item(text)
    reasons = []
    if parsed.flags.unidentified:
        reasons.append("物品未鉴定，显示状态不完整")
    if parsed.flags.veiled:
        reasons.append("含未解锁隐匿词缀")
    if parsed.unclassified:
        reasons.append(f"{len(parsed.unclassified)} 行内容未归类")
    return EssenceItemState(
        rarity=parsed.rarity.value,
        item_level=parsed.item_level,
        item_class=parsed.item_class,
        corrupted=parsed.flags.corrupted,
        mirrored=parsed.flags.mirrored,
        fractured=parsed.flags.fractured,
        incomplete_reason="；".join(reasons),
    )


def render_essence_operation(result: EssenceOperationResult) -> str:
    state = result.before
    labels = {
        "compatible": "已知约束兼容",
        "incompatible": "已知约束不兼容",
        "unknown": "信息不足",
    }
    lines = [
        f"### {result.essence}（{result.essence_en or '英文名未入库'}）状态转移",
        "",
        f"**结论：{labels[result.verdict]}**",
        f"- POEDB 基础效果原文：{result.base_effect}",
        f"- 输入状态：稀有度 {state.rarity or '未提供'}；物品等级 "
        f"{state.item_level if state.item_level is not None else '未知'}；装备类别 "
        f"{state.item_class or '未提供'}",
        "",
        "| 检查 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    status_zh = {"pass": "通过", "fail": "不通过", "unknown": "未知"}
    for check in result.checks:
        lines.append(f"| {check.key} | {status_zh[check.status]} | {check.summary} |")

    after = result.after
    mode_labels = {
        "upgrade_normal_to_rare": "普通物品升级为稀有物品",
        "reroll_rare_properties": "重置稀有物品属性",
        "unknown": "未知",
    }
    lines += [
        "",
        "条件状态转移（仅在操作实际成功执行时）：",
        f"- 操作模式：{mode_labels.get(after['operation_mode'], after['operation_mode'])}",
        f"- 稀有度：{after.get('rarity') or '未知'}",
        "- 前后缀计数：未知（页面原文未给生成数量）",
    ]
    if result.guaranteed_modifier:
        lines.append(
            f"- {result.guaranteed_modifier['item_class']}保底词缀："
            f"{result.guaranteed_modifier['text']}"
        )
    else:
        lines.append("- 保底词缀：未知（缺少可唯一映射的装备类别）")
    if result.unknown_fields:
        lines += ["", "仍未知：", *[f"- {item}" for item in result.unknown_fields]]
    lines += [
        "",
        f"> 来源：[POEDB]({result.source_url})；抓取时间：{result.fetched_at}。",
        f"> 边界：{result.boundary}。",
        "> 腐化、镜像、裂隙、影响力、隐匿、Eldritch 与 Metamod 交互未在本状态机中"
        "自动补全；命中这些状态时保持未知。",
    ]
    return "\n".join(lines)


def _citations(result: EssenceOperationResult, rendered: str) -> list[dict[str, Any]]:
    return [{
        "chunk_id": f"essence_operation:{result.metadata_id}",
        "entity_id": None,
        "title": f"{result.essence} · POEDB 效果与逐类别保底词缀",
        "url": result.source_url,
        "source": result.source,
        "source_priority": result.source_priority,
        "can_support_facts": True,
        "text": rendered,
        "channel": "structured",
    }]


@REGISTRY.register(
    name="essence_operation_state",
    description=(
        "按 POEDB 精华页原文判断某件精华对明确装备状态的保守状态转移。检查普通/稀有"
        "前提、物品等级上限和装备类别，并返回该类别的保底词缀。可接收完整装备复制文本，"
        "或接收稀有度、物品等级与装备类别。返回 compatible / incompatible / unknown；"
        "不生成额外词缀、成功率、步骤、成本或路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "essence": {"type": "string", "minLength": 1, "maxLength": 80},
            "item_text": {"type": "string", "maxLength": MAX_ITEM_TEXT_CHARS},
            "rarity": {"type": "string", "enum": ["普通", "魔法", "稀有", "传奇"]},
            "item_level": {"type": "integer", "minimum": 1, "maximum": 100},
            "item_class": {"type": "string", "minLength": 1, "maxLength": 80},
            "corrupted": {"type": "boolean", "default": False},
            "mirrored": {"type": "boolean", "default": False},
            "fractured": {"type": "boolean", "default": False},
        },
        "required": ["essence"],
        "additionalProperties": False,
    },
)
def essence_operation_state(
    essence: str,
    item_text: str | None = None,
    rarity: str | None = None,
    item_level: int | None = None,
    item_class: str = "",
    corrupted: bool = False,
    mirrored: bool = False,
    fractured: bool = False,
) -> ToolResult:
    if not (essence or "").strip():
        return ToolResult(ok=False, error="essence 不能为空")
    if item_text and any(value is not None for value in (rarity, item_level)):
        return ToolResult(ok=False, error="item_text 与手工 rarity/item_level/item_class 不能混用")
    if item_text and item_class:
        return ToolResult(ok=False, error="item_text 与手工 rarity/item_level/item_class 不能混用")
    if item_text and len(item_text) > MAX_ITEM_TEXT_CHARS:
        return ToolResult(ok=False, error=f"item_text 最多 {MAX_ITEM_TEXT_CHARS} 字符")
    if item_level is not None and (
        isinstance(item_level, bool) or not isinstance(item_level, int) or not 1 <= item_level <= 100
    ):
        return ToolResult(ok=False, error="item_level 必须是 1 到 100 的整数")
    if len(item_class) > 80:
        return ToolResult(ok=False, error="item_class 最多 80 字符")

    try:
        state = _state_from_item_text(item_text) if item_text else EssenceItemState(
            rarity=rarity,
            item_level=item_level,
            item_class=item_class,
            corrupted=bool(corrupted),
            mirrored=bool(mirrored),
            fractured=bool(fractured),
        )
    except ItemParseError as exc:
        return ToolResult(ok=False, error=f"装备文本解析失败：{exc.reason}")

    result = evaluate_essence_operation(dbmod.get_conn(), essence, state)
    if result is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "essence": essence,
                "boundary": "只接受 poedb_essence 中唯一精确命中的中文名或英文名，不按相似名称猜",
            },
            citations=[],
            meta={"channel": "structured", "table": "poedb_essence"},
        )
    rendered = render_essence_operation(result)
    return ToolResult(
        ok=True,
        data={"found": True, **result.as_dict(), "answer": rendered},
        citations=_citations(result, rendered),
        meta={
            "channel": "structured",
            "table": "poedb_essence",
            "verdict": result.verdict,
        },
    )
