"""跨来源差异台账的用户只读查询与聚合。

`source_conflict` 同时保存已确认冲突和待核对差异。任何 `kind` 以 ``_review`` 结尾的行都
只能称为 review，绝不能因“两侧值不同”被升级为已确认冲突。本模块把分类、过滤、稳定游标、
分页和聚合收口到同一处，普通 API、工具与管理视图不再各写一套易漂移的 SQL。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Literal


ConflictStatus = Literal["confirmed", "review"]
StatusFilter = Literal["confirmed", "review", "all"]
ResolutionFilter = Literal["active", "resolved", "all"]
MAX_LIMIT = 100
MAX_QUERY_LENGTH = 160
MAX_FILTER_VALUES = 20
MAX_TEXT_LENGTH = 2000
MAX_DETAIL_TEXT_LENGTH = 16000


class ConflictQueryError(ValueError):
    """查询过滤器、游标或分页范围不合法。"""


@dataclass(frozen=True)
class ConflictItem:
    conflict_id: int
    subject: str
    field: str
    kind: str
    status: ConflictStatus
    source_a: str
    value_a: str
    url_a: str | None
    source_b: str
    value_b: str
    url_b: str | None
    note: str | None
    cause: str | None
    resolved: bool
    first_seen: str
    last_seen: str


@dataclass(frozen=True)
class ConflictDetail(ConflictItem):
    """显式按 ID 读取的原值；过长字段保留长度与截断状态。"""

    ref_a: str | None
    ref_b: str | None
    value_a_length: int
    value_b_length: int
    value_a_truncated: bool
    value_b_truncated: bool


@dataclass(frozen=True)
class ConflictLookupResult:
    status_filter: StatusFilter
    resolution_filter: ResolutionFilter
    query: str
    kinds: tuple[str, ...]
    sources: tuple[str, ...]
    total_matching: int
    returned: int
    confirmed_count: int
    review_count: int
    resolved_count: int
    has_more: bool
    next_cursor: str | None
    items: tuple[ConflictItem, ...]
    classification_rule: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConflictKindSummary:
    kind: str
    status: ConflictStatus
    active_count: int
    resolved_count: int
    total_count: int


@dataclass(frozen=True)
class ConflictSourcePairSummary:
    source_a: str
    source_b: str
    confirmed_count: int
    review_count: int
    total_count: int


@dataclass(frozen=True)
class ConflictCauseSummary:
    cause: str
    review_count: int


@dataclass(frozen=True)
class ConflictSummary:
    total_count: int
    active_count: int
    resolved_count: int
    confirmed_count: int
    confirmed_active_count: int
    review_count: int
    review_active_count: int
    kinds: tuple[ConflictKindSummary, ...]
    source_pairs: tuple[ConflictSourcePairSummary, ...]
    review_causes: tuple[ConflictCauseSummary, ...]
    classification_rule: str
    user_facing_rule: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def status_for_kind(kind: object) -> ConflictStatus:
    value = str(kind or "").strip()
    if not value:
        raise ConflictQueryError("source_conflict.kind 不能为空")
    return "review" if value.endswith("_review") else "confirmed"


def _text(value: object, label: str, *, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise ConflictQueryError(f"{label} 必须是字符串")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise ConflictQueryError(f"{label} 最多 {maximum} 个字符")
    return normalized


def _values(values: object, label: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)) or len(values) > MAX_FILTER_VALUES:
        raise ConflictQueryError(f"{label} 必须是至多 {MAX_FILTER_VALUES} 项的数组")
    normalized: list[str] = []
    for index, value in enumerate(values):
        item = _text(value, f"{label}[{index}]", maximum=80)
        if not item:
            raise ConflictQueryError(f"{label}[{index}] 不能为空")
        if item not in normalized:
            normalized.append(item)
    # 这两个过滤器是集合语义；固定顺序后，同一组值仅重排不会让游标指纹漂移。
    return tuple(sorted(normalized))


def _validate_filters(
    *, query: object, status: object, resolution: object,
    kinds: object, sources: object, limit: object,
) -> tuple[str, StatusFilter, ResolutionFilter, tuple[str, ...], tuple[str, ...], int]:
    normalized_query = _text(query or "", "query", maximum=MAX_QUERY_LENGTH)
    if status not in {"confirmed", "review", "all"}:
        raise ConflictQueryError("status 必须是 confirmed、review 或 all")
    if resolution not in {"active", "resolved", "all"}:
        raise ConflictQueryError("resolution 必须是 active、resolved 或 all")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ConflictQueryError(f"limit 必须是 1..{MAX_LIMIT} 的整数")
    return (
        normalized_query, status, resolution,
        _values(kinds, "kinds"), _values(sources, "sources"), limit,
    )  # type: ignore[return-value]


def _scope_fingerprint(
    *, query: str, status: StatusFilter, resolution: ResolutionFilter,
    kinds: tuple[str, ...], sources: tuple[str, ...],
) -> str:
    payload = json.dumps({
        "query": query, "status": status, "resolution": resolution,
        "kinds": kinds, "sources": sources,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _encode_cursor(*, last_id: int, fingerprint: str) -> str:
    raw = json.dumps(
        {"v": 1, "last_id": last_id, "scope": fingerprint},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: object, *, fingerprint: str) -> int | None:
    if cursor is None or cursor == "":
        return None
    if not isinstance(cursor, str) or len(cursor) > 500:
        raise ConflictQueryError("cursor 无效")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True,
        )
        payload = json.loads(decoded)
    except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ConflictQueryError("cursor 无法解码") from exc
    if (
        not isinstance(payload, dict) or payload.get("v") != 1
        or payload.get("scope") != fingerprint
        or isinstance(payload.get("last_id"), bool)
        or not isinstance(payload.get("last_id"), int)
        or payload["last_id"] <= 0
    ):
        raise ConflictQueryError("cursor 与当前过滤条件不匹配或已损坏")
    return int(payload["last_id"])


def _where(
    *, query: str, status: StatusFilter, resolution: ResolutionFilter,
    kinds: tuple[str, ...], sources: tuple[str, ...], last_id: int | None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    args: list[object] = []
    if status == "confirmed":
        clauses.append("substr(kind, -7) <> '_review'")
    elif status == "review":
        clauses.append("substr(kind, -7) = '_review'")
    if resolution == "active":
        clauses.append("COALESCE(resolved, 0) = 0")
    elif resolution == "resolved":
        clauses.append("COALESCE(resolved, 0) <> 0")
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        clauses.append(
            "(subject LIKE ? ESCAPE '\\' OR field LIKE ? ESCAPE '\\' "
            "OR value_a LIKE ? ESCAPE '\\' OR value_b LIKE ? ESCAPE '\\')"
        )
        args.extend([pattern] * 4)
    if kinds:
        clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
        args.extend(kinds)
    if sources:
        placeholders = ",".join("?" for _ in sources)
        clauses.append(f"(source_a IN ({placeholders}) OR source_b IN ({placeholders}))")
        args.extend(sources)
        args.extend(sources)
    if last_id is not None:
        clauses.append("conflict_id < ?")
        args.append(last_id)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), args


def _clip(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= MAX_TEXT_LENGTH else text[: MAX_TEXT_LENGTH - 1] + "…"


def _item(row: sqlite3.Row) -> ConflictItem:
    return ConflictItem(
        conflict_id=int(row["conflict_id"]), subject=str(row["subject"]),
        field=str(row["field"]), kind=str(row["kind"]),
        status=status_for_kind(row["kind"]),
        source_a=str(row["source_a"]), value_a=_clip(row["value_a"]) or "",
        url_a=_clip(row["url_a"]), source_b=str(row["source_b"]),
        value_b=_clip(row["value_b"]) or "", url_b=_clip(row["url_b"]),
        note=_clip(row["note"]), cause=_clip(row["cause"]),
        resolved=bool(row["resolved"]), first_seen=str(row["first_seen"]),
        last_seen=str(row["last_seen"]),
    )


def _detail_value(value: object) -> tuple[str, int, bool]:
    text = str(value)
    length = len(text)
    return text[:MAX_DETAIL_TEXT_LENGTH], length, length > MAX_DETAIL_TEXT_LENGTH


def get_conflict(conn: sqlite3.Connection, conflict_id: object) -> ConflictDetail | None:
    """ID 精确查询，不通过 review 过滤器提升其事实资格。"""
    if isinstance(conflict_id, bool) or not isinstance(conflict_id, int) or conflict_id <= 0:
        raise ConflictQueryError("conflict_id 必须是正整数")
    row = conn.execute(
        "SELECT conflict_id, subject, field, kind, source_a, value_a, ref_a, url_a, "
        "source_b, value_b, ref_b, url_b, note, cause, resolved, first_seen, last_seen "
        "FROM source_conflict WHERE conflict_id = ?",
        (conflict_id,),
    ).fetchone()
    if row is None:
        return None
    value_a, len_a, truncated_a = _detail_value(row["value_a"])
    value_b, len_b, truncated_b = _detail_value(row["value_b"])
    return ConflictDetail(
        conflict_id=int(row["conflict_id"]), subject=str(row["subject"]),
        field=str(row["field"]), kind=str(row["kind"]),
        status=status_for_kind(row["kind"]),
        source_a=str(row["source_a"]), value_a=value_a,
        ref_a=_clip(row["ref_a"]), url_a=_clip(row["url_a"]),
        source_b=str(row["source_b"]), value_b=value_b,
        ref_b=_clip(row["ref_b"]), url_b=_clip(row["url_b"]),
        note=_clip(row["note"]), cause=_clip(row["cause"]),
        resolved=bool(row["resolved"]), first_seen=str(row["first_seen"]),
        last_seen=str(row["last_seen"]),
        value_a_length=len_a, value_b_length=len_b,
        value_a_truncated=truncated_a, value_b_truncated=truncated_b,
    )


def lookup_conflicts(
    conn: sqlite3.Connection, *, query: object = "", status: object = "confirmed",
    resolution: object = "active", kinds: object = None, sources: object = None,
    limit: object = 20, cursor: object = None,
) -> ConflictLookupResult:
    query_value, status_value, resolution_value, kind_values, source_values, page_size = (
        _validate_filters(
            query=query, status=status, resolution=resolution,
            kinds=kinds, sources=sources, limit=limit,
        )
    )
    fingerprint = _scope_fingerprint(
        query=query_value, status=status_value, resolution=resolution_value,
        kinds=kind_values, sources=source_values,
    )
    last_id = _decode_cursor(cursor, fingerprint=fingerprint)
    page_where, page_args = _where(
        query=query_value, status=status_value, resolution=resolution_value,
        kinds=kind_values, sources=source_values, last_id=last_id,
    )
    rows = conn.execute(
        "SELECT conflict_id, subject, field, kind, source_a, value_a, url_a, "
        "source_b, value_b, url_b, note, cause, resolved, first_seen, last_seen "
        f"FROM source_conflict{page_where} ORDER BY conflict_id DESC LIMIT ?",
        (*page_args, page_size + 1),
    ).fetchall()
    has_more = len(rows) > page_size
    page = rows[:page_size]
    # Total 不受 cursor 影响，便于 UI 显示“第 N 页 / 共多少条”。
    total_where, total_args = _where(
        query=query_value, status=status_value, resolution=resolution_value,
        kinds=kind_values, sources=source_values, last_id=None,
    )
    total = int(conn.execute(
        f"SELECT count(*) FROM source_conflict{total_where}", total_args,
    ).fetchone()[0])
    items = tuple(_item(row) for row in page)
    next_cursor = (
        _encode_cursor(last_id=items[-1].conflict_id, fingerprint=fingerprint)
        if has_more and items else None
    )
    return ConflictLookupResult(
        status_filter=status_value, resolution_filter=resolution_value,
        query=query_value, kinds=kind_values, sources=source_values,
        total_matching=total, returned=len(items),
        confirmed_count=sum(item.status == "confirmed" for item in items),
        review_count=sum(item.status == "review" for item in items),
        resolved_count=sum(item.resolved for item in items),
        has_more=has_more, next_cursor=next_cursor, items=items,
        classification_rule=(
            "kind 以 _review 结尾的行仅为待核对差异；其它 kind 才属于已确认冲突"
        ),
    )


def summarize_conflicts(conn: sqlite3.Connection) -> ConflictSummary:
    totals = conn.execute(
        "SELECT count(*) total, "
        "sum(CASE WHEN COALESCE(resolved,0)=0 THEN 1 ELSE 0 END) active, "
        "sum(CASE WHEN COALESCE(resolved,0)<>0 THEN 1 ELSE 0 END) resolved, "
        "sum(CASE WHEN substr(kind,-7)<>'_review' THEN 1 ELSE 0 END) confirmed, "
        "sum(CASE WHEN substr(kind,-7)<>'_review' AND COALESCE(resolved,0)=0 THEN 1 ELSE 0 END) confirmed_active, "
        "sum(CASE WHEN substr(kind,-7)='_review' THEN 1 ELSE 0 END) review, "
        "sum(CASE WHEN substr(kind,-7)='_review' AND COALESCE(resolved,0)=0 THEN 1 ELSE 0 END) review_active "
        "FROM source_conflict"
    ).fetchone()
    kinds = tuple(ConflictKindSummary(
        kind=str(row["kind"]), status=status_for_kind(row["kind"]),
        active_count=int(row["active"] or 0),
        resolved_count=int(row["resolved"] or 0), total_count=int(row["total"] or 0),
    ) for row in conn.execute(
        "SELECT kind, count(*) total, "
        "sum(CASE WHEN COALESCE(resolved,0)=0 THEN 1 ELSE 0 END) active, "
        "sum(CASE WHEN COALESCE(resolved,0)<>0 THEN 1 ELSE 0 END) resolved "
        "FROM source_conflict GROUP BY kind ORDER BY total DESC, kind"
    ))
    source_pairs = tuple(ConflictSourcePairSummary(
        source_a=str(row["source_a"]), source_b=str(row["source_b"]),
        confirmed_count=int(row["confirmed"] or 0),
        review_count=int(row["review"] or 0), total_count=int(row["total"] or 0),
    ) for row in conn.execute(
        "SELECT source_a, source_b, count(*) total, "
        "sum(CASE WHEN substr(kind,-7)<>'_review' THEN 1 ELSE 0 END) confirmed, "
        "sum(CASE WHEN substr(kind,-7)='_review' THEN 1 ELSE 0 END) review "
        "FROM source_conflict GROUP BY source_a, source_b "
        "ORDER BY total DESC, source_a, source_b"
    ))
    causes = tuple(ConflictCauseSummary(
        cause=str(row["cause"] or "未标注"), review_count=int(row["n"]),
    ) for row in conn.execute(
        "SELECT COALESCE(NULLIF(cause,''),'未标注') cause, count(*) n "
        "FROM source_conflict WHERE substr(kind,-7)='_review' "
        "GROUP BY COALESCE(NULLIF(cause,''),'未标注') ORDER BY n DESC, cause"
    ))
    return ConflictSummary(
        total_count=int(totals["total"] or 0), active_count=int(totals["active"] or 0),
        resolved_count=int(totals["resolved"] or 0),
        confirmed_count=int(totals["confirmed"] or 0),
        confirmed_active_count=int(totals["confirmed_active"] or 0),
        review_count=int(totals["review"] or 0),
        review_active_count=int(totals["review_active"] or 0),
        kinds=kinds, source_pairs=source_pairs, review_causes=causes,
        classification_rule=(
            "kind 以 _review 结尾的行仅为待核对差异；其它 kind 才属于已确认冲突"
        ),
        user_facing_rule=(
            "默认用户查询只返回 active confirmed；review 必须显式请求并始终标注未确认"
        ),
    )
