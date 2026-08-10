#!/usr/bin/env python3
"""覆盖层验证：自描述是否准确、缺据归因是否分得清「未采集」与「未召回」。

    python scripts/test_coverage.py

这套用例检验的是**诚实性**，不是功能：
  - 声明「已采集」的领域必须真有数据，且有权威 URL
  - 声明「未采集」的领域被问到时，必须归因为 not_collected 并指向权威来源
  - 已采集领域查不到时，必须归因为 not_recalled（是检索问题，不该派给采集）
  - 置信度数字必须与库里现算的一致（写死的比例迟早变成假话）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb import coverage as cov  # noqa: E402


class Case(NamedTuple):
    question: str
    #: 期望命中的领域 key。None 表示期望归因为 unknown（不属任何已声明领域）。
    expect_domain: str | None
    note: str


#: 归因用例。
#:
#: **期望的归因种类不写死，由覆盖层实测状态推导**：命中的领域可用即应为
#: not_recalled（是检索用词问题），不可用即应为 not_collected（该去采集）。
#:
#: 为什么这么写：早先每条用例都硬编码 `expect_kind`，于是「天赋树上哪些节点给
#: 药剂效果 → not_collected」在天赋树入库当天变成了假断言，测试报红但代码是对的。
#: 一旦把「哪些领域已采集」抄进测试，它就成了覆盖状态的第二个真源，必然与探测
#: 结果漂移——这正是 `kb/coverage.py` 用 inventory/status 分离要避免的问题，
#: 测试自己不能再犯一遍。
#:
#: 这里检验的不变量是**路由正确性**：问题该落到哪个领域、以及归因种类是否与该
#: 领域的真实可用性一致。至于某个领域该不该已采集，那是覆盖清单的事，不是这里。
CASES = [
    Case("天赋树上哪些节点给药剂效果", "passive_tree", "天赋树类问题"),
    Case("星团珠宝能开出哪些大点", "cluster_jewel", "星团珠宝天赋定义"),
    Case("暴击辅助宝石有哪些", "support_gem", "辅助宝石"),
    Case("猎首现在多少钱", "economy_price", "经济源"),
    Case("稀有装备最多能有几个前缀", "craft_rule", "制作规则常量——可行性判定的前置"),
    Case("守望者升华节点有什么", "ascendancy", "升华"),
    Case("某个我编造出来的词缀叫什么名字", "mod_affix", "词缀"),
    Case("某件不存在的唯一装备属性", "unique_item", "唯一装备"),
    Case("今天上海天气怎么样", None, "完全不属任何领域"),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    conn = dbmod.init_db(cfg.db_path)

    problems: list[str] = []
    rows = cov.load(conn)

    print("=" * 82)
    print(f"覆盖层验证（{len(rows)} 个已声明领域）")
    print("=" * 82)

    # ── 1. 自描述准确性 ──
    print("\n[1] 自描述准确性")
    usable = [s for s in rows if s.usable]
    print(f"    可用领域 {len(usable)} / 共 {len(rows)}")
    for s in rows:
        if not s.spec.authority_url:
            problems.append(f"{s.key} 缺权威来源 URL（未采集时无法指路）")
        if s.usable and s.item_count <= 0:
            problems.append(f"{s.key} 声明可用但条目数为 0")
        if not s.usable and s.item_count > 0:
            problems.append(f"{s.key} 声明不可用但库里有 {s.item_count} 条")
        if s.status == "partial" and not s.spec.partial_note:
            problems.append(f"{s.key} 是部分采集但没说明缺什么")
        if not s.spec.hints and not s.usable:
            problems.append(f"{s.key} 未采集且无意图识别词——被问到时无法归因")
    print("    " + ("发现问题见末尾" if problems else "全部通过"))

    # ── 2. 置信度数字必须现算一致 ──
    print("\n[2] 置信度数字与库内现算是否一致")
    live = cov._confidence_notes(conn, "mod_affix")  # noqa: SLF001
    stored = next((s.confidence_notes for s in rows if s.key == "mod_affix"), [])
    if live != stored:
        problems.append("mod_affix 的置信度声明与实时探测不一致——覆盖声明已滞后，需刷新")
    print(f"    {len(stored)} 条声明，与实时探测{'一致' if live == stored else '**不一致**'}")
    for n in stored:
        print(f"      - {n}")

    # ── 3. 缺据归因 ──
    print("\n[3] 缺据归因（期望种类由覆盖层实测状态推导，不写死）")
    by_key = {s.key: s for s in rows}
    passed = failed = 0
    for q, expect_domain, note in CASES:
        # 期望种类 = 该领域此刻是否可用。领域不存在于清单里则期望 unknown。
        if expect_domain is None:
            expect_kind = "unknown"
            state = "不属任何领域"
        else:
            dom = by_key.get(expect_domain)
            if dom is None:
                problems.append(f"用例引用了未声明的领域 {expect_domain}")
                continue
            expect_kind = "not_recalled" if dom.usable else "not_collected"
            state = f"{expect_domain}={dom.status}"

        attr = cov.attribute_gap(conn, q)
        kind = attr.kind
        hit_keys = [d.key for d in attr.missing_domains + attr.covered_domains]
        ok = kind == expect_kind and (expect_domain is None or expect_domain in hit_keys)
        passed += ok
        failed += not ok
        print(f"\n    [{'PASS' if ok else 'FAIL'}] {note}")
        print(f"      问题 : {q}")
        print(f"      归因 : {kind}（按实测 {state} 应为 {expect_kind}）  命中域={hit_keys}")
        if not ok:
            problems.append(f"归因错误：{q} → {kind}，按实测 {state} 应为 {expect_kind}")
        if kind == "not_collected" and attr.missing_domains:
            print(f"      指路 : {attr.missing_domains[0].spec.authority_url}")

    # ── 4. 工具入口 ──
    print("\n[4] 已采集领域的精确查询入口")
    from tools.registry import REGISTRY  # noqa: PLC0415
    import tools.impl  # noqa: F401,PLC0415  触发注册

    registered = set(REGISTRY.names())
    for s in usable:
        missing_tools = [t for t in s.spec.tools if t not in registered]
        if missing_tools:
            problems.append(f"{s.key} 声明的工具未注册：{missing_tools}")
        mark = "、".join(s.spec.tools) if s.spec.tools else "（仅全文检索）"
        print(f"    {s.spec.display_zh[:28]:30s} → {mark}")

    # ── 5. category 参数是否说真话 ──
    print("\n[5] kb_search 的 category 枚举")
    cats = cov.usable_categories(conn)
    print(f"    实际有数据的分类：{cats}")
    if len(cats) == 6:
        problems.append("六个分类全部声明可用，与实测不符（应只有部分有数据）")

    print("\n" + "=" * 82)
    print(f"归因用例：{passed} 通过 / {failed} 失败")
    if problems:
        print(f"结论：**未通过**，{len(problems)} 个问题")
        for p in problems:
            print(f"  - {p}")
    else:
        print("结论：全部通过")
    dbmod.close_db()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
