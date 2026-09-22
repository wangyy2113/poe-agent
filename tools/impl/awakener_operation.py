"""Awakener's Orb two-item state tool with deterministic rendering."""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.awakener_operations import (
    METADATA_ID,
    NAME_EN,
    NAME_ZH,
    AwakenerItemState,
    AwakenerOperationResult,
    evaluate_awakener_orb,
)
from parsers.item_text import ItemParseError, ParsedItem, parse_item
from tools.registry import REGISTRY, ToolResult

MAX_ITEM_TEXT_CHARS = 20_000


def _state_from_parsed(item: ParsedItem) -> AwakenerItemState:
    reasons: list[str] = []
    if item.flags.unidentified:
        reasons.append("物品未鉴定，显示状态不完整")
    if item.unclassified:
        reasons.append(f"{len(item.unclassified)} 行内容未归类")
    if not item.item_class:
        reasons.append("物品类别未解析")
    return AwakenerItemState(
        item_class=item.item_class,
        rarity=item.rarity.value,
        influences=tuple(item.flags.influences),
        corrupted=item.flags.corrupted,
        mirrored=item.flags.mirrored,
        fractured=item.flags.fractured,
        incomplete_reason="；".join(reasons),
    )


def _manual_state(
    item_class: str,
    rarity: str,
    influences: list[str] | None,
    corrupted: bool,
    mirrored: bool,
    fractured: bool,
) -> AwakenerItemState:
    return AwakenerItemState(
        item_class=(item_class or "").strip(),
        rarity=(rarity or "").strip(),
        influences=tuple(str(value).strip() for value in (influences or []) if str(value).strip()),
        corrupted=bool(corrupted),
        mirrored=bool(mirrored),
        fractured=bool(fractured),
    )


