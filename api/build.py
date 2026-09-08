"""`/build`：PoB 分享码与官方天赋树链接解析（需求 8.1 的入口）。

路由层只做校验与编排，业务在 `parsers/pob_build.py`：
    分享码 → `decode_share_code` → `parse_build` → `bridge_gems`

**这条链路全程不调用 LLM**（与 `/item` 同一条先例）：输出是代码装配的，
同一份分享码永远得到同一份结果。也正因如此它不过出口绑定闸门——
闸门防的是模型编造，这里没有模型。

需求 8.4 的 16 项体检、8.5 的防御补充、8.6 的三类输出分析与 8.7 的
双档优化建议已经接入，
但遵守「**只判不算**」边界：值取自分享码里的 `PlayerStat`，本库只用有出处的
常量与规则判读；缺失项标为「未知」，不会估计。因此返回里
`analysis_done=true` 表示这些项目均已检查，**不表示本库重算了 Build**。输出分析中，
单体只对应当前 Calcs 技能，清图只列覆盖指标，Boss 必须有显式敌人档位。
防御来源只拆装备文本中能逐字核对的部分；优化建议只由已知且低于明确上限的指标触发。
需求 8.9 的版本核对读取当前激活天赋方案的 `treeVersion`，与本地数据表版本比较。

`player_stats` 那一段**是 PoB 自己算的快照**，不是本库算的，也不是知识库证据——
它反映用户自己那份 PoB 的版本与配置。字段名与 `source_note` 都写清这一点，
免得下游把它与本库计算器的结论混在一起。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from core import db as dbmod
from core.config import get_config
from kb.fetcher import Fetcher, Poe2SourceRejected
from parsers.build_advice import build_suggestions
from parsers.build_report import build_health_report
from parsers.build_sources import build_defence_sources
from parsers.build_version import check_build_version
from parsers.pob_build import PobParseError, bridge_gems, decode_share_code, parse_build
from parsers.pob_link import PobLinkError, looks_like_link, resolve_download_url
from parsers.passive_tree_link import (
    PassiveTreeLinkError,
    looks_like_passive_tree_link,
    passive_tree_build,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Build"])


class BuildRequest(BaseModel):
    code: str = Field(
        min_length=1, max_length=200000,
        description=(
            "PoB 的分享码、**已知分享站**的链接，或 pathofexile.com 官方天赋树链接"
            "（pobb.in / pastebin.com / pob.codes / poe.ninja / Maxroll / rentry.co …）。"
            "只认白名单里的站点：服务端会拿这个地址去发请求。"
        ),
    )


class GemOut(BaseModel):
    name_en: str
    name_zh: str = ""
    level: int | None = None
    quality: int | None = None
    enabled: bool = True
    #: 未桥接时为 None——**保留英文原文，不猜**
    gem_id: str | None = None


class SkillGroupOut(BaseModel):
    slot: str = ""
    enabled: bool = True
    is_main: bool = False
    gems: list[GemOut] = []


class ItemOut(BaseModel):
    slot: str = ""
    name: str = ""
    base_type: str = ""
    rarity: str = ""
    item_level: int | None = None
    implicits: list[str] = []
    explicits: list[str] = []
    error: str = ""


class CheckOut(BaseModel):
    key: str
    label: str
    value: str = ""
    known: bool = True
    missing_reason: str = ""
    verdict: str = ""
    basis: str = ""
    source: str = ""


class DefenceSourceOut(BaseModel):
    category: str
    label: str
    slot: str = ""
    item_name: str = ""
    origin: str
    detail: str


class BuildSuggestionOut(BaseModel):
    weakness: str
    tier: str
    action: str
    expected_gain: str
    estimated_cost: str
    assumptions: str


class BuildVersionOut(BaseModel):
    build_version: str = ""
    system_version: str = ""
    known: bool = False
    matches: bool | None = None
    message: str = ""
    basis: str = ""


class BuildResponse(BaseModel):
    input_type: str = "pob"
    level: int | None = None
    class_name: str = ""
    ascend_class_name: str = ""
    bandit: str = ""
    pantheon_major: str = ""
    pantheon_minor: str = ""
    skill_groups: list[SkillGroupOut] = []
    items: list[ItemOut] = []
    config: dict[str, str] = {}
    #: **PoB 自己算的快照**，见 `player_stats_note`
    player_stats: dict[str, float] = {}
    player_stats_note: str = ""
    #: 需求 8.4 的逐项体检。**只判不算**：值取自 build，判据取自本库常量
    report: list[CheckOut] = []
    #: 需求 8.6：单体 / 清图 / Boss 分开，且显式绑定当前 PoB 配置
    output_report: list[CheckOut] = []
    #: 装备文本中可逐字核对的防御来源；不重算、不冒充完整 PoB breakdown
    defence_sources: list[DefenceSourceOut] = []
    defence_sources_unresolved: list[str] = []
    defence_sources_note: str = ""
    suggestions: list[BuildSuggestionOut] = []
    suggestions_note: str = ""
    version_check: BuildVersionOut
    tree_serialization_version: int | None = None
    ascendancy_id: int | None = None
    secondary_ascendancy_id: int | None = None
    mastery_effects: dict[str, str] = {}
    report_notes: list[str] = []
    analysis_done: bool = True
    analysis_note: str = ""
    warnings: list[str] = []


_STATS_NOTE = (
    "以上数值取自你粘贴的 build 自带的 <PlayerStat>，是 **PoB 自己算的**，"
    "既不是本库的计算结果、也不是知识库证据——它反映你那份 PoB 的版本与配置。"
    "本库的计算器结论不与它混排。"
)
_ANALYSIS_NOTE = (
    "需求 8.4 的 16 项、8.5 的防御补充与 8.6 的输出场景已逐项核对，"
    "但**本库没有重算任何一项**："
    "数值取自你的 build，本库只做判读；缺的一律标「未知」而不填估计值。"
    "**DPS 只是复述你 PoB 的数**——本库缺整条加成链，算不了 DPS。"
)


def _check_out(check) -> CheckOut:  # noqa: ANN001
    return CheckOut(
        key=check.key, label=check.label, value=check.value, known=check.known,
        missing_reason=check.missing_reason, verdict=check.verdict,
        basis=check.basis, source=check.source,
    )


def _resolve_code(raw: str) -> str:
    """输入既可能是分享码、也可能是链接。是链接就先抓回原文。

    抓取复用 `kb.fetcher.Fetcher`（限速、robots、缓存、UA 一并继承），
    它内部还会再过一次 `reject_poe2_host`——白名单之外的第二道。
    """
    if not looks_like_link(raw):
        return raw
    url = resolve_download_url(raw)
    cfg = get_config()
    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )
    logger.info("build 链接导入 %s → %s", raw, url)
    return fetcher.fetch(url).text.strip()


@router.post("/build", response_model=BuildResponse)
def analyze_build(req: BuildRequest, caller: str = Depends(require_user)) -> BuildResponse:
    input_type = "pob"
    tree_link = None
    try:
        if looks_like_passive_tree_link(req.code):
            input_type = "passive_tree"
            build, tree_link = passive_tree_build(req.code)
        else:
            code = _resolve_code(req.code)
            xml = decode_share_code(code)
            build = bridge_gems(dbmod.get_conn(), parse_build(xml))
    except PassiveTreeLinkError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "passive_tree_parse_failed", "reason": str(exc)},
        ) from exc
    except (PobLinkError, Poe2SourceRejected) as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "pob_link_rejected", "reason": str(exc)},
        ) from exc
    except OSError as exc:
        # 抓取失败是**外部**问题，与「链接不合法」分开报，用户才知道该重试还是该换链接
        raise HTTPException(
            status_code=502,
            detail={"error": "pob_fetch_failed", "reason": f"抓取分享链接失败：{exc}"},
        ) from exc
    except PobParseError as exc:
        # 422 而不是 400：格式对不上属于语义无法处理，且要把可读原因带回去
        raise HTTPException(
            status_code=422,
            detail={"error": "pob_parse_failed", "reason": str(exc)},
        ) from exc

    report = build_health_report(dbmod.get_conn(), build)
    sources = build_defence_sources(build)
    suggestions = build_suggestions(build)
    version = check_build_version(dbmod.get_conn(), build)
    bridged = sum(1 for g in build.skill_groups for x in g.gems if x.gem_id)
    total_gems = sum(len(g.gems) for g in build.skill_groups)
    logger.info(
        "build 解析 caller=%s 等级=%s 职业=%s/%s 技能组=%d 宝石=%d/%d 装备=%d",
        caller, build.level, build.class_name, build.ascend_class_name,
        len(build.skill_groups), bridged, total_gems, len(build.items),
    )

    return BuildResponse(
        input_type=input_type,
        level=build.level,
        class_name=build.class_name,
        ascend_class_name=build.ascend_class_name,
        bandit=build.bandit,
        pantheon_major=build.pantheon_major,
        pantheon_minor=build.pantheon_minor,
        skill_groups=[
            SkillGroupOut(
                slot=g.slot, enabled=g.enabled, is_main=g.is_main,
                gems=[
                    GemOut(
                        name_en=x.name_en, name_zh=x.name_zh, level=x.level,
                        quality=x.quality, enabled=x.enabled, gem_id=x.gem_id,
                    )
                    for x in g.gems
                ],
            )
            for g in build.skill_groups
        ],
        items=[
            ItemOut(
                slot=i.slot,
                name=i.parsed.name if i.parsed else "",
                base_type=i.parsed.base_type if i.parsed else "",
                rarity=i.parsed.rarity.value if i.parsed else "",
                item_level=i.parsed.item_level if i.parsed else None,
                implicits=[m.text for m in i.parsed.implicits] if i.parsed else [],
                explicits=[m.text for m in i.parsed.explicits] if i.parsed else [],
                error=i.error,
            )
            for i in build.items
        ],
        config={k: str(v) for k, v in build.config.items()},
        player_stats=build.player_stats,
        player_stats_note=_STATS_NOTE if build.player_stats else "",
        report=[_check_out(c) for c in report.items],
        output_report=[_check_out(c) for c in report.output_items],
        defence_sources=[DefenceSourceOut(**line.__dict__) for line in sources.lines],
        defence_sources_unresolved=sources.unresolved,
        defence_sources_note=sources.note,
        suggestions=[BuildSuggestionOut(**item.__dict__) for item in suggestions],
        suggestions_note=(
            "建议只由已知 PlayerStat 短板触发；预期收益是当前值到目标值的可复核差额。"
            "项目尚未接入赛季经济数据，因此成本按改造规模分档，不提供虚构的通货报价。"
        ),
        version_check=BuildVersionOut(**version.__dict__),
        tree_serialization_version=(tree_link.serialization_version if tree_link else None),
        ascendancy_id=(tree_link.ascendancy_id if tree_link else None),
        secondary_ascendancy_id=(tree_link.secondary_ascendancy_id if tree_link else None),
        mastery_effects=dict(tree_link.mastery_effects) if tree_link else {},
        report_notes=report.notes,
        analysis_done=True,
        analysis_note=_ANALYSIS_NOTE,
        warnings=build.warnings,
    )
