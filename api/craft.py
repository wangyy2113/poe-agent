"""制作操作状态机 API；路由只做输入校验并调用同一结构化工具实现。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from api.deps import require_user
from tools.impl.craft_operation import craft_operation_state
from tools.impl.essence_operation import essence_operation_state
from tools.impl.harvest_enchant_operation import harvest_quality_enchant_state
from tools.impl.harvest_exchange_operation import harvest_exchange_state
from tools.impl.harvest_modifier_operation import harvest_modifier_state
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


class HarvestModifierRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=180)
    item_text: str | None = Field(default=None, max_length=20_000)
    source_modifier_present: bool | None = None
    influenced: bool | None = None
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False

    @model_validator(mode="after")
    def no_mixed_influence_state(self) -> "HarvestModifierRequest":
        if self.item_text and self.influenced is not None:
            raise ValueError("item_text 与手工 influenced 不能混用")
        return self


class HarvestModifierResponse(BaseModel):
    found: bool
    verdict: str
    craft_id: str
    effect_text: str
    mode: str
    source_modifier: str
    target_modifier: str
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


class HarvestQualityEnchantRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=180)
    item_text: str | None = Field(default=None, max_length=20_000)
    item_kind: Literal["body_armour", "weapon", "melee_weapon", "other"] | None = None
    quality: int | None = Field(default=None, ge=0, le=100)
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False

    @model_validator(mode="after")
    def no_mixed_item_state(self) -> "HarvestQualityEnchantRequest":
        if self.item_text and (self.item_kind is not None or self.quality is not None):
            raise ValueError("item_text 与手工 item_kind/quality 不能混用")
        return self


class HarvestQualityEnchantResponse(BaseModel):
    found: bool
    verdict: str
    craft_id: str
    effect_text: str
    enchant_key: str
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


class HarvestExchangeRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=260)
    quantity: int | None = Field(default=None, ge=1)
    max_stack_size: int | None = Field(default=None, ge=1)
    rarity: Literal["普通", "魔法", "稀有", "传奇"] | None = None
    corrupted: bool | None = None
    map_tier: int | None = Field(default=None, ge=1, le=100)
    gem_total_experience: int | None = Field(default=None, ge=0)


class HarvestExchangeResponse(BaseModel):
    found: bool
    verdict: str
    craft_id: str
    effect_text: str
    operation_key: str
    mode: str
    input_label: str
    output_relation: str
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


@router.post("/craft/harvest/modifier", response_model=HarvestModifierResponse)
def harvest_modifier_operation_state(
    req: HarvestModifierRequest, _caller: str = Depends(require_user)
) -> HarvestModifierResponse:
    result = harvest_modifier_state(
        operation=req.operation,
        item_text=req.item_text,
        source_modifier_present=req.source_modifier_present,
        influenced=req.influenced,
        corrupted=req.corrupted,
        mirrored=req.mirrored,
        fractured=req.fractured,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_harvest_modifier_operation", "reason": result.error,
        })
    if not (result.data or {}).get("found"):
        raise HTTPException(status_code=422, detail={
            "error": "unsupported_harvest_modifier_operation",
            "reason": "只支持 23 条已审计的元素转换与新增/移除操作，不按相似描述猜语义",
            "supported_examples": (result.data or {}).get("supported_examples", []),
        })
    return HarvestModifierResponse(**result.data)


@router.post("/craft/harvest/quality-enchant", response_model=HarvestQualityEnchantResponse)
def harvest_quality_enchant_operation_state(
    req: HarvestQualityEnchantRequest, _caller: str = Depends(require_user)
) -> HarvestQualityEnchantResponse:
    result = harvest_quality_enchant_state(
        operation=req.operation,
        item_text=req.item_text,
        item_kind=req.item_kind,
        quality=req.quality,
        corrupted=req.corrupted,
        mirrored=req.mirrored,
        fractured=req.fractured,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_harvest_quality_enchant", "reason": result.error,
        })
    if not (result.data or {}).get("found"):
        raise HTTPException(status_code=422, detail={
            "error": "unsupported_harvest_quality_enchant",
            "reason": "只支持 14 条已审计的护甲/武器品质附魔，不按相似描述猜语义",
            "supported_examples": (result.data or {}).get("supported_examples", []),
        })
    return HarvestQualityEnchantResponse(**result.data)


@router.post("/craft/harvest/exchange", response_model=HarvestExchangeResponse)
def harvest_exchange_operation_state(
    req: HarvestExchangeRequest, _caller: str = Depends(require_user)
) -> HarvestExchangeResponse:
    result = harvest_exchange_state(
        operation=req.operation,
        quantity=req.quantity,
        max_stack_size=req.max_stack_size,
        rarity=req.rarity,
        corrupted=req.corrupted,
        map_tier=req.map_tier,
        gem_total_experience=req.gem_total_experience,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_harvest_exchange", "reason": result.error,
        })
    if not (result.data or {}).get("found"):
        raise HTTPException(status_code=422, detail={
            "error": "unsupported_harvest_exchange",
            "reason": "只支持 17 条已审计的 Harvest 材料转换与牺牲，不按相似描述猜语义",
            "supported_operations": (result.data or {}).get("supported_operations", []),
        })
    return HarvestExchangeResponse(**result.data)
