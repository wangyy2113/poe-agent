"""One runtime gate for the HTTP and Knowledge-tool vector channels."""

from __future__ import annotations

import sqlite3

from core import db as dbmod
from core.config import Secrets, get_secrets
from retrieval.embedding import EmbeddingClient
from retrieval.vector import vector_index_status


def vector_runtime(
    conn: sqlite3.Connection, requested: bool, secrets: Secrets | None,
    *, timeout_seconds: float = 20.0,
) -> tuple[dbmod.VectorStatus, EmbeddingClient | None]:
    base_url = str(getattr(secrets, "embedding_base_url", ""))
    api_key = str(getattr(secrets, "embedding_api_key", ""))
    model = str(getattr(secrets, "embedding_model", ""))
    configured = bool(base_url.strip() and api_key.strip() and model.strip())
    client = (
        EmbeddingClient(
            base_url,
            api_key,
            model,
            timeout_seconds=timeout_seconds,
        ) if configured else None
    )
    index = (
        vector_index_status(conn, model)
        if configured and requested else None
    )
    status = dbmod.vector_channel_status(
        requested,
        query_adapter_ready=bool(client and index and index.available),
        query_adapter_reason=(
            index.reason if index else "Embedding 服务地址、Key 或模型未配置"
        ),
    )
    return status, client if status.available else None


def configured_vector_runtime(
    conn: sqlite3.Connection, requested: bool, *, timeout_seconds: float = 20.0
) -> tuple[dbmod.VectorStatus, EmbeddingClient | None]:
    """Avoid loading LLM secrets for the always-available lexical path."""
    if not requested:
        return vector_runtime(conn, False, None, timeout_seconds=timeout_seconds)
    try:
        secrets = get_secrets()
    except Exception:  # noqa: BLE001  配置缺失只禁用可选向量通道
        secrets = None
    return vector_runtime(conn, True, secrets, timeout_seconds=timeout_seconds)
