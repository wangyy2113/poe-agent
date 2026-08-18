"""跨来源词缀对账的配对判据（2026-08-18）。

## 来历

`source_conflict` 里 `kind='mod'`（已确认冲突）长期是 0 条，压着 2729 条 `mod_review`。
需求 15.5 要求确认冲突并列展示，所以这条能力一直无法验收，`citation.conflict_block`
也一直是不可达代码。

查因发现的不是「人没来核对」，而是**配对判据错了**：对账按 `correct_group` 名字配对，
而 `parsers/mod_bridge.py` 早就实测过两侧组名系统性对不上（掉落词缀同名组仅 11.5%，
POEDB `PhysicalDamage` ↔ PoB `LocalPhysicalDamage`）。同一个概念有两份判据，
其中一份已被证明是错的。

实测代价：2729 条待核对里，817 条在 PoB 侧连一个共同档位都没有（配对配错了，
不是数值冲突），831 条的正确对手在**不同名的组**里（被组名差异冤枉成分歧）。

## 判据改成阶梯指纹

同一条词缀在两源是同一份 GGG 数据，整条档位阶梯应逐级相同（每级 ilvl + 区间）。
原语与桥接层共用 `parsers.mod_bridge` 的同一份实现——这是这次改动的重点，
不是又写一套。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from parsers.mod_bridge import ladder_overlap, ladder_slots, slot_key  # noqa: E402
from scripts.verify_pob_mods import (  # noqa: E402
    LADDER_MIN_OVERLAP,
    load_pob,
    pob_ladder_index,
    poedb_ladder_index,
    reconcile,
)


class LadderPrimitiveTest(unittest.TestCase):
    """阶梯原语。公开它们的目的就是**只有一份实现**。"""

    def test_档位键含物品等级与区间(self) -> None:
        self.assertEqual("70:1-1.2", slot_key(70, [(1.0, 1.2)]))

    def test_整数与浮点写法不影响档位键(self) -> None:
        self.assertEqual(slot_key(70, [(1, 2)]), slot_key(70, [(1.0, 2.0)]))

    def test_多段区间按顺序进键(self) -> None:
        self.assertEqual("13:6-8,12-15", slot_key(13, [(6.0, 8.0), (12.0, 15.0)]))

    def test_逐档全等重合度为一(self) -> None:
        a = ladder_slots([(50, [(0.2, 0.4)]), (60, [(0.6, 0.8)])])
        b = ladder_slots([(60, [(0.6, 0.8)]), (50, [(0.2, 0.4)])])
        self.assertEqual(1.0, ladder_overlap(a, b))

    def test_毫无共同档位重合度为零(self) -> None:
        a = ladder_slots([(50, [(0.2, 0.4)])])
        b = ladder_slots([(9, [(1.0, 2.0)])])
        self.assertEqual(0.0, ladder_overlap(a, b))

    def test_空阶梯不算重合(self) -> None:
        """空集与任何集合的 Jaccard 定义为 0，而不是 1——不能让「两边都没数据」
        看起来像「完全一致」。"""
        self.assertEqual(0.0, ladder_overlap(set(), {"1:1-2"}))


def _db() -> sqlite3.Connection:
    """最小内存库：只建对账用到的两张表与列。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE mod_affix(
            uid TEXT, item_class_page TEXT, item_class_zh TEXT, category TEXT,
            correct_group TEXT, ilvl INTEGER, tier INTEGER, tier_count INTEGER,
            weight INTEGER, text_zh TEXT, ranges TEXT, name_zh TEXT, url TEXT
        );
        CREATE TABLE pob_mod(
            uid TEXT, mod_id TEXT, mod_kind TEXT, mod_group TEXT, ilvl INTEGER,
            text_en TEXT, ranges TEXT, affix_type TEXT, affix_name TEXT,
            weights TEXT, spawn_tags TEXT, tags TEXT, trade_hashes TEXT, url TEXT
        );
        """
    )
    return conn


def _poedb(conn, group, page, tiers, category="normal") -> None:  # noqa: ANN001
    for i, (ilvl, ranges) in enumerate(tiers, start=1):
        conn.execute(
            "INSERT INTO mod_affix VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{group}-{page}-{ilvl}", page, page, category, group, ilvl,
             len(tiers) - i + 1, len(tiers), 1000, f"中文 {ranges}",
             json.dumps(ranges), "名", "https://poedb.tw/cn/x"),
        )


def _pob(conn, group, tiers, kind="explicit") -> None:  # noqa: ANN001
    for ilvl, ranges in tiers:
        conn.execute(
            "INSERT INTO pob_mod VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{group}-{ilvl}", f"{group}{ilvl}", kind, group, ilvl,
             f"en {ranges}", json.dumps(ranges), "Suffix", "of X",
             "{}", "[]", "[]", "{}", "https://github.com/x"),
        )


class CrossGroupNameTest(unittest.TestCase):
    """组名不同但阶梯全等 → **一致**，不是待核对。

    这是实测里 831 条被冤枉的那一支：POEDB `LifeLeech` 在项链上是
    (0.2-0.4)/(0.6-0.8)/(1-1.2)，PoB 把它放在 `LifeLeechPermyriad`，
    而同名的 `LifeLeech` 组里是三条权重为 0 的历史条目 (1-2)/(3-4)/(5-6)。
    """

    def _conn(self) -> sqlite3.Connection:
        conn = _db()
        ladder = [(50, [[0.2, 0.4]]), (60, [[0.6, 0.8]]), (70, [[1.0, 1.2]])]
        _poedb(conn, "LifeLeech", "Amulets", ladder)
        _pob(conn, "LifeLeech", [(9, [[1.0, 2.0]]), (25, [[3.0, 4.0]]), (72, [[5.0, 6.0]])])
        _pob(conn, "LifeLeechPermyriad", ladder)
        return conn

    def test_全部判为一致且不产生待核对(self) -> None:
        rep = reconcile(self._conn(), None)
        self.assertEqual(3, rep.agree)
        self.assertEqual(0, rep.needs_review, "组名差异不该被报成分歧")
        self.assertEqual(3, rep.ladder_identical)
        self.assertEqual([], rep.reviews)

    def test_按组名过滤时仍能找到不同名的对手(self) -> None:
        """`--group LifeLeech` 时 PoB 侧不能也被按组名筛掉，否则正确对手先被丢掉。"""
        rep = reconcile(self._conn(), "LifeLeech")
        self.assertEqual(3, rep.agree)
        self.assertEqual(0, rep.needs_review)


class NoLadderTest(unittest.TestCase):
    """阶梯毫无重合 → PoB 没有这条阶梯，是覆盖缺口，**不入**待核对清单。"""

    def test_不重合不进清单(self) -> None:
        conn = _db()
        _poedb(conn, "ManaRegeneration", "Amulets", [(30, [[2.5, 3.0]]), (60, [[4.0, 5.0]])])
        _pob(conn, "ManaRegeneration", [(30, [[19.0, 21.0]]), (60, [[25.0, 30.0]])])
        rep = reconcile(conn, None)
        self.assertEqual(0, rep.needs_review)
        self.assertEqual(2, rep.no_ladder)
        self.assertEqual([], rep.reviews)


class LadderPartialTest(unittest.TestCase):
    """阶梯大部分重合、个别档位对不上 → **这才是值得人工核对的**。"""

    def test_部分重合进清单且带成因与重合度(self) -> None:
        conn = _db()
        # 三档里两档相同，第三档区间不同 —— 同一条词缀但某档对不上
        _poedb(conn, "IncreasedLife", "Amulets",
               [(1, [[10.0, 19.0]]), (11, [[20.0, 29.0]]), (23, [[30.0, 39.0]])])
        _pob(conn, "IncreasedLife",
             [(1, [[10.0, 19.0]]), (11, [[20.0, 29.0]]), (23, [[30.0, 40.0]])])
        rep = reconcile(conn, None)
        self.assertEqual(1, rep.needs_review)
        self.assertEqual(2, rep.agree)
        item = rep.reviews[0]
        self.assertEqual("ladder_partial", item["cause"])
        self.assertGreaterEqual(item["ladder_overlap"], LADDER_MIN_OVERLAP)
        self.assertEqual("IncreasedLife", item["pob_ladder_group"])

    def test_对齐同物品等级而不是任取一条(self) -> None:
        """PoB 侧显示的必须是同 ilvl 的那条。

        以前取 `near[0]`（同组任取一条），于是人看到的「冲突」多半是拿 T3 去比 T1
        的产物——2729 行清单没法用，一半原因在这里。
        """
        conn = _db()
        _poedb(conn, "IncreasedLife", "Amulets",
               [(1, [[10.0, 19.0]]), (11, [[20.0, 29.0]]), (23, [[30.0, 39.0]])])
        _pob(conn, "IncreasedLife",
             [(1, [[10.0, 19.0]]), (11, [[20.0, 29.0]]), (23, [[30.0, 40.0]])])
        item = reconcile(conn, None).reviews[0]
        self.assertEqual(((30.0, 40.0),), item["pob_ranges"],
                         "应取 ilvl=23 的那条，而不是阶梯里的第一条")


class LadderIndexTest(unittest.TestCase):
    def test_POEDB侧按底材类别分开建阶梯(self) -> None:
        """同一词缀组在不同底材上是不同阶梯（弓上 T5、法杖上 T4）。
        合起来算指纹就没有一条能对上 PoB。"""
        conn = _db()
        _poedb(conn, "IncreasedAttackSpeed", "Bows", [(37, [[17.0, 19.0]])])
        _poedb(conn, "IncreasedAttackSpeed", "Claws", [(60, [[23.0, 25.0]])])
        idx = poedb_ladder_index(conn, None)
        self.assertEqual({"37:17-19"}, idx[("normal", "IncreasedAttackSpeed", "Bows")])
        self.assertEqual({"60:23-25"}, idx[("normal", "IncreasedAttackSpeed", "Claws")])

    def test_PoB侧按kind与组建阶梯(self) -> None:
        conn = _db()
        _pob(conn, "IncreasedLife", [(1, [[10.0, 19.0]])])
        _pob(conn, "IncreasedLife", [(1, [[3.0, 5.0]])], kind="corrupted")
        idx = pob_ladder_index(load_pob(conn, None))
        self.assertEqual({"1:10-19"}, idx["explicit"]["IncreasedLife"])
        self.assertEqual({"1:3-5"}, idx["corrupted"]["IncreasedLife"])


if __name__ == "__main__":
    unittest.main()


class LadderAmbiguityTest(unittest.TestCase):
    """多个 PoB 组共享同一条阶梯 → **不敢判一致**，如实登记歧义。

    阶梯形状并不唯一：`+(15-20) to Strength` 与 `+(15-20) to Dexterity` 的
    (ilvl, 区间) 阶梯完全一样。实测就撞上了——POEDB 的 `Dexterity` 组被配到了
    PoB 的 `Strength` 组。若在并列里任取一个再判「一致」，就是拿 Strength 的数值
    去「验证」Dexterity：**假一致比假冲突糟得多，它把真分歧悄悄盖掉了。**

    `parsers/mod_bridge.py` 早写下同一条结论：撞车时不选，返回歧义。
    这条护栏实测把 311 行从「一致」拉回「待核对」。
    """

    def _conn(self) -> sqlite3.Connection:
        conn = _db()
        ladder = [(60, [[15.0, 20.0]]), (72, [[21.0, 25.0]])]
        _poedb(conn, "Dexterity", "Amulets", ladder)
        # 同名组存在但装的是别的东西（历史条目）——所以第一关不会命中
        _pob(conn, "Dexterity", [(9, [[1.0, 2.0]])])
        # 两个组共享与 POEDB 完全相同的阶梯 → 对手不唯一
        _pob(conn, "DexterityImplicit", ladder)
        _pob(conn, "StrengthSuffix", ladder)
        return conn

    def test_并列时不判一致(self) -> None:
        rep = reconcile(self._conn(), None)
        self.assertEqual(0, rep.agree, "对手不唯一时不能判一致")
        self.assertGreaterEqual(rep.ladder_ambiguous, 1)

    def test_歧义行带ladder_ambiguous成因(self) -> None:
        rep = reconcile(self._conn(), None)
        causes = {c["cause"] for c in rep.reviews}
        self.assertIn("ladder_ambiguous", causes)

    def test_唯一对手时仍然判一致(self) -> None:
        """护栏只在并列时生效，别把正常的跨组配对也挡掉。"""
        conn = _db()
        ladder = [(50, [[0.2, 0.4]]), (60, [[0.6, 0.8]])]
        _poedb(conn, "LifeLeech", "Amulets", ladder)
        _pob(conn, "LifeLeech", [(9, [[1.0, 2.0]])])
        _pob(conn, "LifeLeechPermyriad", ladder)
        rep = reconcile(conn, None)
        self.assertEqual(2, rep.agree)
        self.assertEqual(0, rep.ladder_ambiguous)
