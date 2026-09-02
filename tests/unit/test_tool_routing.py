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

    def test_price_question_gets_no_route(self) -> None:
        self.assertEqual("", loop._preferred_tool_route("猎首现在值多少混沌石"))


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
