"""古灵通货状态机工具；答案由代码确定性装配。"""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.eldritch_operations import (
    ELDRITCH_ARMOUR_SLOTS,
    OPERATION_NAMES,
    SLOT_LABELS,
    EldritchItemState,
    EldritchOperationResult,
    evaluate_eldritch_operation,
)
from parsers.item_mods import analyze_mods
from parsers.item_text import ItemParseError, parse_item
from tools.registry import REGISTRY, ToolResult

MAX_ITEM_TEXT_CHARS = 20_000
ITEM_SLOTS = (*ELDRITCH_ARMOUR_SLOTS, "other")


def _infer_slot(item_class: str, base_type: str) -> str:
    text = f"{item_class} {base_type}".casefold()
    for slot, markers in (
        ("body_armour", ("body armour", "body armor", "胸甲", "身体装备", "身體裝備")),
        ("boots", ("boots", "鞋子", "鞋")),
        ("gloves", ("gloves", "手套")),
        ("helmet", ("helmet", "helm", "头盔", "頭盔", "头部", "頭部")),
    ):
        if any(marker in text for marker in markers):
            return slot
    return "other"


def _state_from_item_text(text: str) -> EldritchItemState:
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
        reasons.append("含未解锁隐匿词缀，占位方向未知")
    if parsed.unclassified:
        reasons.append(f"{len(parsed.unclassified)} 行内容未归类")
    influences = set(parsed.flags.influences)
    return EldritchItemState(
        rarity=parsed.rarity.value,
        item_slot=_infer_slot(parsed.item_class, parsed.base_type),
        prefix_count=report.prefix_count,
        suffix_count=report.suffix_count,
        has_searing_implicit="searing_exarch" in influences,
        has_eater_implicit="eater_of_worlds" in influences,
        corrupted=parsed.flags.corrupted,
        mirrored=parsed.flags.mirrored,
        fractured=parsed.flags.fractured,
        incomplete_reason="；".join(reasons),
    )


def render_eldritch_operation(result: EldritchOperationResult) -> str:
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
        f"- 输入：部位 {SLOT_LABELS.get(state.item_slot or '', state.item_slot or '未提供')}；"
        f"首要 {state.dominance or '未提供'}；前缀 "
        f"{state.prefix_count if state.prefix_count is not None else '未知'}；后缀 "
        f"{state.suffix_count if state.suffix_count is not None else '未知'}",
        "",
        "| 检查 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    status_zh = {"pass": "通过", "fail": "不通过", "unknown": "未知"}
    for check in result.checks:
        lines.append(f"| {check.key} | {status_zh[check.status]} | {check.summary} |")

    after = result.after
    lines += ["", "条件状态转移（仅在操作实际成功执行时）："]
    implicit_labels = {
        "unknown": "未知（来源效果原文已变化）",
        "unchanged": "基底词缀不变",
        "set_searing_and_replace_all_except_eater": "添加指定档级的焚界者基底词缀；除灭界者基底词缀外，替换其他原有基底词缀",
        "set_eater_and_replace_all_except_searing": "添加指定档级的灭界者基底词缀；除焚界者基底词缀外，替换其他原有基底词缀",
        "random_one_raised_other_lowered": "随机提高焚界者或灭界者一方，并降低另一方；被降低的次级词缀会被移除",
    }
    explicit_labels = {
        "unknown": "未知（来源效果原文已变化）",
        "unchanged": "显式词缀不变",
        "prefix_modifiers_rerolled": "重置前缀，后缀保持不变",
        "suffix_modifiers_rerolled": "重置后缀，前缀保持不变",
        "one_random_prefix_added": "添加一个随机前缀",
        "one_random_suffix_added": "添加一个随机后缀",
        "one_random_prefix_removed": "移除一个随机前缀",
        "one_random_suffix_removed": "移除一个随机后缀",
    }
    lines.append(f"- 基底词缀：{implicit_labels.get(after['implicit_modifier_change'], after['implicit_modifier_change'])}")
    lines.append(f"- 显式词缀：{explicit_labels.get(after['explicit_modifier_change'], after['explicit_modifier_change'])}")
    if after.get("searing_implicit_tier"):
        lines.append(f"- 新焚界者基底词缀档级：{after['searing_implicit_tier']}")
    if after.get("eater_implicit_tier"):
        lines.append(f"- 新灭界者基底词缀档级：{after['eater_implicit_tier']}")
    lines.append(
        "- 前后缀计数："
        f"{after['prefix_count'] if after['prefix_count'] is not None else '未知'} 前缀 + "
        f"{after['suffix_count'] if after['suffix_count'] is not None else '未知'} 后缀"
    )
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


