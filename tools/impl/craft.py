"""制作可行性结构化工具。

工具只做参数校验、业务调用与证据渲染；约束求解全部在 ``craft.feasibility``。
候选原文与确定性求解结论分别生成 Citation：前者证明输入，后者证明三态结论。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from core import db as dbmod
from craft.feasibility import (
    CRAFT_RULE_URL,
    MAX_TARGET_QUERY_CHARS,
    MAX_TARGET_SOURCE_CHARS,
    MAX_TARGETS,
    FeasibilityReport,
    PageAnalysis,
    TargetResolution,
    analyze_feasibility,
)
from entities.taxonomy import MAX_FACTUAL_PRIORITY, Source
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 1800
MAX_EVIDENCE_CANDIDATES = 12
SOLVER_VERSION = "family-assignment-v1"
DERIVED_SOURCE = "POE1-Agent 确定性求解器（结构化输入：POEDB）"


def _render_target_evidence(resolution: TargetResolution) -> str:
    target = resolution.target
    source = f"，来源限定「{target.source}」" if target.source else ""
    out = [
        f"### {resolution.page} · 目标「{target.query}」{source}",
        "",
        f"结构化候选总数：{resolution.total_candidates}。多档同一词缀已折叠为一个候选。",
        "",
        "| 候选 ID | 词缀文本样例 | 类型 | 来源 | 完整 family_list | 最低物品等级 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for candidate in resolution.candidates[:MAX_EVIDENCE_CANDIDATES]:
        text = (
            candidate.sample_texts[0] if candidate.sample_texts else candidate.mod_code
        ).replace("|", "\\|")
        families = ", ".join(candidate.family_list).replace("|", "\\|") or "（空）"
        out.append(
            f"| {candidate.candidate_id} | {text} | {candidate.gen_label or '其他'} | "
            f"{candidate.category_label} (`{candidate.category}`) | {families} | "
            f"{candidate.min_ilvl} |"
        )
    hidden = resolution.total_candidates - min(
        len(resolution.candidates), MAX_EVIDENCE_CANDIDATES
    )
    if hidden > 0:
        out.extend(["", f"> 另有 {hidden} 个候选未在本证据表展开；请缩小目标后查看。"])
    if resolution.truncated:
        out.extend(["", "> 候选超过求解上限；本页不会据此判定 impossible。"])
    return "\n".join(out)


def _candidate_set_digest(resolution: TargetResolution) -> str:
    """完整求解输入摘要；不受公开候选表的 12 行展示上限影响。"""
    payload = [
        {
            "candidate_id": candidate.candidate_id,
            "correct_group": candidate.correct_group,
            "category": candidate.category,
            "gen_type": candidate.gen_type,
            "family_list": list(candidate.family_list),
            "loose_template": candidate.loose_template,
        }
        for candidate in resolution.candidates
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _render_solver_evidence(page: PageAnalysis) -> str:
    """渲染确定性求解记录，使三态结论本身可被出口闸门绑定。"""
    out = [
        f"### {page.page} · 词缀组合约束检查（工具派生）",
        "",
        f"求解器版本：`{SOLVER_VERSION}`。结构化输入来自 POEDB；页面本身不陈述本结论。",
        "算法：逐目标解析候选；多档同一词缀按稳定梯度身份折叠；不同候选用完整 "
        "family_list 求交；同一 candidate_id 可复用以表示一条混合词缀满足多个目标。",
        "",
        f"**页面结论：{page.verdict}**",
        f"候选截断：{'是' if page.search_truncated else '否'}；"
        f"组合搜索节点：{page.search_nodes}；"
        f"候选分配已穷尽：{'是' if page.search_exhaustive else '否'}。",
    ]
    if page.verdict == "impossible":
        out.extend(
            [
                "",
                "结论证明：候选未截断、组合搜索已穷尽，且没有兼容分配；"
                "因此该页面上的全部候选分配都发生 family_list 冲突，判定 impossible。",
            ]
        )
    elif page.verdict == "possible":
        out.extend(
            [
                "",
                "结论证明：目标全部解析到候选、存在一组无 family_list 冲突的分配、"
                "占位未超出该稀有度的前后缀名额，且这一页没有任何未解决的缺据，"
                "因此判定 possible。",
                "边界：possible 的含义是**这些词缀能共存**，不是「能做出来」——"
                "不含制作步骤、成功率、通货成本与路线比较。",
            ]
        )
    elif page.verdict == "unknown":
        out.extend(
            [
                "",
                "边界：unknown 不表示可制作；它表示现有结构化规则不足以证明 "
                "possible 或 impossible。",
            ]
        )

    out.extend(["", "完整求解输入摘要（SHA-256 覆盖全部已分析候选，不受展示上限影响）："])
    for resolution in page.targets:
        source = f"（来源 {resolution.target.source}）" if resolution.target.source else ""
        out.append(
            f"- {resolution.target.query}{source}: 总计 {resolution.total_candidates} 个，"
            f"参与求解 {len(resolution.candidates)} 个，摘要 "
            f"`{_candidate_set_digest(resolution)}`"
            f"{'，输入已截断' if resolution.truncated else ''}"
        )

    family = next((check for check in page.checks if check.key == "family_exclusivity"), None)
    if family:
        out.extend(["", f"family_list 检查：{family.status}。{family.summary}"])
    capacity = next((check for check in page.checks if check.key == "affix_capacity"), None)
    if capacity:
        # 名额上限是 possible / impossible 的直接依据，必须逐字进证据原文，
        # 否则模型复述「上限 3」时数值回填在证据里找不到这个数。
        out.append(f"前后缀名额检查：{capacity.status}。{capacity.summary}")
    if page.selected_candidate_ids:
        out.append("兼容见证：" + "、".join(page.selected_candidate_ids))
        out.append(
            f"占位见证：前缀 {page.occupied_prefixes}，后缀 {page.occupied_suffixes}，"
            f"其他 {page.occupied_other}。该占位只描述找到的这一组兼容分配。"
        )
    if page.conflicts:
        out.extend(["", "冲突见证（完整 family_list 的交集，最多展示 8 条）："])
        for conflict in page.conflicts[:8]:
            out.append(
                f"- {conflict.left_candidate_id} × {conflict.right_candidate_id}: "
                + "、".join(conflict.shared_families)
            )
    if page.gaps:
        out.extend(["", "阻止结论升级为 possible 的缺据："])
        out.extend(f"- [{gap.code}] {gap.message}" for gap in page.gaps)
    return "\n".join(out)


def _citation(
    *,
    chunk_id: str,
    title: str,
    url: str,
    text: str,
    source: str = Source.POEDB.value,
    source_priority: int = 1,
    channel: str = "structured",
    max_chars: int | None = MAX_EVIDENCE_CHARS,
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "entity_id": None,
        "title": title,
        "url": url,
        "source": source,
        "source_priority": source_priority,
        "can_support_facts": source_priority <= MAX_FACTUAL_PRIORITY,
        "text": text if max_chars is None else text[:max_chars],
        "channel": channel,
    }


def _citations(report: FeasibilityReport) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    for page_index, page in enumerate(report.page_analyses, start=1):
        page_url = next(
            (
                candidate.url
                for resolution in page.targets
                for candidate in resolution.candidates
                if candidate.url
            ),
            CRAFT_RULE_URL,
        )
        for target_index, resolution in enumerate(page.targets, start=1):
            if not resolution.candidates:
                continue
            citations.append(
                _citation(
                    chunk_id=f"craft_target:{page.page}:{page_index}:{target_index}",
                    title=f"{page.page} 词缀候选：{resolution.target.query}",
                    url=next((c.url for c in resolution.candidates if c.url), page_url),
                    text=_render_target_evidence(resolution),
                )
            )
        citations.append(
            _citation(
                chunk_id=f"craft_solver:{page.page}:{page_index}",
                title=f"{page.page} 基于 POEDB family_list 的确定性组合检查",
                url=page_url,
                text=_render_solver_evidence(page),
                source=DERIVED_SOURCE,
                source_priority=1,
                channel="derived",
                max_chars=None,
            )
        )
    return citations


@REGISTRY.register(
    name="check_mod_combination",
    description=(
        "检查指定底材类别上的多个目标词缀能否共存。返回严格的三态结论："
        "`impossible` 只在候选未截断且所有候选分配均被完整 family_list 冲突证明时出现；"
        "`possible` 需要目标全部解析、无 family 冲突、占位未超前后缀名额，"
        "且该页没有任何缺据；其余一律 `unknown` 并逐项列出缺据。"
        "`possible` 的含义是**这些词缀能共存**，不是「能做出来」。"
        "会列出候选、前后缀类型、来源、占位和独立的求解证据。"
        "本工具**不提供制作步骤、成功率或通货成本**。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "item_class": {
                "type": "string",
                "description": "底材类别或 POEDB 页面名，如「胸甲」「项链」「Amulets」。",
            },
            "targets": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_TARGETS,
                "description": "目标词缀。query 必须使用游戏内词缀原文片段；source 可限定来源。",
                "items": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": MAX_TARGET_QUERY_CHARS,
                            "description": "词缀文本片段、词缀名或内部词缀组名。",
                        },
                        "source": {
                            "type": "string",
                            "maxLength": MAX_TARGET_SOURCE_CHARS,
                            "description": "可选来源限定，如 normal、Base、工艺工作台、精华。",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            "rarity": {
                "type": "string",
                "description": (
                    "物品稀有度，如「魔法」「稀有」。**不给就判不了前后缀名额**，结论会停在 unknown——不会替用户假定稀有。"
                ),
            },
            "influences": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选底材影响力列表。影响力规则未入库时明确返回 unknown。",
            },
        },
        "required": ["item_class", "targets"],
        "additionalProperties": False,
    },
)
def check_mod_combination(
    item_class: str,
    targets: list[dict[str, Any]],
    rarity: str | None = None,
    influences: list[str] | None = None,
) -> ToolResult:
    if not (item_class or "").strip():
        return ToolResult(ok=False, error="item_class 不能为空")
    try:
        report = analyze_feasibility(
            dbmod.get_conn(),
            item_class=item_class,
            targets=targets,
            rarity=rarity,
            influences=influences,
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))

    return ToolResult(
        ok=True,
        data=report.to_dict(),
        citations=_citations(report),
        meta={
            "channel": "structured",
            "table": "mod_affix",
            "analysis": "craft_feasibility",
        },
    )


#: 名额规则表的列 → 卡片上的说法。与 `kb/render_craft_rule` 共用同一套措辞，
#: 两处分叉就会出现「卡片说 3、工具说 3 条」这种同一事实两种写法。
_KIND_ZH = {
    "default": "一般装备", "flask": "药剂", "jewel": "珠宝",
    "jewel_abyss_corrupted": "已腐化的深渊珠宝", "graft": "移植物（Graft）",
}
_AGREEMENT_ZH = {
    "both": "POEDB 与 PoB 两侧一致",
    "poedb_only": "仅 POEDB 载明",
    "pob_only": "仅 PoB 载明",
}


@REGISTRY.register(
    name="affix_capacity_rules",
    description=(
        "查某稀有度的装备**最多能有几条前缀、几条后缀**（魔法 1+1、稀有 3+3、"
        "传奇不受数量限制、药剂随魔法，另有珠宝与移植物的 2+2 与"
        "「已腐化的深渊珠宝回到 3+3」这条例外）。不传 rarity 就返回全部 7 条。"
        "名额只约束**随机的**前后缀；腐化、附魔、灭界者等其他生成位不占名额。"
        "本工具只给名额，**不判断某组词缀能不能共存**（那是 check_mod_combination）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "rarity": {
                "type": "string",
                "description": "可选稀有度，如「稀有」「魔法」「传奇」。不给就全列。",
            },
        },
        "additionalProperties": False,
    },
)
def affix_capacity_rules(rarity: str | None = None) -> ToolResult:
    """名额规则的**查表**出口。

    为什么要有它：这张表第三十七轮入库时只做了卡片，而那张卡在 9.4 万条词缀阶梯
    chunk 里**连「前缀名额」都进不了 top20**（第四十四轮实测）——
    卡片存在但不可达，等于这个能力是死的。这与幽魂倍率表、Boss 技能伤害
    「没有卡片、只能靠工具到达」是同一条：**精确事实走结构化工具，不指望 BM25**。
    """
    conn = dbmod.get_conn()
    sql = "SELECT * FROM craft_affix_capacity"
    args: tuple[str, ...] = ()
    wanted = (rarity or "").strip()
    if wanted:
        sql += " WHERE rarity = ?"
        args = (wanted,)
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY rarity, item_kind", args)]
    if not rows:
        known = [r[0] for r in conn.execute(
            "SELECT DISTINCT rarity FROM craft_affix_capacity")]
        return ToolResult(
            ok=True,
            data={"found": False, "rarity": wanted, "known_rarities": known,
                  "reason": f"库里没有稀有度「{wanted}」的名额规则"},
        )

    lines = [
        "### 前后缀名额规则（稀有度 × 物品种类）",
        "",
        "| 稀有度 | 物品种类 | 前缀上限 | 后缀上限 | 来源 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        prefix, suffix = row["prefix_limit"], row["suffix_limit"]
        left = "不受前后缀数量限制" if prefix is None else str(prefix)
        right = "" if prefix is None else str(suffix)
        lines.append(
            f"| {row['rarity']} | {_KIND_ZH.get(row['item_kind'], row['item_kind'])} "
            f"| {left} | {right} | {_AGREEMENT_ZH.get(row['agreement'], '')} |"
        )
    lines.append("")
    for row in rows:
        if row["poedb_text"]:
            lines.append(f"- 页面原文：{row['poedb_text']}")
    lines.append("")
    lines.append(
        "名额只约束**随机的**前后缀。固定词缀（部分底材自带）与腐化、附魔、"
        "灭界者等其他生成位各有各的规则，不占随机前后缀名额。"
    )
    text = "\n".join(lines)
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "rules": [
                {"rarity": r["rarity"], "item_kind": r["item_kind"],
                 "prefix_limit": r["prefix_limit"], "suffix_limit": r["suffix_limit"],
                 "agreement": r["agreement"]}
                for r in rows
            ],
            "boundary": (
                "名额只约束随机前后缀；腐化 / 附魔 / 灭界者等生成位不占名额。"
                "本表不判断某组词缀能否共存"
            ),
        },
        citations=[{
            "text": text,
            "source": rows[0]["source"],
            "url": rows[0]["url"],
            "priority": int(rows[0]["source_priority"]),
            "claim_scope": "affix_capacity",
        }],
        meta={"channel": "structured", "table": "craft_affix_capacity"},
    )
