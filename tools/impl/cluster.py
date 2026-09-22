"""星团珠宝尺寸、小天赋类型与可出现核心天赋查询。

两个 PoB 自动生成数据文件在这里互补：``Data/ClusterJewels.lua`` 给出尺寸与小天赋
类型 tag；``Data/ModJewelCluster.lua`` 的核心天赋词缀给出同一 tag 下的正权重与物等
门槛。因此可以精确回答双向映射。权重只按原值披露，不换算成单次概率。
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 6000
#: 小天赋类型最多列几条。超出写出还剩多少——静默截断等同于漏答。
MAX_SKILL_ROWS = 12
MAX_POOL_ROWS = 24

_SIZE_ZH = {"Small": "小型", "Medium": "中型", "Large": "大型"}
_SIZE_ALIASES = {
    "Small": ("小型", "小星团"),
    "Medium": ("中型", "中星团"),
    "Large": ("大型", "大星团"),
}

_BOUNDARY = (
    "正权重只证明该核心天赋在该小天赋类型的当前 PoB 基础池中可出现；"
    "基础生成权重不是单次概率，未模拟其它词缀、核心天赋占位约束、制作方法、掉落或价格"
)

_NOTABLE_PREFIX = "1 Added Passive Skill is "
_ITEM_LEVEL_RE = re.compile(
    r"(?:物等|物品等级|item\s*level|ilvl)\s*[:：]?\s*(\d{1,3})", re.IGNORECASE
)
_BLOB_COMMIT_RE = re.compile(r"/blob/([0-9a-f]{40})/")


def _size_of(query: str) -> str | None:
    for size, aliases in _SIZE_ALIASES.items():
        if any(alias in query for alias in aliases):
            return size
    for size in _SIZE_ZH:
        if re.search(rf"\b{size}\b", query, re.IGNORECASE):
            return size
    return None


def cluster_notable_row(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """按中文名或英文名查星团核心天赋。`passive_path` 也用它来给出精确理由。"""
    return conn.execute(
        "SELECT * FROM pob_cluster_notable WHERE name_zh = ? OR name_en = ? COLLATE NOCASE",
        (name, name),
    ).fetchone()


def _skill_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """小天赋类型连同天赋树实体中可审计的中文同名。"""
    aliases: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT name_en,name_zh FROM entity WHERE entity_type IN ('passive','ascendancy') "
        "AND name_en IS NOT NULL AND name_en<>'' AND name_zh<>''"
    ):
        aliases.setdefault(str(row["name_en"]), set()).add(str(row["name_zh"]))
    return [
        {**dict(row), "names_zh": sorted(aliases.get(str(row["name_en"]), set()))}
        for row in conn.execute("SELECT * FROM pob_cluster_skill ORDER BY size,name_en")
    ]


def _contained(value: str, query: str) -> bool:
    return bool(value) and value.casefold() in query.casefold()


def _resolve_skill_type(
    rows: list[dict[str, Any]], query: str, size: str | None
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    candidates = [row for row in rows if size is None or row["size"] == size]
    exact = [
        row for row in candidates
        if query.casefold() in {
            str(row["tag"]).casefold(), str(row["weight_tag"]).casefold(),
            str(row["name_en"]).casefold(),
        }
        or query in row["names_zh"]
    ]
    if len(exact) == 1:
        return exact[0], []
    contained = [
        row for row in candidates
        if _contained(str(row["name_en"]), query)
        or any(_contained(name, query) for name in row["names_zh"])
    ]
    if contained:
        # 最长命中优先，避免「持续伤害」抢走「持续混沌伤害加成」。
        score = lambda row: max(  # noqa: E731
            [len(str(row["name_en"]))] + [len(name) for name in row["names_zh"]]
        )
        longest = max(score(row) for row in contained)
        best = [row for row in contained if score(row) == longest]
        if len(best) == 1:
            return best[0], []
        return None, best
    return None, []


def _notable_from_query(conn: sqlite3.Connection, query: str) -> sqlite3.Row | None:
    direct = cluster_notable_row(conn, query)
    if direct is not None:
        return direct
    rows = conn.execute(
        "SELECT * FROM pob_cluster_notable WHERE "
        "(name_zh IS NOT NULL AND instr(?,name_zh)>0) OR instr(lower(?),lower(name_en))>0 "
        "ORDER BY max(length(COALESCE(name_zh,'')),length(name_en)) DESC",
        (query, query),
    ).fetchall()
    if not rows:
        return None
    best_len = max(len(str(rows[0]["name_zh"] or "")), len(str(rows[0]["name_en"])))
    best = [
        row for row in rows
        if max(len(str(row["name_zh"] or "")), len(str(row["name_en"]))) == best_len
    ]
    return best[0] if len(best) == 1 else None


def _notable_mods(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT mod_id,affix_type,ilvl,text_en,weights,weight_mults,source,"
        "source_priority,url,pob_version FROM pob_mod WHERE mod_kind='jewel_cluster' "
        "AND text_en LIKE '1 Added Passive Skill is %' ORDER BY ilvl,text_en,mod_id"
    ).fetchall()
    names = {
        str(row["name_en"]): dict(row)
        for row in conn.execute("SELECT * FROM pob_cluster_notable")
    }
    out = []
    for row in rows:
        text = str(row["text_en"])
        if not text.startswith(_NOTABLE_PREFIX):
            continue
        weights = json.loads(row["weights"] or "{}")
        if not isinstance(weights, dict):
            continue
        name_en = text[len(_NOTABLE_PREFIX):].strip()
        out.append({
            **dict(row),
            "name_en": name_en,
            "name_zh": names.get(name_en, {}).get("name_zh"),
            "weights_parsed": weights,
            "source_commit": (
                match.group(1) if (match := _BLOB_COMMIT_RE.search(str(row["url"]))) else ""
            ),
        })
    return out


def _pool_for_skill(
    mods: list[dict[str, Any]], tag: str, item_level: int | None = None
) -> tuple[list[dict[str, Any]], int]:
    pool = []
    excluded = 0
    for mod in mods:
        weight = mod["weights_parsed"].get(tag, 0)
        if not isinstance(weight, (int, float)) or weight <= 0:
            continue
        required = int(mod["ilvl"] or 1)
        if item_level is not None and required > item_level:
            excluded += 1
            continue
        pool.append({
            "mod_id": mod["mod_id"],
            "name_en": mod["name_en"],
            "name_zh": mod["name_zh"],
            "affix_type": mod["affix_type"],
            "required_item_level": required,
            "base_weight": int(weight),
            "weight_multipliers": json.loads(mod["weight_mults"] or "{}"),
            "source_url": mod["url"],
        })
    pool.sort(key=lambda row: (-row["base_weight"], row["required_item_level"], row["name_en"]))
    return pool, excluded


def _skill_label(skill: dict[str, Any]) -> str:
    zh = " / ".join(skill["names_zh"])
    return f"{zh}（{skill['name_en']}）" if zh else str(skill["name_en"])


def _render_pool(
    skill: dict[str, Any], pool: list[dict[str, Any]], item_level: int | None, excluded: int
) -> str:
    size = _SIZE_ZH.get(str(skill["size"]), str(skill["size"]))
    level = f"，物品等级 {item_level}" if item_level is not None else ""
    out = [f"### {size}星团珠宝 · {_skill_label(skill)}{level}", ""]
    if not pool:
        out.append("当前 PoB 数据里，这个小天赋类型没有正权重的核心天赋词缀。")
    else:
        out += [
            f"可出现的核心天赋共 **{len(pool)}** 个"
            + (f"；另有 {excluded} 个因物品等级不足被排除" if excluded else "") + "。",
            "",
            "| 核心天赋 | 英文名 | 词缀位 | 要求物等 | 基础生成权重 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in pool[:MAX_POOL_ROWS]:
            slot = "前缀" if row["affix_type"] == "Prefix" else "后缀"
            out.append(
                f"| {row['name_zh'] or '—'} | {row['name_en']} | {slot} | "
                f"{row['required_item_level']} | {row['base_weight']} |"
            )
        if len(pool) > MAX_POOL_ROWS:
            out += ["", f"（以上列出前 {MAX_POOL_ROWS} 个；完整 {len(pool)} 个保留在结构化结果中。）"]
    out += [
        "",
        f"> 数据连接：`ClusterJewels.lua` 的 `{skill['weight_tag']}` → "
        "`ModJewelCluster.lua` 同名权重键；只收录权重大于 0 的条目。",
        f"> 边界：{_BOUNDARY}。",
    ]
    return "\n".join(out)


def _reverse_pools(
    skills: list[dict[str, Any]], mods: list[dict[str, Any]], name_en: str
) -> list[dict[str, Any]]:
    matching_mods = [mod for mod in mods if mod["name_en"] == name_en]
    out = []
    for skill in skills:
        weights = [
            int(mod["weights_parsed"].get(skill["weight_tag"], 0)) for mod in matching_mods
            if isinstance(mod["weights_parsed"].get(skill["weight_tag"], 0), (int, float))
            and mod["weights_parsed"].get(skill["weight_tag"], 0) > 0
        ]
        if weights:
            out.append({
                "size": skill["size"], "tag": skill["tag"],
                "weight_tag": skill["weight_tag"], "name_en": skill["name_en"],
                "names_zh": skill["names_zh"], "base_weight": max(weights),
            })
    return out


def _render_sizes(rows: list[sqlite3.Row], skills: dict[str, list[dict[str, Any]]]) -> str:
    out = ["### 星团珠宝的尺寸与可加小天赋数", ""]
    out.append("| 尺寸 | 可加小天赋 | 核心天赋位 | 珠宝插槽位 | 小点类型数 |")
    out.append("| --- | --- | --- | --- | --- |")
    for row in rows:
        size = str(row["size"])
        out.append(
            f"| {_SIZE_ZH.get(size, size)}（{size}） | {row['min_nodes']}–{row['max_nodes']} | "
            f"{row['notable_slots']} | {row['socket_slots']} | {len(skills.get(size, []))} |"
        )
    out.append("")
    # 「星团珠宝上的天赋要花几个天赋点」问的就是这件事：这些天赋**不在主天赋树上**，
    # 所以没有「从起点走过去要几点」这个数字。不写这句，答案就答不到问题上
    # （实测录制里模型只复述了尺寸表）。
    out.append(
        "星团珠宝上的核心天赋与基石**不在主天赋树上**，因此没有「从起点走过去要几个"
        "天赋点」这个数字：它们靠星团珠宝提供，而珠宝要插在天赋树的珠宝插槽里"
        "（那个插槽本身要几点，可以用 passive_path 算）。"
    )
    out.append("")
    out.append(_BOUNDARY + "。")
    out.append("")
    out.append(f"来源：Path of Building {rows[0]['pob_url']}")
    return "\n".join(out)


def _render_skills(size: str, rows: list[dict[str, Any]], size_row: sqlite3.Row | None) -> str:
    label = _SIZE_ZH.get(size, size)
    out = [f"### {label}星团珠宝可选的小天赋类型（{len(rows)} 种）", ""]
    if size_row is not None:
        out.append(
            f"{label}星团珠宝可加 {size_row['min_nodes']}–{size_row['max_nodes']} 个小天赋，"
            f"有 {size_row['notable_slots']} 个核心天赋位、{size_row['socket_slots']} 个珠宝插槽位。"
        )
        out.append("")
    out.append("| 类型 | PoB 英文名 | 小天赋给予（英文原文） |")
    out.append("| --- | --- | --- |")
    for row in rows[:MAX_SKILL_ROWS]:
        stats = "；".join(json.loads(row["stats_en"] or "[]")) or "—"
        out.append(f"| {' / '.join(row['names_zh']) or '—'} | {row['name_en']} | {stats} |")
    if len(rows) > MAX_SKILL_ROWS:
        out.append("")
        out.append(f"（共 {len(rows)} 种，以上列出前 {MAX_SKILL_ROWS} 种）")
    out.append("")
    out.append(
        "中文类型名来自 POEDB 天赋树中的同名节点桥接；小天赋效果文本仍按 PoB 英文原文给出，"
        "不自行翻译。"
    )
    out.append("")
    out.append(_BOUNDARY + "。")
    return "\n".join(out)


def _render_keystones(rows: list[sqlite3.Row]) -> str:
    out = [f"### 只能从星团珠宝获得的基石（{len(rows)} 个）", ""]
    out.append("| 中文名 | 英文名 |")
    out.append("| --- | --- |")
    for row in rows:
        out.append(f"| {row['name_zh'] or '—'} | {row['name_en']} |")
    out.append("")
    out.append("这些基石**不在主天赋树上**，用 `passive_path` 算不出「从起点走过去要几点」。")
    out.append("")
    out.append(_BOUNDARY + "。")
    return "\n".join(out)


def _render_reverse(
    notable: sqlite3.Row,
    pools: list[dict[str, Any]],
    *,
    mapping_available: bool = True,
) -> str:
    label = notable["name_zh"] or notable["name_en"]
    kind = "基石" if notable["is_keystone"] else "核心天赋"
    out = [f"### {label}（星团珠宝{kind}）", ""]
    out.append(
        f"这是**星团珠宝上的{kind}**（英文名 {notable['name_en']}），不在主天赋树上。"
    )
    if not mapping_available:
        out += ["", "核心天赋词缀权重表尚未导入，当前不能反查可出现类型。"]
    elif pools:
        out += ["", f"当前 PoB 正权重映射命中 {len(pools)} 种小天赋类型：", ""]
        out += ["| 尺寸 | 小天赋类型 | 英文名 | 基础生成权重 |", "| --- | --- | --- | --- |"]
        for pool in pools:
            out.append(
                f"| {_SIZE_ZH.get(pool['size'], pool['size'])} | "
                f"{' / '.join(pool['names_zh']) or '—'} | {pool['name_en']} | {pool['base_weight']} |"
            )
    else:
        out += [
            "",
            "当前 `ModJewelCluster.lua` 没有为它记录任何正权重小天赋类型；"
            "这类条目不能据此当作普通随机核心天赋词缀。",
        ]
    out += ["", f"> 边界：{_BOUNDARY}。"]
    return "\n".join(out)


def _citation(row: sqlite3.Row, title: str, text: str) -> dict[str, Any]:
    priority = int(row["source_priority"]) if "source_priority" in row.keys() else 2
    return {
        "chunk_id": f"cluster_jewel:{title}",
        "entity_id": None,
        "title": title,
        "url": str(row["pob_url"]) if "pob_url" in row.keys() else "",
        "source": str(row["source"]) if "source" in row.keys() else "Path of Building",
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "structured",
    }


def _mod_citation(mod: dict[str, Any], title: str, text: str) -> dict[str, Any]:
    priority = int(mod["source_priority"])
    return {
        "chunk_id": f"cluster_notable_pool:{title}",
        "entity_id": None,
        "title": title,
        "url": str(mod["url"]),
        "source": str(mod["source"]),
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "structured",
    }


@REGISTRY.register(
    name="cluster_jewel_info",
    description=(
        "查星团珠宝：各尺寸能加几个小天赋、有几个核心天赋位与珠宝插槽位、"
        "可选的小天赋类型，以及**只能从星团珠宝获得的基石**清单；"
        "可按中英文小天赋类型查能出现的核心天赋、要求物等和基础生成权重，也可反查"
        "某个核心天赋能出现在哪些类型。基础权重不是单次概率。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "问尺寸写「小型/中型/大型」；问某个核心天赋写它的名字，"
                "如「空明之掌」；问池写小天赋类型，如「大型斧剑伤害」",
            },
            "item_level": {
                "type": "integer", "minimum": 1, "maximum": 100,
                "description": "可选的星团珠宝物品等级；用于排除要求物等更高的核心天赋",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)
def cluster_jewel_info(query: str, item_level: int | None = None) -> ToolResult:
    conn = dbmod.get_conn()
    text_query = (query or "").strip()
    if not text_query:
        return ToolResult(ok=False, error="query 不能为空")
    if item_level is not None and (
        isinstance(item_level, bool) or not isinstance(item_level, int) or not 1 <= item_level <= 100
    ):
        return ToolResult(ok=False, error="item_level 必须是 1 到 100 的整数")
    level_match = _ITEM_LEVEL_RE.search(text_query)
    inferred_level = int(level_match.group(1)) if level_match else None
    if inferred_level is not None and not 1 <= inferred_level <= 100:
        return ToolResult(ok=False, error="问题中的物品等级必须是 1 到 100 的整数")
    effective_level = item_level if item_level is not None else inferred_level

    sizes = conn.execute("SELECT * FROM pob_cluster_jewel ORDER BY min_nodes").fetchall()
    if not sizes:
        return ToolResult(
            ok=True, data={"found": False, "reason": "星团珠宝数据尚未导入"}
        )
    skills = _skill_rows(conn)
    skills_by_size: dict[str, list[dict[str, Any]]] = {}
    for row in skills:
        skills_by_size.setdefault(str(row["size"]), []).append(row)

    all_mods = _notable_mods(conn)
    cluster_commit = str(sizes[0]["source_commit"])
    mod_commits = {mod["source_commit"] or "无法解析" for mod in all_mods}
    mods = [mod for mod in all_mods if mod["source_commit"] == cluster_commit]
    sources_compatible = bool(mods) and mod_commits == {cluster_commit}
    size = _size_of(text_query)
    skill, ambiguous = _resolve_skill_type(skills, text_query, size)
    if skill is not None:
        if not all_mods:
            return ToolResult(
                ok=True,
                data={
                    "found": False, "kind": "mapping_data_missing",
                    "reason": "核心天赋词缀权重表尚未导入；请运行 import_pob_mods.py",
                },
                citations=[],
                meta={"channel": "structured", "table": "pob_cluster_skill+pob_mod"},
            )
        if all_mods and not sources_compatible:
            return ToolResult(
                ok=True,
                data={
                    "found": False, "kind": "source_version_mismatch",
                    "reason": "星团类型表与核心天赋词缀表不是同一 PoB commit，拒绝跨版本拼接",
                    "cluster_commit": cluster_commit,
                    "modifier_commits": sorted(mod_commits),
                },
                citations=[],
                meta={"channel": "structured", "table": "pob_cluster_skill+pob_mod"},
            )
        pool, excluded = _pool_for_skill(mods, str(skill["weight_tag"]), effective_level)
        rendered = _render_pool(skill, pool, effective_level, excluded)
        citations = [_citation(
            next(row for row in sizes if row["size"] == skill["size"]),
            f"{_skill_label(skill)}小天赋类型", rendered,
        )]
        if pool:
            source = next(mod for mod in mods if mod["mod_id"] == pool[0]["mod_id"])
            citations.insert(0, _mod_citation(source, f"{_skill_label(skill)}核心天赋池", rendered))
        return ToolResult(
            ok=True,
            data={
                "found": True, "kind": "notable_pool", "size": skill["size"],
                "skill_type": {
                    key: skill[key] for key in ("tag", "weight_tag", "name_en", "names_zh")
                },
                "item_level": effective_level, "count": len(pool),
                "excluded_by_item_level": excluded, "notables": pool,
                "boundary": _BOUNDARY, "answer": rendered,
            },
            citations=citations,
            meta={"channel": "structured", "table": "pob_cluster_skill+pob_mod+pob_cluster_notable"},
        )
    if ambiguous:
        return ToolResult(
            ok=True,
            data={
                "found": False, "kind": "ambiguous_skill_type",
                "candidates": [
                    {"size": row["size"], "tag": row["tag"], "name_en": row["name_en"],
                     "names_zh": row["names_zh"]} for row in ambiguous
                ],
                "reason": "小天赋类型不唯一，请补充尺寸或完整中英文类型名",
            },
            citations=[],
            meta={"channel": "structured", "table": "pob_cluster_skill"},
        )

    # 1) 具体核心天赋：这是把 passive_path 那句「可能」变成事实的那条路
    notable = _notable_from_query(conn, text_query)
    if notable is not None:
        if not all_mods:
            text = _render_reverse(notable, [], mapping_available=False)
            return ToolResult(
                ok=True,
                data={
                    "found": True, "kind": "notable", "name_zh": notable["name_zh"],
                    "name_en": notable["name_en"], "is_keystone": bool(notable["is_keystone"]),
                    "on_main_tree": False, "skill_types": None,
                    "mapping_status": "not_imported", "boundary": _BOUNDARY, "answer": text,
                },
                citations=[_citation(
                    notable, f"{notable['name_zh'] or notable['name_en']}（星团珠宝）", text
                )],
                meta={"channel": "structured", "table": "pob_cluster_notable"},
            )
        if all_mods and not sources_compatible:
            return ToolResult(
                ok=True,
                data={
                    "found": False, "kind": "source_version_mismatch",
                    "reason": "星团核心清单与词缀权重表不是同一 PoB commit，拒绝跨版本拼接",
                    "cluster_commit": cluster_commit,
                    "modifier_commits": sorted(mod_commits),
                },
                citations=[],
                meta={"channel": "structured", "table": "pob_cluster_notable+pob_mod"},
            )
        pools = _reverse_pools(skills, mods, str(notable["name_en"]))
        text = _render_reverse(notable, pools)
        citations = [_citation(
            notable, f"{notable['name_zh'] or notable['name_en']}（星团珠宝）", text
        )]
        if pools:
            source = next(mod for mod in mods if mod["name_en"] == notable["name_en"])
            citations.insert(0, _mod_citation(source, f"{notable['name_en']} 可出现类型", text))
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "kind": "notable",
                "name_zh": notable["name_zh"],
                "name_en": notable["name_en"],
                "is_keystone": bool(notable["is_keystone"]),
                "on_main_tree": False,
                "skill_types": pools,
                "boundary": _BOUNDARY,
                "answer": text,
            },
            citations=citations,
            meta={"channel": "structured", "table": "pob_cluster_notable+pob_mod"},
        )

    # 2) 基石清单
    if "基石" in text_query or "keystone" in text_query.lower():
        rows = conn.execute(
            "SELECT * FROM pob_cluster_notable WHERE is_keystone=1 ORDER BY name_zh"
        ).fetchall()
        return ToolResult(
            ok=True,
            data={"found": True, "kind": "keystones", "count": len(rows),
                  "boundary": _BOUNDARY},
            citations=[_citation(rows[0], "星团珠宝基石清单", _render_keystones(rows))],
            meta={"channel": "structured", "table": "pob_cluster_notable"},
        )

    # 3) 某个尺寸的小点类型
    if size:
        rows = skills_by_size.get(size, [])
        size_row = next((r for r in sizes if str(r["size"]) == size), None)
        if rows and size_row is not None:
            return ToolResult(
                ok=True,
                data={"found": True, "kind": "size", "size": size,
                      "min_nodes": size_row["min_nodes"], "max_nodes": size_row["max_nodes"],
                      "skill_types": len(rows), "boundary": _BOUNDARY},
                citations=[_citation(size_row, f"{_SIZE_ZH[size]}星团珠宝",
                                     _render_skills(size, rows, size_row))],
                meta={"channel": "structured", "table": "pob_cluster_skill"},
            )

    # 4) 兜底：三个尺寸的总览
    return ToolResult(
        ok=True,
        data={"found": True, "kind": "overview", "sizes": len(sizes),
              "boundary": _BOUNDARY},
        citations=[_citation(sizes[0], "星团珠宝尺寸总览", _render_sizes(sizes, skills_by_size))],
        meta={"channel": "structured", "table": "pob_cluster_jewel"},
    )
