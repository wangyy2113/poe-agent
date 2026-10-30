"""Atlas 目标候选与多目标图连接；不把图节点数当游戏点数。"""

from __future__ import annotations

from dataclasses import asdict

from core import db as dbmod
from planner.atlas import plan_atlas, plan_atlas_route
from tools.registry import REGISTRY, ToolResult


@REGISTRY.register(
    name="atlas_candidates",
    description=(
        "按目标词从已入库 POEDB Atlas 原文查候选天赋及 root 连接路径。"
        "先查候选 ID，再以用户选择的 ID 调 atlas_route。路径新增节点数不是已核验的游戏点数；"
        "不推断收益、解锁条件或最优配点。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {"type": "string", "enum": ["刷图速度", "通货收益", "特定材料", "自定义"]},
            "focus": {"type": "string", "description": "特定材料或自定义目标需填 2–80 字"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        "required": ["goal"], "additionalProperties": False,
    },
)
def atlas_candidates(goal: str, focus: str = "", limit: int = 10) -> ToolResult:
    try:
        report = plan_atlas(dbmod.get_conn(), goal=goal, focus=focus, limit=limit)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    citations = []
    for item in report.candidates:
        text = (
            f"{item.name_zh} ({item.name_en}) v{item.tree_version}\n"
            + "\n".join(item.effects)
            + "\n原始图路径："
            + " → ".join(f"{node.name_zh or 'root'}[{node.node_id}]" for node in item.path)
            + f"\n路径中除 root 的图节点数：{item.path_allocatable_nodes}"
        )
        citations.append({
            "chunk_id": f"{item.entity_id}:atlas_route", "entity_id": item.entity_id,
            "title": item.name_zh, "url": item.data_url or item.source_url,
            "source": "POEDB", "source_priority": 1, "can_support_facts": True,
            "text": text, "channel": "atlas_candidates",
        })
    return ToolResult(ok=True, data=asdict(report), citations=citations)


@REGISTRY.register(
    name="atlas_route",
    description=(
        "把用户已选的 1–10 个 atlas_passive 实体 ID 连到 POEDB Atlas root；"
        "可提供用户明确的已分配节点 ID。采用逐目标最短新增图节点贪心路线，"
        "不能称为全局最优，也不把新增图节点数称为实际点数。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "entity_ids": {"type": "array", "items": {"type": "string"},
                           "minItems": 1, "maxItems": 10},
            "allocated_node_ids": {"type": "array", "items": {"type": "string"},
                                   "maxItems": 300},
        },
        "required": ["entity_ids"], "additionalProperties": False,
    },
)
def atlas_route(
    entity_ids: list[str], allocated_node_ids: list[str] | None = None,
) -> ToolResult:
    try:
        report = plan_atlas_route(
            dbmod.get_conn(), entity_ids=entity_ids,
            allocated_node_ids=allocated_node_ids,
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    citations = [{
        "chunk_id": f"{route['entity_id']}:atlas_route",
        "entity_id": route["entity_id"], "title": route["name_zh"],
        "url": route["graph_source_url"], "source": "POEDB", "source_priority": 1,
        "can_support_facts": True,
        "text": (
            f"{route['name_zh']} v{report['tree_version']}："
            + " → ".join(f"{node['name_zh'] or 'root'}[{node['node_id']}]"
                       for node in route["path"])
            + f"；新增图节点 {route['new_node_count']} 个"
        ),
        "channel": "atlas_route",
    } for route in report["routes"]]
    return ToolResult(ok=True, data=report, citations=citations)
