"""工具路由的代码判定（2026-08-18）。

来历：评测里工具路由只命中 3/6，`mod_tiers` / `entity_lookup` / `kb_coverage` 三类问题
模型仍退回 `kb_search`，而提示词已经逐条写了选工具规则。提示词约束不住就落到代码。

**为什么不是协议层的 `tool_choice`**：实测该网关完全忽略它——不传 / `auto` / `required` /
指定函数名四种写法 `tool_calls` 恒为 0。所以「强制」只能靠代码自己调，或把别的选项从
schema 里拿掉。这些用例钉住前者的参数抽取边界。

词表用打桩：真实词表是 `data/dict/poe_terms.txt`，由采集管线生成，不该成为单测的前提。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator import loop

#: 打桩词表。取值照抄真实词表里的同类词，含单字类别「弓」。
_CLASSES = {"腰带", "项链", "弓", "胸甲", "手套"}
_GEN = {"前缀", "后缀"}
_SOURCES = {"精华", "工艺工作台", "隐匿"}
_OTHER = {"力量", "敏捷", "火焰伤害", "生命偷取", "力量和敏捷", "最大生命"}


def _scan(text: str) -> list[str]:
    """极大匹配：长词优先，被更长词包含的短词丢弃（与真实实现同一语义）。"""
    found = sorted(_CLASSES | _GEN | _SOURCES | _OTHER, key=len, reverse=True)
    out: list[str] = []
    for term in found:
        if term in text and not any(term != longer and term in longer for longer in out):
            out.append(term)
    return out


def _kind(kind: str) -> set[str]:
    return {"item_class": _CLASSES, "gen": _GEN, "mod_source": _SOURCES}.get(kind, set())


class ModTiersArgsTest(unittest.TestCase):
    """参数抽取：抽不出唯一解就必须返回 None，交回模型。"""

    def setUp(self) -> None:
        patcher = patch.dict(
            "sys.modules",
            {"retrieval.vocab": _FakeVocab()},  # type: ignore[dict-item]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_extracts_class_and_mod_query(self) -> None:
        self.assertEqual(
            {"item_class": "腰带", "mod_query": "力量"},
            loop._mod_tiers_args("腰带上的力量词缀一共有几档，每档要求什么物品等级"),
        )

    def test_single_char_item_class(self) -> None:
        """单字类别「弓」必须认出来——jieba 会把「弓上」粘成一个词，
        这正是领域词表存在的理由。"""
        self.assertEqual(
            {"item_class": "弓", "mod_query": "火焰伤害"},
            loop._mod_tiers_args("弓上的火焰伤害前缀有哪些等级"),
        )

    def test_gen_and_source_labels_are_not_the_mod_query(self) -> None:
        """「前缀」「精华」是限定词不是词缀内容，不能当查询串。"""
        got = loop._mod_tiers_args("腰带上精华来源的最大生命后缀有几档")
        self.assertEqual({"item_class": "腰带", "mod_query": "最大生命"}, got)

    def test_no_item_class_gives_up(self) -> None:
        self.assertIsNone(loop._mod_tiers_args("这条词缀有几档"))

    def test_two_item_classes_give_up(self) -> None:
        """两个类别时不猜：猜错了得到的是另一个底材的档位，而那种错在答案里看不出来。"""
        self.assertIsNone(loop._mod_tiers_args("腰带和项链上的力量词缀分别有几档"))

    def test_two_mod_candidates_give_up(self) -> None:
        self.assertIsNone(loop._mod_tiers_args("腰带上的力量与敏捷词缀各有几档"))

    def test_compound_mod_wins_over_its_parts(self) -> None:
        """极大匹配偏向「库里真实存在的最长词」。

        `+X 力量和敏捷` 是真词缀（实测 221 行，腰带上 6 档），所以
        「力量和敏捷词缀有几档」抽成一个复合词缀是对的，不是把两条词缀粘在了一起。
        """
        self.assertEqual(
            {"item_class": "腰带", "mod_query": "力量和敏捷"},
            loop._mod_tiers_args("腰带上的力量和敏捷词缀有几档"),
        )

    def test_vocab_unavailable_gives_up(self) -> None:
        with patch.dict("sys.modules", {"retrieval.vocab": None}):
            self.assertIsNone(loop._mod_tiers_args("腰带上的力量词缀有几档"))


class _FakeVocab:
    """替掉 `retrieval.vocab` 的最小桩。"""

    scan_terms = staticmethod(_scan)
    terms_of_kind = staticmethod(_kind)


class PreferredRouteTest(unittest.TestCase):
    """意图词判定：该路由的路由，不该的不动。"""

    def test_tier_questions_route_to_mod_tiers(self) -> None:
        for q in ("腰带上的力量词缀一共有几档", "T1 需要多少物品等级", "各档生成权重是多少"):
            with self.subTest(q=q):
                self.assertEqual("mod_tiers", loop._preferred_tool_route(q))

    def test_总览问法路由到_mod_class_summary(self) -> None:
        """「一共有哪些词缀」`kb_search` 答不了——它是相关度检索，
        `top_k` 之外按设计丢掉，既不知道总数也说不出还剩多少。"""
        for q in ("地图词缀有哪些", "腰带有哪些词缀", "异界地图的词缀一共有多少",
                  "胸甲的词缀池有多大"):
            with self.subTest(q=q):
                self.assertEqual("mod_class_summary", loop._preferred_tool_route(q))

    def test_具体梯度问法胜过总览问法(self) -> None:
        """**成对点**：`mod_class_summary` 必须排在 `mod_tiers` 后面。
        「…精华词缀有哪些**档位**」同时含「词缀有哪些」与「档位」，
        它问的是某一条词缀的梯度——首个命中即返回，排错顺序就会被总览工具抢走
        （实测被 `test_词缀阶梯问法不被抢走` 拦下过）。"""
        for q in ("戒指上冰霜伤害提高的精华词缀有哪些档位",
                  "腰带上的力量词缀一共有几档"):
            with self.subTest(q=q):
                self.assertEqual("mod_tiers", loop._preferred_tool_route(q))

    def test_属性来源枚举不被总览抢走(self) -> None:
        """`stat_sources` 问的是「某属性能从哪来」，跨渠道；总览问的是某底材的全貌。
        两者都含「有哪些」，靠「词缀」这个词区分不开——所以总览的判据是
        **「词缀」紧邻「有哪些」**，而属性题的说法是「渠道 / 来源」。"""
        for q in ("法术压制能从哪些渠道获得", "闪避值有哪些来源"):
            with self.subTest(q=q):
                self.assertNotEqual("mod_class_summary", loop._preferred_tool_route(q))

    def test_enumeration_questions_route_to_stat_sources(self) -> None:
        for q in ("法术压制能从哪些渠道获得", "生命再生能从哪些渠道获得",
                  "闪避值有哪些来源"):
            with self.subTest(q=q):
                self.assertEqual("stat_sources", loop._preferred_tool_route(q))

    def test_物品的怎么获得不归_stat_sources(self) -> None:
        """**这条咬过两次**：第五十一轮「混沌石怎么获得」、第五十五轮「猎首怎么获得」
        都被 `stat_sources` 抢走——那是「某**属性**能从哪来」的工具，对物品答非所问。

        `怎么获得 / 哪里获得 / 从哪获得` 同时适用于属性与物品，**光看问句分不出主语**，
        所以 `stat_sources` 只认「渠道 / 来源」这类专指属性来源清单的说法。
        物品问法落回普通检索：猎首那张卡上写着五张命运卡，混沌石那一维由维度闸门拒答。

        实测七条走这条路的黄金用例里**只有两条是属性题**。
        """
        for q in ("混沌石怎么获得", "猎首怎么获得", "深渊圣甲虫怎么获得",
                  "冰霜新星这个技能石怎么获得"):
            with self.subTest(q=q):
                self.assertNotEqual("stat_sources", loop._preferred_tool_route(q))

    def test_计算器仍然给枚举题让路(self) -> None:
        """成对点：`怎么获得` **仍留在 `_ENUMERATION_INTENTS` 里**，因为那个集合
        还管「计算器遇到枚举题要让路」。一并删掉会让「法术压制怎么获得」被
        `mitigation_calc` 抢走——修一个坑挖另一个。"""
        self.assertIn("怎么获得", loop._ENUMERATION_INTENTS)
        self.assertNotIn("怎么获得", loop._STAT_SOURCE_INTENTS)
        self.assertNotEqual("mitigation_calc", loop._preferred_tool_route("法术压制怎么获得"))

    def test_coverage_questions_route_to_kb_coverage(self) -> None:
        for q in ("你的知识库covered哪些领域", "你能回答哪些问题", "采集了哪些数据"):
            with self.subTest(q=q):
                self.assertEqual("kb_coverage", loop._preferred_tool_route(q))

    def test_conflict_question_is_not_a_coverage_question(self) -> None:
        """「知识库」曾是过宽的意图词：问冲突被路由到覆盖清单，
        把一个正确的缺据声明换成了答非所问的回答。与 boss 领域曾把「怪物」
        当意图词是同一种错——意图词必须专指。"""
        self.assertEqual(
            "", loop._preferred_tool_route("知识库里有哪些已确认的跨来源数据冲突")
        )

    def test_price_question_routes_to_live_economy(self) -> None:
        self.assertEqual(
            "economy_price_lookup", loop._preferred_tool_route("猎首现在值多少混沌石")
        )

    def test_market_price_phrasings_route_to_live_economy(self) -> None:
        for question in ("神圣石现在多少钱", "猎首当前市价", "The Apothecary 行情"):
            with self.subTest(question=question):
                self.assertEqual("economy_price_lookup", loop._preferred_tool_route(question))

    def test_rare_item_price_routes_to_comparables_with_clean_item_text(self) -> None:
        question = (
            "帮我估值\n物品类别: 项链\n稀有度: 稀有\n恐惧之瞳\n玛瑙护身符\n"
            "--------\n物品等级: 84\n--------\n+107 最大生命\n这件值多少？"
        )
        self.assertEqual("economy_rare_comparables", loop._preferred_tool_route(question))
        args = loop._rare_comparable_args(question)
        self.assertIsNotNone(args)
        self.assertTrue(args["item_text"].startswith("物品类别: 项链"))
        self.assertTrue(args["item_text"].endswith("+107 最大生命"))

    def test_price_trend_routes_before_current_price(self) -> None:
        cases = {
            "猎首过去7天价格趋势": 168,
            "神圣石最近24小时涨跌": 24,
            "The Apothecary 过去一个月行情走势": 720,
        }
        for question, hours in cases.items():
            with self.subTest(question=question):
                self.assertEqual(
                    "economy_price_trend", loop._preferred_tool_route(question)
                )
                self.assertEqual(hours, loop._economy_trend_args(question)["window_hours"])

    def test_crafting_cost_is_not_claimed_by_snapshot_lookup(self) -> None:
        self.assertNotEqual(
            "economy_price_lookup", loop._preferred_tool_route("做一把毕业弓的制作成本")
        )

    def test_explicit_material_list_routes_to_deterministic_cost_tool(self) -> None:
        question = "2 个神圣石和 100 个混沌石一共多少钱"
        self.assertEqual("economy_material_cost", loop._preferred_tool_route(question))
        self.assertEqual({
            "materials": [
                {"query": "神圣石", "quantity": 2.0, "category": "auto"},
                {"query": "混沌石", "quantity": 100.0, "category": "auto"},
            ]
        }, loop._material_cost_args(question))

    def test_ambiguous_craft_cost_does_not_invent_a_material_list(self) -> None:
        self.assertIsNone(loop._material_cost_args("做一把毕业弓的制作成本"))
        self.assertNotEqual(
            "economy_material_cost", loop._preferred_tool_route("做一把毕业弓的制作成本")
        )

    def test_single_explicit_quantity_can_be_costed(self) -> None:
        self.assertEqual(
            {"materials": [{"query": "神圣石", "quantity": 10.0, "category": "auto"}]},
            loop._material_cost_args("10 个神圣石一共多少钱"),
        )

    def test_explicit_currency_operation_routes_to_state_machine(self) -> None:
        question = "稀有装备 2 条前缀 3 条后缀还能用崇高石吗"
        self.assertEqual("craft_operation_state", loop._preferred_tool_route(question))
        self.assertEqual({
            "operation": "崇高石",
            "rarity": "稀有",
            "prefix_count": 2,
            "suffix_count": 3,
        }, loop._craft_operation_args(question))

    def test_currency_price_still_routes_to_economy_not_operation(self) -> None:
        question = "崇高石现在多少钱"
        self.assertIsNone(loop._craft_operation_args(question))
        self.assertEqual("economy_price_lookup", loop._preferred_tool_route(question))

    def test_generic_craft_plan_does_not_invent_an_operation(self) -> None:
        self.assertIsNone(loop._craft_operation_args("这把毕业弓应该怎么做"))
        self.assertNotEqual("craft_operation_state", loop._preferred_tool_route("这把毕业弓应该怎么做"))


class FirstRoundToolsTest(unittest.TestCase):
    """网关忽略 tool_choice，所以「强制」靠裁剪 schema。"""

    TOOLS = [
        {"type": "function", "function": {"name": "kb_search"}},
        {"type": "function", "function": {"name": "mod_tiers"}},
        {"type": "function", "function": {"name": "entity_lookup"}},
    ]

    def test_trims_to_the_preferred_tool(self) -> None:
        got = loop._first_round_tools("mod_tiers", self.TOOLS)
        self.assertEqual(["mod_tiers"], [s["function"]["name"] for s in got])

    def test_no_preference_keeps_everything(self) -> None:
        self.assertEqual(self.TOOLS, loop._first_round_tools("", self.TOOLS))

    def test_unknown_tool_keeps_everything(self) -> None:
        """偏好工具不在本轮 schema 里（可能被来源授权裁掉了）时不能把工具清空。"""
        self.assertEqual(
            self.TOOLS, loop._first_round_tools("affix_pool_odds", self.TOOLS)
        )


if __name__ == "__main__":
    unittest.main()
