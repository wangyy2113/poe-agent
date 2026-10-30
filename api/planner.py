"""有来源的 Atlas 目标候选与拓扑路径。"""

from __future__ import annotations

from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from core import db as dbmod
from planner.atlas import plan_atlas, plan_atlas_route

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
