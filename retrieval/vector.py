"""Read legacy index metadata without loading an extension or calling a model."""
from __future__ import annotations

import sqlite3


def vector_index_metadata(conn: sqlite3.Connection) -> dict[str, object]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vector_index_state'"
    ).fetchone()
    row = conn.execute(
        "SELECT dim,indexed_count,dirty FROM vector_index_state WHERE id=1"
    ).fetchone() if exists else None
    return {
        "present": row is not None,
        "dimension": int(row[0]) if row else 0,
        "indexed_count": int(row[1]) if row else 0,
        "dirty": bool(row[2]) if row else False,
    }
