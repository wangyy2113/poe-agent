#!/usr/bin/env python3
"""天赋树对账：POEDB（P1）× Path of Building（P2）。

    python scripts/verify_tree.py            # 离线（两侧都在本地）
    python scripts/verify_tree.py --write    # 把待核对差异写进 source_conflict

## 对账键为什么用 skill id

两侧的节点 id 都是 GGG 的内部标识符，字面一致、语言无关，是天然的配对键。
名字不能当键：POEDB 是中文、PoB 是英文，归一化后是两个不同字符串
（这是 1b.4 在词缀对账上踩过的坑，此处不重复）。

## 比什么

比**数值指纹**——把一个节点全部 stats 里的数字取出来排序。纯数字、语言无关，
所以中英文可以直接比。文本本身不比（比不了）。

## 一个必须先归一的语言差异

中文把「额外发射 **1** 个投射物」的 1 写出来，英文写 "an additional Projectile"
不写数字。于是同一个节点两侧指纹差一个 `1`。这**不是数据冲突**，是量词习惯差异。
实测 27 个指纹不一致里绝大多数属于此类。

不归一就报冲突的后果很具体：冲突要按需求 15.5 并列展示给用户，
而「POEDB 说 1 个、PoB 说没有」这种由翻译习惯造出来的假冲突，
会让用户对真冲突也失去信任。造假冲突比漏报更糟——这条同样来自 1b.4。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.fetcher import Fetcher  # noqa: E402
from kb.sources import pob_lua, poedb_tree  # noqa: E402

#: PoB 检出目录（与 scripts/import_pob_*.py 同一约定：仓库同级）
POB_ROOT = Path(__file__).resolve().parent.parent.parent / "PathOfBuilding"

#: 数值指纹一致率下限。低于此值说明两侧对不上，须人工介入。
MIN_FINGERPRINT_MATCH = 0.90

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _as_list(v) -> list:  # noqa: ANN001
    """PoB 的 Lua 表转成 dict（键 "1"/"2"…），POEDB 是 list。统一成 list。"""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v[k] for k in sorted(v, key=lambda x: int(x) if str(x).isdigit() else 0)]
    return [v]


def _all_stats(node: dict) -> list[str]:
    """节点的全部属性文本，含大师天赋的可选效果。

    大师天赋必须一起比——它的 `stats` 是空的，效果在 `masteryEffects` 里。
    漏掉就等于 61 个大师天赋从对账里消失，而对账报告仍显示「全部一致」。
    """
    out = [str(s) for s in _as_list(node.get("stats")) if str(s).strip()]
    for eff in _as_list(node.get("masteryEffects")):
        if isinstance(eff, dict):
            out += [str(s) for s in _as_list(eff.get("stats")) if str(s).strip()]
    return out


def _fingerprint(node: dict) -> Counter:
    return Counter(_NUM_RE.findall(" ".join(_all_stats(node))))


def _diff_kind(fp_zh: Counter, fp_en: Counter) -> str:
    """把指纹差异分类。

    `numeral_convention`：差的**只是** `1`，且中文侧多。对应「额外 1 个」/
    "an additional" 这种量词习惯差异，不是数据冲突。
    `real`：其余，须人工核对。
    """
    extra_zh = fp_zh - fp_en
    extra_en = fp_en - fp_zh
    if not extra_en and set(extra_zh) <= {"1"}:
        return "numeral_convention"
    return "real"


def _upsert_conflict(conn, row: dict) -> None:  # noqa: ANN001
    conn.execute(
        """
        INSERT INTO source_conflict(subject, field, kind, source_a, value_a, ref_a, url_a,
                                    source_b, value_b, ref_b, url_b, note,
                                    first_seen, last_seen)
        VALUES (:subject,:field,:kind,:source_a,:value_a,:ref_a,:url_a,
                :source_b,:value_b,:ref_b,:url_b,:note,
                datetime('now'),datetime('now'))
        ON CONFLICT(subject, field, source_a, source_b) DO UPDATE SET
            value_a=excluded.value_a, value_b=excluded.value_b,
            note=excluded.note, last_seen=datetime('now')
        """,
        row,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="把待核对差异写进 source_conflict（kind=tree_review）")
    ap.add_argument("--force", action="store_true", help="强制重抓 POEDB 数据")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    conn = dbmod.init_db(cfg.db_path)
    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )

    problems: list[str] = []
    print("=" * 82)
    print("天赋树对账：POEDB（P1）× Path of Building（P2）")
    print("=" * 82)

    version = poedb_tree.discover_version(fetcher, poedb_tree.TARGET_PASSIVE, force=args.force)
    ep = poedb_tree.TreeEndpoint(poedb_tree.TARGET_PASSIVE, version, poedb_tree.LANG_ZH)
    payload = poedb_tree.fetch_payload(fetcher, ep, force=args.force)
    zh_nodes = {str(k): v for k, v in payload["nodes"].items() if isinstance(v, dict)}
    print(f"\n[1] 两侧版本与规模")
    print(f"    POEDB 树版本（发现所得）: {version}")

    # PoB 侧：目录名用下划线（3_29），且必须与 POEDB 版本对得上，
    # 否则就是拿两个版本的树在对账，「不一致」全是噪声。
    pob_dir = POB_ROOT / "src" / "TreeData" / version.replace(".", "_")
    tree_lua = pob_dir / "tree.lua"
    if not tree_lua.is_file():
        print(f"    PoB 侧缺 {tree_lua}")
        print("    → 两侧版本不一致时对账结果无意义，跳过（不判失败）")
        avail = sorted(p.name for p in (POB_ROOT / "src" / "TreeData").glob("*")
                       if (p / "tree.lua").is_file())
        print(f"    PoB 本地可用版本：{avail[-6:]}")
        dbmod.close_db()
        return 0

    pob = pob_lua.parse_return_table(tree_lua.read_text(encoding="utf-8"))
    pob_nodes = {str(k): v for k, v in pob["nodes"].items() if isinstance(v, dict)}
    print(f"    PoB  树版本（目录名）  : {version}（{tree_lua.relative_to(POB_ROOT)}）")
    print(f"    节点数：POEDB {len(zh_nodes):,} ｜ PoB {len(pob_nodes):,}")

    # ── 2. id 覆盖 ──
    zk, pk = set(zh_nodes), set(pob_nodes)
    only_zh, only_pob = sorted(zk - pk), sorted(pk - zk)
    print("\n[2] 节点 id 覆盖")
    print(f"    交集 {len(zk & pk):,} ｜ 仅 POEDB {len(only_zh)} ｜ 仅 PoB {len(only_pob)}")
    if only_zh:
        # POEDB 有而 PoB 没有：我们入库的是 POEDB，所以这批**无法交叉校验**，
        # 必须说出来而不是让它消失在「一致率」里
        print(f"      仅 POEDB（无法交叉校验）：{[zh_nodes[k].get('name') for k in only_zh[:8]]}")
    if only_pob:
        names = [str(pob_nodes[k].get("name") or k) for k in only_pob[:8]]
        print(f"      仅 PoB（我们未入库，PoB 侧可能含历史或 Ruthless 变体）：{names}")

    # ── 3. 数值指纹 ──
    print("\n[3] 数值指纹对账（含大师天赋的可选效果）")
    same = 0
    convention: list[tuple[str, str]] = []
    real: list[tuple[str, str, Counter, Counter, list[str], list[str]]] = []
    compared = 0
    for k in sorted(zk & pk):
        a, b = zh_nodes[k], pob_nodes[k]
        if not _all_stats(a):
            continue
        compared += 1
        fa, fb = _fingerprint(a), _fingerprint(b)
        if fa == fb:
            same += 1
            continue
        name = str(a.get("name") or k)
        if _diff_kind(fa, fb) == "numeral_convention":
            convention.append((k, name))
        else:
            real.append((k, name, fa, fb, _all_stats(a), _all_stats(b)))

    ratio = same / compared if compared else 0.0
    print(f"    参与对账 {compared:,} 个带属性节点")
    print(f"    指纹完全一致        : {same:,}（{ratio:.1%}）")
    print(f"    量词习惯差异（非冲突）: {len(convention)}")
    print(f"    **待人工核对**      : {len(real)}")
    if ratio < MIN_FINGERPRINT_MATCH:
        problems.append(
            f"数值指纹一致率 {ratio:.1%} 低于下限 {MIN_FINGERPRINT_MATCH:.0%}，"
            "两侧可能不是同一版本或抽取有误"
        )
    if convention:
        print(f"      量词差异样例：{[n for _, n in convention[:6]]}")
        print("      说明：中文写出「额外 1 个」的 1，英文用 an/a 不写数字，"
              "属翻译习惯而非数据分歧")
    for k, name, fa, fb, sa, sb in real[:8]:
        print(f"      !! skill={k} {name}")
        print(f"         POEDB 多出：{dict(fa - fb)}  PoB 多出：{dict(fb - fa)}")
        print(f"         POEDB: {json.dumps(sa, ensure_ascii=False)[:110]}")
        print(f"         PoB  : {json.dumps(sb, ensure_ascii=False)[:110]}")

    # ── 4. 属性条数 ──
    print("\n[4] 属性条数一致性")
    cnt_same = cnt_diff = 0
    for k in sorted(zk & pk):
        a, b = zh_nodes[k], pob_nodes[k]
        if not _all_stats(a):
            continue
        # 条数比较对 `\n` 敏感：两侧都保留了 GGG 源数据里的换行，所以按条数比是可比的
        if len(_all_stats(a)) == len(_all_stats(b)):
            cnt_same += 1
        else:
            cnt_diff += 1
    print(f"    条数一致 {cnt_same:,} ｜ 不一致 {cnt_diff}")

    # ── 5. 落库 ──
    if args.write and real:
        for k, name, fa, fb, sa, sb in real:
            _upsert_conflict(conn, {
                "subject": f"passive_node skill={k}",
                "field": "stats_numbers",
                # kind 用 tree_review 而非 tree：**待核对**不等于**已确认冲突**。
                # 沿用 1b.4 的做法——没有可靠判据之前不下冲突结论。
                "kind": "tree_review",
                "source_a": "POEDB",
                "value_a": json.dumps(sa, ensure_ascii=False)[:900],
                "ref_a": f"skill={k}",
                "url_a": ep.page_url,
                "source_b": "Path of Building",
                "value_b": json.dumps(sb, ensure_ascii=False)[:900],
                "ref_b": f"TreeData/{version.replace('.', '_')}/tree.lua",
                "url_b": "",
                "note": f"{name}：数值指纹不一致，非量词习惯差异，待人工核对",
            })
        conn.commit()
        print(f"\n    已写入 source_conflict：{len(real)} 条（kind=tree_review，待人工核对）")
    elif real:
        print("\n    （未写库；加 --write 可把待核对差异记入 source_conflict）")

    print("\n" + "=" * 82)
    if problems:
        print("结论：**未通过**")
        for p in problems:
            print(f"  - {p}")
    else:
        print("结论：通过")
        print(f"  {compared:,} 个节点参与对账，指纹一致 {ratio:.1%}，"
              f"待核对 {len(real)} 条，仅 POEDB 独有 {len(only_zh)} 个")
    dbmod.close_db()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
