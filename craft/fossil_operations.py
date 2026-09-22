"""化石标签权重变换与地渊词缀目录。

本模块只结构化本地来源已经明确给出的两层事实：

* ``poedb_tag_weight kind='化石'`` 中逐标签的乘数；
* ``mod_affix.category='delve'`` 中按底材类别收录的地渊词缀。

乘数只逐标签披露，不推导多个标签如何合并，更不换算成一次制作的最终概率。
共鸣器槽位、化石组合、稀有度变化与完整执行契约没有来源，始终保持 unknown。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from parsers.item_mods import ItemClassRef, resolve_item_class

FossilVerdict = Literal["unknown"]
CheckStatus = Literal["pass", "unknown"]


FOSSIL_NAMES = (
    "丰裕化石", "五彩化石", "以太化石", "冰冽化石", "原始化石", "机巧化石",
    "根基化石", "棱面化石", "炽炎化石", "狼牙化石", "畸变化石", "绑缚化石",
    "腐蚀化石", "致密化石", "透光化石", "金属化石", "锯齿化石", "震颤化石",
)


@dataclass(frozen=True)
class FossilItemState:
    item_class: str = ""
    item_level: int | None = None
    rarity: str | None = None
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False
    incomplete_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FossilCheck:
    key: str
    status: CheckStatus
    summary: str
    source_url: str = ""

    def as_dict(self) -> dict[str, str]:
        out = {"key": self.key, "status": self.status, "summary": self.summary}
        if self.source_url:
            out["source_url"] = self.source_url
        return out


@dataclass
class FossilOperationResult:
    verdict: FossilVerdict
    fossil: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    before: FossilItemState
    after: dict[str, Any]
    weight_rules: list[dict[str, Any]]
    checks: list[FossilCheck] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    delve_pool: dict[str, Any] | None = None
    metamod_interactions: list[dict[str, str | None]] = field(default_factory=list)
    boundary: str = (
        "标签乘数是 POEDB 页面给出的逐标签权重变换，不是最终成功率；地渊目录也不等于"
        "这颗化石单独可生成的完整结果池。共鸣器组合、乘数合并规则、稀有度变化、"
        "具体结果数量、制作步骤、成本与路线均未采集"
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "fossil": self.fossil,
            "source": self.source,
            "source_priority": self.source_priority,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "before": self.before.as_dict(),
            "after": self.after,
            "weight_rules": self.weight_rules,
            "checks": [check.as_dict() for check in self.checks],
            "unknown_fields": self.unknown_fields,
            "delve_pool": self.delve_pool,
            "metamod_interactions": self.metamod_interactions,
            "boundary": self.boundary,
        }


def _resolve_rules(conn: sqlite3.Connection, fossil: str) -> list[sqlite3.Row]:
    value = (fossil or "").strip()
    if value not in FOSSIL_NAMES:
        return []
    return conn.execute(
        "SELECT name_zh,tag,multiplier,row_index,source,source_priority,url,fetched_at "
        "FROM poedb_tag_weight WHERE kind='化石' AND name_zh=? ORDER BY row_index",
        (value,),
    ).fetchall()


def _json_tags(value: object) -> set[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return set()
    return {str(tag) for tag in parsed if isinstance(tag, str)} if isinstance(parsed, list) else set()


def _candidate_pool(
    conn: sqlite3.Connection,
    item_class: str,
    item_level: int | None,
    rules: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, ItemClassRef]:
    ref = resolve_item_class(conn, item_class)
    if not ref.ok:
        return None, ref
    placeholders = ",".join("?" for _ in ref.pages)
    rows = conn.execute(
        f"SELECT mod_code,name_zh,gen_label,correct_group,ilvl,weight,text_zh,tags,"
        f"fossil_tags,url,item_class_page FROM mod_affix WHERE category='delve' "
        f"AND item_class_page IN ({placeholders}) "
        "ORDER BY gen_type,correct_group,ilvl,text_zh,item_class_page",
        ref.pages,
    ).fetchall()
    unique: dict[tuple[str, str, str, int], sqlite3.Row] = {}
    for row in rows:
        key = (
            str(row["mod_code"]), str(row["gen_label"]), str(row["text_zh"]),
            int(row["ilvl"] or 0),
        )
        unique.setdefault(key, row)

    candidates = []
    excluded_by_level = 0
    for row in unique.values():
        ilvl = int(row["ilvl"] or 0)
        if item_level is not None and ilvl > item_level:
            excluded_by_level += 1
            continue
        tags = _json_tags(row["tags"]) | _json_tags(row["fossil_tags"])
        matching = [
            {"tag": rule["tag"], "multiplier": rule["multiplier"]}
            for rule in rules if rule["tag"] in tags
        ]
        candidates.append({
            "mod_code": str(row["mod_code"]),
            "name_zh": str(row["name_zh"] or ""),
            "position": str(row["gen_label"] or "未知"),
            "group": str(row["correct_group"] or ""),
            "item_level": ilvl,
            "base_weight": int(row["weight"] or 0),
            "text": str(row["text_zh"] or ""),
            "matching_weight_rules": matching,
            "blocked_by_explicit_x0": any(rule["multiplier"] == 0 for rule in matching),
            "url": str(row["url"] or ""),
        })

    return {
        "item_class": ref.name_zh or item_class,
        "pages": ref.pages,
        "scope": f"{len(ref.pages)} 个 POEDB 底材页面",
        "item_level_filter": item_level,
        "source_rows": len(rows),
        "unique_before_level_filter": len(unique),
        "excluded_by_item_level": excluded_by_level,
        "unique_candidates": len(candidates),
        "prefix_candidates": sum(row["position"] == "前缀" for row in candidates),
        "suffix_candidates": sum(row["position"] == "后缀" for row in candidates),
        "explicitly_blocked_candidates": sum(row["blocked_by_explicit_x0"] for row in candidates),
        "candidates": candidates,
        "caveat": (
            "这里只列本库 category='delve' 的地渊来源词缀，并逐条标出能直接匹配的标签规则；"
            "它不是普通词缀池、不是单颗化石的完整可生成池，也不把多个标签乘数合并成概率"
        ),
    }, ref


def _metamod_rows(conn: sqlite3.Connection) -> list[dict[str, str | None]]:
    try:
        rows = conn.execute(
            "SELECT guard_text,outcome,raw_value,note_text,source_url FROM "
            "craft_metamod_interaction WHERE operation_text='化石' ORDER BY column_order"
        ).fetchall()
    except sqlite3.Error:
        return []
    return [{
        "guard": str(row["guard_text"]),
        "outcome": str(row["outcome"]),
        "raw_value": str(row["raw_value"]),
        "note": str(row["note_text"]) if row["note_text"] else None,
        "source_url": str(row["source_url"]),
    } for row in rows]


def evaluate_fossil_operation(
    conn: sqlite3.Connection,
    fossil: str,
    state: FossilItemState,
) -> FossilOperationResult | None:
    rows = _resolve_rules(conn, fossil)
    if not rows:
        return None
    rules = [{
        "tag": str(row["tag"]),
        "multiplier": float(row["multiplier"]),
        "effect": "blocked" if float(row["multiplier"]) == 0 else (
            "increased" if float(row["multiplier"]) > 1 else "reduced"
        ),
    } for row in rows]
    url = str(rows[0]["url"])
    checks = [FossilCheck(
        "tag_weight_contract", "pass",
        f"精确命中 {len(rules)} 条 POEDB 标签乘数；只逐条应用，不推导组合后的最终权重或概率",
        url,
    )]

    pool = None
    if state.item_class:
        pool, ref = _candidate_pool(conn, state.item_class, state.item_level, rules)
        if pool is None:
            checks.append(FossilCheck(
                "delve_pool", "unknown", ref.reason or "无法解析底材类别的地渊词缀目录"
            ))
        else:
            checks.append(FossilCheck(
                "delve_pool", "pass",
                f"{pool['item_class']}在 {pool['scope']} 中有 {pool['unique_candidates']} 条"
                "去重后的地渊词缀目录记录；这不是完整生成池或成功率",
                str(pool["candidates"][0]["url"]) if pool["candidates"] else "",
            ))
    else:
        checks.append(FossilCheck(
            "delve_pool", "unknown", "未给底材类别，无法汇总对应的地渊来源词缀目录"
        ))

    special = []
    if state.corrupted:
        special.append("已腐化")
    if state.mirrored:
        special.append("已镜像")
    if state.fractured:
        special.append("含裂隙词缀")
    if special:
        checks.append(FossilCheck(
            "special_state", "unknown", "、".join(special) + "的化石可执行规则没有来源"
        ))
    if state.incomplete_reason:
        checks.append(FossilCheck("state_completeness", "unknown", state.incomplete_reason))

    return FossilOperationResult(
        verdict="unknown",
        fossil=str(rows[0]["name_zh"]),
        source=str(rows[0]["source"]),
        source_priority=int(rows[0]["source_priority"]),
        source_url=url,
        fetched_at=str(rows[0]["fetched_at"]),
        before=state,
        after={
            "rarity": None,
            "prefix_count": None,
            "suffix_count": None,
            "modifier_change": "unknown",
            "known_tag_weight_transform": rules,
        },
        weight_rules=rules,
        checks=checks,
        unknown_fields=[
            "共鸣器槽位、化石组合与使用前提",
            "使用后的稀有度、前后缀数量、具体新增或移除词缀",
            "多个标签乘数的合并规则与最终生成权重",
            "单次结果概率、期望消耗、步骤、成本与制作路线",
        ],
        delve_pool=pool,
        metamod_interactions=_metamod_rows(conn),
    )
