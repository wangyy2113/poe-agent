"""精华：POEDB（主源，已入库）× PoB `Data/Essence.lua`（对账口径）。

    .venv/bin/python scripts/verify_essence.py              # 只对账并打印
    .venv/bin/python scripts/verify_essence.py --write      # 回填 name_en + 落冲突

对账三项，都是纯数字或集合，语言无关：

    条数         两侧各 105
    tier         逐个相等
    装备类别数   两侧各 22

**为什么非要第二个来源**：`poedb_essence` 只有一个来源时，「105 条对不对」
只能靠页面自证（标签页标题里的 `/105`）。PoB 走的是完全独立的一条链
（导 GGG 的 `.dat`，不抓页面），两侧逐项相等才算把这一维钉住。

**这一次它真的抓到了东西**：`哀恸精华 / Essence of Desolation` 页面写
`Essence Tier: 0`、PoB 写 8，而它的四个同类两侧都写 8——POEDB 自己前后不一致。
只看一个来源发现不了。冲突落 `source_conflict(kind='essence_review')`，
**待人工核对，不作为跨来源冲突呈现**（与 `mod_review`/`gem_review` 同一档）。

`name_en` 由这里回填而不是采集时写：主源那一页只给中文，
而**绝不由 slug 反推英文名**——slug 只用来找到 PoB 条目。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.pipeline import reindex_essence_cards  # noqa: E402
from kb.sources.pob import PoBNotFound, load_repo  # noqa: E402
from kb.sources.pob_essence import load_pob_essences  # noqa: E402
from kb.sources.poedb_essence import CLASSES_PER_ESSENCE  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="精华 POEDB × PoB 对账")
    parser.add_argument("--pob-path", default=None)
    parser.add_argument("--write", action="store_true",
                        help="回填 name_en，并把分歧写进 source_conflict")
    args = parser.parse_args()
    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")

    try:
        repo = load_repo(args.pob_path, cfg)
    except PoBNotFound as exc:
        print(f"找不到 PoB 检出：{exc}")
        return 2
    pob = load_pob_essences(repo.root)

    conn = dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    rows = list(conn.execute(
        "SELECT e.metadata_id, e.name_zh, e.slug, e.tier, "
        "       (SELECT count(*) FROM poedb_essence_mod m WHERE m.metadata_id=e.metadata_id) "
        "  FROM poedb_essence e ORDER BY e.name_zh"
    ))
    if not rows:
        print("poedb_essence 是空的，先跑 `-m kb.pipeline --category essence`")
        return 2

    # 逐类别数要按 item_classes 里的顿号计数，分组行数不等于类别数
    class_counts = {
        mid: sum(len(cls.split("、")) for (cls,) in conn.execute(
            "SELECT item_classes FROM poedb_essence_mod WHERE metadata_id=?", (mid,)))
        for mid, *_ in rows
    }

    unbridged: list[str] = []
    tier_conflicts: list[tuple[str, str, int, int]] = []
    class_conflicts: list[tuple[str, int, int]] = []
    backfill: list[tuple[str, str]] = []
    via = Counter()

    for mid, name_zh, _slug, tier, _groups in rows:
        entry = pob.get(mid)
        if entry is None:
            unbridged.append(name_zh)
            continue
        via["元数据路径"] += 1
        backfill.append((entry.name_en, mid))
        if entry.tier != tier:
            tier_conflicts.append((name_zh, entry.name_en, tier, entry.tier))
        ours, theirs = class_counts[mid], len(entry.item_classes)
        if ours != theirs or ours != CLASSES_PER_ESSENCE:
            class_conflicts.append((name_zh, ours, theirs))

    print(f"PoB {repo.short_commit or '未知'}  ｜  POEDB {len(rows)} 条  PoB {len(pob)} 条")
    print(f"桥接        {len(rows) - len(unbridged)}/{len(rows)}  {dict(via)}"
          f"{'  桥不上：' + '、'.join(unbridged[:5]) if unbridged else ''}")
    print(f"tier 一致   {len(rows) - len(unbridged) - len(tier_conflicts)}"
          f"/{len(rows) - len(unbridged)}")
    for name_zh, name_en, ours, theirs in tier_conflicts:
        print(f"    ⚠ {name_zh}（{name_en}）POEDB {ours} ≠ PoB {theirs}")
    print(f"装备类别数  {len(rows) - len(unbridged) - len(class_conflicts)}"
          f"/{len(rows) - len(unbridged)} 两侧均为 {CLASSES_PER_ESSENCE}")
    for name_zh, ours, theirs in class_conflicts[:5]:
        print(f"    ⚠ {name_zh}  POEDB {ours} ≠ PoB {theirs}")

    if not args.write:
        print("\n（只对账，未写库；加 --write 回填 name_en 并登记分歧）")
        return 1 if (unbridged or tier_conflicts or class_conflicts) else 0

    conn.executemany("UPDATE poedb_essence SET name_en=? WHERE metadata_id=?", backfill)
    conn.execute("DELETE FROM source_conflict WHERE kind='essence_review'")
    conn.executemany(
        "INSERT INTO source_conflict("
        "  subject,field,kind,source_a,value_a,url_a,source_b,value_b,url_b,note,cause,"
        "  first_seen,last_seen"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        [
            (
                f"essence={name_zh}", "tier", "essence_review",
                "POEDB", str(ours), "https://poedb.tw/cn/Essence",
                "PoB", str(theirs),
                f"{repo.repo_url}/blob/{repo.commit}/src/Data/Essence.lua",
                f"{name_en}：同类的另外四件两侧都写 {theirs}，判为 POEDB 页面自身不一致",
                "tier_mismatch",
            )
            for name_zh, name_en, ours, theirs in tier_conflicts
        ],
    )
    # 回填了 name_en 就必须重渲染卡片，否则卡上仍只有中文名——
    # 「入库了但没有出口」是这个项目反复踩的坑，这里当场闭合
    chunks, aliases = reindex_essence_cards(conn)
    conn.commit()
    print(f"\n已回填 name_en {len(backfill)} 条，登记 essence_review {len(tier_conflicts)} 条，"
          f"重建卡片 {chunks} 张、别名 {aliases} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
