#!/usr/bin/env python3
"""核对文档里的数字型断言与实际状态是否一致。

## 为什么需要它

`README.md` / `CLAUDE.md` / `tasks.md` 里写着大量数字：单测项数、库内行数、覆盖比例、
缺失比例。它们**会随开发过期，而过期的数字会误导下一轮的优先级判断**。2026-08-18
一天之内撞到两次：

  「目前 140 项单测」        实际 342（后来又变 362）
  「76% 的唯一装备缺需求等级」 实际 0% —— L2 详情页补全早就填满了，
                            而那条待办还排在 tasks.md 1b.7 的第一位

第二条尤其值得记：**一条过期的「已知缺口」会让人去做一件已经做完的事**。
所以把这些断言变成可执行核对，而不是靠人记得回来改。

## 边界

只核对**能从库或测试里直接取到的数字**。像「86.4% 词缀区间一致」这类需要跑对账脚本
才能得出的，不在这里核对——把它塞进来会让本脚本变成又一个慢命令，反而没人跑。
那类数字由各自的 `verify_*.py` 负责。

    python scripts/check_doc_facts.py          # 不一致就非零退出
    python scripts/check_doc_facts.py --list   # 只列出登记了哪些断言
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import REPO_ROOT, get_config  # noqa: E402


@dataclass(frozen=True)
class Fact:
    """一条数字断言。

    `pattern` 必须**只有一个捕获组**且只匹配一处——匹配到多处说明同一个数字在文档里
    写了两遍，那本身就是隐患（改一处漏一处），所以按错误报出来。
    """

    name: str
    doc: str
    pattern: str
    live: Callable[[sqlite3.Connection], int]
    note: str = ""


def _count(table: str) -> Callable[[sqlite3.Connection], int]:
    def inner(conn: sqlite3.Connection) -> int:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608
    return inner


def _gap_magnitude(question: str) -> Callable[[sqlite3.Connection], int]:
    """某个采集类缺口在台账里合计还缺多少条。"""
    def inner(conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(sum(hit_count),0) FROM knowledge_gap "
            "WHERE question=? AND kind='ingest_gap' AND source='import'",
            (question,),
        ).fetchone()
        return int(row[0])

    return inner


def _count_where(table: str, where: str) -> Callable[[sqlite3.Connection], int]:
    def inner(conn: sqlite3.Connection) -> int:
        return int(
            conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]  # noqa: S608
        )
    return inner


def _distinct(table: str, column: str) -> Callable[[sqlite3.Connection], int]:
    def inner(conn: sqlite3.Connection) -> int:
        return int(
            conn.execute(f"SELECT count(DISTINCT {column}) FROM {table}").fetchone()[0]  # noqa: S608
        )
    return inner


def _unit_test_count(_conn: sqlite3.Connection) -> int:
    """跑一遍发现（不执行用例）数出总数。

    只 discover 不 run：run 一遍会把日志打进本脚本的输出里，而且没必要——
    这里核对的是「文档写的项数」，不是「用例是否通过」（那是 /check 的事）。
    """
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(REPO_ROOT / "tests"), top_level_dir=str(REPO_ROOT))

    def walk(s: unittest.TestSuite | unittest.TestCase) -> int:
        if isinstance(s, unittest.TestCase):
            return 1
        return sum(walk(child) for child in s)

    return walk(suite)


def _golden_case_count(_conn: sqlite3.Connection) -> int:
    """黄金问答集的用例数。

    登记它的理由：`CLAUDE.md` 一度写着「47 条」而实际已是 51 条——**没被登记的数字
    就会悄悄漂**，而这个数字直接影响对评测覆盖面的判断。
    """
    sys.path.insert(0, str(REPO_ROOT))
    from tests.golden.loader import load_cases  # noqa: PLC0415

    return len(load_cases())


FACTS: tuple[Fact, ...] = (
    Fact("单测项数", "README.md", r"目前 (\d+) 项", _unit_test_count),
    Fact("CLAUDE.md 单测项数", "CLAUDE.md", r"当前 (\d+) 项", _unit_test_count),
    Fact("唯一装备件数", "README.md", r"POEDB 唯一装备 ([\d,]+) 件",
         _count_where("entity", "entity_type='unique_item'")),
    Fact("词缀条数", "README.md", r"词缀 ([\d,]+) 条（含 T 级", _count("mod_affix")),
    Fact("别名条数", "README.md", r"别名 ([\d,]+) 条", _count("alias")),
    Fact("PoB 词缀条数", "README.md", r"词缀 ([\d,]+) 条、唯一装备", _count("pob_mod")),
    Fact("PoB 唯一装备件数", "README.md", r"唯一装备 ([\d,]+) 件、游戏常量",
         _count("pob_unique")),
    Fact("游戏常量条数", "README.md", r"游戏常量 ([\d,]+) 条", _count("game_constant")),
    Fact("底材条数", "README.md", r"底材库已入库（18 类 ([\d,]+) 条",
         _count("pob_base_item")),
    Fact("底材类别数", "README.md", r"底材库已入库（(\d+) 类", _distinct("pob_base_item", "category_key")),
    Fact("CLAUDE.md 底材条数", "CLAUDE.md", r"`pob_base_item` \*\*([\d,]+)\*\*",
         _count("pob_base_item")),
    Fact("CLAUDE.md 词缀条数", "CLAUDE.md", r"`mod_affix` ([\d,]+)", _count("mod_affix")),
    Fact("CLAUDE.md 实体数", "CLAUDE.md", r"`entity` ([\d,]+)", _count("entity")),
    Fact("CLAUDE.md 切片数", "CLAUDE.md", r"`chunk` ([\d,]+)", _count("chunk")),
    Fact("CLAUDE.md 别名数", "CLAUDE.md", r"`alias` ([\d,]+)", _count("alias")),
    Fact("CLAUDE.md 黄金用例数", "CLAUDE.md", r"questions\.yaml`（(\d+) 条", _golden_case_count),
    # 这三个数此前只在一处被登记，另一处就悄悄漂了：README 的黄金用例数停在 47（实际 87），
    # 常量数只登记了 README 而 CLAUDE.md 那份没人管。登记的成本是一行。
    Fact("README 黄金用例数", "README.md", r"questions\.yaml`（(\d+) 条", _golden_case_count),
    Fact("CLAUDE.md 游戏常量条数", "CLAUDE.md", r"`game_constant` ([\d,]+)",
         _count("game_constant")),
    Fact("元词缀矩阵条数", "README.md", r"页面矩阵 ([\d,]+) 条（operation",
         _count("craft_metamod_interaction")),
    Fact("CLAUDE.md 元词缀矩阵条数", "CLAUDE.md",
         r"`craft_metamod_interaction` \*\*([\d,]+)\*\* 行",
         _count("craft_metamod_interaction")),
    Fact("CLAUDE.md 跨来源差异条数", "CLAUDE.md",
         r"`source_conflict` \*\*([\d,]+)\*\*", _count("source_conflict")),
    Fact("CLAUDE.md 待核对条数", "CLAUDE.md",
         r"`mod_review` \*\*([\d,]+)\*\* 条",
         _count_where("source_conflict", "kind='mod_review'")),
    Fact("审计样本量", "tests/golden/BASELINE.md", r"审计样本 \| 24 条（判 18） \| \*\*(\d+) 条",
         lambda _c: 48),
    Fact("宝石颗数", "README.md", r"技能石与辅助宝石 \| ([\d,]+) 颗",
         _count("pob_skill_gem")),
    Fact("宝石等级行数", "README.md", r"\+ ([\d,]+) 行每级字面值",
         _count("pob_skill_level")),
    Fact("CLAUDE.md 宝石颗数", "CLAUDE.md", r"`pob_skill_gem` \*\*([\d,]+)\*\*",
         _count("pob_skill_gem")),
    Fact("CLAUDE.md 宝石等级行数", "CLAUDE.md", r"`pob_skill_level` \*\*([\d,]+)\*\* 行",
         _count("pob_skill_level")),
    Fact("CLAUDE.md 怪物词缀条数", "CLAUDE.md", r"`poedb_monster_mod` \*\*([\d,]+)\*\*",
         _count("poedb_monster_mod")),
    Fact("CLAUDE.md 幽魂条数", "CLAUDE.md", r"`poedb_spectre` \*\*([\d,]+)\*\*",
         _count("poedb_spectre")),
    Fact("CLAUDE.md PoB 幽魂条数", "CLAUDE.md", r"`pob_spectre` \*\*([\d,]+)\*\*",
         _count("pob_spectre")),
    Fact("CLAUDE.md 已确认幽魂分歧", "CLAUDE.md", r"`spectre` \*\*([\d,]+)\*\*",
         _count_where("source_conflict", "kind='spectre'")),
    Fact("CLAUDE.md 天赋树节点数", "CLAUDE.md",
         r"`passive_graph_node` \*\*([\d,]+)\*\*", _count("passive_graph_node")),
    Fact("CLAUDE.md 天赋树可经过节点", "CLAUDE.md", r"其中可经过 \*\*([\d,]+)\*\*",
         _count_where("passive_graph_node", "passable=1")),
    Fact("CLAUDE.md 天赋树连线数", "CLAUDE.md",
         r"`passive_graph_edge`\n?\*\*([\d,]+)\*\* 条", _count("passive_graph_edge")),
    Fact("天赋树连线数", "README.md", r"连线 ([\d,]+) 条",
         _count("passive_graph_edge")),
    Fact("CLAUDE.md 通货条数", "CLAUDE.md", r"`poedb_currency` \*\*([\d,]+)\*\*",
         _count("poedb_currency")),
    Fact("CLAUDE.md 通货缺效果文本条数", "CLAUDE.md", r"其中 \*\*([\d,]+)\*\* 条在 POEDB 上",
         _count_where("poedb_currency", "effect_zh IS NULL")),
    Fact("通货条数", "README.md", r"\| 通货 \| ([\d,]+) 条",
         _count("poedb_currency")),
    Fact("CLAUDE.md 圣甲虫条数", "CLAUDE.md", r"`poedb_scarab` \*\*([\d,]+)\*\*",
         _count("poedb_scarab")),
    Fact("圣甲虫条数", "README.md", r"\| 圣甲虫 \| ([\d,]+) 个",
         _count("poedb_scarab")),
    Fact("CLAUDE.md 星团核心天赋条数", "CLAUDE.md",
         r"`pob_cluster_notable` \*\*([\d,]+)\*\*", _count("pob_cluster_notable")),
    Fact("星团核心天赋条数", "README.md", r"([\d,]+) 条核心天赋与基石清单",
         _count("pob_cluster_notable")),
    Fact("星团小天赋类型数", "README.md", r"([\d,]+) 条小天赋类型",
         _count("pob_cluster_skill")),
    Fact("CLAUDE.md 地图张数", "CLAUDE.md", r"`poedb_map` \*\*([\d,]+)\*\*",
         _count("poedb_map")),
    Fact("地图张数", "README.md", r"\| 地图 \| ([\d,]+) 张",
         _count("poedb_map")),
    Fact("CLAUDE.md 有等阶地图数", "CLAUDE.md", r"只有 ([\d,]+) 张有，梦魇",
         _count_where("poedb_map", "tier IS NOT NULL")),
    Fact("怪物词缀条数", "README.md", r"怪物词缀 ([\d,]+) 条",
         _count("poedb_monster_mod")),
    Fact("幽魂条数", "README.md", r"可作幽魂怪物 ([\d,]+) 个",
         _count("poedb_spectre")),
    Fact("CLAUDE.md 技能石颗数", "CLAUDE.md", r"（([\d,]+) 技能石 \+ ",
         _count_where("pob_skill_gem", "gem_kind='active'")),
    Fact("CLAUDE.md 辅助宝石颗数", "CLAUDE.md", r"技能石 \+ ([\d,]+) 辅助）",
         _count_where("pob_skill_gem", "gem_kind='support'")),
    Fact("CLAUDE.md 宝石效果文本条数", "CLAUDE.md",
         r"`poedb_gem_stat` \*\*([\d,]+)\*\*", _count("poedb_gem_stat")),
    Fact("CLAUDE.md 宝石每级取值行数", "CLAUDE.md",
         r"`poedb_gem_stat_level` \*\*([\d,]+)\*\* 行", _count("poedb_gem_stat_level")),
    Fact("CLAUDE.md 效果文本入库颗数", "CLAUDE.md", r"820 颗里 \*\*([\d,]+) 颗\*\*",
         lambda c: int(c.execute(
             "SELECT count(DISTINCT gem_id) FROM poedb_gem_stat_level").fetchone()[0])),
    Fact("CLAUDE.md 宝石待核对颗数", "CLAUDE.md", r"`gem_review` \*\*([\d,]+)\*\*",
         _count_where("source_conflict", "kind='gem_review'")),
    #: 文档里写的「未桥接 N 条」直接对着**台账**核：那句话的出处就是台账，
    #: 而台账的 `hit_count` 这一类量的正是缺多少条（见 `core.db.record_ingest_gaps`）。
    #: 第三十一轮实测过反面——文档与台账都写着 11 条，而实际只剩 1 条。
    Fact("CLAUDE.md 宝石未桥接条数", "CLAUDE.md", r"未桥接的 ([\d,]+) 条按成因登记在台账",
         _gap_magnitude("技能石与辅助宝石的完整清单")),
)


def check(conn: sqlite3.Connection) -> list[tuple[Fact, str, str]]:
    """返回不一致项：(fact, 文档写的, 实际)。文档里找不到该断言也算不一致。"""
    problems: list[tuple[Fact, str, str]] = []
    for fact in FACTS:
        path = REPO_ROOT / fact.doc
        if not path.is_file():
            problems.append((fact, "(文档不存在)", "—"))
            continue
        text = path.read_text(encoding="utf-8")
        found = re.findall(fact.pattern, text)
        actual = fact.live(conn)
        if not found:
            problems.append((fact, "(未在文档中找到该断言)", f"{actual:,}"))
            continue
        if len(found) > 1:
            problems.append((
                fact, f"(同一断言出现 {len(found)} 处：{found})", f"{actual:,}",
            ))
            continue
        claimed = int(str(found[0]).replace(",", ""))
        if claimed != actual:
            problems.append((fact, f"{claimed:,}", f"{actual:,}"))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="核对文档数字与实际状态")
    ap.add_argument("--list", action="store_true", help="只列出登记的断言")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.list:
        print(f"已登记 {len(FACTS)} 条数字断言：")
        for f in FACTS:
            print(f"  {f.doc:12s} {f.name:20s} /{f.pattern}/")
        return 0

    cfg = get_config()
    if not cfg.db_path.is_file():
        print(f"[跳过] 数据库不存在：{cfg.db_path}")
        return 0
    conn = sqlite3.connect(str(cfg.db_path))
    try:
        problems = check(conn)
    finally:
        conn.close()

    print("=" * 72)
    print(f"文档事实核对：{len(FACTS)} 条断言")
    print("=" * 72)
    if not problems:
        print("全部一致")
        return 0
    print(f"{len(problems)} 条不一致：\n")
    print(f"  {'文档':12s} {'断言':22s} {'文档写的':>18s}   {'实际':>10s}")
    for fact, claimed, actual in problems:
        print(f"  {fact.doc:12s} {fact.name:22s} {claimed:>18s}   {actual:>10s}")
    print("\n改文档，或者——如果实际值才是错的——先修数据再改文档。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
