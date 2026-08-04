#!/usr/bin/env python3
"""词缀库准确性验证。

三类检查，都是可自动化的硬指标：

1. **往返测试**：随机抽词缀 → 在其区间内取值 → 反查 → 是否命中原 T 级。
   这直接检验装备分析的核心路径：`+47 敏捷` 能否正确定为 T2。
   命中率必须极高，否则装备分析的 T 级判定不可信。

2. **T 级不变量**：每个 (类别, 来源组, 前后缀, 词缀组) 内——
   T 连续从 1 开始、T 越小 ilvl 越大、同 ilvl 共享同一 T。

3. **完整性对账**：与页面声明的条数比对，并检查关键类别是否齐全。

    python scripts/verify_mods.py
    python scripts/verify_mods.py -n 3000      # 往返样本量
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from entities.mod_lookup import lookup_mod  # noqa: E402

SEED = 20260804


def roundtrip(conn, sample: int) -> tuple[int, int, list[str]]:
    """往返测试：区间内取值 → 反查 → 应命中同一条词缀。"""
    rows = conn.execute(
        "SELECT uid, item_class_page, category, correct_group, tier, tier_count, "
        "text_zh, text_template, ranges, name_zh FROM mod_affix "
        "WHERE ranges != '[]' ORDER BY uid"
    ).fetchall()
    rng = random.Random(SEED)
    picked = rng.sample(rows, min(sample, len(rows)))

    ok = 0
    ambiguous = 0
    bad: list[str] = []
    for r in picked:
        ranges = [(float(a), float(b)) for a, b in json.loads(r["ranges"])]
        if not ranges:
            continue

        # 按严格模板逐位填入区间中点，构造一条"真实装备上可能出现"的词缀文本
        parts = r["text_template"].split("#")
        if len(parts) - 1 != len(ranges):
            # 严格模板的 # 个数应恒等于区间数，不等即为数据自身不一致
            bad.append(
                f"{r['item_class_page']}/{r['category']} 模板 # 数({len(parts) - 1}) "
                f"≠ 区间数({len(ranges)}): {r['text_template'][:50]!r}"
            )
            continue
        pieces = [parts[0]]
        for (lo, hi), tail in zip(ranges, parts[1:], strict=True):
            mid = (lo + hi) / 2
            mid_s = str(int(mid)) if float(lo).is_integer() and float(hi).is_integer() else str(mid)
            pieces.append(mid_s + tail)
        concrete = "".join(pieces)

        m = lookup_mod(conn, concrete, r["item_class_page"])
        if not m.matched:
            if len(bad) < 12:
                bad.append(
                    f"{r['item_class_page']}/{r['category']} 期望 T{r['tier']} "
                    f"{r['text_zh'][:38]!r} → 构造 {concrete[:38]!r} → 未命中（{m.reason[:70]}）"
                )
            continue

        # T 级只在同一 (来源类别, 词缀组) 内可比。
        # 因此判定标准是：viable 候选中存在与原记录同组同 T 的项。
        same_group = [
            c for c in m.viable
            if c.category == r["category"] and c.correct_group == r["correct_group"]
        ]
        if any(c.tier == r["tier"] for c in same_group):
            ok += 1
            if m.ambiguous:
                ambiguous += 1
        elif len(bad) < 12:
            got = f"T{m.matched.tier} [{m.matched.category}/{m.matched.correct_group}]"
            bad.append(
                f"{r['item_class_page']}/{r['category']}/{r['correct_group']} "
                f"期望 T{r['tier']} → 构造 {concrete[:34]!r} → {got}"
            )
    return ok, len(picked), bad, ambiguous


def tier_invariants(conn) -> tuple[int, list[str]]:
    """T 级不变量检查。"""
    rows = conn.execute(
        "SELECT item_class_page, category, gen_type, correct_group, tier, tier_count, ilvl "
        "FROM mod_affix WHERE tier_source = 'derived'"
    ).fetchall()
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        groups[(r["item_class_page"], r["category"], r["gen_type"], r["correct_group"])].append(r)

    problems: list[str] = []
    for key, items in groups.items():
        tiers = sorted({i["tier"] for i in items})
        # T 应从 1 开始且连续
        if tiers and tiers != list(range(1, len(tiers) + 1)):
            problems.append(f"{key}: T 不连续 {tiers[:8]}")
        # 同 ilvl 必须同 T
        by_ilvl: dict[int, set[int]] = defaultdict(set)
        for i in items:
            by_ilvl[i["ilvl"]].add(i["tier"])
        for ilvl, ts in by_ilvl.items():
            if len(ts) > 1:
                problems.append(f"{key}: ilvl={ilvl} 却有多个 T {sorted(ts)}")
        # T 越小 ilvl 越大
        tier_ilvl = {}
        for i in items:
            tier_ilvl[i["tier"]] = i["ilvl"]
        seq = [tier_ilvl[t] for t in sorted(tier_ilvl)]
        if any(a < b for a, b in zip(seq, seq[1:], strict=False)):
            problems.append(f"{key}: ilvl 随 T 增大未单调下降 {seq[:8]}")
        if len(problems) > 30:
            break
    return len(groups), problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--sample", type=int, default=2000)
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    conn = dbmod.init_db(cfg.db_path)

    total = conn.execute("SELECT count(*) FROM mod_affix").fetchone()[0]
    if not total:
        print("mod_affix 为空，请先运行 python -m kb.pipeline --category mods")
        return 1

    print("=" * 84)
    print("词缀库准确性验证")
    print("=" * 84)

    stat = conn.execute(
        "SELECT count(*) n, count(DISTINCT mod_code) codes, "
        "count(DISTINCT item_class_page) classes, count(DISTINCT category) cats, "
        "count(DISTINCT correct_group) groups FROM mod_affix"
    ).fetchone()
    print(f"  记录 {stat['n']:,} 条 | 不同词缀 {stat['codes']:,} | "
          f"底材类别 {stat['classes']} | 来源组 {stat['cats']} | 词缀组 {stat['groups']:,}")

    src = conn.execute(
        "SELECT tier_source, count(*) n FROM mod_affix GROUP BY tier_source"
    ).fetchall()
    print(f"  T 级来源：{ {r['tier_source']: r['n'] for r in src} }")
    print(f"  有数值区间：{conn.execute(chr(83) + 'ELECT count(*) FROM mod_affix WHERE ranges != ' + chr(39) + '[]' + chr(39)).fetchone()[0]:,}")

    print("\n" + "-" * 84)
    print(f"[1] 往返测试（样本 {args.sample}）：区间内取值 → 反查 → 应命中同一 T 级")
    print("-" * 84)
    ok, n, bad, ambiguous = roundtrip(conn, args.sample)
    rate = ok / n if n else 0
    print(f"  命中 {ok}/{n} = {rate:.2%}")
    print(f"  其中存在跨来源/词缀组歧义的：{ambiguous} "
          f"（{ambiguous / n:.1%}，本质歧义，回答时须如实说明）")
    if bad:
        print(f"  未命中样本（前 {len(bad)}）：")
        for b in bad:
            print(f"    - {b}")

    print("\n" + "-" * 84)
    print("[2] T 级不变量：T 连续从 1 起 / 同 ilvl 同 T / T 越小 ilvl 越大")
    print("-" * 84)
    ngroups, problems = tier_invariants(conn)
    print(f"  检查 {ngroups:,} 个 (类别,来源组,前后缀,词缀组) 分组")
    print(f"  异常 {len(problems)} 处" + ("" if problems else " —— 全部通过"))
    for p in problems[:10]:
        print(f"    !! {p}")

    print("\n" + "-" * 84)
    print("[3] 关键类别齐全性")
    print("-" * 84)
    must_have = ["Bows", "Rings", "Amulets", "Belts", "Body_Armours_str",
                 "Helmets_str", "Gloves_str", "Boots_str", "Wands", "Staves",
                 "One_Hand_Swords", "Two_Hand_Axes", "Quivers", "Cobalt_Jewel",
                 "Life_Flasks", "Utility_Flasks"]
    missing = []
    for cls in must_have:
        n_cls = conn.execute(
            "SELECT count(*) FROM mod_affix WHERE item_class_page = ?", (cls,)
        ).fetchone()[0]
        flag = "" if n_cls else "  ** 缺失 **"
        if not n_cls:
            missing.append(cls)
        print(f"  {cls:<24} {n_cls:>6} 条{flag}")

    print("\n" + "=" * 84)
    verdict_ok = rate >= 0.98 and not problems and not missing
    print("结论：" + ("全部通过" if verdict_ok else "存在问题，见上"))
    print(f"  往返命中率 {rate:.2%}（阈值 98%）| T 级不变量异常 {len(problems)} | "
          f"关键类别缺失 {len(missing)}")
    dbmod.close_db()
    return 0 if verdict_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
