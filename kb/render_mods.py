"""词缀 → 知识卡片。

## 卡片粒度 = (底材类别, 词缀组)，来源作为卡内分节

先前的粒度是 (底材类别, **来源**, 前后缀, 词缀组)，共 24,418 张。问题是同一个
(底材类别, 词缀组) 在多个来源下各有一张卡，文本高度雷同：实测 1,064 个分组横跨
3 个来源、655 个横跨 4 个、最多的横跨 10 个。后果有两层：

1. **检索候选被近似重复挤满。** 「戒指的抗性词缀权重」的前 20 名曾被虚空忆境 /
   工艺工作台 / 异度天灾占满，常规掉落池的完整梯度根本进不了候选集。当时是靠
   `retrieval/hybrid._diversify()` 限流来补偿的。
2. **同一问题的答案被拆散。** 36.4% 的词缀存在来源歧义（同一数值既可能是掉落也
   可能是工艺台做的），而正确的回答形态是**并列各来源**。来源分在不同卡片上时，
   模型得自己把几张卡拼起来才能说清，很容易只看到一张就下结论。

合并后 13,250 张（降 46%），一张卡就能同时回答「掉落池 T1-T8 / 工艺台固定值 /
精华固定值」。`_diversify()` 与 `_prefer_mod_source()` 这两个补偿层随之删除——
粒度修对了，补偿就不需要了。

**保留 `item_class_page` 不合并属性变体**：7 个 `Body_Armours_*` 的词缀池确有差异，
按中文类别名合并会降到 8,311 张但损失精度。这里不做这一步。

## 卡片只承担语义检索入口

精确数值查询走 `tools/impl/mods.py` 的 `mod_tiers`（直查 `mod_affix`，逐组渲染，
不受卡片截断影响）。卡片的职责是让「跟法术压制有关的词缀有哪些」这类
**语义发现**类问题能召回到正确的组。因此卡内来源按重要性排序：证据在出口会被
截断，截掉的应该是次要来源而不是常规掉落池。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

from entities.models import Chunk, DocumentMetadata
from entities.taxonomy import Category, SubCategory
from retrieval.tokenize import content_words, tokens_for_fts

#: 属性变体后缀 → 中文标注。按最长后缀优先匹配，避免 `_str` 抢先命中 `_str_dex`。
_ATTR_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_str_dex_int", "力敏智"),
    ("_str_dex", "力敏"),
    ("_str_int", "力智"),
    ("_dex_int", "敏智"),
    ("_str", "力量"),
    ("_dex", "敏捷"),
    ("_int", "智慧"),
)

#: 来源类别 → 二级分类。合并卡按**主来源**归类。
_CATEGORY_TO_SUB = {
    "normal": SubCategory.EXPLICIT_MOD,
    "master": SubCategory.CRAFTING_BENCH,
    "essence": SubCategory.ESSENCE,
    "delve": SubCategory.FOSSIL,
    "incursion": SubCategory.INCURSION,
    "bestiary": SubCategory.BESTIARY,
    "corrupted": SubCategory.CORRUPTED_IMPLICIT,
    "synthesis": SubCategory.SYNTHESISED_IMPLICIT,
    "synthesis_corrupted": SubCategory.SYNTHESISED_IMPLICIT,
    "enchant": SubCategory.ENCHANT,
}

#: 来源排序优先级。与 `entities/mod_lookup.py` 的歧义消解顺序一致——
#: 两处若不一致，卡片里排第一的来源和反查时选中的来源会对不上。
_SOURCE_RANK = {"normal": 0, "master": 1, "essence": 2, "delve": 3}
_SOURCE_RANK_DEFAULT = 5


def _source_rank(category: str) -> int:
    return _SOURCE_RANK.get(category, _SOURCE_RANK_DEFAULT)


@dataclass
class SourceSection:
    """卡内一节：同一 (来源类别, 前后缀) 下的完整 T 级梯度。"""

    category: str
    category_label: str
    gen_type: int
    gen_label: str
    rows: list[sqlite3.Row] = field(default_factory=list)

    @property
    def is_ladder(self) -> bool:
        """这一节是不是真的 T 级梯度。

        不是所有节都是梯度：同一词缀组里可以有几百条**平级**的不同词缀
        （实测 `Body_Armours_str_dex_int:AuraEffect` 的灭界者一节有 210 条，
        全是 T1/1——它们是 210 条不同词缀，不是 210 个档位）。
        把它们写成「210 档」是错的，会让人以为存在 T1..T210 的阶梯。
        """
        return max((r["tier_count"] or 0) for r in self.rows) > 1

    @property
    def heading(self) -> str:
        unit = "档" if self.is_ladder else "条"
        return f"{self.category_label} · {self.gen_label or '其他'} · {len(self.rows)} {unit}"

    @property
    def has_weight(self) -> bool:
        return any((r["weight"] or 0) for r in self.rows)


@dataclass
class ModGroupCard:
    """一张卡片：某底材类别上某个词缀组的**全部来源**。"""

    item_class_page: str
    item_class_zh: str
    correct_group: str
    url: str
    sections: list[SourceSection] = field(default_factory=list)

    @property
    def entity_id(self) -> str:
        return f"mod_ladder:{self.item_class_page}:{self.correct_group}"

    @property
    def class_display(self) -> str:
        """带属性变体的类别名。

        POEDB 把同一部位按属性需求拆成多页（7 个 `Body_Armours_*` 的
        `item_class_zh` 全是「胸甲」），词缀池略有差异，必须在标题里区分开。
        """
        base = self.item_class_zh or self.item_class_page
        for suffix, label in _ATTR_SUFFIXES:
            if self.item_class_page.endswith(suffix):
                return f"{base}（{label}）"
        return base

    @property
    def title(self) -> str:
        return f"{self.class_display} · {self.correct_group or '未分组'}"

    @property
    def all_rows(self) -> list[sqlite3.Row]:
        return [r for s in self.sections for r in s.rows]

    @property
    def tags(self) -> list[str]:
        out: list[str] = []
        for r in self.all_rows:
            out.extend(json.loads(r["tags"] or "[]"))
        return list(dict.fromkeys(out))

    @property
    def gen_labels(self) -> list[str]:
        return list(dict.fromkeys(s.gen_label for s in self.sections if s.gen_label))

    @property
    def primary(self) -> SourceSection:
        return self.sections[0]

    # ── 置信度（逐卡现算，用于 facets 与卡内说明）──

    @property
    def weight_coverage(self) -> str:
        """`all` / `partial` / `none`。无权重意味着该来源算不出成功率。"""
        rows = self.all_rows
        with_w = sum(1 for r in rows if (r["weight"] or 0))
        if with_w == len(rows):
            return "all"
        return "partial" if with_w else "none"

    @property
    def tier_source(self) -> str:
        kinds = {(r["tier_source"] or "") for r in self.all_rows}
        if kinds == {"derived"}:
            return "derived"
        if kinds == {"poedb_payload"}:
            return "poedb_payload"
        return "mixed"

    @property
    def ambiguous(self) -> bool:
        """同一宽松模板出现在多个来源里 → 光看数值无法判断出自哪个来源。

        这是**领域固有属性**而非缺陷（实测 36.4%），但回答时必须并列可能来源，
        所以要在卡片里显式标出来，而不是让模型自己发现。
        """
        seen: dict[str, set[str]] = {}
        for s in self.sections:
            for r in s.rows:
                seen.setdefault(r["loose_template"] or "", set()).add(s.category)
        return any(len(v) > 1 for v in seen.values())


def _fetch_cards(conn: sqlite3.Connection) -> list[ModGroupCard]:
    rows = conn.execute(
        "SELECT * FROM mod_affix ORDER BY item_class_page, correct_group, "
        "category, gen_type, tier"
    ).fetchall()

    cards: dict[tuple[str, str], ModGroupCard] = {}
    sections: dict[tuple[str, str, str, int], SourceSection] = {}

    for r in rows:
        ckey = (r["item_class_page"], r["correct_group"] or "")
        card = cards.get(ckey)
        if card is None:
            card = ModGroupCard(
                item_class_page=r["item_class_page"],
                item_class_zh=r["item_class_zh"] or "",
                correct_group=r["correct_group"] or "",
                url=r["url"] or "",
            )
            cards[ckey] = card

        skey = (*ckey, r["category"], r["gen_type"] or 0)
        sec = sections.get(skey)
        if sec is None:
            sec = SourceSection(
                category=r["category"],
                category_label=r["category_label"] or r["category"],
                gen_type=r["gen_type"] or 0,
                gen_label=r["gen_label"] or "",
            )
            sections[skey] = sec
            card.sections.append(sec)
        sec.rows.append(r)

    # 来源排序：常规掉落池在前。证据在出口会被截断，截掉的应是次要来源。
    for card in cards.values():
        card.sections.sort(key=lambda s: (_source_rank(s.category), s.gen_type, -len(s.rows)))
    return list(cards.values())


#: 每节最多列几行。超出的**明确写出还剩多少并指向 mod_tiers**，不静默丢弃。
#:
#: 为什么要截：卡片是语义发现入口，出口只会取前 1800 字作为证据，一张 17,000 字的
#: 卡（实测 AuraEffect 那张 234 条）后面的内容永远看不到，却在 FTS 索引里占位、
#: 稀释 IDF。精确数值有 `mod_tiers` 逐组渲染，不受这个上限影响。
MAX_ROWS_PER_SECTION = 20


def _render_section(sec: SourceSection) -> list[str]:
    out = [f"## {sec.heading}", ""]
    out.append("| T 级 | 词缀名 | 词缀文本 | 物品等级 | 生成权重 |")
    out.append("| --- | --- | --- | --- | --- |")
    for r in sec.rows[:MAX_ROWS_PER_SECTION]:
        tier = f"T{r['tier']}/{r['tier_count']}" if r["tier_count"] else f"T{r['tier']}"
        name = (r["name_zh"] or "—").strip() or "—"
        w = r["weight"] or 0
        weight = f"{w}（{r['weight_pct'] or 0}%）" if w else "无数据"
        out.append(f"| {tier} | {name} | {r['text_zh']} | {r['ilvl']} | {weight} |")
    if len(sec.rows) > MAX_ROWS_PER_SECTION:
        rest = len(sec.rows) - MAX_ROWS_PER_SECTION
        out.append("")
        out.append(
            f"> 本节还有 {rest} 条未列出（卡片只作检索入口）。"
            f"完整列表用 mod_tiers 工具查询，指定来源「{sec.category_label}」。"
        )
    if not sec.has_weight:
        out.append("")
        out.append("> 本来源无生成权重数据，**不能据此给出成功率或概率**。")
    out.append("")
    return out


def render_card(card: ModGroupCard) -> str:
    """渲染合并卡片。数值全部来自 POEDB 原文，不做任何换算。"""
    lines = [f"# {card.title}"]

    meta = [
        f"底材类别：{card.class_display}",
        f"词缀组：{card.correct_group or '未分组'}",
        f"类型：{'/'.join(card.gen_labels) or '其他'}",
        f"来源渠道：{len(card.sections)} 个",
    ]
    lines.append(" ｜ ".join(meta))

    tags = card.tags
    if tags:
        lines.append(f"标签：{'、'.join(tags)}")

    lines.append("")
    labels = "、".join(s.category_label for s in card.sections)
    lines.append(f"本词缀组在该底材上共 {len(card.all_rows)} 条，来自 {len(card.sections)} 个来源：{labels}。")
    if card.correct_group:
        lines.append(
            f"互斥规则：与本组（{card.correct_group}）有相同词缀组的其他词缀"
            "不能同时存在于一件装备上。"
        )
    if card.ambiguous:
        lines.append(
            "⚠ 同一条词缀文本在本卡的多个来源里都出现 → 仅凭装备上的数值**无法判断**"
            "它出自哪个来源，回答时必须并列各种可能。"
        )
    lines.append("")

    for sec in card.sections:
        lines.extend(_render_section(sec))

    notes: list[str] = []
    if card.tier_source in ("derived", "mixed"):
        notes.append(
            "T 级按 POEDB 的算法推导——在同一来源类别与词缀组内，把物品等级去重后"
            "降序排名，物品等级最高者为 T1（算法已复刻并通过不变量校验）"
        )
    if card.weight_coverage == "none":
        notes.append("本卡全部来源都没有生成权重数据，任何概率类问题都无法回答")
    elif card.weight_coverage == "partial":
        notes.append("部分来源没有生成权重数据，那些来源算不出概率")
    if notes:
        lines.append("置信度说明：")
        lines.extend(f"- {n}" for n in notes)
        lines.append("")

    lines.append(f"来源：POEDB → {card.title}")
    lines.append(card.url)
    return "\n".join(lines)


def card_to_chunk(card: ModGroupCard) -> Chunk | None:
    if not card.sections:
        return None
    text = render_card(card)
    primary = card.primary
    sub = _CATEGORY_TO_SUB.get(primary.category, SubCategory.EXPLICIT_MOD)

    metadata = DocumentMetadata(
        title=card.title,
        category=(
            Category.ITEM if primary.category in ("normal", "corrupted") else Category.CRAFTING
        ),
        subcategory=sub.value,
        source="POEDB",
        source_priority=1,
        url=card.url,
        language="zh-CN",
        tags=[card.item_class_zh, *card.gen_labels, card.correct_group, *card.tags][:12],
        facets={
            "item_class": card.class_display,
            "item_class_page": card.item_class_page,
            "mod_group": card.correct_group,
            "gen": "/".join(card.gen_labels),
            # 一张卡现在含多个来源，故给列表而非单值
            "mod_sources": "、".join(s.category_label for s in card.sections),
            "source_count": str(len(card.sections)),
            # ── 置信度进 facets：检索侧据此判断能不能谈概率、T 级可不可信 ──
            "weight_available": card.weight_coverage,
            "tier_source": card.tier_source,
            "ambiguous": "true" if card.ambiguous else "false",
        },
    )
    if not metadata.indexable:
        return None

    # keywords 只收**有区分度**的内容。
    #
    # 实测教训：曾把「词缀 T级 物品等级 权重」等领域词固定写进每张卡，结果所有卡
    # 都含这些词 → IDF 归零 → 反而失去区分度。把「这是词缀类问题」交给意图通道
    # 处理（见 retrieval/hybrid.py），而不是污染索引。
    keyword_src = [r["text_zh"] for r in card.all_rows[:16]]
    keywords = " ".join(
        content_words(
            keyword_src,
            extra=[
                card.class_display, card.item_class_zh, card.correct_group,
                *card.gen_labels,
                *[s.category_label for s in card.sections],
                *card.tags,
            ],
        )
    )

    digest = hashlib.sha256(card.entity_id.encode()).hexdigest()[:16]
    return Chunk(
        chunk_id=f"{card.entity_id}#{digest}",
        entity_id=card.entity_id,
        text=text,
        tokens=tokens_for_fts(text),
        keywords=keywords,
        metadata=metadata,
    )


def build_mod_chunks(conn: sqlite3.Connection) -> list[Chunk]:
    """把 mod_affix 汇总成词缀组卡片切片。"""
    out: list[Chunk] = []
    for card in _fetch_cards(conn):
        c = card_to_chunk(card)
        if c is not None:
            out.append(c)
    return out
