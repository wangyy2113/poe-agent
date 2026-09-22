"""目标词缀组合的保守可行性分析。

本模块只回答当前结构化证据能证明的约束：

* 目标词缀是否出现在指定 POEDB 底材页面；
* 候选分别是前缀、后缀还是其他生成类型；
* 不同候选的完整 ``family_list`` 是否相交；
* 选中候选占用多少个**不同**词缀位（同一条混合词缀可满足多个目标）。

**前后缀容量规则第三十七轮已入库**（``craft_affix_capacity``，POEDB 词缀页 ×
PoB ``Classes/Item.lua`` 两侧对账），所以 ``possible`` 从这一版起真的可达——
在它之前这个模块末尾有一行无条件的 ``verdict = "unknown"``，三态里的第三态是死的。

普通与单一影响来源的底材适用条件已接入：给定逐字具体底材时，使用 PoB 的
``tags`` / ``influenceTags`` 与 POEDB 候选的正权重 ``spawn_tags`` 过滤，并可按物等
过滤。其它非常规来源、没有具体底材的影响条件以及多影响组合仍追加缺口。所以判据
不写成「容量过了就 possible」，而是 **「这一页一条缺口都没有才 possible」**。

容量判定的顺序本身就是安全边界，四种情况一律留在 ``unknown``：

* 候选里有 ``gen_type=0``（其他生成位：腐化 / 附魔 / 灭界者 / 虚空忆境 …），
  那些槽位各有各的规则，本库没采集；
* 没给稀有度——**不替用户假定「稀有」**（与「不替用户假定打 83 级」同一条）；
* 库里查不到该 (稀有度 × 种类) 的规则；
* 规则说「不受限制」（传奇）——那不代表这些词缀能同时出现在一件传奇上，
  传奇的词缀是固定的，不由前后缀名额决定。

``possible`` 的含义仍然只是**「这些词缀能共存」**，不是「能做出来」：
不含制作步骤、成功率、通货成本与路线比较。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from craft.capacity import assess_affix_capacity
from craft.influence import (
    INFLUENCE_CATEGORIES,
    BaseInfluenceProfile,
    InfluenceSpec,
    preferred_modifier_pages,
    resolve_base_profile,
    resolve_influence,
    row_matches_base,
    spawn_rule_url,
)
from parsers.item_mods import resolve_item_class

Verdict = Literal["possible", "impossible", "unknown"]
CheckStatus = Literal["pass", "fail", "unknown"]

MAX_CANDIDATES_PER_TARGET = 80
MAX_SEARCH_NODES = 20_000
MAX_PUBLIC_CANDIDATES = 12
MAX_TARGETS = 8
MAX_TARGET_QUERY_CHARS = 80
MAX_TARGET_SOURCE_CHARS = 40
CRAFT_RULE_URL = "https://poedb.tw/cn/metamods"

@dataclass(frozen=True)
class TargetSpec:
    query: str
    source: str | None = None


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    page: str
    mod_code: str
    name_zh: str
    category: str
    category_label: str
    gen_type: int
    gen_label: str
    family_list: tuple[str, ...]
    correct_group: str
    loose_template: str
    sample_texts: tuple[str, ...]
    tiers: tuple[int, ...]
    min_ilvl: int
    spawn_tags: tuple[str, ...]
    url: str

    @property
    def semantic_key(self) -> tuple[Any, ...]:
        """跨页面比较用；排除 page 和逐 T 级变化的 ``mod_code``。"""
        return (
            self.correct_group,
            self.category,
            self.gen_type,
            self.family_list,
            self.loose_template,
        )

    def to_dict(self) -> dict[str, Any]:
        # URL 只经 ToolResult.citations 交付，避免模型在正文里自行拼接来源。
        return {
            "candidate_id": self.candidate_id,
            "page": self.page,
            "mod_code": self.mod_code,
            "name": self.name_zh,
            "source": self.category,
            "source_label": self.category_label,
            "affix_type": self.gen_label or "其他",
            "family_list": list(self.family_list),
            "correct_group": self.correct_group,
            "sample_texts": list(self.sample_texts),
            "tiers": list(self.tiers),
            "minimum_item_level": self.min_ilvl,
            "spawn_tags": list(self.spawn_tags),
        }


@dataclass
class TargetResolution:
    target: TargetSpec
    page: str
    candidates: list[Candidate] = field(default_factory=list)
    total_candidates: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.target.query,
            "requested_source": self.target.source,
            "page": self.page,
            "candidate_count": self.total_candidates,
            "candidates_returned": min(len(self.candidates), MAX_PUBLIC_CANDIDATES),
            "candidates_truncated": self.truncated,
            "candidates": [c.to_dict() for c in self.candidates[:MAX_PUBLIC_CANDIDATES]],
        }


@dataclass(frozen=True)
class ConstraintCheck:
    key: str
    status: CheckStatus
    summary: str
    evidence_urls: tuple[str, ...] = ()
    missing: str = ""

    def to_dict(self) -> dict[str, Any]:
        # evidence_urls 是工具内部装配 Citation 的元数据，不进入模型正文。
        out: dict[str, Any] = {
            "key": self.key,
            "status": self.status,
            "summary": self.summary,
        }
        if self.missing:
            out["missing"] = self.missing
        return out


@dataclass(frozen=True)
class KnowledgeGap:
    code: str
    message: str
    authority_url: str

    def to_dict(self) -> dict[str, str]:
        # 权威 URL 由 Citation 层统一交付；data 只给缺口语义。
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class Conflict:
    left_candidate_id: str
    right_candidate_id: str
    shared_families: tuple[str, ...]
    left_text: str
    right_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_candidate_id": self.left_candidate_id,
            "right_candidate_id": self.right_candidate_id,
            "shared_families": list(self.shared_families),
            "left_text": self.left_text,
            "right_text": self.right_text,
        }


@dataclass
class PageAnalysis:
    page: str
    verdict: Verdict
    targets: list[TargetResolution]
    checks: list[ConstraintCheck] = field(default_factory=list)
    gaps: list[KnowledgeGap] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    selected_candidate_ids: list[str] = field(default_factory=list)
    occupied_prefixes: int | None = None
    occupied_suffixes: int | None = None
    occupied_other: int | None = None
    search_truncated: bool = False
    search_exhaustive: bool | None = None
    search_nodes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "verdict": self.verdict,
            "target_resolutions": [t.to_dict() for t in self.targets],
            "checks": [c.to_dict() for c in self.checks],
            "gaps": [g.to_dict() for g in self.gaps],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "selected_candidate_ids": self.selected_candidate_ids,
            "affix_occupancy": {
                "prefixes": self.occupied_prefixes,
                "suffixes": self.occupied_suffixes,
                "other": self.occupied_other,
                "note": "按不同 candidate_id 计数；同一条混合词缀满足多个目标时只占一个位置",
            },
            "search_truncated": self.search_truncated,
            "search_exhaustive": self.search_exhaustive,
            "search_nodes": self.search_nodes,
        }


@dataclass
class FeasibilityReport:
    verdict: Verdict
    item_class: str
    rarity: str | None
    influences: list[str]
    base_type: str | None
    item_level: int | None
    pages: list[str]
    scope_note: str
    base_profile: BaseInfluenceProfile | None = None
    page_analyses: list[PageAnalysis] = field(default_factory=list)
    checks: list[ConstraintCheck] = field(default_factory=list)
    gaps: list[KnowledgeGap] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        # 故意没有 steps / probability / cost：这三类输入尚未齐备。
        return {
            "verdict": self.verdict,
            "item_class": self.item_class,
            "rarity": self.rarity,
            "influences": self.influences,
            "base_type": self.base_type,
            "item_level": self.item_level,
            "pages_searched": self.pages,
            "scope_note": self.scope_note,
            "base_profile": self.base_profile.to_dict() if self.base_profile else None,
            "checks": [c.to_dict() for c in self.checks],
            "gaps": [g.to_dict() for g in self.gaps],
            "page_analyses": [p.to_dict() for p in self.page_analyses],
            "boundary": (
                "本结果只检查目标候选、前后缀类型、完整 family_list 互斥与已知来源；"
                "不提供制作步骤、成功率或通货成本"
            ),
        }

    def evidence_candidates(self) -> list[Candidate]:
        seen: set[str] = set()
        out: list[Candidate] = []
        for page in self.page_analyses:
            for target in page.targets:
                for candidate in target.candidates:
                    if candidate.candidate_id in seen:
                        continue
                    seen.add(candidate.candidate_id)
                    out.append(candidate)
        return out


def families_conflict(a: Candidate, b: Candidate) -> tuple[str, ...]:
    """按完整 family_list 求交集；同一候选复用不构成自冲突。"""
    if a.candidate_id == b.candidate_id:
        return ()
    return tuple(sorted(set(a.family_list) & set(b.family_list)))


def _parse_targets(raw_targets: list[dict[str, Any] | str]) -> list[TargetSpec]:
    targets: list[TargetSpec] = []
    for index, raw in enumerate(raw_targets):
        if isinstance(raw, str):
            query, source = raw.strip(), None
        elif isinstance(raw, dict):
            query = str(raw.get("query") or "").strip()
            source_raw = raw.get("source") or raw.get("optional_source")
            source = str(source_raw).strip() if source_raw else None
        else:
            raise ValueError(f"targets[{index}] 必须是字符串或对象")
        if not query:
            raise ValueError(f"targets[{index}].query 不能为空")
        if len(query) > MAX_TARGET_QUERY_CHARS:
            raise ValueError(
                f"targets[{index}].query 最多允许 {MAX_TARGET_QUERY_CHARS} 个字符"
            )
        if source and len(source) > MAX_TARGET_SOURCE_CHARS:
            raise ValueError(
                f"targets[{index}].source 最多允许 {MAX_TARGET_SOURCE_CHARS} 个字符"
            )
        targets.append(TargetSpec(query=query, source=source))
    if not targets:
        raise ValueError("targets 至少需要一项")
    if len(targets) > MAX_TARGETS:
        raise ValueError(
            f"targets 最多允许 {MAX_TARGETS} 项；超过后无法在单条 Citation 中完整审计输入"
        )
    return targets


def _json_list(value: Any) -> tuple[str, ...]:
    try:
        data = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return ()
    return tuple(str(v) for v in data if v is not None) if isinstance(data, list) else ()


def _candidate_id(page: str, key: tuple[Any, ...]) -> str:
    raw = json.dumps([page, *key], ensure_ascii=False, sort_keys=True, default=list)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]  # noqa: S324 非安全用途


def resolve_target_candidates(
    conn: sqlite3.Connection,
    page: str,
    target: TargetSpec,
    *,
    base_profile: BaseInfluenceProfile | None = None,
    requested_influences: tuple[InfluenceSpec, ...] = (),
    item_level: int | None = None,
) -> TargetResolution:
    """在一个确定的 POEDB 底材页面上解析目标候选。

    这里只做字面包含匹配，不做同义词猜测。多档同一词缀按实质身份折叠，避免把 T1/T2
    误算成两个可同时占位的词缀。
    """
    q = target.query
    sql = (
        "SELECT * FROM mod_affix WHERE item_class_page=? "
        "AND (text_zh LIKE ? OR name_zh LIKE ? OR correct_group LIKE ?)"
    )
    params: list[Any] = [page, f"%{q}%", f"%{q}%", f"%{q}%"]
    if target.source:
        sql += " AND (category=? OR category_label=?)"
        params.extend([target.source, target.source])
    if item_level is not None:
        sql += " AND ilvl<=?"
        params.append(item_level)
    sql += " ORDER BY category, gen_type, correct_group, tier, ilvl"

    rows = conn.execute(sql, params).fetchall()
    grouped: dict[tuple[Any, ...], list[sqlite3.Row]] = {}
    for row in rows:
        spawn_tags = _json_list(row["spawn_tags"])
        if base_profile is not None and not row_matches_base(
            category=str(row["category"] or ""),
            spawn_tags=spawn_tags,
            profile=base_profile,
            requested=requested_influences,
        ):
            continue
        families = _json_list(row["family_list"])
        # POEDB 的 mod_code 在真实库中可能按 T 级变化，不能拿它作为梯度身份。
        # correct_group、来源、生成类型、完整 family_list 与模板共同描述一条可占位
        # 的稳定候选；混合词缀因模板/family 不同仍保持独立。
        key = (
            row["correct_group"] or "",
            row["category"] or "",
            int(row["gen_type"] or 0),
            families,
            row["loose_template"] or "",
        )
        grouped.setdefault(key, []).append(row)

    candidates: list[Candidate] = []
    for key, group in grouped.items():
        first = group[0]
        texts = tuple(dict.fromkeys(str(r["text_zh"] or "") for r in group if r["text_zh"]))
        tiers = tuple(sorted({int(r["tier"] or 0) for r in group if r["tier"] is not None}))
        candidates.append(
            Candidate(
                candidate_id=_candidate_id(page, key),
                page=page,
                mod_code=str(first["mod_code"] or ""),
                name_zh=str(first["name_zh"] or ""),
                category=str(first["category"] or ""),
                category_label=str(first["category_label"] or first["category"] or ""),
                gen_type=int(first["gen_type"] or 0),
                gen_label=str(first["gen_label"] or "其他"),
                family_list=key[3],
                correct_group=str(first["correct_group"] or ""),
                loose_template=str(first["loose_template"] or ""),
                sample_texts=texts[:3],
                tiers=tiers,
                min_ilvl=min(int(r["ilvl"] or 0) for r in group),
                spawn_tags=tuple(
                    dict.fromkeys(
                        tag for r in group for tag in _json_list(r["spawn_tags"])
                    )
                ),
                url=str(first["url"] or ""),
            )
        )

    candidates.sort(
        key=lambda c: (
            {"normal": 0, "master": 1, "essence": 2}.get(c.category, 3),
            c.gen_type,
            c.correct_group,
            c.mod_code,
        )
    )
    truncated = len(candidates) > MAX_CANDIDATES_PER_TARGET
    return TargetResolution(
        target=target,
        page=page,
        candidates=candidates[:MAX_CANDIDATES_PER_TARGET],
        total_candidates=len(candidates),
        truncated=truncated,
    )


@dataclass
class _SearchResult:
    assignment: list[Candidate] | None
    exhaustive: bool
    nodes: int
    conflicts: list[Conflict]


def _find_compatible_assignment(resolutions: list[TargetResolution]) -> _SearchResult:
    """找一组兼容候选；只有穷尽搜索后仍无解，才允许证明 family 冲突。"""
    ordered = sorted(resolutions, key=lambda r: len(r.candidates))
    nodes = 0
    cutoff = False
    witnesses: dict[tuple[str, str, tuple[str, ...]], Conflict] = {}
    found: list[Candidate] | None = None

    def visit(index: int, chosen: dict[str, Candidate], assigned: list[Candidate]) -> bool:
        nonlocal nodes, cutoff, found
        if cutoff:
            return False
        if index == len(ordered):
            found = list(assigned)
            return True
        for candidate in ordered[index].candidates:
            nodes += 1
            if nodes > MAX_SEARCH_NODES:
                cutoff = True
                return False
            conflict = None
            for existing in chosen.values():
                shared = families_conflict(candidate, existing)
                if shared:
                    conflict = (existing, shared)
                    pair = tuple(sorted((candidate.candidate_id, existing.candidate_id)))
                    key = (pair[0], pair[1], shared)
                    witnesses.setdefault(
                        key,
                        Conflict(
                            left_candidate_id=existing.candidate_id,
                            right_candidate_id=candidate.candidate_id,
                            shared_families=shared,
                            left_text=(
                                existing.sample_texts[0]
                                if existing.sample_texts else existing.mod_code
                            ),
                            right_text=(
                                candidate.sample_texts[0]
                                if candidate.sample_texts else candidate.mod_code
                            ),
                        ),
                    )
                    break
            if conflict is not None:
                continue
            already = candidate.candidate_id in chosen
            if not already:
                chosen[candidate.candidate_id] = candidate
            assigned.append(candidate)
            if visit(index + 1, chosen, assigned):
                return True
            assigned.pop()
            if not already:
                chosen.pop(candidate.candidate_id, None)
        return False

    success = visit(0, {}, [])
    return _SearchResult(
        assignment=found if success else None,
        exhaustive=not success and not cutoff,
        nodes=nodes,
        conflicts=list(witnesses.values())[:24],
    )


def _gap(code: str, message: str) -> KnowledgeGap:
    return KnowledgeGap(code=code, message=message, authority_url=CRAFT_RULE_URL)


@dataclass(frozen=True)
class _CapacityOutcome:
    """容量判定的结果。`gap` 为 None 表示这一维没有拦住结论。"""

    check: ConstraintCheck
    gap: KnowledgeGap | None


def _check_affix_capacity(
    conn: sqlite3.Connection,
    rarity: str | None,
    analysis: PageAnalysis,
    occupancy: str,
    urls: tuple[str, ...],
) -> _CapacityOutcome:
    """按名额规则判定占位是否超限。顺序即安全边界，见模块 docstring。"""

    def unknown(message: str) -> _CapacityOutcome:
        return _CapacityOutcome(
            check=ConstraintCheck(
                key="affix_capacity",
                status="unknown",
                summary=occupancy,
                evidence_urls=urls,
                missing=message,
            ),
            gap=_gap("affix_capacity", message),
        )

    if analysis.occupied_other:
        return unknown(
            f"该分配里有 {analysis.occupied_other} 个候选落在**其他生成位**"
            "（腐化 / 附魔 / 灭界者 / 虚空忆境等），这些槽位的名额规则未采集，"
            "无法验证是否超限"
        )
    assessment = assess_affix_capacity(
        conn,
        rarity,
        analysis.occupied_prefixes or 0,
        analysis.occupied_suffixes or 0,
        occupancy=occupancy,
    )
    evidence_urls = tuple(
        dict.fromkeys((*urls, *([assessment.rule_url] if assessment.rule_url else [])))
    )
    check = ConstraintCheck(
        key="affix_capacity",
        status=assessment.status,
        summary=assessment.summary,
        evidence_urls=evidence_urls,
        missing=assessment.missing,
    )
    return _CapacityOutcome(
        check=check,
        gap=_gap("affix_capacity", assessment.missing)
        if assessment.status == "unknown" else None,
    )


def _dedupe_gaps(gaps: list[KnowledgeGap]) -> list[KnowledgeGap]:
    seen: set[tuple[str, str]] = set()
    out: list[KnowledgeGap] = []
    for gap in gaps:
        key = (gap.code, gap.message)
        if key not in seen:
            seen.add(key)
            out.append(gap)
    return out


def _analyze_page(
    conn: sqlite3.Connection,
    page: str,
    targets: list[TargetSpec],
    influences: list[str],
    rarity: str | None = None,
    *,
    base_profile: BaseInfluenceProfile | None = None,
    requested_influences: tuple[InfluenceSpec, ...] = (),
    item_level: int | None = None,
) -> PageAnalysis:
    resolutions = [
        resolve_target_candidates(
            conn,
            page,
            target,
            base_profile=base_profile,
            requested_influences=requested_influences,
            item_level=item_level,
        )
        for target in targets
    ]
    analysis = PageAnalysis(page=page, verdict="unknown", targets=resolutions)

    missing: list[TargetResolution] = []
    for resolution in resolutions:
        if not resolution.candidates:
            missing.append(resolution)
            source_note = (
                f"，来源限定「{resolution.target.source}」"
                if resolution.target.source else ""
            )
            message = (
                f"页面 {page} 未找到目标「{resolution.target.query}」"
                f"{source_note}的字面候选"
            )
            analysis.checks.append(
                ConstraintCheck(
                    key=f"target:{resolution.target.query}",
                    status="unknown",
                    summary=message,
                    missing="需要更精确的游戏内词缀原文，或补充该来源的结构化数据",
                )
            )
            analysis.gaps.append(_gap("target_not_resolved", message))
            continue

        urls = tuple(dict.fromkeys(c.url for c in resolution.candidates if c.url))
        analysis.checks.append(
            ConstraintCheck(
                key=f"target:{resolution.target.query}",
                status="pass",
                summary=(
                    f"页面 {page} 找到 {resolution.total_candidates} 个候选定义；"
                    "多档同一词缀已折叠为一个候选"
                ),
                evidence_urls=urls,
            )
        )
        if resolution.truncated:
            analysis.search_truncated = True

    # 用户显式给出的影响力条件与目标是否召回无关，必须独立报告，不能被早退吞掉。
    if influences:
        if base_profile is None:
            influence_message = (
                "需要逐字提供具体底材，才能用 PoB influenceTags 验证请求的影响力"
            )
            analysis.checks.append(
                ConstraintCheck(
                    key="influence_requirement",
                    status="unknown",
                    summary=f"请求的影响力：{'、'.join(influences)}",
                    missing=influence_message,
                )
            )
            analysis.gaps.append(_gap("influence_requirement", influence_message))
        elif len(requested_influences) != len(influences):
            influence_message = "至少一个影响名无法映射到 PoB 的六种传统影响键"
            analysis.checks.append(
                ConstraintCheck(
                    key="influence_requirement",
                    status="unknown",
                    summary=f"请求的影响力：{'、'.join(influences)}",
                    evidence_urls=(base_profile.pob_url,),
                    missing=influence_message,
                )
            )
            analysis.gaps.append(_gap("influence_requirement", influence_message))
        elif len(requested_influences) > 1:
            influence_message = "多个影响同时存在的数量与组合规则尚未结构化"
            analysis.checks.append(
                ConstraintCheck(
                    key="influence_requirement",
                    status="unknown",
                    summary=f"底材支持逐项标签，但请求了 {len(requested_influences)} 种影响",
                    evidence_urls=(base_profile.pob_url,),
                    missing=influence_message,
                )
            )
            analysis.gaps.append(_gap("influence_requirement", influence_message))
        else:
            spec = requested_influences[0]
            generation_tag = base_profile.generation_tag(spec)
            if generation_tag:
                analysis.checks.append(
                    ConstraintCheck(
                        key="influence_requirement",
                        status="pass",
                        summary=(
                            f"底材 {base_profile.name_zh} 的 PoB influenceTags 包含"
                            f"{spec.name_zh}生成标签 {generation_tag}"
                        ),
                        evidence_urls=(
                            base_profile.pob_url,
                            spawn_rule_url(base_profile.source_commit),
                        ),
                    )
                )
            else:
                analysis.checks.append(
                    ConstraintCheck(
                        key="influence_requirement",
                        status="fail",
                        summary=(
                            f"底材 {base_profile.name_zh} 的 PoB influenceTags 不包含"
                            f"{spec.name_zh}"
                        ),
                        evidence_urls=(base_profile.pob_url,),
                    )
                )
                analysis.verdict = "impossible"

    if analysis.search_truncated:
        message = f"页面 {page} 的至少一个目标候选超过分析上限，不能穷尽全部分配"
        analysis.checks.append(
            ConstraintCheck(
                key="family_exclusivity",
                status="unknown",
                summary=message,
                missing="需要缩小目标词缀或指定来源",
            )
        )
        analysis.gaps.append(_gap("candidate_search_truncated", message))

    if missing:
        if not analysis.search_truncated:
            analysis.checks.append(
                ConstraintCheck(
                    key="family_exclusivity",
                    status="unknown",
                    summary="至少一个目标没有候选，无法构造完整候选分配",
                    missing="需要先解析全部目标词缀",
                )
            )
        return analysis
    if analysis.search_truncated:
        return analysis

    search = _find_compatible_assignment(resolutions)
    analysis.conflicts = search.conflicts
    analysis.search_nodes = search.nodes
    analysis.search_exhaustive = search.exhaustive if search.assignment is None else False
    if search.assignment is None:
        if search.exhaustive:
            urls = tuple(
                dict.fromkeys(
                    c.url for resolution in resolutions for c in resolution.candidates if c.url
                )
            )
            analysis.verdict = "impossible"
            analysis.checks.append(
                ConstraintCheck(
                    key="family_exclusivity",
                    status="fail",
                    summary="所有候选分配都存在完整 family_list 交集，组合必然互斥",
                    evidence_urls=urls,
                )
            )
        else:
            message = f"页面 {page} 的候选组合搜索超过 {MAX_SEARCH_NODES} 个节点"
            analysis.checks.append(
                ConstraintCheck(
                    key="family_exclusivity",
                    status="unknown",
                    summary=message,
                    missing="需要缩小目标词缀或指定来源后重新分析",
                )
            )
            analysis.gaps.append(_gap("assignment_search_truncated", message))
        return analysis

    unique = {candidate.candidate_id: candidate for candidate in search.assignment}
    analysis.selected_candidate_ids = list(unique)
    analysis.occupied_prefixes = sum(c.gen_type == 1 for c in unique.values())
    analysis.occupied_suffixes = sum(c.gen_type == 2 for c in unique.values())
    analysis.occupied_other = sum(c.gen_type not in (1, 2) for c in unique.values())
    urls = tuple(dict.fromkeys(c.url for c in unique.values() if c.url))
    analysis.checks.append(
        ConstraintCheck(
            key="family_exclusivity",
            status="pass",
            summary=(
                "至少找到一组候选，其不同词缀之间没有完整 family_list 交集；"
                "这只证明局部兼容，不等于整体可制作"
            ),
            evidence_urls=urls,
        )
    )

    occupancy = (
        f"该候选分配占 {analysis.occupied_prefixes} 个前缀、"
        f"{analysis.occupied_suffixes} 个后缀、{analysis.occupied_other} 个其他生成位"
    )
    capacity = _check_affix_capacity(conn, rarity, analysis, occupancy, urls)
    analysis.checks.append(capacity.check)
    if capacity.gap is not None:
        analysis.gaps.append(capacity.gap)
    if capacity.check.status == "fail":
        # 超出名额 = 这些词缀不可能同时出现。这是容量规则带来的**新真负例**。
        analysis.verdict = "impossible"
        return analysis

    validated_influence_categories = {
        spec.category
        for spec in requested_influences
        if base_profile is not None and base_profile.generation_tag(spec)
    }
    special_sources = sorted(
        {
            c.category
            for c in unique.values()
            if c.category != "normal" and c.category not in validated_influence_categories
        }
    )
    if special_sources:
        source_message = (
            "来源适用条件尚未结构化入库，无法验证这些来源能否共同用于该底材："
            + "、".join(special_sources)
        )
        analysis.checks.append(
            ConstraintCheck(
                key="source_requirement",
                status="unknown",
                summary="候选含非常规来源词缀",
                evidence_urls=urls,
                missing=source_message,
            )
        )
        analysis.gaps.append(_gap("source_requirement", source_message))

    selected_categories = {c.category for c in unique.values()}
    if not influences and selected_categories & INFLUENCE_CATEGORIES:
        influence_message = (
            "影响力底材要求尚未结构化入库，无法验证候选词缀与底材影响力是否兼容"
        )
        analysis.checks.append(
            ConstraintCheck(
                key="influence_requirement",
                status="unknown",
                summary="候选含影响力来源标签",
                evidence_urls=urls,
                missing=influence_message,
            )
        )
        analysis.gaps.append(_gap("influence_requirement", influence_message))

    # **判据是「一条缺口都没有」，不是「容量过了」**：尚未接通的特殊来源与
    # 多影响组合只在条件命中时追加缺口。写成「容量 pass 就 possible」会把这些
    # 尚无证据的条件悄悄放行。
    if any(c.key == "influence_requirement" and c.status == "fail" for c in analysis.checks):
        analysis.verdict = "impossible"
    else:
        analysis.verdict = "possible" if not analysis.gaps else "unknown"
    return analysis


def _page_profile(page: PageAnalysis) -> tuple[Any, ...]:
    return tuple(
        (
            resolution.target.query,
            resolution.target.source,
            tuple(sorted(c.semantic_key for c in resolution.candidates)),
        )
        for resolution in page.targets
    )


def analyze_feasibility(
    conn: sqlite3.Connection,
    item_class: str,
    targets: list[dict[str, Any] | str],
    *,
    rarity: str | None = None,
    influences: list[str] | None = None,
    base_type: str | None = None,
    item_level: int | None = None,
) -> FeasibilityReport:
    """分析目标词缀组合，返回 ``possible / impossible / unknown`` 三态报告。"""
    target_specs = _parse_targets(targets)
    influence_list = [str(v).strip() for v in (influences or []) if str(v).strip()]
    if item_level is not None and (
        isinstance(item_level, bool) or not isinstance(item_level, int) or not 1 <= item_level <= 100
    ):
        raise ValueError("item_level 必须是 1 到 100 的整数")
    base_profile = None
    if base_type:
        base_profile, base_reason = resolve_base_profile(conn, base_type)
        if base_profile is None:
            gap = _gap("base_not_resolved", base_reason)
            return FeasibilityReport(
                verdict="unknown",
                item_class=item_class,
                rarity=rarity,
                influences=influence_list,
                base_type=base_type,
                item_level=item_level,
                pages=[],
                scope_note=base_reason,
                checks=[
                    ConstraintCheck(
                        key="base_type",
                        status="unknown",
                        summary=base_reason,
                        missing="需要 pob_base_item 中唯一的逐字中文或英文底材名",
                    )
                ],
                gaps=[gap],
            )
    requested_influences = tuple(
        spec for raw in influence_list if (spec := resolve_influence(raw)) is not None
    )
    item_class_query = (item_class or "").strip()
    if not item_class_query and base_profile is not None:
        for candidate in (
            base_profile.modifier_page,
            base_profile.poedb_slug,
            base_profile.poedb_page,
        ):
            if candidate and resolve_item_class(conn, candidate).ok:
                item_class_query = candidate
                break
    ref = resolve_item_class(conn, item_class_query)
    if not ref.ok:
        message = ref.reason or f"无法解析底材类别「{item_class_query}」"
        gap = _gap("item_class_not_resolved", message)
        return FeasibilityReport(
            verdict="unknown",
            item_class=item_class_query,
            rarity=rarity,
            influences=influence_list,
            base_type=base_type,
            item_level=item_level,
            pages=[],
            scope_note=message,
            checks=[
                ConstraintCheck(
                    key="item_class",
                    status="unknown",
                    summary=message,
                    missing="需要 POEDB 底材类别页或更精确的类别名",
                )
            ],
            gaps=[gap],
        )

    base_page_candidates = (
        {
            value
            for value in (
                base_profile.modifier_page,
                base_profile.poedb_slug,
                base_profile.poedb_page,
            )
            if value
        }
        if base_profile is not None else set()
    )
    if base_profile is not None and not (base_page_candidates & set(ref.pages)):
        message = (
            f"底材 {base_profile.name_zh} 的 ItemClass 是 {base_profile.item_class}，"
            f"与请求类别 {item_class_query!r}（{ref.code}）不一致"
        )
        return FeasibilityReport(
            verdict="unknown",
            item_class=item_class_query,
            rarity=rarity,
            influences=influence_list,
            base_type=base_type,
            item_level=item_level,
            pages=[],
            scope_note=message,
            checks=[ConstraintCheck(key="base_item_class", status="unknown", summary=message)],
            gaps=[_gap("base_item_class", message)],
        )

    pages = (
        preferred_modifier_pages(base_profile, ref.pages)
        if base_profile is not None else ref.pages
    )

    page_analyses = [
        _analyze_page(
            conn,
            page,
            target_specs,
            influence_list,
            rarity,
            base_profile=base_profile,
            requested_influences=requested_influences,
            item_level=item_level,
        )
        for page in pages
    ]
    checks: list[ConstraintCheck] = []
    gaps = [gap for page in page_analyses for gap in page.gaps]
    scope_note = (
        (
            f"具体底材「{base_profile.name_zh}（{base_profile.name_en}）」"
            f"命中页面 {pages[0]}"
            if base_profile is not None and len(pages) == 1
            else f"底材类别「{item_class_query}」精确命中页面 {pages[0]}"
        )
        if len(pages) == 1
        else f"底材类别「{item_class_query}」对应 {len(pages)} 个 POEDB 页面，已分别分析"
    )

    profiles_equal = len({_page_profile(page) for page in page_analyses}) == 1
    if len(page_analyses) > 1:
        if profiles_equal:
            checks.append(
                ConstraintCheck(
                    key="base_page_scope",
                    status="pass",
                    summary="各底材页面的目标候选定义一致",
                    evidence_urls=tuple(
                        dict.fromkeys(
                            c.url
                            for page in page_analyses
                            for target in page.targets
                            for c in target.candidates
                            if c.url
                        )
                    ),
                )
            )
        else:
            message = (
                "同一中文底材类别下的 POEDB 页面给出不同候选集合；未提供具体底材，"
                "不能把其中一个页面的结论套到全部底材"
            )
            checks.append(
                ConstraintCheck(
                    key="base_page_scope",
                    status="unknown",
                    summary=message,
                    missing="需要具体底材以确定唯一 POEDB 页面",
                )
            )
            gaps.append(_gap("base_page_scope", message))

    if not profiles_equal:
        verdict: Verdict = "unknown"
    elif page_analyses and all(page.verdict == "impossible" for page in page_analyses):
        verdict = "impossible"
    elif page_analyses and all(page.verdict == "possible" for page in page_analyses):
        verdict = "possible"
    else:
        verdict = "unknown"

    return FeasibilityReport(
        verdict=verdict,
        item_class=item_class_query,
        rarity=rarity,
        influences=influence_list,
        base_type=base_type,
        item_level=item_level,
        pages=pages,
        scope_note=scope_note,
        base_profile=base_profile,
        page_analyses=page_analyses,
        checks=checks,
        gaps=_dedupe_gaps(gaps),
    )
