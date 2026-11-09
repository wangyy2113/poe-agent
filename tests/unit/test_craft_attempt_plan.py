"""独立普通词缀选择计划：分布、材料、计价、API 与路由边界。"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

from api.craft import (
    AttemptMaterialRequest,
    CraftAttemptPlanRequest,
    independent_attempt_plan,
)
from craft.attempt_plan import (
    AttemptMaterial,
    parse_materials,
    plan_affix_attempts,
    validate_pricing_snapshot,
)
from economy.models import (
    MaterialCostEstimate,
    MaterialCostLine,
    PricePoint,
)
from economy.service import EconomyUnavailable
from orchestrator import loop
from orchestrator.binding import number_evidence, verify
from tests.unit.test_craft_pool import TOOL_ARGS, _populated_conn, _write_page_audit
from tools.impl.craft_attempt_plan import craft_attempt_plan, render_attempt_plan
from tools.registry import REGISTRY, ToolResult


QUESTION = (
    "独立尝试计划：底材“铁制背心”，物品等级 50，前缀侧，目标“最大生命”；"
    "每次完整重置并只做一次普通同侧选择；每次消耗 1 个混沌石和 2 个增幅石；"
    "最多尝试 20 次；计算成功率、材料预算和市场成本。"
)
MATERIAL_INPUT = [
    {"query": "混沌石", "quantity_per_attempt": 1, "category": "currency"},
    {"query": "增幅石", "quantity_per_attempt": 2, "category": "currency"},
]


def _point(name: str, price: float) -> PricePoint:
    return PricePoint(
        item_id=name,
        name=name,
        category="currency",
        group="currency",
        representative_chaos=price,
        price_basis="mean",
        mean_chaos=price,
        median_chaos=price,
        min_chaos=price * 0.9,
        max_chaos=price * 1.1,
        divine=None,
        daily=100,
        change_percent=0,
        low_confidence=False,
    )


def _line(
    query: str,
    quantity: float,
    price: float,
    *,
    fetched_at: str = "2026-10-09T10:00:00+00:00",
    source: str = "poe.watch",
    league: str = "Standard",
    status: str = "priced",
    stale: bool = False,
) -> MaterialCostLine:
    point = _point(query, price) if status == "priced" else None
    subtotal = price * quantity if point else None
    return MaterialCostLine(
        query=query,
        quantity=quantity,
        requested_category="currency",
        status=status,
        reason="" if status == "priced" else "missing",
        resolved_name=query,
        categories_searched=("currency",),
        league=league,
        source=source,
        source_url="https://poe.watch/api",
        fetched_at=fetched_at,
        from_cache=False,
        stale=stale,
        degraded=False,
        fallback=False,
        fallback_reason="",
        warning="",
        low_confidence=False,
        unit_price=point,
        candidate_count=1 if point else 0,
        subtotal_representative_chaos=subtotal,
        subtotal_mean_chaos=subtotal,
        subtotal_min_chaos=(price * 0.9 * quantity) if point else None,
        subtotal_max_chaos=(price * 1.1 * quantity) if point else None,
    )


def _estimate(
    *,
    lines: tuple[MaterialCostLine, ...] | None = None,
    calculable: bool = True,
    stale: bool = False,
    source: str = "poe.watch",
    league: str = "Standard",
) -> MaterialCostEstimate:
    rows = lines or (_line("混沌石", 1, 1), _line("增幅石", 2, 0.5))
    total = sum(line.subtotal_representative_chaos or 0 for line in rows)
    return MaterialCostEstimate(
        league=league,
        source=source,
        source_url="https://poe.watch/api",
        fetched_at=min(line.fetched_at for line in rows),
        from_cache=False,
        stale=stale,
        degraded=False,
        fallback=False,
        warning="",
        calculable=calculable,
        unresolved_count=0 if calculable else 1,
        total_representative_chaos=total if calculable else None,
        total_mean_chaos=total if calculable else None,
        total_min_chaos=total * 0.9 if calculable else None,
        total_max_chaos=total * 1.1 if calculable else None,
        materials=rows,
    )


def _materials() -> tuple[AttemptMaterial, ...]:
    return parse_materials(MATERIAL_INPUT)


class MaterialValidationTest(unittest.TestCase):
    def test_preserves_decimal_quantity_and_category(self) -> None:
        parsed = parse_materials([
            {"query": "神圣石", "quantity_per_attempt": "0.25", "category": "currency"}
        ])
        self.assertEqual(parsed[0].quantity_per_attempt, Decimal("0.25"))
        self.assertEqual(parsed[0].to_dict()["quantity_per_attempt"], "0.25")

    def test_rejects_empty_duplicate_bool_nonfinite_and_extra_fields(self) -> None:
        invalid = (
            [],
            [
                {"query": "混沌石", "quantity_per_attempt": 1},
                {"query": "混沌石", "quantity_per_attempt": 2},
            ],
            [{"query": "混沌石", "quantity_per_attempt": True}],
            [{"query": "混沌石", "quantity_per_attempt": "NaN"}],
            [{"query": "混沌石", "quantity_per_attempt": 1, "step": "猜的"}],
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_materials(value)


class DistributionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _populated_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def plan(self, **changes):  # noqa: ANN202
        args = {
            **TOOL_ARGS,
            "materials": _materials(),
            "max_attempts": 5,
        }
        args.update(changes)
        return plan_affix_attempts(self.conn, **args)

    def test_geometric_distribution_and_cap_are_exactly_scoped(self) -> None:
        report = self.plan()
        self.assertEqual(report.status, "exact")
        dist = report.distribution
        assert dist is not None
        self.assertEqual(dist["single_attempt_probability"], "0.3")
        self.assertEqual(dist["expected_attempts_until_success"], "3.33333333333333333")
        self.assertEqual(dist["success_probability_within_cap"], "0.83193")
        self.assertEqual(dist["exhaustion_probability"], "0.16807")
        self.assertEqual(dist["expected_attempts_consumed_with_cap"], "2.7731")
        self.assertEqual(
            [(item["label"], item["attempts"]) for item in dist["milestones"]],
            [("p50", 2), ("p90", 7), ("p95", 9)],
        )
        self.assertTrue(dist["milestones"][0]["within_attempt_cap"])
        self.assertFalse(dist["milestones"][1]["within_attempt_cap"])

    def test_material_budgets_distinguish_expectation_cap_and_percentiles(self) -> None:
        report = self.plan()
        chaos, augmentation = report.material_budget
        self.assertEqual(chaos["expected_until_success"], "3.33333333333333333")
        self.assertEqual(chaos["expected_with_attempt_cap"], "2.7731")
        self.assertEqual(chaos["maximum_at_attempt_cap"], "5")
        self.assertEqual(chaos["milestones"], {"p50": "2", "p90": "7", "p95": "9"})
        self.assertEqual(augmentation["expected_with_attempt_cap"], "5.5462")
        self.assertEqual(augmentation["maximum_at_attempt_cap"], "10")

    def test_no_market_request_never_emits_cost(self) -> None:
        report = self.plan()
        self.assertEqual(report.pricing.status, "not_requested")
        self.assertIsNone(report.cost_budget)

    def test_complete_market_snapshot_scales_each_budget(self) -> None:
        report = self.plan(market_cost=_estimate())
        self.assertEqual(report.pricing.status, "complete")
        assert report.cost_budget is not None
        self.assertEqual(report.cost_budget["per_attempt_chaos"], "2")
        self.assertEqual(report.cost_budget["expected_until_success_chaos"], "6.66666666666666667")
        self.assertEqual(report.cost_budget["expected_with_attempt_cap_chaos"], "5.5462")
        self.assertEqual(report.cost_budget["maximum_at_attempt_cap_chaos"], "10")
        self.assertEqual(
            report.cost_budget["milestones_chaos"],
            {"p50": "4", "p90": "14", "p95": "18"},
        )

    def test_unknown_pool_never_emits_distribution_or_cost(self) -> None:
        report = self.plan(base_type="不存在的底材", market_cost=_estimate())
        self.assertEqual(report.status, "unknown")
        self.assertIsNone(report.distribution)
        self.assertEqual(report.material_budget, [])
        self.assertIsNone(report.cost_budget)
        self.assertEqual(report.gaps[0]["code"], "base_not_resolved")

    def test_attempt_cap_rejects_bool_zero_and_overflow(self) -> None:
        for cap in (True, 0, 1_000_001):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                self.plan(max_attempts=cap)

    def test_probability_one_needs_one_attempt_for_every_milestone(self) -> None:
        self.conn.execute(
            "DELETE FROM mod_affix WHERE gen_type=1 AND correct_group!='IncreasedLife'"
        )
        _write_page_audit(self.conn)
        report = self.plan()
        dist = report.distribution
        assert dist is not None
        self.assertEqual(dist["single_attempt_probability"], "1")
        self.assertEqual(dist["expected_attempts_until_success"], "1")
        self.assertEqual(dist["success_probability_within_cap"], "1")
        self.assertEqual(dist["exhaustion_probability"], "0")
        self.assertEqual([item["attempts"] for item in dist["milestones"]], [1, 1, 1])


class PricingSnapshotTest(unittest.TestCase):
    def test_accepts_single_league_source_and_five_minute_window(self) -> None:
        decision = validate_pricing_snapshot(_materials(), _estimate())
        self.assertEqual(decision.status, "complete")
        self.assertEqual(decision.per_attempt_chaos, Decimal("2.0"))

    def test_missing_price_suppresses_entire_total(self) -> None:
        rows = (
            _line("混沌石", 1, 1),
            _line("增幅石", 2, 0.5, status="missing"),
        )
        decision = validate_pricing_snapshot(
            _materials(), _estimate(lines=rows, calculable=False)
        )
        self.assertEqual(decision.status, "incomplete")
        self.assertIsNone(decision.per_attempt_chaos)
        self.assertIn("不输出部分总成本", decision.reason)

    def test_stale_mixed_source_cross_league_and_wide_window_are_rejected(self) -> None:
        later = "2026-10-09T10:06:00+00:00"
        cases = {
            "stale": _estimate(stale=True),
            "source": _estimate(
                lines=(
                    _line("混沌石", 1, 1),
                    _line("增幅石", 2, 0.5, source="GGG Trade API"),
                ),
                source="mixed",
            ),
            "league": _estimate(
                lines=(
                    _line("混沌石", 1, 1),
                    _line("增幅石", 2, 0.5, league="Hardcore"),
                ),
            ),
            "window": _estimate(lines=(
                _line("混沌石", 1, 1),
                _line("增幅石", 2, 0.5, fetched_at=later),
            )),
        }
        for name, estimate in cases.items():
            with self.subTest(name=name):
                decision = validate_pricing_snapshot(_materials(), estimate)
                self.assertEqual(decision.status, "inconsistent_snapshot")
                self.assertIsNone(decision.per_attempt_chaos)

    def test_price_rows_must_match_explicit_material_identity_and_quantity(self) -> None:
        rows = (_line("混沌石", 2, 1), _line("增幅石", 2, 0.5))
        decision = validate_pricing_snapshot(_materials(), _estimate(lines=rows))
        self.assertEqual(decision.status, "incomplete")
        self.assertIn("显式材料", decision.reason)


class ToolApiAndRenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _populated_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def call_tool(self, **changes) -> ToolResult:
        args = {
            **TOOL_ARGS,
            "materials": MATERIAL_INPUT,
            "max_attempts": 20,
        }
        args.update(changes)
        with patch("tools.impl.craft_attempt_plan.dbmod.get_conn", return_value=self.conn):
            return craft_attempt_plan(**args)

    def test_schema_is_closed_and_tool_is_registered(self) -> None:
        spec = REGISTRY.spec("craft_attempt_plan")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertFalse(spec.parameters["additionalProperties"])
        self.assertEqual(
            set(spec.parameters["required"]),
            {"base_type", "item_level", "generation_side", "target_query", "materials", "max_attempts"},
        )
        self.assertFalse(spec.parameters["properties"]["materials"]["items"]["additionalProperties"])

    def test_tool_returns_pool_and_calculation_citations_with_boundary_answer(self) -> None:
        result = self.call_tool()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "exact")
        self.assertEqual(len(result.citations), 4)
        self.assertEqual(result.citations[-1]["claim_scope"], "calculation")
        self.assertEqual(result.citations[-1]["text"], result.data["answer"])
        self.assertIn("P50 / P90 / P95", result.data["answer"])
        self.assertIn("不是任何混沌石", result.data["answer"])
        self.assertIn("系统没有从目标反推步骤", result.data["answer"])
        self.assertNotIn("每次代表成本", result.data["answer"])

    def test_tool_prices_only_after_pool_is_exact(self) -> None:
        service = Mock()
        service.material_cost.return_value = _estimate()
        with patch("tools.impl.craft_attempt_plan.service_from_config", return_value=service):
            exact = self.call_tool(include_market_prices=True, league="Standard")
        self.assertEqual(exact.data["pricing"]["status"], "complete")
        self.assertEqual(exact.data["cost_budget"]["per_attempt_chaos"], "2")
        self.assertEqual(len(exact.citations), 5)
        service.material_cost.assert_called_once()

        service.reset_mock()
        with patch("tools.impl.craft_attempt_plan.service_from_config", return_value=service):
            unknown = self.call_tool(
                base_type="不存在的底材", include_market_prices=True
            )
        self.assertEqual(unknown.data["status"], "unknown")
        self.assertEqual(unknown.data["pricing"]["status"], "incomplete")
        service.material_cost.assert_not_called()


    def test_economy_outage_keeps_quantity_plan_but_not_partial_cost(self) -> None:
        service = Mock()
        service.material_cost.side_effect = EconomyUnavailable("timeout")
        with patch("tools.impl.craft_attempt_plan.service_from_config", return_value=service):
            result = self.call_tool(include_market_prices=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "exact")
        self.assertEqual(result.data["pricing"]["status"], "incomplete")
        self.assertIsNone(result.data["cost_budget"])
        self.assertIn("没有输出部分总价", result.data["answer"])

    def test_unknown_render_overrides_probability_and_cost(self) -> None:
        result = self.call_tool(base_type="不存在的底材")
        answer = render_attempt_plan(result.data)
        self.assertIn("不能解释为 0%", answer)
        self.assertNotIn("期望尝试次数为", answer)

    def test_api_uses_same_tool_and_returns_typed_response(self) -> None:
        request = CraftAttemptPlanRequest(
            base_type="铁制背心",
            item_level=50,
            generation_side="prefix",
            target_query="最大生命",
            materials=[
                AttemptMaterialRequest(
                    query="混沌石", quantity_per_attempt=1, category="currency"
                )
            ],
            max_attempts=10,
        )
        with patch("api.craft.build_craft_attempt_plan", return_value=self.call_tool(
            materials=[{"query": "混沌石", "quantity_per_attempt": 1, "category": "currency"}],
            max_attempts=10,
        )) as handler:
            response = independent_attempt_plan(request, _caller="test")
        self.assertEqual(response.status, "exact")
        self.assertEqual(response.distribution["attempt_cap"], 10)
        handler.assert_called_once()


class RouteBoundaryTest(unittest.TestCase):
    def test_full_fixed_syntax_routes_and_freezes_all_inputs(self) -> None:
        expected = {
            "base_type": "铁制背心",
            "item_level": 50,
            "generation_side": "prefix",
            "target_query": "最大生命",
            "materials": [
                {"query": "混沌石", "quantity_per_attempt": 1.0, "category": "auto"},
                {"query": "增幅石", "quantity_per_attempt": 2.0, "category": "auto"},
            ],
            "max_attempts": 20,
            "include_market_prices": True,
        }
        self.assertEqual(loop._craft_attempt_plan_args(QUESTION), expected)
        self.assertEqual(loop._preferred_tool_route(QUESTION), "craft_attempt_plan")
        self.assertIn("craft_attempt_plan", loop.ALLOWED_TOOLS)

    def test_missing_reset_single_selection_material_or_cap_never_routes(self) -> None:
        invalid = (
            QUESTION.replace("每次完整重置", "每次继续洗"),
            QUESTION.replace("一次普通同侧选择", "一次混沌石"),
            QUESTION.replace("每次消耗 1 个混沌石和 2 个增幅石；", ""),
            QUESTION.replace("最多尝试 20 次；", ""),
            QUESTION.replace("物品等级 50，", ""),
            QUESTION.replace("目标“最大生命”", "目标最大生命"),
        )
        for question in invalid:
            with self.subTest(question=question):
                self.assertIsNone(loop._craft_attempt_plan_args(question))
                self.assertNotEqual(loop._preferred_tool_route(question), "craft_attempt_plan")

    def test_generic_craft_cost_still_does_not_invent_model_or_materials(self) -> None:
        question = "做一件毕业胸甲的成功率和期望成本是多少"
        self.assertIsNone(loop._craft_attempt_plan_args(question))
        self.assertNotEqual(loop._preferred_tool_route(question), "craft_attempt_plan")

    def test_schema_and_execution_are_hidden_without_authorization(self) -> None:
        schemas = [
            {"type": "function", "function": {"name": "kb_search"}},
            {"type": "function", "function": {"name": "craft_attempt_plan"}},
        ]
        with patch("orchestrator.loop._prepare_tools", return_value=schemas):
            self.assertEqual(
                [item["function"]["name"] for item in loop._tools_for_question("制作成本")],
                ["kb_search", "entity_lookup", "finish_answer", "select_tools"],
            )
            self.assertEqual(
                [item["function"]["name"] for item in loop._tools_for_question(QUESTION)],
                ["kb_search", "entity_lookup", "finish_answer", "select_tools"],
            )
        with patch("orchestrator.loop.REGISTRY.call") as call:
            result, matched = loop._call_tool(
                "制作成本", "", "craft_attempt_plan", json.dumps({})
            )
        self.assertFalse(result.ok)
        self.assertFalse(matched)
        call.assert_not_called()


class MilestoneNumericalEdgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _populated_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_large_cap_remains_bounded_and_probabilities_sum_to_one(self) -> None:
        report = plan_affix_attempts(
            self.conn,
            **TOOL_ARGS,
            materials=_materials(),
            max_attempts=1_000_000,
        )
        dist = report.distribution
        assert dist is not None
        success = Decimal(dist["success_probability_within_cap"])
        failure = Decimal(dist["exhaustion_probability"])
        self.assertEqual(success + failure, Decimal(1))
        self.assertEqual(dist["expected_attempts_consumed_with_cap"], "3.33333333333333333")

    def test_pricing_decision_dataclass_is_immutable_but_report_can_replace_it(self) -> None:
        decision = validate_pricing_snapshot(_materials(), None)
        replacement = replace(decision, status="incomplete", reason="unavailable")
        self.assertEqual(decision.status, "not_requested")
        self.assertEqual(replacement.status, "incomplete")
