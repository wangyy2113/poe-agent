"""POEDB（P1，中文）与 PoB（P2，英文）唯一装备交叉校验。

    python scripts/verify_pob_uniques.py
    python scripts/verify_pob_uniques.py --write-conflicts
    python scripts/verify_pob_uniques.py --name Headhunter

两个源都是对 GGG 数据的二次加工（POEDB 抓页面、PoB 导 .dat），任一侧抽取错误都会
让下游全错。这个脚本也是**变体过滤是否正确**的真正检验：POEDB 页面只显示当前版本，
如果 PoB 侧过滤对了，两边的数值应当吻合；对不上多半是过滤规则有问题。

可比字段（都是语言无关的）：
  底材英文名     POEDB 的 attributes.base_type_en vs PoB 的 base_type_en
  需求等级       requirement.level vs level_req
  隐式词缀条数   kind=implicit 计数 vs Implicits: N
  词缀数值区间   逐条词缀的区间序列 —— 纯数字，跨语言可比

**不比词缀文本**：一个中文一个英文，比不了。
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
from parsers.mod_bridge import norm_ranges, same_magnitude  # noqa: E402
from kb.sources.poedb_mods import LINE_SEP  # noqa: E402

#: POEDB 侧不参与事实比对的词缀类型：括号说明文本不是真实词条
SKIP_KINDS = frozenset({"description"})

#: 实质校验字段：数值或名称层面，语言无关，不一致就是两源真的对不上。
SUBSTANTIVE_FIELDS = ("base_type", "level_req", "implicit_count", "ranges")

#: 结构指标：两源对「什么算一条词缀」的建模不同，差异**不代表数值分歧**。
#: PoB 把 `Elder Item`（影响力标记）、`Cannot roll Caster Modifiers` 这类
#: 也算独立词缀，POEDB 不抽取它们；POEDB 又把混合词缀合成一行。
#: 实测归一化混合词缀后一致率没变（52.9%），说明差异来自建模本身而非记法。
#: 所以只做观测，不计入一致率，也不进冲突台账——否则会用几百条结构噪声
#: 把真正需要核对的项目埋掉。
INFORMATIONAL_FIELDS = ("mod_count",)


@dataclass
class Report:
    poedb_total: int = 0
    pob_total: int = 0
    matched: int = 0
    only_poedb: list[str] = field(default_factory=list)
    only_pob: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)

    agree_all: int = 0
    field_ok: Counter = field(default_factory=Counter)
    field_diff: Counter = field(default_factory=Counter)
    field_skipped: Counter = field(default_factory=Counter)
    #: 符号约定差异（量值一致、符号相反）。既不是一致也不是分歧，单独一栏。
    sign_convention: int = 0
    #: 每个字段的跳过**方向**：`{字段: {"POEDB 缺": n, "PoB 缺": n, "两侧都缺": n}}`。
    #: 只记总数会让人把「PoB 缺」误读成本项目的采集缺口（实测踩过）。
    skipped_side_counts: dict[str, Counter] = field(default_factory=dict)
    diffs: list[dict] = field(default_factory=list)


def variable_only(fp: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
    """只保留**可变**区间（下界 != 上界）。

    两个源对固定值的记法不同（实测）：`+1 技能石等级` 在 POEDB 侧记成区间
    `(1,1)`，在 PoB 侧是 `[]`。这是记法差异不是数值分歧，不剔掉会让整条
    指纹序列错位，把一堆一致的装备误报成不一致。
    可变区间才是有意义的比对对象——T 级与掉落浮动都在那里。
    """
    return tuple((a, b) for a, b in fp if a != b)


def poedb_fingerprint(mods: list[dict]) -> list[tuple[tuple[float, float], ...]]:
    return [
        variable_only(norm_ranges(m.get("ranges")))
        for m in mods
        if m.get("kind") not in SKIP_KINDS
    ]


def load_pob(conn, name_filter: str | None):  # noqa: ANN001
    sql = (
        "SELECT uid, name_en, base_type_en, level_req, implicit_count, "
        "mods, ranges_fp, variant_names, current_variants, obsolete_variants, url "
        "FROM pob_unique"
    )
    args: tuple = ()
    if name_filter:
        sql += " WHERE name_en = ?"
        args = (name_filter,)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for r in conn.execute(sql, args):
        d = dict(r)
        d["mods"] = json.loads(d["mods"] or "[]")
        d["variant_names"] = json.loads(d["variant_names"] or "[]")
        d["current_variants"] = json.loads(d["current_variants"] or "[]")
        d["obsolete_variants"] = json.loads(d["obsolete_variants"] or "[]")
        by_name[d["name_en"]].append(d)
    return by_name


def _note_skip(rep: Report, field_name: str, a_missing: bool, b_missing: bool) -> None:
    """记跳过，并记**是哪一侧缺**。

    只记总数就会误导：`level_req` 跳过 926 条曾被这个脚本自己解读成
    「POEDB 侧没抓到，PoB 可回填」，而实测方向正相反（POEDB 全有、PoB 缺 926）。
    方向决定「该不该去补」，所以必须一起记。
    """
    rep.field_skipped[field_name] += 1
    side = (
        "两侧都缺" if a_missing and b_missing
        else "POEDB 缺" if a_missing
        else "PoB 缺"
    )
    rep.skipped_side_counts.setdefault(field_name, Counter())[side] += 1


def compare(rep: Report, poedb: dict, pob: dict) -> dict | None:
    """比对一对装备。返回差异明细，全部一致则返回 None。"""
    attrs = poedb["attrs"]
    mods = [m for m in attrs.get("mods", []) if m.get("kind") not in SKIP_KINDS]
    diff: dict[str, tuple] = {}

    # 底材
    poedb_base = (attrs.get("base_type_en") or "").strip()
    pob_base = (pob["base_type_en"] or "").strip()
    if not poedb_base or not pob_base:
        _note_skip(rep, "base_type", not poedb_base, not pob_base)
    elif poedb_base == pob_base:
        rep.field_ok["base_type"] += 1
    else:
        rep.field_diff["base_type"] += 1
        diff["base_type"] = (poedb_base, pob_base)

    # 需求等级
    req = attrs.get("requirement") or {}
    poedb_lvl = req.get("level")
    pob_lvl = pob["level_req"]
    if poedb_lvl is None or pob_lvl is None:
        _note_skip(rep, "level_req", poedb_lvl is None, pob_lvl is None)
    elif int(poedb_lvl) == int(pob_lvl):
        rep.field_ok["level_req"] += 1
    else:
        rep.field_diff["level_req"] += 1
        diff["level_req"] = (poedb_lvl, pob_lvl)

    # 隐式词缀条数
    poedb_impl = sum(1 for m in mods if m.get("kind") == "implicit")
    pob_impl = int(pob["implicit_count"] or 0)
    if poedb_impl == pob_impl:
        rep.field_ok["implicit_count"] += 1
    else:
        rep.field_diff["implicit_count"] += 1
        diff["implicit_count"] = (poedb_impl, pob_impl)

    # 词缀条数。POEDB 侧把混合词缀的多行合并成一行（用 " / " 分隔，
    # 见 kb/sources/poedb_mods.clean_mod_text），PoB 侧是独立的多行。
    # 不归一会把这个纯结构差异算成数值差异。
    pob_mods = pob["mods"]
    poedb_count = sum(1 + m.get("text", "").count(LINE_SEP.strip()) for m in mods)
    if poedb_count == len(pob_mods):
        rep.field_ok["mod_count"] += 1
    else:
        rep.field_diff["mod_count"] += 1
        diff["mod_count"] = (poedb_count, len(pob_mods))

    # 区间指纹。用**多重集**比而不是按序比：两边的词缀顺序不保证一致，
    # 按序比会把顺序差异误报成数值差异。
    poedb_fp = sorted(poedb_fingerprint(mods))
    pob_fp = sorted(variable_only(norm_ranges(m.get("ranges"))) for m in pob_mods)
    poedb_nonempty = [f for f in poedb_fp if f]
    pob_nonempty = [f for f in pob_fp if f]
    if not poedb_nonempty and not pob_nonempty:
        # 两边都没有带区间的词缀（如固定值装备），无从比对
        _note_skip(rep, "ranges", not poedb_nonempty, not pob_nonempty)
    elif poedb_nonempty == pob_nonempty:
        rep.field_ok["ranges"] += 1
    elif len(poedb_nonempty) == len(pob_nonempty) and all(
        same_magnitude(x, y)
        for x, y in zip(poedb_nonempty, pob_nonempty, strict=True)
    ):
        # **符号约定差异**，不是数值分歧：POEDB 把「移动速度减少 (15-10)%」记成 -15..-10，
        # PoB 存 15..10 并靠文本里的 `reduced` 表达方向。量值逐项一致。
        # 实测样例：Bramblejack [-15,-10] vs [15,10]、Ashrend [-60,-30] vs [30,60]。
        #
        # **单独计数、不并入 field_ok**：并进去就等于对外声称两源在这条上互相验证过，
        # 而我们其实抹掉了符号——真的「一侧把加号看成减号」也会被一起吞掉。
        rep.sign_convention += 1
    else:
        rep.field_diff["ranges"] += 1
        diff["ranges"] = (poedb_nonempty, pob_nonempty)

    substantive = {k: v for k, v in diff.items() if k in SUBSTANTIVE_FIELDS}
    if not substantive:
        rep.agree_all += 1
    if not diff:
        return None
    return {
        "substantive": bool(substantive),
        "name_en": poedb["name_en"],
        "name_zh": poedb["name_zh"],
        "poedb_url": poedb["url"],
        "pob_url": pob["url"],
        "pob_uid": pob["uid"],
        "variants": pob["variant_names"],
        "current_variants": pob["current_variants"],
        "obsolete_variants": pob["obsolete_variants"],
        "fields": diff,
    }


def reconcile(conn, name_filter: str | None) -> Report:  # noqa: ANN001
    rep = Report()
    pob_by_name = load_pob(conn, name_filter)
    rep.pob_total = sum(len(v) for v in pob_by_name.values())

    sql = (
        "SELECT entity_id, name_zh, name_en, base_type, url, attributes FROM entity "
        "WHERE entity_type='unique_item' AND name_en IS NOT NULL AND name_en <> ''"
    )
    args: tuple = ()
    if name_filter:
        sql += " AND name_en = ?"
        args = (name_filter,)

    matched_uids: set[str] = set()
    for r in conn.execute(sql, args):
        rep.poedb_total += 1
        name = r["name_en"]
        candidates = pob_by_name.get(name) or []
        if not candidates:
            rep.only_poedb.append(name)
            continue

        poedb = {
            "entity_id": r["entity_id"], "name_zh": r["name_zh"], "name_en": name,
            "url": r["url"], "attrs": json.loads(r["attributes"] or "{}"),
        }
        if len(candidates) == 1:
            pob = candidates[0]
        else:
            # 同名多底材（Grand Spectrum 有三种珠宝底材）：用底材消歧。
            # 消歧不了就记为待查，不硬配——配错了会产出一堆假差异。
            want = (poedb["attrs"].get("base_type_en") or "").strip()
            hits = [c for c in candidates if (c["base_type_en"] or "").strip() == want]
            if len(hits) != 1:
                rep.ambiguous.append(f"{name}（{want or '底材未知'}）")
                continue
            pob = hits[0]

        rep.matched += 1
        matched_uids.add(pob["uid"])
        d = compare(rep, poedb, pob)
        if d:
            rep.diffs.append(d)

    for name, lst in pob_by_name.items():
        for c in lst:
            if c["uid"] not in matched_uids:
                rep.only_pob.append(f"{name}（{c['base_type_en']}）")
    return rep


def write_conflicts(conn, rep: Report) -> int:  # noqa: ANN001
    """只写**实质**字段的差异。结构指标（mod_count）不写——
    用几百条结构噪声会把真正需要核对的项目埋掉。"""
    rows = []
    for d in rep.diffs:
        for field_name, (a, b) in d["fields"].items():
            if field_name not in SUBSTANTIVE_FIELDS:
                continue
            rows.append((
                f"{d['name_en']} · {field_name}", field_name, "unique_review",
                "POEDB", json.dumps(a, ensure_ascii=False)[:500],
                d["name_en"], d["poedb_url"],
                "Path of Building", json.dumps(b, ensure_ascii=False)[:500],
                d["pob_uid"], d["pob_url"],
                f"变体：{d['variants'] or '无'}；保留 {d['current_variants']}，"
                f"排除 {d['obsolete_variants']}。待人工核对；"
                f"确认为真冲突后改 kind='unique'，届时按需求 15.6 推荐采用 POEDB",
            ))
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
    ap.add_argument("--name", default=None, help="只校验某一件（英文名）")
    ap.add_argument("--write-conflicts", action="store_true")
    ap.add_argument("--show", type=int, default=10)
    ap.add_argument("--field", default=None, help="只看某个字段的差异")
    args = ap.parse_args()

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING", 14)
    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()

    if not conn.execute("SELECT count(*) FROM pob_unique").fetchone()[0]:
        print("pob_unique 是空的，先跑 scripts/import_pob_uniques.py")
        return 2

    rep = reconcile(conn, args.name)

    print("=" * 78)
    print("POEDB（P1 中文） × PoB（P2 英文）唯一装备交叉校验")
    print("=" * 78)
    print(f"  POEDB 有英文名的  : {rep.poedb_total:,}")
    print(f"  PoB 条目          : {rep.pob_total:,}")
    print(f"  成功配对          : {rep.matched:,}")
    print(f"  仅 POEDB 有        : {len(rep.only_poedb):,}  例：{rep.only_poedb[:4]}")
    print(f"  仅 PoB 有          : {len(rep.only_pob):,}  例：{rep.only_pob[:4]}")
    if rep.ambiguous:
        print(f"  同名消歧失败      : {len(rep.ambiguous):,}  例：{rep.ambiguous[:4]}")

    if rep.matched:
        print(f"\n  全部实质字段一致  : {rep.agree_all:,}"
              f"（占已配对 {rep.agree_all / rep.matched:.1%}）")

    def _table(fields: tuple[str, ...]) -> None:
        print(f"    {'字段':<16}{'一致':>7}{'不一致':>8}{'跳过':>7}   一致率")
        for f in fields:
            ok, bad, skip = rep.field_ok[f], rep.field_diff[f], rep.field_skipped[f]
            total = ok + bad
            rate = f"{ok / total:.1%}" if total else "—"
            print(f"    {f:<16}{ok:>7}{bad:>8}{skip:>7}   {rate}")

    print("\n  实质校验（数值/名称层面，语言无关；跳过 = 一方无数据）：")
    _table(SUBSTANTIVE_FIELDS)
    print("\n  结构指标（两源建模方式不同，差异不代表数值分歧，不计入一致率）：")
    _table(INFORMATIONAL_FIELDS)
    if rep.sign_convention:
        print(f"\n  符号约定差异        : {rep.sign_convention}"
              f"（量值逐项一致、符号相反，如 POEDB -15..-10 对 PoB 15..10）"
              f"\n    ← 既不算一致也不算分歧：抹掉符号才能对上，"
              f"而抹掉符号就会连真的符号错误一起吞掉")

    # 跳过率本身就是发现：某个字段大量跳过说明有一侧根本没抓到。
    # **但必须说清是哪一侧缺**，否则这行提示会把人指向错误的方向。
    #
    # 这里原先写的是「POEDB 侧多数唯一装备没抓到需求等级，PoB 侧有值可回填」——
    # **方向是反的**。实测已配对 1215 件里：PoB 缺、POEDB 有 = 926（76.2%），
    # 两侧都有 = 289，POEDB 缺 = 0。也就是说需求等级**不是采集缺口**，
    # POEDB 侧一直是全的，装备分析要的字段本来就在库里。缺的是 PoB
    # （它的唯一装备数据多数不带等级需求），而 P2 缺字段不影响下游。
    if rep.skipped_side_counts:
        print("\n  跳过是哪一侧缺（跳过 = 至少一侧无数据，方向决定该不该去补）：")
        for fld, sides in sorted(rep.skipped_side_counts.items()):
            parts = "，".join(f"{side} {n:,}" for side, n in sorted(sides.items()))
            print(f"    {fld:<18} {parts}")
        print("    ← 只有「POEDB 缺」才是本项目的采集缺口；"
              "「PoB 缺」不影响下游，因为答案以 P1 为准")

    diffs = [d for d in rep.diffs if d["substantive"]]
    if args.field:
        diffs = [d for d in diffs if args.field in d["fields"]]
    if diffs:
        print("\n" + "=" * 78)
        print(f"差异明细（前 {min(args.show, len(diffs))} / {len(diffs)} 件）")
        print("=" * 78)
        print("  这些**不是**已确认的跨来源冲突，是待人工核对项。")
        for d in diffs[:args.show]:
            print(f"\n  {d['name_zh']}（{d['name_en']}）")
            if d["obsolete_variants"]:
                print(f"    变体 {len(d['variants'])} 个，保留 {d['current_variants']}，"
                      f"排除 {d['obsolete_variants']}")
            for f, (a, b) in d["fields"].items():
                if f == "ranges":
                    print(f"    {f}:")
                    print(f"      POEDB {len(a)} 条带区间: {a[:4]}")
                    print(f"      PoB   {len(b)} 条带区间: {b[:4]}")
                else:
                    print(f"    {f}: POEDB={a!r}  PoB={b!r}")

    if args.write_conflicts and diffs:
        conn.execute("DELETE FROM source_conflict WHERE kind='unique_review'")
        n = write_conflicts(conn, rep)
        total = conn.execute(
            "SELECT count(*) FROM source_conflict WHERE kind='unique_review'"
        ).fetchone()[0]
        print(f"\n已写入 {n} 条待核对项，source_conflict 现有 {total} 条 unique_review")
        print("  （只写实质字段差异；mod_count 这类结构差异不写）")
    elif diffs:
        print("\n（未落库；加 --write-conflicts 写入 source_conflict）")

    dbmod.close_db()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
