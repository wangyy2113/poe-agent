"""从 PoB 的 `Misc.lua` 抽取游戏常量与怪物属性表。

`Misc.lua` 是 `calc/` 的常量真源。它的每个块前面都有一行出处注释，指明数据来自
GGG 客户端的哪个 `.dat` / `.ot` 文件：

    -- From GameConstants.dat
    data.gameConstants = { ... }
    -- From Metadata/Characters/Character.ot
    data.characterConstants = { ["base_maximum_all_resistances_%"] = 75, ... }

这些注释必须一起抓下来。设计要求「常量附来源与版本」，而 `Character.ot` 这一层
出处比「来自 PoB」精确得多——出问题时能直接定位到 GGG 的原始表。

玩家侧的上限（抗性 75%、物理减伤 90%、格挡 75%）在 `characterConstants` 里，
不在 `gameConstants`。这两个块别搞混。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kb.sources.pob_lua import load_lua_file, parse_assignments

logger = logging.getLogger(__name__)

MISC_FILE = "Data/Misc.lua"
#: PoB 自己的 magic numbers 表（`data.misc`），与 `Data/Misc.lua` 不是一个文件。
MODULE_DATA_FILE = "Modules/Data.lua"

#: 从 `Modules/Data.lua` 的 `misc` 表里**按白名单**取的常量。
#:
#: 为什么不整表取（那张表有 40 多项）：
#:   1. **provenance 不同**。`Data/Misc.lua` 的每组都有 `-- From X.dat` 注释，
#:      而这张表开头就写着 `-- magic numbers`——里面既有公认的游戏真值
#:      （最大抗性上限 90、抗性下限 -200），也有 PoB 的建模取值。
#:      整表入库会把这条区别抹掉，而这个库的价值就在于「每个数字都说得清出处」。
#:   2. 表里有**表达式**（`data.characterConstants[...] / 60 / 100`），解析出来是未求值对象。
#:   3. 表里有**重复键**（`EnergyShieldRechargeBase` 出现两次，后一个覆盖前一个）——
#:      整表入库会静默取到其中一个。
#: 所以只取计算器当前真正需要的，缺了就在 `calc/` 里炸掉（那是刻意的设计）。
#: 白名单里**故意不收** `DamageReductionCap`：那一项在上游写成
#: `data.characterConstants["maximum_physical_damage_reduction_%"]`（一个引用，不是字面量），
#: 而被引用的那条常量早就在库里、还带着 GGG 原始表出处。收它只会多出一份出处更差的副本。
#: 抽取器对这种项会报「不是数值」——那正是白名单该有的反应。
MODULE_MISC_WHITELIST: tuple[str, ...] = (
    "ResistFloor",
    "MaxResistCap",
    # 受击结算的三个上限与一个效果值（第二十一轮）
    "EvadeChanceCap",
    "BlockChanceCap",
    "SuppressionChanceCap",
    "SuppressionEffect",
    # 回复（第二十三轮）。`EnergyShieldRechargeBase` 在那张表里**出现两次**：
    # 先是 `characterConstants["energy_shield_recharge_rate_per_minute_%"] / 60 / 100`
    # （表达式），紧接着又写了一遍字面量 `0.33`。Lua 后者覆盖前者，
    # 解析器同样取后者——两者相差 1% 以内，`tests/unit/test_recovery.py` 拿库里的
    # 那条 characterConstants 反算来证明这一点。这是白名单里唯一一个重复键。
    "EnergyShieldRechargeBase",
    "EnergyShieldRechargeDelay",
    "LeechRateBase",
    # 持续伤害三兄弟（第二十五轮）。注意**单位不统一**：流血写成 70（百分数），
    # 中毒 0.30 与点燃 0.9 写成小数——PoB 对流血除 100、对另两个不除。
    # 这条差异由 `tests/unit/test_ailment.py` 钉住，照抄成同一种单位就会差 100 倍。
    "BleedPercentBase",
    "BleedDurationBase",
    "PoisonPercentBase",
    "PoisonDurationBase",
    "IgnitePercentBase",
    "IgniteDurationBase",
    "DotDpsCap",
)

#: 出处注释：`-- From GameConstants.dat`
_PROVENANCE_RE = re.compile(r"^--\s*From\s+(.+?)\s*$", re.M)
#: 顶层赋值：`data.gameConstants = {`
_ASSIGN_RE = re.compile(r"^(?:local\s+)?data\.([A-Za-z_][A-Za-z0-9_]*)\s*=", re.M)

#: 怪物按等级的属性表：1..100 的数组，用等级作下标。
#: 单列成一类是因为取值方式不同（按等级索引而非按键取），
#: 混在标量里会让 calc 侧写出 `get("monsterLifeTable")` 这种取不到数的调用。
_LEVEL_TABLES = frozenset({
    "monsterEvasionTable", "monsterAccuracyTable", "monsterLifeTable",
    "monsterLifeTable2", "monsterLifeTable3", "monsterAllyLifeTable",
    "monsterDamageTable", "monsterAllyDamageTable", "monsterArmourTable",
    "monsterAilmentThresholdTable", "monsterPhysConversionMultiTable",
    "mapLevelLifeMult", "mapLevelBossLifeMult", "mapLevelBossAilmentMult",
    "goldRespecPrices",
})


@dataclass
class ConstantGroup:
    """一组常量。`dat_source` 是 GGG 原始表名，比「来自 PoB」精确。"""

    name: str
    dat_source: str = ""
    #: True = 出处注释紧贴在该赋值上方（确定）；False = 从上文延续而来（推断）
    dat_source_direct: bool = True
    line: int = 0
    #: 标量映射：键 → 数值
    scalars: dict[str, float] = field(default_factory=dict)
    #: 数组（按等级索引的表，或其它列表）
    array: list[float] = field(default_factory=list)
    #: 既非标量也非数组的部分（嵌套表），原样保留供诊断
    other: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        if self.array:
            return "level_table" if self.name in _LEVEL_TABLES else "array"
        if self.scalars:
            return "scalars"
        return "other"

    @property
    def count(self) -> int:
        return len(self.scalars) or len(self.array) or len(self.other)


def _provenance_map(text: str) -> dict[str, tuple[str, int, bool]]:
    """把每个 `data.X =` 关联到它上方最近的 `-- From ...` 注释。

    返回 `{名字: (出处, 行号, 是否直接标注)}`。

    注释**延续到下一条注释**：`Misc.lua` 里一条 `-- From DefaultMonsterStats.dat`
    覆盖了连续 11 个怪物属性表的赋值。早先「用完即清」的写法让其中 10 个丢了出处。

    但延续来的出处要标成 `inherited`：紧贴注释的那个块是确定的，后面几个是推断的。
    **错的出处比没有出处更糟**——它会让错误的溯源看起来可信。所以两者要能区分，
    导入时把 inherited 的列出来供人工核对。
    """
    lines = text.splitlines()
    out: dict[str, tuple[str, int, bool]] = {}
    current = ""
    fresh = False  # 当前注释是否还没被用过
    for idx, line in enumerate(lines, 1):
        pm = _PROVENANCE_RE.match(line.strip())
        if pm:
            current = pm.group(1)
            fresh = True
            continue
        am = _ASSIGN_RE.match(line)
        if am:
            out[am.group(1)] = (current, idx, fresh and bool(current))
            fresh = False
    return out


#: `Modules/Data.lua` 里的**嵌套**表，按白名单取数值字段。
#: `nonDamagingAilment` 是六种非伤害异常的默认值 / 上下限 / 持续时间——
#: 门槛型异常的计算离不开它（感电最多 50%、冰缓最多 30% 都在这张表里）。
#: `associatedType`（"Cold"/"Lightning"…）与 `alt` 是字符串与布尔，不入常量表；
#: 那两项由 `calc/threshold.py` 硬编码并用原文钉住（与剧情惩罚同一处理）。
MODULE_TABLE_WHITELIST: tuple[str, ...] = ("nonDamagingAilment",)


def extract_module_tables(pob_root: Path) -> tuple[dict[str, float], list[str]]:
    """从 `Modules/Data.lua` 取白名单里的**嵌套表**，展平成 `表.键.字段` 标量。

    只收数值字段：字符串（关联伤害类型）与布尔（是否为「替代」异常）不进常量表，
    因为 `game_constant.value_num` 是 REAL——硬塞会变成又一批取不出来的死数据
    （那正是这一轮修掉的问题）。
    """
    path = pob_root / "src" / MODULE_DATA_FILE
    problems: list[str] = []
    if not path.is_file():
        return {}, [f"{MODULE_DATA_FILE}：文件不存在"]
    table = parse_assignments(load_lua_file(path))
    out: dict[str, float] = {}
    for name in MODULE_TABLE_WHITELIST:
        nested = table.get(name)
        if not isinstance(nested, dict) or not nested:
            problems.append(f"{MODULE_DATA_FILE}：没有 {name} 表或它不是表")
            continue
        for entry, fields in nested.items():
            if not isinstance(fields, dict):
                continue
            for field, value in fields.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                out[f"{name}.{entry}.{field}"] = float(value)
    return out, problems


def extract_module_misc(pob_root: Path) -> tuple[dict[str, tuple[float, int | None]], list[str]]:
    """从 `Modules/Data.lua` 的 `misc` 表里按白名单取数值常量。

    返回 ``({键: (值, 行号)}, 问题)``。非数值（表达式）与不在白名单里的一律跳过；
    白名单里缺项算问题——那说明上游改名了，而计算器依赖它。

    行号是为了让引用带锚点、点开就是那一行。取不到就留 None，不猜一个。
    """
    path = pob_root / "src" / MODULE_DATA_FILE
    problems: list[str] = []
    if not path.is_file():
        return {}, [f"{MODULE_DATA_FILE}：文件不存在"]
    source = load_lua_file(path)
    table = parse_assignments(source)
    misc = table.get("misc")
    if not isinstance(misc, dict):
        return {}, [f"{MODULE_DATA_FILE}：没有 misc 表"]

    out: dict[str, tuple[float, int | None]] = {}
    for key in MODULE_MISC_WHITELIST:
        value = misc.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(
                f"{MODULE_DATA_FILE} 的 misc.{key} 不是数值（{value!r}）——上游可能改了写法"
            )
            continue
        out[key] = (float(value), _line_of(source, key))
    return out, problems


def _line_of(source: str, key: str) -> int | None:
    """`key = ` 所在行号（1-based）。找不到返回 None——引用宁可没有锚点，不要错的锚点。

    **取最后一次出现**：Lua 里后写的赋值覆盖先写的，解析器拿到的也是后者
    （`EnergyShieldRechargeBase` 在那张表里就写了两次，先表达式后字面量）。
    锚点指向第一次出现的话，点开看到的是一行**没有被采用**的代码——
    比没有锚点更误导人。
    """
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=", re.MULTILINE)
    line: int | None = None
    for match in pattern.finditer(source):
        line = source.count("\n", 0, match.start()) + 1
    return line


def extract(pob_root: Path) -> tuple[list[ConstantGroup], list[str]]:
    """抽取全部常量组。返回 (组列表, 问题列表)。"""
    path = pob_root / "src" / MISC_FILE
    problems: list[str] = []
    if not path.is_file():
        return [], [f"{MISC_FILE}：文件不存在"]

    text = load_lua_file(path)
    provenance = _provenance_map(text)
    table = parse_assignments(text)

    groups: list[ConstantGroup] = []
    for name, value in table.items():
        dat_source, line, direct = provenance.get(name, ("", 0, False))
        g = ConstantGroup(
            name=name, dat_source=dat_source, dat_source_direct=direct, line=line
        )
        if isinstance(value, dict):
            int_keys = sorted(k for k in value if isinstance(k, int))
            str_keys = [k for k in value if isinstance(k, str)]
            # Lua 数组表：连续整数键 1..n
            if int_keys and not str_keys and int_keys == list(range(1, len(int_keys) + 1)):
                nums = [value[k] for k in int_keys]
                if all(isinstance(v, (int, float)) for v in nums):
                    g.array = [float(v) for v in nums]
                else:
                    g.other = {str(k): value[k] for k in int_keys}
            else:
                for k in str_keys:
                    v = value[k]
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        g.scalars[k] = float(v)
                    else:
                        g.other[k] = v
                for k in int_keys:
                    g.other[str(k)] = value[k]
        else:
            g.other["value"] = value

        if not g.dat_source:
            problems.append(f"{name}：没有 `-- From ...` 出处注释，溯源只能到 PoB 文件级")
        elif not g.dat_source_direct:
            problems.append(
                f"{name}：出处 `{g.dat_source}` 从上文注释延续而来（非紧贴标注），"
                f"需人工确认"
            )
        if g.count == 0:
            problems.append(f"{name}：解析出 0 个条目，格式可能已变")
        groups.append(g)

    if not groups:
        problems.append(f"{MISC_FILE}：一个赋值都没解析到，文件格式可能已变")
    return groups, problems


def flatten(groups: list[ConstantGroup]) -> list[dict[str, Any]]:
    """展平成可入库的行。

    标量逐个成行——这样每个常量都能被单独引用。数组整体一行（100 个元素的
    等级表拆成 100 行没有意义，取值时也总是整表用）。
    """
    rows: list[dict[str, Any]] = []
    for g in groups:
        for key, val in g.scalars.items():
            rows.append({
                "key": f"{g.name}.{key}",
                "group_name": g.name,
                "dat_source": g.dat_source,
                "dat_source_direct": g.dat_source_direct,
                "kind": "scalar",
                "value_num": val,
                "value_json": None,
                "line": g.line,
            })
        if g.array:
            rows.append({
                "key": g.name,
                "group_name": g.name,
                "dat_source": g.dat_source,
                "dat_source_direct": g.dat_source_direct,
                "kind": g.kind,
                "value_num": None,
                "value_json": g.array,
                "line": g.line,
            })
        for key, val in g.other.items():
            # **数值型的 other 也要落 value_num**（第二十六轮修）。
            # 先前一律塞进 value_json，于是 `mapLevelLifeMult.66` 这类
            # 稀疏表条目在库里有值、却**取不出来**：`constants.get()` 见
            # value_num 为 NULL 直接抛 ConstantTypeError，`table()` 又不是数组。
            # 46 行常量因此是死数据——而抗性/异常门槛的计算正好需要它们。
            numeric = isinstance(val, (int, float)) and not isinstance(val, bool)
            rows.append({
                "key": f"{g.name}.{key}",
                "group_name": g.name,
                "dat_source": g.dat_source,
                "dat_source_direct": g.dat_source_direct,
                "kind": "scalar" if numeric else "other",
                "value_num": float(val) if numeric else None,
                "value_json": None if numeric else val,
                "line": g.line,
            })
    return rows
