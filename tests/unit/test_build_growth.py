"""Build 成长规划：显式目标、确定性差额、unknown 与 API 接线。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from api.build import (
    BuildGrowthObjectiveRequest,
    BuildGrowthPlanRequest,
    BuildRequest,
    _build_response,
)
from main import create_app
from parsers.build_growth import (
    GrowthObjective,
    plan_build_growth,
    validate_objectives,
)
from parsers.build_report import BuildReport
from parsers.build_sources import DefenceSources
from parsers.build_version import BuildVersionCheck
from parsers.pob_build import PobBuild


class BuildGrowthDomainTest(unittest.TestCase):
    def test_orders_by_explicit_priority_and_calculates_exact_gaps(self) -> None:
        plan = plan_build_growth(
            PobBuild(player_stats={"LifeUnreserved": 4200, "FireResist": 70}),
            [
                GrowthObjective("life", 5000, priority=20),
                GrowthObjective("fire_resistance", 75, priority=10),
            ],
        )
        self.assertEqual([item.metric for item in plan.milestones], [
            "fire_resistance", "life",
        ])
        self.assertEqual([item.gap for item in plan.milestones], [5, 800])
        self.assertEqual(plan.next_metric, "fire_resistance")
        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.completion_percent, 0)

    def test_prefers_effective_and_unreserved_snapshot_fields(self) -> None:
        plan = plan_build_growth(
            PobBuild(player_stats={
                "Life": 5000,
                "LifeUnreserved": 4300,
                "SpellSuppressionChance": 80,
                "EffectiveSpellSuppressionChance": 72,
            }),
            [
                GrowthObjective("life", 4500, priority=1),
                GrowthObjective("spell_suppression", 100, priority=2),
            ],
        )
        life, suppression = plan.milestones
        self.assertEqual((life.source_field, life.current, life.gap), (
            "LifeUnreserved", 4300, 200,
        ))
        self.assertEqual((suppression.source_field, suppression.current), (
            "EffectiveSpellSuppressionChance", 72,
        ))

    def test_complete_plan_marks_targets_as_guardrails(self) -> None:
        plan = plan_build_growth(
            PobBuild(player_stats={"ColdResist": 80, "Armour": 30000}),
            [
                GrowthObjective("cold_resistance", 75, priority=1),
                GrowthObjective("armour", 25000, priority=2),
            ],
        )
        self.assertEqual(plan.status, "complete")
        self.assertEqual((plan.met, plan.remaining, plan.unknown), (2, 0, 0))
        self.assertEqual(plan.completion_percent, 100)
        self.assertEqual(plan.next_metric, "")
        self.assertTrue(all(item.status == "met" for item in plan.milestones))
        self.assertTrue(all("保持不低于" in item.action for item in plan.milestones))

    def test_missing_snapshot_fields_remain_unknown_without_estimates(self) -> None:
        plan = plan_build_growth(
            PobBuild(player_stats={"Life": 4000}),
            [
                GrowthObjective("life", 5000, priority=1),
                GrowthObjective("chaos_max_hit", 20000, priority=2),
            ],
        )
        self.assertEqual(plan.status, "partial")
        self.assertEqual((plan.remaining, plan.unknown), (1, 1))
        unknown = plan.milestones[1]
        self.assertEqual(unknown.status, "unknown")
        self.assertIsNone(unknown.current)
        self.assertIsNone(unknown.gap)
        self.assertEqual(unknown.expected_gain, "")
        self.assertIn("ChaosMaximumHitTaken", unknown.action)

    def test_all_missing_targets_produce_unknown_plan(self) -> None:
        plan = plan_build_growth(
            PobBuild(), [GrowthObjective("dps", 1_000_000)]
        )
        self.assertEqual(plan.status, "unknown")
        self.assertEqual(plan.completion_percent, 0)
        self.assertEqual(plan.next_metric, "")

    def test_snapshot_origin_and_comparison_scope_are_explicit(self) -> None:
        plan = plan_build_growth(
            PobBuild(
                player_stats={"FullDPS": 750000},
                player_stats_basis="取自你确认的截图转录",
            ),
            [GrowthObjective("dps", 1_000_000, label="Boss 输出")],
        )
        milestone = plan.milestones[0]
        self.assertEqual(milestone.label, "Boss 输出")
        self.assertEqual(milestone.source_field, "FullDPS")
        self.assertEqual(milestone.current_source, "取自你确认的截图转录")
        self.assertEqual(milestone.target_source, "用户显式提交的成长目标")
        self.assertIn("相同技能、敌人档位和配置", milestone.verification)

    def test_plan_does_not_invent_items_crafts_prices_or_recalculated_dps(self) -> None:
        plan = plan_build_growth(
            PobBuild(player_stats={"CombinedDPS": 100000}),
            [GrowthObjective("dps", 200000)],
        )
        wire = " ".join((
            plan.note,
            plan.milestones[0].action,
            plan.milestones[0].assumptions,
        ))
        self.assertIn("不猜具体装备或工艺", wire)
        self.assertIn("市场价格均未评估", wire)
        self.assertIn("不把 PoB 数值冒充本库重算结果", wire)

    def test_domain_rejects_duplicate_unknown_invalid_and_oversized_targets(self) -> None:
        cases = (
            [GrowthObjective("life", 1), GrowthObjective("life", 2)],
            [GrowthObjective("not-a-metric", 1)],
            [GrowthObjective("life", -1)],
            [GrowthObjective("life", float("inf"))],
            [GrowthObjective("life", 1, priority=0)],
            [],
        )
        for objectives in cases:
            with self.subTest(objectives=objectives), self.assertRaises(ValueError):
                validate_objectives(objectives)


class BuildGrowthApiTest(unittest.TestCase):
    def test_request_validation_rejects_duplicate_boolean_and_negative_target(self) -> None:
        invalid = (
            {"objectives": [
                {"metric": "life", "target": 5000},
                {"metric": "life", "target": 6000},
            ]},
            {"objectives": [{"metric": "life", "target": True}]},
            {"objectives": [{"metric": "life", "target": "5000"}]},
            {"objectives": [{"metric": "armour", "target": -1}]},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                BuildGrowthPlanRequest.model_validate(payload)
        # 负抗性目标本身是有效的用户输入，不应被偷偷改成常规上限。
        valid = BuildGrowthObjectiveRequest(
            metric="chaos_resistance", target=-20
        )
        self.assertEqual(valid.target, -20)

    def test_build_request_and_openapi_expose_growth_plan(self) -> None:
        request = BuildRequest.model_validate({
            "code": "abc",
            "growth_plan": {"objectives": [
                {"metric": "life", "target": 5000, "priority": 1},
            ]},
        })
        self.assertEqual(request.growth_plan.objectives[0].metric, "life")
        schema = create_app().openapi()
        props = schema["components"]["schemas"]["BuildRequest"]["properties"]
        response_props = schema["components"]["schemas"]["BuildResponse"]["properties"]
        self.assertIn("growth_plan", props)
        self.assertIn("growth_plan", response_props)

    def test_build_response_includes_the_same_deterministic_plan(self) -> None:
        query = BuildGrowthPlanRequest.model_validate({
            "objectives": [
                {"metric": "fire_resistance", "target": 75, "priority": 1},
                {"metric": "life", "target": 5000, "priority": 2},
            ],
        })
        build = PobBuild(player_stats={"FireResist": 60, "Life": 4500})
        with (
            patch("api.build.dbmod.get_conn", return_value=object()),
            patch("api.build.build_health_report", return_value=BuildReport()),
            patch("api.build.build_defence_sources", return_value=DefenceSources()),
            patch("api.build.build_suggestions", return_value=[]),
            patch(
                "api.build.check_build_version", return_value=BuildVersionCheck()
            ),
        ):
            response = _build_response(
                build,
                input_type="pob",
                caller="test",
                growth_plan_query=query,
            )
        self.assertIsNotNone(response.growth_plan)
        self.assertEqual(response.growth_plan.status, "ready")
        self.assertEqual(response.growth_plan.next_metric, "fire_resistance")
        self.assertEqual(
            [item.gap for item in response.growth_plan.milestones], [15, 500]
        )


if __name__ == "__main__":
    unittest.main()
