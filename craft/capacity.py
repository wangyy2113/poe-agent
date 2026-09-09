"""前后缀名额规则的共享判定。

组合可行性工具与装备报告都消费 ``craft_affix_capacity``。规则查询和边界必须只有
一份：否则一个入口会说稀有珠宝是 2+2，另一个仍按普通装备说成 3+3。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from parsers.item_text import Rarity

CapacityStatus = Literal["pass", "fail", "unknown"]


@dataclass(frozen=True)
class CapacityAssessment:
    status: CapacityStatus
    rarity: str
    item_kind: str
    prefix_used: int
    suffix_used: int
    prefix_limit: int | None = None
    suffix_limit: int | None = None
    summary: str = ""
    missing: str = ""
    rule_text: str = ""
    source: str = ""
    source_priority: int = 99
    rule_url: str = ""

    @property
    def prefix_remaining(self) -> int | None:
        if self.status == "unknown" or self.prefix_limit is None:
            return None
        return max(0, self.prefix_limit - self.prefix_used)

    @property
    def suffix_remaining(self) -> int | None:
        if self.status == "unknown" or self.suffix_limit is None:
            return None
        return max(0, self.suffix_limit - self.suffix_used)


def normalize_rarity(raw: str | None) -> str | None:
    """接受 ``Rarity`` 的中英文写法，不另建一份稀有度映射。"""
    text = (raw or "").strip()
    if not text:
        return None
    for member in Rarity:
        if text == member.value or text.lower() == member.name.lower():
            return member.value
    return None


def infer_item_kind(item_class: str, base_type: str = "", *, corrupted: bool = False) -> str:
    """从装备文本的类别识别规则表已有的特殊种类。"""
    # 类别存在时不看具体底材名，避免名字里偶然含 Jewel / Flask 的普通装备被误分类。
    text = (item_class or base_type).casefold()
    if "abyss jewel" in text or "深渊珠宝" in text or "深淵珠寶" in text:
        return "jewel_abyss_corrupted" if corrupted else "jewel"
    if "jewel" in text or "珠宝" in text or "珠寶" in text:
        return "jewel"
    if "flask" in text or "药剂" in text or "藥劑" in text:
        return "flask"
    if "graft" in text:
        return "graft"
    return "default"


def _rule(conn: sqlite3.Connection, rarity: str, item_kind: str) -> sqlite3.Row | None:
    try:
        row = conn.execute(
            "SELECT * FROM craft_affix_capacity WHERE rarity=? AND item_kind=?",
            (rarity, item_kind),
        ).fetchone()
        if row is None and item_kind != "default":
            row = conn.execute(
                "SELECT * FROM craft_affix_capacity WHERE rarity=? AND item_kind='default'",
                (rarity,),
            ).fetchone()
        return row
    except sqlite3.Error:
        return None


def assess_affix_capacity(
    conn: sqlite3.Connection,
    rarity: str | None,
    prefix_used: int,
    suffix_used: int,
    *,
    item_kind: str = "default",
    occupancy: str = "",
    incomplete_reason: str = "",
) -> CapacityAssessment:
    """按结构化规则判断当前占位，缺输入时保持 ``unknown``。"""
    normalized = normalize_rarity(rarity)
    base = occupancy or f"已识别前缀 {prefix_used} 条、后缀 {suffix_used} 条"
    if normalized is None:
        missing = (
            "问题没有给出物品稀有度，无法确定前后缀名额上限；"
            "补上「魔法」或「稀有」即可判定"
        )
        return CapacityAssessment(
            "unknown", str(rarity or ""), item_kind, prefix_used, suffix_used,
            summary=base, missing=missing,
        )

    rule = _rule(conn, normalized, item_kind)
    if rule is None:
        missing = f"库里没有「{normalized} × {item_kind}」的前后缀名额规则"
        return CapacityAssessment(
            "unknown", normalized, item_kind, prefix_used, suffix_used,
            summary=base, missing=missing,
        )

    common = {
        "rarity": normalized,
        "item_kind": item_kind,
        "prefix_used": prefix_used,
        "suffix_used": suffix_used,
        "rule_text": str(rule["poedb_text"] or rule["pob_text"] or ""),
        "source": str(rule["source"] or ""),
        "source_priority": int(rule["source_priority"] or 99),
        "rule_url": str(rule["url"] or ""),
    }
    if rule["prefix_limit"] is None or rule["suffix_limit"] is None:
        missing = (
            f"「{normalized}」不受前后缀数量限制，因此名额不构成约束；"
            "但传奇物品的词缀是固定的，能否共存不由名额决定"
        )
        return CapacityAssessment("unknown", summary=base, missing=missing, **common)

    prefix_limit = int(rule["prefix_limit"])
    suffix_limit = int(rule["suffix_limit"])
    common.update(prefix_limit=prefix_limit, suffix_limit=suffix_limit)
    over = []
    if prefix_used > prefix_limit:
        over.append(f"前缀 {prefix_used} > 上限 {prefix_limit}")
    if suffix_used > suffix_limit:
        over.append(f"后缀 {suffix_used} > 上限 {suffix_limit}")
    limits = f"（{prefix_limit} 前缀 / {suffix_limit} 后缀）"
    if over:
        return CapacityAssessment(
            "fail",
            summary=f"{base}；超出「{normalized}」的名额上限{limits}：" + "，".join(over),
            **common,
        )
    if incomplete_reason:
        return CapacityAssessment(
            "unknown",
            summary=f"{base}；已知计数未超出{limits}，但装备状态不完整",
            missing=incomplete_reason,
            **common,
        )
    return CapacityAssessment(
        "pass",
        summary=(
            f"{base}；未超出「{normalized}」的名额上限{limits}。"
            f"规则原文：{common['rule_text']}"
        ),
        **common,
    )
