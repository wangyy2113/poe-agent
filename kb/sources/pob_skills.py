"""PoB 技能石与辅助宝石抽取器。

PoB ``Data/Gems.lua`` 给宝石元数据（英文名、标签、自然最大等级），
``Data/Skills/*.lua`` 给每级数值，POEDB 的宝石清单页给中文名与 slug。

## 桥接键是 grantedEffectId，不是名字

POEDB 清单页每颗宝石的链接上带 ``data-hover="?s=Data\\GemEffects/<id>"``，那个 ``<id>``
就是 PoB 的 ``grantedEffectId``，两侧只差大小写。2026-08-18 实测：861 条 POEDB 条目按
此键命中 PoB **810 条（94.1%）**，一键多条 **0**。

**不能按名字配对**：中文名正是要从 POEDB 取的东西，用它配对就成了循环依赖；而英文名
在蜕变宝石上大量重复（「怒火之劈砍」与「劈砍」的 baseTypeName 都是 Cleave）。

未桥接的 51 条分两族，都要登记而不是静默丢弃：

``pob_missing``   POEDB 有、PoB 未发。多为历史与未上线条目（``[UNUSED]``、``[DNT]``、
                  「新惩戒」「瓦尔：横扫」）。POEDB 保留历史页面，PoB 只发当前版本。
``no_zh_name``    PoB 有、清单页没有对应条目（实测 19 条，如 ``Portal``、
                  ``SupportColdToFire``）。``entity.name_zh`` 不得为空，也**不许由英文名
                  或 slug 顶替**，所以只能跳过并登记。

## 只取字面值，数值口径以 PoB 自己的 tooltip 为准

每级的伤害在 PoB 里是**乘数**（冰霜新星 1 级存 ``0.8 / 1.2``），要配 ``baseEffectiveness``
与 ``statInterpolation`` 按公式算才是游戏里的显示值——那属于计算器，本模块不算、不存。

会呈现给用户的字段，口径全部来自 ``src/Classes/GemTooltip.lua``（不是猜的）：

============================  ==========================================================
``cost``                      ``round(值 / Costs.lua 的 Divisor, 2)``。六个 ``*PerMinute``
                              资源的除数是 60（存每分钟、显示每秒），其余为 1
``manaMultiplier``            显示值 = **值 + 100**（CWDT 存 150，游戏里是 250%）
``attackSpeedMultiplier``     显示值 = **值 + 100**（「基础攻击速度的 N%」）
``damageEffectiveness``       显示值 = **值 × 100**（「附加伤害的效果 N%」）
``critChance`` / ``cooldown`` 原值，分别按百分数与秒显示
============================  ==========================================================

**属性需求（力/敏/智）不入库**：``Gems.lua`` 的 ``reqStr/reqDex/reqInt`` 不是显示值，
而是换算系数——PoB 算的是
``round((20 + (等级需求 - 3) × 3) × (系数 / 100) ^ 0.9 × statType)``（主动 0.7 / 辅助 0.5，
小于 14 归零，见 ``CalcTools.getGemStatRequirement``）。把系数当需求写出去就是个带引用的
错数字，而复算又要先有可核对的真值来源。宝石**颜色**是字面的，那个可以答。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from selectolax.parser import HTMLParser

from kb.sources.pob_lua import LuaOpaque, load_lua_file, parse_assignments, parse_return_table

#: POEDB 清单页。**只需要这一页**：实测 861 条已完整覆盖 `Skill_Gems`（554）与
#: `Transfigured_Gems`（215），两者都是它的子集，多读一页只会重复解析。
POEDB_GEM_PAGE = "Gem"
POEDB_GEM_URL = f"https://poedb.tw/cn/{POEDB_GEM_PAGE}"

GEM_META_FILE = "Data/Gems.lua"
COSTS_FILE = "Data/Costs.lua"

#: 技能定义文件。写死而不是 glob：新增文件应该在配置层被看见，
#: 而不是让某一天多出来的文件静默改变入库口径。
SKILL_FILES: tuple[str, ...] = (
    "Data/Skills/act_str.lua",
    "Data/Skills/act_dex.lua",
    "Data/Skills/act_int.lua",
    "Data/Skills/other.lua",
    "Data/Skills/sup_str.lua",
    "Data/Skills/sup_dex.lua",
    "Data/Skills/sup_int.lua",
    "Data/Skills/glove.lua",
    "Data/Skills/minion.lua",
    "Data/Skills/spectre.lua",
)

SKIP_POB_MISSING = "pob_missing"
SKIP_NO_ZH = "no_zh_name"
SKIP_COLOR_CONFLICT = "color_conflict"

#: POEDB 的宝石链接 class → 属性归属。第四态 `gem_white` 是无属性需求的宝石。
_CLASS_TO_COLOR: dict[str, str] = {
    "gem_red": "str",
    "gem_green": "dex",
    "gem_blue": "int",
    "gem_white": "none",
}
#: PoB 的 `color` 字段用同一套语义，用于交叉校验（实测 810 条零分歧）。
_POB_COLOR: dict[int, str] = {1: "str", 2: "dex", 3: "int", 4: "none"}

#: 蜕变宝石（Transfigured）判据：``grantedEffectId`` 以 ``Alt`` + 单个大写字母结尾。
#:
#: **不能用 POEDB slug 里的 ``_of_``**：实测 23 条误判——「火焰净化」(Purity_of_Fire)、
#: 「箭雨」(Rain_of_Arrows)、「灰烬之捷」(Herald_of_Ash) 都是常规宝石，英文名里本来就有 of。
_TRANSFIGURED_RE = re.compile(r"Alt[A-Z]$")

_HOVER_RE = re.compile(r"^\?s=Data\\Gem\w+/(?P<gem_key>[\w.]+)$")

_SPACE_RE = re.compile(r"\s+")


class GemStructureError(ValueError):
    """宝石源结构不完整，或跨来源无法唯一对应。"""


@dataclass(frozen=True)
class PoedbGem:
    """POEDB 清单页上的一条宝石：只提供中文名、slug 与颜色。"""

    gem_key: str
    name_zh: str
    poedb_slug: str
    attr_color: str
    #: 同一颗宝石的其它译名（见 `parse_poedb_gems` 的两译名说明）
    name_zh_alt: tuple[str, ...] = ()
    #: 中文标签（法术 / 效果区域 / 冰霜…）。PoB 的 `tagString` 只有英文，
    #: 而中文问法（「有哪些冰霜法术」）要靠这一份才检索得到。
    tags_zh: tuple[str, ...] = ()

    @property
    def poedb_url(self) -> str:
        return f"https://poedb.tw/cn/{self.poedb_slug}"


@dataclass(frozen=True)
class GemLevel:
    """一颗宝石的一个等级。**只有字面值**，没有伤害。

    每个字段都可能缺（辅助宝石没有 cost、非攻击技能没有 attackSpeedMultiplier），
    缺就是 None——补 0 会让「没有冷却」和「冷却 0 秒」变成同一件事。
    """

    gem_level: int
    level_requirement: int | None = None
    #: 资源 → **显示值**（已按 Costs.lua 的 Divisor 换算）
    cost: dict[str, float] = field(default_factory=dict)
    cooldown: float | None = None
    stored_uses: int | None = None
    crit_chance: float | None = None
    mana_multiplier: float | None = None
    attack_speed_multiplier: float | None = None
    reservation_percent: float | None = None
    reservation_flat: float | None = None
    damage_effectiveness: float | None = None


@dataclass(frozen=True)
class SkillGem:
    """一颗宝石。`name_zh` 与 `poedb_slug` 来自 POEDB（P1），其余来自 PoB（P2）。"""

    gem_id: str
    name_en: str
    name_zh: str
    #: 同一颗宝石的其它中文译名（国服 / 繁中并列时），入别名表
    name_zh_alt: tuple[str, ...]
    poedb_slug: str
    gem_kind: str
    attr_color: str
    tags: tuple[str, ...]
    tag_string: str
    #: POEDB 的中文标签，中文检索靠它
    tags_zh: tuple[str, ...]
    natural_max_level: int
    cast_time: float | None
    skill_types: tuple[str, ...]
    is_vaal: bool
    is_transfigured: bool
    description_en: str | None
    source_file: str
    levels: tuple[GemLevel, ...]

    @property
    def poedb_url(self) -> str:
        return f"https://poedb.tw/cn/{self.poedb_slug}"

    @property
    def max_level(self) -> int:
        """PoB 发到的最高等级。腐化与 +等级能超过 `natural_max_level`，所以两者不同。"""
        return max(lv.gem_level for lv in self.levels)

    @property
    def content_hash(self) -> str:
        payload = {
            "name_en": self.name_en,
            "name_zh": self.name_zh,
            "name_zh_alt": self.name_zh_alt,
            "poedb_slug": self.poedb_slug,
            "gem_kind": self.gem_kind,
            "attr_color": self.attr_color,
            "tags": self.tags,
            "tag_string": self.tag_string,
            "tags_zh": self.tags_zh,
            "natural_max_level": self.natural_max_level,
            "cast_time": self.cast_time,
            "skill_types": self.skill_types,
            "is_vaal": self.is_vaal,
            "is_transfigured": self.is_transfigured,
            "description_en": self.description_en,
            "levels": [
                [
                    lv.gem_level, lv.level_requirement, sorted(lv.cost.items()),
                    lv.cooldown, lv.stored_uses, lv.crit_chance, lv.mana_multiplier,
                    lv.attack_speed_multiplier, lv.reservation_percent,
                    lv.reservation_flat,
                    lv.damage_effectiveness,
                ]
                for lv in self.levels
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SkippedGem:
    """未入库的宝石及原因。调用方负责登记，不静默丢弃。"""

    gem_key: str
    name: str
    reason: str
    detail: str = ""


def _text(node) -> str:  # noqa: ANN001
    return _SPACE_RE.sub(" ", node.text(deep=True)).strip() if node is not None else ""


def _tags_zh(link) -> tuple[str, ...]:  # noqa: ANN001
    """取宝石名同格里的中文标签 ``<div class="gem_tags small">攻击, 近战</div>``。

    同一颗宝石在页面上出现在两处，只有主清单那处带标签格（实测按宝石键去重后
    814/861 = 94.5% 有标签，缺的都落在 PoB 未发布、本来就要跳过的历史条目里）。
    所以这里允许取不到，由 `parse_poedb_gems` 用带标签的那条覆盖空的那条。
    """
    cell = link.parent
    node = cell.css_first("div.gem_tags") if cell is not None else None
    if node is None:
        return ()
    return tuple(part.strip() for part in _text(node).split(",") if part.strip())


def parse_poedb_gems(html: str) -> tuple[dict[str, PoedbGem], set[str]]:
    """解析 POEDB 宝石清单页，返回 ``(gem_key -> PoedbGem, 无法唯一命名的键)``。

    页面上同一颗宝石出现两次：一个空文本的图标链接 + 一个带名字的链接，空名跳过。

    ## 同键多名分两种，处理方式不同

    **同一颗宝石的两种译名**（slug 与颜色都相同）：实测 7 条，都是国服译名与繁中译名
    并列——``魔卫复苏`` / ``僵尸复苏``（Raise Zombie）、``召唤魔侍`` / ``召唤骷髅``
    （Summon Skeletons）、``赋予(辅)`` / ``赋予辅助``（Empower）。这不是歧义而是同义词：
    取**文档顺序第一个**为正名（``poedb.tw/cn`` 的主清单先给国服译名），其余进
    ``name_zh_alt`` 并入别名表，两个名字都能检索到，也都写在卡片上。

    早先的做法是把这 7 条全丢掉，那会连「召唤魔侍」「高阶多重投射(辅)」这类高频宝石
    一起丢——为了回避一次显示取舍而丢能力，代价反了。

    **slug 或颜色不同**：那才是真的对不上同一颗宝石，记进 ambiguous 由调用方按
    ``no_zh_name`` 跳过并登记。实测当前页面 0 条。
    """
    tree = HTMLParser(html)
    # gem_key -> 中文名 -> 条目。按名字去重而不是按整条：同一颗宝石在页面上出现两处，
    # 只有一处带中文标签格，按整条去重会把「同名但标签为空」当成第二个译名。
    candidates: dict[str, dict[str, PoedbGem]] = {}
    for link in tree.css("a[data-hover]"):
        classes = str(link.attributes.get("class") or "").split()
        colors = [_CLASS_TO_COLOR[c] for c in classes if c in _CLASS_TO_COLOR]
        if not colors:
            continue
        name = _text(link)
        href = str(link.attributes.get("href") or "").strip()
        if not name or not href:
            continue
        matched = _HOVER_RE.match(unquote(str(link.attributes.get("data-hover") or "")))
        if matched is None:
            continue
        gem_key = matched.group("gem_key")
        slug = href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        if not slug:
            continue
        entry = PoedbGem(gem_key, name, slug, colors[0], tags_zh=_tags_zh(link))
        by_name = candidates.setdefault(gem_key, {})
        previous = by_name.get(name)
        if previous is None:
            by_name[name] = entry
        elif not previous.tags_zh and entry.tags_zh:
            by_name[name] = entry

    out: dict[str, PoedbGem] = {}
    ambiguous: set[str] = set()
    for gem_key, by_name in candidates.items():
        entries = list(by_name.values())
        first = entries[0]
        if any(
            e.poedb_slug != first.poedb_slug or e.attr_color != first.attr_color
            for e in entries[1:]
        ):
            ambiguous.add(gem_key)
            continue
        out[gem_key] = PoedbGem(
            gem_key=first.gem_key,
            name_zh=first.name_zh,
            poedb_slug=first.poedb_slug,
            attr_color=first.attr_color,
            name_zh_alt=tuple(e.name_zh for e in entries[1:]),
            # 标签取第一条非空的：两处只有主清单那处带标签格
            tags_zh=next((e.tags_zh for e in entries if e.tags_zh), ()),
        )
    if not out:
        # 区分两种空：真的没解析到链接（抽取器或页面结构变了），
        # 与解析到了但全都无法唯一命名（数据问题）。混成一句会指错排查方向。
        if ambiguous:
            raise GemStructureError(
                f"POEDB 宝石清单页上 {len(ambiguous)} 个宝石键全部无法唯一命名"
            )
        raise GemStructureError("POEDB 宝石清单页没有可识别的宝石链接")
    return out, ambiguous


def load_cost_divisors(pob_root: Path) -> dict[str, float]:
    """``Data/Costs.lua`` 的 ``Resource -> Divisor``。

    读表而不是写死 ``{"ManaPerMinute": 60, ...}``：除数是上游数据的一部分，
    抄一份就等于埋一个会过期的常量。
    """
    raw = parse_return_table(load_lua_file(pob_root / "src" / Path(COSTS_FILE)))
    out: dict[str, float] = {}
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        resource = entry.get("Resource")
        divisor = entry.get("Divisor")
        if not isinstance(resource, str) or not isinstance(divisor, (int, float)) or not divisor:
            raise GemStructureError(f"Costs.lua 条目缺 Resource/Divisor：{entry!r}")
        out[resource] = float(divisor)
    if not out:
        raise GemStructureError("Costs.lua 没有可识别的资源")
    return out


def _tag_names(value: Any, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, dict):
        raise GemStructureError(f"{path} 必须是集合表")
    return tuple(sorted(str(k) for k, on in value.items() if isinstance(k, str) and on is True))


def _skill_type_names(value: Any) -> tuple[str, ...]:
    """``skillTypes = { [SkillType.Spell] = true }`` → ``("Spell", ...)``。

    键在 Lua 里是枚举引用，解析器保留成 `LuaOpaque('SkillType.Spell')`，取点号后那段。
    不认识的形态直接跳过：技能类型只用于卡片上的标签，宁可少一个也不要写出
    `LuaOpaque(...)` 这种内部表示。
    """
    if not isinstance(value, dict):
        return ()
    names: list[str] = []
    for key, on in value.items():
        if on is not True:
            continue
        text = key.src if isinstance(key, LuaOpaque) else str(key)
        if "." in text:
            text = text.rsplit(".", 1)[-1]
        if text.isidentifier():
            names.append(text)
    return tuple(sorted(names))


def _number(value: Any, *, path: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GemStructureError(f"{path} 不是数值：{value!r}")
    return float(value)


def _int_or_none(value: Any, *, path: str) -> int | None:
    num = _number(value, path=path)
    if num is None:
        return None
    if num != int(num):
        raise GemStructureError(f"{path} 应为整数：{value!r}")
    return int(num)


def _level_costs(raw: Any, divisors: dict[str, float], *, path: str) -> dict[str, float]:
    """按 PoB 的显示口径换算消耗：``round(值 / Divisor, 2)``。

    资源不在 Costs.lua 里时**报错而不是跳过**：那意味着上游加了新资源，
    静默丢掉会让某些技能的消耗凭空消失，而消耗为空与「无消耗」在卡片上没法区分。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise GemStructureError(f"{path} 不是表")
    out: dict[str, float] = {}
    for resource, amount in raw.items():
        if not isinstance(resource, str):
            raise GemStructureError(f"{path} 含非字符串资源名：{resource!r}")
        if resource not in divisors:
            raise GemStructureError(
                f"{path} 的资源 {resource!r} 不在 Costs.lua 里，无法确定显示口径"
            )
        value = _number(amount, path=f"{path}.{resource}")
        if value is None:
            continue
        scaled = value / divisors[resource]
        out[resource] = round(scaled, 2)
    return out


