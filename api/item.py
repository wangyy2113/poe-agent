"""`/item`：装备文本解析与分析（任务 4.4，需求 5.2）。

路由层只做校验与编排，业务在 `parsers/`：
    文本 → `item_text.parse_item` → `item_mods.analyze_mods` → `item_report.build_report`

**这条链路全程不调用 LLM。** 输出是代码装配的，因此完全可预测：同一份装备文本
永远得到同一份报告，数值只可能来自装备文本本身或词缀库的某一行。
也正因如此它不需要过出口绑定闸门——闸门防的是模型编造，这里没有模型。

解析失败返回 422 且带期望格式样例（需求 7.6），不返回半个结果。
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from core import db as dbmod
from parsers.item_mods import TIER_SOURCE_LABEL
from parsers.item_report import build_report, render_report
from parsers.item_text import ItemParseError, parse_item

logger = logging.getLogger(__name__)
router = APIRouter(tags=["装备"])


class ItemRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000, description="游戏内复制的装备文本")
    #: 只要结构化结果、不要渲染好的 Markdown 时置 false，省流量
    render: bool = True


class ModOut(BaseModel):
    text: str
    kind: str
    tier: int | None
    tier_count: int | None
    tier_label: str
    #: T 级来源：item_text（游戏原文）/ poedb / bridge / 空（未匹配）
    tier_source: str
    tier_source_label: str
    affix_type: str
    source_label: str
    ranges: list[tuple[float, float]]
    roll_pct: float | None
    ilvl: int | None
    weight: int | None
    ambiguous: bool
    alternatives: list[str]
    #: 未匹配时说明为什么，绝不给推测的 T 级（需求 7.9）
    reason: str
    url: str


class CitationOut(BaseModel):
    label: str
    source: str
    title: str
    url: str
    priority: int


class JudgementOut(BaseModel):
    topic: str
    verdict: str
    missing: list[str]


class AffixCapacityOut(BaseModel):
    status: Literal["pass", "fail", "unknown"]
    item_kind: str
    prefix_used: int
    suffix_used: int
    prefix_limit: int | None
    suffix_limit: int | None
    prefix_remaining: int | None
    suffix_remaining: int | None
    summary: str
    missing: str
    rule_text: str
    source: str
    url: str


class TierUpgradeOut(BaseModel):
    from_tier: int
    to_tier: int
    to_ranges: list[tuple[float, float]]
    required_ilvl: int | None
    text_zh: str
    page_count: int
    level_gate: Literal["met", "unmet", "unknown"]
    evidence_urls: list[str]


class BlockedAffixOut(BaseModel):
    text: str
    url: str


class CraftDirectionOut(BaseModel):
    mod_text: str
    upgrade: TierUpgradeOut | None
    blocked: list[BlockedAffixOut]
    boundary: str


class ItemResponse(BaseModel):
    language: str
    #: 是否进阶复制（Ctrl+Alt+C）。为 true 时 T 级由游戏原文给出
    advanced_copy: bool
    item_class: str
    #: 词缀档位比对用的底材类别页面（可能多个）
    item_class_pages: list[str]
    #: page/code/zh 是类别解析；base 是具体底材把页定为唯一，base_* 为冲突或未知
    page_resolution: str
    rarity: str
    name: str
    base_type: str
    item_level: int | None
    quality: int | None
    sockets: str
    max_links: int
    requirements: dict[str, int | None]
    flags: list[str]
    mods: list[ModOut]
    matched_count: int
    prefix_count: int
    suffix_count: int
    affix_capacity: AffixCapacityOut
    craft_directions: list[CraftDirectionOut]
    #: 本地报告不自动联网；实时同类挂单比较走 /economy/rare-comparables。
    market_available: bool
    market_reason: str
    judgements: list[JudgementOut]
    citations: list[CitationOut]
    warnings: list[str]
    #: 未归类的行。**不当词缀参与分析**，如实回给调用方
    unclassified: list[str]
    answer: str | None = None


@router.post("/item", response_model=ItemResponse)
def analyze_item(req: ItemRequest, caller: str = Depends(require_user)) -> ItemResponse:
    try:
        parsed = parse_item(req.text)
    except ItemParseError as exc:
        # 422 而不是 400：格式对不上属于语义无法处理，且要把期望格式带回去
        raise HTTPException(
            status_code=422,
            detail={
                "error": "item_parse_failed",
                "reason": exc.reason,
                "hint": exc.hint,
                "expected_sample": exc.expected_sample,
            },
        ) from exc

    report = build_report(dbmod.get_conn(), parsed)
    logger.info(
        "item 分析 caller=%s 类别=%s 稀有度=%s 词缀=%d 已定档=%d 进阶复制=%s",
        caller, parsed.item_class, parsed.rarity.value,
        len(report.mods.mods), report.mods.matched_count, parsed.advanced_copy,
    )

    labels = {c.url: f"E{i}" for i, c in enumerate(report.citations, start=1)}
    return ItemResponse(
        language=parsed.language,
        advanced_copy=parsed.advanced_copy,
        item_class=parsed.item_class,
        item_class_pages=report.mods.item_class.pages,
        page_resolution=report.mods.item_class.matched_by,
        rarity=parsed.rarity.value,
        name=parsed.name,
        base_type=parsed.base_type,
        item_level=parsed.item_level,
        quality=parsed.quality,
        sockets=parsed.sockets.render(),
        max_links=parsed.sockets.max_links,
        requirements={
            "level": parsed.requirements.level,
            "strength": parsed.requirements.strength,
            "dexterity": parsed.requirements.dexterity,
            "intelligence": parsed.requirements.intelligence,
        },
        flags=parsed.flags.labels(),
        mods=[
            ModOut(
                text=a.mod.text,
                kind=a.mod.kind.value,
                tier=a.tier,
                tier_count=a.tier_count,
                tier_label=a.tier_label,
                tier_source=a.tier_source,
                tier_source_label=TIER_SOURCE_LABEL.get(a.tier_source, ""),
                affix_type=a.affix_type,
                source_label=a.source_label,
                ranges=a.ranges,
                roll_pct=round(a.roll_pct, 4) if a.roll_pct is not None else None,
                ilvl=a.ilvl,
                weight=a.weight,
                ambiguous=a.ambiguous,
                alternatives=a.alternatives,
                reason=a.reason,
                url=a.url,
            )
            for a in report.mods.mods
        ],
        matched_count=report.mods.matched_count,
        prefix_count=report.mods.prefix_count,
        suffix_count=report.mods.suffix_count,
        affix_capacity=AffixCapacityOut(
            status=report.capacity.status,
            item_kind=report.capacity.item_kind,
            prefix_used=report.capacity.prefix_used,
            suffix_used=report.capacity.suffix_used,
            prefix_limit=report.capacity.prefix_limit,
            suffix_limit=report.capacity.suffix_limit,
            prefix_remaining=report.capacity.prefix_remaining,
            suffix_remaining=report.capacity.suffix_remaining,
            summary=report.capacity.summary,
            missing=report.capacity.missing,
            rule_text=report.capacity.rule_text,
            source=report.capacity.source,
            url=report.capacity.rule_url,
        ),
        craft_directions=[
            CraftDirectionOut(
                mod_text=d.mod_text,
                upgrade=(TierUpgradeOut(
                    from_tier=d.upgrade.from_tier,
                    to_tier=d.upgrade.to_tier,
                    to_ranges=d.upgrade.to_ranges,
                    required_ilvl=d.upgrade.required_ilvl,
                    text_zh=d.upgrade.text_zh,
                    page_count=d.upgrade.page_count,
                    level_gate=d.upgrade.level_gate,
                    evidence_urls=d.upgrade.evidence_urls,
                ) if d.upgrade else None),
                blocked=[BlockedAffixOut(text=b.text, url=b.url) for b in d.blocked],
                boundary=d.boundary,
            ) for d in report.craft_directions
        ],
        market_available=report.market.available,
        market_reason=report.market.reason,
        judgements=[
            JudgementOut(topic=j.topic, verdict=j.verdict, missing=j.missing)
            for j in report.judgements
        ],
        citations=[
            CitationOut(
                label=labels.get(c.url, ""),
                source=c.source,
                title=c.title,
                url=c.url,
                priority=c.priority,
            )
            for c in report.citations
        ],
        warnings=report.warnings,
        unclassified=parsed.unclassified,
        answer=render_report(report) if req.render else None,
    )
