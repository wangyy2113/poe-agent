"""Inspect a legacy vector index without invoking a second model.

--apply is retained only to reject old automation before it changes any files.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import get_config  # noqa: E402
from core.model_policy import AUXILIARY_MODEL_REASON  # noqa: E402
from retrieval.vector import vector_index_metadata  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="单模型模式不允许远程回填")
    parser.add_argument("--batch-size", type=int, default=16, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.apply:
        print(AUXILIARY_MODEL_REASON + "；索引未修改", file=sys.stderr)
        return 2
    cfg = get_config()
    if not cfg.db_path.exists():
        print("数据库不存在；未创建文件", file=sys.stderr)
        return 2
    conn = sqlite3.connect(cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        metadata = vector_index_metadata(conn)
        print(json.dumps({
            "chunks": int(conn.execute("SELECT count(*) FROM chunk").fetchone()[0]),
            "index": metadata,
            "runtime_enabled": False, "reason": AUXILIARY_MODEL_REASON,
        }, ensure_ascii=False))
        return 0
    except sqlite3.Error:
        print("旧索引不可读取；未修改文件", file=sys.stderr)
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
