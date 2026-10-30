"""断言—引用绑定校验的单元测试（任务 3.6 要求的四类覆盖 + 实际会踩的边界）。

    python -m unittest discover -s tests -t .

用标准库 unittest，不引入 pytest：既有手段已够用，新增依赖要先确认必要性。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator import binding  # noqa: E402

TERMS = ("词缀", "抗性", "物品等级", "权重", "升华", "隐式")


def ev(
    label: str,
    text: str,
    *,
    priority: int = 1,
    facts: bool = True,
    claim_scope: str = "general",
    claim_data: dict | None = None,
) -> binding.Evidence:
    return binding.Evidence(
        label=label,
        chunk_id=f"chunk-{label}",
        entity_id=None,
        title=f"条目 {label}",
        url=f"https://poedb.tw/cn/{label}",
        source="POEDB" if facts else "Community",
        source_priority=priority,
        text=text,
        can_support_facts=facts,
        claim_scope=claim_scope,
        claim_data=claim_data or {},
    )


#: 领域词（生产中来自 retrieval/vocab.py 生成的实体名与底材类别）
DOMAIN = ("猎首", "冈姆的壮志", "琥珀护符", "腰带")


def run(answer: str, evidence: list[binding.Evidence]) -> binding.BindingResult:
    return binding.verify(answer, evidence, factual_terms=TERMS, domain_terms=DOMAIN)


class TestBlocking(unittest.TestCase):
    """该拦的必须拦住。漏出一条无据断言即为严重缺陷（需求 1.7）。"""

    def test_编造机制句无标注被拦(self):
        e = [ev("E1", "猎首：当你击败稀有怪物时，会获得该怪物属性修正 60 秒。")]
        r = run("猎首还会让你免疫冰冻。", e)
        self.assertEqual(r.blocked, 1)
        self.assertIn("未绑定引用", r.verdicts[0].reason)
        self.assertNotIn("免疫冰冻", r.text)

    def test_带数值的陈述句无标注被拦(self):
        e = [ev("E1", "+(40-55) 力量")]
        r = run("这条词缀能给到 80 点力量。", e)
        self.assertEqual(r.blocked, 1)

    def test_标了引用但数值编造仍被拦(self):
        """只检查「有没有标注」是不够的——模型可以随手标一个蒙过去。"""
        e = [ev("E1", "+(40-55) 力量，物品等级 30")]
        r = run("该词缀提供 +72 力量[[E1]]。", e)
        self.assertEqual(r.blocked, 1)
        self.assertIn("72", r.verdicts[0].reason)

    def test_引用编号无效被拦(self):
        e = [ev("E1", "+(40-55) 力量")]
        r = run("该词缀提供 +45 力量[[E7]]。", e)
        self.assertEqual(r.blocked, 1)
        self.assertIn("无效", r.verdicts[0].reason)

    def test_模糊限定语被拦(self):
        """需求 1.5：不得用「一般来说」把推断包装成模糊表述。"""
        e = [ev("E1", "最大抗性上限为 75%")]
        r = run("一般来说抗性上限是 75%[[E1]]。", e)
        self.assertEqual(r.blocked, 1)
        self.assertIn("模糊限定语", r.verdicts[0].reason)

    def test_仅社区来源不能支撑事实断言(self):
        """需求 2.2：P7 社区来源不得支撑任何事实断言。"""
        e = [ev("E1", "有人说该词缀权重是 500", priority=7, facts=False)]
        r = run("该词缀的生成权重是 500[[E1]]。", e)
        self.assertEqual(r.blocked, 1)
        self.assertIn("社区级", r.verdicts[0].reason)

    def test_无领域词无数值的编造机制句也被拦(self):
        """最容易漏的一类：既无数值也无配置词表命中的编造机制描述。
        判不出类型时必须默认按事实句处理，不能当过渡句放行。"""
        e = [ev("E1", "无关内容")]
        r = run("它还会让你免疫冰冻。", e)
        self.assertEqual(r.blocked, 1, "判不出类型的陈述句被当成过渡句放行了")



    def test_无证据时事实句全部被拦(self):
        r = run("猎首的物品等级需求是 40。", [])
        self.assertEqual(r.blocked, 1)

    def test_拦截会产生缺口台账条目(self):
        e = [ev("E1", "+(40-55) 力量")]
        r = run("这条词缀还能给暴击伤害。", e)
        self.assertTrue(r.gaps)
        self.assertEqual(len(r.gaps), r.blocked)


class TestBenchCatalogClaimScope(unittest.TestCase):
    """目录 Citation 不能被反向用来证明页面未提供的制作执行语义。"""

    def setUp(self) -> None:
        self.evidence = [
            ev(
                "E1",
                "页面目录项。效果原文：+(15-25) 最大生命。适用 ItemClasses：项链。"
                "原始费用：5x 改造石。解锁：Default。"
                "目录记录不证明给定装备状态下可执行。",
                claim_scope="bench_catalog",
                claim_data={
                    "effect_text": "+(15-25) 最大生命",
                    "item_classes": ["项链"],
                    "raw_costs": [{"amount": 5, "currency": "改造石"}],
                    "unlock_text": "Default",
                    "unlock_hint": None,
                },
            )
        ]

    def test_执行语义概率成本与路线均被拦(self):
        forbidden = (
            "该工艺可在任意给定装备状态下执行[[E1]]。",
            "该工艺会移除当前词缀[[E1]]。",
            "制作步骤是先使用改造石再应用该工艺[[E1]]。",
            "该工艺的成功率是 5%[[E1]]。",
            "该工艺的市场成本是 5 混沌石[[E1]]。",
            "这是最快且最经济的制作路线[[E1]]。",
            "该工艺允许突破 crafted-mod 上限[[E1]]。",
            "建议采用最经济路线[[E1]]。",
            "页面显示该工艺成功机会为 5%[[E1]]。",
            "目录不包含状态转移，因此它会删除现有前缀[[E1]]。",
            "目录不证明可执行，但它可用于当前装备[[E1]]。",
            "目录不证明可执行，故它可用于当前装备[[E1]]。",
            "建议采用这个目录选项[[E1]]。",
        )
        for sentence in forbidden:
            with self.subTest(sentence=sentence):
                result = run(sentence, self.evidence)
                self.assertEqual(result.blocked, 1)
                self.assertEqual(result.used_labels, [])
                self.assertNotIn(sentence.replace("[[E1]]", ""), result.text)
                self.assertIn("目录证据", result.verdicts[0].reason)

    def test_目录明确字段与边界声明可以通过(self):
        allowed = (
            "POEDB 页面目录列出了该选项[[E1]]。",
            "页面效果原文是 +(15-25) 最大生命[[E1]]。",
            "页面列出的原始费用是 5x 改造石[[E1]]。",
            "页面列出的适用 ItemClasses 是项链[[E1]]。",
            "页面列出的解锁信息是 Default[[E1]]。",
            "该目录不证明它在给定装备状态下可执行[[E1]]。",
        )
        for sentence in allowed:
            with self.subTest(sentence=sentence):
                result = run(sentence, self.evidence)
                self.assertEqual(
                    result.blocked,
                    0,
                    [verdict.reason for verdict in result.verdicts],
                )
                self.assertEqual(result.used_labels, ["E1"])

    def test_目录字段不能夹带错误值或跨字段借数字(self):
        invalid = (
            "页面列出的原始费用是 15x 改造石[[E1]]。",
            "页面列出的适用 ItemClasses 是项链和弓[[E1]]。",
            "页面列出的解锁信息是 Default错误[[E1]]。",
            "页面列出的原始费用是 5x 改造石和 5x 改造石[[E1]]。",
            "页面列出的原始费用是 不包含 5x 改造石[[E1]]。",
            "页面列出的原始费用是 5x 改造石和错误说明[[E1]]。",
            "POEDB 页面目录列出了该选项，故它可用于当前装备[[E1]]。",
            "它可用于当前装备，页面效果原文是 +(15-25) 最大生命[[E1]]。",
            "它可用于当前装备，页面列出的原始费用是 5x 改造石[[E1]]。",
            "它可用于当前装备，页面列出的适用 ItemClasses 是项链[[E1]]。",
            "它可用于当前装备，页面列出的解锁信息是 Default[[E1]]。",
        )
        for sentence in invalid:
            with self.subTest(sentence=sentence):
                result = run(sentence, self.evidence)
                self.assertEqual(result.blocked, 1)
                self.assertEqual(result.used_labels, [])
                self.assertIn("目录证据", result.verdicts[0].reason)

    def test_双费用必须按页面顺序完整声明且保留重数(self):
        evidence = [
            ev(
                "E1",
                "原始费用：5x 灌顶石 + 5x 玻璃弹珠",
                claim_scope="bench_catalog",
                claim_data={
                    "effect_text": "跟接邻的药剂一起自动使用",
                    "item_classes": ["药剂"],
                    "raw_costs": [
                        {"amount": 5, "currency": "灌顶石"},
                        {"amount": 5, "currency": "玻璃弹珠"},
                    ],
                    "unlock_text": "移动速度和药剂附魔 - 等级 3",
                    "unlock_hint": None,
                },
            )
        ]
        valid = run(
            "页面列出的原始费用是 5x 灌顶石和 5x 玻璃弹珠[[E1]]。",
            evidence,
        )
        self.assertEqual(valid.blocked, 0)
        self.assertEqual(valid.used_labels, ["E1"])
        for sentence in (
            "页面列出的原始费用是 5x 灌顶石[[E1]]。",
            "页面列出的原始费用是 5x 玻璃弹珠[[E1]]。",
            "页面列出的原始费用是 5x 玻璃弹珠和 5x 灌顶石[[E1]]。",
            "页面列出的原始费用是 不包含 5x 灌顶石和 5x 玻璃弹珠[[E1]]。",
            "页面列出的原始费用是 5x 灌顶石和随便写和 5x 玻璃弹珠[[E1]]。",
        ):
            with self.subTest(sentence=sentence):
                result = run(sentence, evidence)
                self.assertEqual(result.blocked, 1)
                self.assertEqual(result.used_labels, [])

    def test_真实效果原文中的几率和移除不会被误拦(self):
        cases = (
            ("(15-20)% 几率避免被晕眩", "页面效果原文是 (15-20)% 几率避免被晕眩[[E1]]。"),
            ("移除工艺词缀", "页面效果原文是移除工艺词缀[[E1]]。"),
        )
        for effect_text, sentence in cases:
            evidence = [
                ev(
                    "E1",
                    f"效果原文：{effect_text}",
                    claim_scope="bench_catalog",
                    claim_data={
                        "effect_text": effect_text,
                        "item_classes": ["物品"],
                        "raw_costs": [{"amount": 1, "currency": "重铸石"}],
                        "unlock_text": "Default",
                        "unlock_hint": None,
                    },
                )
            ]
            with self.subTest(effect_text=effect_text):
                result = run(sentence, evidence)
                self.assertEqual(result.blocked, 0, [v.reason for v in result.verdicts])
                self.assertEqual(result.used_labels, ["E1"])

    def test_混合普通证据也不能洗白错误绑定的目录Citation(self):
        evidence = self.evidence + [ev("E2", "另一条普通证据")]
        result = run("该工艺可在当前装备上使用[[E1,E2]]。", evidence)
        self.assertEqual(result.blocked, 1)
        self.assertEqual(result.used_labels, [])


class TestPassing(unittest.TestCase):
    """该放的必须放行。错误拒答与漏出要成对压制（需求 16.7）。"""

    def test_合法事实句放行(self):
        e = [ev("E1", "猎首（Headhunter）：+(40-55) 力量，+(40-55) 敏捷")]
        r = run("猎首提供 +40 到 +55 力量[[E1]]。", e)
        self.assertEqual(r.blocked, 0)
        self.assertEqual(r.used_labels, ["E1"])

    def test_游戏名称中的一不是装备数值但真实等级仍校验(self):
        e = [ev("E1", "猎首：唯一装备腰带，底材为皮革腰带，需求等级 40")]
        good = run("猎首是 POE1 的唯一装备腰带，需求等级 40[[E1]]。", e)
        bad = run("猎首是 POE1 的唯一装备腰带，需求等级 41[[E1]]。", e)
        self.assertEqual(0, good.blocked)
        self.assertEqual(1, bad.blocked)
        self.assertIn("41", bad.verdicts[0].reason)

    def test_证据区间覆盖句中具体值时放行(self):
        """模型从区间 (40-55) 里取具体值 47 是合理表述，不是编造。"""
        e = [ev("E1", "该词缀为 +(40-55) 力量")]
        r = run("这条词缀实际取到了 +47 力量[[E1]]。", e)
        self.assertEqual(r.blocked, 0)

    def test_合法建议句放行且不强制绑定(self):
        """需求 15.4：建议类允许，只需可辨识为建议。"""
        e = [ev("E1", "抗性上限 75%")]
        r = run("建议你优先把三抗堆满再考虑伤害。", e)
        self.assertEqual(r.blocked, 0)
        self.assertEqual(r.verdicts[0].kind, "recommendation")

    def test_合法派生句放行(self):
        """结论逐值出现在确定性计算器证据中，才允许作为数值派生。"""
        e = [
            ev("E1", "基础抗性上限为 75%，该装备提供 +4% 最大抗性上限"),
            ev(
                "E2",
                "输入：75% + 4%；计算结果：最大抗性 79%",
                claim_scope="calculation",
            ),
        ]
        r = run(
            "基础抗性上限为 75%[[E1]]。该装备提供 +4% 最大抗性上限[[E1]]。"
            "因此抗性上限合计为 79%[[E2]]。",
            e,
        )
        self.assertEqual(r.blocked, 0, [v.reason for v in r.verdicts if not v.ok])
        self.assertEqual(r.verdicts[-1].kind, "derived")

    def test_事实输入不能冒充计算器输出(self):
        """即使 75 与 4 都有来源，也不能由模型自行心算出 79。"""
        e = [ev("E1", "基础抗性上限为 75%，该装备提供 +4% 最大抗性上限")]
        r = run(
            "基础抗性上限为 75%[[E1]]。该装备提供 +4% 最大抗性上限[[E1]]。"
            "因此抗性上限合计为 79%[[E1]]。",
            e,
        )
        self.assertEqual(r.blocked, 1)
        self.assertIn("计算器输出", r.verdicts[-1].reason)

    def test_派生数值必须精确出现在计算器输出而非仅落在区间(self):
        e = [ev("E1", "计算结果范围为 70-90%", claim_scope="calculation")]
        r = run("所以最终结果是 79%[[E1]]。", e)
        self.assertEqual(r.blocked, 1)
        self.assertIn("79", r.verdicts[0].reason)

    def test_过渡句放行(self):
        e = [ev("E1", "任意内容")]
        r = run("下面分两点说明。", e)
        self.assertEqual(r.blocked, 0)
        self.assertEqual(r.verdicts[0].kind, "narrative")

    def test_千分位与小数归一(self):
        e = [ev("E1", "+1000 最大生命，每秒再生 3.5 生命")]
        r = run("它提供 +1,000 最大生命与每秒 3.5 生命再生[[E1]]。", e)
        self.assertEqual(r.blocked, 0)

    def test_多引用写法兼容(self):
        """格式差异不该导致有据句被拦——那是错误拒答。"""
        e = [ev("E1", "+(40-55) 力量"), ev("E2", "+(40-55) 敏捷")]
        for mark in ("[[E1,E2]]", "[[E1，E2]]", "[[E1;E2]]"):
            r = run(f"力量与敏捷各 +40{mark}。", e)
            self.assertEqual(r.blocked, 0, f"写法 {mark} 被误拦")
            self.assertEqual(len(r.verdicts[0].labels), 2)


class TestMarkdownStructure(unittest.TestCase):
    """Markdown 排版不是断言。实测一次回答 16 句里 5 句被拦，全是标题与分隔线，
    答案结构被撕碎——那是严重的错误拒答。"""

    def test_标题与标签不被当作断言(self):
        e = [ev("E1", "猎首：+(40-55) 力量")]
        for line in (
            "## 猎首（Headhunter）",
            "### 补充：与仿品的区别",
            "**隐式词缀（Implicit）**",
            "**显式词缀**：",
            "---",
            "***",
            "- ",
            "1.",
        ):
            r = run(line, e)
            self.assertEqual(r.blocked, 0, f"排版元素被误拦：{line!r}")
            self.assertEqual(r.verdicts[0].kind, "narrative", line)

    def test_带数字的标题仍走事实句校验(self):
        """`## 生命上限 1000` 是在断言一个数值，不能因为是标题就放行。"""
        e = [ev("E1", "无相关数值")]
        r = run("## 生命上限 1000", e)
        self.assertEqual(r.blocked, 1)

    def test_列表项有内容时仍校验(self):
        e = [ev("E1", "+(40-55) 力量")]
        r = run("- 该词缀提供 +99 力量[[E1]]", e)
        self.assertEqual(r.blocked, 1, "带内容的列表项应当校验数值")

    def test_纯标点碎片不被当作断言(self):
        """括号内含句号时分句会切出孤立的「）」，
        按事实句处理会在答案里插一条莫名的拦截说明。"""
        e = [ev("E1", "内容")]
        for frag in ("）", "。", "  ", "…", "——"):
            r = run(frag, e)
            self.assertEqual(r.blocked, 0, f"标点碎片被误拦：{frag!r}")


class TestDisclosure(unittest.TestCase):
    """缺据声明断言的是信息**缺失**，无法也不该绑定证据。
    实测模型主动声明缺据时被拦掉了，那是在惩罚正确行为。"""

    def test_缺据声明不要求绑定(self):
        e = [ev("E1", "猎首：+(40-55) 力量")]
        for line in (
            "（本次证据中未包含猎首的掉落来源、适用版本与市场价格信息。）",
            "以上证据没有涉及猎首的掉落来源，如需这些内容需另行查证。",
            "知识库中没有该词缀的权重数据，因此不给出制作成功率。",
        ):
            r = run(line, e)
            self.assertEqual(r.blocked, 0, f"缺据声明被误拦：{line[:24]}")

    def test_缺据声明里夹模糊推断仍被拦(self):
        """「证据里没写，一般来说是 100」不能靠缺据句式整句蒙过去。"""
        e = [ev("E1", "无相关数值")]
        r = run("证据中没有给出上限，一般来说是 100。", e)
        self.assertEqual(r.blocked, 1)

    def test_缺据声明的各种措辞都要覆盖(self):
        """实测两轮各撞一次同类假阳性，只是措辞不同：
        「未包含」→「不包含」、「本次证据」→「本轮证据」。枚举词表补不完，
        所以改成按构式匹配。"""
        e = [ev("E1", "猎首：+(40-55) 力量")]
        for line in (
            "**说明缺口**：本轮证据中不包含猎首的掉落来源、市场价格。",
            "当前证据没有涉及该词缀的权重数据。",
            "以上证据未记载适用版本。",
            "证据里不含掉落来源。",
            "知识库中未收录该条目。",
        ):
            r = run(line, e)
            self.assertEqual(r.blocked, 0, f"缺据声明被误拦：{line[:26]}")

    def test_面向用户的操作指引不要求绑定(self):
        """「若你需要这些内容，请单独提问」讲的是怎么继续用助手，不是断言事实。"""
        e = [ev("E1", "猎首：+(40-55) 力量")]
        for line in (
            "若你需要这些内容，请单独提问以便检索对应条目。",
            "如需其他部位的词缀，请另行提问。",
            "可以再问我具体某一条的 T 级分布。",
        ):
            r = run(line, e)
            self.assertEqual(r.blocked, 0, f"操作指引被误拦：{line[:24]}")

    def test_带数字的指引句仍走事实句校验(self):
        """「如需满抗请堆到 75%」是建议或事实，不是操作指引，不能因句式豁免。"""
        e = [ev("E1", "无相关数值")]
        r = run("如需满抗，请把三抗堆到 88%。", e)
        self.assertEqual(r.blocked, 1)


class TestSplitting(unittest.TestCase):
    def test_分句后拼回与原文等价(self):
        """出口按句缓冲下发，切一次再拼一次不能改动内容，
        否则用户看到的与校验过的不是同一份文本。"""
        for text in (
            "第一句。第二句！第三句？",
            "带换行\n第二行。",
            "无终止标点的尾句",
            "数值 3.5 秒不该被切开。下一句。",
            "标记在句尾[[E1]]。下一句[[E2]]。",
            "",
        ):
            self.assertEqual("".join(binding.split_sentences(text)), text)

    def test_小数不被当作句子边界(self):
        parts = binding.split_sentences("冷却 3.5 秒。")
        self.assertEqual(len(parts), 1)


class TestOrderedListMarkers(unittest.TestCase):
    """有序列表的序号是排版，不是断言——成对写：只删序号，不放行正文里的同一个数。"""

    def test_列表序号不要求溯源(self):
        """实测：五步推导里只有第 3 步被拦，因为 1、2、4、5 碰巧出现在证据原文里。"""
        e = [ev("E1", "火焰抗性加总 120%，实际生效 60%，承受倍率 0.40。")]
        answer = (
            "1. 火焰抗性加总：120%[[E1]]\n"
            "2. 实际生效抗性：60%[[E1]]\n"
            "3. 承受倍率：0.40[[E1]]\n"
        )
        r = run(answer, e)
        self.assertEqual(0, r.blocked, [v.reason for v in r.verdicts if not v.ok])

    def test_序号之外的同一个数字照旧要溯源(self):
        """按值豁免会把正文里的 3 也放过；按位置删除不会。"""
        e = [ev("E1", "猎首的效果持续 60 秒。")]
        r = run("3. 这个效果持续 3 秒[[E1]]。", e)
        self.assertEqual(1, r.blocked)
        self.assertIn("3", r.verdicts[0].reason)

    def test_句中出现的编号不被当成列表序号(self):
        e = [ev("E1", "该词缀共有 5 档。")]
        r = run("这条词缀有 4 档[[E1]]。", e)
        self.assertEqual(1, r.blocked)


class TestTypographicSigns(unittest.TestCase):
    """排版负号必须与 ASCII 负号视为同一个数——成对写：归一不能顺带放宽。

    2026-08-19 第二十轮实测：模型写「抗性下限为 −200%」（U+2212），证据原文写
    `-200%`，提取器读出 `200`、在证据里找不到，于是把一句完全正确的话判成编造。
    抗性、减抗、诅咒这些场景全是负数，不归一等于对整类负数误拦。
    """

    def test_模型用排版负号仍能溯源(self):
        e = [ev("E1", "抗性下限为 -200%，火焰抗性上限 75%。")]
        r = run("抗性下限是 −200%[[E1]]。", e)
        self.assertEqual(0, r.blocked, [v.reason for v in r.verdicts])

    def test_证据用排版负号也能被溯源(self):
        """反方向同样成立：POEDB 中文页用全角符号是常态。"""
        e = [ev("E1", "该词缀为 －60% 元素抗性。")]
        r = run("这条词缀是 -60% 元素抗性[[E1]]。", e)
        self.assertEqual(0, r.blocked, [v.reason for v in r.verdicts])

    def test_归一不放行数值不符的断言(self):
        e = [ev("E1", "抗性下限为 -200%。")]
        r = run("抗性下限是 −300%[[E1]]。", e)
        self.assertEqual(1, r.blocked)
        self.assertIn("300", r.verdicts[0].reason)

    def test_区间连接号不当成负号(self):
        """`40–55` 里的连接号若被当成负号，会读出 −55 并把区间句判成编造。"""
        e = [ev("E1", "力量词缀 T1 的区间是 40 到 55。")]
        r = run("这条词缀的区间是 40–55[[E1]]。", e)
        self.assertEqual(0, r.blocked, [v.reason for v in r.verdicts])


class TestEvidence(unittest.TestCase):
    def test_编号从E1开始且去重(self):
        cites = [
            {"chunk_id": "a", "title": "甲", "url": "u1", "source": "POEDB",
             "source_priority": 1, "text": "x"},
            {"chunk_id": "a", "title": "甲", "url": "u1", "source": "POEDB",
             "source_priority": 1, "text": "x"},
            {"chunk_id": "b", "title": "乙", "url": "u2", "source": "POEDB",
             "source_priority": 1, "text": "y"},
        ]
        got = binding.number_evidence(cites)
        self.assertEqual([e.label for e in got], ["E1", "E2"])

    def test_编号时保留claim_scope且旧Citation默认general(self):
        got = binding.number_evidence(
            [
                {
                    "chunk_id": "bench",
                    "title": "工艺台",
                    "url": "u",
                    "source": "POEDB",
                    "source_priority": 1,
                    "text": "目录项",
                    "claim_scope": "bench_catalog",
                    "claim_data": {"effect_text": "最大生命"},
                },
                {
                    "chunk_id": "normal",
                    "title": "普通证据",
                    "url": "u2",
                    "source": "POEDB",
                    "source_priority": 1,
                    "text": "普通内容",
                },
            ]
        )
        self.assertEqual([e.claim_scope for e in got], ["bench_catalog", "general"])
        self.assertEqual(got[0].claim_data["effect_text"], "最大生命")
        self.assertIn("仅可支撑目录存在", binding.render_evidence_block(got))

    def test_派生通道自动固化为计算器证据范围(self):
        got = binding.number_evidence(
            [
                {
                    "chunk_id": "calc",
                    "title": "抗性计算",
                    "url": "u",
                    "source": "Path of Building",
                    "source_priority": 2,
                    "text": "计算结果 79%",
                    "channel": "derived",
                },
                {
                    "chunk_id": "pool",
                    "title": "条件池计算",
                    "url": "u2",
                    "source": "POEDB",
                    "source_priority": 1,
                    "text": "条件概率 12.5%",
                    "channel": "derived",
                    "claim_scope": "affix_pool_odds",
                },
            ]
        )
        self.assertEqual(got[0].claim_scope, "calculation")
        self.assertEqual(got[0].channel, "derived")
        self.assertEqual(got[1].claim_scope, "affix_pool_odds")
        self.assertEqual(got[1].channel, "derived")
        self.assertIn("确定性计算器输出", binding.render_evidence_block(got))
        result = run("因此最大抗性为 79%[[E1]]。", got)
        self.assertEqual(result.blocked, 0)

    def test_证据块不含URL(self):
        """URL 给了模型就会自己编引用格式；引用由代码在出口装配。"""
        e = [ev("E1", "内容")]
        block = binding.render_evidence_block(e)
        self.assertNotIn("https://", block)
        self.assertIn("[E1]", block)

    def test_低优先级来源在证据块里被标注(self):
        e = [ev("E1", "内容", priority=7, facts=False)]
        self.assertIn("不可支撑事实断言", binding.render_evidence_block(e))


if __name__ == "__main__":
    unittest.main(verbosity=2)
