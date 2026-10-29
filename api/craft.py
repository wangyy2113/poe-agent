"""制作操作状态机 API；路由只做输入校验并调用同一结构化工具实现。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from api.deps import require_user
from core import db as dbmod
from craft.eldritch_operations import ELDRITCH_SPECS
from economy.service import PUBLIC_CATEGORIES
from tools.impl.awakener_operation import awakener_orb_state
from tools.impl.craft_attempt_plan import craft_attempt_plan as build_craft_attempt_plan
from tools.impl.craft_operation import craft_operation_state
from tools.impl.eldritch_operation import eldritch_operation_state
from tools.impl.essence_operation import essence_operation_state
from tools.impl.fossil_operation import fossil_operation_state
from tools.impl.harvest_enchant_operation import harvest_quality_enchant_state
from tools.impl.harvest_exchange_operation import harvest_exchange_state
from tools.impl.harvest_modifier_operation import harvest_modifier_state
from tools.impl.harvest_operation import harvest_reforge_state
from tools.impl.veiled_operation import veiled_operation_state

router = APIRouter(tags=["制作"])


class AttemptMaterialRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    quantity_per_attempt: float = Field(gt=0, le=1_000_000_000)
    category: str = Field(default="auto", max_length=80)

    @model_validator(mode="after")
    def known_category(self) -> "AttemptMaterialRequest":
        if self.category not in PUBLIC_CATEGORIES:
            raise ValueError(f"category 必须是：{', '.join(PUBLIC_CATEGORIES)}")
        return self


class CraftAttemptPlanRequest(BaseModel):
    base_type: str = Field(min_length=1, max_length=80)
    item_level: int = Field(ge=1, le=100)
    generation_side: Literal["prefix", "suffix"]
    target_query: str = Field(min_length=1, max_length=100)
    materials: list[AttemptMaterialRequest] = Field(min_length=1, max_length=20)
    max_attempts: int = Field(ge=1, le=1_000_000)
    include_market_prices: bool = False
    league: str | None = Field(default=None, max_length=100)


class CraftAttemptPlanResponse(BaseModel):
    status: Literal["exact", "unknown"]
    pool: dict[str, Any]
    max_attempts: int
    materials_per_attempt: list[dict[str, Any]]
    distribution: dict[str, Any] | None
    material_budget: list[dict[str, Any]]
    pricing: dict[str, Any]
    cost_budget: dict[str, Any] | None
    assumptions: list[str]
    exclusions: list[str]
    gaps: list[dict[str, str]]
    answer: str


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
    metamod_guard: Literal[
        "前缀无法被变更", "后缀无法被变更", "无法骰出攻击词缀", "无法骰出法术词缀",
    ] | None = None

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
    operation_verdict: str
    metamod_guard: str = ""
    metamod_interaction: dict[str, Any]
    boundary: str
    answer: str


class AwakenerOrbRequest(BaseModel):
    donor_item_text: str | None = Field(default=None, max_length=20_000)
    target_item_text: str | None = Field(default=None, max_length=20_000)
    donor_item_class: str = Field(default="", max_length=80)
    target_item_class: str = Field(default="", max_length=80)
    donor_rarity: Literal["普通", "魔法", "稀有", "传奇"] | None = None
    target_rarity: Literal["普通", "魔法", "稀有", "传奇"] | None = None
    donor_influences: list[str] = Field(default_factory=list, max_length=4)
    target_influences: list[str] = Field(default_factory=list, max_length=4)
    donor_corrupted: bool = False
    target_corrupted: bool = False
    donor_mirrored: bool = False
    target_mirrored: bool = False
    donor_fractured: bool = False
    target_fractured: bool = False

    @model_validator(mode="after")
    def no_mixed_state(self) -> "AwakenerOrbRequest":
        if self.donor_item_text and any((
            self.donor_item_class, self.donor_rarity, self.donor_influences,
            self.donor_corrupted, self.donor_mirrored, self.donor_fractured,
        )):
            raise ValueError("donor_item_text 与供体手工状态字段不能混用")
        if self.target_item_text and any((
            self.target_item_class, self.target_rarity, self.target_influences,
            self.target_corrupted, self.target_mirrored, self.target_fractured,
        )):
            raise ValueError("target_item_text 与目标手工状态字段不能混用")
        return self


class AwakenerOrbResponse(BaseModel):
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
    donor: dict[str, Any]
    target: dict[str, Any]
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


class EssenceCatalogEntry(BaseModel):
    name_zh: str
    name_en: str | None
    tier: int
    level_cap: int | None


class EssenceCatalogResponse(BaseModel):
    total: int
    items: list[EssenceCatalogEntry]


class EldritchOperationRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=80)
    item_text: str | None = Field(default=None, max_length=20_000)
    rarity: Literal["普通", "魔法", "稀有", "传奇"] | None = None
    item_slot: Literal["body_armour", "boots", "gloves", "helmet", "other"] | None = None
    dominance: Literal["焚界者", "灭界者", "平衡", "无"] | None = None
    prefix_count: int | None = Field(default=None, ge=0, le=6)
    suffix_count: int | None = Field(default=None, ge=0, le=6)
    has_searing_implicit: bool | None = None
    has_eater_implicit: bool | None = None
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False

    @model_validator(mode="after")
    def no_mixed_state(self) -> "EldritchOperationRequest":
        manual = (
            self.rarity, self.item_slot, self.dominance, self.prefix_count,
            self.suffix_count, self.has_searing_implicit, self.has_eater_implicit,
        )
        if self.item_text and any(value is not None for value in manual):
            raise ValueError("item_text 与手工状态字段不能混用")
        return self


class EldritchOperationResponse(BaseModel):
    found: bool
    verdict: str
    operation: str
    operation_en: str
    metadata_id: str
    effect_text: str
    action: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    before: dict[str, Any]
    after: dict[str, Any]
    checks: list[dict[str, Any]]
    unknown_fields: list[str]
    metamod_interactions: list[dict[str, Any]]
    boundary: str
    answer: str


class EldritchCatalogEntry(BaseModel):
    name_zh: str
    name_en: str
    action: str
    source_contract_ok: bool


class EldritchCatalogResponse(BaseModel):
    total: int
    items: list[EldritchCatalogEntry]


class VeiledOperationRequest(BaseModel):
    operation: str = Field(min_length=1, max_length=80)
    item_text: str | None = Field(default=None, max_length=20_000)
    rarity: Literal["普通", "魔法", "稀有", "传奇"] | None = None
    item_class: str | None = Field(default=None, min_length=1, max_length=80)
    prefix_count: int | None = Field(default=None, ge=0, le=6)
    suffix_count: int | None = Field(default=None, ge=0, le=6)
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False

    @model_validator(mode="after")
    def no_mixed_state(self) -> "VeiledOperationRequest":
        if self.item_text and any(
            value is not None
            for value in (self.rarity, self.item_class, self.prefix_count, self.suffix_count)
        ):
            raise ValueError("item_text 与手工状态字段不能混用")
        return self


class VeiledOperationResponse(BaseModel):
    found: bool
    verdict: str
    operation: str
    operation_en: str
    metadata_id: str
    effect_text: str
    action: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    before: dict[str, Any]
    after: dict[str, Any]
    checks: list[dict[str, Any]]
    unknown_fields: list[str]
    veiled_pool: dict[str, Any] | None
    metamod_interactions: list[dict[str, Any]]
    boundary: str
    answer: str


class FossilOperationRequest(BaseModel):
    fossil: str = Field(min_length=1, max_length=80)
    item_text: str | None = Field(default=None, max_length=20_000)
    item_class: str | None = Field(default=None, min_length=1, max_length=80)
    item_level: int | None = Field(default=None, ge=1, le=100)
    rarity: Literal["普通", "魔法", "稀有", "传奇"] | None = None
    corrupted: bool = False
    mirrored: bool = False
    fractured: bool = False

    @model_validator(mode="after")
    def no_mixed_state(self) -> "FossilOperationRequest":
        if self.item_text and any(
            value is not None for value in (self.item_class, self.item_level, self.rarity)
        ):
            raise ValueError("item_text 与手工状态字段不能混用")
        return self


class FossilOperationResponse(BaseModel):
    found: bool
    verdict: str
    fossil: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    before: dict[str, Any]
    after: dict[str, Any]
    weight_rules: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    unknown_fields: list[str]
    delve_pool: dict[str, Any] | None
    metamod_interactions: list[dict[str, Any]]
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
        metamod_guard=req.metamod_guard,
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


@router.post("/craft/awakener", response_model=AwakenerOrbResponse)
def awakener_state(
    req: AwakenerOrbRequest, _caller: str = Depends(require_user)
) -> AwakenerOrbResponse:
    result = awakener_orb_state(
        donor_item_text=req.donor_item_text,
        target_item_text=req.target_item_text,
        donor_item_class=req.donor_item_class,
        target_item_class=req.target_item_class,
        donor_rarity=req.donor_rarity or "",
        target_rarity=req.target_rarity or "",
        donor_influences=req.donor_influences,
        target_influences=req.target_influences,
        donor_corrupted=req.donor_corrupted,
        target_corrupted=req.target_corrupted,
        donor_mirrored=req.donor_mirrored,
        target_mirrored=req.target_mirrored,
        donor_fractured=req.donor_fractured,
        target_fractured=req.target_fractured,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_awakener_operation", "reason": result.error,
        })
    if not (result.data or {}).get("found"):
        raise HTTPException(status_code=422, detail={
            "error": "awakener_source_unavailable",
            "reason": (result.data or {}).get("reason", "觉醒者之石来源记录不可用"),
        })
    return AwakenerOrbResponse(**result.data)


@router.get("/craft/essences", response_model=EssenceCatalogResponse)
def essence_catalog(_caller: str = Depends(require_user)) -> EssenceCatalogResponse:
    """当前已入库精华的精确名称；网页选择器不维护另一份手写清单。"""
    rows = dbmod.get_conn().execute(
        "SELECT name_zh,name_en,tier,level_cap FROM poedb_essence "
        "ORDER BY tier,name_zh,metadata_id"
    ).fetchall()
    items = [EssenceCatalogEntry(**dict(row)) for row in rows]
    return EssenceCatalogResponse(total=len(items), items=items)


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


@router.get("/craft/eldritch/operations", response_model=EldritchCatalogResponse)
def eldritch_catalog(_caller: str = Depends(require_user)) -> EldritchCatalogResponse:
    """仅列当前入库的已审计操作；逐字效果漂移也明确标记。"""
    rows = dbmod.get_conn().execute(
        "SELECT metadata_id,effect_zh FROM poedb_currency WHERE metadata_id IN ("
        + ",".join("?" for _ in ELDRITCH_SPECS) + ")",
        tuple(spec.metadata_id for spec in ELDRITCH_SPECS),
    ).fetchall()
    by_id = {row["metadata_id"]: row["effect_zh"] for row in rows}
    items = [EldritchCatalogEntry(
        name_zh=spec.name_zh, name_en=spec.name_en, action=spec.action,
        source_contract_ok=by_id[spec.metadata_id] == spec.expected_effect,
    ) for spec in ELDRITCH_SPECS if spec.metadata_id in by_id]
    return EldritchCatalogResponse(total=len(items), items=items)


@router.post("/craft/eldritch", response_model=EldritchOperationResponse)
def eldritch_state(
    req: EldritchOperationRequest, _caller: str = Depends(require_user)
) -> EldritchOperationResponse:
    result = eldritch_operation_state(
        operation=req.operation,
        item_text=req.item_text,
        rarity=req.rarity,
        item_slot=req.item_slot,
        dominance=req.dominance,
        prefix_count=req.prefix_count,
        suffix_count=req.suffix_count,
        has_searing_implicit=req.has_searing_implicit,
        has_eater_implicit=req.has_eater_implicit,
        corrupted=req.corrupted,
        mirrored=req.mirrored,
        fractured=req.fractured,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_eldritch_operation", "reason": result.error,
        })
    if not (result.data or {}).get("found"):
        raise HTTPException(status_code=422, detail={
            "error": "unsupported_eldritch_operation",
            "reason": "只支持 12 种已审计古灵通货，不按相似名称猜操作语义",
            "supported_operations": (result.data or {}).get("supported_operations", []),
        })
    return EldritchOperationResponse(**result.data)


@router.post("/craft/veiled", response_model=VeiledOperationResponse)
def veiled_state(
    req: VeiledOperationRequest, _caller: str = Depends(require_user)
) -> VeiledOperationResponse:
    result = veiled_operation_state(
        operation=req.operation,
        item_text=req.item_text,
        rarity=req.rarity,
        item_class=req.item_class or "",
        prefix_count=req.prefix_count,
        suffix_count=req.suffix_count,
        corrupted=req.corrupted,
        mirrored=req.mirrored,
        fractured=req.fractured,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_veiled_operation", "reason": result.error,
        })
    if not (result.data or {}).get("found"):
        raise HTTPException(status_code=422, detail={
            "error": "unsupported_veiled_operation",
            "reason": "只支持两种已有逐字效果的加密通货，不按名称猜爱斯林工艺语义",
            "supported_operations": (result.data or {}).get("supported_operations", []),
        })
    return VeiledOperationResponse(**result.data)


@router.post("/craft/fossil", response_model=FossilOperationResponse)
def fossil_state(
    req: FossilOperationRequest, _caller: str = Depends(require_user)
) -> FossilOperationResponse:
    result = fossil_operation_state(
        fossil=req.fossil,
        item_text=req.item_text,
        item_class=req.item_class or "",
        item_level=req.item_level,
        rarity=req.rarity,
        corrupted=req.corrupted,
        mirrored=req.mirrored,
        fractured=req.fractured,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_fossil_operation", "reason": result.error,
        })
    if not (result.data or {}).get("found"):
        raise HTTPException(status_code=422, detail={
            "error": "unsupported_fossil",
            "reason": "只接受标签权重表中 18 件化石的唯一精确中文名，不猜共鸣器或操作语义",
            "supported_fossils": (result.data or {}).get("supported_fossils", []),
        })
    return FossilOperationResponse(**result.data)


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


@router.post("/craft/attempt-plan", response_model=CraftAttemptPlanResponse)
def independent_attempt_plan(
    req: CraftAttemptPlanRequest, _caller: str = Depends(require_user)
) -> CraftAttemptPlanResponse:
    """计算严格独立单次选择模型；不会从目标反推实际制作步骤。"""
    result = build_craft_attempt_plan(
        base_type=req.base_type,
        item_level=req.item_level,
        generation_side=req.generation_side,
        target_query=req.target_query,
        materials=[item.model_dump() for item in req.materials],
        max_attempts=req.max_attempts,
        include_market_prices=req.include_market_prices,
        league=req.league,
    )
    if not result.ok:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_craft_attempt_plan", "reason": result.error,
        })
    return CraftAttemptPlanResponse(**result.data)
