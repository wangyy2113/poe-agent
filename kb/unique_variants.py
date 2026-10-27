"""隔离的 PoB 唯一装备历史变体查询。

这里只读 pob_unique_variant。普通装备检索继续只看当前卡片；历史值必须由
明确的逐变体查询取得，不能悄悄进入 alias、chunk 或 FTS。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from typing import Any


class VariantQueryError(ValueError):
    """名称、变体下标或快照不满足无歧义查询。"""


_PRE_LABEL = re.compile(r"\bPre\s+(\d+\.\d+(?:\.\d+)?)", re.I)


def _uid(conn: sqlite3.Connection, name: str) -> str:
    name = name.strip()
    if not name:
        raise VariantQueryError("必须给出唯一装备名称或 UID")
    rows = conn.execute(
        "SELECT DISTINCT p.uid FROM pob_unique p WHERE p.uid=? "
        "OR p.name_en=? COLLATE NOCASE OR EXISTS ("
        "SELECT 1 FROM entity e WHERE e.entity_type='unique_item' "
        "AND e.name_en=p.name_en COLLATE NOCASE AND e.name_zh=?) "
        "ORDER BY p.uid LIMIT 3", (name, name, name),
    ).fetchall()
    if not rows:
        raise VariantQueryError(f"PoB 当前库里找不到唯一装备「{name}」")
    if len(rows) != 1:
        raise VariantQueryError(f"「{name}」对应多个 PoB 装备，请改用完整 UID")
    return str(rows[0][0])


def _label(name: str) -> str:
    match = _PRE_LABEL.search(name)
    return f"{match.group(1)} 之前" if match else "当前选择型变体"


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "index": row["variant_index"],
        "name": row["variant_name"],
        "scope": row["scope"],
        "label_meaning": _label(row["variant_name"]),
        "base_type_en": row["base_type_en"],
        "mods": json.loads(row["mods"]),
        "source_file": row["source_file"],
        "source": row["source"],
        "source_priority": row["source_priority"],
        "url": row["url"],
        "source_commit": row["source_commit"],
        "pob_version": row["pob_version"],
        "game_version": row["game_version"],
    }


def _snapshot(conn: sqlite3.Connection, name: str) -> tuple[sqlite3.Row, list[dict[str, Any]]]:
    uid = _uid(conn, name)
    item = conn.execute("SELECT * FROM pob_unique WHERE uid=?", (uid,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM pob_unique_variant WHERE unique_uid=? ORDER BY variant_index", (uid,)
    ).fetchall()
    if not rows:
        raise VariantQueryError(
            "这件装备没有已归档的命名变体；请先运行 PoB 唯一装备新版导入"
        )
    variants = [_row(row) for row in rows]
    expected = json.loads(item["variant_names"] or "[]")
    if [v["name"] for v in variants] != expected:
        raise VariantQueryError("当前装备与历史变体快照不一致，请重新完整导入")
    if len({v["source_commit"] for v in variants}) != 1:
        raise VariantQueryError("历史变体来源 commit 不一致，请重新完整导入")
    if any(v["source_file"] != item["source_file"] for v in variants):
        raise VariantQueryError("当前装备与历史变体来源文件不一致，请重新完整导入")
    if any(v["url"] != item["url"] for v in variants):
        raise VariantQueryError("当前装备与历史变体来源版本不一致，请重新完整导入")
    return item, variants


def list_variants(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    item, variants = _snapshot(conn, name)
    return {
        "uid": item["uid"], "name_en": item["name_en"],
        "base_type_current": item["base_type_en"],
        "source_commit": variants[0]["source_commit"],
        "source_url": variants[0]["url"],
        "source": variants[0]["source"],
        "source_priority": variants[0]["source_priority"],
        "variants": [
            {k: v[k] for k in ("index", "name", "scope", "label_meaning", "base_type_en")}
            for v in variants
        ],
        "note": "Pre X 表示 X 之前，不是 X 的精确快照；多个当前变体须明确选择。",
    }


def variant_detail(conn: sqlite3.Connection, name: str, index: int) -> dict[str, Any]:
    item, variants = _snapshot(conn, name)
    found = next((v for v in variants if v["index"] == index), None)
    if found is None:
        raise VariantQueryError(f"没有编号 {index} 的变体；可用编号：{[v['index'] for v in variants]}")
    return {"uid": item["uid"], "name_en": item["name_en"], "variant": found,
            "note": "Pre X 表示 X 之前，不是 X 的精确快照。"}


def _mod_key(mod: dict[str, Any]) -> str:
    # variants 是原始适用索引，不是词缀文本的一部分；共同词缀不能因此误判为变更。
    return json.dumps({k: v for k, v in mod.items() if k != "variants"},
                      ensure_ascii=False, sort_keys=True)


def compare_variants(
    conn: sqlite3.Connection, name: str, left_index: int, right_index: int,
) -> dict[str, Any]:
    if left_index == right_index:
        raise VariantQueryError("对比需要两个不同变体编号")
    item, variants = _snapshot(conn, name)
    by_index = {v["index"]: v for v in variants}
    if left_index not in by_index or right_index not in by_index:
        raise VariantQueryError(f"变体编号不存在；可用编号：{list(by_index)}")
    left, right = by_index[left_index], by_index[right_index]
    a = Counter(_mod_key(mod) for mod in left["mods"])
    b = Counter(_mod_key(mod) for mod in right["mods"])
    common = a & b
    removed = a - b
    added = b - a
    return {
        "uid": item["uid"], "name_en": item["name_en"],
        "left": left, "right": right,
        "shared_mods": [json.loads(key) for key in common.elements()],
        "removed_mods": [json.loads(key) for key in removed.elements()],
        "added_mods": [json.loads(key) for key in added.elements()],
        "base_changed": left["base_type_en"] != right["base_type_en"]
                        if left["base_type_en"] and right["base_type_en"] else None,
        "note": "词缀差异仅比较指定的两个 PoB 变体；未知底材不判定是否改变。"
                "Pre X 表示 X 之前，不是 X 的精确快照。",
    }
