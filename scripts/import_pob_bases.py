#!/usr/bin/env python3
"""离线导入 PoB 胸甲底材注册表。

读取本地 PoB ``Data/Bases/body.lua`` 与已经缓存的 POEDB 中英文
``Body_Armours`` 页面。默认不发起网络请求；任一来源缺失、PoB commit 未钉住或
中英文桥接不唯一时整批拒绝。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from entities.taxonomy import Source  # noqa: E402
from kb.fetcher import cache_path_for  # noqa: E402
from kb.sources.pob import PoBNotFound, PoBRepo, load_repo  # noqa: E402
from kb.sources.pob_bases import (  # noqa: E402
    BODY_BASE_FILE,
    POEDB_BODY_CN_URL,
    POEDB_BODY_EN_URL,
    BaseStructureError,
    BodyArmourBase,
    extract_body_armour_bases,
)

MIN_BODY_BASES = 100
MAX_RELATIVE_DROP = 0.20


def to_row(base: BodyArmourBase, repo: PoBRepo, priority: int) -> tuple:
    return (
        base.base_id,
        base.name_en,
        base.name_zh,
        base.poedb_slug,
        base.item_class,
        base.subtype,
        base.socket_limit,
        json.dumps(base.tags, ensure_ascii=False),
        json.dumps(base.influence_tags, ensure_ascii=False, sort_keys=True),
        base.armour_tag,
        base.modifier_page,
        base.implicit_text,
        json.dumps(base.implicit_mod_types, ensure_ascii=False),
        json.dumps(base.implicit_ids, ensure_ascii=False),
        json.dumps(base.defences, ensure_ascii=False, sort_keys=True),
        json.dumps(base.requirements, ensure_ascii=False, sort_keys=True),
        base.source_file,
        Source.POB.value,
        priority,
        repo.blob_url(BODY_BASE_FILE),
        base.poedb_url,
        repo.commit,
        repo.version_label,
        repo.game_version,
        base.content_hash,
    )


def validate_snapshot_size(
    conn, incoming: int, *, allow_shrink: bool = False  # noqa: ANN001
) -> None:
    existing = int(conn.execute("SELECT count(*) FROM pob_base_item").fetchone()[0])
    reason = ""
    if incoming < MIN_BODY_BASES:
        reason = f"低于绝对下限 {MIN_BODY_BASES}（incoming={incoming}）"
    elif existing and incoming < existing * (1 - MAX_RELATIVE_DROP):
        reason = f"相对缩减超过 {MAX_RELATIVE_DROP:.0%}（{existing} -> {incoming}）"
    if reason and not allow_shrink:
        raise BaseStructureError(f"拒绝替换胸甲底材快照：{reason}")


def replace_snapshot(
    conn, bases: list[BodyArmourBase], repo: PoBRepo, priority: int  # noqa: ANN001
) -> int:
    rows = [to_row(base, repo, priority) for base in bases]
    conn.execute("DELETE FROM pob_base_item")
    conn.executemany(
        """
        INSERT INTO pob_base_item(
            base_id,name_en,name_zh,poedb_slug,item_class,subtype,socket_limit,
            tags,influence_tags,armour_tag,modifier_page,implicit_text,
            implicit_mod_types,implicit_ids,defences,requirements,source_file,
            source,source_priority,pob_url,poedb_url,source_commit,pob_version,
            game_version,content_hash,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        rows,
    )
    return len(rows)


def _read_cache(url: str) -> str:
    cfg = get_config()
    path = cache_path_for(cfg.cache_dir, url)
    if not path.is_file():
        raise BaseStructureError(f"缺少本地缓存 {url}：{path}")
    return path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pob-path", default=None)
    parser.add_argument("--dry-run", action="store_true", help="只解析和体检，不写库")
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="显式允许低于绝对下限或相对大幅缩减（仅人工确认上游变化后使用）",
    )
    args = parser.parse_args()
    cfg = get_config()
    try:
        repo = load_repo(args.pob_path, cfg)
        if not repo.reproducible or len(repo.commit) != 40:
            raise BaseStructureError("PoB commit 未钉住，拒绝导入不可复现引用")
        cn_html = _read_cache(POEDB_BODY_CN_URL)
        en_html = _read_cache(POEDB_BODY_EN_URL)
        started = time.monotonic()
        bases = extract_body_armour_bases(repo.root, cn_html, en_html)
        elapsed_ms = int((time.monotonic() - started) * 1000)
    except (PoBNotFound, BaseStructureError, OSError) as exc:
        print(f"[中止] {exc}")
        return 2

    by_tag = Counter(base.armour_tag for base in bases)
    print(f"PoB：{repo.version_label} commit={repo.commit}")
    print(f"胸甲底材：{len(bases)} 条，解析 {elapsed_ms} ms")
    print(f"属性标签：{dict(sorted(by_tag.items()))}")
    print(f"样例：{bases[0].name_zh} / {bases[0].name_en} -> {bases[0].modifier_page}")
    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0

    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()
    try:
        validate_snapshot_size(conn, len(bases), allow_shrink=args.allow_shrink)
        run = conn.execute(
            "INSERT INTO ingest_run(source,category,started_at,total) "
            "VALUES (?,?,datetime('now'),?)",
            (Source.POB.value, "pob_base_item", len(bases)),
        )
        run_id = int(run.lastrowid)
        conn.execute("SAVEPOINT pob_base_snapshot")
        try:
            written = replace_snapshot(conn, bases, repo, repo.priority(cfg))
            conn.execute("RELEASE pob_base_snapshot")
        except Exception:
            conn.execute("ROLLBACK TO pob_base_snapshot")
            conn.execute("RELEASE pob_base_snapshot")
            raise
        conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
            "elapsed_ms=?,note=? WHERE run_id=?",
            (
                written,
                elapsed_ms,
                json.dumps(
                    {
                        "pob_version": repo.version_label,
                        "game_version": repo.game_version,
                        "commit": repo.commit,
                        "by_armour_tag": dict(sorted(by_tag.items())),
                        "poedb_cn_url": POEDB_BODY_CN_URL,
                        "poedb_en_url": POEDB_BODY_EN_URL,
                    },
                    ensure_ascii=False,
                ),
                run_id,
            ),
        )
        conn.commit()
        print(f"已写入 {written} 条 pob_base_item（run_id={run_id}）")
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        print(f"[中止] 快照未改变：{exc}")
        return 2
    finally:
        dbmod.close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
