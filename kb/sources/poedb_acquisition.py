"""获取途径（POEDB 详情页的 `Acquisition` 区块，第五十四轮）。

「哪张命运卡能出头猎」「托沃卧怎么合」这类问题在这之前一条都答不了。

## 它不只是命运卡

三列是 `来源 ｜ Ingredient ｜ Offer`，实测有两种形态：

    浑沌性情    —              混沌石        ← 命运卡：集齐这张卡换到 Offer
    托沃崩      托沃的祝福      托沃卧        ← 升级配方：拿 来源 + Ingredient 换 Offer

所以这一维叫**获取途径**而不是「命运卡」：`Ingredient` 非空时它是一条合成/升级路线。
把两者混成「命运卡」会让「托沃卧怎么来」得到一个错答案。

## 覆盖是**部分**的，这一点必须传下去

2026-09-01 实测（全部来自已缓存页，零新增抓取）：

    唯一装备   1342 页里 264 页有   338 行
    通货        113 页里  19 页有    36 行
    技能石      820 页里   9 页有    13 行

也就是说**八成的唯一装备没有这一段**。「库里查不到」与「没有卡能出它」
在用户那边分不开，所以工具必须逐条说清是哪一种——与宝石那 152 颗
「详情页未给出使命表」是同一条规矩。
"""

from __future__ import annotations

from dataclasses import dataclass

from selectolax.parser import HTMLParser

from kb.sources.poedb_gem_page import _text

#: `Acquisition` 区块里那张表的表头。按表头定位而不是按下标——
#: 这一页上还有商店配方、词缀、工艺台好几张表。
ACQUISITION_TABLE_HEAD = ("来源", "Ingredient", "Offer")


class AcquisitionError(ValueError):
    """区块结构与预期不符。定位失败即拒绝，不猜列。"""


@dataclass(frozen=True)
class AcquisitionRoute:
    """一条获取途径。

    `ingredient` 为空 → 命运卡（集齐 `source_zh` 换到 `offer_zh`）；
    非空 → 升级/合成配方（`source_zh` + `ingredient` 换到 `offer_zh`）。
    """

    #: 来源名（命运卡名，或合成配方里的主料）
    source_zh: str
    #: 来源的 POEDB slug，取自链接；没有链接就是空串
    source_slug: str
    #: 附加材料。空串表示这是一条命运卡途径
    ingredient: str
    #: 换到手的东西
    offer_zh: str

    @property
    def kind(self) -> str:
        return "recipe" if self.ingredient else "divination_card"


def parse_acquisition(html: str) -> tuple[AcquisitionRoute, ...]:
    """解析一页上的 `Acquisition` 表。没有就返回空元组。

    返回空而不是抛异常：**八成的页面本来就没有这一段**，那是数据缺口
    而不是结构错误，由调用方登记。
    """
    tree = HTMLParser(html)
    out: list[AcquisitionRoute] = []
    for table in tree.css("table"):
        if tuple(_text(th) for th in table.css("thead th")) != ACQUISITION_TABLE_HEAD:
            continue
        for row in table.css("tbody tr"):
            cells = row.css("td")
            if len(cells) != len(ACQUISITION_TABLE_HEAD):
                raise AcquisitionError(
                    f"Acquisition 表某行有 {len(cells)} 格，应为 3 格"
                )
            source, ingredient, offer = (_text(c) for c in cells)
            if not source or not offer:
                # 缺来源或缺产出就说不出「拿什么换什么」，跳过
                continue
            link = cells[0].css_first('a[href^="/cn/"]')
            slug = (link.attributes.get("href") or "").rsplit("/", 1)[-1] if link else ""
            out.append(AcquisitionRoute(
                source_zh=source, source_slug=slug,
                ingredient=ingredient, offer_zh=offer,
            ))
    return tuple(out)
