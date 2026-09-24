"""单技能命中 DPS 主链：公式、转化、边界、工具与 API。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.calculator import HitDpsRequest, calculate_hit_dps  # noqa: E402
from calc.dps import Conversion, hit_dps  # noqa: E402
from tools.impl.dps import hit_dps_calc  # noqa: E402
from tests.unit.test_resistance import POB_ROOT_ENV  # noqa: E402


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
        self.assertIn("不含幸运伤害", result.data["boundary"])

    def test_http_endpoint_reuses_same_calculator(self) -> None:
        request = HitDpsRequest(base_damage={"fire": (50, 150)}, action_rate=3)
        response = calculate_hit_dps(request, _caller="test")
        self.assertEqual(300, response["dps"])
        self.assertEqual("result", response["kind"])

    def test_registered_for_agent_and_http(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS
        from main import create_app
        from tools.registry import REGISTRY

        self.assertIn("hit_dps_calc", ALLOWED_TOOLS)
        self.assertIsNotNone(REGISTRY.spec("hit_dps_calc"))
        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/calc/hit-dps", paths)

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

    def test_vague_build_dps_question_is_not_silently_guessed(self) -> None:
        from orchestrator.loop import _hit_dps_args

        self.assertEqual({}, _hit_dps_args("我的命中 DPS 怎么算"))


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


if __name__ == "__main__":
    unittest.main()
