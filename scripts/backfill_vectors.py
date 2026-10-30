"""Build the optional sqlite-vec index from an OpenAI-compatible embedding API.

Default is read-only.  --apply makes remote requests and writes the index; a
failed or interrupted run leaves the vector channel marked dirty and disabled.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db as dbmod  # noqa: E402
from core.config import get_config, get_secrets  # noqa: E402
from retrieval.embedding import EmbeddingClient, EmbeddingError  # noqa: E402
from retrieval.vector import backfill_vectors, vector_index_status  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="远程调用并写入向量索引")
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()
    cfg = get_config()
    secrets = get_secrets()
    if args.apply:
        conn = dbmod.init_db(cfg.db_path)
        extension = dbmod.vector_status()
    else:
        if not cfg.db_path.exists():
            print("数据库不存在；未创建文件", file=sys.stderr)
            return 2
        conn = sqlite3.connect(cfg.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        extension = dbmod._probe_vector(conn)
    try:
        count = int(conn.execute("SELECT count(*) FROM chunk").fetchone()[0])
        status = vector_index_status(conn, secrets.embedding_model)
        print(json.dumps({
            "chunks": count,
            "extension_available": extension.available,
            "extension_reason": extension.reason,
            "model": secrets.embedding_model or None,
            "index_available": status.available,
            "index_reason": status.reason,
            "apply": args.apply,
        }, ensure_ascii=False))
        if not args.apply:
            return 0
        if not extension.available:
            print("sqlite-vec 不可用；索引未修改", file=sys.stderr)
            return 2
        if not (
            secrets.embedding_base_url.strip()
            and secrets.embedding_api_key.strip()
            and secrets.embedding_model.strip()
        ):
            print("Embedding 地址、Key 或模型未配置；索引未修改", file=sys.stderr)
            return 2
        client = EmbeddingClient(
            secrets.embedding_base_url,
            secrets.embedding_api_key,
            secrets.embedding_model,
            timeout_seconds=float(cfg.get("retrieval.timeout_seconds", 8)),
        )
        result = backfill_vectors(conn, client, batch_size=args.batch_size)
        print(json.dumps({
            "status": result.status,
            "model": result.model,
            "dimension": result.dimension,
            "indexed_count": result.indexed_count,
        }, ensure_ascii=False))
        return 0
    except (EmbeddingError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"向量回填失败：{exc}", file=sys.stderr)
        return 2
    finally:
        if args.apply:
            dbmod.close_db()
        else:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
