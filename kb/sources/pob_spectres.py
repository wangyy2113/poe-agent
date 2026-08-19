"""PoB `Data/Spectres.lua` 抽取器：幽魂的抗性、系数与技能 id。

## 它补的是 POEDB 那页没有的维度

`poedb_spectre`（P1）给清单与倍率——1948 个可作幽魂的怪物、伤害% / 生命%。
本文件给的是另外几维：**四抗性**、护甲 / 闪避 / 能量护盾系数、攻击间隔、技能 id。
代价是覆盖窄：PoB 只建模了 **268** 个（玩家实际会用的那些）。

## 两侧的生命/伤害是同一个量，单位不同

实测（2026-08-19，按 slug→英文名 配对的 200 个单行名字）：
POEDB 的百分数 ÷ 100 与 PoB 的小数**逐条相等**的占 91%（生命）/ 94.8%（伤害）。
所以它们可以对账，差异分两类，**不能混**：

``value_mismatch``  两侧都只有一条却对不上 → 真分歧（实测 8 条，如「水手咒灵」
                    POEDB 生命 100% vs PoB 1.0 但伤害 165% vs 1.32）
``granularity``     POEDB 有多个同名种类、PoB 只建模一个 → 粒度差异（43 条，
                    如「远古创造物」在 POEDB 有 119/168、80/240、200/240 三套）

把粒度差异记成分歧，就是把「谁记错了」和「谁记得更细」当成同一件事——
词缀对账那一轮的教训（2729 条「待核对」里绝大多数其实是配对判据错）。

## 技能 id 只存不呈现

`skillList` 里是 `MonsterLightningThorns` 这类怪物内部技能 id。实测 1235 个 id 与本库
810 颗宝石的 `grantedEffectId` **零命中**——它们不是玩家宝石，没有中文名可给。
存下来是为了将来（接怪物技能表时它就是桥接键），但**不进卡片、不进工具正文**。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kb.sources.pob_lua import load_lua_file, parse_assignments

SPECTRE_FILE = "Data/Spectres.lua"

CONFLICT_VALUE = "value_mismatch"
CONFLICT_GRANULARITY = "granularity"


class SpectreStructureError(ValueError):
    """PoB 幽魂数据结构与预期不符。"""


@dataclass(frozen=True)
class PoBSpectre:
    """一个 PoB 建模的幽魂。系数以 1.0 为基础值，抗性是百分数原值。"""

    metadata_id: str
    name_en: str
    life_mult: float
    damage_mult: float
    armour_mult: float | None
    evasion_mult: float | None
    es_mult: float | None
    fire_resist: int
    cold_resist: int
    lightning_resist: int
    chaos_resist: int
    attack_time: float | None
    attack_range: float | None
    accuracy: float | None
    damage_spread: float | None
    skill_ids: tuple[str, ...]
    monster_tags: tuple[str, ...]

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [
                self.name_en, self.life_mult, self.damage_mult, self.armour_mult,
                self.evasion_mult, self.es_mult, self.fire_resist, self.cold_resist,
                self.lightning_resist, self.chaos_resist, self.attack_time,
                self.attack_range, self.accuracy, self.damage_spread,
                self.skill_ids, self.monster_tags,
            ],
            ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _number(value: Any, *, path: str, required: bool = False) -> float | None:
    if value is None:
        if required:
            raise SpectreStructureError(f"{path} 缺失")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpectreStructureError(f"{path} 不是数值：{value!r}")
    return float(value)


def _int_resist(value: Any, *, path: str) -> int:
    num = _number(value, path=path, required=True)
    assert num is not None  # noqa: S101  上面 required=True 已保证
    if num != int(num):
        raise SpectreStructureError(f"{path} 抗性应为整数：{value!r}")
    return int(num)


def _string_list(value: Any, *, path: str) -> tuple[str, ...]:
    """Lua 的位置项表 → 字符串元组。非字符串项跳过（`modList` 里是未求值表达式）。"""
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise SpectreStructureError(f"{path} 不是表")
    return tuple(
        str(v) for _, v in sorted(
            ((k, v) for k, v in value.items() if isinstance(k, int) and isinstance(v, str)),
            key=lambda kv: kv[0],
        )
    )


def extract_spectres(pob_root: Path) -> list[PoBSpectre]:
    """解析 `Data/Spectres.lua`。缺必填字段即拒绝——那说明上游改了结构。"""
    path = pob_root / "src" / Path(SPECTRE_FILE)
    if not path.is_file():
        raise SpectreStructureError(f"找不到 {SPECTRE_FILE}")
    raw = parse_assignments(load_lua_file(path))
    if not raw:
        raise SpectreStructureError(f"{SPECTRE_FILE} 没有可识别的幽魂")

    out: list[PoBSpectre] = []
    for metadata_id, meta in raw.items():
        if not isinstance(meta, dict):
            raise SpectreStructureError(f"{metadata_id} 不是表")
        name = meta.get("name")
        if not isinstance(name, str) or not name:
            raise SpectreStructureError(f"{metadata_id} 缺 name")
        out.append(
            PoBSpectre(
                metadata_id=str(metadata_id),
                name_en=name,
                life_mult=float(_number(meta.get("life"), path=f"{name}.life", required=True) or 0),
                damage_mult=float(
                    _number(meta.get("damage"), path=f"{name}.damage", required=True) or 0
                ),
                # NULL 与 0 在这里含义不同：前者是「PoB 没建模这一维」，
                # 后者会被读成「这个怪物护甲为 0」
                armour_mult=_number(meta.get("armour"), path=f"{name}.armour"),
                evasion_mult=_number(meta.get("evasion"), path=f"{name}.evasion"),
                es_mult=_number(meta.get("energyShield"), path=f"{name}.energyShield"),
                fire_resist=_int_resist(meta.get("fireResist"), path=f"{name}.fireResist"),
                cold_resist=_int_resist(meta.get("coldResist"), path=f"{name}.coldResist"),
                lightning_resist=_int_resist(
                    meta.get("lightningResist"), path=f"{name}.lightningResist"
                ),
                chaos_resist=_int_resist(meta.get("chaosResist"), path=f"{name}.chaosResist"),
                attack_time=_number(meta.get("attackTime"), path=f"{name}.attackTime"),
                attack_range=_number(meta.get("attackRange"), path=f"{name}.attackRange"),
                accuracy=_number(meta.get("accuracy"), path=f"{name}.accuracy"),
                damage_spread=_number(meta.get("damageSpread"), path=f"{name}.damageSpread"),
                skill_ids=_string_list(meta.get("skillList"), path=f"{name}.skillList"),
                monster_tags=_string_list(meta.get("monsterTags"), path=f"{name}.monsterTags"),
            )
        )
    out.sort(key=lambda s: s.name_en)
    return out


@dataclass(frozen=True)
class SpectreDiff:
    """一条跨来源差异。`cause` 区分真分歧与粒度差异。"""

    name_en: str
    name_zh: str
    cause: str
    poedb_life_pct: int
    poedb_damage_pct: int
    pob_life_mult: float
    pob_damage_mult: float
    #: POEDB 侧同名种类数（>1 即粒度差异）
    poedb_rows: int

    @property
    def subject(self) -> str:
        return f"spectre={self.name_en}"

    @property
    def poedb_value(self) -> str:
        return f"生命 {self.poedb_life_pct}%、伤害 {self.poedb_damage_pct}%"

    @property
    def pob_value(self) -> str:
        return f"生命 {self.pob_life_mult:g}×、伤害 {self.pob_damage_mult:g}×"


def reconcile(
    pob: list[PoBSpectre], poedb_rows: list[dict[str, Any]]
) -> tuple[list[SpectreDiff], dict[str, int]]:
    """两侧按「slug 去下划线 == PoB 英文名」配对，返回 ``(差异, 统计)``。

    为什么用 slug 而不是中文名：中文名重名严重（1948 行只有 1610 个不同中文名），
    而 slug 与 PoB 的英文名是同一套命名。**同名多解的一律不判**（PoB 侧同名 4 例）：
    配不准就不该出结论，那是词缀对账那轮最贵的教训。
    """
    by_name: dict[str, list[PoBSpectre]] = {}
    for entry in pob:
        by_name.setdefault(entry.name_en, []).append(entry)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in poedb_rows:
        grouped.setdefault(str(row["poedb_slug"]).replace("_", " "), []).append(row)

    diffs: list[SpectreDiff] = []
    stats = {
        "paired_names": 0, "agree": 0, CONFLICT_VALUE: 0, CONFLICT_GRANULARITY: 0,
        "pob_only": 0, "poedb_only": 0, "ambiguous_pob_name": 0,
    }
    for name, rows in grouped.items():
        matches = by_name.get(name)
        if not matches:
            stats["poedb_only"] += 1
            continue
        if len(matches) > 1:
            stats["ambiguous_pob_name"] += 1
            continue
        entry = matches[0]
        stats["paired_names"] += 1
        if len(rows) > 1:
            # POEDB 记得更细：PoB 只建模了同名种类里的一个。不是分歧
            stats[CONFLICT_GRANULARITY] += 1
            diffs.append(_diff(entry, rows[0], CONFLICT_GRANULARITY, len(rows)))
            continue
        row = rows[0]
        same = (
            abs(int(row["life_pct"]) / 100 - entry.life_mult) < 1e-6
            and abs(int(row["damage_pct"]) / 100 - entry.damage_mult) < 1e-6
        )
        if same:
            stats["agree"] += 1
        else:
            stats[CONFLICT_VALUE] += 1
            diffs.append(_diff(entry, row, CONFLICT_VALUE, 1))

    stats["pob_only"] = len(by_name) - stats["paired_names"] - stats["ambiguous_pob_name"]
    return diffs, stats


def _diff(entry: PoBSpectre, row: dict[str, Any], cause: str, rows: int) -> SpectreDiff:
    return SpectreDiff(
        name_en=entry.name_en,
        name_zh=str(row["name_zh"]),
        cause=cause,
        poedb_life_pct=int(row["life_pct"]),
        poedb_damage_pct=int(row["damage_pct"]),
        pob_life_mult=entry.life_mult,
        pob_damage_mult=entry.damage_mult,
        poedb_rows=rows,
    )
