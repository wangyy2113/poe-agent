"""POE1-Agent 入口：单进程 FastAPI + 进程内 uvicorn。

平台中立：不硬编码解释器路径、不调用平台专有命令、信号注册前判存在性、
显式 UTF-8。平台差异只存在于 scripts/ 下的启动与托管脚本。

单实例保护靠**端口绑定失败**，不用 PID 文件也不用 kill——跨平台且天然可靠。

    python main.py
"""

from __future__ import annotations

import contextlib
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core import db as dbmod
from core.config import REPO_ROOT, get_config, get_secrets
from core.llm import LLMError, get_llm_client
from core.logging_setup import setup_logging
from retrieval.hybrid import retrieve

logger = logging.getLogger(__name__)

cfg = get_config()


# ── 请求 / 响应模型 ─────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int | None = Field(default=None, ge=1, le=50)
    category: str | None = None


class HitOut(BaseModel):
    chunk_id: str
    entity_id: str | None
    title: str
    url: str
    source: str
    source_priority: int
    channel: str
    score: float
    coverage: float
    text: str


class SearchResponse(BaseModel):
    query: str
    #: False 时上层必须走缺据声明路径，不得基于这些候选作答
    sufficient: bool
    reason: str
    top_coverage: float
    missing_terms: list[str]
    channels: list[str]
    degraded: bool
    note: str
    hits: list[HitOut]


# ── 应用 ───────────────────────────────────────────────────

@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """启动/关停钩子。关停时做 WAL checkpoint，避免残留 -wal/-shm。"""
    dim = int(cfg.get("retrieval.vector.dim", 0) or 0)
    dbmod.init_db(cfg.db_path, vector_dim=dim)
    logger.info("数据库就绪：%s", cfg.db_path)
    logger.info("向量通道：%s", dbmod.vector_status())
    try:
        yield
    finally:
        dbmod.close_db()
        logger.info("已关闭数据库（含 WAL checkpoint）")


