"""多技能动作分配、互斥 DoT、组合时间线、比较与反解。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from api.calculator import (
    HitDpsRequest,
    RotationCompareRequest,
    RotationDpsRequest,
    RotationScenarioRequest,
    RotationSkillRequest,
    RotationTargetRequest,
    calculate_rotation_dps,
    calculate_rotation_target_multiplier,
    compare_rotation_dps_scenarios,
)
from calc import constants
from calc.dps import hit_dps
from calc.dps_rotation import compare_rotations, rotation_dps, target_rate_multiplier
from tests.unit.test_dps import AILMENT_CONSTANT_VALUES, _ailment_constant
from tools.impl.dps_rotation import (
    rotation_dps_calc,
    rotation_dps_compare,
    rotation_dps_target_multiplier,
)
from tools.registry import REGISTRY


def _hit_skill(name: str, *, damage: float = 100, rate: float = 1,
               allocation: float | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "name": name,
        "inputs": {"base_damage": {"fire": [damage, damage]}, "action_rate": rate},
    }
    if allocation is not None:
        result["allocation_pct"] = allocation
    return result


class RotationDpsCoreTest(unittest.TestCase):
    def test_rotation_allocates_action_time_before_single_skill_calculation(self) -> None:
        result = rotation_dps(skills=[
            _hit_skill("火球", rate=2, allocation=50),
            _hit_skill("冰矛", rate=2, allocation=50),
        ])
        self.assertEqual("rotation", result.mode)
        self.assertEqual(100, result.action_budget_pct)
        self.assertEqual(0, result.unallocated_action_budget_pct)
        self.assertEqual(200, result.steady_combined_dps)
        self.assertEqual([1, 1], [row.effective_action_rate for row in result.skills])

    def test_rotation_rejects_missing_or_overallocated_budget(self) -> None:
        with self.assertRaisesRegex(ValueError, "allocation_pct"):
            rotation_dps(skills=[_hit_skill("A"), _hit_skill("B")])
        with patch("calc.dps_rotation.hit_dps") as calculate:
            with self.assertRaisesRegex(ValueError, "合计不能超过"):
                rotation_dps(skills=[
                    _hit_skill("A", allocation=60), _hit_skill("B", allocation=50),
                ])
            calculate.assert_not_called()

    def test_parallel_mode_defaults_each_independent_source_to_full_uptime(self) -> None:
        result = rotation_dps(
            mode="parallel", skills=[_hit_skill("图腾 A"), _hit_skill("图腾 B")],
        )
        self.assertEqual(200, result.action_budget_pct)
        self.assertIsNone(result.unallocated_action_budget_pct)
        self.assertEqual(200, result.steady_total_dps)

    def test_allocated_frequency_still_respects_trigger_cooldown(self) -> None:
        first = _hit_skill("触发技能", rate=10, allocation=50)
        first["inputs"].update({"triggered": True, "cooldown_seconds": 1})  # type: ignore[union-attr]
        result = rotation_dps(skills=[first, _hit_skill("填充技能", allocation=50)])
        self.assertEqual(1, result.skills[0].effective_action_rate)
        self.assertEqual(100, result.skills[0].hit_dps)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    @patch("calc.dps_rotation.constants.get", side_effect=_ailment_constant)
    def test_single_stack_ignite_is_automatically_strongest_across_skills(
        self, _rotation_get: object, _dps_get: object,
    ) -> None:
        weak = _hit_skill("小点燃", damage=1, allocation=50)
        strong = _hit_skill("大点燃", damage=1, allocation=50)
        weak["inputs"]["damaging_ailments"] = [{  # type: ignore[index]
            "name": "ignite", "source_damage": [100, 100], "chance_on_hit_pct": 100,
        }]
        strong["inputs"]["damaging_ailments"] = [{  # type: ignore[index]
            "name": "ignite", "source_damage": [200, 200], "chance_on_hit_pct": 100,
        }]
        result = rotation_dps(skills=[weak, strong])
        self.assertEqual(1, len(result.dot_groups))
        self.assertEqual("auto:ignite:strongest", result.dot_groups[0].name)
        self.assertEqual("大点燃:ignite", result.dot_groups[0].steady_selected_component_id)
        self.assertEqual(180, result.steady_total_dot_dps)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    @patch("calc.dps_rotation.constants.get", side_effect=_ailment_constant)
    def test_poison_sources_sum_without_implicit_exclusion(
        self, _rotation_get: object, _dps_get: object,
    ) -> None:
        skills = []
        for name in ("A", "B"):
            skill = _hit_skill(name, damage=1, allocation=50)
            skill["inputs"]["damaging_ailments"] = [{  # type: ignore[index]
                "name": "poison", "source_damage": [100, 100], "chance_on_hit_pct": 100,
            }]
            skills.append(skill)
        result = rotation_dps(skills=skills)
        self.assertEqual(0, len(result.dot_groups))
        self.assertEqual(60, result.steady_total_dot_dps)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    @patch("calc.dps_rotation.constants.get", side_effect=_ailment_constant)
    def test_explicit_multistack_bleeds_are_not_auto_exclusive(
        self, _rotation_get: object, _dps_get: object,
    ) -> None:
        skills = []
        for name in ("A", "B"):
            skill = _hit_skill(name, damage=1, allocation=50)
            skill["inputs"]["damaging_ailments"] = [{  # type: ignore[index]
                "name": "bleed", "source_damage": [100, 100], "chance_on_hit_pct": 100,
                "stack_limit": 8,
            }]
            skills.append(skill)
        result = rotation_dps(skills=skills)
        self.assertFalse(result.dot_groups)
        self.assertGreater(result.steady_total_dot_dps, 0)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    @patch("calc.dps_rotation.constants.get", side_effect=_ailment_constant)
    def test_explicit_skill_dot_group_keeps_only_strongest_source(
        self, _rotation_get: object, _dps_get: object,
    ) -> None:
        first = _hit_skill("火墙", damage=0, allocation=50)
        second = _hit_skill("灼热地面", damage=0, allocation=50)
        first["inputs"]["skill_dot"] = {"base_damage_per_second": {"fire": 100}}  # type: ignore[index]
        second["inputs"]["skill_dot"] = {"base_damage_per_second": {"fire": 250}}  # type: ignore[index]
        result = rotation_dps(
            skills=[first, second],
            dot_groups=[{
                "name": "燃烧地面", "policy": "strongest",
                "members": [
                    {"skill": "火墙", "component": "skill_dot"},
                    {"skill": "灼热地面", "component": "skill_dot"},
                ],
            }],
        )
        self.assertEqual(250, result.steady_total_dot_dps)
        self.assertEqual("灼热地面:skill_dot", result.dot_groups[0].steady_selected_component_id)

    @patch("calc.dps.constants.get", side_effect=lambda key: {
        **AILMENT_CONSTANT_VALUES, constants.DOT_DPS_CAP: 100.0,
    }[key])
    @patch("calc.dps_rotation.constants.get", side_effect=lambda key: {
        **AILMENT_CONSTANT_VALUES, constants.DOT_DPS_CAP: 100.0,
    }[key])
    def test_global_dot_cap_is_applied_once_after_cross_skill_composition(
        self, _rotation_get: object, _dps_get: object,
    ) -> None:
        skills = []
        for name in ("A", "B"):
            skill = _hit_skill(name, damage=0, allocation=50)
            skill["inputs"]["skill_dot"] = {  # type: ignore[index]
                "base_damage_per_second": {"chaos": 80},
            }
            skills.append(skill)
        result = rotation_dps(skills=skills)
        self.assertEqual(160, result.steady_total_dot_dps_uncapped)
        self.assertEqual(100, result.steady_total_dot_dps)
        self.assertTrue(result.steady_total_dot_dps_capped)

    def test_top_level_cull_is_applied_once_to_composed_total(self) -> None:
        result = rotation_dps(
            skills=[_hit_skill("A", allocation=50), _hit_skill("B", allocation=50)],
            cull_percent=20,
        )
        self.assertEqual(100, result.steady_total_dps)
        self.assertEqual(125, result.effective_steady_total_dps)

    def test_individual_skill_cull_and_duration_are_rejected(self) -> None:
        cull = _hit_skill("A", allocation=50)
        cull["inputs"]["cull_percent"] = 20  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "顶层 cull_percent"):
            rotation_dps(skills=[cull, _hit_skill("B", allocation=50)])
        duration = _hit_skill("A", allocation=50)
        duration["inputs"]["combat_duration_seconds"] = 4  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "顶层统一"):
            rotation_dps(skills=[duration, _hit_skill("B", allocation=50)])

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    @patch("calc.dps_rotation.constants.get", side_effect=_ailment_constant)
    def test_aligned_combat_timeline_composes_components_before_averaging(
        self, _rotation_get: object, _dps_get: object,
    ) -> None:
        skills = []
        for name in ("A", "B"):
            skill = _hit_skill(name, damage=0, allocation=50)
            skill["inputs"]["skill_dot"] = {  # type: ignore[index]
                "base_damage_per_second": {"chaos": 100},
                "can_stack": True, "duration_seconds": 2,
            }
            skills.append(skill)
        result = rotation_dps(skills=skills, combat_duration_seconds=4)
        timeline = result.combat_timeline
        self.assertIsNotNone(timeline)
        assert timeline is not None
        self.assertEqual("aligned_component_simpson", timeline.calculation_mode)
        self.assertEqual(512, timeline.integration_steps)
        self.assertAlmostEqual(150, timeline.average_total_dot_dps, places=4)
        self.assertEqual(21, len(timeline.points))
        self.assertEqual(0, timeline.points[0].total_dot_dps)
        self.assertEqual(200, timeline.points[-1].total_dot_dps)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    @patch("calc.dps_rotation.constants.get", side_effect=_ailment_constant)
    def test_strongest_group_can_switch_dominant_skill_during_combat(
        self, _rotation_get: object, _dps_get: object,
    ) -> None:
        slow = _hit_skill("慢速重击", damage=1, allocation=10)
        fast = _hit_skill("快速轻击", damage=1, allocation=90)
        slow["inputs"]["damaging_ailments"] = [{  # type: ignore[index]
            "name": "ignite", "source_damage": [300, 300], "chance_on_hit_pct": 100,
        }]
        fast["inputs"]["damaging_ailments"] = [{  # type: ignore[index]
            "name": "ignite", "source_damage": [100, 100], "chance_on_hit_pct": 100,
        }]
        result = rotation_dps(skills=[slow, fast], combat_duration_seconds=10)
        self.assertEqual(1, len(result.dot_groups))
        self.assertGreater(result.dot_groups[0].dominant_switches or 0, 0)

    def test_custom_group_validation_fails_closed(self) -> None:
        skills = [_hit_skill("A", allocation=50), _hit_skill("B", allocation=50)]
        with self.assertRaisesRegex(ValueError, "不存在"):
            rotation_dps(skills=skills, dot_groups=[{
                "name": "错误组", "members": [
                    {"skill": "A", "component": "skill_dot"},
                    {"skill": "B", "component": "skill_dot"},
                ],
            }])

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    @patch("calc.dps_rotation.constants.get", side_effect=_ailment_constant)
    def test_automatic_group_name_cannot_collide_with_explicit_group(
        self, _rotation_get: object, _dps_get: object,
    ) -> None:
        skills = []
        for name in ("点燃 A", "点燃 B"):
            skill = _hit_skill(name, damage=1, allocation=50)
            skill["inputs"]["damaging_ailments"] = [{  # type: ignore[index]
                "name": "ignite", "source_damage": [100, 100],
                "chance_on_hit_pct": 100,
            }]
            skill["inputs"]["skill_dot"] = {  # type: ignore[index]
                "base_damage_per_second": {"fire": 10},
            }
            skills.append(skill)
        result = rotation_dps(skills=skills, dot_groups=[{
            "name": "auto:ignite:strongest",
            "members": [
                {"skill": "点燃 A", "component": "skill_dot"},
                {"skill": "点燃 B", "component": "skill_dot"},
            ],
        }])
        self.assertEqual(
            ["auto:ignite:strongest", "auto:ignite:strongest:2"],
            [group.name for group in result.dot_groups],
        )
        self.assertEqual(100, result.steady_total_dot_dps)


class RotationDpsAnalysisTest(unittest.TestCase):
    def test_compare_rotations_reports_steady_and_combat_best(self) -> None:
        slow = {
            "skills": [_hit_skill("A", allocation=50), _hit_skill("B", allocation=50)],
            "combat_duration_seconds": 4,
        }
        fast = {
            "skills": [
                _hit_skill("A", rate=2, allocation=50),
                _hit_skill("B", rate=2, allocation=50),
            ],
            "combat_duration_seconds": 4,
        }
        comparison = compare_rotations([("慢速", slow), ("快速", fast)])
        self.assertEqual("快速", comparison.best_steady_scenario)
        self.assertEqual("快速", comparison.best_combat_scenario)
        self.assertEqual(100, comparison.scenarios[1].steady_delta)

    def test_target_multiplier_scales_all_skill_rates_and_back_substitutes(self) -> None:
        inputs = {
            "skills": [_hit_skill("A", allocation=50), _hit_skill("B", allocation=50)],
        }
        target = target_rate_multiplier(200, inputs)
        self.assertTrue(target.reachable)
        self.assertAlmostEqual(2, target.required_rate_multiplier or 0, places=6)
        self.assertAlmostEqual(200, target.achieved_dps or 0, places=5)

    def test_combat_target_requires_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "combat_duration_seconds"):
            target_rate_multiplier(200, {
                "skills": [_hit_skill("A", allocation=50), _hit_skill("B", allocation=50)],
            }, metric="combat_average_total_dps")

    def test_single_skill_sampler_requires_duration_and_valid_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "一起提供"):
            hit_dps(
                base_damage={"fire": [100, 100]}, action_rate=1,
                combat_sample_times=[0, 1],
            )
        with self.assertRaisesRegex(ValueError, "不能超过"):
            hit_dps(
                base_damage={"fire": [100, 100]}, action_rate=1,
                combat_duration_seconds=1, combat_sample_times=[2],
            )


class RotationDpsSurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.constant = patch("tools.impl.dps.constants.get", return_value=150.0)
        self.provenance = patch(
            "tools.impl.dps.constants.provenance",
            return_value=SimpleNamespace(
                url="https://github.com/x/PathOfBuilding/blob/" + "c" * 40
                + "/src/Data/Misc.lua#L1",
                source="Path of Building", source_priority=2,
            ),
        )
        self.constant.start()
        self.provenance.start()

    def tearDown(self) -> None:
        self.provenance.stop()
        self.constant.stop()

    @staticmethod
    def _request(rate: float = 1) -> RotationDpsRequest:
        return RotationDpsRequest(skills=[
            RotationSkillRequest(
                name="A", allocation_pct=50,
                inputs=HitDpsRequest(base_damage={"fire": (100, 100)}, action_rate=rate),
            ),
            RotationSkillRequest(
                name="B", allocation_pct=50,
                inputs=HitDpsRequest(base_damage={"fire": (100, 100)}, action_rate=rate),
            ),
        ])

    def test_tool_exposes_rotation_breakdown_and_boundary(self) -> None:
        result = rotation_dps_calc(skills=[
            _hit_skill("A", allocation=50), _hit_skill("B", allocation=50),
        ])
        self.assertTrue(result.ok)
        self.assertEqual(100, result.data["steady_total_dps"])
        self.assertIn("多技能组合 DPS", result.data["answer"])
        self.assertIn("不能称为完整 Build DPS", result.data["boundary"])

    def test_http_calculate_compare_and_target_endpoints(self) -> None:
        response = calculate_rotation_dps(self._request(), _caller="test")
        self.assertEqual(100, response["steady_total_dps"])
        comparison = compare_rotation_dps_scenarios(RotationCompareRequest(scenarios=[
            RotationScenarioRequest(name="慢速", inputs=self._request()),
            RotationScenarioRequest(name="快速", inputs=self._request(rate=2)),
        ]), _caller="test")
        self.assertEqual("快速", comparison["best_steady_scenario"])
        target = calculate_rotation_target_multiplier(RotationTargetRequest(
            target_dps=200, inputs=self._request(),
        ), _caller="test")
        self.assertTrue(target["reachable"])
        self.assertAlmostEqual(2, target["required_rate_multiplier"], places=6)

    def test_compare_and_target_tools_render_results(self) -> None:
        slow = {"skills": [_hit_skill("A", allocation=50), _hit_skill("B", allocation=50)]}
        fast = {"skills": [
            _hit_skill("A", rate=2, allocation=50),
            _hit_skill("B", rate=2, allocation=50),
        ]}
        comparison = rotation_dps_compare(scenarios=[
            {"name": "慢速", "inputs": slow}, {"name": "快速", "inputs": fast},
        ])
        self.assertTrue(comparison.ok)
        self.assertIn("最高稳态总 DPS：快速", comparison.data["answer"])
        target = rotation_dps_target_multiplier(target_dps=200, inputs=slow)
        self.assertTrue(target.ok)
        self.assertIn("统一乘以 2", target.data["answer"])

    def test_registry_agent_and_http_routes_are_wired(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS
        from main import create_app

        for name in (
            "rotation_dps_calc", "rotation_dps_compare",
            "rotation_dps_target_multiplier",
        ):
            self.assertIn(name, ALLOWED_TOOLS)
            self.assertIsNotNone(REGISTRY.spec(name))
        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/calc/rotation-dps", paths)
        self.assertIn("/poe_agent/calc/rotation-dps/compare", paths)
        self.assertIn("/poe_agent/calc/rotation-dps/target-multiplier", paths)

    def test_natural_language_routes_to_rotation_tools(self) -> None:
        from orchestrator.loop import _preferred_tool_route

        self.assertEqual("rotation_dps_calc", _preferred_tool_route("两个技能的轮转 DPS 怎么算"))
        self.assertEqual(
            "rotation_dps_compare", _preferred_tool_route("多技能 DPS 方案对比"),
        )
        self.assertEqual(
            "rotation_dps_target_multiplier",
            _preferred_tool_route("整套循环要提速多少才能达到目标"),
        )
