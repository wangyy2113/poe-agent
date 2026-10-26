"""胸甲普通词缀有效池与条件权重概率精确工具。"""

from __future__ import annotations

from typing import Any

from core import db as dbmod
from craft.pool import (
    MAX_BASE_CHARS,
    MAX_ITEM_LEVEL,
    MAX_TARGET_CHARS,
    MIN_ITEM_LEVEL,
    PoolOddsReport,
    analyze_affix_pool,
)
from entities.taxonomy import MAX_FACTUAL_PRIORITY, Source
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 5000
_SIDE_ZH = {"prefix": "前缀", "suffix": "后缀"}
_MAPPING_VERSION = "pob_tag_plus_poedb_identity_v1"


def _pob_base_citation(report: PoolOddsReport) -> dict[str, Any]:
    """P2 只支撑 PoB 原文中的英文底材身份、标签与版本。"""
    assert report.base is not None
    base = report.base
    return {
        "chunk_id": f"affix_pool_pob_base:{base.base_id}",
        "entity_id": None,
        "title": f"PoB 胸甲底材：{base.name_en}",
        "url": base.pob_url,
        "source": Source.POB.value,
        "source_priority": 2,
        "can_support_facts": 2 <= MAX_FACTUAL_PRIORITY,
        "claim_scope": "affix_pool_odds",
        "claim_data": {
            "kind": "pob_base",
            "base_id": base.base_id,
            "name_en": base.name_en,
            "armour_tag": base.armour_tag,
            "source_commit": base.source_commit,
            "pob_version": base.pob_version,
            "game_version": base.game_version,
            "pob_url": base.pob_url,
        },
        "text": (
            "### Path of Building 胸甲底材记录\n\n"
            f"- 英文名：{base.name_en}\n"
            f"- 护甲标签：{base.armour_tag}\n"
            f"- PoB commit：{base.source_commit}\n"
            f"- PoB 版本：{base.pob_version}\n"
            f"- 游戏版本字段：{base.game_version}\n\n"
            "> 边界：该 P2 证据不支撑中文名或 POEDB 页面；这两项必须与独立的"
            " POEDB 身份桥接证据共同使用。"
        ),
        "channel": "structured",
    }


def _base_mapping_citation(report: PoolOddsReport) -> dict[str, Any]:
    """显式表示 POEDB 身份与 PoB tag 到 modifier page 的跨源派生。"""
    assert report.base is not None
    base = report.base
    return {
        "chunk_id": f"affix_pool_base_mapping:{base.base_id}:{base.modifier_page}",
        "entity_id": None,
        "title": f"胸甲底材跨源映射：{base.name_zh}（{base.name_en}）",
        "url": base.poedb_url,
        "source": Source.POEDB.value,
        "source_priority": 1,
        "can_support_facts": True,
        "claim_scope": "affix_pool_odds",
        "claim_data": {
            "kind": "base_mapping",
            "mapping_version": _MAPPING_VERSION,
            "base_id": base.base_id,
            "name_en": base.name_en,
            "name_zh": base.name_zh,
            "poedb_slug": base.poedb_slug,
            "armour_tag": base.armour_tag,
            "modifier_page": base.modifier_page,
            "pob_url": base.pob_url,
            "poedb_url": base.poedb_url,
        },
        "text": (
            "### POEDB 身份 × PoB 标签映射\n\n"
            f"- POEDB 中文名：{base.name_zh}\n"
            f"- POEDB 同 slug 英文名：{base.name_en}\n"
            f"- POEDB 底材 URL：{base.poedb_url}\n"
            f"- PoB 标签：{base.armour_tag}\n"
            f"- 派生 POEDB 词缀页：{base.modifier_page}\n"
            f"- 映射算法：{_MAPPING_VERSION}\n"
            f"- PoB 输入 URL：{base.pob_url}\n\n"
            "> 边界：中文/英文同一身份来自 POEDB；护甲标签来自 PoB；词缀页是两者"
            "按固定映射表的派生结果，不把跨来源事实伪装成单一 PoB 原文。"
        ),
        "channel": "derived",
    }


