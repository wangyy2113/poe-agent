"""商店配方（POEDB `/cn/Vendor_recipe_system`，第五十一轮）。

「怎么用装备换混沌石」「点金石从哪来」是最常问的一类获取途径，而在这之前
本库一条都没有——覆盖层里通货、精华、圣甲虫三个域的 `partial_note` 都写着
「获取途径未采集」。

**这一页一直在缓存里**（179KB、21 张表、201 行），从来没被抽取过。
它同一张表头 `贡品 ｜ 你的物品 ｜ Note` 也出现在宝石详情页上（第四十四轮清点时
数到 32 处，当时记作「商店配方」放过了）。这是「停在一页上就下缺来源的结论」
那个错误的**反面**：这次先把缓存翻了一遍，才没有去抓 113 张通货详情页。

## 三列的含义按页面原文，不改写

    贡品        换到手的东西（点金石、增幅石…）
    你的物品     要付出去的东西（1x 后悔石、6 词缀稀有物品…）
    Note        补充说明，多是在哪买（「章节 3 卡尔莉萨」）

**方向不能弄反**：`贡品` 是**产出**、`你的物品` 是**投入**。写反了会把
「2 个重铸石换 1 个后悔石」说成「1 个后悔石换 2 个重铸石」——一个看起来通顺的错答案。

## 类别取自区块标题，行数由标题自证

每个区块标题形如 `传奇 物品 Recipe /23`，斜杠后是**页面自己声明的行数**，
正好当自证判据（与第四十五轮搭配关系那张表同一条）。
**类别名不唯一**：`賣物品給商人 Recipe` 出现两次（5 行与 2 行两张表），
所以主键不能只用类别名。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from selectolax.parser import HTMLParser

from kb.sources.poedb_gem_page import _text

#: 配方表的定位判据。按表头定位而不是按下标——这一页上有 21 张同构表。
RECIPE_TABLE_HEAD = ("贡品", "你的物品", "Note")

#: 区块标题：`传奇 物品 Recipe /23`。斜杠后是页面声明的行数。
_BLOCK_RE = re.compile(r"^(.*?Recipe)\s*/\s*(\d+)\s*$")


class VendorRecipeError(ValueError):
    """页面结构与预期不符。定位失败即拒绝，不猜列。"""


@dataclass(frozen=True)
class VendorRecipe:
    """一条商店配方。**方向：`result` 是产出，`ingredients` 是投入。**"""

    #: 区块标题里的类别（`传奇 物品 Recipe`）
    category: str
    #: 同类别可能有多张表（`賣物品給商人 Recipe` 出现两次），用它区分
    block_index: int
    #: 这一行在本块里的序号，主键的一部分
    row_index: int
    result: str
    ingredients: str
    note: str = ""


def _block_title(table) -> str | None:  # noqa: ANN001
    """表所属区块的标题。沿 parent 链找 `h5.card-header`。"""
    parent = table.parent
    for _ in range(6):
        if parent is None:
            return None
        header = parent.css_first("h5.card-header")
        if header is not None:
            return _text(header)
        parent = parent.parent
    return None


def parse_vendor_recipes(html: str) -> tuple[VendorRecipe, ...]:
    """解析整页的商店配方。

    每块都跑一次自证：**解析出的行数必须等于标题里声明的数**。
    对不上直接抛错而不是少收几行——静默少收等于漏答，而这一维的用户问题
    正是「有没有这个配方」。
    """
    tree = HTMLParser(html)
    out: list[VendorRecipe] = []
    seen_blocks = 0
    for table in tree.css("table"):
        if tuple(_text(th) for th in table.css("thead th")) != RECIPE_TABLE_HEAD:
            continue
        title = _block_title(table) or ""
        matched = _BLOCK_RE.match(title)
        if matched is None:
            raise VendorRecipeError(
                f"配方表的区块标题 {title!r} 不是「<类别> Recipe /<行数>」的形状"
            )
        category, declared = matched.group(1).strip(), int(matched.group(2))
        rows = table.css("tbody tr")
        if len(rows) != declared:
            raise VendorRecipeError(
                f"区块 {category!r} 声明 {declared} 行，实际解析到 {len(rows)} 行"
            )
        for row_index, row in enumerate(rows):
            cells = row.css("td")
            if len(cells) != len(RECIPE_TABLE_HEAD):
                raise VendorRecipeError(
                    f"{category!r} 第 {row_index + 1} 行有 {len(cells)} 格，应为 3 格"
                )
            result, ingredients, note = (_text(c) for c in cells)
            if not result or not ingredients:
                # 产出或投入缺一个，这条配方就说不出「拿什么换什么」——跳过并让调用方登记
                continue
            out.append(VendorRecipe(
                category=category, block_index=seen_blocks, row_index=row_index,
                result=result, ingredients=ingredients, note=note,
            ))
        seen_blocks += 1
    # 判据是「有没有找到表」，不是「有没有收到行」——两者是不同的失败。
    # 表都没有 = 页面结构变了（该报错）；表在但行都缺列 = 数据问题（返回空，由调用方登记）。
    if seen_blocks == 0:
        raise VendorRecipeError("页面里没有找到任何商店配方表")
    return tuple(out)
