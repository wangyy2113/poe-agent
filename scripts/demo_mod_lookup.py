#!/usr/bin/env python3
"""演示装备词缀反查：给一行装备文本，查出 T 级、区间、词缀组与互斥项。

这是 /item 装备分析的核心能力预览。
    python scripts/demo_mod_lookup.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from entities.mod_lookup import exclusive_with, lookup_mod  # noqa: E402

# (装备文本, 底材类别页, 说明)
CASES = [
    ("+47 敏捷", "Bows", "唯一 T 级"),
    ("该装备的能量护盾提高 73%", "Boots_int", "跨来源歧义：掉落 vs 工艺台"),
    ("+45 最大生命", "Body_Armours_str", "常见词缀"),
    ("法术伤害提高 25%", "Wands", "百分比词缀"),
    ("+100% 火焰抗性", "Rings", "超出任何区间，应拒答"),
    ("这条词缀是我编的", "Rings", "不存在的词缀，应拒答"),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    conn = dbmod.init_db(cfg.db_path)

    print("=" * 88)
    print("装备词缀反查演示")
    print("=" * 88)

    for text, cls, note in CASES:
        print(f"\n【{note}】底材类别 = {cls}")
        print(f"  输入：{text}")
        m = lookup_mod(conn, text, cls)
        print(f"  召回 {len(m.candidates)} 个候选 → 校验通过 {len(m.viable)} 个")

        if not m.matched:
            print(f"  结论：**无法确定** —— {m.reason}")
            continue

        c = m.matched
        print(f"  结论：{m.describe()}")
        print(f"    词缀名   : {c.name_zh or '（无名）'}")
        print(f"    定义文本 : {c.text_zh}")
        print(f"    区间     : {c.ranges}")
        print(f"    词缀组   : {c.correct_group}（互斥判断用 {c.family_list}）")
        print(f"    标签     : {c.tags}")
        print(f"    权重占比 : {c.weight}（组内 {c.weight_pct}%）")
        print(f"    来源     : {c.url}")

        if m.ambiguous:
            print(f"    ⚠ 其余可能（{len(m.alternatives)} 项）：")
            for a in m.alternatives:
                print(f"       - {a.category_label} {a.tier_label} ilvl={a.ilvl} "
                      f"区间={a.ranges} 组={a.correct_group}")

        excl = exclusive_with(conn, c, cls, limit=6)
        if excl:
            print(f"    互斥词缀（同 family 交集，前 {len(excl)} 条）：")
            for e in excl:
                print(f"       - [{e.category_label}] {e.tier_label} {e.text_zh[:44]}")

    dbmod.close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