def _extract_levels(
    raw_levels: Any, divisors: dict[str, float], *, gem_key: str
) -> tuple[GemLevel, ...]:
    if not isinstance(raw_levels, dict) or not raw_levels:
        raise GemStructureError(f"{gem_key} 没有 levels 表")
    out: list[GemLevel] = []
    for level, row in sorted(
        ((k, v) for k, v in raw_levels.items() if isinstance(k, int)), key=lambda kv: kv[0]
    ):
        if not isinstance(row, dict):
            raise GemStructureError(f"{gem_key} 第 {level} 级不是表")
        path = f"{gem_key}.levels[{level}]"
        out.append(
            GemLevel(
                gem_level=level,
                level_requirement=_int_or_none(row.get("levelRequirement"), path=f"{path}.levelRequirement"),
                cost=_level_costs(row.get("cost"), divisors, path=f"{path}.cost"),
                cooldown=_number(row.get("cooldown"), path=f"{path}.cooldown"),
                stored_uses=_int_or_none(row.get("storedUses"), path=f"{path}.storedUses"),
                crit_chance=_number(row.get("critChance"), path=f"{path}.critChance"),
                mana_multiplier=_number(row.get("manaMultiplier"), path=f"{path}.manaMultiplier"),
                attack_speed_multiplier=_number(
                    row.get("attackSpeedMultiplier"), path=f"{path}.attackSpeedMultiplier"
                ),
                reservation_percent=_number(
                    row.get("manaReservationPercent"), path=f"{path}.manaReservationPercent"
                ),
                reservation_flat=_number(
                    row.get("manaReservationFlat"), path=f"{path}.manaReservationFlat"
                ),
                damage_effectiveness=_number(
                    row.get("damageEffectiveness"), path=f"{path}.damageEffectiveness"
                ),
            )
        )
    if not out:
        raise GemStructureError(f"{gem_key} 的 levels 表里没有整数等级")
    return tuple(out)