def _pool_citation(report: PoolOddsReport) -> dict[str, Any]:
    assert report.base is not None
    assert report.pool_digest is not None
    assert report.page_snapshot is not None
    base = report.base
    proof = report.page_snapshot
    scope_label = "normal 词缀行集" if proof.scope == "normal" else "整页行集"
    stored_label = "normal 词缀" if proof.scope == "normal" else "整页"
    side_zh = _SIDE_ZH[report.generation_side]
    rows = "\n".join(
        f"| {row.tier if row.tier is not None else '-'} | {row.required_item_level} | "
        f"{row.weight} | {row.text_zh} |"
        for row in report.target_rows
    )
    text = (
        "### 普通词缀同侧加权池（确定性求和）\n\n"
        f"- 底材：{base.name_zh}（{base.name_en}）\n"
        f"- POEDB 词缀页：{base.modifier_page}\n"
        f"- 物品等级：{report.item_level}\n"
        f"- 生成侧：{side_zh}\n"
        "- 来源类别：normal\n"
        "- 已有 family 排除：未应用\n"
        f"- 有效档位数：{report.pool_size}\n"
        f"- 有效池总权重：{report.total_weight}\n"
        f"- 目标查询：{report.target_query}\n"
        f"- 唯一目标组：{report.target_group}\n"
        f"- 目标组权重：{report.target_weight}\n"
        f"- 约分后条件概率：{report.probability_numerator}/{report.probability_denominator}\n"
        f"- 条件概率小数：{report.probability_decimal}\n"
        f"- 独立同分布选择的数学期望次数：{report.expected_independent_selections}\n"
        f"- 条件池摘要：{report.pool_digest}\n"
        f"- {scope_label}摘要：{proof.row_digest}\n"
        f"- 来源 HTML 摘要：{proof.source_content_hash}\n"
        f"- 来源抓取时间：{proof.source_fetched_at}\n"
        f"- 来源原始行（全页）/已验证 {stored_label}存储行："
        f"{proof.source_total_rows}/{proof.row_count}\n\n"
        "| T 级 | 最低物品等级 | 权重 | 目标组词缀原文 |\n"
        "|---:|---:|---:|---|\n"
        f"{rows}\n\n"
        "> 边界：这是下一次同侧普通加权选择的条件分布，不是任何通货或工艺的"
        "一次使用成功率，也不是实际期望通货消耗。"
    )
    claim_data = {
        "kind": "pool_odds",
        "base_id": base.base_id,
        "base_name_en": base.name_en,
        "base_name_zh": base.name_zh,
        "armour_tag": base.armour_tag,
        "modifier_page": base.modifier_page,
        "poedb_base_url": base.poedb_url,
        "pob_url": base.pob_url,
        "item_level": report.item_level,
        "generation_side": report.generation_side,
        "generation_side_zh": side_zh,
        "source_category": "normal",
        "existing_family_exclusions_applied": False,
        "pool_size": report.pool_size,
        "total_weight": report.total_weight,
        "target_query": report.target_query,
        "target_group": report.target_group,
        "target_weight": report.target_weight,
        "probability_numerator": report.probability_numerator,
        "probability_denominator": report.probability_denominator,
        "probability_decimal": report.probability_decimal,
        "expected_independent_selections": report.expected_independent_selections,
        "pool_digest": report.pool_digest,
        "page_row_count": proof.row_count,
        "page_row_digest": proof.row_digest,
        "snapshot_scope": proof.scope,
        "source_content_hash": proof.source_content_hash,
        "source_fetched_at": proof.source_fetched_at,
        "source_total_rows": proof.source_total_rows,
        "source_deduped_rows": proof.source_deduped_rows,
    }
    return {
        "chunk_id": f"affix_pool:{report.pool_digest}:{report.target_group}",
        "entity_id": None,
        "title": (
            f"{base.name_zh} · 物等 {report.item_level} · {side_zh} · "
            f"{report.target_group} 条件权重"
        ),
        "url": proof.source_url,
        "source": Source.POEDB.value,
        "source_priority": 1,
        "can_support_facts": True,
        "claim_scope": "affix_pool_odds",
        "claim_data": claim_data,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "derived",
    }


def affix_pool_citations(report: PoolOddsReport) -> list[dict[str, Any]]:
    """返回三项相互核对的底材、跨源映射与完整池证据。"""
    if report.status != "exact":
        return []
    return [
        _pob_base_citation(report),
        _base_mapping_citation(report),
        _pool_citation(report),
    ]


@REGISTRY.register(
    name="affix_pool_odds",
    description=(
        "给定逐字胸甲底材、物品等级、prefix/suffix 与逐字目标词缀，枚举 POEDB "
        "normal 同侧完整正权重池，目标只能唯一落到一个 correct_group 时返回整数"
        "权重分子/分母、条件选择概率和独立选择数学期望。只表示下一次同侧普通加权"
        "选择，不是任何通货/工艺成功率；不处理已有词缀、容量、影响力、特殊来源、"
        "步骤、成本或路线。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "base_type": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_BASE_CHARS,
                "description": "pob_base_item 中逐字中文或英文胸甲底材名。",
            },
            "item_level": {
                "type": "integer",
                "minimum": MIN_ITEM_LEVEL,
                "maximum": MAX_ITEM_LEVEL,
                "description": "用户明确给出的物品等级。",
            },
            "generation_side": {
                "type": "string",
                "enum": ["prefix", "suffix"],
                "description": "用户明确指定的生成侧。",
            },
            "target_query": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_TARGET_CHARS,
                "description": "用户逐字给出的目标词缀文本或 correct_group。",
            },
        },
        "required": ["base_type", "item_level", "generation_side", "target_query"],
        "additionalProperties": False,
    },
)
def affix_pool_odds(
    base_type: str,
    item_level: int,
    generation_side: str,
    target_query: str,
) -> ToolResult:
    try:
        report = analyze_affix_pool(
            dbmod.get_conn(),
            base_type=base_type,
            item_level=item_level,
            generation_side=generation_side,  # type: ignore[arg-type]
            target_query=target_query,
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))

    data = report.to_dict()
    data["boundary"] = (
        "仅表示下一次同侧普通加权选择的条件分布；不是任何通货或工艺成功率，"
        "不提供已有装备状态、容量、特殊来源、步骤、实际期望通货、成本或路线"
    )
    if report.status != "exact":
        return ToolResult(
            ok=True,
            data=data,
            citations=[],
            meta={"channel": "derived", "table": "pob_base_item+mod_affix"},
        )
    return ToolResult(
        ok=True,
        data=data,
        citations=affix_pool_citations(report),
        meta={"channel": "derived", "table": "pob_base_item+mod_affix"},
    )
