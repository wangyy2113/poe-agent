"""代码生成的答案附注（2026-08-18）。

## 来历

人工审计判出一族错误拒答：**项目自己要求模型说的话，被项目自己的闸门拦了。**

  「共 38 个 ladder，本次返回 6 个，结果被截断」
      ← 规则明写「枚举类工具静默截断等同于漏答，超出上限必须写出还剩多少」
  「括号内百分比…不等于任何通货或工艺的一次使用成功率」
      ← `affix_pool_odds` 的规则明确要求模型这么说
  stat_sources 的 `coverage_note`：「上面的清单在这些维度上必然不完整——回答时必须如实说明」

这些信息**都不在任何证据里**（实测 `mod_tiers` 的 `ladders_total=21 / returned=6` 只在
工具 `data` 中，卡片里一个字都没有），所以模型只能无绑定地说，然后被拦。
后果是真实的信息损失：用户拿到残缺清单却看不到截断提示。

## 为什么是代码附注而不是白名单豁免

豁免等于允许模型自己编这些数字。附注由代码从工具 `data` 里取值生成，
放在闸门之后，完全可预测——与缺据声明、覆盖声明同一条路。

## 为什么不顺手让提示词告诉模型「这些不用你说」

试过，实测更差：引用有效率 67%→55%、拦截 91→122，只换来元说明类少 3 句，已撤销。
提示词是全局改动，不只影响被提到的那几句。三向对比见 `tests/golden/BASELINE.md` 第四轮。
"""

from __future__ import annotations

import unittest

from orchestrator.binding import Evidence
from orchestrator.loop import _answer_notes


def rec(tool: str, data: dict) -> dict:
    return {"tool": tool, "arguments": {}, "ok": True, "data": data}


def ev(text: str) -> Evidence:
    return Evidence(
        label="E1", chunk_id="c1", entity_id=None, title="t", url="u",
        source="POEDB", source_priority=1, text=text, can_support_facts=True,
    )


class TruncationNoteTest(unittest.TestCase):
    def test_counts_are_stated_with_the_remainder(self) -> None:
        """「还剩多少」必须写出来——这是规则的原话。"""
        notes = _answer_notes(
            [rec("mod_tiers", {"truncated": True, "ladders_total": 21,
                               "ladders_returned": 6})],
            [],
        )
        self.assertEqual(1, len(notes))
        self.assertIn("21", notes[0])
        self.assertIn("6", notes[0])
        self.assertIn("15", notes[0], "必须写出剩余项数")
        self.assertIn("不完整", notes[0])

    def test_truncated_without_counts_still_warns(self) -> None:
        """拿不到计数时也要说清单不完整，不能因为缺字段就沉默。"""
        notes = _answer_notes([rec("bench_recipe_catalog", {"truncated": True})], [])
        self.assertEqual(1, len(notes))
        self.assertIn("不完整", notes[0])

    def test_no_truncation_no_note(self) -> None:
        self.assertEqual(
            [],
            _answer_notes(
                [rec("mod_tiers", {"truncated": False, "ladders_total": 6,
                                   "ladders_returned": 6})],
                [],
            ),
        )


class CoverageNoteTest(unittest.TestCase):
    def test_required_disclosures_are_passed_through_verbatim(self) -> None:
        """`coverage_note` 是工具算好的声明，逐字取用——改写它等于改写覆盖事实。"""
        note = "以下渠道也能提供属性，但**本知识库尚未采集**：技能石；万神殿"
        notes = _answer_notes([rec("stat_sources", {"coverage_note": note})], [])
        self.assertEqual([note], notes)

    def test_zero_hit_note_included(self) -> None:
        note = "以下渠道**已采集但本次零命中**：Atlas 天赋。"
        notes = _answer_notes([rec("stat_sources", {"channels_no_match_note": note})], [])
        self.assertEqual([note], notes)

    def test_empty_fields_ignored(self) -> None:
        self.assertEqual(
            [],
            _answer_notes(
                [rec("stat_sources", {"coverage_note": "", "wording_hint": "  "})], []
            ),
        )


class WeightSemanticsNoteTest(unittest.TestCase):
    def test_caveat_added_when_evidence_shows_weight_percentages(self) -> None:
        notes = _answer_notes([], [ev("| T1/8 | 巫妖的 | … | 80 | 1500（1.888%） |")])
        self.assertEqual(1, len(notes))
        self.assertIn("不等于", notes[0])
        self.assertIn("成功率", notes[0])

    def test_no_caveat_without_percentages(self) -> None:
        self.assertEqual([], _answer_notes([], [ev("| T1/8 | 巫妖的 | 80 | 1500 |")]))


