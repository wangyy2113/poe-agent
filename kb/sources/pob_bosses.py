"""PoB `Data/BossSkills.lua` 与 `Data/Bosses.lua`：Boss 技能的**伤害建模**。

## 补的是哪一维，要说清楚

`kb/coverage.py` 的 `boss` 域长期挂着一条结论：「缺的不是抽取器而是**来源**——
要另找 Boss 详情页或 wiki」。关于 POEDB 那半句是对的（那张缓存页里确实没有 Boss
技能），**结论又一次不完整**：这条 spec 的 `pob_files=()` 是空的，而 PoB 检出里
一直有这两个文件。`pob_files` 至今没有任何运行时消费者（纯文档字段），
所以又一次没人回头看它——**同一个空元组骗了第二次**（第一次是万神殿）。

但 PoB 给的**不是打法攻略**，是**受击侧的伤害建模**：

* 补上：某技能在某等级打多少伤害、穿多少抗性、Uber 变体乘多少、Boss 的护甲/闪避倍率；
* 仍缺：技能的机制说明与应对打法（本地确实没有来源）。

## 伤害不是存下来的，是算出来的

`Modules/ConfigOptions.lua:2258`：

    round(monsterDamageTable[enemyLevel] * (damageMult[1] + rollRangeMult * damageMult[2])
          * bossData.UberDamageMultiplier)

`damageMult = [基准, 每 1% roll 的增量]`，`monsterDamageTable` **早就在
`game_constant` 里**。所以本模块只负责把系数取出来，算在 `calc/boss_damage.py`。

## 一处自带的抽验（是抽验不是全量，别写成全量）

每条技能都带 `tooltip`，而其中**少数几条在文案里复述了穿透数字**：

    UberDamagePenetrations = { FirePen = 10 }
    tooltip = "The Uber variant has 10 ^xB97123Fire^7 penetration ..."

同一文件内两处独立写法，可以逐条比对。**实测 10 条里只有 2 条能这样对**
（Atziri Flameblast 的 10、Shaper Ball 的 40），其余 tooltip 讲的是别的事。
所以这是**抽验**：对不上说明字段读错或上游改了一处忘了另一处；对得上只能说明这两条没错。

## `FirePen = ""` 是上游的真实写法

`Maven Fireball` 的非 Uber 穿透在上游就是**空字符串**——PoB 拿它去
`SetPlaceholder`，空串等于「这一栏不填」，语义是「非 Uber 变体不穿透」。
本模块把它当成**没有这一项**并**报数**，不静默写成 0：
0 与「上游留空」在事后是两件事，而把留空读成 0 恰好是会让人以为我们确认过的那种错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from kb.sources.pob_lua import load_lua_file, parse_assignments, parse_return_table

BOSS_SKILLS_FILE = "Data/BossSkills.lua"
BOSSES_FILE = "Data/Bosses.lua"

#: 伤害倍率里的两个位置项：[基准, 每 1% roll 的增量]。
_MULT_SLOTS = 2

#: tooltip 里的穿透数字，用于抽验。**两个方向都要看**——上游两种写法都有：
#:   `has 10 ^xB97123Fire^7 penetration`（数字在前）
#:   `increases the penetration to 40%`（数字在后）
#: 只写一个方向会漏掉一半可抽验的条目，而漏掉的表现是「抽验通过」——
#: 一个看起来在把关、实际没查的检查比没有检查更糟。
_TIP_PEN_BEFORE = re.compile(r"(\d+)\s*(?:%|\^x[0-9A-Fa-f]{6})?[^.]{0,24}?penetration", re.I)
_TIP_PEN_AFTER = re.compile(r"penetration[^.]{0,24}?(\d+)\s*%", re.I)


class BossSourceError(ValueError):
    """两个 Lua 文件的结构与预期不符；调用方必须拒绝整批写入。"""


@dataclass(frozen=True)
class BossSkill:
    """一条 Boss 技能预设。伤害是**系数**，真实伤害要按等级算。"""

    name: str
    damage_type: str
    #: {伤害类型: (基准, 每 1% roll 增量)}，如 {"Fire": (51.08, 0.255)}
    damage_mults: dict[str, tuple[float, float]]
    #: {穿透键: 值}，上游留空的项**不收**
    penetrations: dict[str, float]
    uber_penetrations: dict[str, float]
    uber_damage_mult: float | None
    speed: float | None
    uber_speed: float | None
    crit_chance: float | None
    earlier_uber: bool
    tooltip: str


@dataclass(frozen=True)
class Boss:
    """一个 Boss 的受击建模系数。"""

    key: str
    armour_mult: float
    evasion_mult: float
    is_uber: bool


@dataclass
class BossExtractStats:
    skills: int = 0
    bosses: int = 0
    #: 上游写成空字符串的穿透项，按「技能名·键」记下来
    blank_penetrations: list[str] = field(default_factory=list)
    #: tooltip 抽验对上的条数与总可抽验条数
    tooltip_checked: int = 0
    tooltip_matched: int = 0
    tooltip_mismatch: list[str] = field(default_factory=list)


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _pens(raw: object, *, skill: str, stats: BossExtractStats, uber: bool) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        number = _num(value)
        if number is None:
            # 上游真实写法就是 ""；记下来而不是当 0
            stats.blank_penetrations.append(
                f"{skill}·{'Uber' if uber else ''}{key}={value!r}"
            )
            continue
        out[str(key)] = number
    return out


def parse_boss_skills(text: str) -> tuple[list[BossSkill], BossExtractStats]:
    """解析 `BossSkills.lua`，并顺带做 tooltip 抽验。"""
    raw = parse_return_table(text)
    if not raw:
        raise BossSourceError(f"{BOSS_SKILLS_FILE} 里没有解析到任何技能预设")

    stats = BossExtractStats()
    out: list[BossSkill] = []
    for name, entry in raw.items():
        if not isinstance(entry, dict) or "DamageMultipliers" not in entry:
            raise BossSourceError(f"{name} 不是预期的技能预设：缺 DamageMultipliers")
        mults: dict[str, tuple[float, float]] = {}
        for dtype, pair in (entry["DamageMultipliers"] or {}).items():
            values = [pair[k] for k in sorted(pair)] if isinstance(pair, dict) else list(pair)
            if len(values) != _MULT_SLOTS:
                raise BossSourceError(
                    f"{name} 的 {dtype} 伤害倍率有 {len(values)} 项，预期 {_MULT_SLOTS} 项"
                    "（[基准, 每 1% roll 增量]）"
                )
            mults[str(dtype)] = (float(values[0]), float(values[1]))
        if not mults:
            raise BossSourceError(f"{name} 没有任何伤害倍率")

        tooltip = str(entry.get("tooltip") or "")
        uber_pens = _pens(entry.get("UberDamagePenetrations"), skill=name, stats=stats, uber=True)
        skill = BossSkill(
            name=str(name),
            damage_type=str(entry.get("DamageType") or ""),
            damage_mults=mults,
            penetrations=_pens(
                entry.get("DamagePenetrations"), skill=name, stats=stats, uber=False
            ),
            uber_penetrations=uber_pens,
            uber_damage_mult=_num(entry.get("UberDamageMultiplier")),
            speed=_num(entry.get("speed")),
            uber_speed=_num(entry.get("UberSpeed")),
            crit_chance=_num(entry.get("critChance")),
            earlier_uber=bool(entry.get("earlierUber")),
            tooltip=tooltip,
        )
        out.append(skill)
        _tooltip_spot_check(skill, stats)

    stats.skills = len(out)
    return out, stats


def _tooltip_spot_check(skill: BossSkill, stats: BossExtractStats) -> None:
    """tooltip 里若复述了穿透数字，就与结构化字段比一比。**抽验，不是全量。**"""
    matched = _TIP_PEN_BEFORE.search(skill.tooltip) or _TIP_PEN_AFTER.search(skill.tooltip)
    if not matched or not skill.uber_penetrations:
        return
    stats.tooltip_checked += 1
    claimed = float(matched.group(1))
    if claimed in set(skill.uber_penetrations.values()):
        stats.tooltip_matched += 1
    else:
        stats.tooltip_mismatch.append(
            f"{skill.name}：tooltip 说 {claimed:g}，字段是 {skill.uber_penetrations}"
        )


def parse_bosses(text: str) -> list[Boss]:
    """解析 `Bosses.lua`（`bosses["X"] = {...}` 形态的赋值）。"""
    raw = parse_assignments(text)
    if not raw:
        raise BossSourceError(f"{BOSSES_FILE} 里没有解析到任何 Boss")
    out: list[Boss] = []
    for key, entry in raw.items():
        if not isinstance(entry, dict) or "armourMult" not in entry:
            raise BossSourceError(f"{key} 不是预期的 Boss 表：缺 armourMult")
        out.append(
            Boss(
                key=str(key),
                armour_mult=float(entry.get("armourMult") or 0),
                evasion_mult=float(entry.get("evasionMult") or 0),
                is_uber=bool(entry.get("isUber")),
            )
        )
    return out


def load_all(data_dir: Path) -> tuple[list[BossSkill], list[Boss], BossExtractStats]:
    skills, stats = parse_boss_skills(load_lua_file(data_dir / "BossSkills.lua"))
    bosses = parse_bosses(load_lua_file(data_dir / "Bosses.lua"))
    stats.bosses = len(bosses)
    return skills, bosses, stats
