"""`/build`：PoB 分享码解析（第四十八轮，需求 8.4 的入口）。

路由层只做校验与编排，业务在 `parsers/pob_build.py`：
    分享码 → `decode_share_code` → `parse_build` → `bridge_gems`

**这条链路全程不调用 LLM**（与 `/item` 同一条先例）：输出是代码装配的，
同一份分享码永远得到同一份结果。也正因如此它不过出口绑定闸门——
闸门防的是模型编造，这里没有模型。

**本版只解析，不做分析。** 需求 8.4 那 16 项（DPS / Effective HP / Max Hit /
抗性 / 法术压制 / 护甲 / 闪避 / 格挡 / 药剂 / 保留 / 诅咒 / 万神殿 / 珠宝 /
天赋树 / 升华）是下一轮的事，返回里 `analysis_done=false` 明说这件事。

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
from parsers.build_report import build_health_report
from parsers.pob_build import PobParseError, bridge_gems, decode_share_code, parse_build

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Build"])


class BuildRequest(BaseModel):
    code: str = Field(
        min_length=1, max_length=200000,
        description="PoB 的分享码（Import/Export → Generate 出来的那串字符）。暂不支持链接。",
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


class BuildResponse(BaseModel):
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
    "需求 8.4 的 16 项已逐项核对（见 `report`），但**本库没有重算任何一项**："
    "数值取自你的 build，本库只做判读；缺的一律标「未知」而不填估计值。"
    "**DPS 只是复述你 PoB 的数**——本库缺整条加成链，算不了 DPS。"
)


@router.post("/build", response_model=BuildResponse)
def analyze_build(req: BuildRequest, caller: str = Depends(require_user)) -> BuildResponse:
    try:
        xml = decode_share_code(req.code)
        build = bridge_gems(dbmod.get_conn(), parse_build(xml))
    except PobParseError as exc:
        # 422 而不是 400：格式对不上属于语义无法处理，且要把可读原因带回去
        raise HTTPException(
            status_code=422,
            detail={"error": "pob_parse_failed", "reason": str(exc)},
        ) from exc

    report = build_health_report(dbmod.get_conn(), build)
    bridged = sum(1 for g in build.skill_groups for x in g.gems if x.gem_id)
    total_gems = sum(len(g.gems) for g in build.skill_groups)
    logger.info(
        "build 解析 caller=%s 等级=%s 职业=%s/%s 技能组=%d 宝石=%d/%d 装备=%d",
        caller, build.level, build.class_name, build.ascend_class_name,
        len(build.skill_groups), bridged, total_gems, len(build.items),
    )

    return BuildResponse(
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
        report=[
            CheckOut(
                key=c.key, label=c.label, value=c.value, known=c.known,
                missing_reason=c.missing_reason, verdict=c.verdict,
                basis=c.basis, source=c.source,
            )
            for c in report.items
        ],
        report_notes=report.notes,
        analysis_done=True,
        analysis_note=_ANALYSIS_NOTE,
        warnings=build.warnings,
    )
