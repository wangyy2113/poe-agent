"""用户显式长期记忆 API。

只接受主动提交的数据；不会从聊天、PoB 或装备内容自动抽取。每条记录按普通 API 调用方
隔离，可停用或删除。记忆是个性化上下文，不是知识库证据。
"""

from __future__ import annotations

from typing import Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from api.deps import require_user
from core import db as dbmod
from core import user_memory

router = APIRouter(tags=["长期记忆"])
MemoryCategory = Literal["preference", "goal", "constraint", "note"]


def _strip_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if "\x00" in normalized:
        raise ValueError("不能包含 NUL 字符")
    return normalized


class MemoryCreate(BaseModel):
    category: MemoryCategory
    label: str = Field(default="", max_length=80)
    content: str = Field(min_length=1, max_length=500)
    active: bool = True

    _normalize_label = field_validator("label", mode="before")(_strip_text)
    _normalize_content = field_validator("content", mode="before")(_strip_text)


class MemoryUpdate(BaseModel):
    category: MemoryCategory | None = None
    label: str | None = Field(default=None, max_length=80)
    content: str | None = Field(default=None, min_length=1, max_length=500)
    active: bool | None = None

    _normalize_label = field_validator("label", mode="before")(_strip_text)
    _normalize_content = field_validator("content", mode="before")(_strip_text)

    @model_validator(mode="after")
    def at_least_one_change(self) -> "MemoryUpdate":
        if not self.model_fields_set:
            raise ValueError("至少提供一个要修改的字段")
        return self


class MemoryOut(BaseModel):
    memory_id: str
    category: MemoryCategory
    label: str
    content: str
    active: bool
    created_at: str
    updated_at: str


class MemoryListOut(BaseModel):
    items: list[MemoryOut]
    total: int
    include_inactive: bool
    limit: int


def _raise_memory_error(exc: user_memory.MemoryError) -> NoReturn:
    if isinstance(exc, user_memory.MemoryNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, (user_memory.DuplicateMemoryError, user_memory.MemoryLimitError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/memories", response_model=MemoryOut, status_code=201)
def create_memory(req: MemoryCreate, caller: str = Depends(require_user)) -> MemoryOut:
    try:
        item = user_memory.create_memory(
            dbmod.get_conn(), owner_id=caller, category=req.category,
            label=req.label, content=req.content, active=req.active,
        )
    except user_memory.MemoryError as exc:
        _raise_memory_error(exc)
    return MemoryOut(**item)


@router.get("/memories", response_model=MemoryListOut)
def memories(
    include_inactive: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
    caller: str = Depends(require_user),
) -> MemoryListOut:
    items, total = user_memory.list_memories(
        dbmod.get_conn(), owner_id=caller,
        include_inactive=include_inactive, limit=limit,
    )
    return MemoryListOut(
        items=[MemoryOut(**item) for item in items], total=total,
        include_inactive=include_inactive, limit=limit,
    )


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
def update_memory(
    memory_id: str,
    req: MemoryUpdate,
    caller: str = Depends(require_user),
) -> MemoryOut:
    try:
        item = user_memory.update_memory(
            dbmod.get_conn(), owner_id=caller, memory_id=memory_id,
            changes=req.model_dump(exclude_unset=True),
        )
    except user_memory.MemoryError as exc:
        _raise_memory_error(exc)
    return MemoryOut(**item)


@router.delete("/memories/{memory_id}")
def remove_memory(
    memory_id: str, caller: str = Depends(require_user)
) -> dict[str, Any]:
    try:
        user_memory.delete_memory(
            dbmod.get_conn(), owner_id=caller, memory_id=memory_id
        )
    except user_memory.MemoryError as exc:
        _raise_memory_error(exc)
    return {"memory_id": memory_id, "deleted": True}
