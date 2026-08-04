#!/usr/bin/env python3
"""自检：配置加载、数据库初始化、FTS5、向量扩展探测、日志。

可在任何平台重复运行，用于验证环境是否就绪。
    python scripts/selfcheck.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import REPO_ROOT, get_config, mask  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 68)
    print("POE1-Agent 自检")
    print("=" * 68)
    print(f"  仓库根   : {REPO_ROOT}")
    print(f"  Python   : {sys.version.split()[0]}  ({sys.platform})")

    ok = True

    # ── 配置 ──
    cfg = get_config()
    print("\n[配置]")
    print(f"  监听        : {cfg.get('server.host')}:{cfg.get('server.port')}")
    print(f"  DB          : {cfg.db_path}")
    print(f"  缓存        : {cfg.cache_dir}")
    print(f"  日志        : {cfg.logs_dir}")
    print(f"  向量启用    : {cfg.get('retrieval.vector.enabled')}")
    print(f"  重排启用    : {cfg.get('retrieval.rerank.enabled')}")
    print(f"  min_score   : {cfg.get('retrieval.min_score')}")
    print(f"  POEDB 优先级: {cfg.priority_of('POEDB')}  未知来源: {cfg.priority_of('某站')}")
    print(f"  别名解析    : glm -> {cfg.resolve_model('glm')}")
    if cfg.priority_of("POEDB") != 1:
        print("  !! POEDB 优先级应为 1")
        ok = False

    # ── 密钥（只显示脱敏标签）──
    print("\n[密钥]")
    try:
        from core.config import get_secrets

        sec = get_secrets()
        specs = sec.credential_specs()
        print(f"  LLM base_url : {sec.llm_base_url}")
        print(f"  LLM model    : {sec.llm_model}")
        print(f"  凭证数       : {len(specs)}")
        for key, model in specs:
            print(f"    - {mask(key)}  绑定模型={model or '(沿用默认)'}")
        print(f"  ADMIN_TOKEN  : {'已设置' if sec.admin_token else '未设置（/admin 将拒绝全部请求）'}")
    except Exception as exc:  # 缺必填项时 pydantic 会抛
        print(f"  !! 读取失败: {type(exc).__name__}: {exc}")
        ok = False

    # ── 日志 ──
    setup_logging(cfg.logs_dir, str(cfg.get("logging.level", "INFO")))
    logs = sorted(cfg.logs_dir.glob("app-*.log"))
    print("\n[日志]")
    print(f"  文件: {[p.name for p in logs] or '（尚无）'}")
    if not logs:
        print("  !! 未生成日志文件")
        ok = False

    # ── 数据库 ──
    print("\n[数据库]")
    dim = int(cfg.get("retrieval.vector.dim", 0) or 0)
    conn = dbmod.init_db(cfg.db_path, vector_dim=dim)
    tables = [
        r[0]
        for r in conn.execute(
            "select name from sqlite_master where type in ('table','view') order by name"
        )
    ]
    print(f"  journal_mode : {conn.execute('PRAGMA journal_mode').fetchone()[0]}")
    print(f"  表 ({len(tables)}): {', '.join(tables)}")

    expected = {
        "entity", "alias", "chunk", "chunk_fts",
        "ingest_run", "knowledge_gap", "conversation", "message", "trace",
    }
    missing = expected - set(tables)
    if missing:
        print(f"  !! 缺表: {sorted(missing)}")
        ok = False

    # FTS5 + bm25 实测
    try:
        conn.execute(
            "insert into chunk_fts(tokens, keywords, chunk_id, entity_id) values (?,?,?,?)",
            ("猎首 皮革腰带 击败 稀有 怪物 获得 属性 修正", "偷取 词缀 怪物 死亡", "_probe", "_probe"),
        )
        rows = conn.execute(
            "select chunk_id, bm25(chunk_fts) from chunk_fts where chunk_fts match ? ",
            ("稀有 OR 词缀",),
        ).fetchall()
        conn.execute("delete from chunk_fts where chunk_id='_probe'")
        conn.commit()
        print(f"  FTS5 + bm25(): 可用，探针命中 {len(rows)} 行")
    except Exception as exc:
        print(f"  !! FTS5 探测失败: {type(exc).__name__}: {exc}")
        ok = False

    vs = dbmod.vector_status()
    print(f"  向量扩展     : {vs}")
    if not vs.available:
        print("     → 已自动禁用向量通道，BM25 + 实体精确匹配不受影响")

    dbmod.close_db()
    wal = cfg.db_path.with_suffix(cfg.db_path.suffix + "-wal")
    print(f"  关闭后 WAL 残留: {'有' if wal.exists() and wal.stat().st_size > 0 else '无'}")

    print("\n" + "=" * 68)
    print("自检结果：" + ("全部通过" if ok else "存在问题，见上面 !! 标记"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
