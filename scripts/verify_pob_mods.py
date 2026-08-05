"""POEDB（P1，中文）与 PoB（P2，英文）词缀对账。

    python scripts/verify_pob_mods.py                 # 对账并打印报告
    python scripts/verify_pob_mods.py --write-conflicts   # 冲突落 source_conflict
    python scripts/verify_pob_mods.py --group IncreasedLife  # 只看某个组

为什么必须对账而不是直接采信一方：两个源都是对 GGG 数据的**二次加工**
（POEDB 抓页面、PoB 导 .dat），任一侧的抽取错误都会让所有下游答案错。
需求 15.5/15.6 要求冲突时并列展示并推荐 POEDB，而不是悄悄挑一个——
所以冲突要落库，不能只在日志里一闪而过。

对账主键是 `group`：两边都用 GGG 内部标识符，字面一致（实测 `IncreasedLife`）。
**模板不能当键**——中英文本归一后是两个不同字符串。

比较的是**数值区间**：纯数字，语言无关，是唯一可跨语言严格比对的字段。
词缀文本本身没法比（一个中文一个英文），所以不比。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.sources.poedb_mods import LINE_SEP  # noqa: E402

#: POEDB 的 `category` → PoB 的 `mod_kind`。只有同类才该被拿来比。
#: 拿精华词缀去比普通词缀会得到一堆假冲突。
CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    "normal": ("explicit",),
    "corrupted": ("corrupted",),
    "eldritch": ("eldritch",),
    "synthesis": ("synthesis",),
    "master": ("master",),
    "veiled": ("veiled",),
    "delve": ("delve",),
    "flask": ("flask",),
    "tincture": ("tincture",),
    "scourge": ("scourge",),
    "necropolis": ("necropolis",),
    "graft": ("graft",),
    "enchant": (
        "enchant_helmet", "enchant_gloves", "enchant_boots",
        "enchant_body", "enchant_belt", "enchant_weapon", "enchant_flask",
    ),
    "unique": ("item_exclusive",),
}


@dataclass
class Report:
    poedb_rows: int = 0
    comparable: int = 0          # 有 group 且类别可映射的 POEDB 行
    matched: int = 0             # 在 PoB 找到同组候选
    agree: int = 0               # 区间指纹在该组内找到完全一致者
    needs_review: int = 0        # 区间指纹在该组内找不到 —— 待核对，**不是**已确认冲突
    hybrid: int = 0              # 多行混合词缀，两源分组语义不同，不下结论
    no_group: int = 0
    no_counterpart: int = 0      # PoB 里没有这个组
    unmapped_category: Counter = field(default_factory=Counter)
    reviews: list[dict] = field(default_factory=list)
    pob_only_groups: set[str] = field(default_factory=set)
    poedb_only_groups: set[str] = field(default_factory=set)


def load_pob(conn, group_filter: str | None) -> dict[tuple[str, str], list[dict]]:  # noqa: ANN001
    """PoB 词缀按 (mod_kind, mod_group) 归组。"""
    sql = (
        "SELECT uid, mod_id, mod_kind, mod_group, ilvl, text_en, ranges, "
        "affix_type, affix_name, weights, spawn_tags, tags, trade_hashes, url "
        "FROM pob_mod WHERE mod_group IS NOT NULL AND mod_group <> ''"
    )
    args: tuple = ()
    if group_filter:
        sql += " AND mod_group = ?"
        args = (group_filter,)
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in conn.execute(sql, args):
        d = dict(r)
        d["ranges"] = json.loads(d["ranges"] or "[]")
        out[(d["mod_kind"], d["mod_group"])].append(d)
    return out


def norm_ranges(raw) -> tuple[tuple[float, float], ...]:
    """区间归一为可比的元组。整数与浮点混写（40 vs 40.0）不该算冲突。"""
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    return tuple(
        (round(float(a), 4), round(float(b), 4))
        for a, b in (raw or [])
        if isinstance(a, (int, float)) and isinstance(b, (int, float))
    )


def reconcile(conn, group_filter: str | None) -> Report:  # noqa: ANN001
    rep = Report()
    pob = load_pob(conn, group_filter)
    pob_groups = {g for _, g in pob}

    sql = (
        "SELECT uid, item_class_page, item_class_zh, category, correct_group, ilvl, "
        "tier, tier_count, weight, text_zh, ranges, name_zh, url "
        "FROM mod_affix"
    )
    args: tuple = ()
    if group_filter:
        sql += " WHERE correct_group = ?"
        args = (group_filter,)

    poedb_groups: set[str] = set()
    for r in conn.execute(sql, args):
        rep.poedb_rows += 1
        group = r["correct_group"]
        if not group:
            rep.no_group += 1
            continue
        poedb_groups.add(group)

        kinds = CATEGORY_MAP.get(r["category"])
        if not kinds:
            rep.unmapped_category[r["category"]] += 1
            continue
        rep.comparable += 1

        candidates: list[dict] = []
        for kind in kinds:
            candidates.extend(pob.get((kind, group), []))
        if not candidates:
            rep.no_counterpart += 1
            continue
        rep.matched += 1

        want = norm_ranges(r["ranges"])

        # ── 第一关：区间指纹。纯数字，语言无关。
        # 只要该组内有任一 PoB 档位的区间与之完全一致，就是两源在这个
        # 档位上取值相同。不强行按行配对——同组同 ilvl 常有多条不同词缀
        # （混合词缀、影响力变体），按位置配对会造一堆假冲突。
        if any(norm_ranges(c["ranges"]) == want for c in candidates):
            rep.agree += 1
            continue

        # ── 第二关：多行混合词缀。两源的分组语义不同：
        # POEDB 把「+X% 全抗 / 召唤生物 +Y% 抗性」整条归在 AllResistances，
        # PoB 则把召唤生物那部分单列 MinionElementalResistance。
        # 这是分类差异，不是数值分歧，**不能报成冲突**。
        if LINE_SEP.strip() in (r["text_zh"] or ""):
            rep.hybrid += 1
            continue

        # ── 第三关：记为**待核对**，不下「冲突」结论。
        #
        # 为什么不敢判冲突：POEDB 的 correct_group 取 family_list[0]，粒度比 PoB 的
        # group 粗，同名组里可能装着语义不同的词缀。实测 `ManaRegeneration` 组下
        # POEDB 是「每秒再生 (2.5-3) 魔力」（固定值），PoB 是「(19-21)% increased
        # Mana Regeneration Rate」（百分比）——两条压根不是同一个词缀，
        # 按 ilvl 配上去再喊「数值不一致」就是凭空造冲突。
        #
        # 而冲突是要按需求 15.5 展示给用户的。造假冲突比漏报冲突更糟：
        # 用户会看到两个"打架"的数值，而实际上两边都没错。
        # 所以这里只登记差异供人工核对，不进用户可见的冲突通道。
        rep.needs_review += 1
        pool_lvl = [c for c in candidates if c["ilvl"] == r["ilvl"]]
        near = pool_lvl or candidates
        rep.reviews.append({
            "group": group,
            # 带上 POEDB uid 前缀：同一 group+ilvl+底材类别下可能有多条
            # （混合词缀变体），不带就会撞 source_conflict 的唯一约束被去重吞掉。
            "subject": f"{group} ilvl={r['ilvl']} {r['item_class_page']} #{r['uid'][:8]}",
            "field": "ranges",
            "poedb_uid": r["uid"],
            "poedb_text": r["text_zh"],
            "poedb_ranges": want,
            "poedb_tier": f"T{r['tier']}/{r['tier_count']}",
            "poedb_url": r["url"],
            "pob_uid": near[0]["uid"],
            "pob_text": near[0]["text_en"],
            "pob_ranges": norm_ranges(near[0]["ranges"]),
            "pob_url": near[0]["url"],
            "candidates": len(candidates),
        })

    rep.pob_only_groups = pob_groups - poedb_groups
    rep.poedb_only_groups = poedb_groups - pob_groups
    return rep


def write_reviews(conn, rep: Report) -> int:  # noqa: ANN001
    """落库为 `kind='mod_review'`，**不是** `'mod'`。

    `'mod'` 是已确认冲突，会按需求 15.5 展示给用户；`'mod_review'` 只是
    「两源在此处对不上，待人工核对」。混用会把未证实的差异当成冲突推给用户。
    """
    rows = [
        (
            c["subject"], c["field"], "mod_review",
            "POEDB", f"{c['poedb_text']} {list(c['poedb_ranges'])}",
            c["poedb_uid"], c["poedb_url"],
            "Path of Building", f"{c['pob_text']} {list(c['pob_ranges'])}",
            c["pob_uid"], c["pob_url"],
            f"POEDB {c['poedb_tier']}；同组 PoB 候选 {c['candidates']} 个。"
            f"待核对：POEDB 的 correct_group 粒度较粗，可能并非同一词缀。"
            f"确认为真冲突后改 kind='mod'，届时按需求 15.6 推荐采用 POEDB",
        )
        for c in rep.reviews
    ]
    conn.executemany(
        """
        INSERT INTO source_conflict(
            subject, field, kind, source_a, value_a, ref_a, url_a,
            source_b, value_b, ref_b, url_b, note, first_seen, last_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
        ON CONFLICT(subject, field, source_a, source_b) DO UPDATE SET
            value_a=excluded.value_a, value_b=excluded.value_b,
            note=excluded.note, last_seen=datetime('now')
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default=None, help="只对账某个词缀组")
    ap.add_argument("--write-conflicts", action="store_true", help="冲突写入 source_conflict")
    ap.add_argument("--show", type=int, default=12, help="打印多少条冲突明细")
    args = ap.parse_args()

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING", 14)
    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()

    n_pob = conn.execute("SELECT count(*) FROM pob_mod").fetchone()[0]
    if not n_pob:
        print("pob_mod 是空的，先跑 scripts/import_pob_mods.py")
        return 2

    rep = reconcile(conn, args.group)

    print("=" * 78)
    print("POEDB（P1 中文） × PoB（P2 英文）词缀对账")
    print("=" * 78)
    print(f"  POEDB 行数        : {rep.poedb_rows:,}")
    print(f"  其中无 group      : {rep.no_group:,}（无法跨语言对账）")
    print(f"  类别可映射        : {rep.comparable:,}")
    print(f"  在 PoB 找到同组    : {rep.matched:,}")
    print(f"  PoB 无对应组      : {rep.no_counterpart:,}")
    if rep.matched:
        rate = rep.agree / rep.matched
        print(f"\n  区间指纹一致      : {rep.agree:,}（占已找到同组的 {rate:.1%}）"
              f"  ← 两源数值互相验证通过")
    print(f"  混合词缀不判定    : {rep.hybrid:,}（两源分组语义不同，非数值分歧）")
    print(f"  待人工核对        : {rep.needs_review:,}"
          f"  ← **不是**已确认冲突，见下方说明")

    if rep.unmapped_category:
        print("\n  类别未映射（这些 POEDB 类别在 PoB 侧没有对应表，未参与对账）：")
        for cat, n in rep.unmapped_category.most_common(12):
            print(f"    {cat:<22} {n:,}")

    print(f"\n  只在 PoB 的组     : {len(rep.pob_only_groups):,}"
          f"  例：{sorted(rep.pob_only_groups)[:4]}")
    print(f"  只在 POEDB 的组   : {len(rep.poedb_only_groups):,}"
          f"  例：{sorted(rep.poedb_only_groups)[:4]}")

    if rep.reviews:
        print("\n" + "=" * 78)
        print("待核对差异")
        print("=" * 78)
        print("  这些**不是**已确认的跨来源冲突。POEDB 的 correct_group 取")
        print("  family_list[0]，粒度比 PoB 的 group 粗，同名组里可能装着语义不同的")
        print("  词缀（实测 ManaRegeneration 组下 POEDB 是固定回魔、PoB 是回魔速率）。")
        print("  在没有可靠跨语言配对键之前，把它们报成冲突就是凭空造冲突——")
        print("  而冲突是要展示给用户的，造假冲突比漏报更糟。")

        by_group = Counter(c["group"] for c in rep.reviews)
        print(f"\n  按组聚合（共 {len(by_group)} 组），差异最多的前 12 组：")
        for g, n in by_group.most_common(12):
            print(f"    {g:<40} {n:>5} 行")

        print(f"\n  明细样例（前 {min(args.show, len(rep.reviews))} 条）：")
        for c in rep.reviews[:args.show]:
            print(f"\n  {c['subject']}  {c['poedb_tier']}")
            print(f"    POEDB: {c['poedb_text'][:64]}   区间 {list(c['poedb_ranges'])}")
            print(f"    PoB  : {c['pob_text'][:64]}   区间 {list(c['pob_ranges'])}")
            print(f"           （同组候选 {c['candidates']} 个）")

    if args.write_conflicts and rep.reviews:
        n = write_reviews(conn, rep)
        total = conn.execute(
            "SELECT count(*) FROM source_conflict WHERE kind='mod_review'"
        ).fetchone()[0]
        print(f"\n已写入 {n} 条待核对项，source_conflict 现有 {total} 条 mod_review")
        print("  （kind='mod_review' 不会作为已确认冲突展示给用户）")
    elif rep.reviews:
        print("\n（未落库；加 --write-conflicts 写入 source_conflict，kind=mod_review）")

    dbmod.close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
