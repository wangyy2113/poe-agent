"""`/admin/*`：采集记录、缺口台账、跨来源差异、执行轨迹。使用独立管理凭证。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.deps import require_admin
from core import db as dbmod

router = APIRouter(prefix="/admin", tags=["管理"], dependencies=[Depends(require_admin)])


@router.get("/stats")
def stats() -> dict[str, Any]:
    """采集任务与缺口台账。缺口按命中频次排序 —— 即下一步该补哪类数据。"""
    conn = dbmod.get_conn()
    runs = [
        dict(r)
        for r in conn.execute(
            "SELECT run_id, source, category, started_at, finished_at, total, "
            "succeeded, failed, skipped, elapsed_ms, note FROM ingest_run "
            "ORDER BY run_id DESC LIMIT 20"
        )
    ]
    # 采集优先级只看 retrieval_gap 与 tool_gap。出口闸门的拦截（binding_block）
    # 是闸门行为的观测量，混进这张表会让频次排序失真：一次回答里被拦的每句话
    # 都会占一行，而它们大多不是「该去采集什么」。
    gaps = [
        dict(r)
        for r in conn.execute(
            "SELECT gap_id, question, missing_point, guessed_category, kind, hit_count, "
            "first_seen, last_seen FROM knowledge_gap "
            "WHERE COALESCE(kind,'retrieval_gap') IN ('retrieval_gap','tool_gap') "
            "ORDER BY hit_count DESC LIMIT 50"
        )
    ]
    by_kind = {
        str(r["kind"] or "retrieval_gap"): r["n"]
        for r in conn.execute(
            "SELECT COALESCE(kind,'retrieval_gap') AS kind, count(*) AS n "
            "FROM knowledge_gap GROUP BY 1 ORDER BY 2 DESC"
        )
    }
    blocked = [
        dict(r)
        for r in conn.execute(
            "SELECT question, missing_point, guessed_category, hit_count, last_seen "
            "FROM knowledge_gap WHERE kind='binding_block' "
            "ORDER BY hit_count DESC LIMIT 20"
        )
    ]
    sizes = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in ("entity", "chunk", "alias", "mod_affix", "pob_mod", "source_conflict")
    }
    return {
        "tables": sizes,
        "ingest_runs": runs,
        #: 采集优先级：按频次排序即下一步该补哪类数据
        "knowledge_gaps": gaps,
        #: 台账各来源的条目数。`binding_block_legacy` 是迁移前混入的闸门噪声
        "gap_counts_by_kind": by_kind,
        #: 出口闸门拦下的无据断言，按归一化后的句子聚合。这是闸门观测量，不是采集待办
        "blocked_claims": blocked,
    }


@router.get("/conflicts")
def conflicts(
    kind: str = Query(default="", description="mod / mod_review / unique / base / constant"),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """跨来源差异台账。

    注意 `kind` 的区别：`mod` 是**已确认冲突**，会按需求 15.5 并列展示给用户；
    `mod_review` 只是「两源对不上，待人工核对」，不得当作冲突呈现。
    """
    conn = dbmod.get_conn()
    sql = (
        "SELECT conflict_id, subject, field, kind, source_a, value_a, url_a, "
        "source_b, value_b, url_b, note, resolved, first_seen, last_seen "
        "FROM source_conflict"
    )
    args: tuple = ()
    if kind:
        sql += " WHERE kind = ?"
        args = (kind,)
    sql += " ORDER BY conflict_id DESC LIMIT ?"
    rows = [dict(r) for r in conn.execute(sql, (*args, limit))]
    by_kind = {
        r["kind"]: r["n"]
        for r in conn.execute("SELECT kind, count(*) n FROM source_conflict GROUP BY 1")
    }
    return {"by_kind": by_kind, "items": rows}


@router.get("/traces")
def traces(limit: int = Query(default=20, ge=1, le=200)) -> dict[str, Any]:
    """执行轨迹。含每条断言的绑定校验结果与拦截原因（需求 4.5）。"""
    conn = dbmod.get_conn()
    out = []
    for r in conn.execute(
        "SELECT trace_id, conversation_id, intent, plan, tool_calls, retrieval_hits, "
        "binding_result, tokens_prompt, tokens_output, tokens_reasoning, elapsed_ms, "
        "created_at FROM trace ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ):
        d = dict(r)
        for f in ("tool_calls", "retrieval_hits", "binding_result"):
            if d.get(f):
                try:
                    d[f] = json.loads(d[f])
                except (TypeError, json.JSONDecodeError):
                    pass
        out.append(d)
    return {"items": out}


@router.get("/trace/{trace_id}")
def trace_detail(trace_id: str) -> dict[str, Any]:
    conn = dbmod.get_conn()
    row = conn.execute("SELECT * FROM trace WHERE trace_id = ?", (trace_id,)).fetchone()
    if row is None:
        return {"error": {"code": "not_found", "message": "轨迹不存在"}}
    d = dict(row)
    for f in ("tool_calls", "retrieval_hits", "binding_result"):
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except (TypeError, json.JSONDecodeError):
                pass
    return d
