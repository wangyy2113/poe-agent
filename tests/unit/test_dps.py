"""单技能命中 DPS 主链：公式、转化、边界、工具与 API。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.calculator import (  # noqa: E402
    DamagingAilmentRequest,
    HitDpsCompareRequest,
    HitDpsRequest,
    HitDpsScenarioRequest,
    HitDpsTargetRateInputs,
    HitDpsTargetRateRequest,
    calculate_hit_dps,
    calculate_hit_dps_target_rate,
    compare_hit_dps_scenarios,
)
from calc import dps as dps_module  # noqa: E402
from calc.dps import Conversion, effective_action_rate, effective_cooldown, hit_dps  # noqa: E402
from calc.dps_analysis import compare_hit_dps, target_action_rate  # noqa: E402
from calc import constants  # noqa: E402
from tools.impl.dps import hit_dps_calc, hit_dps_compare, hit_dps_target_rate  # noqa: E402
from tests.unit.test_resistance import POB_ROOT_ENV  # noqa: E402


AILMENT_CONSTANT_VALUES = {
    constants.BASE_CRIT_MULTIPLIER: 150.0,
    constants.IGNITE_PERCENT: 0.9,
    constants.IGNITE_DURATION: 4.0,
    constants.POISON_PERCENT: 0.3,
    constants.POISON_DURATION: 2.0,
    constants.BLEED_PERCENT: 70.0,
    constants.BLEED_DURATION: 5.0,
    constants.DOT_DPS_CAP: (2**31 - 1) / 60,
    constants.MONSTER_MAX_PHYS_DR: 75.0,
}


def _ailment_constant(key: str) -> float:
    return AILMENT_CONSTANT_VALUES[key]


class HitDpsCoreTest(unittest.TestCase):
    def test_full_scalar_chain(self) -> None:
        result = hit_dps(
            base_damage={"fire": [100, 200]}, added_damage={"fire": [20, 40]},
            damage_effectiveness=0.5, increased_damage_pct=100,
            more_damage_multipliers=[1.2, 1.5], enemy_resistance={"fire": 50},
            penetration={"fire": 20}, hit_chance_pct=90, crit_chance_pct=50,
            crit_multiplier_pct=300, action_rate=2,
        )
        # (100..200 + 10..20) * 2 * 1.2 * 1.5 = 396..792;
        # 30% effective resistance, midpoint 415.8; crit x2; hit .9; rate 2.
        self.assertAlmostEqual(1496.88, result.dps, places=6)
        self.assertAlmostEqual(2.0, result.crit_effect)

    def test_physical_to_lightning_to_cold_keeps_all_type_modifiers(self) -> None:
        result = hit_dps(
            base_damage={"physical": [100, 100]}, action_rate=1,
            increased_by_type={"physical": 100, "lightning": 50, "cold": 50},
            conversions=[
                Conversion("physical", "lightning", 100),
                Conversion("lightning", "cold", 100),
            ],
        )
        self.assertEqual(["cold"], [item.damage_type for item in result.by_type])
        cold = next(item for item in result.by_type if item.damage_type == "cold")
        self.assertEqual(300, cold.before_mitigation.average)
        self.assertEqual(300, result.dps)

    def test_global_conversion_over_cap_is_scaled(self) -> None:
        result = hit_dps(
            base_damage={"physical": [100, 100]}, action_rate=1,
            conversions=[
                Conversion("physical", "fire", 80),
                Conversion("physical", "cold", 80),
            ],
        )
        self.assertAlmostEqual(0.5, result.conversion["physical"]["fire"])
        self.assertAlmostEqual(0.5, result.conversion["physical"]["cold"])
        self.assertEqual(100, result.non_crit_hit.average)

    def test_skill_conversion_has_priority_over_global(self) -> None:
        result = hit_dps(
            base_damage={"physical": [100, 100]}, action_rate=1,
            conversions=[
                Conversion("physical", "cold", 60, "skill"),
                Conversion("physical", "fire", 80, "global"),
            ],
        )
        self.assertAlmostEqual(0.6, result.conversion["physical"]["cold"])
        self.assertAlmostEqual(0.4, result.conversion["physical"]["fire"])

    def test_gain_as_extra_does_not_consume_source(self) -> None:
        result = hit_dps(
            base_damage={"physical": [100, 100]}, action_rate=1,
            conversions=[Conversion("physical", "fire", 30, "gain")],
        )
        self.assertEqual(130, result.non_crit_hit.average)
        self.assertEqual(1.0, result.conversion["physical"]["mult"])

    def test_each_final_type_uses_its_own_resistance_and_penetration(self) -> None:
        result = hit_dps(
            base_damage={"fire": [100, 100], "cold": [100, 100]}, action_rate=1,
            enemy_resistance={"fire": 75, "cold": 50}, penetration={"fire": 25},
        )
        self.assertEqual(100, result.non_crit_hit.average)

    def test_physical_reduction_is_separate_from_resistance(self) -> None:
        result = hit_dps(
            base_damage={"physical": [100, 100]}, action_rate=1,
            enemy_physical_reduction_pct=40,
        )
        self.assertAlmostEqual(60, result.dps)

    def test_rejects_reverse_conversion(self) -> None:
        with self.assertRaisesRegex(ValueError, "转化顺序"):
            hit_dps(
                base_damage={"fire": [100, 100]}, action_rate=1,
                conversions=[Conversion("fire", "physical", 10)],
            )

    def test_rejects_bad_ranges_and_probabilities(self) -> None:
        with self.assertRaisesRegex(ValueError, "最小值"):
            hit_dps(base_damage={"fire": [200, 100]}, action_rate=1)
        with self.assertRaisesRegex(ValueError, "0%..100%"):
            hit_dps(base_damage={"fire": [1, 2]}, action_rate=1, hit_chance_pct=101)

    def test_uses_pob_half_up_rounding_not_python_bankers_rounding(self) -> None:
        result = hit_dps(base_damage={"fire": [0.5, 2.5]}, action_rate=1)
        self.assertEqual((1.0, 3.0), (result.non_crit_hit.minimum, result.non_crit_hit.maximum))

    def test_zero_minimum_conversion_keeps_pob_unrounded_incoming_branch(self) -> None:
        result = hit_dps(
            base_damage={"physical": [0, 1]}, action_rate=1,
            conversions=[Conversion("physical", "fire", 50)],
        )
        fire = next(item for item in result.by_type if item.damage_type == "fire")
        self.assertEqual((0.0, 0.5), (fire.before_mitigation.minimum, fire.before_mitigation.maximum))

    def test_rejects_physical_resistance_penetration_and_negative_more(self) -> None:
        with self.assertRaisesRegex(ValueError, "enemy_physical_reduction_pct"):
            hit_dps(base_damage={"fire": [1, 2]}, action_rate=1,
                    enemy_resistance={"physical": 10})
        with self.assertRaisesRegex(ValueError, "压倒"):
            hit_dps(base_damage={"fire": [1, 2]}, action_rate=1,
                    penetration={"physical": 10})
        with self.assertRaisesRegex(ValueError, "more_by_type"):
            hit_dps(base_damage={"fire": [1, 2]}, action_rate=1,
                    more_by_type={"fire": -1})

    def test_rejects_overflowing_multiplier_chain(self) -> None:
        with self.assertRaisesRegex(ValueError, "乘积"):
            hit_dps(base_damage={"fire": [1, 2]}, action_rate=1,
                    more_damage_multipliers=[1e15, 1e15])

    def test_lucky_unlucky_and_extreme_lucky_follow_pob_expectation(self) -> None:
        lucky = hit_dps(base_damage={"fire": [100, 200]}, action_rate=1,
                        damage_roll_chance_pct=100)
        unlucky = hit_dps(base_damage={"fire": [100, 200]}, action_rate=1,
                          damage_roll_chance_pct=-100)
        extreme = hit_dps(base_damage={"fire": [100, 200]}, action_rate=1,
                          damage_roll_chance_pct=100, extreme_luck=True)
        self.assertAlmostEqual(500 / 3, lucky.non_crit_average)
        self.assertAlmostEqual(400 / 3, unlucky.non_crit_average)
        self.assertAlmostEqual(175, extreme.non_crit_average)

    def test_crit_can_use_a_separate_damage_roll_mode(self) -> None:
        result = hit_dps(
            base_damage={"fire": [100, 200]}, action_rate=1,
            crit_chance_pct=50, crit_multiplier_pct=200,
            damage_roll_chance_pct=-100, crit_damage_roll_chance_pct=100,
        )
        self.assertAlmostEqual(400 / 3, result.non_crit_average)
        self.assertAlmostEqual(1000 / 3, result.crit_average)
        self.assertAlmostEqual(700 / 3, result.average_hit)
        self.assertEqual(-100, result.damage_roll_chance_pct)
        self.assertEqual(100, result.crit_damage_roll_chance_pct)
        self.assertFalse(result.extreme_luck)

    def test_triple_damage_overrides_overlapping_double_damage(self) -> None:
        result = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=1,
            double_damage_chance_pct=50, triple_damage_chance_pct=20,
        )
        self.assertEqual(40, result.double_damage_chance_pct)
        self.assertEqual(20, result.triple_damage_chance_pct)
        self.assertAlmostEqual(1.8, result.scaled_damage_effect)
        self.assertAlmostEqual(180, result.dps)

    def test_on_crit_double_and_triple_chances_are_weighted_first(self) -> None:
        result = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=1,
            crit_chance_pct=50, crit_multiplier_pct=100,
            double_damage_chance_pct=100, triple_damage_chance_on_crit_pct=100,
        )
        self.assertEqual(50, result.triple_damage_chance_pct)
        self.assertEqual(50, result.double_damage_chance_pct)
        self.assertEqual(2.5, result.scaled_damage_effect)

    def test_resistance_inversion_ignore_and_unmodifiable_modes(self) -> None:
        inverted = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=1,
            enemy_resistance={"fire": 75}, penetration={"fire": 25},
            resistance_inversion_chance_pct={"fire": 100},
        )
        self.assertEqual(200, inverted.dps)
        ignored_half = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=1,
            enemy_resistance={"fire": 75}, penetration={"fire": 25},
            resistance_inversion_chance_pct={"fire": 100},
            resistance_ignore_chance_pct={"fire": 50},
        )
        self.assertEqual(150, ignored_half.dps)
        immutable = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=1,
            enemy_resistance={"fire": 75}, penetration={"fire": 25},
            resistance_inversion_chance_pct={"fire": 100},
            resistance_ignore_chance_pct={"fire": 100},
            unmodifiable_resistance_types=["fire"],
        )
        self.assertEqual(175, immutable.dps)
        self.assertTrue(immutable.by_type[0].resistance_unmodifiable)

    def test_overwhelm_and_chance_to_ignore_physical_reduction(self) -> None:
        result = hit_dps(
            base_damage={"physical": [100, 100]}, action_rate=1,
            enemy_physical_reduction_pct=40,
            overwhelm_physical_reduction_pct=30,
            physical_reduction_ignore_chance_pct=50,
        )
        self.assertEqual(5, result.by_type[0].effective_resistance)
        self.assertEqual(95, result.dps)

    def test_per_type_damage_taken_modifiers_stack_with_global(self) -> None:
        result = hit_dps(
            base_damage={"fire": [100, 100], "cold": [100, 100]}, action_rate=1,
            enemy_damage_taken_increased_pct=30,
            enemy_damage_taken_increased_by_type={"fire": 20},
            enemy_damage_taken_more_multiplier=1.2,
            enemy_damage_taken_more_by_type={"fire": 1.5},
        )
        fire = next(item for item in result.by_type if item.damage_type == "fire")
        cold = next(item for item in result.by_type if item.damage_type == "cold")
        self.assertAlmostEqual(270, fire.non_crit_average)
        self.assertAlmostEqual(156, cold.non_crit_average)

    def test_triggered_and_non_triggered_cooldowns_use_different_rates(self) -> None:
        self.assertEqual(5, effective_action_rate(10, 0.2, triggered=True))
        self.assertAlmostEqual(10 / 3, effective_action_rate(10, 0.2, triggered=False))
        triggered = hit_dps(base_damage={"fire": [100, 100]}, action_rate=10,
                            cooldown_seconds=0.2, triggered=True)
        self.assertEqual(500, triggered.dps)

    def test_burst_damage_counts_first_hit_and_penalized_repeats(self) -> None:
        result = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=1,
            burst_hit_count=4, repeat_damage_multiplier=0.5,
        )
        self.assertEqual(250, result.average_burst_damage)
        self.assertEqual(100, result.dps, "重复爆发不能偷偷乘进持续 DPS")

    def test_server_tick_rounds_single_use_cooldown_but_not_stored_uses(self) -> None:
        rounded, changed = effective_cooldown(0.2, server_tick_seconds=0.033)
        self.assertAlmostEqual(0.231, rounded)
        self.assertTrue(changed)
        unchanged, changed = effective_cooldown(
            0.2, server_tick_seconds=0.033, cooldown_uses=2
        )
        self.assertEqual(0.2, unchanged)
        self.assertFalse(changed)
        result = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=10,
            cooldown_seconds=0.2, triggered=True, server_tick_seconds=0.033,
        )
        self.assertAlmostEqual(1 / 0.231, result.action_rate)
        self.assertTrue(result.cooldown_tick_rounded)
        capped = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=100,
            server_tick_seconds=0.033, action_repeats=2,
        )
        self.assertAlmostEqual(2 / 0.033, capped.action_rate)
        self.assertTrue(capped.action_rate_tick_capped)
        channelled = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=100,
            server_tick_seconds=0.033, channelled=True,
        )
        self.assertEqual(100, channelled.action_rate)

    def test_ruthless_fist_of_war_and_exert_uptime_compose(self) -> None:
        result = hit_dps(
            base_damage={"physical": [100, 100]}, action_rate=2,
            ruthless_blow_every_hits=3, ruthless_blow_hit_multiplier=2,
            fist_of_war_more_damage_pct=100, fist_of_war_cooldown_seconds=1,
            exerted_attack_more_damage_pct=50, exerted_attack_uptime_pct=40,
        )
        self.assertEqual(33, result.ruthless_blow_chance_pct)
        self.assertAlmostEqual(1.33, result.ruthless_blow_effect)
        self.assertEqual(50, result.fist_of_war_uptime_pct)
        self.assertAlmostEqual(1.5, result.fist_of_war_effect)
        self.assertAlmostEqual(1.2, result.exerted_attack_effect)
        self.assertAlmostEqual(478.8, result.dps)

    def test_impale_uses_pre_mitigation_physical_hit_and_separate_reduction(self) -> None:
        result = hit_dps(
            base_damage={"physical": [100, 100]}, action_rate=2,
            enemy_physical_reduction_pct=20,
            impale_chance_pct=100, impale_stacks=5,
            impale_effect_increased_pct=50,
            enemy_impale_physical_reduction_pct=40,
        )
        self.assertEqual(100, result.physical_hit_before_mitigation_average)
        self.assertEqual(15, result.impale_stored_damage_pct)
        self.assertAlmostEqual(0.45, result.impale_multiplier)
        self.assertEqual(160, result.dps)
        self.assertAlmostEqual(90, result.impale_dps)
        self.assertAlmostEqual(250, result.combined_dps)

    def test_impale_respects_crit_chance_duration_and_reflected_taken(self) -> None:
        result = hit_dps(
            base_damage={"physical": [100, 100]}, action_rate=1,
            crit_chance_pct=50, crit_multiplier_pct=200,
            impale_chance_pct=0, impale_chance_on_crit_pct=100,
            impale_stacks=5, enemy_reflected_damage_taken_increased_pct=20,
            enemy_reflected_damage_taken_more_multiplier=1.5,
        )
        self.assertEqual(50, result.impale_chance_pct)
        self.assertAlmostEqual(67.5, result.impale_dps)
        expired = hit_dps(
            base_damage={"physical": [100, 100]}, action_rate=1,
            impale_chance_pct=100, impale_stacks=5, impale_duration_seconds=0,
        )
        self.assertEqual(0, expired.impale_dps)

    def test_regular_and_critical_cull_are_reported_as_effective_dps(self) -> None:
        regular = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=1, cull_percent=20,
        )
        self.assertEqual(100, regular.dps)
        self.assertEqual(25, regular.culling_dps)
        self.assertEqual(125, regular.effective_dps_with_cull)
        critical = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=2,
            crit_chance_pct=50, crit_multiplier_pct=100, critical_cull_percent=20,
        )
        self.assertAlmostEqual(15, critical.cull_percent)
        self.assertAlmostEqual(200 / 0.85, critical.effective_dps_with_cull)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    def test_poison_steady_state_uses_rate_duration_and_chance(self, _get: object) -> None:
        result = hit_dps(
            base_damage={"chaos": [100, 100]}, action_rate=2,
            damaging_ailments=[{
                "name": "poison", "source_damage": [100, 100],
                "chance_on_hit_pct": 100,
            }],
        )
        poison = result.damaging_ailments[0]
        self.assertEqual(2, poison.application_rate)
        self.assertEqual(4, poison.stack_potential)
        self.assertEqual(4, poison.active_stacks)
        self.assertEqual(30, poison.single_instance_dps)
        self.assertEqual(120, result.ailment_dps)
        self.assertEqual(320, result.total_dps)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    def test_ignite_models_uptime_and_strongest_roll(self, _get: object) -> None:
        sparse = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=0.125,
            damaging_ailments=[{
                "name": "点燃", "source_damage": [100, 200],
                "chance_on_hit_pct": 100,
            }],
        ).damaging_ailments[0]
        self.assertEqual(0.5, sparse.stack_potential)
        self.assertEqual(0.5, sparse.active_stacks)
        self.assertEqual(50, sparse.average_roll_pct)
        self.assertEqual(67.5, sparse.dps)

        sustained = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=1,
            damaging_ailments=[{
                "name": "ignite", "source_damage": [100, 200],
                "chance_on_hit_pct": 100,
            }],
        ).damaging_ailments[0]
        self.assertEqual(4, sustained.stack_potential)
        self.assertEqual(1, sustained.active_stacks)
        self.assertEqual(80, sustained.average_roll_pct)
        self.assertEqual(162, sustained.dps)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    def test_ailment_crit_chance_source_and_dot_multipliers_are_separate(self, _get: object) -> None:
        result = hit_dps(
            base_damage={"chaos": [100, 100]}, action_rate=1,
            crit_chance_pct=50, crit_multiplier_pct=500,
            damaging_ailments=[{
                "name": "poison", "source_damage": [100, 100],
                "crit_source_damage": [200, 200],
                "chance_on_hit_pct": 0, "chance_on_crit_pct": 100,
                "dot_multiplier_pct": 25, "crit_dot_multiplier_pct": 50,
            }],
        )
        poison = result.damaging_ailments[0]
        self.assertEqual(50, poison.effective_chance_pct)
        self.assertEqual(300, poison.average_source_damage)
        self.assertEqual(90, poison.single_instance_dps)
        self.assertEqual(90, poison.dps)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    def test_ailment_mitigation_does_not_use_hit_penetration(self, _get: object) -> None:
        result = hit_dps(
            base_damage={"fire": [100, 100]}, action_rate=1,
            enemy_resistance={"fire": 75}, penetration={"fire": 75},
            damaging_ailments=[{
                "name": "ignite", "source_damage": [100, 100],
                "chance_on_hit_pct": 100,
            }],
        )
        self.assertEqual(100, result.dps)
        self.assertEqual(22.5, result.ailment_dps)
        self.assertEqual(75, result.damaging_ailments[0].mitigation_pct)

    @patch("calc.dps.constants.get", side_effect=lambda key: {
        **AILMENT_CONSTANT_VALUES, constants.DOT_DPS_CAP: 100.0,
    }[key])
    def test_each_ailment_has_an_independent_dot_cap(self, _get: object) -> None:
        result = hit_dps(
            base_damage={"fire": [1, 1]}, action_rate=100,
            damaging_ailments=[
                {"name": "ignite", "source_damage": [1000, 1000], "chance_on_hit_pct": 100},
                {"name": "poison", "source_damage": [1000, 1000], "chance_on_hit_pct": 100},
            ],
        )
        self.assertEqual(200, result.ailment_dps)
        self.assertTrue(all(item.capped for item in result.damaging_ailments))

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    def test_only_poison_stacks_use_quantity_and_bleed_uses_dot_reduction_cap(self, _get: object) -> None:
        result = hit_dps(
            base_damage={"physical": [1, 1]}, action_rate=1,
            quantity_multiplier=3, enemy_physical_reduction_pct=90,
            damaging_ailments=[
                {"name": "bleed", "source_damage": [100, 100], "chance_on_hit_pct": 100},
                {"name": "poison", "source_damage": [100, 100], "chance_on_hit_pct": 100},
            ],
        )
        bleed, poison = result.damaging_ailments
        self.assertEqual(1, bleed.application_rate)
        self.assertEqual(75, bleed.mitigation_pct)
        self.assertEqual(17.5, bleed.dps)
        self.assertEqual(3, poison.application_rate)
        self.assertEqual(6, poison.active_stacks)
        self.assertEqual(180, poison.dps)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    def test_ailment_inputs_fail_closed(self, _get: object) -> None:
        with self.assertRaisesRegex(ValueError, "source_damage"):
            hit_dps(
                base_damage={"fire": [1, 1]}, action_rate=1,
                damaging_ailments=[{"name": "ignite", "chance_on_hit_pct": 100}],
            )
        with self.assertRaisesRegex(ValueError, "不能重复"):
            hit_dps(
                base_damage={"fire": [1, 1]}, action_rate=1,
                damaging_ailments=[
                    {"name": "ignite", "source_damage": [1, 1]},
                    {"name": "点燃", "source_damage": [1, 1]},
                ],
            )

    def test_combat_mechanic_inputs_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "fist_of_war_cooldown_seconds"):
            hit_dps(base_damage={"fire": [1, 1]}, action_rate=1,
                    fist_of_war_more_damage_pct=100)
        with self.assertRaisesRegex(ValueError, "整数"):
            hit_dps(base_damage={"fire": [1, 1]}, action_rate=1,
                    ruthless_blow_every_hits=2.5)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "小于 100"):
            hit_dps(base_damage={"fire": [1, 1]}, action_rate=1, cull_percent=100)

    def test_advanced_probability_and_type_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "-100%..100%"):
            hit_dps(base_damage={"fire": [1, 2]}, action_rate=1,
                    damage_roll_chance_pct=-101)
        with self.assertRaisesRegex(ValueError, "抗性忽略/反转"):
            hit_dps(base_damage={"fire": [1, 2]}, action_rate=1,
                    resistance_ignore_chance_pct={"physical": 10})
        with self.assertRaisesRegex(ValueError, "burst_hit_count"):
            hit_dps(base_damage={"fire": [1, 2]}, action_rate=1, burst_hit_count=0)
        with self.assertRaisesRegex(ValueError, "只支持闪电"):
            hit_dps(base_damage={"chaos": [1, 2]}, action_rate=1,
                    unmodifiable_resistance_types=["chaos"])


class HitDpsAnalysisTest(unittest.TestCase):
    def test_compare_uses_first_scenario_as_baseline(self) -> None:
        result = compare_hit_dps([
            ("基线", {"base_damage": {"fire": [100, 100]}, "action_rate": 1}),
            ("升级", {"base_damage": {"fire": [100, 100]}, "action_rate": 2}),
        ])
        self.assertEqual("基线", result.baseline)
        self.assertEqual("升级", result.best_dps_scenario)
        self.assertEqual(100, result.scenarios[1].dps_delta)
        self.assertEqual(100, result.scenarios[1].dps_delta_pct)

    def test_compare_rejects_duplicate_names_and_bad_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "2..10"):
            compare_hit_dps([("唯一", {"base_damage": {"fire": [1, 1]}, "action_rate": 1})])
        with self.assertRaisesRegex(ValueError, "不能重复"):
            compare_hit_dps([
                ("同名", {"base_damage": {"fire": [1, 1]}, "action_rate": 1}),
                ("同名", {"base_damage": {"fire": [2, 2]}, "action_rate": 1}),
            ])

    def test_target_rate_without_cooldown_and_with_trigger_cap(self) -> None:
        inputs = {"base_damage": {"fire": [100, 100]}}
        plain = target_action_rate(400, inputs)
        self.assertTrue(plain.reachable)
        self.assertEqual(4, plain.required_raw_action_rate)
        triggered = target_action_rate(400, {**inputs, "cooldown_seconds": 0.2, "triggered": True})
        self.assertTrue(triggered.reachable)
        self.assertEqual(4, triggered.required_raw_action_rate)
        blocked = target_action_rate(600, {**inputs, "cooldown_seconds": 0.2, "triggered": True})
        self.assertFalse(blocked.reachable)
        self.assertEqual(5, blocked.cooldown_cap)

    def test_target_rate_solves_non_triggered_action_plus_cooldown(self) -> None:
        result = target_action_rate(400, {
            "base_damage": {"fire": [100, 100]}, "cooldown_seconds": 0.2,
        })
        self.assertTrue(result.reachable)
        self.assertAlmostEqual(20, result.required_raw_action_rate or 0)
        self.assertAlmostEqual(400, result.achieved_dps or 0)
        blocked = target_action_rate(500, {
            "base_damage": {"fire": [100, 100]}, "cooldown_seconds": 0.2,
        })
        self.assertFalse(blocked.reachable)

    def test_compare_and_target_support_impale_and_cull_metrics(self) -> None:
        compared = compare_hit_dps([
            ("纯命中", {"base_damage": {"physical": [100, 100]}, "action_rate": 1}),
            ("穿刺", {
                "base_damage": {"physical": [100, 100]}, "action_rate": 1,
                "impale_chance_pct": 100, "impale_stacks": 5,
            }),
            ("终结", {
                "base_damage": {"physical": [100, 100]}, "action_rate": 1,
                "cull_percent": 20,
            }),
        ])
        self.assertEqual("穿刺", compared.best_combined_dps_scenario)
        self.assertEqual("穿刺", compared.best_effective_dps_scenario)
        target = target_action_rate(150, {
            "base_damage": {"physical": [100, 100]},
            "impale_chance_pct": 100, "impale_stacks": 5,
        }, metric="combined_dps")
        self.assertTrue(target.reachable)
        self.assertAlmostEqual(1, target.required_raw_action_rate or 0)
        self.assertEqual("combined_dps", target.metric)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    def test_compare_and_target_support_ailment_and_total_metrics(self, _get: object) -> None:
        ailment = {
            "name": "poison", "source_damage": [100, 100],
            "chance_on_hit_pct": 100,
        }
        compared = compare_hit_dps([
            ("慢速", {"base_damage": {"chaos": [100, 100]}, "action_rate": 1,
                     "damaging_ailments": [ailment]}),
            ("快速", {"base_damage": {"chaos": [100, 100]}, "action_rate": 2,
                     "damaging_ailments": [ailment]}),
        ])
        self.assertEqual("快速", compared.best_ailment_dps_scenario)
        self.assertEqual("快速", compared.best_total_dps_scenario)
        target = target_action_rate(120, {
            "base_damage": {"chaos": [100, 100]},
            "damaging_ailments": [ailment],
        }, metric="ailment_dps")
        self.assertTrue(target.reachable)
        self.assertAlmostEqual(2, target.required_raw_action_rate or 0)

    @patch("calc.dps.constants.get", side_effect=lambda key: {
        **AILMENT_CONSTANT_VALUES, constants.DOT_DPS_CAP: 100.0,
    }[key])
    def test_target_rate_reports_independent_dot_cap(self, _get: object) -> None:
        result = target_action_rate(101, {
            "base_damage": {"fire": [1, 1]},
            "damaging_ailments": [{
                "name": "ignite", "source_damage": [1000, 1000],
                "chance_on_hit_pct": 100,
            }],
        }, metric="ailment_dps")
        self.assertFalse(result.reachable)
        self.assertEqual(("dot_cap",), result.cap_sources)
        self.assertEqual(100, result.maximum_achievable_dps)
        self.assertIn("DoT 上限", result.reason)

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    def test_target_rate_rejects_non_monotonic_ailment_inputs(self, _get: object) -> None:
        with self.assertRaisesRegex(ValueError, "可能非单调"):
            target_action_rate(100, {
                "base_damage": {"fire": [100, 100]},
                "fist_of_war_more_damage_pct": 100,
                "fist_of_war_cooldown_seconds": 1,
                "damaging_ailments": [{
                    "name": "ignite", "source_damage": [100, 100],
                    "chance_on_hit_pct": 100,
                }],
            }, metric="ailment_dps")

    def test_target_rate_numerically_solves_rate_dependent_critical_cull(self) -> None:
        result = target_action_rate(220, {
            "base_damage": {"fire": [100, 100]},
            "crit_chance_pct": 50, "crit_multiplier_pct": 100,
            "critical_cull_percent": 20,
        }, metric="effective_dps_with_cull")
        self.assertTrue(result.reachable)
        self.assertAlmostEqual(220, result.achieved_dps or 0, places=6)

    def test_target_rate_reports_server_tick_cap(self) -> None:
        result = target_action_rate(4_000, {
            "base_damage": {"fire": [100, 100]},
            "server_tick_seconds": 0.033,
        })
        self.assertFalse(result.reachable)
        self.assertEqual(("server_tick",), result.cap_sources)
        self.assertAlmostEqual(1 / 0.033, result.action_rate_cap or 0)
        self.assertAlmostEqual(100 / 0.033, result.maximum_achievable_dps or 0)


class HitDpsSurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.constant = patch("tools.impl.dps.constants.get", return_value=150.0)
        self.provenance = patch(
            "tools.impl.dps.constants.provenance",
            return_value=SimpleNamespace(
                url="https://github.com/x/PathOfBuilding/blob/" + "c" * 40 + "/src/Data/Misc.lua#L1",
                source="Path of Building", source_priority=2,
            ),
        )
        self.constant.start()
        self.provenance.start()

    def tearDown(self) -> None:
        self.provenance.stop()
        self.constant.stop()

    def test_tool_reports_missing_inputs_without_guessing(self) -> None:
        with patch("tools.impl.dps.constants.get", side_effect=AssertionError("不应读常量")):
            result = hit_dps_calc()
        self.assertTrue(result.ok)
        self.assertEqual("missing_inputs", result.data["kind"])
        self.assertIn("不能", result.data["reason"])

    def test_tool_returns_deterministic_answer_and_citation(self) -> None:
        result = hit_dps_calc(base_damage={"火焰": [100, 200]}, action_rate=2)
        self.assertTrue(result.ok)
        self.assertEqual(300, result.data["dps"])
        self.assertIn("命中 DPS：300", result.data["answer"])
        self.assertIn("CalcOffence.lua#L3615-L3628", result.citations[0]["url"])
        self.assertIn("不含护甲对穿刺的动态减伤", result.data["boundary"])

    def test_advanced_tool_fields_and_output(self) -> None:
        result = hit_dps_calc(
            base_damage={"火焰": [100, 200]}, action_rate=10,
            damage_roll_chance_pct=100, double_damage_chance_pct=50,
            cooldown_seconds=0.2, triggered=True, burst_hit_count=3,
            repeat_damage_multiplier=0.5,
        )
        self.assertTrue(result.ok)
        self.assertEqual(5, result.data["action_rate"])
        self.assertEqual(1.5, result.data["scaled_damage_effect"])
        self.assertGreater(result.data["average_burst_damage"], 0)
        self.assertIn("一轮爆发", result.data["answer"])

    def test_compare_and_target_tools(self) -> None:
        comparison = hit_dps_compare(scenarios=[
            {"name": "A", "inputs": {"base_damage": {"fire": [100, 100]}, "action_rate": 1}},
            {"name": "B", "inputs": {"base_damage": {"fire": [100, 100]}, "action_rate": 2}},
        ])
        self.assertTrue(comparison.ok)
        self.assertEqual("B", comparison.data["best_dps_scenario"])
        target = hit_dps_target_rate(
            target_dps=400,
            inputs={"base_damage": {"fire": [100, 100]}, "cooldown_seconds": 0.2},
        )
        self.assertTrue(target.ok)
        self.assertTrue(target.data["reachable"])
        self.assertAlmostEqual(20, target.data["required_raw_action_rate"])

    def test_compare_renders_zero_baseline_delta_as_undefined(self) -> None:
        comparison = hit_dps_compare(scenarios=[
            {"name": "零命中", "inputs": {
                "base_damage": {"fire": [100, 100]}, "action_rate": 1,
                "hit_chance_pct": 0,
            }},
            {"name": "可命中", "inputs": {
                "base_damage": {"fire": [100, 100]}, "action_rate": 1,
            }},
        ])
        self.assertTrue(comparison.ok)
        self.assertIsNone(comparison.data["scenarios"][1]["dps_delta_pct"])
        self.assertIn("| 可命中 | 100 | 100 | 0 | 100 | 100 | 不可定义 |", comparison.data["answer"])

    def test_http_endpoint_reuses_same_calculator(self) -> None:
        request = HitDpsRequest(base_damage={"fire": (50, 150)}, action_rate=3)
        response = calculate_hit_dps(request, _caller="test")
        self.assertEqual(300, response["dps"])
        self.assertEqual("result", response["kind"])

    def test_http_endpoint_exposes_combat_impale_and_cull_fields(self) -> None:
        request = HitDpsRequest(
            base_damage={"physical": (100, 100)}, action_rate=100,
            server_tick_seconds=0.033, ruthless_blow_every_hits=3,
            ruthless_blow_hit_multiplier=2, impale_chance_pct=100,
            impale_stacks=5, cull_percent=20,
        )
        response = calculate_hit_dps(request, _caller="test")
        self.assertTrue(response["action_rate_tick_capped"])
        self.assertGreater(response["impale_dps"], 0)
        self.assertGreater(response["effective_dps_with_cull"], response["combined_dps"])

    @patch("calc.dps.constants.get", side_effect=_ailment_constant)
    def test_tool_and_http_expose_steady_state_ailments(self, _get: object) -> None:
        tool_result = hit_dps_calc(
            base_damage={"chaos": [100, 100]}, action_rate=2,
            damaging_ailments=[{
                "name": "poison", "source_damage": [100, 100],
                "chance_on_hit_pct": 100,
            }],
        )
        self.assertTrue(tool_result.ok)
        self.assertEqual(120, tool_result.data["ailment_dps"])
        self.assertIn("伤害性异常合计：120", tool_result.data["answer"])
        response = calculate_hit_dps(HitDpsRequest(
            base_damage={"chaos": (100, 100)}, action_rate=2,
            damaging_ailments=[DamagingAilmentRequest(
                name="poison", source_damage=(100, 100), chance_on_hit_pct=100,
            )],
        ), _caller="test")
        self.assertEqual(320, response["total_dps"])
        self.assertEqual("中毒", response["damaging_ailments"][0]["name"])

    def test_compare_and_target_http_endpoints(self) -> None:
        comparison = compare_hit_dps_scenarios(HitDpsCompareRequest(scenarios=[
            HitDpsScenarioRequest(name="A", inputs=HitDpsRequest(
                base_damage={"fire": (100, 100)}, action_rate=1,
            )),
            HitDpsScenarioRequest(name="B", inputs=HitDpsRequest(
                base_damage={"fire": (100, 100)}, action_rate=2,
            )),
        ]), _caller="test")
        self.assertEqual("B", comparison["best_dps_scenario"])
        target = calculate_hit_dps_target_rate(HitDpsTargetRateRequest(
            target_dps=400,
            inputs=HitDpsTargetRateInputs(
                base_damage={"fire": (100, 100)}, cooldown_seconds=0.2,
            ),
        ), _caller="test")
        self.assertTrue(target["reachable"])
        combined = calculate_hit_dps_target_rate(HitDpsTargetRateRequest(
            target_dps=150,
            metric="combined_dps",
            inputs=HitDpsTargetRateInputs(
                base_damage={"physical": (100, 100)}, impale_chance_pct=100,
                impale_stacks=5,
            ),
        ), _caller="test")
        self.assertEqual("combined_dps", combined["metric"])
        self.assertAlmostEqual(1, combined["required_raw_action_rate"])

    def test_registered_for_agent_and_http(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS
        from main import create_app
        from tools.registry import REGISTRY

        self.assertIn("hit_dps_calc", ALLOWED_TOOLS)
        self.assertIsNotNone(REGISTRY.spec("hit_dps_calc"))
        self.assertIsNotNone(REGISTRY.spec("hit_dps_compare"))
        self.assertIsNotNone(REGISTRY.spec("hit_dps_target_rate"))
        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/calc/hit-dps", paths)
        self.assertIn("/poe_agent/calc/hit-dps/compare", paths)
        self.assertIn("/poe_agent/calc/hit-dps/target-rate", paths)

    def test_natural_language_route_and_conservative_argument_extraction(self) -> None:
        from orchestrator.loop import _hit_dps_args, _preferred_tool_route

        question = (
            "100-200 火焰伤害，伤害提高 100%，20% 更多伤害，命中率 90%，"
            "暴击率 50%，暴击加成 300%，敌人火抗 50%，火焰穿透 20%，"
            "每秒攻击 2 次，命中 DPS 怎么算"
        )
        self.assertEqual("hit_dps_calc", _preferred_tool_route(question))
        args = _hit_dps_args(question)
        self.assertEqual({"fire": [100.0, 200.0]}, args["base_damage"])
        self.assertEqual(2.0, args["action_rate"])
        self.assertEqual([1.2], args["more_damage_multipliers"])
        self.assertEqual({"fire": 50.0}, args["enemy_resistance"])
        self.assertEqual({"fire": 20.0}, args["penetration"])

    def test_natural_language_extracts_advanced_expected_damage_inputs(self) -> None:
        from orchestrator.loop import _hit_dps_args

        args = _hit_dps_args(
            "100-200 火焰伤害，伤害幸运，30% 几率造成双倍伤害，"
            "敌人受到的伤害提高 20%，冷却 0.2 秒，触发，每秒 10 次，命中 DPS 怎么算"
        )
        self.assertEqual(100, args["damage_roll_chance_pct"])
        self.assertEqual(30, args["double_damage_chance_pct"])
        self.assertEqual(20, args["enemy_damage_taken_increased_pct"])
        self.assertEqual(0.2, args["cooldown_seconds"])
        self.assertTrue(args["triggered"])

    def test_natural_language_extracts_combat_mechanics(self) -> None:
        from orchestrator.loop import _hit_dps_args

        args = _hit_dps_args(
            "100-100 物理伤害，每秒攻击 2 次，每3次攻击有一次无情一击，"
            "无情一击造成200%伤害，战争之拳100%更多伤害，战争之拳冷却1秒，"
            "战吼增助50%更多伤害，战吼增助覆盖率40%，穿刺几率100%，"
            "5层穿刺，穿刺效果提高50%，终结20%，服务器 tick 0.033秒"
        )
        self.assertEqual(3, args["ruthless_blow_every_hits"])
        self.assertEqual(2, args["ruthless_blow_hit_multiplier"])
        self.assertEqual(100, args["fist_of_war_more_damage_pct"])
        self.assertEqual(40, args["exerted_attack_uptime_pct"])
        self.assertEqual(100, args["impale_chance_pct"])
        self.assertEqual(5, args["impale_stacks"])
        self.assertEqual(20, args["cull_percent"])
        self.assertEqual(0.033, args["server_tick_seconds"])
        self.assertNotIn("cooldown_seconds", args)

    def test_natural_language_extracts_only_explicit_ailment_source_damage(self) -> None:
        from orchestrator.loop import _hit_dps_args

        args = _hit_dps_args(
            "100-100 火焰伤害，每秒攻击2次，点燃源伤害100-200，"
            "点燃几率100%，点燃持续伤害加成50%，点燃持续时间提高25%，命中和点燃 DPS 怎么算"
        )
        self.assertEqual([{
            "name": "ignite", "source_damage": [100.0, 200.0],
            "chance_on_hit_pct": 100.0, "dot_multiplier_pct": 50.0,
            "duration_increased_pct": 25.0,
        }], args["damaging_ailments"])
        vague = _hit_dps_args("100-100 火焰伤害，每秒攻击2次，点燃几率100%，总 DPS 怎么算")
        self.assertNotIn("damaging_ailments", vague)

    def test_vague_build_dps_question_is_not_silently_guessed(self) -> None:
        from orchestrator.loop import _hit_dps_args

        self.assertEqual({}, _hit_dps_args("我的命中 DPS 怎么算"))

    def test_comparison_and_target_rate_intents_select_dedicated_tools(self) -> None:
        from orchestrator.loop import _preferred_tool_route

        self.assertEqual("hit_dps_compare", _preferred_tool_route("把这两个 DPS 方案对比一下"))
        self.assertEqual(
            "hit_dps_target_rate",
            _preferred_tool_route("目标 DPS 需要每秒攻击多少次"),
        )


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (POB_ROOT_ENV / "src/Modules/CalcOffence.lua").read_text(encoding="utf-8")

    def test_conversion_order_and_cap_logic_remain_pinned(self) -> None:
        self.assertIn('local dmgTypeList = {"Physical", "Lightning", "Cold", "Fire", "Chaos"}', self.source)
        block = "\n".join(self.source.splitlines()[1878:1922])
        self.assertIn("if skillTotal > 100 then", block)
        self.assertIn("elseif globalTotal + skillTotal > 100 then", block)

    def test_final_dps_formula_remains_pinned(self) -> None:
        block = "\n".join(self.source.splitlines()[3614:3628])
        self.assertIn("output.AverageDamage = output.AverageHit * output.HitChance / 100", block)
        self.assertIn("output.TotalDPS = output.AverageDamage", block)

    def test_lucky_double_triple_resistance_and_cooldown_stay_pinned(self) -> None:
        scaled = "\n".join(self.source.splitlines()[3100:3127])
        self.assertIn("Triple Damage overrides Double Damage", scaled)
        self.assertIn("1 + output.DoubleDamageEffect + output.TripleDamageEffect", scaled)
        rolls = "\n".join(self.source.splitlines()[3297:3313])
        self.assertIn("damageTypeHitAvgLucky", rolls)
        resistance = "\n".join(self.source.splitlines()[3338:3415])
        self.assertIn("PartialIgnoreEnemyPhysicalDamageReduction", resistance)
        self.assertIn("HitsInvertEleResChance", resistance)
        rate = "\n".join(self.source.splitlines()[2403:2418])
        self.assertIn("m_max(output.HitTime, output.Cooldown)", rate)
        self.assertIn("output.HitTime + output.Cooldown", rate)

    def test_combat_mechanics_cull_and_impale_stay_pinned(self) -> None:
        tick = "\n".join(self.source.splitlines()[273:285])
        self.assertIn("m_ceil(cooldown * data.misc.ServerTickRate)", tick)
        combat = "\n".join(self.source.splitlines()[2804:2861])
        self.assertIn("output.RuthlessBlowHitEffect", combat)
        self.assertIn("globalOutput.AvgFistOfWarDamageEffect", combat)
        cull = "\n".join(self.source.splitlines()[3128:3137])
        self.assertIn("globalOutput.CullMultiplier", cull)
        impale = "\n".join(self.source.splitlines()[5387:5437])
        self.assertIn("local impaleDMGModifier", impale)
        self.assertIn("output.ImpaleStoredDamage", impale)

    def test_damaging_ailment_steady_state_stays_pinned(self) -> None:
        lines = self.source.splitlines()
        chance = "\n".join(lines[dps_module.AILMENT_CHANCE_LINES[0] - 1:dps_module.AILMENT_CHANCE_LINES[1]])
        source = "\n".join(lines[dps_module.AILMENT_DAMAGE_LINES[0] - 1:dps_module.AILMENT_DAMAGE_LINES[1]])
        bleed = "\n".join(lines[dps_module.BLEED_DPS_LINES[0] - 1:dps_module.BLEED_DPS_LINES[1]])
        poison = "\n".join(lines[dps_module.POISON_DPS_LINES[0] - 1:dps_module.POISON_DPS_LINES[1]])
        ignite = "\n".join(lines[dps_module.IGNITE_DPS_LINES[0] - 1:dps_module.IGNITE_DPS_LINES[1]])
        self.assertIn("BleedChanceOnCrit", chance)
        self.assertIn("local function calcAilmentDamage", source)
        self.assertIn("bleedStacks = bleedStacks * globalOutput.BleedDuration", bleed)
        self.assertIn("activeBleeds = m_min(bleedStacks, maxStacks)", bleed)
        self.assertIn("PoisonStacks = PoisonStacks * globalOutput.PoisonDuration", poison)
        self.assertIn("PoisonDPSCapped = m_min(PoisonDPSUncapped, data.misc.DotDpsCap)", poison)
        self.assertIn("igniteStacks = igniteStacks * globalOutput.IgniteDuration", ignite)
        self.assertIn("activeIgnites = m_min(igniteStacks, maxStacks)", ignite)


if __name__ == "__main__":
    unittest.main()
