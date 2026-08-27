"""制作可行性三态分析器测试。

桩库复用真实 POEDB 词缀区间；额外行只用于构造 family_list 组合关系，测试的是
求解器约束而不是游戏数值。未知规则必须保持 unknown，不能由测试替实现补常识。
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from craft.feasibility import (  # noqa: E402
    MAX_TARGET_QUERY_CHARS,
    MAX_TARGET_SOURCE_CHARS,
    MAX_TARGETS,
    analyze_feasibility,
)
from tests.unit.test_item_mods import _insert_affix, fixture_conn  # noqa: E402
from tools.impl.craft import check_mod_combination  # noqa: E402
from tools.registry import REGISTRY  # noqa: E402


def _add_affix(
    conn,
    *,
    page: str = "Amulets",
    group: str,
    text: str,
    gen_label: str = "前缀",
    family_list: list[str] | None = None,
    category: str = "normal",
    category_label: str = "Base",
) -> None:
    _insert_affix(
        conn,
        page=page,
        code="Amulet" if page == "Amulets" else "Gloves",
        zh="项链" if page == "Amulets" else "手套",
        group=group,
        gen_label=gen_label,
        ladder=[(1, 1, 1, [[1, 2]], text)],
        category=category,
        category_label=category_label,
    )
    if family_list is not None:
        conn.execute(
            "UPDATE mod_affix SET family_list=? WHERE item_class_page=? AND correct_group=? AND category=?",
            [json.dumps(family_list), page, group, category],
        )
    conn.commit()


def _page(report, page: str = "Amulets"):
    return next(p for p in report.page_analyses if p.page == page)


class CraftFeasibilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = fixture_conn()

    def tearDown(self) -> None:
        self.conn.close()


class TestThreeStateVerdict(CraftFeasibilityTestCase):
    def test_无冲突且容量够用判possible(self):
        """第三十七轮之前这条**恒为 unknown**：模块末尾有一行无条件的
        `verdict = "unknown"`，三态里的第三态是死的。容量规则入库后它才活过来。
        """
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "最大生命"}, {"query": "火焰抗性"}],
            rarity="稀有",
        )
        self.assertEqual(report.verdict, "possible")
        page = _page(report)
        self.assertEqual(page.occupied_prefixes, 1)
        self.assertEqual(page.occupied_suffixes, 1)
        self.assertFalse(report.gaps)
        capacity = next(c for c in page.checks if c.key == "affix_capacity")
        self.assertEqual(capacity.status, "pass")
        self.assertIn("3 前缀 / 3 后缀", capacity.summary)
        family = next(c for c in page.checks if c.key == "family_exclusivity")
        self.assertEqual(family.status, "pass")
        self.assertIn("不等于整体可制作", family.summary)

    def test_没给稀有度仍必须unknown(self):
        """**不替用户假定「稀有」**——与「不替用户假定打 83 级」同一条。"""
        report = analyze_feasibility(
            self.conn, "项链", [{"query": "最大生命"}, {"query": "火焰抗性"}]
        )
        self.assertEqual(report.verdict, "unknown")
        self.assertTrue(any(g.code == "affix_capacity" for g in report.gaps))
        capacity = next(c for c in _page(report).checks if c.key == "affix_capacity")
        self.assertEqual(capacity.status, "unknown")
        self.assertIn("稀有度", capacity.missing)

    def test_超出名额判impossible(self):
        """容量规则带来的**新真负例**：稀有最多 3 前缀，4 个前缀目标不可能共存。"""
        for index in range(3):
            _add_affix(
                self.conn,
                group=f"ExtraPrefix{index}",
                text=f"额外前缀属性 {index}",
            )
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "最大生命"}] + [{"query": f"额外前缀属性 {i}"} for i in range(3)],
            rarity="稀有",
        )
        self.assertEqual(report.verdict, "impossible")
        page = _page(report)
        self.assertEqual(page.occupied_prefixes, 4)
        capacity = next(c for c in page.checks if c.key == "affix_capacity")
        self.assertEqual(capacity.status, "fail")
        self.assertIn("前缀 4 > 上限 3", capacity.summary)
        # 已经证明不可行，不该再挂一条「缺容量规则」的缺口
        self.assertFalse(any(g.code == "affix_capacity" for g in report.gaps))

    def test_传奇不受名额限制但仍是unknown(self):
        """「不受限制」≠「这些词缀能共存」：传奇的词缀是固定的，不由名额决定。"""
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "最大生命"}, {"query": "火焰抗性"}],
            rarity="传奇",
        )
        self.assertEqual(report.verdict, "unknown")
        capacity = next(c for c in _page(report).checks if c.key == "affix_capacity")
        self.assertEqual(capacity.status, "unknown")
        self.assertIn("不受前后缀数量限制", capacity.missing)

    def test_其他生成位让容量判不了(self):
        """腐化 / 附魔 / 灭界者等有各自的槽位规则，本库没采集。"""
        _add_affix(
            self.conn, group="CorruptedExtra", text="你能施加一个额外诅咒",
            category="corrupted", category_label="已腐化",
        )
        self.conn.execute(
            "UPDATE mod_affix SET gen_type=0, gen_label='其他' WHERE correct_group=?",
            ("CorruptedExtra",),
        )
        self.conn.commit()
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "最大生命"}, {"query": "你能施加一个额外诅咒"}],
            rarity="稀有",
        )
        self.assertEqual(report.verdict, "unknown")
        page = _page(report)
        self.assertEqual(page.occupied_other, 1)
        capacity = next(c for c in page.checks if c.key == "affix_capacity")
        self.assertEqual(capacity.status, "unknown")
        self.assertIn("其他生成位", capacity.missing)

    def test_所有候选同family才判impossible(self):
        _add_affix(
            self.conn,
            group="HybridRegen",
            text="+(1-2) 每秒生命再生",
            gen_label="后缀",
            family_list=["HybridRegen", "IncreasedLife"],
        )
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "IncreasedLife"}, {"query": "生命再生"}],
        )
        self.assertEqual(report.verdict, "impossible")
        page = _page(report)
        self.assertTrue(page.conflicts)
        self.assertIn("IncreasedLife", page.conflicts[0].shared_families)
        self.assertEqual(
            next(c for c in page.checks if c.key == "family_exclusivity").status,
            "fail",
        )
        # 冲突已经足够证明不可行；容量缺失不应稀释这个结论。
        self.assertFalse(any(g.code == "affix_capacity" for g in page.gaps))

    def test_存在一组无冲突候选不得误判impossible(self):
        _add_affix(
            self.conn,
            group="HybridRecovery",
            text="+(1-2) 生命恢复",
            family_list=["HybridRecovery", "IncreasedLife"],
        )
        _add_affix(
            self.conn,
            group="ManaRecovery",
            text="+(1-2) 魔力恢复",
            gen_label="后缀",
        )
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "IncreasedLife"}, {"query": "恢复"}],
        )
        self.assertEqual(report.verdict, "unknown")
        self.assertEqual(
            next(c for c in _page(report).checks if c.key == "family_exclusivity").status,
            "pass",
        )

    def test_目标没有字面候选时不以缺失证明不可能(self):
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "这个词缀纯属虚构"}],
        )
        self.assertEqual(report.verdict, "unknown")
        self.assertTrue(any(g.code == "target_not_resolved" for g in report.gaps))


class TestOccupancyAndScope(CraftFeasibilityTestCase):
    def test_同一条混合词缀满足两个目标只占一个位置(self):
        _add_affix(
            self.conn,
            group="HybridLifeMana",
            text="+(1-2) 最大生命 / +(1-2) 最大魔力",
            family_list=["IncreasedLife", "IncreasedMana"],
        )
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "最大生命"}, {"query": "最大魔力"}],
        )
        page = _page(report)
        self.assertEqual(report.verdict, "unknown")
        self.assertEqual(len(page.selected_candidate_ids), 1)
        self.assertEqual(page.occupied_prefixes, 1)
        self.assertEqual(page.occupied_suffixes, 0)

    def test_逐T级不同mod_code仍折叠为一条稳定候选(self):
        _insert_affix(
            self.conn,
            page="Amulets",
            code="Amulet",
            zh="项链",
            group="StableEnergyShield",
            gen_label="前缀",
            ladder=[
                (1, 2, 20, [[10, 12]], "+(10-12) 最大能量护盾"),
                (2, 2, 1, [[5, 9]], "+(5-9) 最大能量护盾"),
            ],
        )
        self.conn.execute(
            "UPDATE mod_affix SET mod_code=correct_group || tier "
            "WHERE correct_group='StableEnergyShield'"
        )
        self.conn.commit()

        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "最大能量护盾"}],
        )
        resolution = _page(report).targets[0]
        self.assertEqual(resolution.total_candidates, 1)
        self.assertEqual(resolution.candidates[0].tiers, (1, 2))

    def test_跨页面候选集合不一致必须unknown(self):
        _add_affix(
            self.conn,
            page="Gloves_str",
            group="GloveFireResistance",
            text="+(1-2)% 手套专属火焰抗性",
            gen_label="后缀",
        )
        report = analyze_feasibility(
            self.conn,
            "手套",
            [{"query": "最大生命"}, {"query": "手套专属火焰抗性"}],
        )
        self.assertEqual(report.verdict, "unknown")
        scope = next(c for c in report.checks if c.key == "base_page_scope")
        self.assertEqual(scope.status, "unknown")
        self.assertTrue(any(g.code == "base_page_scope" for g in report.gaps))


class TestMissingRules(CraftFeasibilityTestCase):
    def test_零召回与另一目标截断必须同时报告(self):
        for index in range(81):
            _add_affix(
                self.conn,
                group=f"WideTarget{index}",
                text=f"+(1-2) 宽目标候选 {index}",
            )
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "这个词缀纯属虚构"}, {"query": "宽目标"}],
        )
        codes = {gap.code for gap in report.gaps}
        self.assertIn("target_not_resolved", codes)
        self.assertIn("candidate_search_truncated", codes)

    def test_真实影响力来源类别必须触发缺据(self):
        for category in ("searing", "eater"):
            with self.subTest(category=category):
                conn = fixture_conn()
                self.addCleanup(conn.close)
                _add_affix(
                    conn,
                    group=f"{category.title()}Only",
                    text=f"+(1-2) {category} 专属词缀",
                    category=category,
                    category_label=category,
                )
                report = analyze_feasibility(
                    conn,
                    "项链",
                    [{"query": f"{category} 专属词缀"}],
                )
                self.assertTrue(
                    any(g.code == "influence_requirement" for g in report.gaps)
                )

    def test_非常规来源规则缺失必须unknown(self):
        _add_affix(
            self.conn,
            group="EssenceOnlyLife",
            text="+(1-2) 精华生命",
            category="essence",
            category_label="精华",
        )
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "精华生命", "source": "精华"}],
        )
        self.assertEqual(report.verdict, "unknown")
        self.assertTrue(any(g.code == "source_requirement" for g in report.gaps))

    def test_请求影响力但规则缺失必须unknown(self):
        report = analyze_feasibility(
            self.conn,
            "项链",
            [{"query": "最大生命"}],
            influences=["猎人"],
        )
        self.assertEqual(report.verdict, "unknown")
        self.assertTrue(any(g.code == "influence_requirement" for g in report.gaps))


class TestToolContract(CraftFeasibilityTestCase):
    @staticmethod
    def _all_keys(value):
        if isinstance(value, dict):
            yield from value.keys()
            for child in value.values():
                yield from TestToolContract._all_keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from TestToolContract._all_keys(child)

    def test_impossible有独立且穷尽的求解证据(self):
        _add_affix(
            self.conn,
            group="HybridRegen",
            text="+(1-2) 每秒生命再生",
            gen_label="后缀",
            family_list=["HybridRegen", "IncreasedLife"],
        )
        with patch("tools.impl.craft.dbmod.get_conn", return_value=self.conn):
            result = check_mod_combination(
                "项链",
                [{"query": "IncreasedLife"}, {"query": "生命再生"}],
            )
        solver = next(
            citation
            for citation in result.citations
            if citation["chunk_id"].startswith("craft_solver:")
        )
        self.assertIn("候选分配已穷尽：是", solver["text"])
        self.assertIn("页面结论：impossible", solver["text"])
        self.assertIn("结论证明", solver["text"])
        self.assertIn("求解器版本", solver["text"])
        self.assertIn("SHA-256", solver["text"])
        self.assertIn("确定性求解器", solver["source"])
        self.assertEqual(solver["channel"], "derived")

    def test_事实有证据未知有gap且不越界(self):
        with patch("tools.impl.craft.dbmod.get_conn", return_value=self.conn):
            result = check_mod_combination(
                "项链",
                [{"query": "最大生命"}, {"query": "火焰抗性"}],
                rarity="稀有",
            )
        self.assertTrue(result.ok)
        self.assertTrue(result.citations)
        self.assertTrue(all(c["url"] and c["text"] for c in result.citations))
        # 容量规则入库后这一问是 possible、没有缺口；「有缺口」改由下面那条
        # 没给稀有度的调用来钉——把两件事分开，免得再有用例为错误的理由变绿。
        self.assertEqual(result.data["verdict"], "possible")
        self.assertFalse(result.data["gaps"])
        page = result.data["page_analyses"][0]
        candidates = [
            candidate
            for target in page["target_resolutions"]
            for candidate in target["candidates"]
        ]
        self.assertTrue(candidates)
        self.assertTrue(all("url" not in candidate for candidate in candidates))

        data_keys = set(self._all_keys(result.data))
        self.assertFalse({"url", "authority_url", "evidence_urls"} & data_keys)
        self.assertFalse({"steps", "probability", "cost"} & data_keys)

        # 同一问法不给稀有度：仍然是 unknown，且缺口要原样列出来
        with patch("tools.impl.craft.dbmod.get_conn", return_value=self.conn):
            vague = check_mod_combination(
                "项链", [{"query": "最大生命"}, {"query": "火焰抗性"}]
            )
        self.assertEqual(vague.data["verdict"], "unknown")
        self.assertTrue(vague.data["gaps"])
        self.assertTrue(
            any(g["code"] == "affix_capacity" for g in vague.data["gaps"])
        )

    def test_八个目标的solver证据保留全部输入摘要(self):
        target_names = []
        for index in range(MAX_TARGETS):
            prefix = f"审计目标{index}"
            target_names.append(
                prefix + "甲" * (MAX_TARGET_QUERY_CHARS - len(prefix))
            )
        for index, target_name in enumerate(target_names):
            _add_affix(
                self.conn,
                group=f"AuditTarget{index}",
                text=f"+(1-2) {target_name}",
                gen_label="前缀" if index % 2 == 0 else "后缀",
                family_list=[f"AuditFamily{index}"],
            )

        with patch("tools.impl.craft.dbmod.get_conn", return_value=self.conn):
            result = check_mod_combination(
                "项链",
                [{"query": target_name} for target_name in target_names],
            )

        self.assertTrue(result.ok)
        solver = next(
            citation
            for citation in result.citations
            if citation["chunk_id"].startswith("craft_solver:")
        )
        for target_name in target_names:
            self.assertIn(target_name, solver["text"])
        self.assertEqual(len(re.findall(r"`[0-9a-f]{64}`", solver["text"])), MAX_TARGETS)

    def test_九个目标被工具明确拒绝(self):
        targets = [{"query": f"目标{index}"} for index in range(MAX_TARGETS + 1)]
        with patch("tools.impl.craft.dbmod.get_conn", return_value=self.conn):
            result = check_mod_combination("项链", targets)

        self.assertFalse(result.ok)
        self.assertIn(f"targets 最多允许 {MAX_TARGETS} 项", result.error)

    def test_超长目标或来源被工具明确拒绝(self):
        cases = (
            (
                [
                    {"query": "甲" * (MAX_TARGET_QUERY_CHARS + 1)},
                    {"query": "火焰抗性"},
                ],
                f"query 最多允许 {MAX_TARGET_QUERY_CHARS} 个字符",
            ),
            (
                [
                    {
                        "query": "最大生命",
                        "source": "甲" * (MAX_TARGET_SOURCE_CHARS + 1),
                    },
                    {"query": "火焰抗性"},
                ],
                f"source 最多允许 {MAX_TARGET_SOURCE_CHARS} 个字符",
            ),
        )
        for targets, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                with patch("tools.impl.craft.dbmod.get_conn", return_value=self.conn):
                    result = check_mod_combination("项链", targets)
                self.assertFalse(result.ok)
                self.assertIn(expected_error, result.error)

    def test_工具schema声明目标数与文本长度上限(self):
        spec = REGISTRY.spec("check_mod_combination")
        self.assertIsNotNone(spec)
        assert spec is not None
        targets_schema = spec.parameters["properties"]["targets"]
        self.assertEqual(targets_schema["maxItems"], MAX_TARGETS)
        item_schema = targets_schema["items"]["properties"]
        self.assertEqual(item_schema["query"]["maxLength"], MAX_TARGET_QUERY_CHARS)
        self.assertEqual(item_schema["source"]["maxLength"], MAX_TARGET_SOURCE_CHARS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
