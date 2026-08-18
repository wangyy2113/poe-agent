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

from parsers.mod_bridge import (  # noqa: E402
    ladder_overlap,
    ladder_slots,
    norm_ranges,
    same_magnitude,
    slot_key,
)
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
    """并列身份下**数值都一致**时可判一致；**身份仍然是未知的**。

    这一族用例原先断言「并列即不判一致」。后来实测发现那太严：POEDB 弓上的火焰/冰霜
    附加伤害阶梯完全相同（GGG 给三元素同一组数值），词缀名也同为「升级的/Upgraded」，
    三个 PoB 组里那一档的区间是同一个——无论正确对手是哪个，取值都一样。
    于是判据细化为「**在每一个候选身份下都一致**才判一致」，见
    `AmbiguousButAgreeingTest`。这里保留的是它的另一半：判一致**不等于**认定了身份。
    """

    def _conn(self) -> sqlite3.Connection:
        conn = _db()
        ladder = [(60, [[15.0, 20.0]]), (72, [[21.0, 25.0]])]
        _poedb(conn, "Dexterity", "Amulets", ladder)
        # 同名组存在但装的是别的东西（历史条目）——所以第一关不会命中
        _pob(conn, "Dexterity", [(9, [[1.0, 2.0]])])
        # 两个组共享与 POEDB 完全相同的阶梯 → 身份不唯一，但取值处处相同
        _pob(conn, "DexterityImplicit", ladder)
        _pob(conn, "StrengthSuffix", ladder)
        return conn

    def test_取值处处相同则判一致(self) -> None:
        rep = reconcile(self._conn(), None)
        self.assertEqual(2, rep.agree)
        self.assertEqual(2, rep.ladder_ambiguous_agreed)
        self.assertEqual(0, rep.needs_review)

    def test_判一致不代表认定了身份(self) -> None:
        """计数分开记：`ladder_ambiguous_agreed` 明确标出「这批的身份没定」。

        混进普通的 agree 里，就等于对外声称两源在这条词缀上互相验证过了——
        而实际上只验证了**数值**，没验证**是哪条词缀**。
        """
        rep = reconcile(self._conn(), None)
        self.assertEqual(rep.ladder_ambiguous_agreed, 2)
        self.assertEqual(rep.ladder_identical, 0, "这批不该被记成阶梯全等的那类")

    def test_唯一对手时走的是另一条路(self) -> None:
        conn = _db()
        ladder = [(50, [[0.2, 0.4]]), (60, [[0.6, 0.8]])]
        _poedb(conn, "LifeLeech", "Amulets", ladder)
        _pob(conn, "LifeLeech", [(9, [[1.0, 2.0]])])
        _pob(conn, "LifeLeechPermyriad", ladder)
        rep = reconcile(conn, None)
        self.assertEqual(2, rep.agree)
        self.assertEqual(0, rep.ladder_ambiguous_agreed, "对手唯一，不该记进歧义那批")


class DisplayCandidateTest(unittest.TestCase):
    """review 行展示的 PoB 侧必须来自**最佳重合组** × **同 ilvl**。

    这张清单的用处全在这一点上，而这里踩过两次：
      第一版 `near[0]`（同名组任取一条）→ 人看到的「冲突」多半是拿 T3 比 T1；
      第二版按 ilvl 对齐，但候选池仍限在同名组 → 实测 POEDB 弓·命中值 与 PoB 同名组
      `IncreasedAccuracy` 的阶梯重合度是 **0.000**，展示出来的仍是不相干的那条。
    """

    def test_展示的对手来自最佳重合组而不是同名组(self) -> None:
        conn = _db()
        # POEDB：三档，与同名组毫无共同档位，与另一个组三档里重合两档
        _poedb(conn, "IncreasedAccuracy", "Bows",
               [(1, [[80.0, 130.0]]), (20, [[131.0, 215.0]]), (40, [[216.0, 325.0]])])
        _pob(conn, "IncreasedAccuracy", [(1, [[5.0, 15.0]]), (12, [[16.0, 60.0]])])
        _pob(conn, "LocalAccuracyRating",
             [(1, [[80.0, 130.0]]), (20, [[131.0, 215.0]]), (40, [[216.0, 250.0]])])
        rep = reconcile(conn, None)
        item = next(c for c in rep.reviews if "ilvl=40" in c["subject"])
        self.assertEqual("LocalAccuracyRating", item["pob_ladder_group"])
        self.assertEqual(((216.0, 250.0),), item["pob_ranges"],
                         "必须展示最佳重合组里同 ilvl 的那条")


