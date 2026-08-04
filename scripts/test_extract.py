#!/usr/bin/env python3
"""抽取器冒烟测试：用真实 POEDB 页面验证抽取质量。

    python scripts/test_extract.py            # 用缓存（无缓存则抓取）
    python scripts/test_extract.py --force    # 强制重新抓取
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.fetcher import Fetcher  # noqa: E402
from kb.sources.poedb_unique import LIST_URL, extract_uniques  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="强制重新抓取")
    ap.add_argument("--show", type=int, default=5, help="展示前 N 条")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "INFO")

    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )

    print("=" * 72)
    print("POEDB 唯一装备抽取冒烟测试")
    print("=" * 72)

    res = fetcher.fetch(LIST_URL, force=args.force)
    print(f"  URL       : {res.url}")
    print(f"  来自缓存  : {res.from_cache}")
    print(f"  状态      : {res.status_code}")
    print(f"  大小      : {len(res.text):,} 字符")
    print(f"  内容哈希  : {res.content_hash[:16]}…")
    if not res.ok:
        print("  抓取失败，终止")
        return 1

    entities, stats = extract_uniques(res.text)

    print(f"\n  候选卡片  : {stats.total}")
    print(f"  抽取成功  : {stats.succeeded}  ({stats.success_ratio:.1%})")
    print(f"  抽取失败  : {stats.failed}")
    print(f"  零词缀    : {stats.zero_mod}  ({stats.zero_mod_ratio:.1%}，"
          f"碎片/契约/奇珍类合法无属性物品)")
    if stats.failures:
        print("\n  失败样本（前 10）：")
        for ident, reason in stats.failures[:10]:
            print(f"    - {ident}: {reason}")

    if not entities:
        print("\n  !! 未抽到任何实体，抽取器与页面结构不匹配")
        return 1

    # 分布统计
    from collections import Counter

    by_class = Counter(e.item_class for e in entities)
    print(f"\n  按类别    : {dict(by_class)}")
    mod_counts = [len(e.factual_mods) for e in entities]
    print(f"  词缀条数  : 最少 {min(mod_counts)}, 最多 {max(mod_counts)}, "
          f"平均 {sum(mod_counts) / len(mod_counts):.1f}")
    no_base = [e.name_zh for e in entities if not e.base_type]
    print(f"  无底材    : {len(no_base)} 条 {no_base[:5]}")
    no_req = sum(1 for e in entities if e.requirement is None)
    print(f"  无需求信息: {no_req} 条")
    with_range = sum(1 for e in entities for m in e.factual_mods if m.ranges)
    total_mods = sum(len(e.factual_mods) for e in entities)
    print(f"  含数值词缀: {with_range}/{total_mods}")

    print("\n" + "=" * 72)
    print(f"样本详情（前 {args.show} 条）")
    print("=" * 72)
    for e in entities[: args.show]:
        print(f"\n【{e.name_zh}】{e.base_type or '(无底材)'}   [{e.item_class}]")
        print(f"  entity_id : {e.entity_id}")
        print(f"  url       : {e.ref.url}")
        print(f"  name_en   : {e.name_en if e.name_en else '(未取，需详情页)'}")
        if e.requirement:
            print(f"  需求      : {e.requirement.raw}")
        for m in e.mods:
            tag = {"implicit": "隐式", "explicit": "显式", "description": "说明"}.get(m.kind.value, m.kind.value)
            rng = "  ranges=" + str([r.render() for r in m.ranges]) if m.ranges else ""
            print(f"    [{tag}] {m.text}{rng}")
        print(f"  keywords  : {' '.join(e.search_keywords[:14])}")

    # 定点验证：猎首必须在结果里，且词缀与实测一致
    print("\n" + "=" * 72)
    print("定点验证：猎首（Headhunter）")
    print("=" * 72)
    hh = [e for e in entities if e.slug == "Headhunter"]
    if not hh:
        print("  !! 未抽到猎首")
        return 1
    e = hh[0]
    print(f"  名称: {e.name_zh} / 底材: {e.base_type} / 类别: {e.item_class}")
    print(f"  需求: {e.requirement.raw if e.requirement else '-'}")
    for m in e.mods:
        print(f"    [{m.kind.value}] {m.text}")
    expect_fragments = ["最大生命", "力量", "敏捷", "稀有怪物"]
    joined = " ".join(m.text for m in e.mods)
    missing = [f for f in expect_fragments if f not in joined]
    print(f"  期望片段缺失: {missing or '无'}")

    return 0 if stats.succeeded > 0 and not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
