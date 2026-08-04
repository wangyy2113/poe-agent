"""词缀 → 知识卡片。

**卡片粒度 = 一条 T 级梯度**，即 (底材类别, 来源类别, 前后缀, 词缀组)。

为什么不是「一条词缀一张卡」：93,875 条记录里同一条词缀在 100 个底材类别上
重复出现，逐条建卡会让检索被大量近似重复结果淹没。而玩家真正问的是
「胸甲上的生命词缀有几级、各级什么范围、T1 要多少物品等级」——
这正好是一条完整梯度。按梯度建卡，约 2.5 万张，直接对应问题。

精确数值仍可经 entity_id 回 `mod_affix` 表逐条取回（Tier A 精确层），
卡片只承担语义检索入口的职责（Tier B）。
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

#: 来源类别 → 二级分类。决定卡片归到哪个知识分类下。
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


@dataclass
class Ladder:
    """一条 T 级梯度。"""

    item_class_page: str
    item_class_zh: str
    category: str
    category_label: str
    gen_type: int
    gen_label: str
    correct_group: str
    url: str
    rows: list[sqlite3.Row] = field(default_factory=list)

    @property
    def entity_id(self) -> str:
        return (
            f"mod_ladder:{self.item_class_page}:{self.category}:"
            f"{self.gen_type}:{self.correct_group}"
        )

    @property
    def class_display(self) -> str:
        """带属性变体的类别名。

        POEDB 把同一部位按属性需求拆成多页（7 个 Body_Armours_* 的
        item_class_zh 全是「胸甲」），词缀池略有差异，必须在标题里区分开，
        否则 7 张近似卡片无法辨识。
        """
        base = self.item_class_zh or self.item_class_page
        variant = ""
        for suffix, label in _ATTR_SUFFIXES:
            if self.item_class_page.endswith(suffix):
                variant = label
                break
        return f"{base}（{variant}）" if variant else base

    @property
    def title(self) -> str:
        parts = [self.class_display]
        if self.gen_label:
            parts.append(self.gen_label)
        parts.append(self.correct_group or "未分组")
        head = " · ".join(parts)
        return f"{head}（{self.category_label}）"

    @property
    def tags(self) -> list[str]:
        out: list[str] = []
        for r in self.rows:
            out.extend(json.loads(r["tags"] or "[]"))
        return list(dict.fromkeys(out))


def _fetch_ladders(conn: sqlite3.Connection) -> list[Ladder]:
    rows = conn.execute(
        "SELECT * FROM mod_affix ORDER BY item_class_page, category, gen_type, "
        "correct_group, tier, ilvl DESC"
    ).fetchall()

    ladders: dict[tuple, Ladder] = {}
    for r in rows:
        key = (r["item_class_page"], r["category"], r["gen_type"], r["correct_group"])
        lad = ladders.get(key)
        if lad is None:
            lad = Ladder(
                item_class_page=r["item_class_page"],
                item_class_zh=r["item_class_zh"] or "",
                category=r["category"],
                category_label=r["category_label"] or r["category"],
                gen_type=r["gen_type"] or 0,
                gen_label=r["gen_label"] or "",
                correct_group=r["correct_group"] or "",
                url=r["url"] or "",
            )
            ladders[key] = lad
        lad.rows.append(r)
    return list(ladders.values())


def render_ladder(lad: Ladder) -> str:
    """渲染梯度卡片。数值全部来自 POEDB 原文，不做换算。"""
    lines = [f"# {lad.title}"]

    meta = [
        f"底材类别：{lad.class_display}",
        f"来源：{lad.category_label}",
        f"类型：{lad.gen_label or '其他'}",
        f"词缀组：{lad.correct_group or '未分组'}",
    ]
    lines.append(" ｜ ".join(meta))

    tags = lad.tags
    if tags:
        lines.append(f"标签：{'、'.join(tags)}")

    lines.append("")
    lines.append(f"共 {len(lad.rows)} 条，梯度如下（物品等级越高 T 级越好）：")
    lines.append("")
    lines.append("| T 级 | 词缀名 | 词缀文本 | 物品等级 | 生成权重 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in lad.rows:
        tier = f"T{r['tier']}/{r['tier_count']}" if r["tier_count"] else f"T{r['tier']}"
        name = (r["name_zh"] or "—").strip() or "—"
        weight = r["weight"] or 0
        pct = r["weight_pct"] or 0
        w = f"{weight}（{pct}%）" if weight else "—"
        lines.append(f"| {tier} | {name} | {r['text_zh']} | {r['ilvl']} | {w} |")

    lines.append("")
    if lad.correct_group:
        lines.append(
            f"互斥规则：与本组（{lad.correct_group}）有相同词缀组的其他词缀不能同时存在于一件装备上。"
        )
    if any((r["tier_source"] or "") == "derived" for r in lad.rows):
        lines.append(
            "T 级说明：按 POEDB 的算法推导——在同一来源类别与词缀组内，"
            "把物品等级去重后降序排名，物品等级最高者为 T1。"
        )

    lines.append("")
    lines.append(f"来源：POEDB → {lad.title}")
    lines.append(lad.url)
    return "\n".join(lines)


def ladder_to_chunk(lad: Ladder) -> Chunk | None:
    if not lad.rows:
        return None
    text = render_ladder(lad)
    sub = _CATEGORY_TO_SUB.get(lad.category, SubCategory.EXPLICIT_MOD)

    metadata = DocumentMetadata(
        title=lad.title,
        category=Category.ITEM if lad.category in ("normal", "corrupted") else Category.CRAFTING,
        subcategory=sub.value,
        source="POEDB",
        source_priority=1,
        url=lad.url,
        language="zh-CN",
        tags=[lad.item_class_zh, lad.gen_label, lad.correct_group, *lad.tags][:12],
        facets={
            # mod_source 用**中文标签**，与查询里玩家写的词一致（「精华」「工艺工作台」）；
            # mod_category 保留 POEDB 的英文代码，用于与来源优先级规则比对。
            "mod_source": lad.category_label,
            "mod_category": lad.category,
            "item_class": lad.class_display,
            "item_class_page": lad.item_class_page,
            "gen": lad.gen_label,
            "mod_group": lad.correct_group,
        },
    )
    if not metadata.indexable:
        return None

    # keywords 只收录**有区分度**的内容：词缀文本实义词 + 类别 / 前后缀 / 词缀组 / 标签。
    #
    # 实测教训：曾把「词缀 T级 物品等级 权重」等领域词固定写进每张卡，
    # 结果所有 24418 张卡都含这些词 → IDF 归零 → 反而失去区分度，
    # 让原本能命中的查询退化。把「这是词缀类问题」交给意图通道处理
    # （见 retrieval/hybrid.py 的 mod 意图通道），而不是污染索引。
    keyword_src = [r["text_zh"] for r in lad.rows[:12]]
    keywords = " ".join(
        content_words(
            keyword_src,
            extra=[
                lad.class_display, lad.item_class_zh, lad.gen_label,
                lad.correct_group, lad.category_label, *lad.tags,
            ],
        )
    )

    digest = hashlib.sha256(lad.entity_id.encode()).hexdigest()[:16]
    return Chunk(
        chunk_id=f"{lad.entity_id}#{digest}",
        entity_id=lad.entity_id,
        text=text,
        tokens=tokens_for_fts(text),
        keywords=keywords,
        metadata=metadata,
    )


def build_mod_chunks(conn: sqlite3.Connection) -> list[Chunk]:
    """把 mod_affix 汇总成梯度卡片切片。"""
    out: list[Chunk] = []
    for lad in _fetch_ladders(conn):
        c = ladder_to_chunk(lad)
        if c is not None:
            out.append(c)
    return out
