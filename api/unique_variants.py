"""PoB 唯一装备命名变体的只读查询。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from core import db as dbmod
from kb.unique_variants import (
    VariantQueryError, compare_variants, list_variants, variant_detail,
)

router = APIRouter(prefix="/unique-variants", tags=["唯一装备历史变体"])


@router.get("")
def variants(name: str = Query(min_length=1, max_length=150)) -> dict:
    try:
        return list_variants(dbmod.get_conn(), name)
    except VariantQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/detail")
def detail(
    name: str = Query(min_length=1, max_length=150),
    index: int = Query(ge=1),
) -> dict:
    try:
        return variant_detail(dbmod.get_conn(), name, index)
    except VariantQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/compare")
def compare(
    name: str = Query(min_length=1, max_length=150),
    left_index: int = Query(ge=1),
    right_index: int = Query(ge=1),
) -> dict:
    try:
        return compare_variants(dbmod.get_conn(), name, left_index, right_index)
    except VariantQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