class AmbiguousButAgreeingTest(unittest.TestCase):
    """身份不唯一 ≠ 数值不可判（2026-08-18）。

    实测：POEDB 弓上的火焰 / 冰霜附加伤害阶梯**完全相同**（GGG 给三元素同一组数值），
    词缀名也同为「升级的 / Upgraded」，连词缀名锚点都分不开。但三个 PoB 组里该档的区间
    是同一个——所以无论正确对手是哪一个，两源取值都一致，与身份无关地成立。

    反之只要有**一个**候选身份下数值对不上，就仍要人看：那时「判一致」等于挑了一个
    看着顺眼的身份，正是 `parsers/mod_bridge.py` 明令禁止的做法。
    """

    def test_每个候选身份下都一致就判一致(self) -> None:
        conn = _db()
        ladder = [(20, [[19.0, 21.0], [32.0, 41.0]]), (40, [[38.0, 46.0], [73.0, 85.0]])]
        _poedb(conn, "FireDamage", "Bows", ladder)
        _pob(conn, "FireDamage", [(9, [[1.0, 2.0]])])          # 同名组装着别的东西
        _pob(conn, "ColdDamageBow", ladder)                     # 并列身份 1
        _pob(conn, "LightningDamageBow", ladder)                # 并列身份 2
        rep = reconcile(conn, None)
        self.assertEqual(2, rep.agree)
        self.assertEqual(2, rep.ladder_ambiguous_agreed)
        self.assertEqual(0, rep.needs_review)

    def test_某档在任一身份下都对不上就仍需人看(self) -> None:
        """两个并列身份的重合度相等（各 0.5），而 ilvl=40 那档的区间两边都没有。

        这时判一致就等于挑一个看着顺眼的身份，正是 mod_bridge 明令禁止的做法。
        """
        conn = _db()
        _poedb(conn, "FireDamage", "Bows",
               [(1, [[10.0, 19.0]]), (20, [[20.0, 29.0]]), (40, [[30.0, 39.0]])])
        _pob(conn, "FireDamage", [(9, [[1.0, 2.0]])])
        _pob(conn, "ColdDamageBow",
             [(1, [[10.0, 19.0]]), (20, [[20.0, 29.0]]), (40, [[30.0, 40.0]])])
        _pob(conn, "LightningDamageBow",
             [(1, [[10.0, 19.0]]), (20, [[20.0, 29.0]]), (40, [[30.0, 41.0]])])
        rep = reconcile(conn, None)
        causes = [c["cause"] for c in rep.reviews]
        self.assertIn("ladder_ambiguous", causes, "有身份下数值对不上时不能判一致")
        self.assertEqual(2, rep.agree, "前两档在两个身份下都一致，可判一致")


class NormRangesTest(unittest.TestCase):
    """区间归一。两处对账脚本此前**各写了一份逐字相同的实现**，现在共用一份。"""

    def test_整数与浮点写法等价(self) -> None:
        self.assertEqual(norm_ranges([[40, 50]]), norm_ranges("[[40.0, 50.0]]"))

    def test_端点顺序被归一(self) -> None:
        """区间是一组取值，端点顺序是**呈现**而不是事实。

        PoB 对减益类词缀按降序存（`(50-40)% reduced ...`），POEDB 写 (40-50)。
        不归一就会把 `Song of the Sirens` 的 [40,50] 与 [50,40] 报成数值分歧。
        """
        self.assertEqual(((40.0, 50.0),), norm_ranges([[50, 40]]))
        self.assertEqual(norm_ranges([[20, 30]]), norm_ranges([[30, 20]]))

    def test_非法项被丢掉而不是抛异常(self) -> None:
        self.assertEqual(((1.0, 2.0),), norm_ranges([[1, 2], ["a", "b"], [3]]))

    def test_符号不被抹掉(self) -> None:
        """抹掉符号能让更多行「对上」，但也会把真的符号错误一起吞掉。"""
        self.assertEqual(((-15.0, -10.0),), norm_ranges([[-10, -15]]))
        self.assertNotEqual(norm_ranges([[-15, -10]]), norm_ranges([[10, 15]]))


class SameMagnitudeTest(unittest.TestCase):
    """符号约定差异的识别：量值一致、符号相反。

    POEDB 把「移动速度减少 (15-10)%」记成 -15..-10，PoB 存 15..10 并靠文本里的
    `reduced` 表达方向。实测样例：Bramblejack、Greed's Embrace、Ashrend。
    """

    def test_符号相反但量值一致(self) -> None:
        self.assertTrue(same_magnitude(norm_ranges([[-15, -10]]), norm_ranges([[15, 10]])))

    def test_多段区间逐段比(self) -> None:
        self.assertTrue(same_magnitude(
            norm_ranges([[-60, -30], [1, 2]]), norm_ranges([[30, 60], [2, 1]])
        ))

    def test_量值不同不算约定差异(self) -> None:
        self.assertFalse(same_magnitude(norm_ranges([[-15, -10]]), norm_ranges([[20, 30]])))

    def test_段数不同不算约定差异(self) -> None:
        self.assertFalse(same_magnitude(norm_ranges([[-15, -10]]), norm_ranges([[15, 10], [1, 2]])))

    def test_空区间不算约定差异(self) -> None:
        """两边都没有区间时不能说「量值一致」——那是无从比对，不是比对通过。"""
        self.assertFalse(same_magnitude((), ()))
