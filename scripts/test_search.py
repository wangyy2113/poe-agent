#!/usr/bin/env python3
"""检索验证：专名查询、描述性查询、数值类查询、以及知识库确定不覆盖的查询。

最后一类最关键——用来验证「无据不答」是否真的生效，而不是编一个答案。

    python scripts/test_search.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from retrieval.hybrid import retrieve  # noqa: E402

# (查询, 期望命中的实体 slug；None=只要求有命中；"REFUSE"=必须拒答, 说明)
REFUSE = "REFUSE"

CASES: list[tuple[str, str | None, str]] = [
    # ── 专名查询（中文）──
    ("猎首", "Headhunter", "专名·中文"),
    ("猎首这件装备有什么词缀", "Headhunter", "专名·带上下文提问"),
    ("冈姆的壮志", "Kaoms_Heart", "专名·中文"),
    # ── 专名查询（英文名与 slug 去下划线形式）──
    ("Headhunter", "Headhunter", "专名·英文名"),
    ("Abberaths Horn", "Abberaths_Horn", "专名·slug 去下划线（别名匹配）"),
    ("Abberath's Horn", "Abberaths_Horn", "专名·权威英文名（带撇号，slug 反推不出）"),
    ("Tabula Rasa", "Tabula_Rasa", "专名·英文名（L2 类别页补集来源）"),
    # ── 描述性查询：考察 search_keywords 对向量通道缺失的补偿 ──
    ("击败稀有怪物获得其属性修正的腰带", "Headhunter", "描述性·猎首效果"),
    ("法术伤害提高 最大魔力 法杖", None, "数值类·只要求有命中"),
    # ── L2 补全覆盖到的物品：曾被误记为「缺失」，现在必须命中 ──
    # 教训：这两条原先是「已知缺失·必须拒答」用例，前提是错的。
    #   · 无尽之衣(Tabula_Rasa) 确实不在 /cn/Unique_item 概览页上，但 /cn/Body_Armours
    #     类别页有它，详情页也存在 → 是我们的采集面不全，不是 POEDB 没有。
    #   · 「玄虚之靴」这个中文名 POEDB 根本没有；该装备真名是「神行靴」
    #     (Seven-League Step)，而且一直就在库里。当初连 slug 都猜错了
    #     （`Seven_League_Step` 实为 `Seven-League_Step`）。
    # 把猜测当成「已知缺口」写进用例，会让真实缺口被掩盖，也会把正常命中判成漏出。
    ("无尽之衣", "Tabula_Rasa", "L2 补集·索引页没有但类别页有"),
    ("神行靴", "Seven-League_Step", "专名·中文（曾被误记为缺失项）"),
    ("真知灼见", "Anatomical_Knowledge", "L2 补集·唯一珠宝（索引页对珠宝覆盖不全）"),
    # ── 知识库确定不覆盖：必须拒答，不得编造 ──
    ("玄虚之靴有什么属性", REFUSE, "POEDB 无此名称·必须拒答（真名为神行靴）"),
    ("流放之路2 的升华机制是什么", REFUSE, "越界·POE2"),
    ("辛迪加情报网怎么运作", REFUSE, "越界·制作系统（V1 只采了唯一装备）"),
    ("今天上海天气怎么样", REFUSE, "越界·非 POE 问题"),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    conn = dbmod.init_db(cfg.db_path)

    total = conn.execute("SELECT count(*) FROM chunk").fetchone()[0]
    print("=" * 76)
    print(f"检索验证（索引内 {total} 条切片）")
    print("=" * 76)

    min_score = float(cfg.get("retrieval.min_score", 0.35))
    min_coverage = float(cfg.get("retrieval.min_coverage", 0.5))
    top_k = int(cfg.get("retrieval.top_k", 20))
    print(f"  判据：查询词覆盖率 ≥ {min_coverage:.0%}（或实体精确命中）")
    passed = failed = 0
    wrong_refusals = leakages = 0

    for query, expect, note in CASES:
        r = retrieve(
            conn,
            query,
            top_k=top_k,
            min_score=min_score,
            min_coverage=min_coverage,
            rrf_k=int(cfg.get("retrieval.rrf_k", 60)),
            priority_decay=float(cfg.get("retrieval.priority_decay", 0.08)),
            vector_enabled=bool(cfg.get("retrieval.vector.enabled", False)),
            rerank_enabled=bool(cfg.get("retrieval.rerank.enabled", False)),
        )

        top = r.hits[0] if r.hits else None
        top_ent = (top.entity_id or "").split(":")[-1] if top else "-"

        if expect == REFUSE:
            ok = not r.sufficient
            verdict = "正确拒答" if ok else f"**漏出**：错误地给出依据（top={top_ent}）"
            leakages += not ok
        elif expect is None:
            ok = r.sufficient and bool(r.hits)
            verdict = f"有命中 top={top_ent}" if ok else "无命中（错误拒答）"
            wrong_refusals += not ok
        else:
            ok = r.sufficient and top_ent == expect
            if not r.sufficient:
                verdict = f"**错误拒答**（应命中 {expect}）"
                wrong_refusals += 1
            else:
                verdict = f"top={top_ent}" + ("" if ok else f"（期望 {expect}）")

        passed += ok
        failed += not ok
        mark = "PASS" if ok else "FAIL"
        print(f"\n[{mark}] {note}")
        print(f"  查询   : {query}")
        print(f"  命中   : {len(r.hits)} 条，通道={r.channels}，充分依据={r.sufficient}")
        print(f"  覆盖率 : {r.top_coverage:.0%}   理由：{r.reason}")
        print(f"  判定   : {verdict}")
        for h in r.hits[:3]:
            name = h.metadata.get("title", "?")
            print(f"    - [{h.channel}] {name}  cov={h.coverage:.0%} "
                  f"score={h.score:.3f} {h.source}(P{h.source_priority})")

    print("\n" + "=" * 76)
    print(f"结果：{passed} 通过 / {failed} 失败")
    print(f"  无据漏出（必须为 0）: {leakages}")
    print(f"  错误拒答            : {wrong_refusals}")
    print("  两项须成对监控：只压漏出会退化成什么都不答，只压拒答会让推断重新漏出。")
    dbmod.close_db()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
