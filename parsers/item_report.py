"""装备分析报告（任务 4.3，需求 7.4/7.5/7.7/7.8）。

把 `item_text` 的解析结果与 `item_mods` 的档位匹配装配成一份报告，并渲染为
带引用标记的中文 Markdown。

## 三类内容分开，不混排

| 类型 | 依据 | 呈现 |
| --- | --- | --- |
| 基础信息 | 用户粘贴的装备文本本身 | 事实，标注「来自装备文本」 |
| 词缀档位 | POEDB 词缀库 / 装备文本自带 | 事实，逐条带引用 `[[En]]` |
| 价值判断 | 需要成交/制作价值数据，本报告不联网 | 明确写「数据不足」+ 缺什么 |

## 为什么价值判断只输出「数据不足」

需求 7.5 要求给出是否值得裂界 / 镜像湖 / 镜子级别的判断。这些判断的输入是
**该配置在当前赛季的市场溢价**；同底材在线可比挂单也不能替代成交记录或制作价值。

按第一原则，此处不给「看起来合理」的结论：一条「建议裂界」会让人真的去花
分裂石，而我们连该词缀裂界后值多少都查不到。所以输出格式是
「判定不了 + 缺哪项数据 + 已知的相关事实」，把已有的事实（档位、roll、权重）
交给用户自己判断。

## 可制作方向也只说有据的

前后缀余量由 ``craft_affix_capacity`` 的结构化规则计算；若有未识别显式词缀、隐匿
占位或未鉴定状态，则只展示已识别计数并把结论保持为未知。除此之外只输出两类内容：

  1. 同词缀组在该底材上的**更高档位**及其物品等级门槛（来自 `mod_affix`）
  2. 与现有词缀**互斥**的词缀（来自 `family_list` 交集，规则对齐 POEDB）
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from craft.capacity import CapacityAssessment, assess_affix_capacity, infer_item_kind
from entities.models import Citation
from entities.taxonomy import Source
from parsers.item_mods import (
    TIER_FROM_ITEM_TEXT,
    TIER_SOURCE_LABEL,
    ItemModReport,
    ModAnalysis,
    analyze_mods,
    blocking_mods,
)
from parsers.item_text import ParsedItem


@dataclass
class TierUpgrade:
    """同词缀组在该底材上的更高档位。"""

    from_tier: int
    to_tier: int
    to_ranges: list[tuple[float, float]]
    required_ilvl: int | None
    text_zh: str
    evidence_urls: list[str]
    page_count: int
    #: 这里只判断物等门槛，不表示该词缀可通过某种实际工艺生成。
    level_gate: str  # met / unmet / unknown


@dataclass
class BlockedAffix:
    text: str
    url: str


@dataclass
class CraftDirection:
    mod_text: str
    upgrade: TierUpgrade | None = None
    blocked: list[BlockedAffix] = field(default_factory=list)
    boundary: str = ""


@dataclass
class Judgement:
    """价值判断。缺少成交与制作价值数据时保持「数据不足」。"""

    topic: str
    verdict: str
    missing: list[str] = field(default_factory=list)
    known_facts: list[str] = field(default_factory=list)


@dataclass
class MarketInfo:
    """市场价值。需求 7.7/7.8：有数据必带来源与时间戳，没数据就明说没有。"""

    available: bool = False
    reason: str = ""
    league: str | None = None
    source: str = ""
    fetched_at: str = ""


@dataclass
class ItemReport:
    parsed: ParsedItem
    mods: ItemModReport
    capacity: CapacityAssessment
    craft_directions: list[CraftDirection] = field(default_factory=list)
    judgements: list[Judgement] = field(default_factory=list)
    market: MarketInfo = field(default_factory=MarketInfo)
    citations: list[Citation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_report(conn: sqlite3.Connection, item: ParsedItem) -> ItemReport:
    mod_report = analyze_mods(conn, item)
    capacity = assess_affix_capacity(
        conn,
        item.rarity.value,
        mod_report.prefix_count,
        mod_report.suffix_count,
        item_kind=infer_item_kind(
            item.item_class, item.base_type, corrupted=item.flags.corrupted
        ),
        occupancy=(
            f"当前已识别前缀 {mod_report.prefix_count} 条、"
            f"后缀 {mod_report.suffix_count} 条"
        ),
        incomplete_reason=_capacity_incomplete_reason(item, mod_report),
    )
    report = ItemReport(parsed=item, mods=mod_report, capacity=capacity)
    report.warnings = [*item.warnings, *mod_report.warnings]
    report.craft_directions = _craft_directions(conn, mod_report, item)
    report.citations = _citations(mod_report, capacity, report.craft_directions)
    report.judgements = _judgements(item, mod_report)
    report.market = MarketInfo(
        available=False,
        reason=(
            "本报告不自动联网。可另用 economy_rare_comparables 查询经官方 stat 校验的"
            "同底材在线可比挂单；它不按 roll/品质/插槽/链接归一化，也不是这件装备的"
            "精确成交价或制作价值。"
        ),
    )
    return report


# ── 引用 ────────────────────────────────────────────────────


def _citations(
    mod_report: ItemModReport, capacity: CapacityAssessment,
    directions: list[CraftDirection],
) -> list[Citation]:
    """按 URL 去重装配引用。标题用中文词缀原文，便于对照。"""
    out: list[Citation] = []
    seen: set[str] = set()
    for analysis in mod_report.mods:
        if not analysis.url or analysis.url in seen:
            continue
        seen.add(analysis.url)
        cls = mod_report.item_class.name_zh or mod_report.item_class.raw
        out.append(
            Citation(
                source=analysis.evidence_source,
                title=(f"{cls} 词缀表" if analysis.evidence_source == Source.POEDB.value
                       else f"底材 {mod_report.item_class.base_type} 的 PoB 固有隐式"),
                url=analysis.url,
                priority=analysis.evidence_priority,
            )
        )
    if capacity.rule_url and capacity.rule_url not in seen:
        out.append(
            Citation(
                source=capacity.source or Source.POEDB.value,
                title="前后缀名额规则",
                url=capacity.rule_url,
                priority=capacity.source_priority,
            )
        )
        seen.add(capacity.rule_url)
    for base in mod_report.item_class.base_evidence:
        for source, priority, title, url in (
            (Source.POB.value, 2, f"底材 {base.name_en} 的 PoB 属性标签", base.pob_url),
            (Source.POEDB.value, 1, f"底材 {base.name_zh} 的 POEDB 名称", base.poedb_url),
        ):
            if url and url not in seen:
                seen.add(url)
                out.append(Citation(source=source, title=title, url=url, priority=priority))
    for direction in directions:
        urls = [*(direction.upgrade.evidence_urls if direction.upgrade else []),
                *(blocked.url for blocked in direction.blocked)]
        for url in urls:
            if url and url not in seen:
                seen.add(url)
                out.append(Citation(
                    source=Source.POEDB.value,
                    title="词缀升级门槛与互斥关系",
                    url=url,
                    priority=1,
                ))
    return out


def _capacity_incomplete_reason(item: ParsedItem, mod_report: ItemModReport) -> str:
    """找出会让“剩余名额”被低估的输入缺口。"""
    countable = {"explicit", "crafted", "fractured"}
    unresolved = [
        analysis
        for analysis in mod_report.mods
        if analysis.mod.kind.value in countable and not analysis.affix_type
    ]
    reasons = []
    if unresolved:
        reasons.append(f"有 {len(unresolved)} 条显式词缀未能识别为前缀或后缀")
    if item.flags.veiled:
        reasons.append("含未解锁隐匿词缀，占位方向未知")
    if item.flags.unidentified:
        reasons.append("物品未鉴定，词缀未完整显示")
    if item.unclassified:
        reasons.append(f"有 {len(item.unclassified)} 行内容未归类")
    return "；".join(reasons)


def _label_of(report: ItemReport, url: str) -> str:
    for index, citation in enumerate(report.citations, start=1):
        if citation.url == url:
            return f"E{index}"
    return ""


# ── 可制作方向 ──────────────────────────────────────────────


def _craft_directions(
    conn: sqlite3.Connection, mod_report: ItemModReport, item: ParsedItem
) -> list[CraftDirection]:
    out: list[CraftDirection] = []
    ref = mod_report.item_class
    for analysis in mod_report.mods:
        if analysis.tier is None or not analysis.mod_group:
            continue
        if analysis.ambiguous:
            out.append(CraftDirection(
                mod_text=analysis.mod.text,
                boundary="这条词缀存在来源或词缀组歧义；不推断升级与互斥方向",
            ))
            continue
        if not analysis.url:
            out.append(CraftDirection(
                mod_text=analysis.mod.text,
                boundary="当前档位缺少可引用的来源 URL，不推断制作方向",
            ))
            continue
        upgrade, note = _best_tier(conn, analysis, mod_report, item.item_level)
        blocked = _blocked_labels(conn, analysis, ref)
        if len(ref.pages) > 1:
            note = (note + "；" if note else "") + "具体词缀页未确定，互斥列表暂不提供"
        if upgrade or blocked or note:
            out.append(
                CraftDirection(mod_text=analysis.mod.text, upgrade=upgrade,
                               blocked=blocked, boundary=note)
            )
    return out


def _blocked_labels(
    conn: sqlite3.Connection, analysis: ModAnalysis, ref, limit: int = 5
) -> list[BlockedAffix]:
    """与该词缀互斥的**其他**词缀。

    两处降噪，否则这段没法看：
      - 剔掉同一条词缀自己的其他档位。「T1 火抗 与 T3 火抗 互斥」是废话，
        谁都知道一件装备上不会有两条同名词缀
      - 按宽松模板去重，只保留每条词缀的最优档位。不去重的话一条词缀会因为
        有 8 个档位而刷 8 行
    """
    seen: set[str] = set()
    out: list[BlockedAffix] = []
    for c in blocking_mods(conn, analysis, ref, limit=60):
        if not c.url:
            continue
        key = c.loose_template or c.text_zh
        if analysis.loose_template and key == analysis.loose_template:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(BlockedAffix(
            text=f"{c.text_zh}（{c.category_label} {c.tier_label}）", url=c.url,
        ))
        if len(out) >= limit:
            break
    return out


def _best_tier(
    conn: sqlite3.Connection, analysis: ModAnalysis, mod_report: ItemModReport,
    item_level: int | None,
) -> tuple[TierUpgrade | None, str]:
    """查**同来源类别、同词缀组**在该底材上的最高档位。已经是 T1 就没有升级空间。

    必须带 `category`：同一个词缀组下，掉落词缀与工艺台词缀各有一套 T 级。
    只按组查会把工艺台的 T1 当成掉落的 T1——实测项链生命组里工艺台 T1 是
    `(41-45)`，而掉落 T1 是 `(115-129)`，差一个量级。报出前者等于给了个错答案。

    也必须带 `loose_template`：同一组里还有混合词缀（`+X 生命 / 每秒再生 N 魔力`），
    它们是另一条词缀，不是当前这条的更高档位。
    """
    if analysis.tier is None or analysis.tier <= 1 or not mod_report.item_class.pages:
        return None, ""
    if not analysis.category or not analysis.loose_template:
        return None, "当前词缀只有游戏原文档位，未核实同来源的完整阶梯"
    pages = mod_report.item_class.pages
    placeholders = ",".join("?" * len(pages))
    rows = conn.execute(
        f"""SELECT item_class_page, tier, ilvl, ranges, text_zh, url FROM mod_affix
            WHERE correct_group = ? AND category = ? AND loose_template = ?
              AND tier > 0 AND item_class_page IN ({placeholders})
            ORDER BY item_class_page, tier ASC""",  # noqa: S608
        [analysis.mod_group, analysis.category, analysis.loose_template, *pages],
    ).fetchall()
    by_page: dict[str, list[sqlite3.Row]] = {page: [] for page in pages}
    for row in rows:
        by_page[row["item_class_page"]].append(row)
    if any(not page_rows for page_rows in by_page.values()):
        return None, "并非每个候选底材页都收录了同来源、同文本的档位阶梯"
    highest = [
        [row for row in page_rows if row["tier"] == page_rows[0]["tier"]]
        for page_rows in by_page.values()
    ]
    signatures = {
        (int(row["tier"] or 0), int(row["ilvl"] or 0), row["ranges"], row["text_zh"])
        for group in highest for row in group
    }
    if len(signatures) != 1:
        return None, "候选底材页的最高档位、区间或物等门槛不同，不给单一升级目标"
    if any(not row["url"] for group in highest for row in group):
        return None, "最高档位缺少可引用的来源 URL，不给升级目标"
    row = highest[0][0]
    tier = int(row["tier"] or 0)
    if tier >= analysis.tier:
        return None, ""
    required_ilvl = int(row["ilvl"] or 0) or None
    level_gate = (
        "unknown" if item_level is None or required_ilvl is None else
        "met" if item_level >= required_ilvl else "unmet"
    )
    urls = list(dict.fromkeys(str(row["url"]) for group in highest for row in group))
    return TierUpgrade(
        from_tier=analysis.tier,
        to_tier=tier,
        to_ranges=[(float(a), float(b)) for a, b in json.loads(row["ranges"] or "[]")],
        required_ilvl=required_ilvl,
        text_zh=row["text_zh"] or "",
        evidence_urls=urls,
        page_count=len(pages),
        level_gate=level_gate,
    ), ""


# ── 价值判断 ────────────────────────────────────────────────

_JUDGEMENT_TOPICS = [
    (
        "是否值得裂界（Fracture）",
        [
            "该词缀在当前赛季裂界后的成交溢价（当前只有在线可比挂单）",
            "分裂石与相关通货的当前价格",
        ],
    ),
    (
        "是否值得镜像湖（Reflecting Mist）",
        [
            "同配置物品的当前成交记录与 roll 归一化价格",
            "镜像湖与相关通货的当前价格",
        ],
    ),
    (
        "是否达到镜子级别（Mirror Worthy）",
        [
            "同底材顶级配置在当前赛季的分布（需要交易站聚合数据）",
            "镜子价格与镜像服务的行情",
        ],
    ),
]


def _judgements(item: ParsedItem, mod_report: ItemModReport) -> list[Judgement]:
    facts = _value_facts(item, mod_report)
    return [
        Judgement(topic=topic, verdict="数据不足，不作判断", missing=missing,
                  known_facts=facts)
        for topic, missing in _JUDGEMENT_TOPICS
    ]


def _value_facts(item: ParsedItem, mod_report: ItemModReport) -> list[str]:
    """与价值有关、且**有据**的事实。只陈述，不下结论。"""
    facts: list[str] = []
    matched = [m for m in mod_report.mods if m.tier is not None]
    if matched:
        top = [m for m in matched if m.tier == 1]
        facts.append(
            f"已匹配 {len(matched)} 条词缀档位，其中 T1 共 {len(top)} 条"
        )
        rolls = [m.roll_pct for m in matched if m.roll_pct is not None]
        if rolls:
            facts.append(
                f"可算 roll 百分比的 {len(rolls)} 条中，平均 {sum(rolls) / len(rolls):.0%}"
            )
    if mod_report.unmatched:
        facts.append(
            f"另有 {len(mod_report.unmatched)} 条词缀未能确定档位，"
            f"因此无法据词缀完整评估这件装备"
        )
    if item.flags.corrupted:
        facts.append("已腐化：无法再用常规工艺台改动")
    if item.flags.fractured:
        facts.append("含裂隙词缀：该词缀在重铸时保留")
    if item.flags.mirrored:
        facts.append("已镜像：不能再被镜像，也不能再改动")
    if item.item_level is not None:
        facts.append(f"物品等级 {item.item_level}")
    return facts


# ── 渲染 ────────────────────────────────────────────────────


def render_report(report: ItemReport) -> str:
    """渲染为中文 Markdown。事实带 `[[En]]` 引用标记，建议类单独分区。"""
    item = report.parsed
    lines: list[str] = []

    title = item.display_name or "未命名物品"
    lines.append(f"## {title}")
    lines.append("")
    lines.append("### 基础信息")
    lines.append("")
    lines.append("> 以下来自你粘贴的装备文本本身，未经外部数据源")
    lines.append("")
    basics = [
        ("类别", item.item_class or "未知"),
        ("稀有度", item.rarity.value),
        ("底材", item.base_type or "未知"),
        ("物品等级", str(item.item_level) if item.item_level is not None else "未知"),
        ("品质", f"+{item.quality}%" if item.quality is not None else "未知"),
    ]
    if item.sockets.groups:
        basics.append(("插槽", f"{item.sockets.render()}（{item.sockets.max_links} 连）"))
    req = item.requirements
    req_parts = [
        f"等级 {req.level}" if req.level else "",
        f"力量 {req.strength}" if req.strength else "",
        f"敏捷 {req.dexterity}" if req.dexterity else "",
        f"智慧 {req.intelligence}" if req.intelligence else "",
    ]
    req_text = " / ".join(p for p in req_parts if p)
    if req_text:
        basics.append(("需求", req_text))
    labels = item.flags.labels()
    if labels:
        basics.append(("标记", "、".join(labels)))
    for key, value in basics:
        lines.append(f"- **{key}**：{value}")

    lines.append("")
    lines.append("### 词缀分析")
    lines.append("")
    cls = report.mods.item_class
    lines.append(
        f"- 词缀档位对照的底材类别：{cls.name_zh or cls.raw or '未确定'}"
        + (f"（{len(cls.pages)} 个 POEDB 底材页面）" if len(cls.pages) > 1 else "")
    )
    if cls.base_evidence:
        base_labels = [
            f"[[{label}]]"
            for base in cls.base_evidence
            for url in (base.pob_url, base.poedb_url)
            if (label := _label_of(report, url))
        ]
        lines.append(
            f"- 词缀查询页：{'、'.join(cls.pages)}；依据逐字底材「{cls.base_type}」"
            f"的 PoB 标签与 POEDB 名称桥{''.join(dict.fromkeys(base_labels))}"
        )
    lines.append(
        f"- 已确定档位 {report.mods.matched_count} 条 / 共 {len(report.mods.mods)} 条"
        f"；前缀 {report.mods.prefix_count} 条、后缀 {report.mods.suffix_count} 条"
    )
    capacity = report.capacity
    capacity_label = _label_of(report, capacity.rule_url)
    capacity_mark = f"[[{capacity_label}]]" if capacity_label else ""
    lines.append(f"- 前后缀容量：{capacity.summary}{capacity_mark}")
    if capacity.status == "pass":
        lines.append(
            f"- 可用随机词缀位：前缀 {capacity.prefix_remaining} 条、"
            f"后缀 {capacity.suffix_remaining} 条{capacity_mark}"
        )
    elif capacity.missing:
        lines.append(f"- 容量结论未知：{capacity.missing}")
    lines.append("")

    for analysis in report.mods.mods:
        lines.append(_render_mod(report, analysis))

    if report.mods.unmatched:
        lines.append("")
        lines.append("#### 未能确定档位的词缀")
        lines.append("")
        lines.append("> 这些词缀查不到对应档位，**不给推测的 T 级**。原因逐条如下")
        lines.append("")
        for analysis in report.mods.unmatched:
            lines.append(f"- `{analysis.mod.text}`：{analysis.reason}")

    if report.craft_directions:
        lines.append("")
        lines.append("### 可制作方向")
        lines.append("")
        lines.append("> 建议类内容。事实前提（档位、物品等级门槛、互斥关系）各自带引用")
        lines.append("")
        for direction in report.craft_directions:
            lines.append(f"- `{direction.mod_text}`")
            if direction.upgrade:
                up = direction.upgrade
                ranges = "、".join(f"{lo:g}-{hi:g}" for lo, hi in up.to_ranges)
                marks = "".join(
                    f"[[{label}]]" for url in up.evidence_urls
                    if (label := _label_of(report, url))
                )
                scope = ("该底材页" if up.page_count == 1
                         else f"已核对的 {up.page_count} 个候选底材页共同")
                level_phrase = (f"要求物品等级 {up.required_ilvl}"
                                if up.required_ilvl is not None else "物品等级门槛未知")
                lines.append(
                    f"  - {scope}最高档位是 T{up.to_tier}（{ranges}），"
                    f"{level_phrase}{marks}"
                )
                gate = {
                    "met": "当前物品等级已达到该档位门槛；这不表示某种工艺可执行",
                    "unmet": "当前物品等级低于该档位门槛；不能把它作为当前底材可生成的档位",
                    "unknown": "当前物品等级或来源门槛未知，无法判断是否达到门槛",
                }[up.level_gate]
                lines.append(f"  - 物等校验：{gate}")
            for blocked in direction.blocked:
                label = _label_of(report, blocked.url)
                mark = f"[[{label}]]" if label else ""
                lines.append(f"  - 互斥：{blocked.text}{mark}")
            if direction.boundary:
                lines.append(f"  - 边界：{direction.boundary}")

    lines.append("")
    lines.append("### 市场价值")
    lines.append("")
    lines.append(f"**无价格数据。** {report.market.reason}")

    lines.append("")
    lines.append("### 价值判断")
    lines.append("")
    lines.append("> 以下三项需要经济数据才能判断，目前一项都给不出结论")
    lines.append("")
    for judgement in report.judgements:
        lines.append(f"- **{judgement.topic}**：{judgement.verdict}")
        for missing in judgement.missing:
            lines.append(f"  - 缺：{missing}")
    if report.judgements and report.judgements[0].known_facts:
        lines.append("")
        lines.append("已知的相关事实（供你自己判断）：")
        for fact in report.judgements[0].known_facts:
            lines.append(f"- {fact}")

    if report.warnings:
        lines.append("")
        lines.append("### 解析告警")
        lines.append("")
        for warning in report.warnings:
            lines.append(f"- {warning}")

    if report.citations:
        lines.append("")
        lines.append("### 引用")
        lines.append("")
        for index, citation in enumerate(report.citations, start=1):
            lines.append(f"[E{index}] {citation.render()}")
            lines.append(f"      {citation.url}")

    return "\n".join(lines)


def _render_mod(report: ItemReport, analysis: ModAnalysis) -> str:
    label = _label_of(report, analysis.url)
    mark = f"[[{label}]]" if label else ""
    head = f"- `{analysis.mod.text}` — **{analysis.tier_label}**{mark}"
    bits: list[str] = []
    if analysis.affix_type:
        bits.append(analysis.affix_type)
    if analysis.source_label:
        bits.append(analysis.source_label)
    if analysis.ranges:
        bits.append("区间 " + "、".join(f"{lo:g}-{hi:g}" for lo, hi in analysis.ranges))
    if analysis.roll_pct is not None:
        bits.append(f"roll {analysis.roll_pct:.0%}")
    if analysis.ilvl:
        bits.append(f"物品等级 {analysis.ilvl}")
    if analysis.weight:
        bits.append(f"生成权重 {analysis.weight}")
    detail = "；".join(bits)
    source = TIER_SOURCE_LABEL.get(analysis.tier_source, "") if analysis.tier is not None else ""
    tail = f"\n  - {detail}" if detail else ""
    if source:
        tail += f"\n  - T 级来源：{source}"
    if analysis.tier_source == TIER_FROM_ITEM_TEXT:
        tail += "（游戏原文，比查库更可靠）"
    if analysis.ambiguous and analysis.alternatives:
        tail += f"\n  - ⚠ 存在歧义，也可能是：{'；'.join(analysis.alternatives)}"
    return head + tail