def create_app() -> FastAPI:
    app = FastAPI(
        title="POE1-Agent",
        version="0.1.0",
        description="面向《流放之路 1》的领域 AI Agent。核心原则：无据不答。",
        lifespan=_lifespan,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """反映 SQLite / 向量扩展 / LLM 与 embedding 通道配置状态。"""
        out: dict[str, Any] = {"status": "ok"}
        try:
            conn = dbmod.get_conn()
            out["db"] = {
                "path": str(cfg.db_path),
                "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                "entities": conn.execute("SELECT count(*) FROM entity").fetchone()[0],
                "chunks": conn.execute("SELECT count(*) FROM chunk").fetchone()[0],
                "aliases": conn.execute("SELECT count(*) FROM alias").fetchone()[0],
            }
        except Exception as exc:  # noqa: BLE001
            out["status"] = "degraded"
            out["db"] = {"error": f"{type(exc).__name__}: {exc}"}

        vs = dbmod.vector_status()
        out["vector"] = {
            "extension_available": vs.available,
            "reason": vs.reason,
            "enabled": bool(cfg.get("retrieval.vector.enabled", False)),
        }
        try:
            sec = get_secrets()
            out["llm"] = {
                "base_url": sec.llm_base_url,
                "model": sec.llm_model,
                "credentials": len(sec.credential_specs()),
            }
            # 凭证池状态（脱敏）：能看出哪个 Key 在冷却、为什么冷却。
            # 建客户端不发请求，所以这里不会产生费用，也不代表 Key 真的可用。
            try:
                pool = get_llm_client().credential_status()
                out["llm"]["pool"] = pool
                if not any(c["healthy"] for c in pool):
                    out["status"] = "degraded"
            except LLMError as exc:
                out["status"] = "degraded"
                out["llm"]["pool_error"] = str(exc)
            out["embedding"] = {
                "configured": bool(sec.embedding_base_url and sec.embedding_model),
                "model": sec.embedding_model or None,
            }
        except Exception as exc:  # noqa: BLE001
            out["status"] = "degraded"
            out["llm"] = {"error": f"{type(exc).__name__}: {exc}"}
        return out

    @app.post("/search", response_model=SearchResponse)
    def search(req: SearchRequest) -> SearchResponse:
        """纯检索接口，供检索质量评测（需求 3.7）。

        注意 `sufficient` 字段：为 False 时说明知识库没有足够依据，
        调用方**不得**基于返回的候选生成事实性回答。
        """
        conn = dbmod.get_conn()
        result = retrieve(
            conn,
            req.query,
            top_k=req.top_k or int(cfg.get("retrieval.top_k", 20)),
            min_score=float(cfg.get("retrieval.min_score", 0.35)),
            min_coverage=float(cfg.get("retrieval.min_coverage", 0.5)),
            rrf_k=int(cfg.get("retrieval.rrf_k", 60)),
            priority_decay=float(cfg.get("retrieval.priority_decay", 0.08)),
            vector_enabled=bool(cfg.get("retrieval.vector.enabled", False)),
            rerank_enabled=bool(cfg.get("retrieval.rerank.enabled", False)),
            category=req.category,
        )
        return SearchResponse(
            query=result.query,
            sufficient=result.sufficient,
            reason=result.reason,
            top_coverage=result.top_coverage,
            missing_terms=result.missing_terms,
            channels=result.channels,
            degraded=result.degraded,
            note=result.note,
            hits=[
                HitOut(
                    chunk_id=h.chunk_id,
                    entity_id=h.entity_id,
                    title=h.metadata.get("title", ""),
                    url=h.metadata.get("url", ""),
                    source=h.source,
                    source_priority=h.source_priority,
                    channel=h.channel,
                    score=round(h.score, 4),
                    coverage=round(h.coverage, 4),
                    text=h.text,
                )
                for h in result.hits
            ],
        )

    @app.get("/admin/stats")
    def admin_stats(authorization: str = Header(default="")) -> dict[str, Any]:
        """管理端：采集任务与缺口台账。使用独立管理令牌，不与用户 Key 共用。"""
        _require_admin(authorization)
        conn = dbmod.get_conn()
        runs = [
            dict(r)
            for r in conn.execute(
                "SELECT run_id, source, category, started_at, finished_at, total, "
                "succeeded, failed, skipped, elapsed_ms, note FROM ingest_run "
                "ORDER BY run_id DESC LIMIT 20"
            )
        ]
        gaps = [
            dict(r)
            for r in conn.execute(
                "SELECT question, missing_point, guessed_category, hit_count, last_seen "
                "FROM knowledge_gap ORDER BY hit_count DESC LIMIT 50"
            )
        ]
        return {"ingest_runs": runs, "knowledge_gaps": gaps}

    static_dir = REPO_ROOT / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


def _require_admin(authorization: str) -> None:
    """管理端点鉴权。ADMIN_TOKEN 未配置时**拒绝全部请求**——
    不能因为没配就默认放行，那等于把高开销操作公开。"""
    token = ""
    try:
        token = get_secrets().admin_token.strip()
    except Exception:  # noqa: BLE001
        pass
    if not token:
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN 未配置，管理端点已禁用")
    supplied = authorization.removeprefix("Bearer ").strip()
    if supplied != token:
        raise HTTPException(status_code=401, detail="管理凭证无效")


app = create_app()


@app.exception_handler(Exception)
async def _unhandled(_request, exc: Exception) -> JSONResponse:
    """结构化错误，不泄露密钥与堆栈（需求 5.4）。"""
    logger.exception("未处理异常")
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": type(exc).__name__}},
    )


def _install_signal_handlers() -> None:
    """注册前判存在性：Windows 没有 SIGHUP。"""
    def _handler(signum, _frame):  # noqa: ANN001
        logger.info("收到信号 %s，准备退出", signum)
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            signal.signal(sig, _handler)


def main() -> int:
    import uvicorn

    # Windows 默认 cp936 会破坏中文输出
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8")

    setup_logging(
        cfg.logs_dir,
        str(cfg.get("logging.level", "INFO")),
        int(cfg.get("logging.retention_days", 14)),
    )

    # 进程名 best-effort：Windows 上无效，不可作为功能依赖
    with contextlib.suppress(Exception):
        import setproctitle

        setproctitle.setproctitle("poe-agent")

    _install_signal_handlers()

    host = str(cfg.get("server.host", "127.0.0.1"))
    port = int(cfg.get("server.port", 8787))
    if host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning(
            "监听 %s 而非仅本机：管理端点与流式接口会暴露，"
            "请确认已配置反向代理、TLS 与鉴权", host,
        )

    logger.info("启动 POE1-Agent，监听 http://%s:%d", host, port)
    logger.info("单实例保护靠端口绑定：若端口已占用会直接失败退出")

    try:
        uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
    except OSError as exc:
        logger.error("无法绑定 %s:%d —— %s（可能已有实例在运行）", host, port, exc)
        return 1
    except KeyboardInterrupt:
        logger.info("收到中断，正在退出")
    finally:
        dbmod.close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
