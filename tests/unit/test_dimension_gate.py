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
            _status("cluster_jewel", "not_collected", 0),
            # 2026-08-18：技能石与辅助宝石已入库（PoB × POEDB 桥接 810 颗），
            # 转 partial —— 伤害数值、效果描述与属性需求仍未采集
            _status("skill_gem", "partial", 548),
            _status("support_gem", "partial", 262),
            _status("boss", "harvested", 0),
            _status("currency", "harvested", 0),
            # 2026-08-19：怪物页拆成三个域。boss（技能与应对）仍未采集，
            # 怪物词缀与幽魂各自入库
            _status("monster_mod", "partial", 183),
            _status("spectre", "partial", 1948),
        ]
        cov.load = lambda conn: self.rows  # type: ignore[assignment]
        self.cov = cov

    def tearDown(self) -> None:
        self.cov.load = self._real_load  # type: ignore[assignment]

    def _verdict(self, question: str, ids: tuple[str, ...] = ()) -> DimensionVerdict:
        return dimension_gap(_FakeConn(), question, exact_entity_ids=ids)

    # ── 该拦的拦住 ──

    def test_price_question_is_gap_even_with_entity_hit(self) -> None:
        """实体在库里不等于被问的那一面有数据 —— 这是首次评测的漏出之一。"""
        v = self._verdict("猎首现在值多少混沌石")
        self.assertTrue(v.is_gap)
        self.assertIn("economy_price", [s.key for s in v.uncovered])
        self.assertIn("市场价格", v.reason())

    def test_partial_domain_gaps_when_asked_dimension_is_the_missing_one(self) -> None:
        """partial 领域被问到它缺的那一维时必须拒答。

        例子换过一次：原先用「点到某节点要花几点」，那一维在 2026-08-19 第十五轮
        随连接关系入库而变成能力（见 `num_passive_path_cost`）。现在用同一个领域
        **当前**仍缺的那维——星团珠宝上的天赋不在主树上。
        用例要跟着数据走，留着一句已经不成立的边界比没有用例更糟。
        """
        v = self._verdict("星团珠宝上的天赋要花几个天赋点")
        self.assertTrue(v.is_gap)
        self.assertIn("passive_tree", [s.key for s in v.uncovered])
        self.assertIn("未采集的部分", v.reason())

    def test_not_collected_domain_is_gap(self) -> None:
        """星团珠宝一条都没入库 —— 未采集的领域整个维度都是缺的。"""
        v = self._verdict("大型星团珠宝能开出哪些天赋")
        self.assertTrue(any(s.key == "cluster_jewel" for s in v.uncovered))

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

    def test_实体命中不能把缺口放宽成有据(self) -> None:
        """别名命中的领域**只能进 uncovered，不能进 covered**。

        2026-08-19 实测回归：给闸门加上「实体命中也算问到该领域」之后，
        「猎首现在值多少混沌石」因为命中了猎首（unique_item 全量入库）而让 covered
        非空，于是「有一个领域覆盖到就不拦」的规则把价格缺口放过去了
        （评测里 gap_price_headhunter 从通过变成漏出）。

        两条通道证明力不同：意图词说明「在问这一面」，实体命中只说明「点了个专名」。
        """
        v = self._verdict("猎首现在值多少混沌石", ("unique_item:Headhunter",))
        self.assertTrue(v.is_gap)
        # 「混沌石」还会命中通货领域（harvested），它一起进 uncovered 是对的；
        # 关键是命中的唯一装备**没有**进 covered
        self.assertIn("economy_price", [s.key for s in v.uncovered])
        self.assertEqual([], [s.key for s in v.covered])

    def test_意图词命中的已入库领域照旧算覆盖(self) -> None:
        """上一条不能顺手把正常放行也压掉：靠意图词命中的领域仍要进 covered，
        否则复合问题会被整轮拦掉（拒答粒度是断言不是整轮）。"""
        v = self._verdict("猎首有哪些词缀，值多少钱", ("unique_item:Headhunter",))
        self.assertTrue(v.uncovered)
        self.assertTrue(v.covered)
        self.assertFalse(v.is_gap)

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
    """宝石入库后的维度边界（2026-08-18 第十二轮）。

    ## 机制换了一层，用例也跟着换

    PR #12 时技能石整个领域未采集，于是「识别出这是技能石问题」等于拒答，伤害类问法
    （`级时的基础伤害` 等）就放在 `hints` 里。这一轮 810 颗宝石入库、领域转 `partial`，
    同一批问法必须改由 `missing_dimension_hints` 拦——领域是「问到了」，缺的是伤害那一面。
    两处说的是同一件事，所以只能同时改（规约第 5 条的成对点）。

    ## 领域识别多了一条通道：别名精确命中

    「冰霜脉冲的伤害是多少」里没有任何技能石意图词，而 810 颗宝石名不可能进词表。
    所以别名通道命中的实体类型也算「问到了这个领域」。**只收别名通道**：BM25 的前几条
    里夹一张宝石卡是常事，拿它当领域信号会让含「伤害」的装备问题被误拦。
    """

    #: 别名精确命中的宝石实体（真实链路里由 `orchestrator/loop._exact_entity_ids` 传入）
    GEM_HIT = ("skill:FreezingPulse",)

    def test_伤害维度被拦_靠意图词(self) -> None:
        for q in ("技能石的伤害数值在哪查", "宝石等级提升后伤害是多少"):
            with self.subTest(q=q):
                self.assertTrue(self._verdict(q).is_gap, q)

    def test_伤害维度被拦_靠别名命中(self) -> None:
        """点名了一颗库里的宝石 + 问的是伤害 → 仍然拒答（本轮不入库伤害数值）。"""
        for q in ("冰霜脉冲在 20 级时的基础伤害是多少",
                  "冰霜脉冲 20 级的基础伤害",
                  "冰霜脉冲的伤害是多少",
                  "冰霜脉冲要多少智力需求"):
            with self.subTest(q=q):
                self.assertTrue(self._verdict(q, self.GEM_HIT).is_gap, q)

    def test_已入库维度放行(self) -> None:
        """同一颗宝石，问的是已入库的那一面 —— 必须答得出来。"""
        for q in ("冰霜脉冲 20 级需要多少魔力",
                  "冰霜脉冲是什么颜色的宝石",
                  "冰霜脉冲有哪些标签",
                  "冰霜脉冲 20 级需要角色多少级"):
            with self.subTest(q=q):
                self.assertFalse(self._verdict(q, self.GEM_HIT).is_gap, q)

    def test_装备等级问法不被误拦(self) -> None:
        """这四句问的是装备/词缀，不是宝石。

        判据必须**专指**：放宽成「等级」或「伤害」就会误拦
        「冈姆的壮志的需求等级是多少」——黄金集里 `num_kaoms_req_level` 是钉这个的
        反例哨兵（答案 68 在库里）。
        """
        for q in ("冈姆的壮志的需求等级是多少",
                  "猎首的需求等级是多少",
                  "腰带上物品等级 85 的力量词缀有几档",
                  "冈姆的壮志需要多少力量"):
            with self.subTest(q=q):
                self.assertFalse(self._verdict(q).is_gap, q)

    def test_唯一装备的伤害问题不被宝石维度误拦(self) -> None:
        """别名命中的是唯一装备时，宝石领域不该被牵连。

        这是「只收别名通道」之外的第二道保险：命中的实体类型决定问到了哪个领域。
        """
        v = self._verdict("冈姆的壮志加多少伤害", ("unique_item:Kaoms_Heart",))
        self.assertFalse(v.is_gap)
        self.assertFalse(any(s.key in {"skill_gem", "support_gem"} for s in v.uncovered))

    def test_辅助宝石的加成维度被拦(self) -> None:
        """辅助宝石给出的加成数值同样未入库（要靠 PoB 的 stat 插值算）。"""
        v = self._verdict("受伤时施放能提升多少伤害",
                          ("support_gem:SupportCastWhenDamageTaken",))
        self.assertTrue(v.is_gap)
        self.assertIn("辅助宝石", v.reason())


