"""基础通货制作操作状态机工具；结果完全由代码装配，不经模型改写。"""

from __future__ import annotations

import json
from typing import Any

from core import db as dbmod
from craft.capacity import infer_item_kind
from craft.operations import ItemState, OPERATION_NAMES, OperationResult, evaluate_operation
from parsers.item_mods import analyze_mods
from parsers.item_text import ItemParseError, parse_item
from tools.impl.metamods import metamod_interaction_matrix
from tools.registry import REGISTRY, ToolResult

MAX_ITEM_TEXT_CHARS = 20_000
ITEM_KINDS = ("default", "jewel", "flask", "graft", "jewel_abyss_corrupted")
METAMOD_GUARDS = (
    "前缀无法被变更", "后缀无法被变更", "无法骰出攻击词缀", "无法骰出法术词缀",
)
# 基础通货状态机沿用当前游戏中文名；来源矩阵仍使用旧译名“无效石”。只在这张
# 逐字桥接表内转换，不能把任意相似通货名猜成矩阵操作。
_METAMOD_OPERATION_NAMES = {"剥离石": "无效石"}


def _state_from_item_text(text: str) -> ItemState:
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
    return ItemState(
        rarity=parsed.rarity.value,
        prefix_count=report.prefix_count,
        suffix_count=report.suffix_count,
        item_kind=infer_item_kind(
            parsed.item_class, parsed.base_type, corrupted=parsed.flags.corrupted
        ),
        corrupted=parsed.flags.corrupted,
        mirrored=parsed.flags.mirrored,
        fractured=parsed.flags.fractured,
        incomplete_reason="；".join(reasons),
    )


def render_operation(result: OperationResult) -> str:
    state = result.before
    labels = {"compatible": "已知约束兼容", "incompatible": "已知约束不兼容", "unknown": "信息不足"}
    lines = [
        f"### {result.operation}（{result.operation_en}）状态转移",
        "",
        f"**结论：{labels[result.verdict]}**",
        f"- POEDB 效果原文：{result.effect_text}",
        f"- 输入状态：稀有度 {state.rarity or '未提供'}；前缀 "
        f"{state.prefix_count if state.prefix_count is not None else '未知'}；后缀 "
        f"{state.suffix_count if state.suffix_count is not None else '未知'}；种类 {state.item_kind}",
        "",
        "| 检查 | 状态 | 说明 |",
        "| --- | --- | --- |",
    ]
    status_zh = {"pass": "通过", "fail": "不通过", "unknown": "未知"}
    for check in result.checks:
        lines.append(f"| {check.key} | {status_zh[check.status]} | {check.summary} |")

    after = result.after
    lines += ["", "条件状态转移（仅在操作实际成功执行时）："]
    if after.get("possible_rarities"):
        lines.append("- 稀有度可能为：" + " / ".join(after["possible_rarities"]))
    else:
        lines.append(f"- 稀有度：{after.get('rarity') or '未知'}")
    if after.get("possible_affix_counts"):
        choices = " 或 ".join(
            f"{item['prefix_count']} 前缀 + {item['suffix_count']} 后缀"
            for item in after["possible_affix_counts"]
        )
        lines.append(f"- 前后缀计数可能为：{choices}")
    else:
        prefix = after.get("prefix_count")
        suffix = after.get("suffix_count")
        lines.append(
            f"- 前后缀计数：{prefix if prefix is not None else '未知'} 前缀 + "
            f"{suffix if suffix is not None else '未知'} 后缀"
        )
    identity_labels = {
        "unchanged": "现有词缀身份不变",
        "one_random_modifier_added": "新增一条随机词缀",
        "all_properties_rerolled": "现有属性被新的随机属性重铸",
        "all_properties_removed": "属性全部移除",
        "one_random_property_removed": "随机移除一条属性",
        "not_specified_by_source": "来源原文未说明",
    }
    value_labels = {
        "unchanged": "现有词缀数值不变",
        "rerolled_with_properties": "随属性一并重铸",
        "random_modifier_values_rerolled": "随机词缀数值被重置",
    }
    identities = after.get("modifier_identities")
    values = after.get("modifier_values")
    lines += [
        f"- 词缀身份变化：{identity_labels.get(identities, identities)}",
        f"- 词缀数值变化：{value_labels.get(values, values)}",
    ]
    if result.unknown_fields:
        lines += ["", "仍未知：", *[f"- {item}" for item in result.unknown_fields]]
    lines += [
        "",
        f"> 来源：[POEDB]({result.source_url})；抓取时间：{result.fetched_at}。",
        f"> 边界：{result.boundary}。",
        "> 腐化、镜像、裂隙、影响力、隐匿与 Eldritch 未在本状态机中自动补全；"
        "Metamod 只有在显式提供逐字 guard 时并列披露来源矩阵原值。",
    ]
    return "\n".join(lines)


