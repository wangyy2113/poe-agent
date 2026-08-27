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
