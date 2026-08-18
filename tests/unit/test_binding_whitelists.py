"""过渡句与缺据声明白名单的放宽（2026-08-17 第三轮）。

来历：人工审计抽样 12 条被拦句，判出 4 条错误拒答（33%），其中 3 条是白名单太窄：

  「以下按渠道逐一列出，并在最后说明覆盖限制。」  `_NARRATIVE_RE` 的 12 字尾部上限
  「该核心节点具有以下效果：」                    `_NARRATIVE_RE` 锚定句首，引导词在句中
  「…均返回无匹配结果」「…与技能石数值无关」      `_DISCLOSURE_RE` 没覆盖这两种构式

**放宽白名单必然抬高漏出风险**，所以这里每一条放行用例都配一条「换个说法就必须拦」的
反例。`classify` 的默认值是事实句，这是本项目刻意选的安全侧，不能被这次放宽动摇。
"""

from __future__ import annotations

import unittest

from orchestrator.binding import classify, is_structural

TERMS = ("词缀", "抗性", "物品等级", "权重")
DOMAIN = ("猎首", "皮革腰带")


def kind(sentence: str, labels: tuple[str, ...] = ()) -> str:
    return classify(sentence, labels, TERMS, DOMAIN)


class DisclosureTest(unittest.TestCase):
    """缺据声明断言的是**信息缺失**，无法也不该绑定证据。"""

    def test_empty_query_result_is_disclosure(self) -> None:
        for s in (
            "根据结构化词缀库的直接查询，在腰带底材类别上均返回无匹配结果。",
            "该关键词在库中无命中。",
            "检索结果为空。",
            "未检索到相关条目。",
            "[E1]–[E8] 列出的腰带词缀组中没有一条涉及偷取。",
            "库里没有任何一项记载该机制。",
        ):
            with self.subTest(s=s):
                self.assertEqual("narrative", kind(s))

    def test_irrelevance_is_disclosure(self) -> None:
        for s in (
            "证据 [E2]–[E8] 全部为装备上 ColdDamage 词缀组的 T 级数据，与技能石数值无关。",
            "上述条目与本次提问不相关。",
        ):
            with self.subTest(s=s):
                self.assertEqual("narrative", kind(s))

    def test_hedged_disclosure_is_still_factual(self) -> None:
        """「证据里没写，一般来说是 100」不能靠前半句蒙过去——既有约束不能被削弱。"""
        self.assertEqual(
            "factual", kind("证据中没有记载，一般来说该词缀最高是 T1。")
        )

    def test_positive_claim_with_similar_wording_is_factual(self) -> None:
        """「无」字沾边不等于缺据声明：断言游戏机制的照旧要绑定。"""
        for s in (
            "该词缀无视抗性上限。",
            "腰带上的力量词缀有 10 档。",
        ):
            with self.subTest(s=s):
                self.assertEqual("factual", kind(s))


class ColonLeadInTest(unittest.TestCase):
    """冒号收尾意味着内容在后面，后面的句子各自过闸门。"""

    def test_mid_sentence_lead_in_passes(self) -> None:
        for s in (
            "该核心节点具有以下效果：",
            "显式词缀如下：",
            "按来源渠道分别列出：",
        ):
            with self.subTest(s=s):
                self.assertEqual("narrative", kind(s))

    def test_lead_in_with_number_is_factual(self) -> None:
        """含数值一律回到事实句校验——数值是编造的主要载体。"""
        for s in (
            "该节点提供 3 个效果：",
            "共 10 档，分别是：",
        ):
            with self.subTest(s=s):
                self.assertEqual("factual", kind(s))

    def test_long_sentence_is_not_a_lead_in(self) -> None:
        """超长句不算引导句：真断言通常更长，这条只服务短引导句。"""
        s = "这件武器围绕点燃机制设计并通过击杀被点燃的敌人回血，具体表现为以下几点："
        self.assertEqual("factual", kind(s))

    def test_assertion_without_colon_is_factual(self) -> None:
        """没有冒号就没有「内容在后面」这个结构性保证。"""
        self.assertEqual("factual", kind("该核心节点具有召唤图腾相关效果"))

    def test_fabricated_mechanic_with_colon_prefix_still_blocked(self) -> None:
        """放宽的是「冒号收尾的短句」，不是「冒号之前可以随便写」。

        `它会免疫冰冻，效果如下：` 这种把断言塞在冒号前的写法，长度已超出上限；
        真要更短（`会免疫冰冻：`）则命中不了 24 字以内 + 无数值 + 无领域词的组合时
        仍会被判事实句——这里钉住前一种。
        """
        self.assertEqual(
            "factual",
            kind("它还会让你免疫冰冻并且大幅提高你的移动速度，具体效果如下："),
        )


class DefaultIsStillFactualTest(unittest.TestCase):
    """这次放宽不能动摇「判不出类型即按事实句处理」这个安全侧默认。"""

    def test_bare_mechanic_claim(self) -> None:
        self.assertEqual("factual", kind("猎首还会让你免疫冰冻。"))

    def test_bare_list_item(self) -> None:
        self.assertEqual("factual", kind("- 类型：唯一装备"))

    def test_labelled_sentence_is_factual(self) -> None:
        """模型自己标了引用就是认为这是事实句，即使句式像引导句。"""
        self.assertEqual("factual", kind("该节点具有以下效果：", ("E1",)))


class StructuralScaffoldingTest(unittest.TestCase):
    """排版脚手架不是断言：标题里的章节序号、整行只有 URL。

    两条都是人工审计抽出来的错误拒答，与表格序号列同一族问题。
    """

    def test_numbered_heading_is_structural(self) -> None:
        for s in ("### 1. 装备词缀", "## 2、按来源渠道", "#### 3) 天赋树"):
            with self.subTest(s=s):
                self.assertTrue(is_structural(s))
                self.assertEqual("narrative", kind(s))

    def test_heading_with_real_number_is_still_factual(self) -> None:
        """`## 生命上限 1000` 的 1000 必须继续走事实句校验——数值是编造的主要载体。"""
        for s in ("## 生命上限 1000", "### 1. 生命上限 1000"):
            with self.subTest(s=s):
                self.assertFalse(is_structural(s))
                self.assertEqual("factual", kind(s))

    def test_bare_url_line_is_structural(self) -> None:
        for s in (
            "https://poedb.tw/cn/Skill_Gems",
            "<https://poedb.tw/cn/Skill_Gems>",
            "（https://www.poewiki.net/index.php?search=x）",
        ):
            with self.subTest(s=s):
                self.assertTrue(is_structural(s))
                self.assertEqual("narrative", kind(s))

    def test_url_with_surrounding_claim_is_factual(self) -> None:
        """URL 前后带断言就不是纯链接行了。"""
        self.assertEqual(
            "factual",
            kind("该词缀最高 T1，详见 https://poedb.tw/cn/Belts"),
        )


if __name__ == "__main__":
    unittest.main()
