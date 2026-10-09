"""跨来源已确认冲突与待核对差异的只读查询工具。"""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from kb.conflicts import (
    ConflictItem,
    ConflictQueryError,
    ConflictSummary,
    lookup_conflicts,
    summarize_conflicts,
)
from orchestrator.citation import conflict_block
from tools.registry import REGISTRY, ToolResult


BOUNDARY = (
    "kind 以 _review 结尾的记录只是待核对差异，不是已确认冲突；默认仅返回未解决的 confirmed。"
    "查询结果只复述台账两侧原值、来源和审计状态，不自行裁决正确值，也不把 review 数量当作"
    "数据错误数量"
)
MAX_RENDER_ITEMS = 20
MAX_RENDER_VALUE = 300


def _short(value: str, maximum: int = MAX_RENDER_VALUE) -> str:
    normalized = " ".join(value.split()).replace("|", "\\|")
    return normalized if len(normalized) <= maximum else normalized[: maximum - 1] + "…"


def _citation(item: ConflictItem) -> dict[str, Any]:
    url = item.url_a or item.url_b or ""
    text = (
        f"审计状态：{item.status}；对象：{item.subject}；字段：{item.field}；"
        f"{item.source_a}={item.value_a}；{item.source_b}={item.value_b}；"
        f"kind={item.kind}；resolved={item.resolved}"
    )
    return {
        "chunk_id": f"source_conflict:{item.conflict_id}",
        "entity_id": f"source_conflict:{item.conflict_id}",
        "title": f"跨来源核对：{item.subject}",
        "url": url,
        "source": f"跨来源核对台账（{item.source_a} vs {item.source_b}）",
        "source_priority": 1,
        # review 只证明“台账存在待核对差异”，不能支撑“游戏数据确有冲突”的事实句。
        "can_support_facts": item.status == "confirmed",
        "text": text[:1800],
        "channel": "structured" if item.status == "confirmed" else "review",
    }


def _render_lookup(data: dict[str, Any]) -> str:
    items = list(data["items"])
    lines = ["### 跨来源差异查询", ""]
    if not items:
        lines.extend([
            "**没有匹配记录。**",
            f"- 当前过滤：status={data['status_filter']}；"
            f"resolution={data['resolution_filter']}",
            "", f"> 边界：{BOUNDARY}。",
        ])
        return "\n".join(lines)
    lines.extend([
        f"匹配 {data['total_matching']} 条，本页返回 {data['returned']} 条；"
        f"其中已确认 {data['confirmed_count']}、待核对 {data['review_count']}、"
        f"已解决 {data['resolved_count']}。",
        "",
    ])
    confirmed = [item for item in items if item["status"] == "confirmed"]
    review = [item for item in items if item["status"] == "review"]
    if confirmed:
        lines.extend([
            "#### 已确认冲突",
            "| ID | 对象 / 字段 | kind | 来源 A | 来源 B | 状态 |",
            "| ---: | --- | --- | --- | --- | --- |",
        ])
        for item in confirmed[:MAX_RENDER_ITEMS]:
            lines.append(
                f"| {item['conflict_id']} | {_short(item['subject'])} / "
                f"{_short(item['field'])} | {_short(item['kind'])} | "
                f"{_short(item['source_a'])}：{_short(item['value_a'])} | "
                f"{_short(item['source_b'])}：{_short(item['value_b'])} | "
                f"{'已解决' if item['resolved'] else '未解决'} |"
            )
        lines.append("")
    if review:
        lines.extend([
            "#### 待核对差异（不是已确认冲突）",
            "| ID | 对象 / 字段 | kind / cause | 来源 A | 来源 B |",
            "| ---: | --- | --- | --- | --- |",
        ])
        for item in review[:MAX_RENDER_ITEMS]:
            lines.append(
                f"| {item['conflict_id']} | {_short(item['subject'])} / "
                f"{_short(item['field'])} | {_short(item['kind'])} / "
                f"{_short(item.get('cause') or '未标注')} | "
                f"{_short(item['source_a'])}：{_short(item['value_a'])} | "
                f"{_short(item['source_b'])}：{_short(item['value_b'])} |"
            )
        lines.extend([
            "", "> 上述 review 只表示核对规则发现粒度、配对或原值差异；"
            "在人工/确定性规则确认前不得称为真实冲突。",
        ])
    if data["has_more"]:
        lines.extend(["", "- 还有后续页；使用响应中的 `next_cursor` 保持相同过滤条件继续读取。"])
    lines.extend(["", f"> 分类：{data['classification_rule']}。", f"> 边界：{BOUNDARY}。"])
    return "\n".join(lines)