def render_awakener_operation(result: AwakenerOperationResult) -> str:
    labels = {
        "compatible": "已知约束兼容",
        "incompatible": "已知约束不兼容",
        "unknown": "信息不足",
    }
    donor = result.donor
    target = result.target
    lines = [
        f"### {NAME_ZH}（{NAME_EN}）双物品状态转移",
        "",
        f"**结论：{labels[result.verdict]}**",
        f"- POEDB 效果原文：{result.effect_text}",
        f"- 供体：类别 {donor.item_class or '未提供'}；影响 "
        f"{' / '.join(donor.influences) or '未提供'}",
        f"- 目标：类别 {target.item_class or '未提供'}；影响 "
        f"{' / '.join(target.influences) or '未提供'}",
        "",
        "| 检查 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    status_zh = {"pass": "通过", "fail": "不通过", "unknown": "未知"}
    for check in result.checks:
        lines.append(f"| {check.key} | {status_zh[check.status]} | {check.summary} |")

    after = result.after
    lines += ["", "条件状态转移（仅在操作实际成功执行时）："]
    donor_destroyed = after.get("donor_destroyed")
    lines.append(
        "- 供体：" + ("被摧毁" if donor_destroyed is True else "来源契约变化，无法确定")
    )
    lines.append(f"- 目标稀有度：{after.get('target_rarity') or '未知'}")
    influences = after.get("target_influences") or []
    lines.append(f"- 目标影响：{' + '.join(influences) if influences else '无法唯一确定'}")
    modifier_label = {
        "reforged_with_new_modifiers": "重铸并获得新词缀；具体结果未知",
        "unknown": "未知",
    }.get(after.get("target_modifier_identities"), str(after.get("target_modifier_identities")))
    lines.append(f"- 目标词缀：{modifier_label}")
    if result.unknown_fields:
        lines += ["", "仍未知：", *[f"- {item}" for item in result.unknown_fields]]
    lines += [
        "",
        f"> 来源：[POEDB]({result.source_url})；抓取时间：{result.fetched_at}。",
        f"> 边界：{result.boundary}。",
    ]
    return "\n".join(lines)


def _citation(result: AwakenerOperationResult, rendered: str) -> list[dict[str, Any]]:
    return [{
        "chunk_id": f"awakener_operation:{METADATA_ID}",
        "entity_id": None,
        "title": f"{NAME_ZH} · POEDB 效果与双物品状态转移",
        "url": result.source_url,
        "source": result.source,
        "source_priority": result.source_priority,
        "can_support_facts": True,
        "text": rendered,
        "channel": "structured",
    }]


@REGISTRY.register(
    name="awakener_orb_state",
    description=(
        "按 POEDB 原文检查觉醒者之石的供体与目标：两件物品类别是否相同、各自是否有"
        "唯一且不同的传统影响，并给出供体销毁、目标重铸为稀有且兼有两种影响的条件后状态。"
        "可直接传两段游戏复制装备文本。不会声称保留具体影响词缀，也不提供概率、成本或路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "donor_item_text": {"type": "string", "maxLength": MAX_ITEM_TEXT_CHARS},
            "target_item_text": {"type": "string", "maxLength": MAX_ITEM_TEXT_CHARS},
            "donor_item_class": {"type": "string", "maxLength": 80},
            "target_item_class": {"type": "string", "maxLength": 80},
            "donor_rarity": {"type": "string", "enum": ["普通", "魔法", "稀有", "传奇"]},
            "target_rarity": {"type": "string", "enum": ["普通", "魔法", "稀有", "传奇"]},
            "donor_influences": {
                "type": "array", "items": {"type": "string", "minLength": 1},
                "maxItems": 4, "uniqueItems": True,
            },
            "target_influences": {
                "type": "array", "items": {"type": "string", "minLength": 1},
                "maxItems": 4, "uniqueItems": True,
            },
            "donor_corrupted": {"type": "boolean", "default": False},
            "target_corrupted": {"type": "boolean", "default": False},
            "donor_mirrored": {"type": "boolean", "default": False},
            "target_mirrored": {"type": "boolean", "default": False},
            "donor_fractured": {"type": "boolean", "default": False},
            "target_fractured": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    },
)
def awakener_orb_state(
    donor_item_text: str | None = None,
    target_item_text: str | None = None,
    donor_item_class: str = "",
    target_item_class: str = "",
    donor_rarity: str = "",
    target_rarity: str = "",
    donor_influences: list[str] | None = None,
    target_influences: list[str] | None = None,
    donor_corrupted: bool = False,
    target_corrupted: bool = False,
    donor_mirrored: bool = False,
    target_mirrored: bool = False,
    donor_fractured: bool = False,
    target_fractured: bool = False,
) -> ToolResult:
    if donor_item_text and (
        donor_item_class or donor_rarity or donor_influences
        or donor_corrupted or donor_mirrored or donor_fractured
    ):
        return ToolResult(ok=False, error="donor_item_text 与供体手工状态字段不能混用")
    if target_item_text and (
        target_item_class or target_rarity or target_influences
        or target_corrupted or target_mirrored or target_fractured
    ):
        return ToolResult(ok=False, error="target_item_text 与目标手工状态字段不能混用")
    try:
        donor = (
            _state_from_parsed(parse_item(donor_item_text)) if donor_item_text
            else _manual_state(
                donor_item_class, donor_rarity, donor_influences,
                donor_corrupted, donor_mirrored, donor_fractured,
            )
        )
        target = (
            _state_from_parsed(parse_item(target_item_text)) if target_item_text
            else _manual_state(
                target_item_class, target_rarity, target_influences,
                target_corrupted, target_mirrored, target_fractured,
            )
        )
    except ItemParseError as exc:
        return ToolResult(ok=False, error=f"装备文本解析失败：{exc.reason}")

    result = evaluate_awakener_orb(dbmod.get_conn(), donor, target)
    if result is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "operation": NAME_ZH,
                "reason": "本地 poedb_currency 没有觉醒者之石的精确来源记录",
            },
            citations=[],
            meta={"channel": "structured", "table": "poedb_currency"},
        )
    rendered = render_awakener_operation(result)
    return ToolResult(
        ok=True,
        data={**result.as_dict(), "answer": rendered},
        citations=_citation(result, rendered),
        meta={
            "channel": "structured",
            "table": "poedb_currency",
            "verdict": result.verdict,
        },
    )
