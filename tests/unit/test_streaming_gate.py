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


def ev(label: str, text: str) -> binding.Evidence:
    return binding.Evidence(
        label=label, chunk_id=f"c-{label}", entity_id=None,
        title=f"条目{label}", url=f"https://poedb.tw/cn/{label}",
        source="POEDB", source_priority=1, text=text, can_support_facts=True,
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
    )

    def test_逐句判定与批量一致(self):
        e = [
            ev("E1", "猎首（Headhunter）：+(40-55) 力量，需求等级 40，基础抗性上限为 75%"),
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
                    [v.kind for v in got.verdicts],
                    [v.kind for v in batch.verdicts],
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