def _render_summary(summary: ConflictSummary) -> str:
    lines = [
        "### 跨来源差异台账概览", "",
        f"- 总记录：{summary.total_count}；active {summary.active_count}；"
        f"resolved {summary.resolved_count}",
        f"- 已确认冲突：{summary.confirmed_count}（active "
        f"{summary.confirmed_active_count}）",
        f"- 待核对差异：{summary.review_count}（active {summary.review_active_count}）",
        "", "#### 按 kind",
        "| kind | 分类 | active | resolved | 总数 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for row in summary.kinds:
        lines.append(
            f"| {_short(row.kind)} | {'已确认' if row.status == 'confirmed' else '待核对'} | "
            f"{row.active_count} | {row.resolved_count} | {row.total_count} |"
        )
    if summary.source_pairs:
        lines.extend([
            "", "#### 按来源对",
            "| 来源 A | 来源 B | 已确认 | 待核对 | 总数 |",
            "| --- | --- | ---: | ---: | ---: |",
        ])
        for row in summary.source_pairs:
            lines.append(
                f"| {_short(row.source_a)} | {_short(row.source_b)} | "
                f"{row.confirmed_count} | {row.review_count} | {row.total_count} |"
            )
    if summary.review_causes:
        lines.extend(["", "#### Review 成因", "| cause | 条数 |", "| --- | ---: |"])
        for row in summary.review_causes:
            lines.append(f"| {_short(row.cause)} | {row.review_count} |")
    lines.extend([
        "", f"> 分类：{summary.classification_rule}。",
        f"> 用户出口：{summary.user_facing_rule}。", f"> 边界：{BOUNDARY}。",
    ])
    return "\n".join(lines)


@REGISTRY.register(
    name="source_conflict_lookup",
    description=(
        "查询跨来源台账。默认只返回未解决的已确认冲突；可显式切换 review/all，并按对象文本、"
        "kind、来源与解决状态过滤。review 永远单列为待核对，不能称为冲突。"
    ),
    parameters={
        "type": "object", "additionalProperties": False,
        "properties": {
            "query": {"type": "string", "maxLength": 160, "default": ""},
            "status": {
                "type": "string", "enum": ["confirmed", "review", "all"],
                "default": "confirmed",
            },
            "resolution": {
                "type": "string", "enum": ["active", "resolved", "all"],
                "default": "active",
            },
            "kinds": {
                "type": "array", "maxItems": 20, "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 80},
                "default": [],
            },
            "sources": {
                "type": "array", "maxItems": 20, "uniqueItems": True,
                "items": {"type": "string", "minLength": 1, "maxLength": 80},
                "default": [],
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "cursor": {"type": ["string", "null"], "maxLength": 500},
        },
    },
)
def source_conflict_lookup(
    query: str = "", status: str = "confirmed", resolution: str = "active",
    kinds: list[str] | None = None, sources: list[str] | None = None,
    limit: int = 20, cursor: str | None = None,
) -> ToolResult:
    try:
        result = lookup_conflicts(
            dbmod.get_conn(), query=query, status=status, resolution=resolution,
            kinds=kinds, sources=sources, limit=limit, cursor=cursor,
        )
    except ConflictQueryError as exc:
        return ToolResult(ok=False, error=str(exc))
    data = result.as_dict()
    data.update({
        "found": bool(result.items), "boundary": BOUNDARY,
        "answer": _render_lookup(data),
    })
    confirmed = [as_item for as_item in result.items if as_item.status == "confirmed"]
    if confirmed:
        data["conflict_block"] = conflict_block([
            {
                "subject": item.subject, "source_a": item.source_a,
                "value_a": item.value_a, "source_b": item.source_b,
                "value_b": item.value_b,
            }
            for item in confirmed
        ])
    if result.review_count:
        data["review_notice"] = (
            f"本页 {result.review_count} 条 _review 记录仅为待核对差异，不是已确认冲突"
        )
    return ToolResult(
        ok=True, data=data, citations=[_citation(item) for item in result.items],
        meta={
            "channel": "structured", "table": "source_conflict",
            "confirmed": result.confirmed_count, "review": result.review_count,
        },
    )


@REGISTRY.register(
    name="source_conflict_summary",
    description=(
        "汇总跨来源台账的 confirmed/review、active/resolved、kind、来源对与 review cause；"
        "不会把待核对数量冒充已确认冲突数量。"
    ),
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)
def source_conflict_summary() -> ToolResult:
    summary = summarize_conflicts(dbmod.get_conn())
    data = summary.as_dict()
    data.update({"boundary": BOUNDARY, "answer": _render_summary(summary)})
    return ToolResult(ok=True, data=data, meta={
        "channel": "structured", "table": "source_conflict",
        "confirmed": summary.confirmed_count, "review": summary.review_count,
    })
