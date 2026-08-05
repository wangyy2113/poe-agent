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
            rows.append({
                "key": f"{g.name}.{key}",
                "group_name": g.name,
                "dat_source": g.dat_source,
                "dat_source_direct": g.dat_source_direct,
                "kind": "other",
                "value_num": None,
                "value_json": val,
                "line": g.line,
            })
    return rows
