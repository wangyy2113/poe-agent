"""PoB `Classes/Item.lua` 的前后缀名额规则（对账用的 P2 侧）。

## 为什么这里是正则锚点而不是解析器

本库其它 PoB 抽取器读的都是 `src/Data/*.lua`——机器导出的**字面量表**，
`kb/sources/pob_lua.py` 一个字面量解析器就够。这一条不同：名额规则写在
`Classes/Item.lua` 的 **控制流**里：

    elseif self.rarity == "MAGIC" then          -- affixLimit = 2
    elseif self.rarity == "RARE" then
        self.affixLimit = (((self.type == "Jewel" and not (self.base.subType == "Abyss" and self.corrupted))
                            or self.type == "Graft") and 4 or 6)

对 `if/elseif` 求值等于把 PoB 的一段逻辑重实现一遍，而项目硬规则说**不重算 PoB 引擎**。
所以按**锚点原文**取那两个字面量，并断言原文仍在——与
`tests/unit/test_resistance.py` 钉住上游公式行号是同一个做法：
数据漂移有 `check_doc_facts.py` 拦，**公式漂移只能靠钉原文**。
上游改写导致锚点失配时**抛异常**，不静默沿用旧值。

## 三条规则，其中一条反直觉

`affixLimit` 是**前后缀之和**，PoB 在别处按 `affixLimit / 2` 平分到前后缀
（`Item.lua:1462`、`:2029`），所以 6 → 3+3、4 → 2+2、2 → 1+1。

* 魔法：2（1+1）
* 稀有 默认：6（3+3）
* 稀有 珠宝 / Graft：4（2+2）
* **稀有 已腐化的深渊珠宝：回到 6（3+3）** —— 判据里那个
  `not (subType == "Abyss" and corrupted)` 把这一支排除在 4 之外。
  这条最容易写反，单测按原文钉住。

## 与 POEDB 的分工

魔法 1+1、稀有 3+3 两侧都说，是**对账口径**；珠宝与 Graft 的 2+2 只有 PoB 说，
入库时标成 `pob_only`，不当成跨来源冲突。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ITEM_CLASS_FILE = "Classes/Item.lua"

#: 魔法分支：`elseif self.rarity == "MAGIC" then` 之后那个 `self.affixLimit = N`。
_MAGIC_RE = re.compile(
    r'self\.rarity\s*==\s*"MAGIC"\s*then'      # 分支头
    r".*?"
    r"self\.affixLimit\s*=\s*(?P<limit>\d+)\s*$",  # 该分支里的字面量赋值
    re.S | re.M,
)

#: 稀有分支那一行三元式。同时取出「谁是 4」与「默认是 6」。
_RARE_RE = re.compile(
    r'self\.rarity\s*==\s*"RARE"\s*then\s*\n'
    r"\s*self\.affixLimit\s*=\s*\(\(\(self\.type\s*==\s*\"(?P<jewel>\w+)\"\s*and\s*not\s*"
    r"\(self\.base\.subType\s*==\s*\"(?P<abyss>\w+)\"\s*and\s*self\.corrupted\)\)\s*or\s*"
    r"self\.type\s*==\s*\"(?P<graft>\w+)\"\)\s*and\s*(?P<small>\d+)\s*or\s*(?P<big>\d+)\)"
)


class AffixLimitSourceError(ValueError):
    """`Classes/Item.lua` 里的名额规则锚点失配；上游可能已改写。"""


@dataclass(frozen=True)
class PoBAffixLimit:
    """PoB 侧的一条名额规则。`item_kind` 与 POEDB 侧共用同一套取值。"""

    rarity: str
    item_kind: str
    prefix_limit: int
    suffix_limit: int
    line: int
    source_text: str


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def parse_affix_limits(text: str) -> list[PoBAffixLimit]:
    """从 `Classes/Item.lua` 原文抽出名额规则。"""
    magic = _MAGIC_RE.search(text)
    if magic is None:
        raise AffixLimitSourceError(
            f"{ITEM_CLASS_FILE} 里找不到 MAGIC 分支的 affixLimit 赋值；上游可能已改写"
        )
    rare = _RARE_RE.search(text)
    if rare is None:
        raise AffixLimitSourceError(
            f"{ITEM_CLASS_FILE} 里找不到 RARE 分支的 affixLimit 三元式；上游可能已改写"
        )

    magic_limit = int(magic.group("limit"))
    small = int(rare.group("small"))
    big = int(rare.group("big"))
    for name, value in (("MAGIC", magic_limit), ("RARE 小", small), ("RARE 默认", big)):
        if value % 2 != 0:
            # PoB 在别处按 affixLimit / 2 平分到前后缀，奇数说明我读错了那个字面量
            raise AffixLimitSourceError(
                f"{ITEM_CLASS_FILE} 的 {name} affixLimit={value} 不是偶数，"
                "无法按 PoB 的 affixLimit/2 平分到前后缀"
            )

    magic_line = _line_of(text, magic.start("limit"))
    rare_line = _line_of(text, rare.start())
    rare_src = re.sub(r"\s+", " ", rare.group(0)).strip()

    out = [
        PoBAffixLimit(
            rarity="魔法", item_kind="default",
            prefix_limit=magic_limit // 2, suffix_limit=magic_limit // 2,
            line=magic_line, source_text=f'rarity == "MAGIC" → affixLimit = {magic_limit}',
        ),
        PoBAffixLimit(
            rarity="稀有", item_kind="default",
            prefix_limit=big // 2, suffix_limit=big // 2,
            line=rare_line, source_text=rare_src,
        ),
    ]
    # 珠宝与 Graft 走小额度；**已腐化的深渊珠宝被判据排除在外**，因此回到默认额度。
    for kind in ("jewel", "graft"):
        out.append(
            PoBAffixLimit(
                rarity="稀有", item_kind=kind,
                prefix_limit=small // 2, suffix_limit=small // 2,
                line=rare_line, source_text=rare_src,
            )
        )
    out.append(
        PoBAffixLimit(
            rarity="稀有", item_kind="jewel_abyss_corrupted",
            prefix_limit=big // 2, suffix_limit=big // 2,
            line=rare_line, source_text=rare_src,
        )
    )
    return out


def load_affix_limits(path: Path) -> list[PoBAffixLimit]:
    return parse_affix_limits(path.read_text(encoding="utf-8"))
