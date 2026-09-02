"""PoB `Data/Essence.lua` —— 精华的**对账口径**，不是入库主源。

主源是 POEDB 的 `/cn/Essence`（中文、面向用户）；这一侧提供三样东西：
英文名、独立的 tier、以及每个精华覆盖的装备类别集合。

## 为什么值得单独对一次

`poedb_essence` 那 105 行如果只有一个来源，「105 条对不对」就只能靠页面自证。
PoB 是完全独立的一条链（导 GGG 的 `.dat`，不抓页面），所以两侧的
**条数、tier、装备类别数**逐项相等时，才算真的把这一维钉住了。

实测抓到的分歧只有一条，而它正是这层对账的价值所在：
`哀恸精华 / Essence of Desolation` 在 POEDB 页面上写 `Essence Tier: 0`，
PoB 写 `tier = 8`，而它的四个同类（浮夸/错乱/极恐/谵妄）两侧都写 8。
**页面自己前后不一致**——单看 POEDB 发现不了。

## 桥接：一把键就够

表键 `Metadata/Items/Currency/CurrencyEssenceHatred1` 逐字等于 POEDB
`data-hover` 里的元数据路径，**105/105 命中，零多解**。

这里差点多写一条 `slug → 英文名` 的退化桥接：先前探针在这一页上数到「8 张卡只给
tooltip 哈希」，于是我按幽魂那轮的写法准备了退化键。**实测那 8 张不在 `精华`
标签页里**——它们来自同一页的 `精华物品` / `精髓仓库页` 两个区块，而那两个区块
本来就不取。退化键因此一次都不会触发，是**带着解释性 docstring 的死代码**，已删。

教训与 `DomainSpec.pob_files` 同一条：**没有读者的分支迟早说假话**。
量的时候要量**真正会被解析的那个范围**，不是整页。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ESSENCE_LUA = "src/Data/Essence.lua"

#: `["Metadata/..."] = { name = "...", type = 0, tier = 1, mods = { ["Amulet"] = "...", }, },`
_ENTRY_RE = re.compile(r'^\s*\["(Metadata/Items/Currency/[^"]+)"\]\s*=\s*\{(.*)\}\s*,\s*$')
_NAME_RE = re.compile(r'name\s*=\s*"([^"]+)"')
_TIER_RE = re.compile(r'tier\s*=\s*(\d+)')
_TYPE_RE = re.compile(r'type\s*=\s*(\d+)')
_CLASS_RE = re.compile(r'\["([^"]+)"\]\s*=\s*"([^"]+)"')


class PobEssenceError(ValueError):
    """PoB 精华数据结构与预期不符。"""


@dataclass(frozen=True)
class PobEssence:
    key: str
    name_en: str
    tier: int
    kind: int
    #: 这个精华在哪些装备类别上有保底词缀（PoB 用英文类别名）
    item_classes: frozenset[str]


def load_pob_essences(root: Path) -> dict[str, PobEssence]:
    """读 `Data/Essence.lua`。名字重复即整批拒绝：英文名是对账报告里的人读标识，
    重名会让「哪一条对不上」说不清。"""
    path = root / ESSENCE_LUA
    if not path.is_file():
        raise PobEssenceError(f"找不到 {path}")
    out: dict[str, PobEssence] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        matched = _ENTRY_RE.match(line)
        if matched is None:
            continue
        key, body = matched.groups()
        name = _NAME_RE.search(body)
        tier = _TIER_RE.search(body)
        kind = _TYPE_RE.search(body)
        if not (name and tier and kind):
            raise PobEssenceError(f"{key} 缺 name/tier/type")
        out[key] = PobEssence(
            key=key,
            name_en=name.group(1),
            tier=int(tier.group(1)),
            kind=int(kind.group(1)),
            item_classes=frozenset(a for a, _ in _CLASS_RE.findall(body)),
        )
    if not out:
        raise PobEssenceError(f"{path} 里没解析出任何精华")
    names = [e.name_en for e in out.values()]
    if len(set(names)) != len(names):
        raise PobEssenceError("PoB 精华名有重复，对账报告将无法指认具体条目")
    return out
