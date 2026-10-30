"""有来源的 Atlas 目标候选与拓扑路径。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from api.build import BuildScenarioStructureDiffOut, GrowthMetric, _resolve_code
from api.deps import require_user
from core import db as dbmod
from kb.fetcher import Poe2SourceRejected
from parsers.pob_build import PobParseError, decode_share_code, parse_build
from parsers.pob_link import PobLinkError
from parsers.passive_tree_link import looks_like_passive_tree_link
from planner.atlas import plan_atlas, plan_atlas_route
from planner.league_starter import plan_league_starter

router = APIRouter(tags=["规划"], dependencies=[Depends(require_user)])


class AtlasPlanRequest(BaseModel):
    goal: Literal["刷图速度", "通货收益", "特定材料", "自定义"]
    focus: str = Field(default="", max_length=80)
    limit: int = Field(default=10, ge=1, le=30)


@router.post("/planner/atlas")
def atlas_plan(request: AtlasPlanRequest) -> dict:
    try:
        return asdict(plan_atlas(
            dbmod.get_conn(), goal=request.goal, focus=request.focus,
            limit=request.limit,
        ))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class AtlasRouteRequest(BaseModel):
    entity_ids: list[str] = Field(min_length=1, max_length=10)
    allocated_node_ids: list[str] = Field(default_factory=list, max_length=300)


@router.post("/planner/atlas-route")
def atlas_route(request: AtlasRouteRequest) -> dict:
    try:
        return plan_atlas_route(
            dbmod.get_conn(), entity_ids=request.entity_ids,
            allocated_node_ids=request.allocated_node_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class LeagueStageRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=200000)

    @model_validator(mode="after")
    def normalize(self) -> "LeagueStageRequest":
        self.label = self.label.strip()
        self.goal = self.goal.strip()
        self.code = self.code.strip()
        if not self.label or not self.goal or not self.code:
            raise ValueError("阶段 label、goal 与 code 不能为空")
        return self


class LeagueStarterRequest(BaseModel):
    current_code: str = Field(min_length=1, max_length=200000)
    stages: list[LeagueStageRequest] = Field(min_length=1, max_length=5)
    metrics: list[GrowthMetric] | None = Field(default=None, min_length=1, max_length=18)

    @model_validator(mode="after")
    def validate_route(self) -> "LeagueStarterRequest":
        self.current_code = self.current_code.strip()
        if not self.current_code:
            raise ValueError("current_code 不能为空")
        labels = [item.label for item in self.stages]
        if "当前" in labels or len(set(labels)) != len(labels):
            raise ValueError("阶段 label 不能重复或使用保留名称「当前」")
        if self.metrics is not None and len(set(self.metrics)) != len(self.metrics):
            raise ValueError("同一个比较指标不能重复")
        return self


class LeagueStageMetricOut(BaseModel):
    metric: GrowthMetric
    label: str
    unit: str
    status: Literal["ready", "unknown"]
    previous_value: float | None
    previous_field: str
    target_value: float | None
    target_field: str
    delta: float | None
    delta_percent: float | None
    reason: str


class LeagueStageEquipmentOut(BaseModel):
    slot: str
    status: Literal["present", "empty", "unknown"]
    name: str
    base_type: str
    note: str


class LeagueStageOut(BaseModel):
    sequence: int
    label: str
    goal: str
    previous_label: str
    target_level: int | None
    target_class: str
    target_ascendancy: str
    target_skill: str
    target_tree_version: str
    structure: BuildScenarioStructureDiffOut
    equipment_targets: list[LeagueStageEquipmentOut]
    context_status: Literal["ready", "mismatch", "unknown"]
    context_reasons: list[str]
    metrics: list[LeagueStageMetricOut]
    verification: str


class LeagueStarterResponse(BaseModel):
    status: Literal["ready", "partial", "unknown"]
    stage_count: int
    metric_transition_count: int
    numeric_ready: int
    numeric_unknown: int
    stages: list[LeagueStageOut]
    note: str


def _parse_stage_code(code: str, label: str):
    try:
        if looks_like_passive_tree_link(code):
            raise PobParseError("官方天赋树链接不含完整 Build 与 PlayerStat")
        return parse_build(decode_share_code(_resolve_code(code)))
    except (PobLinkError, Poe2SourceRejected) as exc:
        raise HTTPException(status_code=422, detail={
            "error": "league_starter_link_rejected", "stage": label, "reason": str(exc),
        }) from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail={
            "error": "league_starter_fetch_failed", "stage": label,
            "reason": f"抓取分享链接失败：{exc}",
        }) from exc
    except PobParseError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "league_starter_parse_failed", "stage": label, "reason": str(exc),
        }) from exc


@router.post("/planner/league-starter", response_model=LeagueStarterResponse)
def league_starter(request: LeagueStarterRequest) -> LeagueStarterResponse:
    current = _parse_stage_code(request.current_code, "当前")
    stages = [
        (stage.label, stage.goal, _parse_stage_code(stage.code, stage.label))
        for stage in request.stages
    ]
    try:
        plan = plan_league_starter(
            current, stages,
            metrics=list(request.metrics) if request.metrics is not None else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_league_starter_query", "reason": str(exc),
        }) from exc
    return LeagueStarterResponse.model_validate(plan, from_attributes=True)
