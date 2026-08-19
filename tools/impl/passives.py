"""天赋树路径：点到某个节点要花几点、怎么走。

## 为什么这是「算」而不是「猜」

答案是**在已入库的图上做 BFS**，而且把整条路径逐节点列出来当证据——用户可以照着核对，
而不是接受一个不知从哪来的数字。这与「经验推断」的区别就在这里：
数字有可复核的来源（`passive_graph_edge` 的 3382 条边），不是模型的记忆。

## 三条排除都是游戏规则

`passable=0` 的节点不能出现在路径中间（判据写在 `core/db.py` 的表注释里）：

- **root**：7 个职业起点都连在它上面，但游戏里不能从一个职业穿到另一个职业。
  不排除它，「野蛮人 → 女巫」会算成 2 点，实测真值是 22。
- **升华节点**：只能从升华起点进入，不在主树连通域里。
- **大师天赋**：点了之后选一个效果，它不作为通路。

珠宝插槽**可以**经过——游戏里它就是一个能点的普通节点。

## 同名节点取最近的一处

「力量」「生命」这类小点在树上有几十处，`神威之盾` 这类基石只有一处。同名多处时
取**最近的一处**并写出「同名节点共 N 处」——取最远或随便取一处都会给出错的点数。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from kb.sources.poedb_tree import build_adjacency, nearest_path
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 2400
#: 路径里最多逐个列几个节点。超出时写出还剩多少——静默截断等同于漏答。
MAX_PATH_NODES = 40

#: 不可经过的节点为什么走不到，逐类说清。含糊地说「查不到」会让用户以为数据缺失。
_UNREACHABLE_REASON = {
    "mastery": "大师天赋不是「从起点走过去」的节点：它要先点到同组的相邻节点，"
               "再从它提供的若干效果里选一个",
    "ascendancy": "升华节点只能从升华起点进入，不在主天赋树的连通域里"
                  "（升华点与天赋点也是两套点数）",
    "ascendancy_notable": "升华节点只能从升华起点进入，不在主天赋树的连通域里",
}


def _load_graph(conn: sqlite3.Connection) -> tuple[set[tuple[str, str]], set[str]]:
    edges = {
        (str(r["node_a"]), str(r["node_b"]))
        for r in conn.execute("SELECT node_a,node_b FROM passive_graph_edge")
    }
    passable = {
        str(r["node_id"])
        for r in conn.execute("SELECT node_id FROM passive_graph_node WHERE passable=1")
    }
    return edges, passable


def _find_targets(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """按名字找节点。专名优先（基石 / 核心天赋），其次小点。"""
    return conn.execute(
        """
        SELECT * FROM passive_graph_node
        WHERE name_zh = ? OR name_en = ? COLLATE NOCASE OR name_zh LIKE ?
        ORDER BY (name_zh = ?) DESC,
                 CASE kind WHEN 'keystone' THEN 0 WHEN 'notable' THEN 1 ELSE 2 END,
                 node_id
        """,
        (name, name, f"%{name}%", name),
    ).fetchall()


def _node_names(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    return {
        str(r["node_id"]): (str(r["name_zh"]) or "（无名小点）")
        for r in conn.execute(
            f"SELECT node_id,name_zh FROM passive_graph_node "  # noqa: S608 已参数化
            f"WHERE node_id IN ({placeholders})",
            ids,
        )
    }


def _render(
    conn: sqlite3.Connection,
    target: sqlite3.Row,
    per_class: dict[str, tuple[int, list[str]]],
    same_name: int,
    total_points: float | None,
    url: str,
) -> str:
    kind_zh = {"keystone": "基石", "notable": "核心天赋", "jewel_socket": "珠宝插槽",
               "normal": "普通天赋"}.get(str(target["kind"]), str(target["kind"]))
    out = [f"### 点到「{target['name_zh']}」（{kind_zh}）要花几个天赋点", ""]
    if same_name > 1:
        out.append(f"树上同名节点有 {same_name} 处，以下按**最近的一处**计算。")
        out.append("")
    out.append("| 起始职业 | 需要天赋点 |")
    out.append("| --- | --- |")
    for cls, (cost, _path) in sorted(per_class.items(), key=lambda kv: kv[1][0]):
        out.append(f"| {cls} | {cost} |")
    if total_points:
        out.append("")
        out.append(f"角色满级（100 级）加任务奖励共 {total_points:g} 个天赋点。")

    cheapest = min(per_class.items(), key=lambda kv: kv[1][0])
    cls, (cost, path) = cheapest
    names = _node_names(conn, path)
    shown = path[:MAX_PATH_NODES]
    # 起点用**职业名**，不用那个节点自己的名字：JSON 里贵族与暗影的起点节点叫
    # 「七」「六」（占位名），照搬出去用户看不懂。职业名取自顶层 classes 数组。
    labels = [cls] + [names.get(n, n) for n in shown[1:]]
    out.append("")
    out.append(f"**最省的一条（{cls}，{cost} 点）**：")
    out.append(" → ".join(labels))
    if len(path) > MAX_PATH_NODES:
        out.append(f"（路径共 {len(path)} 个节点，以上仅列前 {MAX_PATH_NODES} 个）")
    out.append("")
    out.append(
        "点数 = 路径上除出生点以外的节点数。路径**不经过**升华节点与大师天赋"
        "（它们不是通路），也不穿过树心（游戏里不能从一个职业穿到另一个职业）。"
    )
    out.append("")
    out.append(f"来源：POEDB 天赋树数据 {url}")
    return "\n".join(out)


@REGISTRY.register(
    name="passive_path",
    description=(
        "算**点到某个天赋节点需要几个天赋点**，并给出逐节点的路径。"
        "问「从起点点到 XX 要花几点 / 怎么走到 XX / XX 要几个天赋点」时用它。\n"
        "点数按职业分别给（7 个职业起点位置不同，差别很大）。"
        "升华节点与大师天赋**算不了**——它们不是「从起点走过去」的节点，"
        "工具会说明原因而不是给一个数字。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "node_name": {
                "type": "string",
                "description": "目标节点名，中文或英文，如「神威之盾」「坚毅之心」",
            },
            "class_name": {
                "type": "string",
                "description": "可选，只看某个职业的起点，如「野蛮人」「女巫」。不填则七个职业都给",
            },
        },
        "required": ["node_name"],
    },
)
def passive_path(node_name: str, class_name: str | None = None) -> ToolResult:
    conn = dbmod.get_conn()
    name = (node_name or "").strip()
    if not name:
        return ToolResult(ok=False, error="node_name 不能为空")

    starts = {
        str(r["class_start"]): str(r["node_id"])
        for r in conn.execute(
            "SELECT class_start,node_id FROM passive_graph_node WHERE class_start <> ''"
        )
    }
    if not starts:
        return ToolResult(
            ok=True,
            data={"found": False, "reason": "天赋树图结构尚未入库，无法计算路径"},
        )
    if class_name:
        wanted = class_name.strip()
        starts = {k: v for k, v in starts.items() if wanted in k}
        if not starts:
            return ToolResult(
                ok=True,
                data={"found": False, "reason": f"没有名为「{class_name}」的职业",
                      "known_classes": sorted(starts)},
            )

    candidates = _find_targets(conn, name)
    if not candidates:
        return ToolResult(
            ok=True,
            data={"found": False,
                  "reason": f"天赋树上没有名为「{name}」的节点",
                  "hint": "确认是天赋节点名而不是装备词缀或技能名；星团珠宝上的天赋不在主树上"},
        )

    target = candidates[0]
    reachable = [row for row in candidates if row["passable"]]
    if not reachable:
        kind = str(target["kind"])
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": _UNREACHABLE_REASON.get(
                    kind, f"该节点（{kind}）不在主天赋树的连通域里，算不出从起点走过去的点数"
                ),
                "node_kind": kind,
            },
        )

    edges, passable = _load_graph(conn)
    adj = build_adjacency(edges, passable)
    # 每个职业**一次** BFS，在里面找最近的同名位置。逐位置各跑一次的话，
    # 「力量」（73 处）单次查询要 327ms（实测）。
    targets = {str(row["node_id"]) for row in reachable}
    per_class: dict[str, tuple[int, list[str]]] = {}
    for cls, src in starts.items():
        path = nearest_path(adj, src, targets)
        if path is not None:
            per_class[cls] = (len(path) - 1, path)
    if not per_class:
        # 说清是「走不到」而不是「查不到」。**优先给精确理由**：
        # 2026-08-19 第十八轮起星团珠宝清单入库，所以这里不再说「可能」——
        # 能查到就直接说它是星团珠宝上的核心天赋/基石。
        from tools.impl.cluster import cluster_notable_row  # noqa: PLC0415  避免导入环

        cluster = cluster_notable_row(conn, str(target["name_zh"]))
        if cluster is not None:
            kind_zh = "基石" if cluster["is_keystone"] else "核心天赋"
            reason = (
                f"「{target['name_zh']}」是**星团珠宝上的{kind_zh}**，不在主天赋树上，"
                "所以没有「从起点走过去要几点」这个数字——它要靠星团珠宝提供，"
                "而珠宝插在树上的珠宝插槽里。详情用 cluster_jewel_info 查"
            )
        else:
            reason = (
                f"「{target['name_zh']}」在主天赋树上不可达。它不在星团珠宝清单里，"
                "多半属于升华子树（那要从升华起点进入，与天赋点是两套点数）"
            )
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": reason,
                "node_kind": str(target["kind"]),
                "cluster_only": cluster is not None,
            },
        )

    total = conn.execute(
        "SELECT value_num FROM passive_tree_meta WHERE key='totalPoints'"
    ).fetchone()
    url = str(target["url"])
    text = _render(
        conn, target, per_class, len(reachable),
        float(total["value_num"]) if total else None, url,
    )
    priority = int(target["source_priority"])
    cheapest_cls, (cheapest_cost, _) = min(per_class.items(), key=lambda kv: kv[1][0])
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "node": str(target["name_zh"]),
            "node_kind": str(target["kind"]),
            "same_name_positions": len(reachable),
            "cost_by_class": {c: v[0] for c, v in per_class.items()},
            "cheapest": {"class": cheapest_cls, "points": cheapest_cost},
            "boundary": (
                "点数是在已入库的天赋树连线上算的最短路径；"
                "不含升华点、不考虑星团珠宝，也不判断这条路径值不值得点"
            ),
        },
        citations=[{
            "chunk_id": f"passive_path:{target['node_id']}",
            "entity_id": None,
            "title": f"点到「{target['name_zh']}」的天赋点消耗",
            "url": url,
            "source": str(target["source"]),
            "source_priority": priority,
            "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
            "text": text[:MAX_EVIDENCE_CHARS],
            "channel": "structured",
        }],
        meta={"channel": "structured", "table": "passive_graph_edge"},
    )
