"""加密通货状态机工具；结果完全由代码装配。"""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.veiled_operations import (
    OPERATION_NAMES,
    VeiledItemState,
    VeiledOperationResult,
    evaluate_veiled_operation,
)
from parsers.item_mods import analyze_mods
from parsers.item_text import ItemParseError, parse_item
from tools.registry import REGISTRY, ToolResult

MAX_ITEM_TEXT_CHARS = 20_000
MAX_RENDERED_CANDIDATES = 12


def _state_from_item_text(text: str) -> VeiledItemState:
    parsed = parse_item(text)
    report = analyze_mods(dbmod.get_conn(), parsed)
    unresolved = [
        item for item in report.mods
        if item.mod.kind.value in {"explicit", "crafted", "fractured"} and not item.affix_type
    ]
    reasons = []
    if unresolved:
        reasons.append(f"{len(unresolved)} 条显式词缀未识别为前缀或后缀")
    if parsed.flags.unidentified:
        reasons.append("物品未鉴定，显示状态不完整")
    if parsed.flags.veiled:
        reasons.append("输入已有未解锁隐匿占位；其前后缀方向未从普通复制文本确定")
    if parsed.unclassified:
        reasons.append(f"{len(parsed.unclassified)} 行内容未归类")
    return VeiledItemState(
        rarity=parsed.rarity.value,
        item_class=parsed.item_class,
        prefix_count=report.prefix_count,
        suffix_count=report.suffix_count,
        corrupted=parsed.flags.corrupted,
        mirrored=parsed.flags.mirrored,
        fractured=parsed.flags.fractured,
        incomplete_reason="；".join(reasons),
    )


