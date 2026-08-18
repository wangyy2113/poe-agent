"""出口闸门对 Markdown 表格的处理。

来历：首次出口闸门评测（32 条真实回答）里 **192 句拦截有 92 句（48%）是表格行**——
分句按行切，于是一张表被撕成表头、分隔行与数据行，表头被判「事实句未绑定引用」，
序号列的行号被当成待溯源数值。那是错误拒答，而且把「引用有效率」压低了近一半，
掩盖了真实的漏出情况。

修法是**把表格整块当一个断言单位**：绑定标记出现在表内任何位置都对整张表生效，
数值仍逐个回填校验，只有确认为序号列的数字不参与。

最关键的一条仍然是流式与批量必须一致——表格必须等整块收齐才下发，
否则流式按行校验、批量按整块校验，同一个回答两条路径结果不同。
"""

from __future__ import annotations

import unittest

from orchestrator import binding

TERMS = ("词缀", "物品等级", "权重", "抗性")
DOMAIN = ("猎首", "皮革腰带", "腰带")


def ev(label: str, text: str) -> binding.Evidence:
    return binding.Evidence(
        label=label, chunk_id=f"c-{label}", entity_id=None, title=f"条目{label}",
        url=f"https://poedb.tw/cn/{label}", source="POEDB", source_priority=1,
        text=text, can_support_facts=True,
    )


def verify(text: str, evidence: list[binding.Evidence]) -> binding.BindingResult:
    return binding.verify(text, evidence, factual_terms=TERMS, domain_terms=DOMAIN)


def stream(text: str, evidence: list[binding.Evidence], chunk: int) -> binding.BindingResult:
    g = binding.StreamingGate(evidence, factual_terms=TERMS, domain_terms=DOMAIN)
    for i in range(0, len(text), chunk):
        g.feed(text[i:i + chunk])
    g.flush()
    return g.result()


EVIDENCE = [ev("E1", "腰带 · Strength\n| T 级 | 词缀文本 | 物品等级 | 生成权重 |\n"
                     "| T1/10 | +(56-60) 力量 | 85 | 1000 |\n"
                     "| T2/10 | +(51-55) 力量 | 82 | 1000 |")]

BOUND_TABLE = (
    "腰带上的力量词缀档位如下[[E1]]：\n\n"
    "| T 级 | 词缀文本 | 物品等级 | 生成权重 |\n"
    "|---|---|---|---|\n"
    "| T1/10 | +(56-60) 力量 | 85 | 1000 |\n"
    "| T2/10 | +(51-55) 力量 | 82 | 1000 |\n\n"
    "以上为该底材的完整梯度[[E1]]。\n"
)


