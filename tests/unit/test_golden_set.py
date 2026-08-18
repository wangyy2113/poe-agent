"""黄金问答集的结构校验（任务 2.7）。

这些用例不碰数据库、不发网络——它们守的是**评测集本身**的正确性。
一条写错的用例会让评测输出看起来正常的错数字，那比直接报错危险得多，
所以加载器对未知字段与不相容期望一律抛错，这里把该行为钉住。
"""

from __future__ import annotations

import unittest

from tests.golden.loader import (
    KINDS,
    Case,
    GoldenSetError,
    by_kind,
    load_cases,
    matches_slug,
)


class TestGoldenSetLoads(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = load_cases()

    def test_loads_and_ids_unique(self) -> None:
        self.assertGreater(len(self.cases), 30, "样本太少，指标没有解释力")
        ids = [c.id for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_kind_has_samples(self) -> None:
        """六类都要有样本：缺哪一类，对应的失败模式就测不到。"""
        groups = by_kind(self.cases)
        for kind in KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(groups[kind], f"{kind} 类没有样本")

    def test_gap_cases_are_the_largest_negative_set(self) -> None:
        """拒答类是 V1 最关键的验收项，样本量不能低于 10 条。"""
        groups = by_kind(self.cases)
        self.assertGreaterEqual(len(groups["gap"]), 10)

    def test_gap_cases_carry_no_positive_expectation(self) -> None:
        for c in self.cases:
            if c.expect_gap:
                with self.subTest(case=c.id):
                    self.assertFalse(c.expect_slug)
                    self.assertFalse(c.expect_any_hit)
                    self.assertFalse(c.expect_values)
                    self.assertTrue(c.expects_sufficient is False)

    def test_numeric_cases_pin_values(self) -> None:
        """数值类必须写死期望数值，否则它退化成「有没有命中」。"""
        for c in by_kind(self.cases)["numeric"]:
            with self.subTest(case=c.id):
                self.assertTrue(c.expect_values, f"{c.id} 没有 expect_values")

    def test_tool_route_cases_name_a_registered_tool(self) -> None:
        """期望的工具必须真的注册过，否则用例永远不可能通过。"""
        import tools.impl  # noqa: F401  导入即注册
        from agents.knowledge import ALLOWED_TOOLS
        from tools.registry import REGISTRY

        registered = set(REGISTRY.names())
        for c in by_kind(self.cases)["tool_route"]:
            with self.subTest(case=c.id):
                self.assertIn(c.expect_tool, registered)
                self.assertIn(c.expect_tool, ALLOWED_TOOLS)


class TestLoaderRejectsBadCases(unittest.TestCase):
    """加载器必须对结构问题抛错而不是忽略。"""

    def _write(self, body: str) -> str:
        import tempfile

        path = tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        path.write(body)
        path.close()
        return path.name

    def _expect_error(self, body: str, needle: str) -> None:
        from pathlib import Path

        with self.assertRaises(GoldenSetError) as ctx:
            load_cases(Path(self._write(body)))
        self.assertIn(needle, str(ctx.exception))

    def test_unknown_field_rejected(self) -> None:
        self._expect_error(
            "cases:\n  - id: a\n    query: q\n    kind: name\n"
            "    expect_any_hit: true\n    expcet_slug: typo\n",
            "未知字段",
        )

    def test_bad_kind_rejected(self) -> None:
        self._expect_error(
            "cases:\n  - id: a\n    query: q\n    kind: nmae\n    expect_any_hit: true\n",
            "kind=",
        )

    def test_duplicate_id_rejected(self) -> None:
        self._expect_error(
            "cases:\n"
            "  - id: a\n    query: q\n    kind: name\n    expect_any_hit: true\n"
            "  - id: a\n    query: r\n    kind: name\n    expect_any_hit: true\n",
            "id 重复",
        )

    def test_gap_with_positive_expectation_rejected(self) -> None:
        self._expect_error(
            "cases:\n  - id: a\n    query: q\n    kind: gap\n"
            "    expect_gap: true\n    expect_slug: Foo\n",
            "互斥",
        )

    def test_expectationless_case_rejected(self) -> None:
        self._expect_error(
            "cases:\n  - id: a\n    query: q\n    kind: name\n",
            "必须至少给一项期望",
        )

    def test_string_where_list_expected_rejected(self) -> None:
        """`expect_values: "75"` 会被逐字符迭代成 ['7','5']，必须报错而不是默默接受。"""
        self._expect_error(
            "cases:\n  - id: a\n    query: q\n    kind: numeric\n"
            '    expect_values: "75"\n',
            "必须是列表",
        )


class TestMatchesSlug(unittest.TestCase):
    def test_bare_slug_compares_last_segment(self) -> None:
        self.assertTrue(matches_slug("unique_item:Headhunter", "Headhunter"))
        self.assertFalse(matches_slug("unique_item:Replica_Headhunter", "Headhunter"))

    def test_typed_prefix_asserts_type(self) -> None:
        """天赋类实体 id 末尾带内容哈希，按前缀比；类型必须一致。"""
        self.assertTrue(
            matches_slug("passive:Divine_Shield_ae58e77b8f", "passive:Divine_Shield_*")
        )
        self.assertFalse(
            matches_slug("unique_item:Divine_Shield", "passive:Divine_Shield_*")
        )

    def test_empty_entity_never_matches(self) -> None:
        self.assertFalse(matches_slug(None, "Headhunter"))
        self.assertFalse(matches_slug("", "Headhunter"))


class TestCaseSemantics(unittest.TestCase):
    def test_answer_only_case_is_not_retrieval_scorable(self) -> None:
        """只有答案级期望的用例不参与检索侧判定，否则会得到假的错误拒答。"""
        c = Case(id="x", query="q", kind="cross_source", expect_in_answer=("POEDB",))
        self.assertFalse(c.retrieval_scorable)

    def test_gap_case_is_retrieval_scorable(self) -> None:
        c = Case(id="x", query="q", kind="gap", expect_gap=True)
        self.assertTrue(c.retrieval_scorable)
        self.assertFalse(c.expects_sufficient)


if __name__ == "__main__":
    unittest.main()