def render_veiled_operation(result: VeiledOperationResult) -> str:
    labels = {
        "compatible": "已知约束兼容",
        "incompatible": "已知约束不兼容",
        "unknown": "信息不足",
    }
    state = result.before
    lines = [
        f"### {result.operation}（{result.operation_en}）状态转移",
        "",
        f"**结论：{labels[result.verdict]}**",
        f"- POEDB 效果原文：{result.effect_text}",
        f"- 输入：稀有度 {state.rarity or '未提供'}；类别 {state.item_class or '未提供'}；"
        f"前缀 {state.prefix_count if state.prefix_count is not None else '未知'}；"
        f"后缀 {state.suffix_count if state.suffix_count is not None else '未知'}",
        "",
        "| 检查 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    status_zh = {"pass": "通过", "fail": "不通过", "unknown": "未知"}
    for check in result.checks:
        lines.append(f"| {check.key} | {status_zh[check.status]} | {check.summary} |")

    after = result.after
    change_labels = {
        "all_modifiers_rerolled_with_one_random_veiled_modifier": "重置全部词缀，并包含一个随机加密词缀",
        "one_random_modifier_removed_and_one_random_veiled_modifier_added": "移除一个随机词缀，并添加一个随机加密词缀",
    }
    lines += [
        "",
        "条件状态转移（仅在操作实际成功执行时）：",
        f"- 词缀变化：{change_labels.get(after['modifier_change'], after['modifier_change'])}",
        f"- 加密词缀可能位置：{' / '.join(after['veiled_modifier_position'])}",
    ]
    if after["possible_affix_counts"]:
        counts = " 或 ".join(
            f"{item['prefix_count']} 前缀 + {item['suffix_count']} 后缀"
            for item in after["possible_affix_counts"]
        )
        lines.append(f"- 前后缀计数可能为：{counts}")
    else:
        lines.append(
            "- 前后缀计数："
            f"{after['prefix_count'] if after['prefix_count'] is not None else '未知'} 前缀 + "
            f"{after['suffix_count'] if after['suffix_count'] is not None else '未知'} 后缀"
        )

    pool = result.veiled_pool
    if pool:
        lines += [
            "",
            f"隐匿来源词缀目录：{pool['item_class']}，去重后 {pool['unique_candidates']} 条"
            f"（前缀 {pool['prefix_candidates']} / 后缀 {pool['suffix_candidates']}；"
            f"底层 {pool['source_rows']} 行，覆盖 {pool['scope']}）。",
            "",
            "| 生成位 | 词缀名 | 词缀文本 | 物品等级 | 页面权重 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for candidate in pool["candidates"][:MAX_RENDERED_CANDIDATES]:
            lines.append(
                f"| {candidate['position']} | {candidate['name_zh'] or '—'} | {candidate['text']} "
                f"| {candidate['item_level']} | {candidate['weight'] or '无数据'} |"
            )
        remaining = pool["unique_candidates"] - min(
            pool["unique_candidates"], MAX_RENDERED_CANDIDATES
        )
        if remaining:
            lines.append(f"（以上列前 {MAX_RENDERED_CANDIDATES} 条，另有 {remaining} 条保留在结构化结果中。）")
        lines.append(f"> 目录边界：{pool['caveat']}。")

    if result.metamod_interactions:
        lines += ["", "已采集的 Craft of Exile 元词缀矩阵原值："]
        for cell in result.metamod_interactions:
            note = f"（{cell['note']}）" if cell.get("note") else ""
            lines.append(f"- {cell['guard']}：{cell['raw_value']}{note}")
    if result.unknown_fields:
        lines += ["", "仍未知：", *[f"- {item}" for item in result.unknown_fields]]
    lines += [
        "",
        f"> 来源：[POEDB]({result.source_url})；抓取时间：{result.fetched_at}。",
        f"> 边界：{result.boundary}。",
        "> 元词缀矩阵原值只作并列披露，不自动解释成当前装备一定可执行。",
    ]
    return "\n".join(lines)


def _citations(result: VeiledOperationResult, rendered: str) -> list[dict[str, Any]]:
    citations = [{
        "chunk_id": f"veiled_operation:{result.metadata_id}",
        "entity_id": None,
        "title": f"{result.operation} · POEDB 效果与状态转移",
        "url": result.source_url,
        "source": result.source,
        "source_priority": result.source_priority,
        "can_support_facts": True,
        "text": rendered,
        "channel": "structured",
    }]
    pool = result.veiled_pool
    if pool and pool["candidates"]:
        citations.append({
            "chunk_id": f"veiled_pool:{pool['item_class']}",
            "entity_id": None,
            "title": f"{pool['item_class']} · 隐匿来源词缀目录",
            "url": pool["candidates"][0]["url"],
            "source": "POEDB",
            "source_priority": 1,
            "can_support_facts": True,
            "text": f"{pool['item_class']}：去重后 {pool['unique_candidates']} 条；"
                    f"前缀 {pool['prefix_candidates']}，后缀 {pool['suffix_candidates']}。"
                    f"{pool['caveat']}",
            "channel": "structured",
        })
    if result.metamod_interactions:
        citations.append({
            "chunk_id": f"veiled_metamod:{result.operation}",
            "entity_id": None,
            "title": f"{result.operation} · 元词缀交互矩阵",
            "url": str(result.metamod_interactions[0]["source_url"]),
            "source": "Craft of Exile",
            "source_priority": 4,
            "can_support_facts": True,
            "text": "\n".join(
                f"{cell['guard']}：{cell['raw_value']}" for cell in result.metamod_interactions
            ),
            "channel": "structured",
        })
    capacity = result.capacity
    if capacity and capacity.rule_url and capacity.rule_url != result.source_url:
        citations.append({
            "chunk_id": f"craft_capacity:{capacity.rarity}:{capacity.item_kind}",
            "entity_id": None,
            "title": f"{capacity.rarity}装备前后缀名额",
            "url": capacity.rule_url,
            "source": capacity.source,
            "source_priority": capacity.source_priority,
            "can_support_facts": True,
            "text": capacity.summary,
            "channel": "structured",
        })
    return citations


@REGISTRY.register(
    name="veiled_operation_state",
    description=(
        "按 POEDB 原文模拟加密崇高石与加密混沌石：检查稀有度和可移除词缀，"
        "给出随机移除/重铸并加入加密词缀的条件状态；指定底材类别时列出本库"
        "category=veiled 的去重候选目录。目录不是单次揭露选项，权重不是操作结果概率。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": list(OPERATION_NAMES)},
            "item_text": {"type": "string", "maxLength": MAX_ITEM_TEXT_CHARS},
            "rarity": {"type": "string", "enum": ["普通", "魔法", "稀有", "传奇"]},
            "item_class": {"type": "string", "minLength": 1, "maxLength": 80},
            "prefix_count": {"type": "integer", "minimum": 0, "maximum": 6},
            "suffix_count": {"type": "integer", "minimum": 0, "maximum": 6},
            "corrupted": {"type": "boolean", "default": False},
            "mirrored": {"type": "boolean", "default": False},
            "fractured": {"type": "boolean", "default": False},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
)
def veiled_operation_state(
    operation: str,
    item_text: str | None = None,
    rarity: str | None = None,
    item_class: str = "",
    prefix_count: int | None = None,
    suffix_count: int | None = None,
    corrupted: bool = False,
    mirrored: bool = False,
    fractured: bool = False,
) -> ToolResult:
    if not (operation or "").strip():
        return ToolResult(ok=False, error="operation 不能为空")
    if item_text and any(value is not None for value in (rarity, prefix_count, suffix_count)):
        return ToolResult(ok=False, error="item_text 与手工 rarity/prefix_count/suffix_count 不能混用")
    if item_text and item_class:
        return ToolResult(ok=False, error="item_text 与手工 item_class 不能混用")
    if item_text and len(item_text) > MAX_ITEM_TEXT_CHARS:
        return ToolResult(ok=False, error=f"item_text 最多 {MAX_ITEM_TEXT_CHARS} 字符")
    for label, value in (("prefix_count", prefix_count), ("suffix_count", suffix_count)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6):
            return ToolResult(ok=False, error=f"{label} 必须是 0 到 6 的整数")

    try:
        state = _state_from_item_text(item_text) if item_text else VeiledItemState(
            rarity=rarity,
            item_class=item_class,
            prefix_count=prefix_count,
            suffix_count=suffix_count,
            corrupted=bool(corrupted),
            mirrored=bool(mirrored),
            fractured=bool(fractured),
        )
    except ItemParseError as exc:
        return ToolResult(ok=False, error=f"装备文本解析失败：{exc.reason}")

    result = evaluate_veiled_operation(dbmod.get_conn(), operation, state)
    if result is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "operation": operation,
                "supported_operations": list(OPERATION_NAMES),
                "boundary": "只支持两种已有逐字效果的加密通货，不按名称猜爱斯林工艺语义",
            },
            citations=[],
            meta={"channel": "structured", "table": "poedb_currency"},
        )
    rendered = render_veiled_operation(result)
    return ToolResult(
        ok=True,
        data={"found": True, **result.as_dict(), "answer": rendered},
        citations=_citations(result, rendered),
        meta={"channel": "structured", "table": "poedb_currency+mod_affix", "verdict": result.verdict},
    )