class DedupAndShapeTest(unittest.TestCase):
    def test_same_note_from_two_tools_appears_once(self) -> None:
        note = "以下渠道尚未采集：技能石"
        notes = _answer_notes(
            [rec("stat_sources", {"coverage_note": note}),
             rec("mod_tiers", {"coverage_note": note})],
            [],
        )
        self.assertEqual([note], notes)

    def test_non_dict_data_is_ignored(self) -> None:
        """工具失败时 `data` 可能是 None 或字符串，不能因此炸掉整轮。"""
        self.assertEqual(
            [],
            _answer_notes(
                [{"tool": "x", "data": None}, {"tool": "y", "data": "boom"}, {}], []
            ),
        )

    def test_notes_do_not_carry_binding_markers(self) -> None:
        """附注不经闸门，所以它自己**不能**长成需要绑定的样子——
        混进 `[[E1]]` 会让引用列表出现没被任何句子用到的编号。"""
        notes = _answer_notes(
            [rec("mod_tiers", {"truncated": True, "ladders_total": 21,
                               "ladders_returned": 6})],
            [ev("1500（1.888%）")],
        )
        for note in notes:
            with self.subTest(note=note[:20]):
                self.assertNotIn("[[E", note)


if __name__ == "__main__":
    unittest.main()


class BoundaryNoteTest(unittest.TestCase):
    """`boundary` 是第四次撞上「项目要求模型说的话被项目自己拦了」（2026-08-18）。

    元词缀矩阵刚入库后的第一次真实调用：模型照提示词写了边界声明并绑定 [[E1]]，
    闸门判「Metamod 矩阵证据仅支撑页面单元格原字段，不能支撑给定装备状态下的可执行性」。
    **拦得没错**——E1 支撑不了那些语义；错的是让模型来说这句话。原文就在工具的
    `data.boundary` 里，所以走代码附注。
    """

    def test_矩阵边界被原样附上(self) -> None:
        boundary = "仅复述来源矩阵单元格；不证明可执行，不提供状态转移、步骤、概率、成本或路线"
        notes = _answer_notes(
            [rec("metamod_interaction_matrix", {"found": True, "boundary": boundary})], []
        )
        self.assertEqual([boundary], notes)

    def test_工艺台边界也附上(self) -> None:
        boundary = "目录记录不证明给定装备状态下可执行"
        notes = _answer_notes([rec("bench_recipe_catalog", {"boundary": boundary})], [])
        self.assertEqual([boundary], notes)

    def test_同一边界只出现一次(self) -> None:
        boundary = "仅复述来源矩阵单元格"
        notes = _answer_notes(
            [rec("metamod_interaction_matrix", {"boundary": boundary}),
             rec("bench_recipe_catalog", {"boundary": boundary})],
            [],
        )
        self.assertEqual([boundary], notes)


class DerivationBlockTest(unittest.TestCase):
    """计算器推导表的代码附注（2026-08-27 第四十一轮）。

    实测：31 条带表格的答案里 **12 条（39%）的推导表被出口闸门拦掉**（计算器类 9/21）。
    闸门没错——模型把工具的推导表原样抄下来却没绑编号，那就是无据断言。
    丢的不是结论（12 条里只有 1 条因此真的漏了期望数值），是**可核对的算式**，
    而项目的规矩是「算出来的数字要连算式一起给」。
    """

    def _ev(self, text: str, label: str = "E9"):  # noqa: ANN202
        from orchestrator.binding import Evidence  # noqa: PLC0415

        return Evidence(
            label=label, chunk_id="c", entity_id=None, title="t", url="u",
            source="s", source_priority=1, text=text,
        )

    _TABLE = (
        "| 步骤 | 算式 | 结果 |\n"
        "| --- | --- | --- |\n"
        "| 怪物伤害表 | monsterDamageTable[84] | 821.73 |\n"
        "| 取整 | round(14382) | 14,382 |"
    )

    def test_模型没贴推导表时由代码附上(self) -> None:
        from orchestrator.loop import _derivation_blocks  # noqa: PLC0415

        blocks = _derivation_blocks([self._ev(f"### 结算\n{self._TABLE}\n\n> 边界")], "命中率 83%[[E9]]。")
        self.assertEqual(1, len(blocks))
        self.assertIn("monsterDamageTable[84]", blocks[0])
        # 要标出取自哪条证据——代码附注也得说得清出处
        self.assertIn("[[E9]]", blocks[0])

    def test_模型自己贴了就不重复附(self) -> None:
        """19/31 的答案里模型是会贴的；无条件附会让它出现两次。"""
        from orchestrator.loop import _derivation_blocks  # noqa: PLC0415

        answer = f"### 推导\n{self._TABLE}\n"
        self.assertEqual([], _derivation_blocks([self._ev(self._TABLE)], answer))

    def test_没有推导表的证据不附任何东西(self) -> None:
        from orchestrator.loop import _derivation_blocks  # noqa: PLC0415

        self.assertEqual([], _derivation_blocks([self._ev("普通卡片正文")], "答案"))

    def test_表头与render_steps保持一致(self) -> None:
        """判重与抽取都认这个表头；两处分叉就会既判不了重也切不出表。"""
        from calc.derive import render_steps  # noqa: PLC0415
        from orchestrator.loop import _STEP_TABLE_HEADER  # noqa: PLC0415

        self.assertEqual(_STEP_TABLE_HEADER, render_steps([])[0])

    def test_只附第一张表(self) -> None:
        """多工具同轮时全附会刷屏，而用户问的通常是其中一件事。"""
        from orchestrator.loop import _derivation_blocks  # noqa: PLC0415

        evs = [self._ev(self._TABLE, "E1"), self._ev(self._TABLE, "E2")]
        self.assertEqual(1, len(_derivation_blocks(evs, "答案")))
