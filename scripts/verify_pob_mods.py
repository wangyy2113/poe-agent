"""POEDB（P1，中文）与 PoB（P2，英文）词缀对账。

    python scripts/verify_pob_mods.py                 # 对账并打印报告
    python scripts/verify_pob_mods.py --write-conflicts   # 冲突落 source_conflict
    python scripts/verify_pob_mods.py --group IncreasedLife  # 只看某个组

为什么必须对账而不是直接采信一方：两个源都是对 GGG 数据的**二次加工**
（POEDB 抓页面、PoB 导 .dat），任一侧的抽取错误都会让所有下游答案错。
需求 15.5/15.6 要求冲突时并列展示并推荐 POEDB，而不是悄悄挑一个——
所以冲突要落库，不能只在日志里一闪而过。

对账**不能**按 `group` 配对。曾经这么做，理由是「两边都用 GGG 内部标识符，字面一致
（实测 IncreasedLife）」——那句话只在个别组上成立。`parsers/mod_bridge.py` 早就实测过：
`mod_affix` 的 1578 个组里只有 856 个在 `pob_mod` 有同名组，掉落词缀更低（930 个里 107 个，
11.5%），局部武器词缀是**系统性**命名不同（POEDB `PhysicalDamage` ↔ PoB `LocalPhysicalDamage`）。

按组名配对的代价被量出来了：2729 条 `mod_review` 里 **89.2% 在 PoB 侧根本没有对应阶梯**——
那些不是数值冲突，是把不相关的词缀拉到一起比。所以配对判据改成**阶梯指纹**
（整条档位阶梯的 (ilvl, 区间) 集合），与桥接层共用 `parsers.mod_bridge` 的同一份原语。

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
from parsers.mod_bridge import (  # noqa: E402
    ladder_overlap,
    ladder_slots,
    parse_ranges_json,
)

#: 阶梯部分重合的最低门槛。低于它就认为两条阶梯不是同一条词缀，
#: 差异只说明「PoB 没有这条阶梯」而不是数值冲突，所以不进待核对清单。
#: 0.5 的依据：实测 2729 条里 89.2% 的重合度为 0（毫无共同档位），
#: 9.5% 落在 ≥0.5，中间几乎没有分布——这个阈值不在分布的密集区，改动它不敏感。
LADDER_MIN_OVERLAP = 0.5

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
    no_ladder: int = 0           # 同名组存在，但 PoB 侧没有与之重合的阶梯 → 不是冲突
    ladder_identical: int = 0    # 阶梯逐档全等：两源同一条词缀，且逐档一致
    ladder_ambiguous: int = 0    # 多个 PoB 组共享同一条阶梯 → 对手不唯一，不敢判一致
    ladder_ambiguous_agreed: int = 0  # 身份不唯一，但每个候选身份下数值都一致 → 与身份无关地判一致
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


def pob_ladder_index(pob: dict[tuple[str, str], list[dict]]) -> dict[str, dict[str, set[str]]]:
    """PoB 侧阶梯指纹：`mod_kind → mod_group → {档位键}`。

    PoB 的一个组是所有物品类型的并集（同一条词缀在弓和法杖上是同一组），
    所以这里不按物品类型再切——切了指纹就永远对不上 POEDB 的单类别阶梯。
    """
    out: dict[str, dict[str, set[str]]] = defaultdict(dict)
    for (kind, group), rows in pob.items():
        out[kind][group] = ladder_slots(
            [(int(r["ilvl"] or 0), [tuple(x) for x in (r["ranges"] or [])]) for r in rows]
        )
    return out


def poedb_ladder_index(conn, group_filter: str | None) -> dict[tuple[str, str, str], set[str]]:  # noqa: ANN001
    """POEDB 侧阶梯指纹：`(category, correct_group, item_class_page) → {档位键}`。

    必须带 `item_class_page`：同一词缀组在不同底材上是**不同的阶梯**
    （弓上 T5、法杖上 T4），合起来算指纹就没有一条能对上 PoB。
    """
    sql = (
        "SELECT category, correct_group, item_class_page, ilvl, ranges FROM mod_affix "
        "WHERE correct_group IS NOT NULL AND correct_group <> ''"
    )
    args: tuple = ()
    if group_filter:
        sql += " AND correct_group = ?"
        args = (group_filter,)
    rows: dict[tuple[str, str, str], list[tuple[int, list[tuple[float, float]]]]] = defaultdict(list)
    for r in conn.execute(sql, args):
        d = dict(r)
        rows[(d["category"], d["correct_group"], d["item_class_page"])].append(
            (int(d["ilvl"] or 0), parse_ranges_json(d["ranges"]))
        )
    return {key: ladder_slots(value) for key, value in rows.items()}


def reconcile(conn, group_filter: str | None) -> Report:  # noqa: ANN001
    rep = Report()
    pob = load_pob(conn, group_filter)
    pob_groups = {g for _, g in pob}
    # 阶梯指纹索引。**不受 group_filter 影响地建 PoB 侧**是有意的：配对判据就是
    # 「阶梯长得一样」，而对上的那个 PoB 组常常**不同名**（LifeLeech ↔
    # LifeLeechPermyriad），按组名过滤会把正确的对手先筛掉。
    # `pob_all` 是**未按组名过滤**的那份，阶梯索引与「不同名对手」的候选都从它取。
    pob_all = load_pob(conn, None) if group_filter else pob
    pob_ladders = pob_ladder_index(pob_all)
    poedb_ladders = poedb_ladder_index(conn, group_filter)

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

        # ── 第三关：先判**两条阶梯是不是同一条词缀**，再谈数值分歧。
        #
        # 这一关以前直接登记「待核对」，用的 PoB 侧是 `near[0]`——同组里任取一条。
        # 后果被量了出来：2729 条待核对里 89.2% 在 PoB 侧连一个共同档位都没有，
        # 也就是说被拿来比的两条词缀压根不是同一条（POEDB 的 correct_group 取
        # family_list[0]，粒度比 PoB 的 group 粗）。人要逐行看 2729 次，
        # 而九成是配对错误——这清单实际上没法用。
        #
        # 判据换成阶梯指纹（与 `parsers.mod_bridge` 共用同一份原语）：
        #   逐档全等         → 两源同一条词缀且数值一致，不是分歧
        #   重合 ≥ 阈值      → 同一条词缀但某些档位对不上 → **这才值得人工核对**
        #   重合 < 阈值      → PoB 没有这条阶梯，是覆盖缺口而不是数值冲突，不入清单
        poedb_ladder = poedb_ladders.get((r["category"], group, r["item_class_page"]))
        scored: list[tuple[float, str]] = []
        for kind in kinds:
            for cand_group, slots in pob_ladders.get(kind, {}).items():
                score = ladder_overlap(poedb_ladder or set(), slots)
                if score > 0:
                    scored.append((score, cand_group))
        best_score = max((s for s, _ in scored), default=0.0)
        best_groups = sorted(g for s, g in scored if s == best_score)
        # 同名组在并列里就优先它——最不意外的对齐
        best_group = group if group in best_groups else (best_groups[0] if best_groups else "")

        if best_score >= LADDER_MIN_OVERLAP and best_group != group and len(best_groups) == 1:
            # **一致性检查也要在阶梯配对出来的那个组里做**，不是只在同名组里做。
            #
            # 第一关只查同名组，而两源组名系统性不同（POEDB `LifeLeech` ↔ PoB
            # `LifeLeechPermyriad`、POEDB 弓上的 `PhysicalDamage` ↔ PoB
            # `LocalPhysicalDamage`），所以真正的对手常常不在同名组里。
            #
            # 阈值用 `LADDER_MIN_OVERLAP` 而不是「逐档全等」：实测 POEDB 弓上
            # 「附加 (13-17) - (28-32) 基础物理伤害」与 PoB「Adds (13-17) to (28-32)
            # Physical Damage」**区间与词缀名（砥砺的/Honed）都对得上**，只是两条阶梯的
            # 档位集合不完全相同（各自多几档），所以卡在 <1.0。只要该行的区间在对手组里
            # 出现，这个档位上两源就是一致的——阶梯是否逐档全等是**组**层面的事，
            # 不该拦住**行**层面的一致判定。
            #
            # **唯一性是必需条件。** 阶梯形状并不唯一：`+(15-20) to Strength` 与
            # `+(15-20) to Dexterity` 的 (ilvl, 区间) 阶梯完全一样。并列时任取一个
            # 就可能拿 Strength 的数值去「验证」Dexterity，得到一个**假一致**——
            # 那比假冲突糟得多，因为它把真分歧悄悄盖掉了。
            # `parsers/mod_bridge.py` 早就写下同一条结论：撞车时不选，返回歧义。
            aligned_group = [
                c for kind in kinds for c in pob_all.get((kind, best_group), [])
            ]
            if any(norm_ranges(c["ranges"]) == want for c in aligned_group):
                rep.agree += 1
                rep.ladder_identical += 1
                continue

        if best_score >= LADDER_MIN_OVERLAP and len(best_groups) > 1:
            # 多个 PoB 组共享同一条阶梯 → 对手身份不唯一。
            #
            # 但**身份不唯一不等于数值不可判**：若该行的区间在**每一个**并列组里都出现，
            # 那么无论正确对手是哪一个，两源在这个档位上的取值都一致 —— 与身份无关地成立。
            # 实测这一支占歧义行的 66%：POEDB 弓上的火焰/冰霜/闪电附加伤害阶梯完全相同
            # （GGG 给三元素同一组数值），词缀名也同为「升级的/Upgraded」，
            # 连词缀名锚点都分不开；但三个组里那一档的区间是同一个，所以数值没有分歧。
            #
            # 反之，只要有**一个**并列身份下数值对不上，就仍然要人看——那种情况下
            # 「判一致」等于挑了一个看着顺眼的身份，正是 mod_bridge 禁止的做法。
            if all(
                any(
                    norm_ranges(c["ranges"]) == want
                    for kind in kinds
                    for c in pob_all.get((kind, tied), [])
                )
                for tied in best_groups
            ):
                rep.agree += 1
                rep.ladder_ambiguous_agreed += 1
                continue
            rep.ladder_ambiguous += 1
        elif best_score >= 1.0:
            # 逐档全等、组名也相同，却没在第一关命中：同一条词缀内部的抽取问题，
            # 仍要人看，但归因不同。
            rep.ladder_identical += 1
        elif best_score < LADDER_MIN_OVERLAP:
            rep.no_ladder += 1
            continue

        rep.needs_review += 1
        # PoB 侧展示谁，决定了这张清单能不能看。取法：**最佳重合组** × **同 ilvl**。
        #
        # 两次都踩在这里：第一版用 `near[0]`（同名组里任取一条），人看到的「冲突」多半是
        # 拿 T3 比 T1 的产物；第二版改成按 ilvl 对齐，但候选池仍限在**同名组**里——
        # 实测 POEDB 弓·命中值 与 PoB 同名组 `IncreasedAccuracy` 的阶梯重合度是 **0.000**，
        # 最佳重合的是另一个组。于是展示出来的仍然是不相干的那条。
        #
        # 所以候选池要取自 `best_group`（可能与同名组不同），再按 ilvl 对齐。
        pool = (
            [c for kind in kinds for c in pob_all.get((kind, best_group), [])]
            if best_group and best_group != group else candidates
        ) or candidates
        aligned = [c for c in pool if c["ilvl"] == r["ilvl"]]
        near = aligned or pool
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
            # cause 必须与上面的计数分支同一套判据。改了分支忘了改这里，
            # 结果就是 `rep.ladder_ambiguous` 加了 1 而落库的 cause 写成 partial——
            # 「按成因分批核对」立刻失真（单测抓到过一次）。
            "cause": (
                "ladder_ambiguous" if len(best_groups) > 1
                else "ladder_identical" if best_score >= 1.0
                else "ladder_partial"
            ),
            "ladder_overlap": round(best_score, 3),
            "pob_ladder_group": best_group,
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
            f"POEDB {c['poedb_tier']}；阶梯重合度 {c['ladder_overlap']}"
            f"（PoB 组 {c['pob_ladder_group'] or '?'}，同组候选 {c['candidates']} 个）。"
            f"待核对：两源阶梯大部分重合但该档对不上。"
            f"确认为真冲突后改 kind='mod'，届时按需求 15.6 推荐采用 POEDB",
            c.get("cause"),
        )
        for c in rep.reviews
    ]
    # 先清掉旧判据产出的 mod_review 行。**必须清**：判据从「按组名配对」换成
    # 「按阶梯指纹配对」之后，旧行里有 817 条根本不该存在（PoB 侧没有那条阶梯）、
    # 831 条其实是一致的（正确对手在不同名的组里）。留着它们，`GROUP BY cause`
    # 就会把两套判据的产物混在一起，人工核对又回到无从下手的状态。
    #
    # 只删 `mod_review`：`unique_review` / `tree_review` 是别的对账脚本写的。
    conn.execute("DELETE FROM source_conflict WHERE kind = 'mod_review'")
    conn.executemany(
        """
        INSERT INTO source_conflict(
            subject, field, kind, source_a, value_a, ref_a, url_a,
            source_b, value_b, ref_b, url_b, note, cause, first_seen, last_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))
        ON CONFLICT(subject, field, source_a, source_b) DO UPDATE SET
            value_a=excluded.value_a, value_b=excluded.value_b,
            note=excluded.note, cause=excluded.cause, last_seen=datetime('now')
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
    print(f"  阶梯无重合不判定  : {rep.no_ladder:,}（PoB 侧没有这条阶梯 → 覆盖缺口，非数值分歧）")
    print(f"  身份歧义但数值一致: {rep.ladder_ambiguous_agreed:,}"
          f"（多个 PoB 组共享该阶梯，但每个候选身份下取值都相同）")
    print(f"  待人工核对        : {rep.needs_review:,}"
          f"  ← **不是**已确认冲突，见下方说明")
    if rep.reviews:
        by_cause = Counter(c.get("cause") or "?" for c in rep.reviews)
        for cause, n in by_cause.most_common():
            hint = {
                "ladder_partial": "同一条词缀但部分档位对不上 —— 最值得先看",
                "ladder_identical": "阶梯逐档全等却有单行对不上 —— 同一词缀内部的抽取问题",
                "ladder_ambiguous": "多个 PoB 组共享同一条阶梯 —— 对手不唯一，不敢判一致",
            }.get(cause, "")
            print(f"      {cause:<18} {n:,}  {hint}")

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
