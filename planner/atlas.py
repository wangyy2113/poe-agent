"""Atlas 目标节点与原始连接路径；不推导未经核验的点数与收益。"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from typing import Literal


Goal = Literal["刷图速度", "通货收益", "特定材料", "自定义"]
DEFAULT_FOCUS = {"刷图速度": "速度", "通货收益": "通货"}


@dataclass(frozen=True)
class AtlasPathNode:
    node_id: str
    name_zh: str
    kind: str


@dataclass(frozen=True)
class AtlasCandidate:
    entity_id: str
    name_zh: str
    name_en: str
    effects: tuple[str, ...]
    matched_lines: tuple[str, ...]
    node_kind: str
    tree_version: str
    source_url: str
    data_url: str
    path: tuple[AtlasPathNode, ...]
    path_allocatable_nodes: int
    completion_check: str


@dataclass(frozen=True)
class AtlasPlan:
    goal: Goal
    focus: str
    status: Literal["candidates", "partial", "unknown"]
    tree_version: str = ""
    graph_updated_at: str = ""
    candidates: tuple[AtlasCandidate, ...] = ()
    missing: tuple[str, ...] = ()
    boundary: str = (
        "候选来自 POEDB Atlas 原文的字面匹配；路径只表示源 JSON 的连接，"
        "不等于最省点配点或最高收益。点数消耗、前置条件与实际收益尚未核验。"
    )


def _paths(conn: sqlite3.Connection) -> tuple[str, dict[str, AtlasPathNode], dict[str, str]]:
    rows = conn.execute(
        "SELECT node_id,name_zh,kind,passable,tree_version FROM atlas_graph_node"
    ).fetchall()
    if not rows:
        return "", {}, {}
    versions = {str(row["tree_version"]) for row in rows}
    if len(versions) != 1:
        return "", {}, {}
    version = next(iter(versions))
    nodes = {
        str(row["node_id"]): AtlasPathNode(str(row["node_id"]),
                                               str(row["name_zh"]), str(row["kind"]))
        for row in rows if row["passable"]
    }
    if "root" not in nodes:
        return version, nodes, {}
    adj: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT node_a,node_b FROM atlas_graph_edge WHERE tree_version=?", (version,)
    ):
        a, b = str(row["node_a"]), str(row["node_b"])
        if a in nodes and b in nodes:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
    parent = {"root": "root"}
    queue = deque(["root"])
    while queue:
        current = queue.popleft()
        for next_id in sorted(adj.get(current, ())):
            if next_id not in parent:
                parent[next_id] = current
                queue.append(next_id)
    return version, nodes, parent


def _graph_updated_at(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT max(updated_at) FROM atlas_graph_node").fetchone()
    return str(row[0] or "")


def _route(node_id: str, nodes: dict[str, AtlasPathNode],
           parent: dict[str, str]) -> tuple[AtlasPathNode, ...]:
    if node_id not in parent:
        return ()
    ids = [node_id]
    while ids[-1] != "root":
        ids.append(parent[ids[-1]])
    return tuple(nodes[key] for key in reversed(ids))


def plan_atlas(
    conn: sqlite3.Connection, *, goal: Goal, focus: str = "", limit: int = 10,
) -> AtlasPlan:
    """按显式目标词找原文节点，展示可核对连接；绝不按名字猜效果。"""
    if goal not in ("刷图速度", "通货收益", "特定材料", "自定义"):
        raise ValueError("不支持的 Atlas 目标")
    term = focus.strip() or DEFAULT_FOCUS.get(goal, "")
    if not 2 <= len(term) <= 80 or not 1 <= limit <= 30:
        raise ValueError("Atlas 目标词需为 2–80 字，结果上限为 1–30")
    version, nodes, parent = _paths(conn)
    candidates: list[AtlasCandidate] = []
    for row in conn.execute(
        "SELECT entity_id,name_zh,name_en,attributes,version,url FROM entity "
        "WHERE entity_type='atlas_passive' ORDER BY entity_id"
    ):
        attrs = json.loads(row["attributes"])
        effects = tuple(
            str(mod.get("text") or "") for mod in attrs.get("mods", [])
            if isinstance(mod, dict) and mod.get("kind") == "passive"
        )
        lines = tuple(line for line in effects if term.casefold() in line.casefold())
        if not lines and term.casefold() not in str(row["name_zh"]).casefold() and (
            term.casefold() not in str(row["name_en"] or "").casefold()
        ):
            continue
        node_paths = [
            _route(str(node_id), nodes, parent)
            for node_id in attrs.get("skill_ids", [])
            if version and str(row["version"] or "") == version
        ]
        connected = [path for path in node_paths if path]
        path = min(connected, key=lambda p: (len(p), tuple(n.node_id for n in p))) if connected else ()
        candidates.append(AtlasCandidate(
            entity_id=str(row["entity_id"]), name_zh=str(row["name_zh"]),
            name_en=str(row["name_en"] or ""), effects=effects,
            matched_lines=lines, node_kind=str(attrs.get("node_kind") or ""),
            tree_version=str(row["version"] or ""), source_url=str(row["url"]),
            data_url=str(attrs.get("data_url") or ""), path=path,
            path_allocatable_nodes=max(0, len(path) - 1) if path else 0,
            completion_check=(
                f"在版本 {row['version'] or '未知'} 的 Atlas 树中确认已分配"
                f"「{row['name_zh']}」对应节点"
            ),
        ))
    # 展示顺序：原文直接匹配的有名节点靠前；路径短只作展示排序，不表示推荐强度。
    order = {"keystone": 0, "notable": 1, "normal": 2, "mastery": 3}
    candidates.sort(key=lambda item: (
        not bool(item.matched_lines), order.get(item.node_kind, 4),
        not bool(item.path), len(item.path) if item.path else 9999, item.entity_id,
    ))
    missing: list[str] = []
    if not candidates:
        missing.append("当前 Atlas 实体中没有目标词的字面效果或名称匹配")
    if candidates and not any(item.path for item in candidates[:limit]):
        missing.append("Atlas 原始连接图缺失、版本不一致或目标节点不可从 root 连通")
    if candidates:
        missing.append("未验证每个节点的点数消耗、解锁条件与收益，不能给精确配点或利润")
    status = "unknown" if not candidates else "candidates" if any(
        item.path for item in candidates[:limit]
    ) else "partial"
    return AtlasPlan(goal=goal, focus=term, status=status, tree_version=version,
                     graph_updated_at=_graph_updated_at(conn),
                     candidates=tuple(candidates[:limit]), missing=tuple(missing))


def _shortest_from_selected(
    adj: dict[str, set[str]], selected: set[str], targets: set[str],
) -> tuple[str, ...]:
    """从已确认连接域寻找最短新增节点路径；不换算游戏点数。"""
    queue = deque(sorted(selected))
    parent = {node: node for node in selected}
    found = ""
    while queue:
        current = queue.popleft()
        if current in targets:
            found = current
            break
        for next_id in sorted(adj.get(current, ())):
            if next_id not in parent:
                parent[next_id] = current
                queue.append(next_id)
    if not found:
        return ()
    route = [found]
    while route[-1] not in selected:
        route.append(parent[route[-1]])
    return tuple(reversed(route))


def plan_atlas_route(
    conn: sqlite3.Connection, *, entity_ids: list[str],
    allocated_node_ids: list[str] | None = None,
) -> dict:
    """连接 1–10 个用户选定节点；贪心逐目标路径不冒充全局最优配点。"""
    if (
        not 1 <= len(entity_ids) <= 10
        or any(not isinstance(value, str) or not 1 <= len(value) <= 200
               for value in entity_ids)
        or len(set(entity_ids)) != len(entity_ids)
    ):
        raise ValueError("须选择 1–10 个互不重复的 Atlas 实体 ID")
    allocated = list(allocated_node_ids or [])
    if (
        len(allocated) > 300
        or any(not isinstance(value, str) or not 1 <= len(value) <= 64
               for value in allocated)
        or len(set(allocated)) != len(allocated)
    ):
        raise ValueError("已分配节点最多 300 个且不能重复")
    version, nodes, _ = _paths(conn)
    boundary = (
        "连接顺序按每一步最少新增图节点贪心选择，只是 POEDB 原始连接图上的候选路线；"
        "不保证多个目标的全局最优路线。新增节点数不是经游戏规则核验的 Atlas 点数。"
        "当前可用点数、前置解锁、收益和重置成本均未核验。"
    )
    if not version or "root" not in nodes:
        return {"status": "unknown", "tree_version": version,
                "graph_updated_at": _graph_updated_at(conn), "routes": [],
                "new_nodes": [], "new_node_count": 0,
                "unresolved": [{"reason": "Atlas 原始图缺失或版本混杂"}],
                "boundary": boundary}
    adj: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT node_a,node_b FROM atlas_graph_edge WHERE tree_version=?", (version,)
    ):
        a, b = str(row["node_a"]), str(row["node_b"])
        if a in nodes and b in nodes:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
    selected = {"root", *allocated}
    if not selected <= nodes.keys():
        raise ValueError("已分配节点含不在当前可连接 Atlas 图中的 ID")
    connected = {"root"}
    queue = deque(["root"])
    while queue:
        for node_id in adj.get(queue.popleft(), set()) & selected:
            if node_id not in connected:
                connected.add(node_id)
                queue.append(node_id)
    if connected != selected:
        return {"status": "unknown", "tree_version": version,
                "graph_updated_at": _graph_updated_at(conn), "routes": [],
                "new_nodes": [], "new_node_count": 0,
                "unresolved": [{"reason": "给定已分配节点不与 root 连通；无法确认起点"}],
                "boundary": boundary}
    pending: dict[str, tuple[sqlite3.Row, set[str]]] = {}
    unresolved: list[dict[str, str]] = []
    for entity_id in entity_ids:
        row = conn.execute(
            "SELECT entity_id,name_zh,version,url,attributes FROM entity "
            "WHERE entity_id=? AND entity_type='atlas_passive'", (entity_id,)
        ).fetchone()
        if row is None:
            unresolved.append({"entity_id": entity_id, "reason": "Atlas 实体未入库"})
            continue
        if str(row["version"] or "") != version:
            unresolved.append({"entity_id": entity_id, "reason": "实体与 Atlas 图版本不一致"})
            continue
        attrs = json.loads(row["attributes"])
        targets = {str(value) for value in attrs.get("skill_ids", [])} & nodes.keys()
        if not targets:
            unresolved.append({"entity_id": entity_id,
                               "reason": "实体没有当前图中可连接的位置"})
            continue
        pending[entity_id] = row, targets
    routes: list[dict] = []
    new_ids: list[str] = []
    while pending:
        options = [
            (len(path) - 1, entity_id, path)
            for entity_id, (_, targets) in pending.items()
            if (path := _shortest_from_selected(adj, selected, targets))
        ]
        if not options:
            unresolved.extend({"entity_id": entity_id, "reason": "目标从已选连接域不可达"}
                              for entity_id in sorted(pending))
            break
        _, entity_id, path = min(options)
        row, _ = pending.pop(entity_id)
        added = [node_id for node_id in path if node_id not in selected]
        selected.update(added)
        new_ids.extend(added)
        routes.append({
            "entity_id": entity_id, "name_zh": str(row["name_zh"]),
            "source_url": str(row["url"]),
            "graph_source_url": conn.execute(
                "SELECT source_url FROM atlas_graph_node WHERE node_id=?", (path[-1],)
            ).fetchone()[0],
            "path": [nodes[node_id].__dict__ for node_id in path],
            "new_node_ids": added, "new_node_count": len(added),
            "completion_check": f"在版本 {version} 的 Atlas 树上核对并分配「{row['name_zh']}」",
        })
    return {
        "status": "unknown" if not routes else "partial" if unresolved else "connected",
        "tree_version": version, "graph_updated_at": _graph_updated_at(conn),
        "routes": routes,
        "new_nodes": [nodes[node_id].__dict__ for node_id in new_ids],
        "new_node_count": len(new_ids), "unresolved": unresolved,
        "boundary": boundary,
    }
