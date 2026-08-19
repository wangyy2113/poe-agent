"""游戏常量访问层。数据来自 `game_constant` 表（由 PoB `Misc.lua` 导入）。

# 设计上最重要的一条：缺失即抛异常，绝不给默认值

`get("characterConstants.base_maximum_all_resistances_%")` 取不到时抛
`MissingConstant`，而不是返回 75。

理由：默认值会让「常量没导入」和「常量确实是这个值」无法区分。抗性上限静默取到
一个错误的默认值，会让整条防御结论反向，而且不报错——正是本项目第一原则要拦的
那种失败。宁可让调用方炸掉。

# 每个常量都带出处

`provenance(key)` 返回 GGG 原始表名（`Metadata/Characters/Character.ot`）、
PoB 版本、游戏版本与钉住 commit 的 URL。需求 12 要求常量表附来源与版本，
这里做到的是**逐个常量**可溯源，不是整张表一个来源。

注意 `dat_source_inherited=1` 的常量，其出处是从上文注释延续推断的，不是紧贴标注。
用于事实断言前应人工确认。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class MissingConstant(KeyError):
    """常量不存在。**不要用默认值兜底。**"""


class ConstantTypeError(TypeError):
    """常量存在但类型不符（拿等级表当标量取，或反之）。"""


# ── 关键常量的键名 ─────────────────────────────────────────
# 写成常量是为了让拼写错误在导入期暴露（`verify` 会逐个检查），
# 而不是等到某次计算返回了 KeyError 才发现。

MAX_ALL_RESISTANCES = "characterConstants.base_maximum_all_resistances_%"
MAX_PHYS_DAMAGE_REDUCTION = "characterConstants.maximum_physical_damage_reduction_%"
MAX_BLOCK = "characterConstants.maximum_block_%"
MAX_SPELL_BLOCK = "characterConstants.base_maximum_spell_block_%"
ES_RECHARGE_RATE_PER_MINUTE = "characterConstants.energy_shield_recharge_rate_per_minute_%"
MANA_REGEN_RATE_PER_MINUTE = "characterConstants.mana_regeneration_rate_per_minute_%"
PHYS_DR_PER_ENDURANCE_CHARGE = "characterConstants.physical_damage_reduction_%_per_endurance_charge"
CRIT_CHANCE_PER_POWER_CHARGE = "characterConstants.critical_strike_chance_+%_per_power_charge"
ATTACK_SPEED_PER_FRENZY_CHARGE = "characterConstants.base_attack_speed_+%_per_frenzy_charge"

#: 这两个不在 `Data/Misc.lua`，而在 PoB 自己的 magic numbers 表 `Modules/Data.lua`，
#: 因此 `dat_source` 为空（没有 GGG 原始表出处）。抗性结算离不开它们：
#: 上限 90 是「最大抗性」自己的天花板（基础 75 + 加成后仍不能超过它），
#: 下限 −200 是被减抗/诅咒压下去时的地板。
MAX_RESIST_CAP = "pobMisc.MaxResistCap"
RESIST_FLOOR = "pobMisc.ResistFloor"

ES_RATIO_OF_LIFE = "gameConstants.EnergyShieldRatioOfLife"
MONSTER_DR_IMPROVEMENT = "gameConstants.MonsterDamageReductionImprovement"
MONSTER_ACCURACY_IMPROVEMENT = "gameConstants.MonsterAccuracyImprovement"
MONSTER_EVASION_IMPROVEMENT = "gameConstants.MonsterEvasionImprovement"

MONSTER_LIFE_TABLE = "monsterLifeTable"
MONSTER_ARMOUR_TABLE = "monsterArmourTable"
MONSTER_EVASION_TABLE = "monsterEvasionTable"
MONSTER_ACCURACY_TABLE = "monsterAccuracyTable"
MONSTER_DAMAGE_TABLE = "monsterDamageTable"
MONSTER_AILMENT_THRESHOLD_TABLE = "monsterAilmentThresholdTable"

#: `calc/` 依赖的全部常量。导入脚本据此校验完整性。
REQUIRED = (
    MAX_ALL_RESISTANCES, MAX_PHYS_DAMAGE_REDUCTION, MAX_BLOCK, MAX_SPELL_BLOCK,
    ES_RECHARGE_RATE_PER_MINUTE, MANA_REGEN_RATE_PER_MINUTE,
    PHYS_DR_PER_ENDURANCE_CHARGE, CRIT_CHANCE_PER_POWER_CHARGE,
    ATTACK_SPEED_PER_FRENZY_CHARGE,
    MAX_RESIST_CAP, RESIST_FLOOR,
    ES_RATIO_OF_LIFE, MONSTER_DR_IMPROVEMENT,
    MONSTER_LIFE_TABLE, MONSTER_ARMOUR_TABLE, MONSTER_EVASION_TABLE,
    MONSTER_ACCURACY_TABLE, MONSTER_DAMAGE_TABLE,
)


@dataclass(frozen=True)
class Provenance:
    """一个常量的溯源信息。"""

    key: str
    #: GGG 原始表名，如 Metadata/Characters/Character.ot
    dat_source: str
    #: True = 出处是从上文注释延续推断的，用于事实断言前应人工确认
    inherited: bool
    source: str
    source_priority: int
    url: str
    pob_version: str
    game_version: str

    def render(self) -> str:
        origin = self.dat_source or "未标注"
        hint = "（出处为推断）" if self.inherited else ""
        return (
            f"来源：{self.source} → {self.key}"
            f"（GGG 原始表 {origin}{hint}，{self.pob_version}，游戏 {self.game_version}）"
        )


@dataclass(frozen=True)
class _Row:
    kind: str
    value_num: float | None
    value_json: Any
    provenance: Provenance


_cache: dict[str, _Row] | None = None
_lock = threading.Lock()


def load(conn: sqlite3.Connection | None = None, *, force: bool = False) -> dict[str, _Row]:
    """把常量表读进内存。幂等，进程内只读一次。"""
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        if conn is None:
            from core import db as dbmod

            conn = dbmod.get_conn()
        rows: dict[str, _Row] = {}
        for r in conn.execute(
            "SELECT key, group_name, dat_source, dat_source_inherited, kind, "
            "value_num, value_json, source, source_priority, url, pob_version, "
            "game_version FROM game_constant"
        ):
            raw = r["value_json"]
            parsed: Any = None
            if raw:
                try:
                    parsed = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    logger.warning("常量 %s 的 value_json 不是合法 JSON，已忽略", r["key"])
            rows[r["key"]] = _Row(
                kind=r["kind"],
                value_num=r["value_num"],
                value_json=parsed,
                provenance=Provenance(
                    key=r["key"],
                    dat_source=r["dat_source"] or "",
                    inherited=bool(r["dat_source_inherited"]),
                    source=r["source"],
                    source_priority=int(r["source_priority"]),
                    url=r["url"],
                    pob_version=r["pob_version"] or "",
                    game_version=r["game_version"] or "",
                ),
            )
        _cache = rows
        logger.info("已载入 %d 个游戏常量", len(rows))
        return rows


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None


def _row(key: str) -> _Row:
    rows = load()
    r = rows.get(key)
    if r is None:
        raise MissingConstant(
            f"常量 {key!r} 不存在（库里共 {len(rows)} 个）。"
            "请先跑 scripts/import_pob_constants.py。"
            "**不要用默认值兜底**——静默的错误常量会让整条结论反向且不报错。"
        )
    return r


def get(key: str) -> float:
    """取标量常量。缺失或类型不符即抛异常。"""
    r = _row(key)
    if r.value_num is None:
        raise ConstantTypeError(
            f"常量 {key!r} 不是标量（kind={r.kind}），请用 table() 或 at_level()"
        )
    return float(r.value_num)


def table(key: str) -> list[float]:
    """取数组常量（如按等级索引的怪物属性表）。"""
    r = _row(key)
    if not isinstance(r.value_json, list):
        raise ConstantTypeError(f"常量 {key!r} 不是数组（kind={r.kind}），请用 get()")
    return [float(v) for v in r.value_json]


def at_level(key: str, level: int) -> float:
    """按等级取值。等级是 **1-based**，与游戏一致。

    越界即抛异常，不做截断：`at_level(..., 120)` 静默返回 100 级的值，
    会让「超出表范围」这个事实消失。
    """
    values = table(key)
    if level < 1 or level > len(values):
        raise MissingConstant(
            f"常量 {key!r} 只覆盖 1..{len(values)} 级，取不到 {level} 级"
        )
    return values[level - 1]


def provenance(key: str) -> Provenance:
    return _row(key).provenance


def missing(keys: tuple[str, ...] = REQUIRED) -> list[str]:
    """返回缺失的常量键。导入脚本与自检用。"""
    rows = load()
    return [k for k in keys if k not in rows]


