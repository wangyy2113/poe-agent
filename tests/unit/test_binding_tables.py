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


class TableInheritsPrecedingBindingTest(unittest.TestCase):
    """表格块继承紧邻前一句的绑定（2026-08-18）。

    模型的自然写法是「档位如下[[E1]]：」紧跟一张表。引导句绑定了证据、表格是它的续写，
    但分句后表格成了独立单位、自己没有标记，于是整张表被判「未绑定引用」。
    实测 104 句拦截里 17 句是这种形态，其中 13 句紧邻前一句有绑定可继承，
    且继承后**全部通过**数值校验——说明拦它们是错误拒答。

    关键是继承只提供**编号**：数值回填校验照旧跑，表里编造的数字仍然会被拦。
    """

    def test_table_inherits_lead_in_binding(self) -> None:
        text = (
            "腰带上的力量词缀档位如下[[E1]]：\n\n"
            "| T 级 | 词缀文本 | 物品等级 |\n"
            "|---|---|---|\n"
            "| T1/10 | +(56-60) 力量 | 85 |\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(0, res.blocked, [v.reason for v in res.verdicts if not v.ok])
        table = next(v for v in res.verdicts if v.text.lstrip().startswith("|"))
        self.assertEqual(("E1",), table.labels, "表格应继承引导句的编号")

    def test_blank_line_does_not_break_adjacency(self) -> None:
        """`如下[[E1]]：\n\n| 表 |` 中间那个空行是排版，不该让表格失去上文。"""
        text = "档位如下[[E1]]：\n\n\n| T 级 | 物品等级 |\n|---|---|\n| T1/10 | 85 |\n"
        self.assertEqual(0, verify(text, EVIDENCE).blocked)

    def test_fabricated_number_in_inherited_table_still_blocked(self) -> None:
        """**继承编号救不了编造的数字。** 这条是整个放宽的安全前提。"""
        text = (
            "档位如下[[E1]]：\n\n"
            "| T 级 | 词缀文本 | 物品等级 |\n|---|---|---|\n"
            "| T1/10 | +(56-60) 力量 | 999 |\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)
        self.assertIn("999", res.verdicts[-1].reason if not res.verdicts[-1].ok else
                      next(v.reason for v in res.verdicts if not v.ok))

    def test_unbound_lead_in_gives_nothing_to_inherit(self) -> None:
        """引导句自己没绑定时，表格无处可继承，仍然要拦。"""
        text = "档位如下：\n\n| T 级 | 物品等级 |\n|---|---|\n| T1/10 | 85 |\n"
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)

    def test_blocked_lead_in_cannot_be_inherited_from(self) -> None:
        """被拦的句子不能当依据来源——否则一句无据断言能把绑定「传染」给整张表。"""
        text = (
            "该词缀最高档是 T1[[E99]]。\n\n"          # 编号无效 → 被拦
            "| T 级 | 物品等级 |\n|---|---|\n| T1/10 | 85 |\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(2, res.blocked, "引导句与表格都该被拦")

    def test_own_marker_wins_over_inheritance(self) -> None:
        """表格自己写了标记时以它自己的为准，不被前一句覆盖。"""
        text = (
            "档位如下[[E1]]：\n\n"
            "| T 级 | 物品等级 |[[E99]]\n|---|---|\n| T1/10 | 85 |\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)
        self.assertIn("E99", next(v.reason for v in res.verdicts if not v.ok))

    def test_non_table_sentence_does_not_inherit(self) -> None:
        """只给表格开这个口子：普通句子不是引导句的续写。"""
        text = "档位如下[[E1]]：\n\n该词缀在弓上是 T5 档。\n"
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)

    def test_streaming_matches_batch_on_inheritance(self) -> None:
        """继承必须在流式侧同样成立，否则两条路径判定分叉。"""
        text = (
            "腰带上的力量词缀档位如下[[E1]]：\n\n"
            "| T 级 | 词缀文本 | 物品等级 |\n|---|---|---|\n"
            "| T1/10 | +(56-60) 力量 | 85 |\n\n以上为完整梯度[[E1]]。\n"
        )
        batch = verify(text, EVIDENCE)
        for chunk in (1, 3, 11, 200):
            with self.subTest(chunk=chunk):
                streamed = stream(text, EVIDENCE, chunk)
                self.assertEqual(batch.blocked, streamed.blocked)
                self.assertEqual(
                    [(v.kind, v.ok, v.labels) for v in batch.verdicts if v.text.strip()],
                    [(v.kind, v.ok, v.labels) for v in streamed.verdicts if v.text.strip()],
                )


class TableTrailingMarkerTest(unittest.TestCase):
    """表后**单独一行**的标记属于那张表（2026-08-22 第二十二轮）。

    模型的自然写法之一是贴出表、把 `[[E1]]` 写在表后一行。表格继承只看前一句，
    于是整张表被判未绑定——而它是有据的。判据很紧：那一行**除了标记什么都没有**。
    """

    def test_marker_only_line_after_table_binds_it(self) -> None:
        text = (
            "| T 级 | 词缀文本 | 物品等级 |\n|---|---|---|\n"
            "| T1/10 | +(56-60) 力量 | 85 |\n\n[[E1]]\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(0, res.blocked, [v.reason for v in res.verdicts if not v.ok])

    def test_single_bracket_marker_line_also_counts(self) -> None:
        text = "| T 级 | 物品等级 |\n|---|---|\n| T1/10 | 85 |\n\n[E1]\n"
        self.assertEqual(0, verify(text, EVIDENCE).blocked)

    def test_prose_line_with_marker_is_not_merged(self) -> None:
        """「以上来自 [[E1]]。」是一句话，自己带标记就能过；表格不该跟着一起过。

        这条守住的是判据的紧：如果把「后面任何带标记的句子」都算进表，
        表就能从一句无关的话里借到编号。
        """
        text = (
            "| T 级 | 物品等级 |\n|---|---|\n| T1/10 | 999 |\n\n以上来自 [[E1]]。\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)
        self.assertTrue(
            next(v for v in res.verdicts if not v.ok).text.lstrip().startswith("|")
        )

    def test_numbers_still_checked_after_merge(self) -> None:
        """并进来只是给编号，编造的数字照旧拦。"""
        text = "| T 级 | 物品等级 |\n|---|---|\n| T1/10 | 999 |\n\n[[E1]]\n"
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)
        self.assertIn("999", next(v.reason for v in res.verdicts if not v.ok))

    def test_streaming_waits_for_the_trailing_line(self) -> None:
        """流式必须等那一行写完再放行整块，否则两边判定分叉（实测一致率 64/66）。"""
        text = (
            "| T 级 | 词缀文本 | 物品等级 |\n|---|---|---|\n"
            "| T1/10 | +(56-60) 力量 | 85 |\n\n[[E1]]\n\n后续说明[[E1]]。\n"
        )
        batch = verify(text, EVIDENCE)
        for chunk in (1, 2, 5, 200):
            with self.subTest(chunk=chunk):
                streamed = stream(text, EVIDENCE, chunk)
                self.assertEqual(batch.blocked, streamed.blocked)
                self.assertEqual(
                    [(v.ok, v.labels) for v in batch.verdicts if v.text.strip()],
                    [(v.ok, v.labels) for v in streamed.verdicts if v.text.strip()],
                )


class ListInheritsLeadInTest(unittest.TestCase):
    """引导句后的**列表项**继承引导句的绑定（第二十二轮）。

    实测 92 条录制离线重放：135 句拦截里 127 句是「未绑定引用」，其中 42 句紧邻上句
    有可继承的标记。逐条看完之后**只有「引导句 + 清单」是真正的续写结构**——
    链式继承（跟着上一个列表项）与引用块（`>` 里多是模型自己的覆盖说明）不是，
    那些拦得对。所以放宽只覆盖这一种，且判据写死在 `_is_list_lead_in`。
    """

    def test_list_items_inherit_lead_in(self) -> None:
        text = "力量词缀如下[[E1]]：\n\n- +(56-60) 力量\n- 物品等级 85\n"
        res = verify(text, EVIDENCE)
        self.assertEqual(0, res.blocked, [v.reason for v in res.verdicts if not v.ok])

    def test_bold_label_lead_in_counts(self) -> None:
        """`**隐式词缀**[[E1]]` 后面跟一份清单，也是引导句。"""
        text = "**显式词缀**[[E1]]\n\n- +(56-60) 力量\n"
        self.assertEqual(0, verify(text, EVIDENCE).blocked)

    def test_ordered_list_items_inherit(self) -> None:
        text = "以下 2 条[[E1]]：\n\n1. +(56-60) 力量\n2. 物品等级 85\n"
        self.assertEqual(0, verify(text, EVIDENCE).blocked)

    def test_plain_sentence_is_not_a_lead_in(self) -> None:
        """普通陈述句后面的列表项不继承——那多是模型自己举的例子。

        实测形态：「超出的 45% 是一个缓冲池……[[E1]]」后面跟
        「- 比如地图词缀减 30% 火抗……」，那一条不是证据的续写。
        """
        text = "该词缀最高档是 T1，物品等级 85[[E1]]。\n\n- 弓上则是 T5 档。\n"
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)

    def test_no_chained_inheritance_between_items(self) -> None:
        """不做链式继承：上一项带标记，不代表下一项说的是同一条证据。

        实测形态：`- **污秽混沌石**：……[[E2]]` 后面跟 `- **古灵混沌石**：……`，
        后者的依据是另一条证据。继承过去等于把 A 的引用贴到 B 的断言上，
        而这类断言常常没有数值可校。
        """
        text = "- 甲词缀提供 +(56-60) 力量[[E1]]。\n- 乙词缀提供别的东西。\n"
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)

    def test_quote_block_does_not_inherit(self) -> None:
        """`>` 块里多是模型自己的覆盖说明，不是证据的续写。"""
        text = "力量词缀如下[[E1]]：\n\n> 注意：以上仅基于已检索到的证据。\n"
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)

    def test_fabricated_number_in_inherited_item_still_blocked(self) -> None:
        """继承编号救不了编造的数字——与表格同一条安全前提。"""
        text = "力量词缀如下[[E1]]：\n\n- +(56-60) 力量，物品等级 999\n"
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)
        self.assertIn("999", next(v.reason for v in res.verdicts if not v.ok))

    def test_unbound_lead_in_gives_nothing_to_inherit(self) -> None:
        text = "力量词缀如下：\n\n- +(56-60) 力量\n"
        self.assertEqual(1, verify(text, EVIDENCE).blocked)

    def test_own_marker_wins(self) -> None:
        text = "力量词缀如下[[E1]]：\n\n- +(56-60) 力量[[E99]]\n"
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)
        self.assertIn("E99", next(v.reason for v in res.verdicts if not v.ok))

    def test_lead_in_only_reaches_its_own_list(self) -> None:
        """隔了一个小节标题之后的列表项不再继承：那已经不是同一份清单。"""
        text = (
            "力量词缀如下[[E1]]：\n\n- +(56-60) 力量\n\n"
            "## 另一节\n\n- 这里说的是别的东西。\n"
        )
        res = verify(text, EVIDENCE)
        self.assertEqual(1, res.blocked)

    def test_streaming_matches_batch(self) -> None:
        text = "力量词缀如下[[E1]]：\n\n- +(56-60) 力量\n- 物品等级 85\n\n完[[E1]]。\n"
        batch = verify(text, EVIDENCE)
        for chunk in (1, 3, 9, 200):
            with self.subTest(chunk=chunk):
                streamed = stream(text, EVIDENCE, chunk)
                self.assertEqual(batch.blocked, streamed.blocked)
                self.assertEqual(
                    [(v.ok, v.labels) for v in batch.verdicts if v.text.strip()],
                    [(v.ok, v.labels) for v in streamed.verdicts if v.text.strip()],
                )


