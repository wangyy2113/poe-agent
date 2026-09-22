"""`/admin/*`：采集记录、缺口台账、跨来源差异、执行轨迹。使用独立管理凭证。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import require_admin
from core import db as dbmod
from core.config import get_config
from kb.sync import CATEGORY_SPECS, build_plan, load_sync_run, validate_categories

router = APIRouter(prefix="/admin", tags=["管理"], dependencies=[Depends(require_admin)])


@router.get("/sync/plan")
def sync_plan(
    categories: str = Query(default="", description="逗号分隔的分类；空值使用配置"),
    stale_after_hours: float | None = Query(default=None, gt=0),
) -> dict[str, Any]:
    """只读同步计划；不会联网，也不会修改事实表。"""
    cfg = get_config()
    raw = (
        [item.strip() for item in categories.split(",") if item.strip()]
        if categories
        else cfg.get("knowledge_sync.categories", [spec.name for spec in CATEGORY_SPECS])
    )
    if not isinstance(raw, list):
        raise HTTPException(status_code=503, detail="knowledge_sync.categories 配置无效")
    try:
        selected = validate_categories(str(item) for item in raw)
        plan = build_plan(
            dbmod.get_conn(),
            categories=selected,
            stale_after_hours=(
                stale_after_hours
                if stale_after_hours is not None
                else float(cfg.get("knowledge_sync.stale_after_hours", 168))
            ),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "items": [item.as_dict() for item in plan],
        "due": [item.category for item in plan if item.due],
        "boundary": "read_only_plan_use_scripts_sync_knowledge_py_apply_to_execute",
    }


@router.get("/sync/runs")
def sync_runs(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    """最近的跨分类同步运行与汇总，不展开逐行样例。"""
    rows = []
    for row in dbmod.get_conn().execute(
        "SELECT sync_id,mode,requested,planned,status,started_at,finished_at,summary,error "
        "FROM knowledge_sync_run ORDER BY started_at DESC LIMIT ?",
        (limit,),
    ):
        item = dict(row)
        for field in ("requested", "planned", "summary"):
            if item.get(field):
                item[field] = json.loads(item[field])
        rows.append(item)
    return {"items": rows}


@router.get("/sync/runs/{sync_id}")
def sync_run_detail(sync_id: str) -> dict[str, Any]:
    """一次同步的分类级新增、删除、修改、版本字段变化与样例键。"""
    result = load_sync_run(dbmod.get_conn(), sync_id)
    if result is None:
        raise HTTPException(status_code=404, detail="同步记录不存在")
    return result


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
    # 采集优先级排除两类噪声（第三十轮实测，台账 93% 是评测流量）：
    #   source='eval'  评测把同一批问题反复跑，hit_count 量的是「跑了几次」
    #   kind='out_of_scope'  POE2 永远不会被采集，它没有待办
    # `ingest_gap` 反过来要**加进来**：它在用、却一直没在这张视图里露出来，
    # 而「这 51 条 POEDB 有而 PoB 没有」恰恰是最可执行的一类。
    gaps = [
        dict(r)
        for r in conn.execute(
            "SELECT gap_id, question, missing_point, guessed_category, kind, "
            "COALESCE(source,'chat') AS source, hit_count, first_seen, last_seen "
            "FROM knowledge_gap "
            "WHERE COALESCE(kind,'retrieval_gap') IN ('retrieval_gap','tool_gap','ingest_gap') "
            "AND COALESCE(source,'chat') <> 'eval' "
            "ORDER BY hit_count DESC LIMIT 50"
        )
    ]
    #: 被排除掉的条目数——藏起来就等于又给自己一个失真的信号。
    #: `coverage_note` 是第五十七轮加的第三档：那些条目说的是「上游那一页本来就
    #: 没有这一段」，去补它需要新来源，不是待办（见 `core.db.GAP_KINDS`）。
    excluded = {
        str(r["why"]): r["n"]
        for r in conn.execute(
            "SELECT CASE COALESCE(kind,'retrieval_gap') "
            "  WHEN 'out_of_scope' THEN 'out_of_scope' "
            "  WHEN 'coverage_note' THEN 'coverage_note' "
            "  ELSE 'eval_traffic' END AS why, count(*) AS n "
            "FROM knowledge_gap "
            "WHERE COALESCE(kind,'retrieval_gap') IN "
            "('retrieval_gap','tool_gap','ingest_gap','out_of_scope','coverage_note') "
            "AND (COALESCE(source,'chat')='eval' "
            "OR COALESCE(kind,'retrieval_gap') IN ('out_of_scope','coverage_note')) "
            "GROUP BY 1"
        )
    }
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
        #: 采集优先级：按频次排序即下一步该补哪类数据（已排除评测流量与永久越界）
        "knowledge_gaps": gaps,
        #: 从优先级里排除掉的条目数，按原因分。不列出来就等于换一个失真信号
        "gaps_excluded": excluded,
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
