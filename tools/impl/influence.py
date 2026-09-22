"""Exact base-item influence eligibility lookup."""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.influence import (
    INFLUENCES,
    influence_info_url,
    resolve_base_profile,
    resolve_influence,
    spawn_rule_url,
)
from entities.taxonomy import Source
from tools.registry import REGISTRY, ToolResult

MAX_BASE_TYPE_CHARS = 100


def _render(profile, requested=None) -> str:  # noqa: ANN001
    rows = []
    for spec in INFLUENCES:
        generation_tag = profile.generation_tag(spec)
        if requested is not None and spec != requested:
            continue
        rows.append(
            f"| {spec.name_zh} | {spec.name_en} | {spec.key} | "
            f"{generation_tag or '不支持'} | {'是' if generation_tag else '否'} |"
        )
    return "\n".join(
        [
            f"### {profile.name_zh}（{profile.name_en}）影响力底材能力",
            "",
            f"- ItemClass：{profile.item_class}",
            f"- PoB 底材标签：{', '.join(profile.tags)}",
            "",
            "| 影响 | PoB 显示名 | 内部键 | 生成标签 | 底材支持 |",
            "| --- | --- | --- | --- | --- |",
            *rows,
            "",
            "> 判据：PoB 的底材 `influenceTags` 映射存在该键，即该底材可提供对应影响生成标签；",
            "> 词缀适用性还需让目标词缀的正权重 `spawn_tags` 命中这个生成标签。",
            "> 边界：不说明影响底材的获得方式、影响数量限制、觉醒者之石状态转移、制作步骤或概率。",
        ]
    )


def _citations(profile, rendered: str) -> list[dict[str, Any]]:  # noqa: ANN001
    return [
        {
            "chunk_id": f"base_influence:{profile.base_id}",
            "entity_id": None,
            "title": f"{profile.name_zh} · PoB 底材与 influenceTags",
            "url": profile.pob_url,
            "source": Source.POB.value,
            "source_priority": 2,
            "can_support_facts": True,
            "text": rendered,
            "channel": "structured",
        },
        {
            "chunk_id": f"influence_mapping:{profile.source_commit}",
            "entity_id": None,
            "title": "PoB 影响显示名与内部键映射",
            "url": influence_info_url(profile.source_commit),
            "source": Source.POB.value,
            "source_priority": 2,
            "can_support_facts": True,
            "text": (
                "PoB ItemTools.lua 的 influenceInfo.all 映射："
                + "；".join(
                    f"{spec.name_en}={spec.key}" for spec in INFLUENCES
                )
            ),
            "channel": "structured",
        },
        {
            "chunk_id": f"influence_spawn_rule:{profile.source_commit}",
            "entity_id": None,
            "title": "PoB 词缀生成权重的底材与影响标签匹配规则",
            "url": spawn_rule_url(profile.source_commit),
            "source": Source.POB.value,
            "source_priority": 2,
            "can_support_facts": True,
            "text": (
                "PoB Item.GetModSpawnWeight：词缀权重键命中 base.tags、includeTags 或当前"
                "底材 influenceTags 中已启用影响对应的标签时，采用该键的生成权重。"
            ),
            "channel": "structured",
        },
    ]


@REGISTRY.register(
    name="base_influence_profile",
    description=(
        "按逐字中文或英文底材名查询 PoB 的传统影响力能力。返回塑界者、裂界者、督军、"
        "狩猎者、圣战者与救赎者对应的内部生成标签；可指定一种影响。"
        "只证明底材标签适用性，不说明获得方式、影响数量限制、觉醒者之石、步骤或概率。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "base_type": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_BASE_TYPE_CHARS,
                "description": "pob_base_item 中逐字中文或英文底材名。",
            },
            "influence": {
                "type": "string",
                "maxLength": 40,
                "description": "可选影响名，如猎人、狩猎者、Hunter。",
            },
        },
        "required": ["base_type"],
        "additionalProperties": False,
    },
)
def base_influence_profile(
    base_type: str, influence: str | None = None
) -> ToolResult:
    if len(base_type or "") > MAX_BASE_TYPE_CHARS:
        return ToolResult(ok=False, error=f"base_type 最多允许 {MAX_BASE_TYPE_CHARS} 个字符")
    profile, reason = resolve_base_profile(dbmod.get_conn(), base_type)
    if profile is None:
        return ToolResult(
            ok=True,
            data={"found": False, "base_type": base_type, "reason": reason},
            citations=[],
            meta={"channel": "structured", "table": "pob_base_item"},
        )
    requested = None
    if influence:
        requested = resolve_influence(influence)
        if requested is None:
            return ToolResult(
                ok=False,
                error=(
                    "influence 必须是已登记的六种传统影响名之一："
                    + "、".join(spec.name_zh for spec in INFLUENCES)
                ),
            )
    rendered = _render(profile, requested)
    data = profile.to_dict()
    if requested is not None:
        generation_tag = profile.generation_tag(requested)
        data["requested_influence"] = {
            "key": requested.key,
            "category": requested.category,
            "name_zh": requested.name_zh,
            "name_en": requested.name_en,
            "supported": bool(generation_tag),
            "generation_tag": generation_tag or None,
        }
    data.update(
        {
            "found": True,
            "answer": rendered,
            "boundary": (
                "只证明底材 influenceTags 与词缀生成标签适用性；不提供获得方式、"
                "影响数量限制、觉醒者之石状态转移、步骤或概率"
            ),
        }
    )
    return ToolResult(
        ok=True,
        data=data,
        citations=_citations(profile, rendered),
        meta={"channel": "structured", "table": "pob_base_item"},
    )