if __name__ == "__main__":
    unittest.main()


class IndexColumnHeaderTest(unittest.TestCase):
    """序号列识别此前**从未生效过**（2026-08-18，48 句审计时发现）。

    `_table_rows` 返回的第一行是表头，而原实现把表头一起拿去判「整列都是纯整数」。
    表头那格（`#`、`序号`、`T 级`）永远让判定失败，于是整列被跳过——函数写了、
    注释写了、测试也写了（测试构造的是无表头的表），线上一次都没生效。

    实测后果：`| # | 词缀文本 |` 那张表被判「数值 1, 2 未出现在绑定证据 E1 中」，
    而 1/2 就是行号。这属于错误拒答。
    """

    def test_表头为井号时序号列被识别(self) -> None:
        ignored = binding._index_column_numbers(
            "| # | 词缀文本 |\n|---|---------|\n| 1 | 没有插槽 |\n| 2 | +1000 最大生命 |"
        )
        self.assertEqual({"1", "2"}, ignored)

    def test_表头为序号时也被识别(self) -> None:
        ignored = binding._index_column_numbers(
            "| 序号 | 词缀 |\n| --- | --- |\n| 1 | 甲 |\n| 2 | 乙 |\n| 3 | 丙 |"
        )
        self.assertEqual({"1", "2", "3"}, ignored)

    def test_真实数值列不被当成序号列(self) -> None:
        """物品等级 85/82 不构成 1..n，必须继续参与回填校验。"""
        self.assertEqual(
            set(),
            binding._index_column_numbers(
                "| 档 | 物品等级 |\n| --- | --- |\n| T1 | 85 |\n| T2 | 82 |"
            ),
        )

    def test_恰好是1和2的真实数值列会被放过(self) -> None:
        """已知代价，明确写下来：任何 1..n 列都被当序号列。

        这是保守方向选错的一侧——放过两个数字的校验，换回整张表不被误拦。
        真要收紧得靠表头语义，而表头是模型自由文本，不可靠。
        """
        self.assertEqual(
            {"1", "2"},
            binding._index_column_numbers(
                "| 插槽数 | 说明 |\n| --- | --- |\n| 1 | 单插 |\n| 2 | 双插 |"
            ),
        )

    def test_只有一个数据行时不判序号列(self) -> None:
        self.assertEqual(
            set(),
            binding._index_column_numbers("| # | 词缀 |\n| --- | --- |\n| 1 | 甲 |"),
        )