def _citations(result: OperationResult, rendered: str) -> list[dict[str, Any]]:
    citations = [{
        "chunk_id": f"craft_operation:{result.metadata_id}",
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
            "title": f"{capacity.rarity} × {capacity.item_kind} 前后缀名额",
            "url": capacity.rule_url,
            "source": capacity.source,
            "source_priority": capacity.source_priority,
            "can_support_facts": True,
            "text": capacity.summary,
            "channel": "structured",
        })
    return citations


def _metamod_context(
    result: OperationResult, guard: str
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """读取一个明确 guard 的矩阵原值；不解释 YES/NO 的游戏语义。"""
    matrix_operation = _METAMOD_OPERATION_NAMES.get(result.operation, result.operation)
    lookup = metamod_interaction_matrix(matrix_operation, guard)
    if not lookup.ok:
        reason = lookup.error or "Metamod 矩阵查询失败"
        return ({
            "status": "error",
            "operation": matrix_operation,
            "guard": guard,
            "reason": reason,
        }, [], f"\n\nMetamod 交互：{reason}；因此不生成该元词缀下的结论。")
    data = lookup.data or {}
    if not data.get("found"):
        status = "source_blank" if data.get("source_blank") else "not_recorded"
        reason = str(data.get("missing_reason") or "没有逐字匹配的来源矩阵记录")
        return ({
            "status": status,
            "operation": matrix_operation,
            "guard": guard,
            "reason": reason,
        }, [], "\n".join([
            "",
            "Metamod 交互：",
            f"- {matrix_operation} × {guard}：{reason}",
            "- 因此只保留基础通货效果，不能给出该元词缀下的交互结论。",
        ]))

    citations = list(lookup.citations)
    if len(citations) != 1 or not citations[0].get("claim_data"):
        return ({
            "status": "error",
            "operation": matrix_operation,
            "guard": guard,
            "reason": "矩阵记录缺少唯一结构化引用，拒绝合成",
        }, [], "\n\nMetamod 交互：矩阵记录缺少唯一结构化引用，拒绝合成。")
    claim = dict(citations[0].get("claim_data") or {}) if citations else {}
    context = {
        "status": "recorded",
        "operation": matrix_operation,
        "guard": guard,
        "section": claim.get("section", ""),
        "outcome": claim.get("outcome", ""),
        "raw_value": claim.get("raw_value", ""),
        "note_text": claim.get("note_text") or "",
        "version_scope": claim.get("version_scope", "unknown"),
        "source_url": claim.get("source_url", ""),
        "mirror_url": claim.get("mirror_url", ""),
    }
    note = f"；原始注释：{context['note_text']}" if context["note_text"] else ""
    rendered = "\n".join([
        "",
        "Metamod 交互（来源矩阵原值）：",
        f"- {matrix_operation} × {guard}：{context['raw_value']}{note}",
        f"- 版本范围：{context['version_scope']}",
        "- 该矩阵值只作为独立事实并列披露；不解释 YES/NO 的外延，也不据此改写上面的状态转移。",
    ])
    return context, citations, rendered


@REGISTRY.register(
    name="craft_operation_state",
    description=(
        "按 POEDB 效果原文模拟 10 种基础通货的保守状态转移：增幅、崇高、重铸、神圣、"
        "剥离、改造、混沌、富豪、机会、点金。可直接接收游戏复制的装备文本，或接收"
        "稀有度与前后缀计数；新增词缀操作会查结构化名额规则。返回 compatible / "
        "incompatible / unknown。可选逐字 metamod_guard 只并列来源矩阵原值；"
        "不解释该值，不生成具体结果词缀、概率、步骤、成本或路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": list(OPERATION_NAMES)},
            "item_text": {"type": "string", "maxLength": MAX_ITEM_TEXT_CHARS},
            "rarity": {"type": "string", "enum": ["普通", "魔法", "稀有", "传奇"]},
            "prefix_count": {"type": "integer", "minimum": 0, "maximum": 6},
            "suffix_count": {"type": "integer", "minimum": 0, "maximum": 6},
            "item_kind": {"type": "string", "enum": list(ITEM_KINDS), "default": "default"},
            "corrupted": {"type": "boolean", "default": False},
            "mirrored": {"type": "boolean", "default": False},
            "fractured": {"type": "boolean", "default": False},
            "metamod_guard": {
                "type": "string",
                "enum": list(METAMOD_GUARDS),
                "description": (
                    "可选的来源矩阵逐字列名；只并列返回该基础通货与元词缀的原始交互值，"
                    "不解释其外延或改写状态转移。"
                ),
            },
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
)
def craft_operation_state(
    operation: str,
    item_text: str | None = None,
    rarity: str | None = None,
    prefix_count: int | None = None,
    suffix_count: int | None = None,
    item_kind: str = "default",
    corrupted: bool = False,
    mirrored: bool = False,
    fractured: bool = False,
    metamod_guard: str | None = None,
) -> ToolResult:
    if not (operation or "").strip():
        return ToolResult(ok=False, error="operation 不能为空")
    if item_text and any(value is not None for value in (rarity, prefix_count, suffix_count)):
        return ToolResult(ok=False, error="item_text 与手工 rarity/prefix_count/suffix_count 不能混用")
    if item_text and len(item_text) > MAX_ITEM_TEXT_CHARS:
        return ToolResult(ok=False, error=f"item_text 最多 {MAX_ITEM_TEXT_CHARS} 字符")
    if item_kind not in ITEM_KINDS:
        return ToolResult(ok=False, error=f"不支持的 item_kind={item_kind!r}")
    guard = str(metamod_guard or "").strip()
    if guard and guard not in METAMOD_GUARDS:
        return ToolResult(ok=False, error="metamod_guard 必须是来源矩阵的逐字列名")
    for label, value in (("prefix_count", prefix_count), ("suffix_count", suffix_count)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6):
            return ToolResult(ok=False, error=f"{label} 必须是 0 到 6 的整数")

    try:
        state = _state_from_item_text(item_text) if item_text else ItemState(
            rarity=rarity,
            prefix_count=prefix_count,
            suffix_count=suffix_count,
            item_kind=item_kind,
            corrupted=bool(corrupted),
            mirrored=bool(mirrored),
            fractured=bool(fractured),
        )
    except ItemParseError as exc:
        return ToolResult(ok=False, error=f"装备文本解析失败：{exc.reason}")

    result = evaluate_operation(dbmod.get_conn(), operation, state)
    if result is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "operation": operation,
                "supported_operations": list(OPERATION_NAMES),
                "boundary": "只支持已审计的 10 种基础通货，不按相似名称猜操作语义",
            },
            citations=[],
            meta={"channel": "structured", "table": "poedb_currency"},
        )
    rendered = render_operation(result)
    base_citations = _citations(result, rendered)
    metamod = {
        "status": "not_requested", "operation": "", "guard": "", "reason": "",
    }
    metamod_citations: list[dict[str, Any]] = []
    unknown_fields = list(result.unknown_fields)
    verdict = result.verdict
    if guard:
        metamod, metamod_citations, metamod_rendered = _metamod_context(result, guard)
        rendered += metamod_rendered
        if metamod["status"] != "recorded":
            unknown_fields.append(f"{metamod['operation']} × {guard} 的明确 Metamod 矩阵结果")
            if verdict == "compatible":
                verdict = "unknown"
        rendered += (
            "\n\n> 合流边界：基础通货效果来自 POEDB；Metamod 交互来自 Craft of Exile "
            "矩阵镜像且版本范围未给。两者并列不等于完整制作可执行性。"
        )
    payload = result.as_dict()
    payload.update({
        "verdict": verdict,
        "operation_verdict": result.verdict,
        "unknown_fields": list(dict.fromkeys(unknown_fields)),
        "metamod_guard": guard,
        "metamod_interaction": metamod,
        "boundary": (
            result.boundary
            + (
                "；Metamod 只并列来源矩阵逐字原值，页面未给版本范围，不据此解释"
                " YES/NO 或改写状态转移"
                if guard else ""
            )
        ),
        "answer": rendered,
    })
    return ToolResult(
        ok=True,
        data={"found": True, **payload},
        citations=base_citations + metamod_citations,
        meta={
            "channel": "structured",
            "table": "poedb_currency",
            "tables": ["poedb_currency", "craft_metamod_interaction"] if guard
            else ["poedb_currency"],
            "verdict": verdict,
        },
    )
