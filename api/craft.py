"""制作操作状态机 API；路由只做输入校验并调用同一结构化工具实现。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from api.deps import require_user
from tools.impl.craft_operation import craft_operation_state

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
