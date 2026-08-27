"""POEDB `/cn/Modifiers` 的**前后缀名额规则**抽取器。

## 这一维不是「只有 PoB 说得清」

`craft/feasibility.py` 从第一版起就写着「前后缀容量规则尚未结构化入库」，于是
`check_mod_combination` 的三态里 `possible` 是死的。补这一维时最容易的假设是
「只能去 PoB 的代码里抠」——**实测不是**：POEDB 词缀页用四条 `<li>` 明说了规则，
而且这一页早就在缓存里。

    <li><span class="item_magic">魔法物品</span>最多一随机前缀一随机后缀</li>
    <li><span class="item_rare">稀有物品</span>最多三随机前缀三随机后缀</li>
    <li><span class="item_unique">传奇物品</span>不受前后缀的数量限制</li>
    <li>药剂因为只有魔法物品, 所以最多一随机前缀一随机后缀</li>

所以这一维有 **P1 来源**，PoB（P2）那份是用来对账的，不是唯一依据。

## 按句式匹配，不按下标取 `<li>`

同一页有 900 多个 `<li>`。判据写成「稀有度 + 最多 N 随机前缀 N 随机后缀」这个**句式**，
上游改排版不会错位；真改了措辞就匹配不上，于是**报错**而不是悄悄少一条规则。

## 页面还说了一条本模块不取的事实

「部分物品会有固定词缀……固定词缀不受前后缀的数量限制」——固有词缀不占名额。
本库的 `mod_affix.gen_type=0`（其他生成位）大致对应这一类，但两者**不是同一个划分**
（`gen_type=0` 里还有腐化、附魔、灭界者等各有各的槽位规则）。所以不把这句话
翻译成一条容量规则，只让调用方在遇到 `gen_type=0` 时判 `unknown`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from selectolax.parser import HTMLParser

MODIFIERS_URL = "https://poedb.tw/cn/Modifiers"

#: 中文数字 → 整数。规则里只会出现个位数。
_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6}

#: 「稀有物品最多三随机前缀三随机后缀」
_LIMIT_RE = re.compile(
    r"(?P<rarity>魔法|稀有|传奇)物品最多(?P<prefix>[一二两三四五六])随机前缀"
    r"(?P<suffix>[一二两三四五六])随机后缀"
)
#: 「传奇物品不受前后缀的数量限制」
_UNLIMITED_RE = re.compile(r"(?P<rarity>魔法|稀有|传奇)物品不受前后缀的数量限制")
#: 「药剂因为只有魔法物品, 所以最多一随机前缀一随机后缀」
_FLASK_RE = re.compile(
    r"药剂因为只有(?P<rarity>魔法|稀有|传奇)物品.{0,4}所以最多"
    r"(?P<prefix>[一二两三四五六])随机前缀(?P<suffix>[一二两三四五六])随机后缀"
)


class ModifiersStructureError(ValueError):
    """规则句式与预期不符；调用方必须拒绝整批写入。"""


@dataclass(frozen=True)
class AffixLimitRule:
    """一条名额规则。`prefix_limit is None` 表示不受限制（传奇）。"""

    rarity: str
    item_kind: str
    prefix_limit: int | None
    suffix_limit: int | None
    source_text: str

    @property
    def unlimited(self) -> bool:
        return self.prefix_limit is None and self.suffix_limit is None


def _text(node) -> str:  # noqa: ANN001
    return re.sub(r"\s+", " ", node.text(deep=True)).strip()


def extract_affix_limits(html: str) -> list[AffixLimitRule]:
    """抽取前后缀名额规则。至少要抽到魔法与稀有两条，否则判结构变了。"""
    tree = HTMLParser(html)
    rules: dict[tuple[str, str], AffixLimitRule] = {}

    for li in tree.css("li"):
        text = _text(li)
        if len(text) > 160:
            continue

        matched = _FLASK_RE.search(text)
        if matched:
            # 药剂只有魔法物品，所以它的名额等于魔法物品的名额
            key = ("魔法", "flask")
            rules.setdefault(
                key,
                AffixLimitRule(
                    rarity="魔法",
                    item_kind="flask",
                    prefix_limit=_CN_DIGITS[matched.group("prefix")],
                    suffix_limit=_CN_DIGITS[matched.group("suffix")],
                    source_text=text,
                ),
            )
            continue

        matched = _LIMIT_RE.search(text)
        if matched:
            key = (matched.group("rarity"), "default")
            rules.setdefault(
                key,
                AffixLimitRule(
                    rarity=matched.group("rarity"),
                    item_kind="default",
                    prefix_limit=_CN_DIGITS[matched.group("prefix")],
                    suffix_limit=_CN_DIGITS[matched.group("suffix")],
                    source_text=text,
                ),
            )
            continue

        matched = _UNLIMITED_RE.search(text)
        if matched:
            key = (matched.group("rarity"), "default")
            rules.setdefault(
                key,
                AffixLimitRule(
                    rarity=matched.group("rarity"),
                    item_kind="default",
                    prefix_limit=None,
                    suffix_limit=None,
                    source_text=text,
                ),
            )

    required = {("魔法", "default"), ("稀有", "default")}
    missing = required - set(rules)
    if missing:
        raise ModifiersStructureError(
            "词缀页上没有抽到必须的名额规则："
            + "、".join(f"{r}·{k}" for r, k in sorted(missing))
            + "（页面措辞可能已改，请核对后再改判据）"
        )
    return sorted(rules.values(), key=lambda r: (r.rarity, r.item_kind))