def _citations(result: EldritchOperationResult, rendered: str) -> list[dict[str, Any]]:
    citations = [{
        "chunk_id": f"eldritch_operation:{result.metadata_id}",
        "entity_id": None,
        "title": f"{result.operation} · POEDB 效果与状态转移",
        "url": result.source_url,
        "source": result.source,
        "source_priority": result.source_priority,
        "can_support_facts": True,
        "text": rendered,
        "channel": "structured",
    }]
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
    if result.metamod_interactions:
        citations.append({
            "chunk_id": f"eldritch_metamod:{result.operation}",
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
    return citations


@REGISTRY.register(
    name="eldritch_operation_state",
    description=(
        "按 POEDB 原文模拟 12 种古灵通货：四档余烬、四档溶液、古灵混沌石、"
        "古灵崇高石、古灵剥离石与冲突石。检查四个装备部位、首要影响、前后缀名额与"
        "双基底词缀状态；并列返回已有元词缀矩阵原值。不生成具体随机词缀、概率、步骤、成本或路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": list(OPERATION_NAMES)},
            "item_text": {"type": "string", "maxLength": MAX_ITEM_TEXT_CHARS},
            "rarity": {"type": "string", "enum": ["普通", "魔法", "稀有", "传奇"]},
            "item_slot": {"type": "string", "enum": list(ITEM_SLOTS)},
            "dominance": {"type": "string", "enum": ["焚界者", "灭界者", "平衡", "无"]},
            "prefix_count": {"type": "integer", "minimum": 0, "maximum": 6},
            "suffix_count": {"type": "integer", "minimum": 0, "maximum": 6},
            "has_searing_implicit": {"type": "boolean"},
            "has_eater_implicit": {"type": "boolean"},
            "corrupted": {"type": "boolean", "default": False},
            "mirrored": {"type": "boolean", "default": False},
            "fractured": {"type": "boolean", "default": False},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
)
def eldritch_operation_state(
    operation: str,
    item_text: str | None = None,
    rarity: str | None = None,
    item_slot: str | None = None,
    dominance: str | None = None,
    prefix_count: int | None = None,
    suffix_count: int | None = None,
    has_searing_implicit: bool | None = None,
    has_eater_implicit: bool | None = None,
    corrupted: bool = False,
    mirrored: bool = False,
    fractured: bool = False,
) -> ToolResult:
    if not (operation or "").strip():
        return ToolResult(ok=False, error="operation 不能为空")
    manual = (rarity, item_slot, dominance, prefix_count, suffix_count,
              has_searing_implicit, has_eater_implicit)
    if item_text and any(value is not None for value in manual):
        return ToolResult(ok=False, error="item_text 与手工状态字段不能混用")
    if item_text and len(item_text) > MAX_ITEM_TEXT_CHARS:
        return ToolResult(ok=False, error=f"item_text 最多 {MAX_ITEM_TEXT_CHARS} 字符")
    if item_slot is not None and item_slot not in ITEM_SLOTS:
        return ToolResult(ok=False, error=f"不支持的 item_slot={item_slot!r}")
    if dominance is not None and dominance not in ("焚界者", "灭界者", "平衡", "无"):
        return ToolResult(ok=False, error=f"不支持的 dominance={dominance!r}")
    for label, value in (("prefix_count", prefix_count), ("suffix_count", suffix_count)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6):
            return ToolResult(ok=False, error=f"{label} 必须是 0 到 6 的整数")

    try:
        state = _state_from_item_text(item_text) if item_text else EldritchItemState(
            rarity=rarity,
            item_slot=item_slot,
            dominance=dominance,  # type: ignore[arg-type]
            prefix_count=prefix_count,
            suffix_count=suffix_count,
            has_searing_implicit=has_searing_implicit,
            has_eater_implicit=has_eater_implicit,
            corrupted=bool(corrupted),
            mirrored=bool(mirrored),
            fractured=bool(fractured),
        )
    except ItemParseError as exc:
        return ToolResult(ok=False, error=f"装备文本解析失败：{exc.reason}")

    result = evaluate_eldritch_operation(dbmod.get_conn(), operation, state)
    if result is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "operation": operation,
                "supported_operations": list(OPERATION_NAMES),
                "boundary": "只支持 12 种已审计古灵通货，不按相似名称猜操作语义",
            },
            citations=[],
            meta={"channel": "structured", "table": "poedb_currency"},
        )
    rendered = render_eldritch_operation(result)
    return ToolResult(
        ok=True,
        data={"found": True, **result.as_dict(), "answer": rendered},
        citations=_citations(result, rendered),
        meta={"channel": "structured", "table": "poedb_currency", "verdict": result.verdict},
    )
