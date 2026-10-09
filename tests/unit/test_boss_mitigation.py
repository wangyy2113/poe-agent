"""Boss 临时减伤窗口与 Guard 吸收池的公式和表面契约。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api.boss import (
    BossDotWindow,
    BossLayeredEvent,
    BossMitigationCompareRequest,
    BossMitigationScenarioRequest,
    BossMitigationSequenceRequest,
    BossMitigationWindow,
    compare_mitigation_sequences as compare_mitigation_api,
    mitigation_sequence,
)
from api.build import BuildBossMitigationSequenceOut, BuildRequest

from calc import boss_mitigation, constants
from calc.boss_dot import DotTimelineConfig, DotWindow
from calc.boss_layered import LayeredDamageEvent
from calc.boss_mitigation import MitigationWindow
from calc.boss_recovery import ActiveRecoveryEvent, RecoveryPoolConfig
from calc.boss_timeline import TimedDamageEvent
from core import db as dbmod
from orchestrator.loop import _preferred_tool_route
from parsers.pob_build import PobBuild
from tests.unit.test_boss_survival import _conn
from tools.impl.boss_mitigation import (
    boss_mitigation_sequence_check,
    boss_mitigation_sequence_compare,
    boss_mitigation_sequence_from_build,
)
from tools.registry import REGISTRY


def _dot(
    name: str = "燃烧地面", *, start: float = 0, duration: float = 4,
    low: float = 20, high: float | None = None, damage_type: str = "Fire",
    bypass: float = 100,
) -> DotWindow:
    return DotWindow(
        name=name, start_seconds=start, duration_seconds=duration,
        damage_type=damage_type, taken_dps_low=low,
        taken_dps_high=low if high is None else high,
        energy_shield_bypass_pct=bypass,
    )  # type: ignore[arg-type]


def _hit(
    at: float, low: float, *, high: float | None = None,
    damage_type: str = "Physical", bypass: float = 100, name: str = "重击",
) -> LayeredDamageEvent:
    upper = low if high is None else high
    return LayeredDamageEvent(
        energy_shield_bypass_pct=bypass,
        damage=TimedDamageEvent(
            at_seconds=at, skill=name, damage_type=damage_type,
            raw_low=low, raw_high=upper, taken_low=low, taken_high=upper,
        ),
    )


def _window(
    name: str = "临时防御", *, start: float = 0, duration: float = 4,
    scope: str = "both", types: tuple[str, ...] = (), multiplier: float = 1,
    guard: float = 0, priority: int = 100,
) -> MitigationWindow:
    return MitigationWindow(
        name=name, start_seconds=start, duration_seconds=duration,
        scope=scope, damage_types=types, taken_multiplier=multiplier,
        guard_capacity=guard, priority=priority,
    )  # type: ignore[arg-type]


def _config(
    *, life: float = 200, energy_shield: float = 0, mana: float = 0,
    mom: float = 0, recovery: float = 0,
) -> DotTimelineConfig:
    return DotTimelineConfig(pools=RecoveryPoolConfig(
        life=life, energy_shield=energy_shield, mana=mana,
        mana_before_life_pct=mom, life_recovery_per_second=recovery,
    ))


class BossMitigationFormulaTest(unittest.TestCase):
    def test_multiplier_window_reduces_dot_before_pool_routing(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[], dot_windows=[_dot(duration=4, low=20)],
            recovery_events=[], config=_config(),
            mitigation_windows=[_window(multiplier=0.5)],
        )
        roll = result.high_roll
        self.assertEqual(80, roll.damage_before_mitigation)
        self.assertEqual(40, roll.damage_after_mitigation)
        self.assertEqual(40, roll.multiplier_prevented)
        self.assertEqual(160, roll.timeline.ending_life)

    def test_guard_depletes_at_exact_dot_time_and_residual_resumes(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[], dot_windows=[_dot(duration=5, low=20)],
            recovery_events=[], config=_config(),
            mitigation_windows=[_window(duration=5, guard=50)],
        )
        roll = result.high_roll
        self.assertEqual(50, roll.guard_absorbed)
        self.assertEqual(50, roll.damage_after_mitigation)
        self.assertEqual(2.5, roll.windows[0].guard_depletion_time_seconds)
        self.assertEqual(2, len(roll.dot_segments))

    def test_hit_consumes_guard_before_later_dot(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[_hit(1, 40)], dot_windows=[_dot(duration=4, low=20)],
            recovery_events=[], config=_config(),
            mitigation_windows=[_window(guard=50)],
        )
        roll = result.high_roll
        # DoT before the hit consumed the first 20 points, so only 30 remained for the hit.
        self.assertEqual(30, roll.hit_steps[0].guard_absorbed)
        self.assertEqual(10, roll.hit_steps[0].damage_after_guards)
        self.assertEqual(0, roll.windows[0].unused_guard_capacity)

    def test_scope_hit_does_not_reduce_dot(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[_hit(1, 40)], dot_windows=[_dot(duration=2, low=20)],
            recovery_events=[], config=_config(),
            mitigation_windows=[_window(duration=2, scope="hit", multiplier=0.5)],
        )
        roll = result.high_roll
        self.assertEqual(20, roll.hit_steps[0].damage_after_guards)
        self.assertEqual(40, sum(row.damage_after_guards for row in roll.dot_segments))

    def test_scope_dot_does_not_reduce_hit(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[_hit(1, 40)], dot_windows=[_dot(duration=2, low=20)],
            recovery_events=[], config=_config(),
            mitigation_windows=[_window(duration=2, scope="dot", multiplier=0.5)],
        )
        roll = result.high_roll
        self.assertEqual(40, roll.hit_steps[0].damage_after_guards)
        self.assertEqual(20, sum(row.damage_after_guards for row in roll.dot_segments))

    def test_damage_type_filter_only_matches_selected_type(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[_hit(1, 40, damage_type="Physical")],
            dot_windows=[_dot(duration=2, low=20, damage_type="Fire")],
            recovery_events=[], config=_config(),
            mitigation_windows=[_window(
                duration=2, types=("Fire",), multiplier=0.5,
            )],
        )
        roll = result.high_roll
        self.assertEqual(40, roll.hit_steps[0].damage_after_guards)
        self.assertEqual(20, sum(row.damage_after_guards for row in roll.dot_segments))

    def test_overlapping_multipliers_apply_in_priority_order(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[], dot_windows=[_dot(duration=2, low=100)],
            recovery_events=[], config=_config(life=500),
            mitigation_windows=[
                _window("后序", duration=2, multiplier=0.5, priority=20),
                _window("前序", duration=2, multiplier=0.8, priority=10),
            ],
        )
        roll = result.high_roll
        self.assertEqual(80, roll.damage_after_mitigation)
        summaries = {row.name: row for row in roll.windows}
        self.assertEqual(40, summaries["前序"].multiplier_prevented)
        self.assertEqual(80, summaries["后序"].multiplier_prevented)

    def test_overlapping_guards_use_priority_not_input_order(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[_hit(0, 80)], dot_windows=[_dot(duration=1, low=0)],
            recovery_events=[], config=_config(),
            mitigation_windows=[
                _window("后序", duration=1, guard=50, priority=20),
                _window("前序", duration=1, guard=50, priority=10),
            ],
        )
        rows = {row.name: row for row in result.high_roll.windows}
        self.assertEqual(50, rows["前序"].guard_absorbed)
        self.assertEqual(30, rows["后序"].guard_absorbed)
        self.assertEqual(20, rows["后序"].unused_guard_capacity)

    def test_end_boundary_is_exclusive_for_hit(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[_hit(2, 40)], dot_windows=[_dot(duration=2, low=0)],
            recovery_events=[], config=_config(),
            mitigation_windows=[_window(duration=2, multiplier=0.5)],
        )
        self.assertEqual(40, result.high_roll.hit_steps[0].damage_after_guards)
        self.assertEqual((), result.high_roll.hit_steps[0].matching_windows)

    def test_low_high_tracks_deplete_guard_at_different_times(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[], dot_windows=[_dot(duration=5, low=10, high=20)],
            recovery_events=[], config=_config(),
            mitigation_windows=[_window(duration=5, guard=50)],
        )
        self.assertEqual(5, result.low_roll.windows[0].guard_depletion_time_seconds)
        self.assertEqual(2.5, result.high_roll.windows[0].guard_depletion_time_seconds)
        self.assertEqual(0, result.low_roll.damage_after_mitigation)
        self.assertEqual(50, result.high_roll.damage_after_mitigation)

    def test_recovery_and_layered_routing_run_after_mitigation(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[], dot_windows=[_dot(duration=2, low=100, bypass=50)],
            recovery_events=[ActiveRecoveryEvent(
                "瞬回", 1, "life", "instant", amount=20,
            )], config=_config(life=100, energy_shield=100, mana=40, mom=50),
            mitigation_windows=[_window(duration=2, multiplier=0.5)],
        )
        timeline = result.high_roll.timeline
        self.assertEqual(50, timeline.total_energy_shield_damage)
        self.assertEqual(25, timeline.total_mana_damage)
        self.assertEqual(25, timeline.total_life_damage)
        self.assertGreater(timeline.total_recovery_applied, 0)

    def test_mitigation_impact_compares_same_timeline_without_windows(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[_hit(1, 80)], dot_windows=[_dot(duration=2, low=30)],
            recovery_events=[], config=_config(life=100),
            mitigation_windows=[_window(duration=2, guard=80)],
        )
        self.assertFalse(result.mitigation_impact.high_roll_survived_without_mitigation)
        self.assertTrue(result.high_roll.timeline.survived)
        self.assertTrue(result.mitigation_impact.changed_high_roll_survival)

    def test_thresholds_are_from_mitigated_residual_timeline(self) -> None:
        result = boss_mitigation.assess_mitigation_timeline(
            hit_events=[], dot_windows=[_dot(duration=2, low=100)],
            recovery_events=[], config=_config(life=300),
            mitigation_windows=[_window(duration=2, multiplier=0.5)],
        )
        self.assertEqual(101, result.minimum_life_for_full_range.value)

    def test_comparison_keeps_margin_prevention_and_threshold_separate(self) -> None:
        common = dict(
            hit_events=[], dot_windows=[_dot(duration=3, low=50)],
            recovery_events=[], config=_config(life=100),
        )
        weak = boss_mitigation.assess_mitigation_timeline(
            **common, mitigation_windows=[_window(duration=3, guard=10)],
        )
        strong = boss_mitigation.assess_mitigation_timeline(
            **common, mitigation_windows=[_window(duration=3, guard=100)],
        )
        compared = boss_mitigation.compare_mitigation_timelines([
            ("弱窗口", weak), ("强窗口", strong),
        ])
        self.assertEqual("强窗口", compared.best_survival_scenario)
        self.assertEqual("强窗口", compared.highest_prevented_damage_scenario)
        self.assertEqual("强窗口", compared.lowest_required_life_scenario)

    def test_validation_rejects_noop_duplicate_and_out_of_timeline(self) -> None:
        base = dict(
            hit_events=[], dot_windows=[_dot(duration=2)], recovery_events=[],
            config=_config(),
        )
        with self.assertRaisesRegex(boss_mitigation.BossMitigationError, "必须降低承伤"):
            boss_mitigation.assess_mitigation_timeline(
                **base, mitigation_windows=[_window(duration=2)],
            )
        with self.assertRaisesRegex(boss_mitigation.BossMitigationError, "不重复"):
            boss_mitigation.assess_mitigation_timeline(
                **base, mitigation_windows=[
                    _window("A", duration=2, guard=1),
                    _window("A", duration=2, guard=1),
                ],
            )
        with self.assertRaisesRegex(boss_mitigation.BossMitigationError, "不能超出"):
            boss_mitigation.assess_mitigation_timeline(
                **base, mitigation_windows=[_window(start=1, duration=2, guard=1)],
            )


class BossMitigationSurfaceTest(unittest.TestCase):
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
    def _inputs(*, guard: float = 1000) -> dict[str, object]:
        return {
            "events": [
                {"skill": "Test Slam", "at_seconds": 0},
                {"skill": "Test Slam", "at_seconds": 3,
                 "energy_shield_bypass_pct": 20},
            ],
            "dot_windows": [{
                "name": "显式燃烧地面", "start_seconds": 0,
                "duration_seconds": 3, "damage_type": "Fire",
                "damage_basis": "raw", "dps_low": 400, "dps_high": 480,
                "energy_shield_bypass_pct": 10,
            }],
            "recovery_events": [{
                "name": "生命药剂", "at_seconds": 1, "pool": "life",
                "kind": "instant", "amount": 500,
            }],
            "mitigation_windows": [{
                "name": "显式 Guard 窗口", "start_seconds": 0,
                "duration_seconds": 3, "scope": "both",
                "damage_types": [], "taken_multiplier": 0.8,
                "guard_capacity": guard, "priority": 10,
            }],
            "level": 84, "life": 15000, "energy_shield": 3000,
            "mana": 1000, "mana_before_life_pct": 30, "armour": 0,
            "fire_resist_pct": 75, "life_recovery_per_second": 100,
            "energy_shield_recovery_per_second": 50,
            "mana_recovery_per_second": 80,
            "energy_shield_recharge_per_second": 990,
        }

    def test_tool_returns_window_audit_guard_and_baseline_impact(self) -> None:
        result = boss_mitigation_sequence_check(**self._inputs())
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(1, result.data["mitigation_window_count"])
        self.assertGreater(result.data["high_roll"]["total_prevented"], 0)
        self.assertTrue(result.data["high_roll"]["hit_steps"])
        self.assertTrue(result.data["high_roll"]["dot_segments"])
        self.assertIn("Boss 临时减伤与 Guard 时间轴", result.data["answer"])
        self.assertIn("Ward 破裂", result.data["boundary"])
        self.assertEqual(2, len(result.citations))

    def test_unknown_dot_resistance_stays_unknown_before_windows(self) -> None:
        inputs = self._inputs()
        inputs.pop("fire_resist_pct")
        result = boss_mitigation_sequence_check(**inputs)
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertIn("火焰抗性", result.data["missing_inputs"])
        self.assertNotIn("high_roll", result.data)

    def test_window_validation_rejects_unknown_and_noop_shapes(self) -> None:
        inputs = self._inputs()
        inputs["mitigation_windows"] = [{
            "name": "错误", "start_seconds": 0, "duration_seconds": 1,
            "guard_capacity": 10, "cooldown": 4,
        }]
        unknown = boss_mitigation_sequence_check(**inputs)
        self.assertFalse(unknown.ok)
        inputs["mitigation_windows"] = [{
            "name": "空窗口", "start_seconds": 0, "duration_seconds": 1,
        }]
        noop = boss_mitigation_sequence_check(**inputs)
        self.assertFalse(noop.ok)
        self.assertIn("必须降低承伤", noop.error)

    def test_compare_tool_keeps_best_metrics_separate(self) -> None:
        result = boss_mitigation_sequence_compare(scenarios=[
            {"name": "小 Guard", "inputs": self._inputs(guard=100)},
            {"name": "大 Guard", "inputs": self._inputs(guard=2000)},
        ])
        self.assertTrue(result.ok)
        self.assertEqual("大 Guard", result.data["highest_prevented_damage_scenario"])
        self.assertEqual(2, len(result.data["scenarios"]))
        self.assertIn("临时减伤与 Guard 方案比较", result.data["answer"])

    def test_http_endpoints_and_openapi_contract(self) -> None:
        request = BossMitigationSequenceRequest(
            events=[BossLayeredEvent(skill="Test Slam", at_seconds=0)],
            dot_windows=[BossDotWindow(
                name="燃烧", start_seconds=0, duration_seconds=2,
                damage_type="Fire", damage_basis="raw", dps_low=100, dps_high=120,
            )], recovery_events=[], mitigation_windows=[BossMitigationWindow(
                name="Guard", start_seconds=0, duration_seconds=2,
                taken_multiplier=0.8, guard_capacity=100,
            )], level=84, life=15000, fire_resist_pct=75,
        )
        response = mitigation_sequence(request, _caller="test")
        self.assertEqual(1, response.mitigation_window_count)
        self.assertIsNotNone(response.high_roll)
        compared = compare_mitigation_api(BossMitigationCompareRequest(scenarios=[
            BossMitigationScenarioRequest(name="A", inputs=request),
            BossMitigationScenarioRequest(name="B", inputs=request.model_copy(update={
                "mitigation_windows": [request.mitigation_windows[0].model_copy(
                    update={"guard_capacity": 200},
                )],
            })),
        ]), _caller="test")
        self.assertEqual(2, len(compared["scenarios"]))
        from main import create_app

        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/boss/mitigation-sequence", paths)
        self.assertIn("/poe_agent/boss/mitigation-sequence/compare", paths)

    def test_registry_knowledge_and_routes_are_wired(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS

        for name in (
            "boss_mitigation_sequence_check", "boss_mitigation_sequence_compare",
        ):
            self.assertIn(name, ALLOWED_TOOLS)
            self.assertIsNotNone(REGISTRY.spec(name))
        self.assertEqual(
            "boss_mitigation_sequence_check",
            _preferred_tool_route("Boss 临时减伤窗口和 Guard 吸收池怎么结算"),
        )
        self.assertEqual(
            "boss_mitigation_sequence_compare",
            _preferred_tool_route("两套 Guard 临时防御方案对比"),
        )

    @staticmethod
    def _build_stats() -> dict[str, float]:
        return {
            "LifeUnreserved": 15000, "EnergyShield": 3000,
            "ManaUnreserved": 1000, "Armour": 0,
            "FireResist": 75, "ColdResist": 75,
            "LightningResist": 75, "ChaosResist": 0,
            "NetLifeRegen": 100, "NetEnergyShieldRegen": 50,
            "NetManaRegen": 80, "EnergyShieldRecharge": 990,
        }

    def test_build_reads_snapshot_but_keeps_windows_explicit(self) -> None:
        inputs = self._inputs()
        result = boss_mitigation_sequence_from_build(
            PobBuild(player_stats=self._build_stats()),
            events=inputs["events"], dot_windows=inputs["dot_windows"],
            recovery_events=inputs["recovery_events"],
            mitigation_windows=inputs["mitigation_windows"], level=84,
            mana_before_life_pct=30,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(
            "explicit_request", result.data["source_config"]["mitigation_windows"]
        )
        self.assertEqual(75, result.data["source_fields"]["FireResist"])
        response = BuildBossMitigationSequenceOut(
            **result.data, citations=result.citations,
        )
        self.assertEqual(
            "build_temporary_mitigation_sequence", response.calculation_mode
        )

    def test_build_request_exposes_mitigation_contract(self) -> None:
        request = BuildRequest(code="abc", boss_mitigation_sequence={
            "events": [{"skill": "Test Slam", "at_seconds": 0}],
            "dot_windows": [{
                "name": "燃烧", "start_seconds": 0, "duration_seconds": 2,
                "damage_type": "Fire", "damage_basis": "taken",
                "dps_low": 100, "dps_high": 120,
            }],
            "recovery_events": [],
            "mitigation_windows": [{
                "name": "Guard", "start_seconds": 0, "duration_seconds": 2,
                "scope": "dot", "guard_capacity": 500, "priority": 10,
            }],
            "level": 84,
        })
        self.assertIsNotNone(request.boss_mitigation_sequence)
        assert request.boss_mitigation_sequence is not None
        window = request.boss_mitigation_sequence.mitigation_windows[0]
        self.assertEqual("dot", window.scope)
        self.assertEqual(500, window.guard_capacity)


if __name__ == "__main__":
    unittest.main()
