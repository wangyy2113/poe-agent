"""制作操作状态机 API；路由只做输入校验并调用同一结构化工具实现。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from api.deps import require_user
from tools.impl.craft_operation import craft_operation_state
from tools.impl.essence_operation import essence_operation_state
from tools.impl.harvest_operation import harvest_reforge_state

router = APIRouter(tags=["制作"])


class CraftOperationRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=80)
    item_text: str | None = Field(default=None, max_length=20_000)
    rarity: Literal["普通", "魔法", "稀有", "传奇"] | None = None
    prefix_count: int | None = Field(default=None, ge=0, le=6)
    suffix_count: int | None = Field(default=None, ge=0, le=6)
    item_kind: Literal["default", "jewel", "flask", "graft", "jewel_abyss_corrupted"] = "default"
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False

    @model_validator(mode="after")
    def no_mixed_state(self) -> "CraftOperationRequest":
        if self.item_text and any(
            value is not None for value in (self.rarity, self.prefix_count, self.suffix_count)
        ):
            raise ValueError("item_text 与手工状态字段不能混用")
        return self


class CraftOperationResponse(BaseModel):
    found: bool
    verdict: str
    operation: str
    operation_en: str
    metadata_id: str
    effect_text: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    before: dict[str, Any]
    after: dict[str, Any]
    checks: list[dict[str, Any]]
    unknown_fields: list[str]
    boundary: str
    answer: str


class EssenceOperationRequest(BaseModel):
    essence: str = Field(min_length=1, max_length=80)
    item_text: str | None = Field(default=None, max_length=20_000)
    rarity: Literal["普通", "魔法", "稀有", "传奇"] | None = None
    item_level: int | None = Field(default=None, ge=1, le=100)
    item_class: str | None = Field(default=None, min_length=1, max_length=80)
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False

    @model_validator(mode="after")
    def no_mixed_state(self) -> "EssenceOperationRequest":
        if self.item_text and any(
            value is not None for value in (self.rarity, self.item_level, self.item_class)
        ):
            raise ValueError("item_text 与手工状态字段不能混用")
        return self


class EssenceOperationResponse(BaseModel):
    found: bool
    verdict: str
    essence: str
    essence_en: str
    metadata_id: str
    tier: int
    base_effect: str
    level_cap: int | None
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    before: dict[str, Any]
    after: dict[str, Any]
    guaranteed_modifier: dict[str, str] | None
    checks: list[dict[str, Any]]
    unknown_fields: list[str]
    boundary: str
    answer: str


class HarvestReforgeRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=160)
    item_text: str | None = Field(default=None, max_length=20_000)
    rarity: Literal["普通", "魔法", "稀有", "传奇"] | None = None
    influenced: bool | None = None
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False

    @model_validator(mode="after")
    def no_mixed_state(self) -> "HarvestReforgeRequest":
        if self.item_text and any(value is not None for value in (self.rarity, self.influenced)):
            raise ValueError("item_text 与手工状态字段不能混用")
        return self


class HarvestReforgeResponse(BaseModel):
    found: bool
    verdict: str
    craft_id: str
    effect_text: str
    mode: str
    guaranteed_tag: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    costs: list[dict[str, Any]]
    before: dict[str, Any]
    after: dict[str, Any]
    checks: list[dict[str, Any]]
    unknown_fields: list[str]
    boundary: str
    answer: str


@router.post("/craft/operation", response_model=CraftOperationResponse)
def operation_state(
    req: CraftOperationRequest, _caller: str = Depends(require_user)
) -> CraftOperationResponse:
    result = craft_operation_state(
        operation=req.operation,
        item_text=req.item_text,
        rarity=req.rarity,
        prefix_count=req.prefix_count,
        suffix_count=req.suffix_count,
        item_kind=req.item_kind,
        corrupted=req.corrupted,
        mirrored=req.mirrored,
        fractured=req.fractured,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_craft_operation", "reason": result.error,
        })
    if not (result.data or {}).get("found"):
        raise HTTPException(status_code=422, detail={
            "error": "unsupported_craft_operation",
            "reason": "只支持已审计的 10 种基础通货，不按相似名称猜操作语义",
            "supported_operations": (result.data or {}).get("supported_operations", []),
        })
    return CraftOperationResponse(**result.data)


@router.post("/craft/essence", response_model=EssenceOperationResponse)
def essence_state(
    req: EssenceOperationRequest, _caller: str = Depends(require_user)
) -> EssenceOperationResponse:
    result = essence_operation_state(
        essence=req.essence,
        item_text=req.item_text,
        rarity=req.rarity,
        item_level=req.item_level,
        item_class=req.item_class or "",
        corrupted=req.corrupted,
        mirrored=req.mirrored,
        fractured=req.fractured,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_essence_operation", "reason": result.error,
        })
    if not (result.data or {}).get("found"):
        raise HTTPException(status_code=422, detail={
            "error": "unsupported_essence",
            "reason": "只接受本库 105 件精华的唯一精确名称，不按相似名称猜操作语义",
        })
    return EssenceOperationResponse(**result.data)


@router.post("/craft/harvest/reforge", response_model=HarvestReforgeResponse)
def harvest_state(
    req: HarvestReforgeRequest, _caller: str = Depends(require_user)
) -> HarvestReforgeResponse:
    result = harvest_reforge_state(
        operation=req.operation,
        item_text=req.item_text,
        rarity=req.rarity,
        influenced=req.influenced,
        corrupted=req.corrupted,
        mirrored=req.mirrored,
        fractured=req.fractured,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_harvest_reforge", "reason": result.error,
        })
    if not (result.data or {}).get("found"):
        raise HTTPException(status_code=422, detail={
            "error": "unsupported_harvest_operation",
            "reason": "只支持 20 条已审计 Harvest 重铸，不按相似词猜转换、牺牲或附魔语义",
            "supported_operations": (result.data or {}).get("supported_operations", []),
        })
    return HarvestReforgeResponse(**result.data)
