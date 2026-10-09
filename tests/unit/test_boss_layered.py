"""Boss 分层防御池状态机、工具、HTTP 与 Build 契约。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api.boss import (
    BossLayeredCompareRequest,
    BossLayeredEvent,
    BossLayeredScenarioRequest,
    BossLayeredSequenceRequest,
    compare_layered_sequences as compare_layered_api,
    layered_sequence,
)
from api.build import BuildBossLayeredSequenceOut, BuildRequest
from calc import boss_layered, constants
from calc.boss_layered import LayeredDamageEvent, LayeredPoolConfig
from calc.boss_timeline import TimedDamageEvent
from core import db as dbmod
from orchestrator.loop import _preferred_tool_route
from parsers.pob_build import PobBuild
from tests.unit.test_boss_survival import _conn
from tools.impl.boss_layered import (
    boss_layered_sequence_check,
    boss_layered_sequence_compare,
    boss_layered_sequence_from_build,
)
from tools.registry import REGISTRY


def _event(
    at: float, low: float = 80, high: float | None = None,
    bypass: float = 0, name: str = "命中",
) -> LayeredDamageEvent:
    upper = low if high is None else high
    return LayeredDamageEvent(
        energy_shield_bypass_pct=bypass,
        damage=TimedDamageEvent(
            at_seconds=at, skill=name, damage_type="Physical",
            raw_low=low, raw_high=upper, taken_low=low, taken_high=upper,
        ),
    )


class BossLayeredFormulaTest(unittest.TestCase):
    def test_damage_routes_energy_shield_then_mana_then_life(self) -> None:
        result = boss_layered.assess_layered_sequence(
            events=[_event(0), _event(3)],
            config=LayeredPoolConfig(
                life=100, energy_shield=50, mana=40, mana_before_life_pct=40,
            ),
        )
        first = result.high_roll.steps[0]
        self.assertEqual(50, first.energy_shield_absorbed)
        self.assertEqual(12, first.mana_target_damage)
        self.assertEqual(12, first.mana_absorbed)
        self.assertEqual(18, first.life_damage)
        second = result.high_roll.steps[1]
        self.assertEqual(32, second.mana_target_damage)
        self.assertEqual(28, second.mana_absorbed)
        self.assertEqual(4, second.mana_shortfall_to_life)
        self.assertEqual(52, second.life_damage)
        self.assertEqual(30, result.high_roll.ending_life)

    def test_explicit_energy_shield_bypass_is_applied_before_mana_split(self) -> None:
        result = boss_layered.assess_layered_sequence(
            events=[_event(0, 100, bypass=50)],
            config=LayeredPoolConfig(
                life=100, energy_shield=50, mana=30, mana_before_life_pct=40,
            ),
        )
        step = result.high_roll.steps[0]
        self.assertEqual(50, step.energy_shield_bypassed)
        self.assertEqual(50, step.energy_shield_absorbed)
        self.assertEqual(20, step.mana_absorbed)
        self.assertEqual(30, step.life_damage)
        self.assertEqual(70, step.life_after_hit)

    def test_insufficient_mana_falls_back_to_life(self) -> None:
        result = boss_layered.assess_layered_sequence(
            events=[_event(0, 100)],
            config=LayeredPoolConfig(
                life=100, mana=5, mana_before_life_pct=40,
            ),
        )
        step = result.high_roll.steps[0]
        self.assertEqual(40, step.mana_target_damage)
        self.assertEqual(5, step.mana_absorbed)
        self.assertEqual(35, step.mana_shortfall_to_life)
        self.assertEqual(95, step.life_damage)
        self.assertEqual(5, step.life_after_hit)

    def test_fixed_recovery_is_independent_and_capped_per_pool(self) -> None:
        result = boss_layered.assess_layered_sequence(
            events=[_event(0, 60), _event(2, 20)],
            config=LayeredPoolConfig(
                life=100, energy_shield=30, mana=20, mana_before_life_pct=50,
                life_recovery_per_second=10,
                energy_shield_recovery_per_second=10,
                mana_recovery_per_second=5,
            ),
        )
        step = result.high_roll.steps[1]
        self.assertEqual(15, step.life_recovered)
        self.assertEqual(20, step.energy_shield_recovered)
        self.assertEqual(10, step.mana_recovered)
        self.assertEqual(100, step.life_before_hit)
        self.assertEqual(20, step.energy_shield_before_hit)
        self.assertEqual(15, step.mana_before_hit)

    def test_energy_shield_recharge_waits_for_delay_and_is_capped(self) -> None:
        result = boss_layered.assess_layered_sequence(
            events=[_event(0), _event(3)],
            config=LayeredPoolConfig(
                life=100, energy_shield=50,
                energy_shield_recharge_per_second=20,
                energy_shield_recharge_delay_seconds=2,
            ),
        )
        second = result.high_roll.steps[1]
        self.assertEqual(1, second.recharge_eligible_seconds)
        self.assertEqual(20, second.energy_shield_recharged)
        self.assertEqual(20, second.energy_shield_before_hit)
        self.assertEqual(2, result.high_roll.recharge_interruption_count)
        self.assertEqual(10, result.high_roll.ending_life)

    def test_recharge_interrupt_mode_distinguishes_bypass_hit(self) -> None:
        events = [_event(0, 15), _event(3, 10, bypass=100), _event(4, 10)]
        es_damage = boss_layered.assess_layered_sequence(
            events=events,
            config=LayeredPoolConfig(
                life=100, energy_shield=20,
                energy_shield_recharge_per_second=10,
                energy_shield_recharge_delay_seconds=2,
                energy_shield_recharge_interrupt_mode="energy_shield_damage",
            ),
        )
        any_hit = boss_layered.assess_layered_sequence(
            events=events,
            config=LayeredPoolConfig(
                life=100, energy_shield=20,
                energy_shield_recharge_per_second=10,
                energy_shield_recharge_delay_seconds=2,
                energy_shield_recharge_interrupt_mode="any_hit",
            ),
        )
        self.assertFalse(es_damage.high_roll.steps[1].recharge_interrupted)
        self.assertTrue(any_hit.high_roll.steps[1].recharge_interrupted)
        self.assertGreater(
            es_damage.high_roll.steps[2].energy_shield_before_hit,
            any_hit.high_roll.steps[2].energy_shield_before_hit,
        )

    def test_low_and_high_roll_tracks_produce_roll_dependent_outcome(self) -> None:
        result = boss_layered.assess_layered_sequence(
            events=[_event(0, 30, 70), _event(1, 30, 70)],
            config=LayeredPoolConfig(life=100),
        )
        self.assertEqual("roll_dependent", result.outcome)
        self.assertTrue(result.low_roll.survived)
        self.assertFalse(result.high_roll.survived)
        self.assertEqual(40, result.low_roll.ending_life)
        self.assertEqual(-40, result.high_roll.ending_life)
        self.assertEqual(2, result.high_roll.depletion_hit)

    def test_exact_zero_life_is_depleted_and_cannot_recover(self) -> None:
        result = boss_layered.assess_layered_sequence(
            events=[_event(0, 100), _event(10, 1)],
            config=LayeredPoolConfig(life=100, life_recovery_per_second=100),
        )
        self.assertFalse(result.high_roll.survived)
        self.assertEqual(1, result.high_roll.depletion_hit)
        self.assertEqual(0, result.high_roll.total_life_recovered)
        self.assertTrue(result.high_roll.steps[1].depleted)

    def test_single_axis_thresholds_keep_other_layers_fixed(self) -> None:
        result = boss_layered.assess_layered_sequence(
            events=[_event(0), _event(3)],
            config=LayeredPoolConfig(
                life=100, energy_shield=50, mana=40, mana_before_life_pct=40,
            ),
        )
        self.assertEqual(71, result.minimum_life_for_full_range.value)
        self.assertEqual(21, result.minimum_energy_shield_for_full_range.value)
        self.assertEqual(11, result.minimum_mana_for_full_range.value)
        self.assertTrue(result.minimum_life_for_full_range.keeps_other_layers_fixed)
        no_mom = boss_layered.assess_layered_sequence(
            events=[_event(0)], config=LayeredPoolConfig(life=100),
        )
        self.assertEqual("not_applicable", no_mom.minimum_mana_for_full_range.status)

    def test_energy_shield_axis_can_be_unreachable_when_all_damage_bypasses(self) -> None:
        result = boss_layered.assess_layered_sequence(
            events=[_event(0, 100, bypass=100)],
            config=LayeredPoolConfig(life=50, energy_shield=20),
        )
        self.assertEqual(
            "unreachable", result.minimum_energy_shield_for_full_range.status
        )
        self.assertIsNone(result.minimum_energy_shield_for_full_range.value)

    def test_validation_rejects_invalid_order_bypass_and_interrupt_mode(self) -> None:
        with self.assertRaisesRegex(boss_layered.BossLayeredError, "非递减"):
            boss_layered.assess_layered_sequence(
                events=[_event(2), _event(1)], config=LayeredPoolConfig(life=100),
            )
        with self.assertRaisesRegex(boss_layered.BossLayeredError, "不能超过 100"):
            boss_layered.assess_layered_sequence(
                events=[_event(0, bypass=101)],
                config=LayeredPoolConfig(life=100),
            )
        with self.assertRaisesRegex(boss_layered.BossLayeredError, "只支持"):
            boss_layered.assess_layered_sequence(
                events=[_event(0)],
                config=LayeredPoolConfig(
                    life=100, energy_shield_recharge_interrupt_mode="sometimes",  # type: ignore[arg-type]
                ),
            )

    def test_comparison_ranks_survival_margin_damage_and_life_threshold(self) -> None:
        weak = boss_layered.assess_layered_sequence(
            events=[_event(0), _event(3)], config=LayeredPoolConfig(life=100),
        )
        layered = boss_layered.assess_layered_sequence(
            events=[_event(0), _event(3)],
            config=LayeredPoolConfig(life=100, energy_shield=50),
        )
        compared = boss_layered.compare_layered_sequences([
            ("纯生命", weak), ("生命护盾", layered),
        ])
        self.assertEqual("生命护盾", compared.best_survival_scenario)
        self.assertEqual("生命护盾", compared.best_high_roll_life_margin_scenario)
        self.assertEqual("生命护盾", compared.lowest_high_roll_life_damage_scenario)
        self.assertEqual("生命护盾", compared.lowest_required_life_scenario)


class BossLayeredSurfaceTest(unittest.TestCase):
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
    def _inputs(*, energy_shield: float = 3000) -> dict[str, object]:
        return {
            "events": [
                {"skill": "Test Slam", "at_seconds": 0},
                {"skill": "Test Slam", "at_seconds": 3,
                 "energy_shield_bypass_pct": 20},
            ],
            "level": 84, "life": 15000, "energy_shield": energy_shield,
            "mana": 1000, "mana_before_life_pct": 30, "armour": 0,
            "life_recovery_per_second": 100,
            "energy_shield_recovery_per_second": 50,
            "mana_recovery_per_second": 80,
            "energy_shield_recharge_per_second": 990,
        }

    def test_tool_reuses_damage_chain_and_returns_layered_steps(self) -> None:
        result = boss_layered_sequence_check(**self._inputs())
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(2, result.data["hit_count"])
        self.assertGreater(result.data["high_roll"]["total_energy_shield_absorbed"], 0)
        self.assertGreater(result.data["high_roll"]["total_mana_absorbed"], 0)
        self.assertEqual(
            20, result.data["requested_events"][1]["energy_shield_bypass_pct"]
        )
        self.assertIn("分层防御池", result.data["answer"])
        self.assertIn("不从伤害类型", result.data["boundary"])
        self.assertEqual(2, len(result.citations))

    def test_tool_preserves_unknown_from_damage_chain(self) -> None:
        result = boss_layered_sequence_check(
            events=[{"skill": "Shaper Ball", "at_seconds": 0}],
            level=84, life=10000, energy_shield=2000,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertEqual(["冰霜抗性"], result.data["missing_inputs"])
        self.assertNotIn("high_roll", result.data)

    def test_tool_rejects_unknown_event_fields_and_reordered_time(self) -> None:
        unknown = boss_layered_sequence_check(
            events=[{"skill": "Test Slam", "at_seconds": 0, "ward": 100}],
            level=84, life=10000,
        )
        self.assertFalse(unknown.ok)
        reordered = boss_layered_sequence_check(
            events=[
                {"skill": "Test Slam", "at_seconds": 2},
                {"skill": "Test Slam", "at_seconds": 1},
            ],
            level=84, life=10000,
        )
        self.assertFalse(reordered.ok)
        self.assertIn("非递减", reordered.error)

    def test_compare_tool_returns_separate_best_metrics(self) -> None:
        result = boss_layered_sequence_compare(scenarios=[
            {"name": "纯生命", "inputs": self._inputs(energy_shield=0)},
            {"name": "生命护盾", "inputs": self._inputs(energy_shield=3000)},
        ])
        self.assertTrue(result.ok)
        self.assertEqual("生命护盾", result.data["best_survival_scenario"])
        self.assertEqual(2, len(result.data["scenarios"]))
        self.assertIn("分层防御池方案比较", result.data["answer"])

    def test_http_endpoints_and_openapi_contract(self) -> None:
        request = BossLayeredSequenceRequest(
            events=[
                BossLayeredEvent(skill="Test Slam", at_seconds=0),
                BossLayeredEvent(
                    skill="Test Slam", at_seconds=3,
                    energy_shield_bypass_pct=20,
                ),
            ],
            level=84, life=15000, energy_shield=3000,
            mana=1000, mana_before_life_pct=30,
        )
        response = layered_sequence(request, _caller="test")
        self.assertEqual(2, response.hit_count)
        self.assertIsNotNone(response.high_roll)
        compared = compare_layered_api(BossLayeredCompareRequest(scenarios=[
            BossLayeredScenarioRequest(name="A", inputs=request),
            BossLayeredScenarioRequest(name="B", inputs=request.model_copy(
                update={"energy_shield": 0},
            )),
        ]), _caller="test")
        self.assertEqual(2, len(compared["scenarios"]))
        from main import create_app

        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/boss/layered-sequence", paths)
        self.assertIn("/poe_agent/boss/layered-sequence/compare", paths)

    def test_registry_knowledge_and_natural_language_routes_are_wired(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS

        for name in ("boss_layered_sequence_check", "boss_layered_sequence_compare"):
            self.assertIn(name, ALLOWED_TOOLS)
            self.assertIsNotNone(REGISTRY.spec(name))
        self.assertEqual(
            "boss_layered_sequence_check",
            _preferred_tool_route("Boss 分层防御池里伤害由魔力承担后还能扛吗"),
        )
        self.assertEqual(
            "boss_layered_sequence_compare",
            _preferred_tool_route("两套生命护盾魔力方案对比"),
        )

    @staticmethod
    def _build_stats() -> dict[str, float]:
        return {
            "LifeUnreserved": 15000,
            "EnergyShield": 3000,
            "ManaUnreserved": 1000,
            "Armour": 0,
            "NetLifeRegen": 100,
            "NetEnergyShieldRegen": 50,
            "NetManaRegen": 80,
            "EnergyShieldRecharge": 990,
        }

    def test_build_reads_all_layer_and_recovery_fields(self) -> None:
        result = boss_layered_sequence_from_build(
            PobBuild(player_stats=self._build_stats()),
            events=self._inputs()["events"], level=84,
            mana_before_life_pct=30,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(15000, result.data["config"]["life"])
        self.assertEqual(3000, result.data["config"]["energy_shield"])
        self.assertEqual(1000, result.data["config"]["mana"])
        self.assertEqual(990, result.data["config"]["energy_shield_recharge_per_second"])
        self.assertEqual(
            "pob_player_stat:EnergyShieldRecharge",
            result.data["source_config"]["energy_shield_recharge_per_second"],
        )
        response = BuildBossLayeredSequenceOut(
            **result.data, citations=result.citations,
        )
        self.assertEqual("build_layered_pool_timed_sequence", response.calculation_mode)

    def test_build_without_mom_does_not_require_mana_fields(self) -> None:
        stats = self._build_stats()
        stats.pop("ManaUnreserved")
        stats.pop("NetManaRegen")
        result = boss_layered_sequence_from_build(
            PobBuild(player_stats=stats), events=self._inputs()["events"],
            level=84, mana_before_life_pct=0,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(0, result.data["config"]["mana"])
        self.assertEqual("not_applicable", result.data["source_config"]["mana"])

    def test_build_with_zero_energy_shield_does_not_require_es_recovery(self) -> None:
        stats = self._build_stats()
        stats["EnergyShield"] = 0
        stats.pop("NetEnergyShieldRegen")
        stats.pop("EnergyShieldRecharge")
        result = boss_layered_sequence_from_build(
            PobBuild(player_stats=stats), events=self._inputs()["events"],
            level=84, mana_before_life_pct=30,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(0, result.data["config"]["energy_shield"])
        self.assertEqual(
            "not_applicable",
            result.data["source_config"]["energy_shield_recharge_per_second"],
        )

    def test_build_missing_relevant_layer_field_stays_unknown(self) -> None:
        stats = self._build_stats()
        stats.pop("EnergyShieldRecharge")
        result = boss_layered_sequence_from_build(
            PobBuild(player_stats=stats), events=self._inputs()["events"],
            level=84, mana_before_life_pct=30,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertIn(
            "energy_shield_recharge_per_second", result.data["missing_inputs"][-1]
        )
        self.assertNotIn("high_roll", result.data)

    def test_build_explicit_zero_recovery_is_not_treated_as_missing(self) -> None:
        stats = self._build_stats()
        for key in (
            "NetLifeRegen", "NetEnergyShieldRegen", "NetManaRegen",
            "EnergyShieldRecharge",
        ):
            stats.pop(key)
        result = boss_layered_sequence_from_build(
            PobBuild(player_stats=stats), events=self._inputs()["events"],
            level=84, mana_before_life_pct=30,
            life_recovery_per_second=0,
            energy_shield_recovery_per_second=0,
            mana_recovery_per_second=0,
            energy_shield_recharge_per_second=0,
        )
        self.assertTrue(result.data["assessable"])
        self.assertEqual(
            "explicit_request", result.data["source_config"]["life_recovery_per_second"]
        )

    def test_build_request_and_response_expose_layered_contract(self) -> None:
        request = BuildRequest(code="abc", boss_layered_sequence={
            "events": [{
                "skill": "Test Slam", "at_seconds": 0,
                "energy_shield_bypass_pct": 25,
            }],
            "level": 84, "mana_before_life_pct": 30,
        })
        self.assertIsNotNone(request.boss_layered_sequence)
        assert request.boss_layered_sequence is not None
        self.assertEqual(
            25, request.boss_layered_sequence.events[0].energy_shield_bypass_pct
        )
        self.assertIsNone(request.boss_layered_sequence.life_recovery_per_second)


if __name__ == "__main__":
    unittest.main()
