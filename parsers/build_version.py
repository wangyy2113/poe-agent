"""PoB Build 与本地数据版本核对（需求 8.9）。"""

from __future__ import annotations

from dataclasses import dataclass
from sqlite3 import DatabaseError

from parsers.pob_build import PobBuild


@dataclass(frozen=True)
class BuildVersionCheck:
    build_version: str = ""
    system_version: str = ""
    known: bool = False
    matches: bool | None = None
    message: str = ""
    basis: str = ""


def _normalise(value: object) -> str:
    return str(value or "").strip().removeprefix("v").replace("_", ".")


def _system_version(conn) -> tuple[str, str]:  # noqa: ANN001
    """优先用天赋图版本；没有时退回 PoB 常量表的游戏版本。"""
    queries = (
        (
            "SELECT tree_version, COUNT(*) AS n FROM passive_graph_node "
            "WHERE tree_version <> '' GROUP BY tree_version ORDER BY n DESC LIMIT 1",
            "passive_graph_node.tree_version",
        ),
        (
            "SELECT game_version, COUNT(*) AS n FROM game_constant "
            "WHERE game_version <> '' GROUP BY game_version ORDER BY n DESC LIMIT 1",
            "game_constant.game_version",
        ),
    )
    for sql, basis in queries:
        try:
            row = conn.execute(sql).fetchone()
        except DatabaseError:
            continue
        if row and row[0]:
            return _normalise(row[0]), basis
    return "", ""


def check_build_version(conn, build: PobBuild) -> BuildVersionCheck:  # noqa: ANN001
    build_version = _normalise(build.tree_version)
    system_version, system_basis = _system_version(conn)
    if not build_version:
        return BuildVersionCheck(
            system_version=system_version,
            message=(
                "这份 Build 没有导出当前天赋树的 `treeVersion`，无法判断是否与本地数据同版。"
            ),
            basis="Build 版本应取当前 `<Tree activeSpec>` 对应 `<Spec treeVersion>`",
        )
    if not system_version:
        return BuildVersionCheck(
            build_version=build_version,
            message="本地数据库没有可用版本标记，无法与 Build 版本比对。",
            basis="优先读取 passive_graph_node.tree_version，其次读取 game_constant.game_version",
        )
    matches = build_version == system_version
    if matches:
        message = f"Build 天赋树版本 {build_version} 与本地数据版本一致。"
    else:
        message = (
            f"Build 天赋树版本 {build_version} 与本地数据版本 {system_version} 不一致；"
            "PlayerStat 仍是原 PoB 的快照，但本地宝石桥接、规则判读与优化建议可能存在版本偏差。"
        )
    return BuildVersionCheck(
        build_version=build_version,
        system_version=system_version,
        known=True,
        matches=matches,
        message=message,
        basis=(
            "Build 取当前激活 `<Spec treeVersion>`；本地版本取 " + system_basis
            + " 的占多数版本"
        ),
    )
