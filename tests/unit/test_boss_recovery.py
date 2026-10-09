"""Boss 主动恢复事件状态机、工具、HTTP 与 Build 契约。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api.boss import (
    BossLayeredEvent,
    BossRecoveryCompareRequest,
    BossRecoveryEvent,
    BossRecoveryScenarioRequest,
    BossRecoverySequenceRequest,
    compare_recovery_sequences as compare_recovery_api,
    recovery_sequence,
)
from api.build import BuildBossRecoverySequenceOut, BuildRequest
from calc import boss_recovery, constants
from calc.boss_layered import LayeredDamageEvent
from calc.boss_recovery import ActiveRecoveryEvent, RecoveryPoolConfig
from calc.boss_timeline import TimedDamageEvent
from core import db as dbmod
from orchestrator.loop import _preferred_tool_route
from parsers.pob_build import PobBuild
from tests.unit.test_boss_survival import _conn
from tools.impl.boss_recovery import (
    boss_recovery_sequence_check,
    boss_recovery_sequence_compare,
    boss_recovery_sequence_from_build,
)
from tools.registry import REGISTRY


def _hit(
    at: float, low: float = 60, high: float | None = None,
    *, bypass: float = 0, name: str = "命中",
) -> LayeredDamageEvent:
    upper = low if high is None else high
    return LayeredDamageEvent(
        energy_shield_bypass_pct=bypass,
        damage=TimedDamageEvent(
            at_seconds=at, skill=name, damage_type="Physical",
            raw_low=low, raw_high=upper, taken_low=low, taken_high=upper,
        ),
    )


def _instant(
    at: float, amount: float, *, pool: str = "life", phase: str = "before_hit",
    name: str = "瞬回",
) -> ActiveRecoveryEvent:
    return ActiveRecoveryEvent(
        name=name, at_seconds=at, pool=pool, kind="instant", amount=amount,
        phase=phase,
    )  # type: ignore[arg-type]


def _rate(
    at: float, rate: float, duration: float, *, pool: str = "life",
    kind: str = "timed_rate", name: str = "持续恢复",
) -> ActiveRecoveryEvent:
    return ActiveRecoveryEvent(
        name=name, at_seconds=at, pool=pool, kind=kind,
        rate_per_second=rate, duration_seconds=duration,
    )  # type: ignore[arg-type]


class BossRecoveryFormulaTest(unittest.TestCase):
    def test_instant_between_hits_applies_once_and_caps_at_pool(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0), _hit(2)],
            recovery_events=[_instant(1, 80)],
            config=RecoveryPoolConfig(life=100),
        )
        roll = result.high_roll
        self.assertTrue(roll.survived)
        self.assertEqual(40, roll.ending_life)
        self.assertEqual(60, roll.total_instant_recovery_applied)
        self.assertEqual(20, roll.total_recovery_overflow)
        self.assertEqual(1, len(roll.recovery_records))

    def test_before_hit_and_after_hit_have_distinct_ordering(self) -> None:
        before = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 80), _hit(1, 80)],
            recovery_events=[_instant(1, 70, phase="before_hit")],
            config=RecoveryPoolConfig(life=100),
        )
        after = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 80), _hit(1, 80)],
            recovery_events=[_instant(1, 70, phase="after_hit")],
            config=RecoveryPoolConfig(life=100),
        )
        self.assertTrue(before.high_roll.survived)
        self.assertEqual(10, before.high_roll.ending_life)
        self.assertFalse(after.high_roll.survived)
        self.assertEqual(-60, after.high_roll.ending_life)
        self.assertTrue(after.high_roll.recovery_records[0].skipped_because_depleted)

    def test_same_timestamp_groups_hits_around_instant_phases(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 30), _hit(1, 40), _hit(1, 40)],
            recovery_events=[
                _instant(1, 20, phase="before_hit", name="命中前"),
                _instant(1, 10, phase="after_hit", name="命中后"),
            ],
            config=RecoveryPoolConfig(life=120),
        )
        roll = result.high_roll
        self.assertEqual(20, roll.steps[1].life_before_hit - 90)
        self.assertEqual(0, roll.steps[1].recovery[0].instant_after_applied)
        self.assertEqual(10, roll.steps[2].recovery[0].instant_after_applied)
        self.assertEqual(40, roll.ending_life)

    def test_timed_rate_integrates_only_over_overlap(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 80), _hit(5, 60)],
            recovery_events=[_rate(1, 10, 2)],
            config=RecoveryPoolConfig(life=100),
        )
        roll = result.high_roll
        self.assertEqual(20, roll.total_timed_rate_recovery_applied)
        self.assertEqual(-20, roll.ending_life)
        self.assertFalse(roll.survived)

    def test_overlapping_timed_rates_are_summed_and_pool_capped(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 80), _hit(3, 10)],
            recovery_events=[_rate(0, 20, 3), _rate(1, 30, 2, name="第二瓶")],
            config=RecoveryPoolConfig(life=100),
        )
        roll = result.high_roll
        self.assertEqual(80, roll.total_timed_rate_recovery_applied)
        self.assertEqual(40, roll.total_recovery_overflow)
        self.assertEqual(90, roll.ending_life)

    def test_leech_instances_sum_then_use_life_rate_cap(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 50), _hit(2, 50)],
            recovery_events=[
                _rate(0, 30, 4, kind="leech", name="偷取一"),
                _rate(0, 30, 4, kind="leech", name="偷取二"),
            ],
            config=RecoveryPoolConfig(life=100),
        )
        roll = result.high_roll
        self.assertEqual(40, roll.total_leech_recovery_applied)
        self.assertEqual(80, roll.total_leech_suppressed_by_rate_cap)
        self.assertEqual(40, roll.ending_life)

    def test_leech_caps_are_independent_per_pool_and_overridable(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 120), _hit(2, 10)],
            recovery_events=[
                _rate(0, 100, 2, pool="life", kind="leech", name="生命偷取"),
                _rate(0, 100, 2, pool="energy_shield", kind="leech", name="护盾偷取"),
            ],
            config=RecoveryPoolConfig(
                life=100, energy_shield=50,
                life_leech_rate_cap_pct=30,
                energy_shield_leech_rate_cap_pct=20,
            ),
        )
        roll = result.high_roll
        self.assertEqual(60, roll.steps[1].recovery[0].leech_after_rate_cap)
        self.assertEqual(20, roll.steps[1].recovery[1].leech_after_rate_cap)

    def test_fixed_timed_leech_and_recharge_are_separately_audited(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 70), _hit(3, 10)],
            recovery_events=[
                _rate(0, 5, 3), _rate(0, 50, 3, kind="leech", name="偷取"),
            ],
            config=RecoveryPoolConfig(
                life=100, energy_shield=20, life_recovery_per_second=5,
                energy_shield_recharge_per_second=10,
                energy_shield_recharge_delay_seconds=2,
            ),
        )
        roll = result.high_roll
        self.assertGreater(roll.total_fixed_recovery_applied, 0)
        self.assertGreater(roll.total_timed_rate_recovery_applied, 0)
        self.assertGreater(roll.total_leech_recovery_applied, 0)
        self.assertGreater(roll.total_recharge_applied, 0)

    def test_layered_damage_routing_remains_shield_then_mana_then_life(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 100, bypass=50)],
            recovery_events=[],
            config=RecoveryPoolConfig(
                life=100, energy_shield=50, mana=30, mana_before_life_pct=40,
            ),
        )
        step = result.high_roll.steps[0]
        self.assertEqual(50, step.energy_shield_absorbed)
        self.assertEqual(20, step.mana_absorbed)
        self.assertEqual(30, step.life_damage)
        self.assertEqual(70, step.life_after_hit)

    def test_depletion_stops_all_later_recovery_and_damage_processing(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 100), _hit(10, 1)],
            recovery_events=[_instant(2, 100), _rate(1, 100, 5)],
            config=RecoveryPoolConfig(life=100),
        )
        roll = result.high_roll
        self.assertFalse(roll.survived)
        self.assertEqual(1, roll.depletion_hit)
        self.assertEqual(0, roll.total_instant_recovery_applied)
        self.assertEqual(0, roll.total_timed_rate_recovery_applied)

    def test_low_high_roll_and_active_recovery_impact_are_separate(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 30, 60), _hit(2, 30, 60)],
            recovery_events=[_instant(1, 30)],
            config=RecoveryPoolConfig(life=100),
        )
        self.assertEqual("survives_full_range", result.outcome)
        self.assertTrue(result.active_recovery_impact.changed_high_roll_survival)
        self.assertFalse(
            result.active_recovery_impact.high_roll_survived_without_active_recovery
        )
        self.assertEqual(30, result.active_recovery_impact.high_roll_life_gain)

    def test_single_axis_thresholds_keep_other_layers_and_recovery_fixed(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 80), _hit(2, 80)],
            recovery_events=[_instant(1, 20)],
            config=RecoveryPoolConfig(
                life=100, energy_shield=20, mana=20, mana_before_life_pct=50,
            ),
        )
        self.assertEqual("reachable", result.minimum_life_for_full_range.status)
        self.assertTrue(result.minimum_life_for_full_range.keeps_other_layers_fixed)
        self.assertEqual("reachable", result.minimum_energy_shield_for_full_range.status)
        self.assertEqual("reachable", result.minimum_mana_for_full_range.status)

    def test_energy_shield_axis_unreachable_when_all_damage_bypasses(self) -> None:
        result = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 100, bypass=100)], recovery_events=[],
            config=RecoveryPoolConfig(life=50, energy_shield=20),
        )
        self.assertEqual(
            "unreachable", result.minimum_energy_shield_for_full_range.status
        )

    def test_validation_rejects_ambiguous_event_shapes(self) -> None:
        with self.assertRaisesRegex(boss_recovery.BossRecoveryError, "瞬回必须"):
            boss_recovery.assess_recovery_sequence(
                damage_events=[_hit(0)],
                recovery_events=[ActiveRecoveryEvent(
                    "错误", 0, "life", "instant", amount=10,
                    rate_per_second=5,
                )],
                config=RecoveryPoolConfig(life=100),
            )
        with self.assertRaisesRegex(boss_recovery.BossRecoveryError, "最后一次命中之后"):
            boss_recovery.assess_recovery_sequence(
                damage_events=[_hit(1)], recovery_events=[_instant(2, 10)],
                config=RecoveryPoolConfig(life=100),
            )
        with self.assertRaisesRegex(boss_recovery.BossRecoveryError, "只有瞬回"):
            boss_recovery.assess_recovery_sequence(
                damage_events=[_hit(0)],
                recovery_events=[ActiveRecoveryEvent(
                    "错误", 0, "life", "leech", rate_per_second=5,
                    duration_seconds=1, phase="after_hit",
                )],
                config=RecoveryPoolConfig(life=100),
            )

    def test_comparison_keeps_survival_margin_recovery_and_threshold_separate(self) -> None:
        weak = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 70), _hit(2, 70)], recovery_events=[],
            config=RecoveryPoolConfig(life=100),
        )
        active = boss_recovery.assess_recovery_sequence(
            damage_events=[_hit(0, 70), _hit(2, 70)],
            recovery_events=[_instant(1, 50)], config=RecoveryPoolConfig(life=100),
        )
        compared = boss_recovery.compare_recovery_sequences([
            ("无主动恢复", weak), ("带瞬回", active),
        ])
        self.assertEqual("带瞬回", compared.best_survival_scenario)
        self.assertEqual("带瞬回", compared.best_high_roll_life_margin_scenario)
        self.assertEqual("带瞬回", compared.highest_active_recovery_scenario)
        self.assertEqual("带瞬回", compared.lowest_required_life_scenario)


class BossRecoverySurfaceTest(unittest.TestCase):
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
    def _inputs(*, active: bool = True) -> dict[str, object]:
        return {
            "events": [
                {"skill": "Test Slam", "at_seconds": 0},
                {"skill": "Test Slam", "at_seconds": 3,
                 "energy_shield_bypass_pct": 20},
            ],
            "recovery_events": ([
                {"name": "生命药剂", "at_seconds": 1, "pool": "life",
                 "kind": "instant", "amount": 1000},
                {"name": "生命偷取", "at_seconds": 0, "pool": "life",
                 "kind": "leech", "rate_per_second": 500,
                 "duration_seconds": 3},
            ] if active else []),
            "level": 84, "life": 15000, "energy_shield": 3000,
            "mana": 1000, "mana_before_life_pct": 30, "armour": 0,
            "life_recovery_per_second": 100,
            "energy_shield_recovery_per_second": 50,
            "mana_recovery_per_second": 80,
            "energy_shield_recharge_per_second": 990,
        }

    def test_tool_reuses_damage_chain_and_returns_recovery_audit(self) -> None:
        result = boss_recovery_sequence_check(**self._inputs())
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(2, result.data["hit_count"])
        self.assertEqual(2, len(result.data["requested_recovery_events"]))
        self.assertGreater(
            result.data["high_roll"]["total_instant_recovery_applied"], 0
        )
        self.assertIn("主动恢复事件时间轴", result.data["answer"])
        self.assertIn("等比例归属", result.data["recovery_attribution_model"])
        self.assertIn("药剂充能", result.data["boundary"])
        self.assertEqual(2, len(result.citations))

    def test_tool_preserves_unknown_from_damage_preflight(self) -> None:
        result = boss_recovery_sequence_check(
            events=[{"skill": "Shaper Ball", "at_seconds": 0}],
            recovery_events=[], level=84, life=10000,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertEqual(["冰霜抗性"], result.data["missing_inputs"])
        self.assertNotIn("high_roll", result.data)

    def test_tool_rejects_unknown_and_ambiguous_recovery_fields(self) -> None:
        inputs = self._inputs()
        inputs["recovery_events"] = [{
            "name": "错误", "at_seconds": 1, "pool": "life",
            "kind": "instant", "amount": 10, "charges": 1,
        }]
        unknown = boss_recovery_sequence_check(**inputs)
        self.assertFalse(unknown.ok)
        inputs["recovery_events"] = [{
            "name": "错误", "at_seconds": 1, "pool": "life",
            "kind": "instant", "amount": 10, "rate_per_second": 1,
        }]
        ambiguous = boss_recovery_sequence_check(**inputs)
        self.assertFalse(ambiguous.ok)
        self.assertIn("瞬回必须", ambiguous.error)

    def test_compare_tool_returns_independent_best_metrics(self) -> None:
        result = boss_recovery_sequence_compare(scenarios=[
            {"name": "无主动恢复", "inputs": self._inputs(active=False)},
            {"name": "带主动恢复", "inputs": self._inputs(active=True)},
        ])
        self.assertTrue(result.ok)
        self.assertEqual("带主动恢复", result.data["highest_active_recovery_scenario"])
        self.assertEqual(2, len(result.data["scenarios"]))
        self.assertIn("主动恢复方案比较", result.data["answer"])

    def test_http_endpoints_and_openapi_contract(self) -> None:
        request = BossRecoverySequenceRequest(
            events=[
                BossLayeredEvent(skill="Test Slam", at_seconds=0),
                BossLayeredEvent(skill="Test Slam", at_seconds=3),
            ],
            recovery_events=[BossRecoveryEvent(
                name="生命药剂", at_seconds=1, pool="life",
                kind="instant", amount=1000,
            )],
            level=84, life=15000,
        )
        response = recovery_sequence(request, _caller="test")
        self.assertEqual(2, response.hit_count)
        self.assertIsNotNone(response.high_roll)
        compared = compare_recovery_api(BossRecoveryCompareRequest(scenarios=[
            BossRecoveryScenarioRequest(name="A", inputs=request),
            BossRecoveryScenarioRequest(name="B", inputs=request.model_copy(
                update={"recovery_events": []},
            )),
        ]), _caller="test")
        self.assertEqual(2, len(compared["scenarios"]))
        from main import create_app

        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/boss/recovery-sequence", paths)
        self.assertIn("/poe_agent/boss/recovery-sequence/compare", paths)

    def test_registry_knowledge_and_natural_language_routes_are_wired(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS

        for name in ("boss_recovery_sequence_check", "boss_recovery_sequence_compare"):
            self.assertIn(name, ALLOWED_TOOLS)
            self.assertIsNotNone(REGISTRY.spec(name))
        self.assertEqual(
            "boss_recovery_sequence_check",
            _preferred_tool_route("Boss 命中前瞬回的主动恢复事件时间轴"),
        )
        self.assertEqual(
            "boss_recovery_sequence_compare",
            _preferred_tool_route("两套药剂恢复方案对比"),
        )

    @staticmethod
    def _build_stats() -> dict[str, float]:
        return {
            "LifeUnreserved": 15000, "EnergyShield": 3000,
            "ManaUnreserved": 1000, "Armour": 0,
            "NetLifeRegen": 100, "NetEnergyShieldRegen": 50,
            "NetManaRegen": 80, "EnergyShieldRecharge": 990,
        }

    def test_build_reads_layers_but_keeps_active_events_explicit(self) -> None:
        result = boss_recovery_sequence_from_build(
            PobBuild(player_stats=self._build_stats()),
            events=self._inputs()["events"],
            recovery_events=self._inputs()["recovery_events"],
            level=84, mana_before_life_pct=30,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(15000, result.data["config"]["life"])
        self.assertEqual(
            "explicit_request", result.data["source_config"]["recovery_events"]
        )
        self.assertEqual(
            "pob_player_stat_and_explicit_recovery_events",
            result.data["input_source"],
        )
        response = BuildBossRecoverySequenceOut(
            **result.data, citations=result.citations,
        )
        self.assertEqual(
            "build_active_recovery_timed_sequence", response.calculation_mode
        )

    def test_build_missing_relevant_fixed_recovery_stays_unknown(self) -> None:
        stats = self._build_stats()
        stats.pop("NetLifeRegen")
        result = boss_recovery_sequence_from_build(
            PobBuild(player_stats=stats), events=self._inputs()["events"],
            recovery_events=self._inputs()["recovery_events"],
            level=84, mana_before_life_pct=30,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertIn("life_recovery_per_second", result.data["missing_inputs"][-1])

    def test_build_request_exposes_recovery_contract(self) -> None:
        request = BuildRequest(code="abc", boss_recovery_sequence={
            "events": [{"skill": "Test Slam", "at_seconds": 0}],
            "recovery_events": [{
                "name": "生命药剂", "at_seconds": 0, "pool": "life",
                "kind": "instant", "amount": 1000, "phase": "after_hit",
            }],
            "level": 84,
        })
        self.assertIsNotNone(request.boss_recovery_sequence)
        assert request.boss_recovery_sequence is not None
        self.assertEqual(
            "after_hit", request.boss_recovery_sequence.recovery_events[0].phase
        )
        self.assertEqual(20, request.boss_recovery_sequence.life_leech_rate_cap_pct)


if __name__ == "__main__":
    unittest.main()
