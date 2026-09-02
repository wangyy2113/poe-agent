"""POEDB 精华页（`/cn/Essence`）的**精华物品本身**。

## 这一维缺的从来不是词缀，是物品

`mod_affix` 里早就有 **5985 条** `category='essence'` 的词缀，带着
`name_zh='憎恨之低语精华'` × `item_class_zh='项链'` × `text_zh`，
而且它们**已经渲染进词缀组卡**（实测 57 个 chunk 含「憎恨之低语精华」）。
所以「憎恨之低语精华在戒指上给什么」一直答得出来。

`essence` 域的 `partial_note` 也一直写着缺口是「精华**物品本身**的等级、
掉落与兑换成本」——这一轮补的正是那个「物品本身」：

    tier（0..8）｜ 堆叠上限 ｜ 等级上限 ｜ 基础效果原文

**动手前先查了库**，否则这里会多出一张 2310 行、与 `mod_affix` 逐条重复的表。
这是「先翻缓存再动手」在数据侧的同一条：**先查库，再判断缺什么。**

## 实测出来的机制分界（tier 4 与 5 之间）

| | tier 0–4（41 件） | tier 5–8（64 件） |
| --- | --- | --- |
| 基础效果 | 升级一件普通物品至稀有物品并且确保一项特定属性 | 确保一项特定属性，升级一件普通物品至稀有物品**或者重置一件稀有物品的属性** |
| 等级上限 | 有（15 / 35 / 45 / 60 / 75） | **无** |

低阶精华只能升白装且限等级，高阶还能重铸稀有装——这条差异写在效果原文里，
不由代码推断。腐化专属的 5 件（哀恸 / 极恐 / 浮夸 / 谵妄 / 错乱）里 4 件是 tier 8，
**哀恸精华是页面上唯一的 tier 0，且带 15 级上限**——它同时是本轮对账抓到的那条分歧。

## 三条实测判据

- **取哪个区块看标签页 id，既不是位置也不是选中态。** 这一页有 9 个 `tab-pane`，
  其中 4 个都含精华卡（`精华` 105、`精华物品` 104、`精髓仓库页` 104、`Acronym` 2），
  而**选中的是 `MinimapIcons`**（0 张卡）。按选中态取会得到空，按位置取会撞上
  另外三个近似区块。判据是标签页自己声明的条数：`#精华 /105`。
- **单个装备类别的行会被误判成基础效果**。分组行的形状是
  `箭袋, 头部, 胸甲: +(6—11)% 冰霜抗性`，而 `弓: 此物品上装备的弓技能石等级+2`
  只有一个类别、两个字符。类别名最短 1 字（`弓` `爪`），所以下界只能是 1。
  实测踩过：下界写 2 时那 3 条被并进基础效果文本。
- **量的时候要量真正会被解析的那个范围。** 先前的探针在**整页**上数到
  「8 张卡的 `data-hover` 只给 tooltip 哈希」，于是我按幽魂那轮的写法准备了
  一条 slug → 英文名的退化桥接。实测**那 8 张不在 `精华` 标签页里**——
  它们属于同一页的 `精华物品` / `精髓仓库页`，而那两个区块本来就不取。
  这一页的 105 张卡**全部**给出元数据路径，退化键一次都不会触发，已删。

  同一个错在这一轮出现了两次：`哀恸精华` 的等级上限也是先按整页统计
  得出「没有上限」，实际它自己那张卡上写着 15。

## 页面自证

三处都不依赖外部来源：标签页标题里的 `/105`、每张卡恰好 **22** 个装备类别、
以及 105 个精华名与 `mod_affix` 的 105 个**完全重合**。对不上就整页拒绝——
静默少收等于漏答，而这一维的用户问题正是「有没有这个精华」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from selectolax.parser import HTMLParser

from kb.sources.poedb_cards import CardStructureError, assert_chinese, parse_cards, text_of

ESSENCE_URL = "https://poedb.tw/cn/Essence"

#: 精华卡所在标签页的 id。**不是**选中的那个（选中的是 `MinimapIcons`）。
PANE_ID = "精华"
CARD_SELECTOR = "a.item_currency"

#: 每个精华覆盖的装备类别数。PoB 与 POEDB 两侧都是 22，对不上说明页面结构变了。
CLASSES_PER_ESSENCE = 22

#: 标签页标题里的自证条数：`精华 /105`。
_PANE_COUNT_RE = re.compile(r"/(\d+)\s*$")
#: `箭袋, 头部, 胸甲: +(6—11)% 冰霜抗性`。
#: **下界是 1 不是 2**：`弓` `爪` 都是单字类别名，写 2 会把它们并进基础效果。
_GROUP_RE = re.compile(r"^([一-鿿][一-鿿,，、 ]{0,59})[:：]\s*(.+)$")
#: `属性限制等级需求35与以下物品`
_LEVEL_CAP_RE = re.compile(r"等级需求\s*(\d+)")
_TIER_PROP = "Essence Tier"


class EssenceStructureError(CardStructureError):
    """精华页结构与预期不符。"""


@dataclass(frozen=True)
class EssenceMod:
    """一条「这些装备类别上保底出这个词缀」。按页面原始分组存，不拆成 22 行。

    拆开会与 `mod_affix` 的 5985 条逐条重复；保留分组则是**页面自己的表述**，
    卡片照着渲染就是用户在 POEDB 上看到的样子。
    """

    group_index: int
    item_classes: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class Essence:
    key: str
    name_zh: str
    slug: str
    tier: int
    stack_max: int | None
    #: tier 0–4 的 41 件才有；tier 5 起没有等级上限，**留 None 而不是填 0**
    level_cap: int | None
    base_effect: str
    mods: tuple[EssenceMod, ...]

    @property
    def item_class_count(self) -> int:
        return sum(len(m.item_classes) for m in self.mods)


def _pane(tree: HTMLParser) -> tuple[object, int | None]:
    """取 `id=精华` 的标签页，并返回它的标签标题里声明的条数。"""
    panes = [p for p in tree.css("div.tab-pane") if p.attributes.get("id") == PANE_ID]
    if not panes:
        raise EssenceStructureError(f"精华页上找不到 id={PANE_ID} 的标签页")
    declared: int | None = None
    for nav in tree.css("a.nav-link"):
        if str(nav.attributes.get("href") or "").lstrip("#") == PANE_ID:
            matched = _PANE_COUNT_RE.search(text_of(nav))
            declared = int(matched.group(1)) if matched else None
            break
    return panes[0], declared


def _split_effect(lines: list[str]) -> tuple[str, int | None, tuple[EssenceMod, ...]]:
    """把 `explicitMod` 各行分成「基础效果 / 等级上限 / 逐类别保底词缀」三类。"""
    head: list[str] = []
    mods: list[EssenceMod] = []
    level_cap: int | None = None
    for line in lines:
        matched = _GROUP_RE.match(line)
        if matched is not None:
            classes = tuple(
                c.strip() for c in re.split(r"[,，、]", matched.group(1)) if c.strip()
            )
            mods.append(EssenceMod(len(mods), classes, matched.group(2).strip()))
            continue
        cap = _LEVEL_CAP_RE.search(line)
        if cap is not None:
            level_cap = int(cap.group(1))
            continue
        head.append(line)
    return " ".join(head).strip(), level_cap, tuple(mods)


def parse_essences(html: str, *, page_url: str = ESSENCE_URL) -> tuple[Essence, ...]:
    """解析精华页的 `精华` 标签页，返回 105 个精华物品。

    条数与页面自己声明的 `/105` 对不上就整页拒绝。
    """
    tree = HTMLParser(html)
    pane, declared = _pane(tree)
    cards = parse_cards(pane.html or "", CARD_SELECTOR)
    assert_chinese([c.name for c in cards.values()], what="精华页")

    out: list[Essence] = []
    for key, card in cards.items():
        raw_tier = card.properties.get(_TIER_PROP, "").strip()
        if not raw_tier.isdigit():
            raise EssenceStructureError(f"{card.name} 的 {_TIER_PROP} 不是数字：{raw_tier!r}")
        lines = [x.strip() for x in (card.effect or "").split(" / ") if x.strip()]
        base_effect, level_cap, mods = _split_effect(lines)
        if not base_effect:
            raise EssenceStructureError(f"{card.name} 没有基础效果文本")
        out.append(Essence(
            key=key,
            name_zh=card.name,
            slug=card.slug,
            tier=int(raw_tier),
            stack_max=card.stack_max,
            level_cap=level_cap,
            base_effect=base_effect,
            mods=mods,
        ))

    if declared is not None and len(out) != declared:
        raise EssenceStructureError(
            f"精华条数与页面声明不符：解析 {len(out)}，标签页声明 {declared}"
        )
    bad = [e.name_zh for e in out if e.item_class_count != CLASSES_PER_ESSENCE]
    if bad:
        raise EssenceStructureError(
            f"{len(bad)} 个精华的装备类别数不是 {CLASSES_PER_ESSENCE}："
            f"{'、'.join(bad[:5])}"
        )
    _ = page_url  # 溯源由调用方写进表，这里只做结构校验
    return tuple(out)
