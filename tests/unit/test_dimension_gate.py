"""维度覆盖闸门（`kb.coverage.dimension_gap`）与意图词剔除。

这两处都是评测跑出来的缺陷的修复点，用例按「该拦的拦住」与「该放的放行」成对写——
错误拒答与无据漏出必须一起压（需求 16.7）。

不连真实库：`dimension_gap` 只依赖 `DomainStatus`，用内存桩即可。
数值与状态照抄 2026-08-17 实测快照。
"""

from __future__ import annotations

import unittest

from kb.coverage import DOMAINS, DomainStatus, DimensionVerdict, dimension_gap
from retrieval.hybrid import _drop_intent_terms

_BY_KEY = {d.key: d for d in DOMAINS}


def _status(key: str, status: str, count: int = 0) -> DomainStatus:
    return DomainStatus(spec=_BY_KEY[key], status=status, item_count=count)


class _FakeConn:
    """`dimension_gap` 只通过 `load()` 取数据，这里直接替掉 load。"""


class DimensionGapTest(unittest.TestCase):
    def setUp(self) -> None:
        import kb.coverage as cov

        self._real_load = cov.load
        # 快照照抄 2026-08-17 实测：base_item 导入 123 条胸甲底材后为 partial
        self.rows = [
            _status("unique_item", "ingested", 1342),
            _status("mod_affix", "ingested", 93897),
            _status("passive_tree", "partial", 1631),
            _status("base_item", "partial", 123),
            _status("economy_price", "not_collected", 0),
            _status("skill_gem", "harvested", 0),
            _status("boss", "harvested", 0),
            _status("currency", "harvested", 0),
        ]
        cov.load = lambda conn: self.rows  # type: ignore[assignment]
        self.cov = cov

    def tearDown(self) -> None:
        self.cov.load = self._real_load  # type: ignore[assignment]

    def _verdict(self, question: str) -> DimensionVerdict:
        return dimension_gap(_FakeConn(), question)

    # ── 该拦的拦住 ──

    def test_price_question_is_gap_even_with_entity_hit(self) -> None:
        """实体在库里不等于被问的那一面有数据 —— 这是首次评测的漏出之一。"""
        v = self._verdict("猎首现在值多少混沌石")
        self.assertTrue(v.is_gap)
        self.assertIn("economy_price", [s.key for s in v.uncovered])
        self.assertIn("市场价格", v.reason())

    def test_partial_domain_gaps_when_asked_dimension_is_the_missing_one(self) -> None:
        """天赋节点属性已入库，但天赋点消耗未入库 —— 问的正是缺的那面。"""
        v = self._verdict("从起点点到神威之盾要花几个天赋点")
        self.assertTrue(v.is_gap)
        self.assertEqual(["passive_tree"], [s.key for s in v.uncovered])
        self.assertIn("未采集的部分", v.reason())

    def test_not_collected_domain_is_gap(self) -> None:
        v = self._verdict("大型星团珠宝的技能石有什么")
        self.assertTrue(any(s.key == "skill_gem" for s in v.uncovered))

    # ── 该放的放行 ──

    def test_partial_domain_passes_when_asked_dimension_is_covered(self) -> None:
        """同一个 partial 领域，问已入库的那一面必须放行。"""
        v = self._verdict("神威之盾这个天赋给什么属性")
        self.assertFalse(v.is_gap)
        self.assertEqual(["passive_tree"], [s.key for s in v.covered])

    def test_mixed_question_is_not_gapped_wholesale(self) -> None:
        """复合问题里有一个领域覆盖到就不整轮拦 —— 拒答粒度是断言不是整轮。

        缺的那部分由提示词的「覆盖边界」章节声明，不能把有据的那半也丢掉。
        """
        v = self._verdict("猎首有哪些词缀，值多少钱")
        self.assertTrue(v.uncovered, "价格维度应被识别为无数据")
        self.assertTrue(v.covered, "词缀维度应被识别为有数据")
        self.assertFalse(v.is_gap)

    def test_unique_item_question_with_monster_wording_passes(self) -> None:
        """「怪物」不是 Boss 领域的意图词 —— 它在词缀文本里到处都是。

        实测这两句曾被闸门误拦成缺据（错误拒答），因为 boss.hints 里有单独的「怪物」。
        """
        for q in (
            "击败稀有怪物获得其属性修正的腰带",
            "猎首获得稀有怪物属性修正持续多久",
        ):
            with self.subTest(q=q):
                self.assertFalse(self._verdict(q).is_gap)

    def test_no_domain_match_is_not_gap(self) -> None:
        """一个 hint 都没命中时不表态 —— 这一层只在有把握时改判。"""
        v = self._verdict("猎首提供多少力量")
        self.assertFalse(v.is_gap)
        self.assertFalse(v.uncovered)

    def test_gate_only_tightens(self) -> None:
        """闸门只能把「充分」改判为「不足」，不能反向放宽。"""
        v = self._verdict("神威之盾这个天赋给什么属性")
        self.assertFalse(v.is_gap)
        # 没有任何 API 能让 verdict 把不足改判为充分
        self.assertFalse(hasattr(v, "make_sufficient"))

    def test_category_label_feeds_the_ledger(self) -> None:
        v = self._verdict("猎首现在值多少混沌石")
        self.assertTrue(v.category_label().startswith("not_collected/"))
        self.assertTrue(v.authority_urls())
        self.assertTrue(v.missing_points())


