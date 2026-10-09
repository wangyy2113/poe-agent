"""Boss 概率防御、生存分布、比较、HTTP 与 Build 契约。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api.boss import (
    BossProbabilisticCompareRequest,
    BossProbabilisticEvent,
    BossProbabilisticScenarioRequest,
    BossProbabilisticSequenceRequest,
    compare_probabilistic_sequences as compare_probability_api,
    probabilistic_sequence,
)
from api.build import (
    BuildBossProbabilisticSequenceOut,
    BuildRequest,
)
from calc import boss_probability, constants
from calc.boss_probability import DefenceProfile, ProbabilisticEvent
from calc.boss_timeline import TimedDamageEvent
from core import db as dbmod
from orchestrator.loop import _preferred_tool_route
from parsers.pob_build import PobBuild
from tests.unit.test_boss_survival import _conn
from tools.impl.boss_probability import (
    boss_probabilistic_sequence_check,
    boss_probabilistic_sequence_compare,
    boss_probabilistic_sequence_from_build,
)
from tools.registry import REGISTRY


def _event(
    at: float, low: float = 60, high: float | None = None,
    hit_type: str = "attack", name: str = "命中",
) -> ProbabilisticEvent:
    upper = low if high is None else high
    return ProbabilisticEvent(
        hit_type=hit_type,  # type: ignore[arg-type]
        damage=TimedDamageEvent(
            at_seconds=at, skill=name, damage_type="Physical",
            raw_low=low, raw_high=upper, taken_low=low, taken_high=upper,
        ),
    )


class BossProbabilityFormulaTest(unittest.TestCase):
    def test_attack_miss_and_block_are_mutually_exclusive_branches(self) -> None:
        outcomes = boss_probability.damage_outcomes(
            "attack", DefenceProfile(
                attack_hit_chance_pct=80, attack_block_chance_pct=25,
            ),
        )
        by_multiplier = {row.damage_multiplier: row.probability_pct for row in outcomes}
        self.assertAlmostEqual(40, by_multiplier[0])
        self.assertAlmostEqual(60, by_multiplier[1])
        self.assertAlmostEqual(100, sum(by_multiplier.values()))

    def test_spell_block_precedes_suppression_without_double_dipping(self) -> None:
        outcomes = boss_probability.damage_outcomes(
            "spell", DefenceProfile(
                spell_block_chance_pct=20,
                spell_suppression_chance_pct=50,
                suppressed_damage_taken_pct=60,
            ),
        )
        by_multiplier = {row.damage_multiplier: row.probability_pct for row in outcomes}
        self.assertAlmostEqual(20, by_multiplier[0])
        self.assertAlmostEqual(40, by_multiplier[0.6])
        self.assertAlmostEqual(40, by_multiplier[1])

    def test_two_attacks_have_exact_binomial_survival_distribution(self) -> None:
        result = boss_probability.assess_probabilistic_sequence(
            events=[_event(0), _event(1)], pool=100,
            profile=DefenceProfile(attack_hit_chance_pct=50), bucket_size=1,
        )
        self.assertEqual(75, result.high_roll.survival_probability_pct)
        self.assertEqual(25, result.high_roll.depletion_probability_pct)
        self.assertEqual(45, result.high_roll.expected_remaining_pool)
        self.assertEqual(60, result.high_roll.expected_remaining_pool_if_alive)
        self.assertEqual(2, result.high_roll.most_likely_depletion_hit)
        self.assertEqual(25, result.high_roll.steps[1].depletion_probability_on_hit_pct)
        bins = {
            row.remaining_pool: row.probability_pct
            for row in result.high_roll.final_distribution
        }
        self.assertEqual({0.0: 25, 40.0: 50, 100.0: 25}, bins)
        self.assertFalse(result.deterministic_worst_case_survives)

    def test_low_and_high_damage_rolls_are_separate_not_assumed_probabilities(self) -> None:
        result = boss_probability.assess_probabilistic_sequence(
            events=[_event(0, 30, 70), _event(1, 30, 70)], pool=100,
            profile=DefenceProfile(attack_hit_chance_pct=100), bucket_size=1,
        )
        self.assertEqual(100, result.low_roll.survival_probability_pct)
        self.assertEqual(0, result.high_roll.survival_probability_pct)
        self.assertEqual("low_roll", result.low_roll.damage_basis)
        self.assertEqual("high_roll", result.high_roll.damage_basis)
        self.assertIn("不假设伤害区间", result.probability_model)

    def test_full_suppression_applies_partial_damage_not_avoidance(self) -> None:
        result = boss_probability.assess_probabilistic_sequence(
            events=[_event(0, 80, hit_type="spell"),
                    _event(1, 80, hit_type="spell")],
            pool=100,
            profile=DefenceProfile(
                spell_suppression_chance_pct=100,
                suppressed_damage_taken_pct=50,
            ),
            bucket_size=1,
        )
        self.assertEqual(100, result.high_roll.survival_probability_pct)
        self.assertEqual(20, result.high_roll.expected_remaining_pool)
        self.assertEqual(100, result.high_roll.steps[0].partial_damage_probability_pct)
        self.assertEqual(0, result.high_roll.steps[0].avoid_probability_pct)

    def test_recovery_uses_existing_conservative_scheduled_event_windows(self) -> None:
        result = boss_probability.assess_probabilistic_sequence(
            events=[_event(0), _event(2)], pool=100,
            profile=DefenceProfile(), recovery_per_second=20,
            recovery_delay_seconds=1, bucket_size=1,
        )
        self.assertEqual(1, result.high_roll.steps[1].recovery_eligible_seconds)
        self.assertEqual(0, result.high_roll.survival_probability_pct)
        self.assertEqual(0, result.deterministic_worst_case_ending_pool)

    def test_quantization_contract_and_bucket_limit_are_explicit(self) -> None:
        result = boss_probability.assess_probabilistic_sequence(
            events=[_event(0, 33), _event(1, 33), _event(2, 33)],
            pool=100, profile=DefenceProfile(attack_hit_chance_pct=50),
            bucket_size=2,
        )
        self.assertEqual(50, result.bucket_count)
        self.assertEqual(3, result.quantization_error_bound)
        with self.assertRaisesRegex(boss_probability.BossProbabilityError, "上限"):
            boss_probability.assess_probabilistic_sequence(
                events=[_event(0)], pool=100,
                profile=DefenceProfile(), bucket_size=0.001,
            )

    def test_exact_zero_is_depleted_and_remains_in_dead_mass(self) -> None:
        result = boss_probability.assess_probabilistic_sequence(
            events=[_event(0, 100), _event(10, 1)], pool=100,
            profile=DefenceProfile(), recovery_per_second=100, bucket_size=1,
        )
        self.assertEqual(0, result.high_roll.survival_probability_pct)
        self.assertEqual(100, result.high_roll.depletion_probability_pct)
        self.assertEqual(1, result.high_roll.most_likely_depletion_hit)
        self.assertEqual(0, result.high_roll.expected_applied_damage_before_depletion - 100)

    def test_profile_and_event_validation_reject_unsafe_values(self) -> None:
        with self.assertRaisesRegex(boss_probability.BossProbabilityError, "不能超过 100"):
            boss_probability.assess_probabilistic_sequence(
                events=[_event(0)], pool=100,
                profile=DefenceProfile(attack_block_chance_pct=101),
            )
        with self.assertRaisesRegex(boss_probability.BossProbabilityError, "attack 或 spell"):
            boss_probability.assess_probabilistic_sequence(
                events=[_event(0, hit_type="slam")], pool=100,
            )
        with self.assertRaisesRegex(boss_probability.BossProbabilityError, "非递减"):
            boss_probability.assess_probabilistic_sequence(
                events=[_event(2), _event(1)], pool=100,
            )

    def test_comparison_ranks_probability_tail_expected_and_guarantee_separately(self) -> None:
        risky = boss_probability.assess_probabilistic_sequence(
            events=[_event(0), _event(1)], pool=100,
            profile=DefenceProfile(attack_hit_chance_pct=50), bucket_size=1,
        )
        safe = boss_probability.assess_probabilistic_sequence(
            events=[_event(0), _event(1)], pool=130,
            profile=DefenceProfile(attack_hit_chance_pct=100), bucket_size=1,
        )
        compared = boss_probability.compare_probabilistic_sequences([
            ("概率规避", risky), ("池子兜底", safe),
        ])
        self.assertEqual("池子兜底", compared.best_high_roll_survival_scenario)
        self.assertEqual("池子兜底", compared.best_high_roll_p05_margin_scenario)
        self.assertEqual(("池子兜底",), compared.deterministic_survivors)


class BossProbabilitySurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patcher = patch.object(dbmod, "get_conn", return_value=self.conn)
        self.patcher.start()
        constants.reset_cache()

    def tearDown(self) -> None:
        self.patcher.stop()
        self.conn.close()
        constants.reset_cache()

    @staticmethod
    def _inputs(*, block: float = 50) -> dict[str, object]:
        return {
            "events": [
                {"skill": "Test Slam", "at_seconds": 0, "hit_type": "attack"},
                {"skill": "Test Slam", "at_seconds": 2, "hit_type": "attack"},
            ],
            "level": 84, "pool": 15000, "armour": 0,
            "recovery_per_second": 2000,
            "attack_hit_chance_pct": 100,
            "attack_block_chance_pct": block,
            "bucket_size": 10,
        }

    def test_tool_reuses_damage_chain_and_returns_probability_tail(self) -> None:
        result = boss_probabilistic_sequence_check(**self._inputs())
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(2, result.data["hit_count"])
        self.assertEqual("attack", result.data["requested_events"][0]["hit_type"])
        self.assertGreater(result.data["high_roll"]["survival_probability_pct"], 0)
        self.assertIn("remaining_pool_p05", result.data["high_roll"])
        self.assertIn("全为高伤", result.data["answer"])
        self.assertIn("概率不是保证", result.data["boundary"])
        self.assertEqual(2, len(result.citations))

    def test_tool_preserves_unknown_from_boss_damage_chain(self) -> None:
        result = boss_probabilistic_sequence_check(
            events=[{
                "skill": "Shaper Ball", "at_seconds": 0, "hit_type": "spell",
            }],
            level=84, pool=10000, spell_suppression_chance_pct=100,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertEqual(["冰霜抗性"], result.data["missing_inputs"])
        self.assertNotIn("high_roll", result.data)

    def test_tool_rejects_missing_hit_type_unknown_fields_and_reordered_time(self) -> None:
        missing = boss_probabilistic_sequence_check(
            events=[{"skill": "Test Slam", "at_seconds": 0}],
            level=84, pool=10000,
        )
        self.assertFalse(missing.ok)
        unknown = boss_probabilistic_sequence_check(
            events=[{
                "skill": "Test Slam", "at_seconds": 0,
                "hit_type": "attack", "count": 2,
            }],
            level=84, pool=10000,
        )
        self.assertFalse(unknown.ok)
        reordered = boss_probabilistic_sequence_check(
            events=[
                {"skill": "Test Slam", "at_seconds": 2, "hit_type": "attack"},
                {"skill": "Test Slam", "at_seconds": 1, "hit_type": "attack"},
            ],
            level=84, pool=10000,
        )
        self.assertFalse(reordered.ok)
        self.assertIn("非递减", reordered.error)

    def test_compare_tool_keeps_deterministic_and_probability_rankings(self) -> None:
        result = boss_probabilistic_sequence_compare(scenarios=[
            {"name": "无格挡", "inputs": self._inputs(block=0)},
            {"name": "半格挡", "inputs": self._inputs(block=50)},
        ])
        self.assertTrue(result.ok)
        self.assertEqual("半格挡", result.data["best_high_roll_survival_scenario"])
        self.assertEqual(2, len(result.data["scenarios"]))
        self.assertIn("概率防御方案比较", result.data["answer"])

    def test_http_endpoints_and_openapi_contract(self) -> None:
        request = BossProbabilisticSequenceRequest(
            events=[
                BossProbabilisticEvent(
                    skill="Test Slam", at_seconds=0, hit_type="attack",
                ),
                BossProbabilisticEvent(
                    skill="Test Slam", at_seconds=2, hit_type="attack",
                ),
            ],
            level=84, pool=15000, attack_block_chance_pct=50,
            bucket_size=10,
        )
        response = probabilistic_sequence(request, _caller="test")
        self.assertEqual(2, response.hit_count)
        self.assertIsNotNone(response.high_roll)
        compared = compare_probability_api(BossProbabilisticCompareRequest(scenarios=[
            BossProbabilisticScenarioRequest(name="A", inputs=request),
            BossProbabilisticScenarioRequest(name="B", inputs=request.model_copy(
                update={"attack_block_chance_pct": 0},
            )),
        ]), _caller="test")
        self.assertEqual(2, len(compared["scenarios"]))
        from main import create_app

        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/boss/probabilistic-sequence", paths)
        self.assertIn("/poe_agent/boss/probabilistic-sequence/compare", paths)

    def test_registry_knowledge_and_natural_language_routes_are_wired(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS

        for name in (
            "boss_probabilistic_sequence_check",
            "boss_probabilistic_sequence_compare",
        ):
            self.assertIn(name, ALLOWED_TOOLS)
            self.assertIsNotNone(REGISTRY.spec(name))
        self.assertEqual(
            "boss_probabilistic_sequence_check",
            _preferred_tool_route("这组 Boss 命中的生存概率和 P95 风险是多少"),
        )
        self.assertEqual(
            "boss_probabilistic_sequence_compare",
            _preferred_tool_route("两套格挡压制方案对比"),
        )

    @staticmethod
    def _build_stats() -> dict[str, float]:
        return {
            "LifeUnreserved": 15000,
            "Armour": 0,
            "NetLifeRegen": 2000,
            "EvadeChance": 25,
            "EffectiveBlockChance": 40,
            "EffectiveSpellBlockChance": 30,
            "EffectiveSpellSuppressionChance": 80,
        }

    def test_build_reads_attack_probability_fields_and_discloses_conversion(self) -> None:
        result = boss_probabilistic_sequence_from_build(
            PobBuild(player_stats=self._build_stats()),
            events=self._inputs()["events"], level=84, pool_mode="life",
            bucket_size=10,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(75, result.data["profile"]["attack_hit_chance_pct"])
        self.assertEqual(40, result.data["profile"]["attack_block_chance_pct"])
        self.assertEqual(
            "pob_player_stat:EvadeChance:100-minus",
            result.data["source_config"]["attack_hit_chance_pct"],
        )
        self.assertEqual(25, result.data["source_fields"]["EvadeChance"])
        response = BuildBossProbabilisticSequenceOut(
            **result.data, citations=result.citations,
        )
        self.assertEqual("build_probabilistic_timed_sequence", response.calculation_mode)

    def test_build_reads_spell_block_and_suppression_without_attack_fields(self) -> None:
        stats = self._build_stats()
        stats.pop("EvadeChance")
        stats.pop("EffectiveBlockChance")
        result = boss_probabilistic_sequence_from_build(
            PobBuild(player_stats=stats),
            events=[{
                "skill": "Test Slam", "at_seconds": 0, "hit_type": "spell",
            }],
            level=84, pool_mode="life", bucket_size=10,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(30, result.data["profile"]["spell_block_chance_pct"])
        self.assertEqual(80, result.data["profile"]["spell_suppression_chance_pct"])
        self.assertEqual(
            "not_applicable", result.data["source_config"]["attack_hit_chance_pct"]
        )

    def test_build_missing_relevant_probability_field_stays_unknown(self) -> None:
        stats = self._build_stats()
        stats.pop("EffectiveBlockChance")
        result = boss_probabilistic_sequence_from_build(
            PobBuild(player_stats=stats), events=self._inputs()["events"],
            level=84, pool_mode="life", bucket_size=10,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertIn("attack_block_chance_pct", result.data["missing_inputs"][0])
        self.assertNotIn("high_roll", result.data)

    def test_build_explicit_probability_overrides_are_supported(self) -> None:
        stats = self._build_stats()
        stats.pop("EvadeChance")
        stats.pop("EffectiveBlockChance")
        result = boss_probabilistic_sequence_from_build(
            PobBuild(player_stats=stats), events=self._inputs()["events"],
            level=84, pool_mode="life", attack_hit_chance_pct=90,
            attack_block_chance_pct=20, bucket_size=10,
        )
        self.assertTrue(result.data["assessable"])
        self.assertEqual(
            "explicit_request",
            result.data["source_config"]["attack_hit_chance_pct"],
        )

    def test_build_request_and_response_expose_probability_contract(self) -> None:
        request = BuildRequest(code="abc", boss_probabilistic_sequence={
            "events": [{
                "skill": "Test Slam", "at_seconds": 0, "hit_type": "attack",
            }],
            "level": 84, "pool_mode": "life", "bucket_size": 10,
        })
        self.assertIsNotNone(request.boss_probabilistic_sequence)
        assert request.boss_probabilistic_sequence is not None
        self.assertIsNone(request.boss_probabilistic_sequence.attack_hit_chance_pct)
        self.assertEqual(
            "attack", request.boss_probabilistic_sequence.events[0].hit_type
        )


if __name__ == "__main__":
    unittest.main()
