"""PoB `Data/Pantheons.lua` 抽取器：万神殿的结构、英文名、数值与 GGG stat id。

## 这个文件一直都在

`kb/coverage.py` 的 `pantheon` 域从第十七轮起就写着「缺的是**来源**」，而那条
spec **自己就声明了** `pob_files=("Data/Pantheons.lua",)`——本机 PoB 检出里
这个文件一直在。`pob_files` 至今没有任何运行时消费者（纯文档字段），
所以没人回头看它。判断「做不了」之前先确认卡点是数据、口径，还是自己的印象。

## stat id 在注释里，要单独捞

    mods = {
        -- degen_effect_+%
        [1] = { line = "10% reduced Damage taken from Damage Over Time", value = { -10 }, },
    },

`pob_lua` 的词法器按设计丢弃注释，而这一行正是**跨来源对账的键**：同一个
stat id 也出现在 POEDB 逐神页的 `<li>` 里。所以用一次正则把它捞回来。

**按 `line` 原文建映射，不按文件顺序 zip。** 实测 41 条 line 与 41 个 stat id
两两互不重复，因此「行文 → stat id」是一一对应且与顺序无关的；
顺序 zip 看起来也能跑通，但上游哪天插一条没有注释的词缀就会**整体错位一格**，
而错位后的每一条都仍然「有 stat id」——那是最难发现的一类错。
查不到就抛异常，不静默留空。

## `value` 是 stat 的**原生单位**，不是行文里显示的那个数

    -- life_regeneration_rate_per_minute_%_while_stationary
    [1] = { line = "Regenerate 2% of Life per second while stationary", value = { 120 }, },

120 是**每分钟**，行文写的是每秒 2%。所以 `value` 不能拿去与页面上显示的数字配对
（第三十五轮实测，图克哈玛第二层）。它只用于与 POEDB 的 Attr 表对账——
那张表给的同样是原生值 120，两侧口径一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from kb.sources.pob_lua import load_lua_file, parse_return_table

PANTHEON_FILE = "Data/Pantheons.lua"

#: 注释里的 stat id + 紧随其后的 `line` 原文。`%`/`+`/`-` 都是 stat id 的合法字符
#: （`base_stun_recovery_+%`、`additional_chaos_resistance_against_damage_over_time_%`）。
_STAT_ID_RE = re.compile(
    r"--\s*([A-Za-z0-9_%+\-]+)\s*\r?\n"
    r"\s*\[\d+\]\s*=\s*\{\s*line\s*=\s*\"((?:[^\"\\]|\\.)*)\"",
)

#: 实测 12 位神祇 / 32 层神力 / 41 条词缀（PoB 2.66.x，游戏 3.29）。
#: 下限护栏用的是「主神 4 位、次神 8 位」这个游戏规则本身，不是当前条数。
MAJOR_GODS = 4
MINOR_GODS = 8


class PantheonStructureError(ValueError):
    """`Pantheons.lua` 的结构与预期不符。修抽取器，不要放宽断言。"""


@dataclass(frozen=True)
class PoBSoulMod:
    """一条神力词缀。`value` 是 stat 原生单位，**不等于行文里显示的数**。"""

    stat_id: str
    value: float
    line_en: str


@dataclass(frozen=True)
class PoBSoul:
    """一层神力。`index` 1 是基础神力，2..4 是打头目解锁的强化。"""

    index: int
    name_en: str
    mods: tuple[PoBSoulMod, ...]


@dataclass(frozen=True)
class PoBGod:
    """一位神祇。`key` 就是 Lua 表键，也是 POEDB Attr 表的 `ID`。"""

    key: str
    is_major: bool
    souls: tuple[PoBSoul, ...]

    @property
    def base_name_en(self) -> str:
        """基础神力英文名，如 `Soul of Arakaali`。"""
        return self.souls[0].name_en

    @property
    def poedb_slug(self) -> str:
        """POEDB 逐神页 slug。由英文名推出（`Soul of the Brine King` →
        `Soul_of_the_Brine_King`），**推出来之后必须由页面上的 `ID` 与 `Name`
        两道判据自证**，见 `kb/sources/poedb_pantheon.py`。
        """
        return self.base_name_en.replace(" ", "_")


def stat_ids_by_line(text: str) -> dict[str, str]:
    """`line` 原文 → GGG stat id。

    重复的 line 会让映射产生歧义，直接报错而不是让后来者覆盖前者。
    """
    out: dict[str, str] = {}
    for stat_id, line in _STAT_ID_RE.findall(text):
        line = line.replace('\\"', '"')
        if line in out and out[line] != stat_id:
            raise PantheonStructureError(
                f"同一条词缀原文对应两个 stat id：{line!r} → {out[line]!r} / {stat_id!r}"
            )
        out[line] = stat_id
    return out


def parse_pantheons(text: str) -> list[PoBGod]:
    """解析 `Pantheons.lua`。返回按文件顺序排列的神祇。"""
    raw = parse_return_table(text)
    by_line = stat_ids_by_line(text)

    gods: list[PoBGod] = []
    for key, god in raw.items():
        if not isinstance(god, dict) or "souls" not in god:
            raise PantheonStructureError(f"{key} 不是预期的神祇表：{god!r}")
        souls: list[PoBSoul] = []
        for idx in sorted(god["souls"]):
            soul = god["souls"][idx]
            mods: list[PoBSoulMod] = []
            for mod_idx in sorted(soul["mods"]):
                mod = soul["mods"][mod_idx]
                line = str(mod["line"])
                stat_id = by_line.get(line)
                if not stat_id:
                    # 注释是对账键，缺了就没法与 POEDB 比对——不静默留空
                    raise PantheonStructureError(
                        f"{key} 第 {idx} 层的词缀在注释里找不到 stat id：{line!r}"
                    )
                values = mod["value"]
                if len(values) != 1:
                    raise PantheonStructureError(
                        f"{key} 第 {idx} 层的词缀有 {len(values)} 个值，预期 1 个：{line!r}"
                    )
                mods.append(
                    PoBSoulMod(
                        stat_id=stat_id,
                        value=float(values[sorted(values)[0]]),
                        line_en=line,
                    )
                )
            souls.append(
                PoBSoul(index=int(idx), name_en=str(soul["name"]), mods=tuple(mods))
            )
        gods.append(
            PoBGod(key=str(key), is_major=bool(god["isMajorGod"]), souls=tuple(souls))
        )

    majors = sum(1 for g in gods if g.is_major)
    minors = len(gods) - majors
    if majors != MAJOR_GODS or minors != MINOR_GODS:
        raise PantheonStructureError(
            f"神祇数量与游戏规则不符：主神 {majors}（预期 {MAJOR_GODS}）、"
            f"次神 {minors}（预期 {MINOR_GODS}）"
        )
    return gods


def load_pantheons(path: Path) -> list[PoBGod]:
    """从 `Data/Pantheons.lua` 读取并解析。"""
    return parse_pantheons(load_lua_file(path))