class TableIsOneUnitTest(unittest.TestCase):
    def test_table_merged_into_single_piece(self) -> None:
        pieces = binding.split_sentences(BOUND_TABLE)
        tables = [p for p in pieces if p.lstrip().startswith("|")]
        self.assertEqual(1, len(tables), f"表格应合成一段，实际 {pieces}")
        self.assertIn("T2/10", tables[0], "整张表要在同一段里")

    def test_split_is_lossless(self) -> None:
        """拼回去必须与原文等价——下发内容不能因为分句而改变。"""
        self.assertEqual(BOUND_TABLE, "".join(binding.split_sentences(BOUND_TABLE)))

    def test_binding_inside_table_covers_whole_table(self) -> None:
        """标记标在表内任何一行，整张表都算已绑定。"""
        text = (
            "| T 级 | 词缀文本 | 物品等级 |\n"
            "|---|---|---|\n"
            "| T1/10 | +(56-60) 力量 | 85 |[[E1]]\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(0, res.blocked, [v.reason for v in res.verdicts if not v.ok])

    def test_table_without_any_binding_is_still_blocked(self) -> None:
        """放宽的是绑定的**位置**，不是要不要绑定。"""
        text = (
            "| T 级 | 词缀文本 | 物品等级 |\n"
            "|---|---|---|\n"
            "| T1/10 | +(56-60) 力量 | 85 |\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)
        self.assertIn("未绑定引用", res.verdicts[0].reason)

    def test_unsourced_number_in_table_still_blocked(self) -> None:
        """数值回填校验照旧生效 —— 编造的具体形态就是数字。"""
        text = (
            "| T 级 | 词缀文本 | 物品等级 |[[E1]]\n"
            "|---|---|---|\n"
            "| T1/10 | +(56-60) 力量 | 999 |\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)
        self.assertIn("999", res.verdicts[0].reason)


class IndexColumnTest(unittest.TestCase):
    def test_sequential_index_column_is_not_an_assertion(self) -> None:
        """序号列 1/2/3 的含义不来自证据，要求它可溯源必然失败（实测踩到）。"""
        text = (
            "| # | 词缀文本 | 物品等级 |[[E1]]\n"
            "|---|---|---|\n"
            "| 1 | +(56-60) 力量 | 85 |\n"
            "| 2 | +(51-55) 力量 | 82 |\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(0, res.blocked, [v.reason for v in res.verdicts if not v.ok])

    def test_real_values_are_not_mistaken_for_an_index(self) -> None:
        """物品等级这类真实数值不会连成 1,2,3，所以不会被当成序号放过。"""
        text = (
            "| 词缀文本 | 物品等级 |[[E1]]\n"
            "|---|---|\n"
            "| +(56-60) 力量 | 85 |\n"
            "| +(51-55) 力量 | 77 |\n"      # 77 不在证据里
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)
        self.assertIn("77", res.verdicts[0].reason)

    def test_single_row_table_has_no_index_column(self) -> None:
        """只有一行时无法判断是不是序号列，保守起见照常校验。"""
        ignored = binding._index_column_numbers(
            "| # | 词缀 |\n|---|---|\n| 1 | 力量 |\n"
        )
        self.assertEqual(set(), ignored)


class StreamingParityTest(unittest.TestCase):
    """表格必须等整块收齐才下发，否则两条路径判定分叉。"""

    CASES = (
        BOUND_TABLE,
        # 表格在最末尾，靠 flush 收尾
        "档位如下[[E1]]：\n\n| T 级 | 物品等级 |\n|---|---|\n| T1/10 | 85 |\n",
        # 表格之间夹正文
        "第一段[[E1]]。\n\n| T 级 | 物品等级 |\n|---|---|\n| T1/10 | 85 |\n\n"
        "第二段[[E1]]。\n\n| T 级 | 物品等级 |\n|---|---|\n| T2/10 | 82 |\n",
        # 无绑定的表格：拦截行为也必须一致
        "| T 级 | 物品等级 |\n|---|---|\n| T1/10 | 85 |\n",
    )

    def test_batch_and_stream_agree(self) -> None:
        for text in self.CASES:
            batch = verify(text, EVIDENCE)
            for chunk in (1, 2, 5, 17, 200):
                with self.subTest(chunk=chunk, text=text[:24]):
                    streamed = stream(text, EVIDENCE, chunk)
                    self.assertEqual(
                        batch.blocked, streamed.blocked,
                        f"拦截数不一致：批量 {batch.blocked} vs 流式 {streamed.blocked}",
                    )
                    self.assertEqual(
                        [(v.kind, v.ok) for v in batch.verdicts if v.text.strip()],
                        [(v.kind, v.ok) for v in streamed.verdicts if v.text.strip()],
                        "分类或放行结果不一致",
                    )

    def test_table_not_emitted_before_it_ends(self) -> None:
        """未收齐的表格不得下发——下发了就撤不回来。"""
        g = binding.StreamingGate(EVIDENCE, factual_terms=TERMS, domain_terms=DOMAIN)
        emitted: list[str] = []
        text = "| T 级 | 物品等级 |[[E1]]\n|---|---|\n| T1/10 | 85 |\n"
        for ch in text:
            emitted += [v.text for v in g.feed(ch)]
        self.assertFalse(
            any(v.lstrip().startswith("|") for v in emitted),
            f"表格在 flush 前就被下发了：{emitted}",
        )
        final = [v.text for v in g.flush()]
        self.assertTrue(any(v.lstrip().startswith("|") for v in final))


if __name__ == "__main__":
    unittest.main()
