"""Shared configuration for API, tools and retrieval diagnostics."""
from __future__ import annotations

import sqlite3

from core.config import get_config
from retrieval.hybrid import RetrievalResult, retrieve


def configured_retrieve(
    conn: sqlite3.Connection, query: str, *, top_k: int | None = None,
    category: str | None = None, league: str | None = None,
    version: str | None = None,
) -> RetrievalResult:
    cfg = get_config()
    return retrieve(
        conn, query,
        top_k=top_k if top_k is not None else int(cfg.get("retrieval.top_k", 20)),
        min_coverage=float(cfg.get("retrieval.min_coverage", 0.5)),
        rrf_k=int(cfg.get("retrieval.rrf_k", 60)),
        priority_decay=float(cfg.get("retrieval.priority_decay", 0.08)),
        timeout_seconds=float(cfg.get("retrieval.timeout_seconds", 8)),
        category=category, league=league, version=version,
    )