class MonsterDomainTest(DimensionGapTest):
    """怪物页拆成三个域之后的边界（2026-08-19 第十三轮）。

    原先 `boss` 一个域叫「Boss 与怪物」，把三件事混在一起：Boss 打法（缓存页里**没有**）、
    怪物词缀（有）、按等级的基础数值表（PoB 侧早在 `game_constant` 里）。
    混在一起时「已抓取未入库」读起来像是差一个抽取器，而实际缺的是**来源**。
    """

    def test_Boss打法仍然拒答(self) -> None:
        """黄金集里的 `gap_boss_mechanics` 钉的就是这条：拆域不能把它变成能答。"""
        for q in ("潜伏之主的技能怎么应对", "这个 boss 怎么打"):
            with self.subTest(q=q):
                v = self._verdict(q)
                self.assertTrue(v.is_gap, q)
                self.assertIn("boss", [s.key for s in v.uncovered])

    def test_怪物词缀效果可答(self) -> None:
        for q in ("传奇怪物会带哪些词缀", "魔符词缀是什么效果"):
            with self.subTest(q=q):
                self.assertFalse(self._verdict(q).is_gap, q)

    def test_幽魂的倍率抗性与排名都可答(self) -> None:
        """**期望在第十四轮变了**：接入 PoB `Data/Spectres.lua` 的 268 条建模数据后，
        抗性与排名从缺口变成能力。上一轮这三句是断言拒答的——数据补上了就得改断言，
        留着一句已经不成立的边界，等于让闸门把能答的问题声明成缺口。
        """
        for q in ("不朽精锐的幽魂倍率是多少", "生命倍率最高的幽魂是哪个",
                  "幽魂的抗性是多少", "幽魂选哪个好"):
            with self.subTest(q=q):
                self.assertFalse(self._verdict(q).is_gap, q)

    def test_幽魂的掉落与技能仍然拒答(self) -> None:
        """上一条的成对点。这两维两个来源都没有：技能在 PoB 侧只有内部 id，
        实测 1235 个与本库 810 颗宝石零命中，没有中文名可给。"""
        for q in ("幽魂会掉落什么东西", "幽魂会放什么技能"):
            with self.subTest(q=q):
                v = self._verdict(q)
                self.assertTrue(v.is_gap, q)
                self.assertIn("spectre", [s.key for s in v.uncovered])

    def test_档位问法不被幽魂的比较词误伤(self) -> None:
        """`spectre` 的 `missing_dimension_hints` 里有「最高」，但它只在本域被识别后
        才参与判定——词缀档位问题不含「幽魂」，不该被它拦。"""
        self.assertFalse(self._verdict("腰带上力量词缀的最高档位是多少").is_gap)

    def test_怪物词缀意图词不误伤装备问题(self) -> None:
        """「传奇」在 POE1 同时是装备稀有度，所以域意图词只收「传奇怪物」这种专指写法。"""
        for q in ("击败稀有怪物获得其属性修正的腰带", "猎首获得稀有怪物属性修正持续多久"):
            with self.subTest(q=q):
                self.assertFalse(self._verdict(q).is_gap, q)


