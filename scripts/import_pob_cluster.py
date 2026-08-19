#!/usr/bin/env python3
"""离线导入星团珠宝的尺寸、小天赋类型与核心天赋清单。

**不发起网络请求**（PoB 是本地检出）；commit 未钉住时拒绝导入。
中文名从**已入库的天赋实体**桥接（判据见 `kb/sources/pob_cluster.py`）。

    python scripts/import_pob_cluster.py --dry-run   # 只清点与桥接率
    python scripts/import_pob_cluster.py             # 写库
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from entities.taxonomy import Source  # noqa: E402
from kb.sources.pob import PoBNotFound, load_repo  # noqa: E402
from kb.sources.pob_cluster import (  # noqa: E402
    CLUSTER_FILE,
    ClusterStructureError,
    as_json,
    bridge_chinese,
    extract_cluster_data,
)

#: 核心天赋条数下限。2026-08-19 实测 300 个 notable + 8 个基石。
MIN_NOTABLES = 250


def _passive_names(conn) -> dict[str, tuple[str, str]]:  # noqa: ANN001
    """已入库天赋卡的 ``英文名 -> (中文名, entity_id)``。

    同一个英文名可能对应多个实体（同名节点按内容分成多条）；取第一个即可——
    这里只用来取中文名，而同名同内容的节点中文名相同。
    """
    out: dict[str, tuple[str, str]] = {}
    for row in conn.execute(
        "SELECT name_en, name_zh, entity_id FROM entity "
        "WHERE entity_type='passive' AND name_en IS NOT NULL AND name_en <> ''"
    ):
        out.setdefault(str(row["name_en"]), (str(row["name_zh"]), str(row["entity_id"])))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pob-path", default=None)
    parser.add_argument("--dry-run", action="store_true", help="只解析与桥接，不写库")
    args = parser.parse_args()
    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")

    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()

    try:
        repo = load_repo(args.pob_path, cfg)
        if not repo.reproducible or len(repo.commit) != 40:
            raise ClusterStructureError("PoB commit 未钉住，拒绝导入不可复现引用")
        started = time.monotonic()
        data = extract_cluster_data(repo.root)
        elapsed_ms = int((time.monotonic() - started) * 1000)
    except (PoBNotFound, ClusterStructureError, OSError) as exc:
        print(f"[中止] {exc}")
        return 2

    notables = bridge_chinese(data.notables, _passive_names(conn))
    bridged = sum(1 for n in notables if n.name_zh)
    keystones = [n for n in notables if n.is_keystone]
    print(f"PoB：{repo.version_label} commit={repo.commit}")
    print(f"尺寸 {len(data.sizes)}：" + "、".join(
        f"{s.size} {s.min_nodes}–{s.max_nodes} 小点／{s.notable_slots} 核心位／"
        f"{s.socket_slots} 插槽" for s in data.sizes
    ))
    print(f"小天赋类型 {len(data.skills)} 条（按尺寸：" + "、".join(
        f"{s.size} {sum(1 for k in data.skills if k.size == s.size)}" for s in data.sizes
    ) + "，仅英文)")
    print(f"核心天赋 {len(notables)} 条（其中基石 {len(keystones)}），"
          f"中文名桥接 {bridged}/{len(notables)}（{bridged / len(notables):.1%}），解析 {elapsed_ms} ms")
    print("  基石：" + "、".join(f"{n.name_zh or n.name_en}" for n in keystones))
    unbridged = [n.name_en for n in notables if not n.name_zh]
    if unbridged:
        print(f"  未桥接（留空不硬翻）：{unbridged[:5]}{' 等' if len(unbridged) > 5 else ''}")
    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0

    if len(notables) < MIN_NOTABLES:
        print(f"[中止] 核心天赋只有 {len(notables)} 条，低于下限 {MIN_NOTABLES}")
        return 2

    priority = repo.priority(cfg)
    url = repo.blob_url(CLUSTER_FILE)
    run = conn.execute(
        "INSERT INTO ingest_run(source,category,started_at,total) "
        "VALUES (?,?,datetime('now'),?)",
        (Source.POB.value, "pob_cluster", len(notables)),
    )
    run_id = int(run.lastrowid)
    conn.execute("SAVEPOINT cluster_snapshot")
    try:
        conn.execute("DELETE FROM pob_cluster_jewel")
        conn.executemany(
            "INSERT INTO pob_cluster_jewel(size,min_nodes,max_nodes,notable_slots,"
            "socket_slots,source,source_priority,pob_url,source_commit,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
            [(s.size, s.min_nodes, s.max_nodes, s.notable_slots, s.socket_slots,
              Source.POB.value, priority, url, repo.commit) for s in data.sizes],
        )
        conn.execute("DELETE FROM pob_cluster_skill")
        conn.executemany(
            "INSERT INTO pob_cluster_skill(size,tag,name_en,stats_en,enchant_en) "
            "VALUES (?,?,?,?,?)",
            [(s.size, s.tag, s.name_en, as_json(s.stats_en), as_json(s.enchant_en))
             for s in data.skills],
        )
        conn.execute("DELETE FROM pob_cluster_notable")
        conn.executemany(
            "INSERT INTO pob_cluster_notable(name_en,name_zh,entity_id,is_keystone,"
            "sort_order,source,source_priority,pob_url,source_commit,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
            [(n.name_en, n.name_zh, n.entity_id, int(n.is_keystone), n.sort_order,
              Source.POB.value, priority, url, repo.commit) for n in notables],
        )
        conn.execute("RELEASE cluster_snapshot")
    except Exception:
        conn.execute("ROLLBACK TO cluster_snapshot")
        conn.execute("RELEASE cluster_snapshot")
        raise

    note = json.dumps(
        {
            "sizes": len(data.sizes),
            "skills": len(data.skills),
            "notables": len(notables),
            "keystones": len(keystones),
            "name_zh_bridged": bridged,
            "note": "哪些核心天赋出现在哪种小天赋类型上：本地无此映射，未采集",
        },
        ensure_ascii=False,
    )
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
        "elapsed_ms=?,note=? WHERE run_id=?",
        (len(notables), elapsed_ms, note, run_id),
    )
    conn.commit()
    print(f"已写入：尺寸 {len(data.sizes)}、小天赋类型 {len(data.skills)}、"
          f"核心天赋 {len(notables)}（run_id={run_id}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