def load_pob_gems(pob_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """读 PoB 两侧原始表，返回 ``(grantedEffectId 小写 -> 宝石元数据, 技能定义)``。

    元数据按小写键索引是为了桥接：POEDB 与 PoB 的同一个 id 存在大小写差异
    （``SupportCastonMeleeKill`` vs ``SupportCastOnMeleeKill``，实测 8 条靠这一步救回）。
    同一小写键落到多条元数据时报错——那说明 PoB 侧真的重了，得先看清再入库。
    """
    meta_path = pob_root / "src" / Path(GEM_META_FILE)
    raw_gems = parse_return_table(load_lua_file(meta_path))
    by_key: dict[str, dict[str, Any]] = {}
    for gem_id, meta in raw_gems.items():
        if not isinstance(meta, dict):
            raise GemStructureError(f"Gems.lua 条目 {gem_id!r} 不是表")
        granted = meta.get("grantedEffectId")
        if not isinstance(granted, str) or not granted:
            # 没有 grantedEffectId 的条目无法参与桥接（PoB 侧的占位项），跳过
            continue
        key = granted.lower()
        if key in by_key and by_key[key] is not meta:
            raise GemStructureError(
                f"PoB 侧 grantedEffectId {granted!r} 对应多条宝石元数据，桥接键不再唯一"
            )
        by_key[key] = meta

    skills: dict[str, dict[str, Any]] = {}
    for rel in SKILL_FILES:
        path = pob_root / "src" / Path(rel)
        if not path.is_file():
            raise GemStructureError(f"找不到 {rel}")
        for name, value in parse_assignments(load_lua_file(path)).items():
            if isinstance(value, dict):
                value.setdefault("__source_file", rel)
                skills[name] = value
    if not by_key or not skills:
        raise GemStructureError("PoB 宝石元数据或技能定义为空")
    return by_key, skills


def extract_gems(
    pob_root: Path, poedb_html: str
) -> tuple[list[SkillGem], list[SkippedGem]]:
    """抽取宝石并与 POEDB 中文名逐条桥接。返回 ``(入库条目, 跳过条目)``。

    桥不上**不抛异常**：POEDB 保留了 PoB 已经不发的历史条目，整批拒绝会让 94% 的
    可用数据一起进不来。但跳过的必须逐条带原因返回，由调用方登记进缺口台账。
    """
    poedb_gems, ambiguous = parse_poedb_gems(poedb_html)
    meta_by_key, skills = load_pob_gems(pob_root)
    divisors = load_cost_divisors(pob_root)

    out: list[SkillGem] = []
    skipped: list[SkippedGem] = []
    matched_meta: set[str] = set()

    for gem_key in sorted(poedb_gems):
        entry = poedb_gems[gem_key]
        meta = meta_by_key.get(gem_key.lower())
        if meta is None:
            skipped.append(
                SkippedGem(
                    gem_key, entry.name_zh, SKIP_POB_MISSING,
                    "PoB 当前版本未发布该宝石（POEDB 保留历史与未上线条目）",
                )
            )
            continue
        granted = str(meta["grantedEffectId"])
        skill = skills.get(granted)
        if skill is None:
            skipped.append(
                SkippedGem(
                    gem_key, entry.name_zh, SKIP_POB_MISSING,
                    f"Gems.lua 有元数据但 Data/Skills 里没有 {granted!r} 的技能定义",
                )
            )
            continue

        pob_color = _POB_COLOR.get(skill.get("color")) if isinstance(skill.get("color"), int) else None
        if pob_color is not None and pob_color != entry.attr_color:
            # 两源对同一颗宝石的属性归属不一致 → 只跳这一条并登记。
            # 整批拒绝不成比例（实测当前 0 条），静默取一侧则是在两个都可能过期的
            # 来源之间替用户做判断。
            skipped.append(
                SkippedGem(
                    gem_key, entry.name_zh, SKIP_COLOR_CONFLICT,
                    f"POEDB 判为 {entry.attr_color}，PoB 判为 {pob_color}",
                )
            )
            continue

        name_en = meta.get("name") or skill.get("name")
        if not isinstance(name_en, str) or not name_en:
            raise GemStructureError(f"{granted!r} 两侧都没有英文名")
        natural_max = meta.get("naturalMaxLevel")
        if not isinstance(natural_max, int) or natural_max <= 0:
            raise GemStructureError(f"{granted!r}.naturalMaxLevel 非法：{natural_max!r}")

        is_support = bool(skill.get("support")) or bool(
            (meta.get("tags") or {}).get("support")
        )
        description = skill.get("description")
        if description is not None and not isinstance(description, str):
            raise GemStructureError(f"{granted!r}.description 不是字符串")

        out.append(
            SkillGem(
                gem_id=granted,
                name_en=name_en,
                name_zh=entry.name_zh,
                name_zh_alt=entry.name_zh_alt,
                poedb_slug=entry.poedb_slug,
                gem_kind="support" if is_support else "active",
                attr_color=entry.attr_color,
                tags=_tag_names(meta.get("tags") or {}, path=f"{granted}.tags"),
                tag_string=str(meta.get("tagString") or ""),
                tags_zh=entry.tags_zh,
                natural_max_level=natural_max,
                cast_time=_number(skill.get("castTime"), path=f"{granted}.castTime"),
                skill_types=_skill_type_names(skill.get("skillTypes")),
                is_vaal=meta.get("vaalGem") is True,
                is_transfigured=bool(_TRANSFIGURED_RE.search(granted)),
                description_en=description,
                source_file=str(skill.get("__source_file") or ""),
                levels=_extract_levels(skill.get("levels"), divisors, gem_key=granted),
            )
        )
        matched_meta.add(granted.lower())

    for gem_key in sorted(ambiguous):
        skipped.append(
            SkippedGem(gem_key, "", SKIP_NO_ZH, "POEDB 清单页上该宝石有多个不同中文名，无法唯一命名")
        )

    for key, meta in sorted(meta_by_key.items()):
        if key in matched_meta:
            continue
        name_en = str(meta.get("name") or key)
        skipped.append(
            SkippedGem(
                str(meta.get("grantedEffectId") or key), name_en, SKIP_NO_ZH,
                "POEDB 清单页上没有对应条目，取不到中文名（不许用英文名或 slug 顶替）",
            )
        )

    out.sort(key=lambda gem: (gem.gem_kind, gem.name_en))
    return out, skipped