class MissingDimensionHintsConsistencyTest(unittest.TestCase):
    """`missing_dimension_hints` 只对 partial 领域有意义，且必须与 partial_note 并存。"""

    def test_only_declared_where_partial_note_exists(self) -> None:
        for spec in DOMAINS:
            if spec.missing_dimension_hints:
                with self.subTest(domain=spec.key):
                    self.assertTrue(
                        spec.partial_note,
                        f"{spec.key} 声明了未覆盖维度词却没写 partial_note，"
                        "两者说的是同一件事，必须同步",
                    )

    def test_hints_and_missing_hints_do_not_swallow_each_other(self) -> None:
        """未覆盖维度词不能把整个领域的 hints 全盖住，否则该领域永远判缺据。"""
        for spec in DOMAINS:
            if not spec.missing_dimension_hints:
                continue
            with self.subTest(domain=spec.key):
                swallowed = [h for h in spec.hints if h in spec.missing_dimension_hints]
                self.assertNotEqual(
                    sorted(swallowed), sorted(spec.hints),
                    f"{spec.key} 的全部 hints 都被列为未覆盖维度，等于该领域整体不可用，"
                    "应改 status 而不是靠维度词",
                )


class DropIntentTermsTest(unittest.TestCase):
    """意图词不参与「全库为零即无依据」判定。"""

    def test_drops_registered_intent_term(self) -> None:
        """「档位」是用户口语（库里写 T 级 / 阶级 / 梯度），已登记为词缀意图词。

        它曾让「腰带上力量词缀的最高档位是多少」被判无依据，而正确的梯度卡已经召回。
        """
        self.assertEqual([], _drop_intent_terms(["档位"]))
        self.assertEqual(["玄虚"], _drop_intent_terms(["玄虚", "档位"]))

    def test_keeps_subject_terms(self) -> None:
        """专名必须留下 —— 那是这道判据真正要抓的东西。"""
        for term in ("玄虚", "流放之路2", "霜锻穹顶"):
            with self.subTest(term=term):
                self.assertEqual([term], _drop_intent_terms([term]))

    def test_empty_in_empty_out(self) -> None:
        self.assertEqual([], _drop_intent_terms([]))


if __name__ == "__main__":
    unittest.main()


class SkillGemIntentTest(DimensionGapTest):
    """技能石意图词必须**专指**（2026-08-18）。

    ## 来历

    检索侧唯一剩下的漏出：「冰霜脉冲在 20 级时的基础伤害是多少」。技能石未采集，
    但这句里 `冰霜`/`基础`/`伤害` 都是通用词、覆盖率 75% 达标；未覆盖的 `脉冲` 在库里
    有 12 条（全是能量护盾与防御词缀里的无关文本），所以「未覆盖词全库为零」那道护栏
    也不触发。领域识别不到 → 维度闸门不拦 → 整轮漏出。

    ## 为什么不改成按稀有度加权

    `retrieval/fts.absent_from_corpus` 的 docstring 明确写着「刻意不做 IDF 或稀有度
    加权」，理由是那会把「已采集但这次没召回」也判成拒答，而两类的正确处理完全不同。
    那是一个**已决且写下理由的决定**，所以这里不动它，改在领域识别层解决。

    ## 判据只能专指技能石

    装备说「物品等级」「需求等级」，角色说「角色等级」，**裸的「N 级」+ 数值问法**
    才是技能石特征。放宽成「等级」或「级」会把「冈姆的壮志的需求等级是多少」误拦——
    黄金问答集里 `num_kaoms_req_level` 就是钉这个的反例哨兵。
    """

    def test_技能石问法被拦(self) -> None:
        for q in ("冰霜脉冲在 20 级时的基础伤害是多少",
                  "冰霜脉冲 20 级的基础伤害",
                  "技能等级提高有什么用",
                  "技能石的数值在哪查"):
            with self.subTest(q=q):
                self.assertTrue(self._verdict(q).is_gap, q)

    def test_装备等级问法不被误拦(self) -> None:
        """这四句都必须能答——它们问的是装备/词缀，不是技能石。"""
        for q in ("冈姆的壮志的需求等级是多少",
                  "猎首的需求等级是多少",
                  "腰带上物品等级 85 的力量词缀有几档",
                  "冈姆的壮志需要多少力量"):
            with self.subTest(q=q):
                self.assertFalse(self._verdict(q).is_gap, q)