class SubsumedCoverageTest(DimensionGapTest):
    """更专指的意图词命中时，被它包含的泛化命中不算覆盖（2026-08-19）。"""

    def test_泛化词缀命中不取消怪物词缀的缺口(self) -> None:
        """实测反例：「惊悸迷雾怪物词缀的生成权重是多少」。

        `monster_mod` 靠「怪物词缀」命中且权重正落在它的未采集维度，而 `mod_affix`
        （装备词缀，已全量入库）靠泛化的「词缀」「权重」命中被记为 covered，
        于是整句判为有据、模型拿到的是**装备**词缀的权重。
        """
        v = self._verdict("惊悸迷雾怪物词缀的生成权重是多少")
        self.assertTrue(v.is_gap)
        self.assertIn("monster_mod", [s.key for s in v.uncovered])
        self.assertEqual([], [s.key for s in v.covered])

    def test_复合问题仍然保留部分作答(self) -> None:
        """这条是上一条的成对点：判据只取「子串」这一档，不做同义词或语义相似度——
        否则复合问题会被误判成同一主语，而它必须能答有据的那半。
        """
        v = self._verdict("猎首有哪些词缀，值多少钱")
        self.assertTrue(v.uncovered)
        self.assertTrue(v.covered)
        self.assertFalse(v.is_gap)

    def test_不同主语的两个领域互不吞并(self) -> None:
        """「幽魂」与「词缀」没有包含关系，两边各自成立。"""
        v = self._verdict("猎首有哪些词缀，幽魂会掉落什么东西")
        self.assertIn("spectre", [s.key for s in v.uncovered])
        self.assertTrue(v.covered)
        self.assertFalse(v.is_gap)
