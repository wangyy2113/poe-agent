#!/usr/bin/env python3
"""词缀抽取冒烟测试 + T 级算法验证。

    python scripts/test_mods.py                  # 默认测 Bows
    python scripts/test_mods.py --page Rings
    python scripts/test_mods.py --all            # 全部 101 个类别页
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.fetcher import Fetcher  # noqa: E402
from kb.sources import poedb_index, poedb_mods  # noqa: E402


def show_page(fetcher: Fetcher, slug: str, verbose: bool) -> tuple[int, int, int]:
    """返回 (词缀数, 失败标记, 无词缀数据标记)。"""
    url = f"{poedb_index.BASE}/{slug}"
    res = fetcher.fetch(url)
    if not res.ok:
        print(f"  {slug}: 抓取失败 HTTP {res.status_code}")
        return 0, 1, 0
    try:
        mods, stats = poedb_mods.extract_mods(res.text, slug, url)
    except poedb_mods.NoModsDataError as exc:
        print(f"  {slug:<32}     — 无常规词缀池（{exc}）")
        return 0, 0, 1
    except poedb_mods.ModStructureError as exc:
        print(f"  {slug}: **结构断言失败** —— {exc}")
        return 0, 1, 0

    if not verbose:
        conflict = f"  冲突={stats.dedup_conflicts}" if stats.dedup_conflicts else ""
        print(f"  {slug:<32} {stats.succeeded:>5} 条  "
              f"({stats.success_ratio:.0%})  组数={len(stats.categories)}{conflict}")
        return stats.succeeded, stats.failed + stats.dedup_conflicts, 0

    print("=" * 96)
    print(f"/cn/{slug}  —— 词缀抽取")
    print("=" * 96)
    print(f"  页面大小   : {len(res.text):,} 字符（{'缓存' if res.from_cache else '抓取'}）")
    print(f"  候选       : {stats.total}")
    print(f"  成功       : {stats.succeeded} ({stats.success_ratio:.1%} 非重复中)")
    print(f"  构建失败   : {stats.failed}")
    print(f"  主键重复   : {stats.deduped} ({stats.dedup_ratio:.1%})")
    print(f"  **去重冲突**: {stats.dedup_conflicts}  （必须为 0，否则说明主键太粗在丢数据）")
    if stats.failures:
        for ident, reason in stats.failures[:5]:
            print(f"    - 失败 {ident}: {reason}")
    if stats.conflict_samples:
        print("  冲突样本（实质字段不同却被合并）：")
        for cat, name, diff in stats.conflict_samples[:6]:
            print(f"    !! [{cat}] {name}: {diff}")
    if stats.dup_samples:
        print("  无损重复样本（内容完全一致）：")
        for cat, name, text in stats.dup_samples[:4]:
            print(f"    - [{cat}] {name} | {text}")

    print(f"\n  按来源组分布（{len(stats.categories)} 组）：")
    for cat, n in sorted(stats.categories.items(), key=lambda x: -x[1]):
        label = next((m.category_label for m in mods if m.category == cat), cat)
        print(f"    {cat:<24} {n:>5}  {label}")

    prefix = sum(1 for m in mods if m.gen_type == 1)
    suffix = sum(1 for m in mods if m.gen_type == 2)
    other = sum(1 for m in mods if m.gen_type == 0)
    print(f"\n  前缀 {prefix} / 后缀 {suffix} / 其他 {other}")
    src = Counter(m.tier_source for m in mods)
    print(f"  T 级来源：{dict(src)}")
    print(f"  有数值区间的：{sum(1 for m in mods if m.ranges)}/{len(mods)}")

    # T 级算法验证：挑一个词缀组，展示完整梯度
    print("\n" + "=" * 96)
    print("T 级算法验证：取 normal 组中级数最多的词缀组，检查 T 级与 ilvl 单调性")
    print("=" * 96)
    normal = [m for m in mods if m.category == "normal"]
    by_group: dict[tuple[int, str], list] = {}
    for m in normal:
        by_group.setdefault((m.gen_type, m.correct_group), []).append(m)
    if by_group:
        key, group = max(by_group.items(), key=lambda kv: len(kv[1]))
        group.sort(key=lambda m: m.tier)
        print(f"  词缀组: gen={key[0]}({group[0].gen_label})  CorrectGroup={key[1]}  "
              f"共 {len(group)} 条，级数 {group[0].tier_count}")
        for m in group:
            print(f"    T{m.tier:<2}/{m.tier_count:<2} ilvl={m.ilvl:<3} "
                  f"权重={m.weight:<5} ({m.weight_pct:>6.2f}%)  {m.name_zh:<10} {m.text_zh}")

        # 断言：T 级越小 ilvl 应越大（同 T 可同 ilvl）
        bad = []
        for a, b in zip(group, group[1:], strict=False):
            if a.tier < b.tier and a.ilvl < b.ilvl:
                bad.append((a, b))
        print(f"\n  单调性检查（T 越小 ilvl 应越大）：{'通过' if not bad else f'{len(bad)} 处异常'}")
        for a, b in bad[:3]:
            print(f"    T{a.tier} ilvl={a.ilvl} 却低于 T{b.tier} ilvl={b.ilvl}")

        # 同 Level 应共享同一 T
        lvl_to_tiers: dict[int, set[int]] = {}
        for m in group:
            lvl_to_tiers.setdefault(m.ilvl, set()).add(m.tier)
        conflict = {lv: ts for lv, ts in lvl_to_tiers.items() if len(ts) > 1}
        print(f"  同 ilvl 共享同一 T：{'通过' if not conflict else f'冲突 {conflict}'}")

    # 文本模板化样例
    print("\n" + "=" * 96)
    print("文本模板化样例（用于装备 Ctrl+C 文本反查）")
    print("=" * 96)
    for m in [x for x in normal if x.ranges][:8]:
        print(f"  T{m.tier:<2} {m.text_zh}")
        print(f"        → 模板 {m.text_template}   区间={m.ranges}")

    print("\n=== 互斥判断样例 ===")
    if len(normal) >= 2:
        same = [m for m in normal if m.correct_group == normal[0].correct_group]
        diff = [m for m in normal if m.correct_group != normal[0].correct_group]
        if len(same) >= 2:
            print(f"  同组（应互斥）: {same[0].name_zh} vs {same[1].name_zh} -> "
                  f"{poedb_mods.are_exclusive(same[0], same[1])}")
        if same and diff:
            print(f"  异组（应不互斥）: {same[0].name_zh} vs {diff[0].name_zh} -> "
                  f"{poedb_mods.are_exclusive(same[0], diff[0])}")

    # 与非 verbose 分支保持同一口径：`dedup_conflicts` 必须计入失败。
    # 它的含义是「主键太粗，实质不同的两条被合并」= 静默丢数据，
    # 上面刚打印「必须为 0」，却不让它影响退出码的话，这条断言就只是句装饰。
    return stats.succeeded, stats.failed + stats.dedup_conflicts, 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default="Bows")
    ap.add_argument("--all", action="store_true", help="遍历全部 101 个类别页")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )

    if args.all:
        slugs = poedb_index.discover_mod_class_pages(fetcher)
        print(f"遍历 {len(slugs)} 个类别页\n")
        total = failed_pages = no_data_pages = 0
        for slug in slugs:
            ok, bad, nodata = show_page(fetcher, slug, verbose=False)
            total += ok
            failed_pages += 1 if bad else 0
            no_data_pages += nodata
        print(f"\n{'=' * 76}")
        print(f"合计词缀记录 {total:,} 条")
        print(f"  有词缀的页面   : {len(slugs) - failed_pages - no_data_pages}/{len(slugs)}")
        print(f"  无常规词缀池   : {no_data_pages}（合法，如只有固定词缀的特殊珠宝）")
        print(f"  **结构失败**   : {failed_pages}（必须为 0）")
        return 1 if failed_pages else 0

    ok, bad, nodata = show_page(fetcher, args.page, verbose=True)
    return 0 if ok and not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
