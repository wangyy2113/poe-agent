#!/usr/bin/env python3
"""词缀检索验证：确认词缀梯度卡片可被自然语言命中，
且一万多张词缀卡不会淹没唯一装备的检索。

    python scripts/test_search_mods.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from retrieval.hybrid import retrieve  # noqa: E402

REFUSE = "REFUSE"


class Case(NamedTuple):
    query: str
    #: 'mod'=词缀卡 / 'unique'=唯一装备 / REFUSE
    expect: str
    #: **底材类别断言**：首条命中的标题里必须出现的串。
    #: 教训：早先只校验「是不是词缀卡」，结果查「弓」返回爪 / 匕首 / 短杖的火焰词缀
    #: 也算通过——那是答错了物品类别，属于「看起来回答了问题、实际说的是别的东西」，
    #: 正是无据不答要拦的形态。
    #:
    #: 只管**底材类别**。卡片标题 = `底材类别 · 词缀组`，来源已随粒度合并挪进卡内，
    #: 不再出现在标题里，故「来源」类断言一律走 `facet_has`。
    title_has: str | None
    #: **召回断言**：候选集里必须存在以此为前缀的 entity_id。
    #:
    #: 卡片粒度合并成 (底材类别, 词缀组) 之后，同组的各个来源在**同一张卡**里，
    #: 因此这里断言的是「该词缀组的卡有没有进候选集」。合并前需要断言
    #: `mod_ladder:Rings:normal:` 这种带来源的前缀，因为常规掉落池那张卡会被
    #: 同组其他来源的近似重复挤出候选集——那个问题已随粒度修正消失。
    must_recall: str | None
    note: str
    #: **facet 断言** `(facet 键, 必须包含的串)`：首条命中的该 facet 值须含此串。
    #:
    #: 为什么不复用 `title_has`：粒度合并后来源是**卡内维度**（`facets.mod_sources`
    #: 形如 `Base、精华、异度天灾 有利`），不在标题里。若继续拿标题去断言来源，
    #: 结果只有两种——要么永远失败，要么把断言删掉当没这回事；前者是假警报，
    #: 后者是真漏测。断言要跟着事实搬家，而不是就地放宽。
    facet_has: tuple[str, str] | None = None


CASES = [
    Case("弓上的火焰伤害前缀有哪些等级", "mod", "弓",
         "mod_ladder:Bows:FireDamage", "词缀梯度查询（须锁定弓）"),
    Case("胸甲最大生命词缀 T1 需要多少物品等级", "mod", "胸甲",
         "mod_ladder:Body_Armours_", "T 级+ilvl 查询（须锁定胸甲）"),
    Case("戒指的抗性词缀权重", "mod", "戒指",
         "mod_ladder:Rings:", "权重查询（须锁定戒指）"),
    Case("爪的攻击速度后缀有几级", "mod", "爪",
         "mod_ladder:Claws:IncreasedAttackSpeed",
         "单字类别名（jieba 会粘连，考验词表）"),
    # 来源类别查询。底材类别不设限（问题没指定部位），但命中的卡**必须真含精华这一节**——
    # 否则就是「答的是别的来源的词缀，只因文本里出现过精华二字」。
    #
    # 注意这条只验检索落点对不对，不代表这是回答该问题的正确路径：枚举类问题靠语义
    # 检索只能拿到候选集里的一部分来源，把不完整的枚举当完整的说出去就是错答案。
    # 正确出口是 `mod_sources` 工具（带 coverage_note 声明未采集渠道），见
    # scripts/test_tools.py。
    Case("精华可以给什么词缀", "mod", None, None, "来源类别查询",
         facet_has=("mod_sources", "精华")),
    Case("猎首", "unique", "猎首", "unique_item:Headhunter",
         "唯一装备专名——不应被词缀卡淹没"),
    Case("猎首这件装备有什么词缀", "unique", "猎首", "unique_item:Headhunter",
         "唯一装备+词缀字样，仍应命中装备"),
    Case("艾贝拉斯之角", "unique", "艾贝拉斯之角", "unique_item:Abberaths_Horn",
         "唯一装备专名"),
    # 曾被误记为「已知缺失项·必须拒答」——实际 POEDB 类别页有它，是我们采集面不全。
    # 详见 scripts/test_search.py 里同一处的说明。
    Case("无尽之衣", "unique", "无尽之衣", "unique_item:Tabula_Rasa",
         "L2 补集·索引页没有但类别页有"),
    Case("玄虚之靴有什么属性", REFUSE, None, None,
         "POEDB 无此名称，必须拒答（真名为神行靴）"),
    Case("辛迪加情报网怎么运作", REFUSE, None, None, "V1 未采集，必须拒答"),
    Case("今天上海天气怎么样", REFUSE, None, None, "非 POE 问题"),
]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    conn = dbmod.init_db(cfg.db_path)
    vector_requested = bool(cfg.get("retrieval.vector.enabled", False))
    vector_runtime = dbmod.vector_channel_status(vector_requested)

    n_mod = conn.execute(
        "SELECT count(*) FROM chunk WHERE entity_id LIKE 'mod_ladder:%'"
    ).fetchone()[0]
    n_uniq = conn.execute(
        "SELECT count(*) FROM chunk WHERE entity_id LIKE 'unique_item:%'"
    ).fetchone()[0]
    print("=" * 82)
    print(f"词缀检索验证（索引内 词缀梯度卡 {n_mod:,} 张 / 唯一装备 {n_uniq:,} 张）")
    print("=" * 82)

    passed = failed = leak = wrong_refuse = wrong_class = missed_recall = 0
    wrong_facet = 0
    for query, expect, want_in_title, must_recall, note, facet_has in CASES:
        r = retrieve(
            conn, query,
            top_k=int(cfg.get("retrieval.top_k", 20)),
            min_score=float(cfg.get("retrieval.min_score", 0.35)),
            min_coverage=float(cfg.get("retrieval.min_coverage", 0.5)),
            rrf_k=int(cfg.get("retrieval.rrf_k", 60)),
            priority_decay=float(cfg.get("retrieval.priority_decay", 0.08)),
            vector_enabled=vector_runtime.available,
            vector_disabled_reason=(
                vector_runtime.reason
                if vector_requested and not vector_runtime.available
                else ""
            ),
            rerank_enabled=bool(cfg.get("retrieval.rerank.enabled", False)),
        )
        top = r.hits[0] if r.hits else None
        kind = ""
        if top and top.entity_id:
            kind = "mod" if top.entity_id.startswith("mod_ladder:") else "unique"

        title = top.metadata.get("title", "") if top else ""
        recall_rank = next(
            (i for i, h in enumerate(r.hits, 1)
             if must_recall and (h.entity_id or "").startswith(must_recall)),
            None,
        )
        if expect == REFUSE:
            ok = not r.sufficient
            verdict = "正确拒答" if ok else f"**漏出** top={title or '-'}"
            leak += not ok
        else:
            facets = (top.metadata.get("facets") or {}) if top else {}
            facet_val = facets.get(facet_has[0], "") if facet_has else ""
            title_ok = (want_in_title is None) or (want_in_title in title)
            recall_ok = (must_recall is None) or (recall_rank is not None)
            facet_ok = (facet_has is None) or (facet_has[1] in facet_val)
            ok = r.sufficient and kind == expect and title_ok and recall_ok and facet_ok
            if not r.sufficient:
                verdict = f"**错误拒答**（应命中 {expect}）"
                wrong_refuse += 1
            elif kind != expect:
                verdict = f"命中 {kind}（期望 {expect}）"
            elif not title_ok:
                verdict = f"**类别错误** 首条为「{title}」，应含「{want_in_title}」"
                wrong_class += 1
            elif not recall_ok:
                verdict = f"**召回缺失** 候选集内无 {must_recall}*"
                missed_recall += 1
            elif not facet_ok:
                verdict = (f"**facet 错误** 首条「{title}」的 {facet_has[0]}="
                           f"「{facet_val or '-'}」，应含「{facet_has[1]}」")
                wrong_facet += 1
            else:
                verdict = f"命中 {kind}「{title}」"
                if facet_has:
                    verdict += f"；{facet_has[0]}=「{facet_val}」"
                if recall_rank:
                    verdict += f"；{must_recall}* 在第 {recall_rank} 位"

        passed += ok
        failed += not ok
        print(f"\n[{'PASS' if ok else 'FAIL'}] {note}")
        print(f"  查询   : {query}")
        print(f"  依据   : {r.sufficient}  覆盖率={r.top_coverage:.0%}  通道={r.channels}")
        print(f"  判定   : {verdict}")
        for h in r.hits[:3]:
            t = "词缀" if (h.entity_id or "").startswith("mod_ladder:") else "装备"
            print(f"    - [{t}/{h.channel}] {h.metadata.get('title', '?')[:52]}  "
                  f"cov={h.coverage:.0%}")

    print("\n" + "=" * 82)
    print(f"结果：{passed} 通过 / {failed} 失败")
    print(f"  无据漏出（必须 0）: {leak}    错误拒答: {wrong_refuse}    "
          f"类别错误: {wrong_class}    召回缺失: {missed_recall}    "
          f"facet 错误: {wrong_facet}")
    dbmod.close_db()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
