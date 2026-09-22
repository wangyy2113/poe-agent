"""化石标签权重与地渊目录工具；没有来源的执行语义保持 unknown。"""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.fossil_operations import (
    FOSSIL_NAMES,
    FossilItemState,
    FossilOperationResult,
    evaluate_fossil_operation,
)
from parsers.item_text import ItemParseError, parse_item
from tools.registry import REGISTRY, ToolResult

MAX_ITEM_TEXT_CHARS = 20_000
MAX_RENDERED_CANDIDATES = 12


def _state_from_item_text(text: str) -> FossilItemState:
    parsed = parse_item(text)
    reasons = []
    if parsed.flags.unidentified:
        reasons.append("物品未鉴定，显示状态不完整")
    if parsed.unclassified:
        reasons.append(f"{len(parsed.unclassified)} 行内容未归类")
    return FossilItemState(
        item_class=parsed.item_class,
        item_level=parsed.item_level,
        rarity=parsed.rarity.value,
        corrupted=parsed.flags.corrupted,
        mirrored=parsed.flags.mirrored,
        fractured=parsed.flags.fractured,
        incomplete_reason="；".join(reasons),
    )


def _fmt_multiplier(value: float) -> str:
    return f"x{value:g}"


def render_fossil_operation(result: FossilOperationResult) -> str:
    state = result.before
    lines = [
        f"### {result.fossil}的已知权重变换",
        "",
        "**结论：只能确认标签权重变换；完整化石操作仍未知**",
        f"- 输入：类别 {state.item_class or '未提供'}；物品等级 "
        f"{state.item_level if state.item_level is not None else '未提供'}；"
        f"稀有度 {state.rarity or '未提供'}",
        "",
        "| 标签 | 页面乘数 | 确定含义 |",
        "| --- | --- | --- |",
    ]
    labels = {"blocked": "明确屏蔽", "increased": "提高权重", "reduced": "降低权重"}
    for rule in result.weight_rules:
        lines.append(
            f"| {rule['tag']} | {_fmt_multiplier(rule['multiplier'])} | "
            f"{labels[rule['effect']]} |"
        )

    lines += ["", "| 检查 | 状态 | 说明 |", "| --- | --- | --- |"]
    status_zh = {"pass": "通过", "unknown": "未知"}
    for check in result.checks:
        lines.append(f"| {check.key} | {status_zh[check.status]} | {check.summary} |")

    pool = result.delve_pool
    if pool:
        level_note = (
            f"，已按物品等级 {pool['item_level_filter']} 排除 "
            f"{pool['excluded_by_item_level']} 条高等级记录"
            if pool["item_level_filter"] is not None else "，未按物品等级筛选"
        )
        lines += [
            "",
            f"地渊来源词缀目录：{pool['item_class']}，去重后 {pool['unique_candidates']} 条"
            f"（前缀 {pool['prefix_candidates']} / 后缀 {pool['suffix_candidates']}；"
            f"底层 {pool['source_rows']} 行{level_note}）。",
            "",
            "| 生成位 | 词缀名 | 词缀文本 | 物等 | 基础权重 | 命中本化石规则 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for candidate in pool["candidates"][:MAX_RENDERED_CANDIDATES]:
            matched = "、".join(
                f"{rule['tag']} {_fmt_multiplier(rule['multiplier'])}"
                for rule in candidate["matching_weight_rules"]
            ) or "无直接标签匹配"
            lines.append(
                f"| {candidate['position']} | {candidate['name_zh'] or '—'} | "
                f"{candidate['text']} | {candidate['item_level']} | "
                f"{candidate['base_weight'] or '无数据'} | {matched} |"
            )
        remaining = pool["unique_candidates"] - min(
            pool["unique_candidates"], MAX_RENDERED_CANDIDATES
        )
        if remaining:
            lines.append(
                f"（以上列前 {MAX_RENDERED_CANDIDATES} 条，另有 {remaining} 条保留在结构化结果中。）"
            )
        lines.append(f"> 目录边界：{pool['caveat']}。")

    if result.metamod_interactions:
        lines += ["", "已采集的 Craft of Exile 元词缀矩阵原值："]
        for cell in result.metamod_interactions:
            note = f"（{cell['note']}）" if cell.get("note") else ""
            lines.append(f"- {cell['guard']}：{cell['raw_value']}{note}")
    lines += ["", "仍未知：", *[f"- {item}" for item in result.unknown_fields]]
    lines += [
        "",
        f"> 标签乘数来源：[POEDB]({result.source_url})；抓取时间：{result.fetched_at}。",
        f"> 边界：{result.boundary}。",
        "> 元词缀矩阵原值只作并列披露，不自动解释成当前装备一定可执行。",
    ]
    return "\n".join(lines)


def _citations(result: FossilOperationResult, rendered: str) -> list[dict[str, Any]]:
    citations = [{
        "chunk_id": f"fossil_operation:{result.fossil}",
        "entity_id": None,
        "title": f"{result.fossil}标签权重变换",
        "url": result.source_url,
        "source": result.source,
        "source_priority": result.source_priority,
        "can_support_facts": True,
        "text": rendered,
        "channel": "structured",
    }]
    if result.delve_pool and result.delve_pool["candidates"]:
        candidate = result.delve_pool["candidates"][0]
        citations.append({
            "chunk_id": f"fossil_pool:{result.delve_pool['pages'][0]}",
            "entity_id": None,
            "title": f"{result.delve_pool['item_class']}地渊来源词缀目录",
            "url": candidate["url"],
            "source": "POEDB",
            "source_priority": 1,
            "can_support_facts": True,
            "text": result.delve_pool["caveat"],
            "channel": "structured",
        })
    return citations


@REGISTRY.register(
    name="fossil_operation_state",
    description=(
        "查某颗具名化石对词缀标签的确定性权重乘数，并可按底材类别列出"
        "category=delve 的地渊来源词缀目录。x0 可判定标签被屏蔽；其它乘数只逐标签披露。"
        "完整使用契约、共鸣器组合、最终成功率、稀有度变化与具体结果保持 unknown。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "fossil": {"type": "string", "enum": list(FOSSIL_NAMES)},
            "item_text": {"type": "string", "maxLength": MAX_ITEM_TEXT_CHARS},
            "item_class": {"type": "string", "minLength": 1, "maxLength": 80},
            "item_level": {"type": "integer", "minimum": 1, "maximum": 100},
            "rarity": {"type": "string", "enum": ["普通", "魔法", "稀有", "传奇"]},
            "corrupted": {"type": "boolean", "default": False},
            "mirrored": {"type": "boolean", "default": False},
            "fractured": {"type": "boolean", "default": False},
        },
        "required": ["fossil"],
        "additionalProperties": False,
    },
)
def fossil_operation_state(
    fossil: str,
    item_text: str | None = None,
    item_class: str = "",
    item_level: int | None = None,
    rarity: str | None = None,
    corrupted: bool = False,
    mirrored: bool = False,
    fractured: bool = False,
) -> ToolResult:
    if not (fossil or "").strip():
        return ToolResult(ok=False, error="fossil 不能为空")
    if item_text and any(value not in (None, "") for value in (item_class, item_level, rarity)):
        return ToolResult(ok=False, error="item_text 与手工 item_class/item_level/rarity 不能混用")
    if item_text and len(item_text) > MAX_ITEM_TEXT_CHARS:
        return ToolResult(ok=False, error=f"item_text 最多 {MAX_ITEM_TEXT_CHARS} 字符")
    if item_level is not None and (
        isinstance(item_level, bool) or not isinstance(item_level, int) or not 1 <= item_level <= 100
    ):
        return ToolResult(ok=False, error="item_level 必须是 1 到 100 的整数")

    try:
        state = _state_from_item_text(item_text) if item_text else FossilItemState(
            item_class=item_class,
            item_level=item_level,
            rarity=rarity,
            corrupted=bool(corrupted),
            mirrored=bool(mirrored),
            fractured=bool(fractured),
        )
    except ItemParseError as exc:
        return ToolResult(ok=False, error=f"装备文本解析失败：{exc.reason}")

    result = evaluate_fossil_operation(dbmod.get_conn(), fossil, state)
    if result is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "fossil": fossil,
                "supported_fossils": list(FOSSIL_NAMES),
                "boundary": "只接受权重表中 18 件化石的唯一精确中文名，不按相似名称猜语义",
            },
            citations=[],
            meta={"channel": "structured", "table": "poedb_tag_weight"},
        )
    rendered = render_fossil_operation(result)
    return ToolResult(
        ok=True,
        data={"found": True, **result.as_dict(), "answer": rendered},
        citations=_citations(result, rendered),
        meta={
            "channel": "structured",
            "table": "poedb_tag_weight+mod_affix+craft_metamod_interaction",
            "verdict": result.verdict,
        },
    )
