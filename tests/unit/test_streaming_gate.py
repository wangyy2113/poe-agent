"""增量出口闸门的单元测试。

最重要的一条：**流式与批量的校验结果必须一致**。两条路径分叉的话，
同一个回答走流式和非流式会得到不同的拦截结果——那时哪个是对的都说不清。

其次是「未校验内容绝不下发」：任何 `feed()` 返回的句子都已经过校验。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator import binding  # noqa: E402

TERMS = ("词缀", "抗性", "物品等级", "权重")
DOMAIN = ("猎首", "皮革腰带")


def ev(
    label: str,
    text: str,
    *,
    claim_scope: str = "general",
    claim_data: dict | None = None,
) -> binding.Evidence:
    return binding.Evidence(
        label=label, chunk_id=f"c-{label}", entity_id=None,
        title=f"条目{label}", url=f"https://poedb.tw/cn/{label}",
        source="POEDB", source_priority=1, text=text, can_support_facts=True,
        claim_scope=claim_scope, claim_data=claim_data or {},
    )


def gate(evidence: list[binding.Evidence]) -> binding.StreamingGate:
    return binding.StreamingGate(
        evidence, factual_terms=TERMS, domain_terms=DOMAIN
    )


def stream(text: str, evidence: list[binding.Evidence], chunk: int = 3) -> binding.BindingResult:
    """按固定长度切片喂进去，模拟 token 流。"""
    g = gate(evidence)
    for i in range(0, len(text), chunk):
        g.feed(text[i:i + chunk])
    g.flush()
    return g.result()


class TestParityWithBatch(unittest.TestCase):
    """流式与批量必须给出同样的判定。"""

    CASES = (
        "猎首提供 +40 到 +55 力量[[E1]]。",
        "## 猎首（Headhunter）\n**基础信息**：需求等级 40[[E1]]。",
        "它还会让你免疫冰冻。猎首提供 +40 力量[[E1]]。",
        "该词缀提供 +72 力量[[E1]]。",
        "力量与敏捷各 +40[[E1;E2]]。",
        "基础抗性上限为 75%[[E1]]。因此上限为 79%[[E1]]。",
        "建议优先堆抗性。",
        "（本次证据中未包含猎首的掉落来源。）",
        "没有终止标点的尾句[[E1]]",
        "",
        # 表后单独一行的标记（第二十二轮）：批量把它并进表，流式必须**等这一行写完**
        # 才放行整块，否则流式判表未绑定、批量判已绑定——实测一致率掉到 64/66。
        "力量档位如下：\n\n| T 级 | 力量 |\n| --- | --- |\n| T1 | +40 |\n\n[[E1]]\n\n后续说明。",
        "| T 级 | 力量 |\n| --- | --- |\n| T1 | +40 |\n\n[[E1]]",
        # 表后跟的是**正文**（不是只有标记）：不并，两边都判表格未绑定
        "| T 级 | 力量 |\n| --- | --- |\n| T1 | +40 |\n\n[注] 见上表。",
        # 引导句 + 列表项：列表项继承引导句的标记
        "力量词缀如下[[E1]]：\n\n- +40 力量\n- +55 力量\n",
    )

    @staticmethod
    def _kinds(result: binding.BindingResult) -> list[str]:
        """判定序列，**跳过纯空白片段**。

        空行归到哪一段是排版差异（`档位如下：\n\n` 一段 vs 两段），
        评测口径也只把它算作「仅空白字符不同」。拿它当不一致会挡住真正的一致性用例。
        """
        return [v.kind for v in result.verdicts if v.text.strip()]

    def test_逐句判定与批量一致(self):
        e = [
            ev("E1", "猎首（Headhunter）：+(40-55) 力量，需求等级 40，基础抗性上限为 75%；"
                     "T1 档为 +40 力量"),
            ev("E2", "+(40-55) 敏捷"),
        ]
        for text in self.CASES:
            batch = binding.verify(
                text, e, factual_terms=TERMS, domain_terms=DOMAIN
            )
            for chunk in (1, 3, 7, 999):
                got = stream(text, e, chunk=chunk)
                self.assertEqual(
                    got.blocked, batch.blocked,
                    f"切片={chunk} 文本={text[:34]!r} 拦截数不一致",
                )
                self.assertEqual(
                    self._kinds(got), self._kinds(batch),
                    f"切片={chunk} 文本={text[:34]!r} 分类不一致",
                )
                self.assertEqual(
                    got.text, batch.text,
                    f"切片={chunk} 文本={text[:34]!r} 输出不一致",
                )

    def test_引用编号收集与批量一致(self):
        e = [ev("E1", "+(40-55) 力量"), ev("E2", "+(40-55) 敏捷")]
        text = "力量 +40[[E1]]。敏捷 +40[[E2]]。"
        batch = binding.verify(text, e, factual_terms=TERMS, domain_terms=DOMAIN)
        got = stream(text, e, chunk=2)
        self.assertEqual(got.used_labels, batch.used_labels)

    def test_工艺台目录范围在批量与流式均拦截且不下发Citation(self):
        evidence = [
            ev(
                "E1",
                "页面目录项；原始费用：5x 改造石；目录记录不证明给定装备状态下可执行。",
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
        cases = (
            "该工艺可在任意给定装备状态下执行[[E1]]。",
            "该工艺成功率是 5%[[E1]]。",
            "建议采用最快且最经济的路线[[E1]]。",
            "页面显示该工艺成功机会为 5%[[E1]]。",
            "目录不包含状态转移，因此它会删除现有前缀[[E1]]。",
            "目录不证明可执行，但它可用于当前装备[[E1]]。",
            "目录不证明可执行，故它可用于当前装备[[E1]]。",
            "建议采用这个目录选项[[E1]]。",
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
        for text in cases:
            batch = binding.verify(text, evidence, factual_terms=TERMS, domain_terms=DOMAIN)
            self.assertEqual(batch.blocked, 1)
            self.assertEqual(batch.used_labels, [])
            for chunk in (1, 3, 7):
                got = stream(text, evidence, chunk=chunk)
                self.assertEqual(got.blocked, batch.blocked)
                self.assertEqual(got.text, batch.text)
                self.assertEqual(got.used_labels, [])
                self.assertNotIn(text.replace("[[E1]]", ""), got.text)
    def test_双费用遗漏重复和乱序在批量与流式均拒绝(self):
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
        cases = (
            "页面列出的原始费用是 5x 灌顶石[[E1]]。",
            "页面列出的原始费用是 5x 玻璃弹珠[[E1]]。",
            "页面列出的原始费用是 5x 玻璃弹珠和 5x 灌顶石[[E1]]。",
            "页面列出的原始费用是 不包含 5x 灌顶石和 5x 玻璃弹珠[[E1]]。",
            "页面列出的原始费用是 5x 灌顶石和随便写和 5x 玻璃弹珠[[E1]]。",
        )
        for text in cases:
            batch = binding.verify(text, evidence, factual_terms=TERMS, domain_terms=DOMAIN)
            self.assertEqual(batch.blocked, 1)
            self.assertEqual(batch.used_labels, [])
            for chunk in (1, 3, 7):
                got = stream(text, evidence, chunk=chunk)
                self.assertEqual(got.blocked, 1)
                self.assertEqual(got.used_labels, [])
                self.assertEqual(got.text, batch.text)


class TestNoUnverifiedOutput(unittest.TestCase):
    """未校验内容绝不下发。"""

    def test_句子未说完不产出(self):
        e = [ev("E1", "+(40-55) 力量")]
        g = gate(e)
        self.assertEqual(g.feed("猎首提供 "), [])
        self.assertEqual(g.feed("+40 力量"), [])
        self.assertEqual(g.feed("[[E1]]"), [])
        out = g.feed("。")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].ok)

    def test_绑定标记未闭合时不切句(self):
        """`[[E1;E2]]` 只写到 `[[E1;` 时，那个分号会被当成句末标点。
        切开会让标记失效，有据句被判成未绑定引用——错误拒答。"""
        e = [ev("E1", "+(40-55) 力量"), ev("E2", "+(40-55) 敏捷")]
        g = gate(e)
        self.assertEqual(g.feed("力量与敏捷各 +40[[E1;"), [])
        self.assertEqual(g.feed("E2]]"), [])
        out = g.feed("。")
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].ok, out[0].reason)
        self.assertEqual(len(out[0].labels), 2)

    def test_产出的每一句都已判定(self):
        e = [ev("E1", "+(40-55) 力量")]
        g = gate(e)
        produced: list[binding.SentenceVerdict] = []
        for ch in "第一句无据。力量 +40[[E1]]。":
            produced += g.feed(ch)
        produced += g.flush()
        for v in produced:
            self.assertIn(v.kind, ("factual", "derived", "recommendation", "narrative"))
            self.assertTrue(v.reason, "每一句都必须有判定理由")

    def test_无据句在流式下同样被拦(self):
        e = [ev("E1", "无关内容")]
        g = gate(e)
        out: list[binding.SentenceVerdict] = []
        for ch in "猎首还会让你免疫冰冻。":
            out += g.feed(ch)
        out += g.flush()
        self.assertEqual(g.blocked, 1)
        self.assertTrue(g.gaps)
        self.assertIn("已被拦截", "".join(v.output for v in out))


class TestFlush(unittest.TestCase):
    def test_收尾处理无终止标点的尾句(self):
        e = [ev("E1", "+(40-55) 力量")]
        g = gate(e)
        g.feed("力量 +40[[E1]]")
        self.assertEqual(g.verdicts, [])
        out = g.flush()
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].ok)

    def test_收尾时未闭合标记也要交出(self):
        """模型中途断流留下 `[[E1` 时不能把这段吞掉——
        吞掉等于静默丢内容，比拦截更糟。"""
        e = [ev("E1", "+(40-55) 力量")]
        g = gate(e)
        g.feed("力量 +40[[E1")
        out = g.flush()
        self.assertEqual(len(out), 1)
        self.assertIn("力量", out[0].text)

    def test_空输入收尾不产出(self):
        g = gate([ev("E1", "x")])
        self.assertEqual(g.flush(), [])
        self.assertEqual(g.result().blocked, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
