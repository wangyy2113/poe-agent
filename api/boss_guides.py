"""已核验 PoE Wiki 终局 Boss 页面原文查询。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from core import db as dbmod
from kb import boss_guides
from parsers.pob_build import PobParseError, decode_share_code, parse_build
from planner.boss_prep import compare_boss_guide

router = APIRouter(tags=["Boss 攻略"], dependencies=[Depends(require_user)])


class GuideSummary(BaseModel):
    boss_key: str
    page_title: str
    source_url: str
    fetched_at: str
    content_hash: str


class GuideSectionOut(BaseModel):
    heading: str
    lines: list[str]


class GuideDetail(GuideSummary):
    sections: list[GuideSectionOut]
    updated_at: str
    source: str = "PoE Wiki (P3)"
    boundary: str = (
        "逐字复述抓取时的 Wiki 攻略章节；这是可变的社区维护页面，"
        "不是 GGG 官方技能数值。药剂、门槛与当前版本适用性须分别核验。"
    )


class BossGuideBuildCheckRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200000, description="PoB 分享码本身")
    same_boss_configuration: bool = Field(
        default=False,
        description="用户确认 PoB 敌人、技能、增益配置适用于所问 Boss 后才比较 DPS 门槛",
    )


@router.get("/boss/guides", response_model=list[GuideSummary])
def list_boss_guides() -> list[GuideSummary]:
    return [GuideSummary(**row) for row in boss_guides.list_guides(dbmod.get_conn())]


@router.get("/boss/guides/{boss_key}", response_model=GuideDetail)
def get_boss_guide(boss_key: str) -> GuideDetail:
    if boss_key not in boss_guides.PAGES:
        raise HTTPException(status_code=404, detail="不支持的 Boss 攻略页面")
    row = boss_guides.detail(dbmod.get_conn(), boss_key)
    if row is None:
        raise HTTPException(status_code=404, detail="Boss 攻略尚未入库")
    return GuideDetail(**row)


@router.post("/boss/guides/{boss_key}/build-check")
def check_boss_guide_build(boss_key: str, request: BossGuideBuildCheckRequest) -> dict:
    if boss_key not in boss_guides.PAGES:
        raise HTTPException(status_code=404, detail="不支持的 Boss 攻略页面")
    guide = boss_guides.detail(dbmod.get_conn(), boss_key)
    if guide is None:
        raise HTTPException(status_code=404, detail="Boss 攻略尚未入库")
    try:
        xml = decode_share_code(request.code)
        if len(xml) > 5_000_000:
            raise PobParseError("PoB XML 超过分析上限")
        build = parse_build(xml)
    except PobParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return compare_boss_guide(
        guide, build, same_boss_configuration=request.same_boss_configuration,
    )
