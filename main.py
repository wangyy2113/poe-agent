"""POE1-Agent 入口：单进程 FastAPI + 进程内 uvicorn。

平台中立：不硬编码解释器路径、不调用平台专有命令、信号注册前判存在性、
显式 UTF-8。平台差异只存在于 scripts/ 下的启动与托管脚本。

单实例保护靠**端口绑定失败**，不用 PID 文件也不用 kill——跨平台且天然可靠。

所有路由挂在 `server.base_path`（默认 `/poe_agent`）下，包括 `/docs` 与静态前端。

    python main.py
"""

from __future__ import annotations

import contextlib
import logging
import signal
import sys
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from api import normalize_base_path
from api.admin import router as admin_router
from api.chat import router as chat_router
from api.item import router as item_router
from api.search import router as search_router
from core import db as dbmod
from core.config import REPO_ROOT, get_config, get_secrets
from core.llm import LLMError, get_llm_client
from core.logging_setup import setup_logging

# 导入即注册工具（kb_search / entity_lookup）
import tools.impl  # noqa: F401

logger = logging.getLogger(__name__)

cfg = get_config()
BASE_PATH = normalize_base_path(cfg.get("server.base_path", "/poe_agent"))


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """启动/关停钩子。关停时做 WAL checkpoint，避免残留 -wal/-shm。"""
    dim = int(cfg.get("retrieval.vector.dim", 0) or 0)
    dbmod.init_db(cfg.db_path, vector_dim=dim)
    logger.info("数据库就绪：%s", cfg.db_path)
    logger.info("向量通道：%s", dbmod.vector_status())
    _warn_if_open()
    try:
        yield
    finally:
        dbmod.close_db()
        logger.info("已关闭数据库（含 WAL checkpoint）")


def _warn_if_open() -> None:
    """普通 API 未配 Key 时告警。不阻止启动——本机开发默认无需鉴权，
    但对外提供服务时不配就会被当成免费 LLM 代理（需求 16.9）。"""
    from api.deps import user_keys

    if not user_keys():
        logger.warning(
            "API_KEYS 未配置，%s/chat 无鉴权。仅限本机开发；"
            "对外提供服务前必须配置，否则会被当作免费 LLM 代理", BASE_PATH,
        )


def health() -> dict[str, Any]:
    """反映 SQLite / 向量扩展 / LLM 凭证池 / embedding 通道状态。"""
    out: dict[str, Any] = {"status": "ok", "base_path": BASE_PATH or "/"}
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
    out["tools"] = __import__("tools.registry", fromlist=["REGISTRY"]).REGISTRY.names()
    try:
        sec = get_secrets()
        out["llm"] = {
            "base_url": sec.llm_base_url,
            "model": sec.llm_model,
            "credentials": len(sec.credential_specs()),
        }
        # 凭证池状态（脱敏）：能看出哪个 Key 在冷却、为什么冷却。
        # 建客户端不发请求，所以不产生费用，也不代表 Key 真的可用。
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


def create_app() -> FastAPI:
    base = BASE_PATH
    app = FastAPI(
        title="POE1-Agent",
        version="0.2.0",
        description=(
            "面向《流放之路 1》的领域 AI Agent。核心原则：无据不答。\n\n"
            f"所有路由挂在 `{base or '/'}` 下。"
        ),
        lifespan=_lifespan,
        # 文档与 schema 也放到前缀下，保持一致
        docs_url=f"{base}/docs",
        redoc_url=f"{base}/redoc",
        openapi_url=f"{base}/openapi.json",
    )

    api = APIRouter(prefix=base)
    api.add_api_route("/healthz", health, methods=["GET"], tags=["运维"])
    api.include_router(search_router)
    api.include_router(chat_router)
    api.include_router(item_router)
    api.include_router(admin_router)
    app.include_router(api)

    static_dir = REPO_ROOT / "static"
    has_ui = static_dir.is_dir()

    if base:
        # 裸根路径给个指引，否则用户只会看到 404 而不知道前缀是什么。
        # 有前端就进前端，没有就进 API 文档。
        target = f"{base}/" if has_ui else f"{base}/docs"

        @app.get("/", include_in_schema=False)
        def _root() -> RedirectResponse:
            return RedirectResponse(url=target)

    if has_ui:
        # 挂在 API 路由**之后**：Starlette 按注册顺序匹配，精确路径的接口
        # 先于 Mount 命中，剩下的（/、style.css、app.js）才落到静态文件。
        app.mount(
            base or "/",
            StaticFiles(directory=str(static_dir), html=True),
            name="static",
        )
    return app


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

    logger.info("启动 POE1-Agent，监听 http://%s:%d%s", host, port, BASE_PATH or "")
    logger.info("交互式 API 文档：http://%s:%d%s/docs", host, port, BASE_PATH)
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
