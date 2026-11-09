"""One policy gate for every production vector retrieval entry point."""

from __future__ import annotations

import sqlite3

from core import db as dbmod
from core.config import Secrets
from core.model_policy import AUXILIARY_MODEL_REASON
from retrieval.embedding import EmbeddingClient


def vector_runtime(
    conn: sqlite3.Connection, requested: bool, secrets: Secrets | None,
    *, timeout_seconds: float = 20.0,
) -> tuple[dbmod.VectorStatus, EmbeddingClient | None]:
    # Keep old indexes and their metadata intact; they do not authorize a second model.
    return dbmod.VectorStatus(False, AUXILIARY_MODEL_REASON if requested else "配置关闭"), None


def configured_vector_runtime(
    conn: sqlite3.Connection, requested: bool, *, timeout_seconds: float = 20.0
) -> tuple[dbmod.VectorStatus, EmbeddingClient | None]:
    return vector_runtime(conn, requested, None, timeout_seconds=timeout_seconds)
