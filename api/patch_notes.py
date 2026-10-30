"""已发布 GGG 官方 PoE1 补丁公告的只读查询。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.deps import require_user
from core import db as dbmod
from kb import patch_notes
from parsers.pob_build import PobParseError, decode_share_code, parse_build
from parsers.item_text import Rarity

router = APIRouter(tags=["版本公告"], dependencies=[Depends(require_user)])


class PatchNoteSummary(BaseModel):
    thread_id: int
    version: str
    kind: str
    title: str
    published_on: str
    source_url: str
    fetched_at: str
    body_length: int


class PatchNoteList(BaseModel):
    items: list[PatchNoteSummary]
    total_returned: int
    boundary: str = "只列已核验的 GGG 官方 PoE1 公告；最新入库公告不自动等于当前游戏版本"


class PatchNoteDetail(BaseModel):
    thread_id: int
    version: str
    kind: str
    title: str
    published_on: str
    body: str
    source_url: str
    content_hash: str
    fetched_at: str
    updated_at: str
    boundary: str = "保留 GGG 英文原文和论坛日期；论坛未给时区，不推断准确发布时间或 Build 影响"


class PatchMention(BaseModel):
    thread_id: int
    version: str
    title: str
    published_on: str
    source_url: str
    line_number: int
    term: str
    source_excerpt: str
    excerpt_truncated: bool


class PatchTimeline(BaseModel):
    query: str
    items: list[PatchMention]
    status: str
    boundary: str = (
        "逐字匹配已入库的英文官方公告；命中只说明原文提及该词，"
        "不自动判定机制生效时间、增减方向或当前游戏版本"
    )


class PatchImpactRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200000, description="PoB 分享码本身")
    version: str = Field(default="", max_length=40, description="可选的精确公告版本")
    limit: int = Field(default=50, ge=1, le=200)


class PatchImpact(PatchTimeline):
    build_version: str = ""
    checked_components: list[str] = []
    unknown_components: list[str] = []
    boundary: str = (
        "仅报告 PoB 中英文技能、升华与传奇装备名称在官方公告的字面提及，供人工核对；"
        "不声称这些改动已作用于该 Build，也不重算伤害或判断收益"
    )


def _build_terms(code: str) -> tuple[str, list[str], list[str]]:
    try:
        xml = decode_share_code(code)
        if len(xml) > 5_000_000:
            raise PobParseError("PoB XML 超过分析上限")
        build = parse_build(xml)
    except PobParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    terms: list[str] = []
    unknown: list[str] = []
    for group in build.skill_groups:
        if group.enabled:
            terms.extend(gem.name_en.strip() for gem in group.gems if gem.enabled and gem.name_en.strip())
    if build.ascend_class_name:
        terms.append(build.ascend_class_name.strip())
    for item in build.items:
        if item.parsed and item.parsed.rarity == Rarity.UNIQUE and item.parsed.name:
            terms.append(item.parsed.name.strip())
        elif item.error:
            unknown.append(f"装备 {item.slot or '未知部位'} 解析失败")
    terms = list(dict.fromkeys(term for term in terms if 2 <= len(term) <= 120))
    if len(terms) > 100:
        unknown.append("可核对部件超过 100 个，只检查前 100 个")
    if not terms:
        unknown.append("PoB 中没有可逐字匹配的英文技能、升华或传奇装备名")
    return build.tree_version, terms[:100], unknown


@router.get("/patch-notes", response_model=PatchNoteList)
def list_patch_notes(
    version: str = Query(default="", max_length=40),
    query: str = Query(default="", max_length=200),
    limit: int = Query(default=20, ge=1, le=100),
) -> PatchNoteList:
    items = [PatchNoteSummary(**row) for row in patch_notes.search(
        dbmod.get_conn(), version=version, query=query, limit=limit,
    )]
    return PatchNoteList(items=items, total_returned=len(items))


@router.get("/patch-notes/timeline", response_model=PatchTimeline)
def patch_timeline(
    query: str = Query(min_length=2, max_length=120),
    version: str = Query(default="", max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
) -> PatchTimeline:
    term = query.strip()
    if len(term) < 2:
        raise HTTPException(status_code=422, detail="查询词至少两个字符")
    items = [PatchMention(**row) for row in patch_notes.mentions(
        dbmod.get_conn(), [term], version=version, limit=limit,
    )]
    return PatchTimeline(query=term, items=items, status="mentions" if items else "unknown")


@router.post("/patch-notes/build-impact", response_model=PatchImpact)
def patch_build_impact(request: PatchImpactRequest) -> PatchImpact:
    build_version, terms, unknown = _build_terms(request.code)
    items = [PatchMention(**row) for row in patch_notes.mentions(
        dbmod.get_conn(), terms, version=request.version, limit=request.limit,
    )] if terms else []
    return PatchImpact(
        query="PoB 英文部件字面匹配", items=items,
        status="mentions" if items else "unknown", build_version=build_version,
        checked_components=terms, unknown_components=unknown,
    )


@router.get("/patch-notes/{thread_id}", response_model=PatchNoteDetail)
def get_patch_note(thread_id: int) -> PatchNoteDetail:
    note = patch_notes.detail(dbmod.get_conn(), thread_id)
    if note is None:
        raise HTTPException(status_code=404, detail="公告未入库")
    return PatchNoteDetail(**note)