SECTION_EVIDENCE = [
    ev("E2",
       "# 异界地图 · MapUberModSynthMiniboss\n"
       "| T 级 | 词缀名 | 词缀文本 | 物品等级 | 生成权重 |\n"
       "| T1/1 | 合成的 | 地图首领伴随着一个虚空忆境首领 / "
       "该区域物品掉落数量提高 13% / 怪物群规模扩大 20% | 1 | 50（3.2468%） |")
]


class ListUnderPlainLabelInheritsSectionTest(unittest.TestCase):
    """纯标签引导句下的清单，继承**本小节**的绑定（2026-09-03 第六十一轮）。

    来历：地图词缀入库后，`desc_map_mods` 一条录制里 **31 句事实句拦了 27 句**，
    而 `确定误拦 = 0`——拦得对，但结果是**表格活下来、词缀文本全没了**。
    模型的写法是把标记放在小节标题上，正文清单跟在一个纯标签后面：

        ### 1. 合成的（MapUberModSynthMiniboss）[[E2]]   ← 绑定上下文
        | T 级 | 词缀名 | … |                            ← 表格行（已继承 E2）
        **词缀文本：**                                   ← 纯标签，自己没有标记
        - 地图首领伴随着一个虚空忆境首领                     ← 先前被拦

    `**词缀文本：**` 本身**通过**校验（加粗标签豁免），但它没有编号可给下面的清单，
    于是 `_list_lead_in` 在它那里停住、整份正文被判无据。

    放宽的边界是 `_section_binding` **只穿过结构行**（空行 / 表格行 / 纯标签），
    碰到任何别的东西就停——所以它不是「往前数到有标记为止」，
    更不是第三十九轮被否掉的那个「整行带标记就全继承」。
    离线重放 152 条录制实测：**放行 20 句、新增拦截 0 句**，确定误拦仍为 0。
    """

    SECTION = (
        "### 1. 合成的（MapUberModSynthMiniboss）[[E2]]\n\n"
        "| T 级 | 词缀名 | 物品等级 |\n"
        "|---|---|---|\n"
        "| T1/1 | 合成的 | 1 |\n\n"
        "**词缀文本：**\n"
        "- 地图首领伴随着一个虚空忆境首领\n"
        "- 该区域物品掉落数量提高 13%\n"
    )

    def test_清单继承小节标题的绑定(self) -> None:
        res = verify(self.SECTION, SECTION_EVIDENCE)
        self.assertEqual(0, res.blocked, [v.reason for v in res.verdicts if not v.ok])
        item = next(v for v in res.verdicts if "虚空忆境" in v.text)
        self.assertEqual(("E2",), item.labels)

    def test_编造的数字仍然拦得住(self) -> None:
        """**这是整个放宽的安全前提**：继承只给编号，数值回填照旧跑。"""
        text = self.SECTION.replace("提高 13%", "提高 99%")
        res = verify(text, SECTION_EVIDENCE)
        self.assertEqual(1, res.blocked)
        self.assertIn("99", next(v.reason for v in res.verdicts if not v.ok))

    def test_中间夹着模型自己的话就不继承(self) -> None:
        """只穿过结构行。夹一句散文进去，继承链必须断——
        否则「小节标题标一个引用」就等于 license 整节，那正是被否掉的那个放宽。"""
        text = (
            "### 1. 合成的[[E2]]\n\n"
            "因为火焰属于元素，这条也能产出火焰词缀。\n\n"
            "**词缀文本：**\n"
            "- 地图首领伴随着一个虚空忆境首领\n"
        )
        res = verify(text, SECTION_EVIDENCE)
        self.assertGreaterEqual(res.blocked, 2, "散文与清单都不该被放行")

    def test_小节没有绑定时清单照旧拦(self) -> None:
        text = "### 1. 合成的\n\n**词缀文本：**\n- 地图首领伴随着一个虚空忆境首领\n"
        self.assertGreaterEqual(verify(text, SECTION_EVIDENCE).blocked, 1)

    def test_被拦的句子不能当继承来源(self) -> None:
        text = (
            "### 1. 合成的[[E99]]\n\n"      # 编号无效 → 被拦
            "**词缀文本：**\n"
            "- 地图首领伴随着一个虚空忆境首领\n"
        )
        res = verify(text, SECTION_EVIDENCE)
        self.assertGreaterEqual(res.blocked, 2)

    def test_流式与批量一致(self) -> None:
        """两侧必须同结果——这是整个闸门最容易漏改的成对点。

        **只比非空白句**，与 `eval_run.replay_case` 同一口径：纯空白句的归属
        （批量把 `\\n\\n` 并进上一句、流式单独成句）不影响用户看到的内容，
        项目自己的指标也把它单列成「仅空白字符不同」。混进来会让真分叉被噪声盖住。
        """
        res = verify(self.SECTION, SECTION_EVIDENCE)
        batch = [(v.kind, v.ok, v.labels) for v in res.verdicts if v.text.strip()]
        self.assertEqual(0, res.blocked)
        for size in (1, 7, 40):
            streamed = stream(self.SECTION, SECTION_EVIDENCE, size)
            got = [(v.kind, v.ok, v.labels) for v in streamed.verdicts if v.text.strip()]
            self.assertEqual(batch, got, f"切片 {size} 与批量不一致")
            self.assertEqual(res.blocked, streamed.blocked, f"切片 {size} 拦截数不一致")
