"""PoB `Data/ClusterJewels.lua` 抽取器：星团珠宝的尺寸、小天赋类型与核心天赋清单。

## 这一轮补的是**分类**，不是数据

300 个星团核心天赋早就在库里了——它们在天赋树 JSON 里，随天赋树一起入库成了天赋卡
（实测 300/300 都能按中文名查到）。缺的一直是「**这些只能从星团珠宝拿、不在主树上**」
这个事实：上一轮的 `passive_path` 只能说「可能只存在于星团珠宝或升华子树」，
而 300 个节点在图里都是 `passable=1`、只是没有任何连线。

这一份清单把那句「可能」变成事实。8 个星团基石里正好有上一轮 BFS 判为不可达的
「百战老兵」「空明之掌」「深渊斗士」——分类与图的观测互相印证。

## 中文名从已入库的天赋实体桥接

PoB 只有英文名。不另抓树数据：按英文名对 `entity.name_en`（天赋卡的英文名实测 0 缺失）。
桥接不上的留 NULL，**不由英文名硬翻**。

## 小天赋类型只有英文

「增加的小天赋给予…」的中文附魔文本不在任何已缓存页上（`Cobalt_Jewel` 页 601KB 里
一句「小天赋」都没有）。所以按 PoB 原文存，卡片上标明是英文——与宝石的
`description_en` 同一处理，不自己翻译。

## 核心天赋池来自另一张自动生成表

`ClusterJewels.lua` 自身只给「尺寸 → 类型」与一份扁平 notable 清单；同一 PoB commit 的
`ModJewelCluster.lua` 则在每条核心天赋词缀的 `weightKey` / `weightVal` 中给出类型 tag、
正权重和物等门槛。抽取器保持两类原始数据独立，查询工具再按 tag 与 commit 连接，避免
把跨版本数据拼成一张看似完整的表。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kb.sources.pob_lua import load_lua_file, parse_return_table

CLUSTER_FILE = "Data/ClusterJewels.lua"

#: 尺寸键（PoB 用 `Small Cluster Jewel` 这种全名）→ 简称
_SIZE_KEY = {
    "Small Cluster Jewel": "Small",
    "Medium Cluster Jewel": "Medium",
    "Large Cluster Jewel": "Large",
}


class ClusterStructureError(ValueError):
    """星团珠宝数据结构与预期不符。"""


@dataclass(frozen=True)
class ClusterSize:
    """一个尺寸的星团珠宝。`min_nodes`/`max_nodes` 是能加的小天赋数量。"""

    size: str
    min_nodes: int
    max_nodes: int
    notable_slots: int
    socket_slots: int


@dataclass(frozen=True)
class ClusterSkill:
    """一个尺寸下可选的「增加的小天赋给予…」类型。全为 PoB 英文原文。"""

    size: str
    skill_id: str
    weight_tag: str
    name_en: str
    stats_en: tuple[str, ...]
    enchant_en: tuple[str, ...]


@dataclass(frozen=True)
class ClusterNotable:
    """星团珠宝上的一个核心天赋或基石。`name_zh` 由调用方桥接后填。"""

    name_en: str
    is_keystone: bool
    sort_order: int | None = None
    name_zh: str | None = None
    entity_id: str | None = None


@dataclass
class ClusterData:
    sizes: list[ClusterSize] = field(default_factory=list)
    skills: list[ClusterSkill] = field(default_factory=list)
    notables: list[ClusterNotable] = field(default_factory=list)


def _positional(value: Any) -> list[Any]:
    """Lua 位置项表 → 列表。"""
    if not isinstance(value, dict):
        return []
    return [value[k] for k in sorted((k for k in value if isinstance(k, int)))]


def _int(value: Any, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ClusterStructureError(f"{path} 不是数值：{value!r}")
    return int(value)


def extract_cluster_data(pob_root: Path) -> ClusterData:
    """解析 `Data/ClusterJewels.lua`。缺尺寸或清单为空即拒绝。"""
    path = pob_root / "src" / Path(CLUSTER_FILE)
    if not path.is_file():
        raise ClusterStructureError(f"找不到 {CLUSTER_FILE}")
    raw = parse_return_table(load_lua_file(path))

    jewels = raw.get("jewels")
    if not isinstance(jewels, dict) or not jewels:
        raise ClusterStructureError(f"{CLUSTER_FILE} 缺 jewels 表")

    out = ClusterData()
    for full_name, jewel in jewels.items():
        size = _SIZE_KEY.get(str(full_name))
        if size is None or not isinstance(jewel, dict):
            # 上游加了新尺寸时**报错而不是跳过**：静默漏一个尺寸会让「能加几个小点」少一档
            raise ClusterStructureError(f"未知的星团珠宝尺寸 {full_name!r}")
        out.sizes.append(
            ClusterSize(
                size=size,
                min_nodes=_int(jewel.get("minNodes"), path=f"{size}.minNodes"),
                max_nodes=_int(jewel.get("maxNodes"), path=f"{size}.maxNodes"),
                notable_slots=len(_positional(jewel.get("notableIndicies"))),
                socket_slots=len(_positional(jewel.get("socketIndicies"))),
            )
        )
        skills = jewel.get("skills")
        if not isinstance(skills, dict) or not skills:
            raise ClusterStructureError(f"{size} 没有 skills 表")
        for skill_id, skill in skills.items():
            if not isinstance(skill, dict):
                continue
            name = skill.get("name")
            if not isinstance(name, str) or not name:
                raise ClusterStructureError(f"{size}.{skill_id} 缺 name")
            weight_tag = skill.get("tag")
            if not isinstance(weight_tag, str) or not weight_tag:
                raise ClusterStructureError(f"{size}.{skill_id} 缺 tag")
            out.skills.append(
                ClusterSkill(
                    size=size,
                    skill_id=str(skill_id),
                    weight_tag=weight_tag,
                    name_en=name,
                    stats_en=tuple(str(s) for s in _positional(skill.get("stats"))),
                    enchant_en=tuple(str(s) for s in _positional(skill.get("enchant"))),
                )
            )

    sort_order = raw.get("notableSortOrder")
    if not isinstance(sort_order, dict) or not sort_order:
        raise ClusterStructureError(f"{CLUSTER_FILE} 缺 notableSortOrder")
    keystones = {str(k) for k in _positional(raw.get("keystones"))}
    for name, order in sort_order.items():
        out.notables.append(
            ClusterNotable(
                name_en=str(name),
                is_keystone=str(name) in keystones,
                sort_order=int(order) if isinstance(order, (int, float)) else None,
            )
        )
    # 基石可能不在 notableSortOrder 里，补齐——漏掉它们就答不了
    # 「哪些基石只能靠星团珠宝拿」
    known = {n.name_en for n in out.notables}
    for name in sorted(keystones - known):
        out.notables.append(ClusterNotable(name_en=name, is_keystone=True))

    if len(out.sizes) != 3:
        raise ClusterStructureError(f"星团珠宝尺寸应为 3 个，实际 {len(out.sizes)}")
    return out


def bridge_chinese(
    notables: list[ClusterNotable], by_name_en: dict[str, tuple[str, str]]
) -> list[ClusterNotable]:
    """按英文名把中文名与实体 id 填进去。

    `by_name_en` 形如 ``{英文名: (中文名, entity_id)}``，由调用方从**已入库的天赋实体**
    取——不另抓一遍树数据。桥接不上的留 None：由英文名硬翻出来的中文名是编造。
    """
    out: list[ClusterNotable] = []
    for item in notables:
        found = by_name_en.get(item.name_en)
        out.append(
            ClusterNotable(
                name_en=item.name_en,
                is_keystone=item.is_keystone,
                sort_order=item.sort_order,
                name_zh=found[0] if found else None,
                entity_id=found[1] if found else None,
            )
        )
    return out


def as_json(values: tuple[str, ...]) -> str:
    return json.dumps(values, ensure_ascii=False)
