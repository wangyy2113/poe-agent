"""Boss Ward 破裂、恢复与命中吸收状态机的公式和表面契约。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api.boss import (
    BossDotWindow,
    BossLayeredEvent,
    BossWardCompareRequest,
    BossWardConfig,
    BossWardScenarioRequest,
    BossWardSequenceRequest,
    compare_ward_sequences as compare_ward_api,
    ward_sequence,
)
from api.build import BuildBossWardSequenceOut, BuildRequest
from calc import boss_ward, constants
from calc.boss_dot import DotTimelineConfig, DotWindow
from calc.boss_layered import LayeredDamageEvent
from calc.boss_mitigation import MitigationWindow
from calc.boss_recovery import RecoveryPoolConfig
from calc.boss_timeline import TimedDamageEvent
from calc.boss_ward import WardConfig
from core import db as dbmod
from orchestrator.loop import _preferred_tool_route
from parsers.pob_build import PobBuild
from tests.unit.test_boss_survival import _conn
from tools.impl.boss_ward import (
    boss_ward_sequence_check,
    boss_ward_sequence_compare,
    boss_ward_sequence_from_build,
)
from tools.registry import REGISTRY


def _dot(
    *, duration: float = 4, low: float = 0, high: float | None = None,
    damage_type: str = "Fire", bypass: float = 100,
) -> DotWindow:
    return DotWindow(
        name="显式 DoT", start_seconds=0, duration_seconds=duration,
        damage_type=damage_type, taken_dps_low=low,
        taken_dps_high=low if high is None else high,
        energy_shield_bypass_pct=bypass,
    )  # type: ignore[arg-type]


def _hit(
    at: float, low: float, *, high: float | None = None,
    bypass: float = 100, name: str = "重击",
) -> LayeredDamageEvent:
    upper = low if high is None else high
    return LayeredDamageEvent(
        energy_shield_bypass_pct=bypass,
        damage=TimedDamageEvent(
            at_seconds=at, skill=name, damage_type="Physical",
            raw_low=low, raw_high=upper, taken_low=low, taken_high=upper,
        ),
    )


def _ward(
    capacity: float = 40, *, delay: float = 2,
    initial: str = "ready", initial_restore: float | None = None,
    break_mode: str = "break", timer_mode: str = "preserve",
) -> WardConfig:
    return WardConfig(
        capacity=capacity, restore_delay_seconds=delay,
        initial_state=initial, initial_restore_at_seconds=initial_restore,
        break_mode=break_mode, broken_hit_timer_mode=timer_mode,
    )  # type: ignore[arg-type]


def _config(
    *, life: float = 300, energy_shield: float = 0,
    mana: float = 0, mom: float = 0,
) -> DotTimelineConfig:
    return DotTimelineConfig(pools=RecoveryPoolConfig(
        life=life, energy_shield=energy_shield, mana=mana,
        mana_before_life_pct=mom,
    ))


class BossWardFormulaTest(unittest.TestCase):
    def test_ward_breaks_and_restores_at_exact_hit_time(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0, 50), _hit(1, 50), _hit(2, 50)],
            dot_windows=[_dot(duration=3)], recovery_events=[],
            config=_config(), ward=_ward(),
        )
        steps = result.high_roll.hit_steps
        self.assertEqual([40, 0, 40], [row.ward_absorbed for row in steps])
        self.assertTrue(steps[2].restored_before_hit)
        self.assertEqual(2, result.high_roll.ward_state.break_count)
        self.assertEqual(1, result.high_roll.ward_state.restoration_count)
        self.assertEqual(
            ["break", "restore", "break"],
            [row.kind for row in result.high_roll.state_transitions],
        )
        self.assertEqual(2, result.high_roll.state_transitions[1].at_seconds)
        self.assertEqual(3, result.high_roll.state_transitions[1].hit_index)
        self.assertEqual(0, result.high_roll.ward_state.ready_uptime_pct)
        self.assertEqual(0, result.high_roll.ward_state.ready_uptime_seconds)
        self.assertEqual(3, result.high_roll.ward_state.broken_downtime_seconds)

    def test_hit_larger_than_capacity_passes_exact_residual(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0, 125)], dot_windows=[_dot(duration=1)],
            recovery_events=[], config=_config(), ward=_ward(capacity=40),
        )
        step = result.high_roll.hit_steps[0]
        self.assertEqual(40, step.ward_absorbed)
        self.assertEqual(85, step.damage_after_ward)
        self.assertEqual(85, result.high_roll.timeline.total_life_damage)

    def test_dot_skips_ward_and_does_not_break_it(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(3, 20)], dot_windows=[_dot(duration=3, low=30)],
            recovery_events=[], config=_config(), ward=_ward(capacity=50),
        )
        roll = result.high_roll
        self.assertEqual(20, roll.hit_steps[0].ward_absorbed)
        self.assertEqual(90, sum(
            segment.damage_after_guards
            for segment in roll.temporary_mitigation.dot_segments
        ))
        self.assertIn("DoT 跳过 Ward", result.ordering_model)

    def test_same_timestamp_uses_request_order_after_break(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(1, 50, name="第一击"), _hit(1, 50, name="第二击")],
            dot_windows=[_dot(duration=2)], recovery_events=[],
            config=_config(), ward=_ward(),
        )
        steps = result.high_roll.hit_steps
        self.assertEqual(40, steps[0].ward_absorbed)
        self.assertEqual(0, steps[1].ward_absorbed)
        self.assertEqual("第二击", steps[1].skill)

    def test_zero_delay_restores_between_same_timestamp_hits(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(1, 50), _hit(1, 50)],
            dot_windows=[_dot(duration=2)], recovery_events=[],
            config=_config(), ward=_ward(delay=0),
        )
        self.assertEqual(
            [40, 40], [row.ward_absorbed for row in result.high_roll.hit_steps]
        )

    def test_preserve_and_reset_broken_timer_diverge(self) -> None:
        common = dict(
            hit_events=[_hit(0, 50), _hit(1, 50), _hit(2.5, 50)],
            dot_windows=[_dot(duration=3)], recovery_events=[], config=_config(),
        )
        preserve = boss_ward.assess_ward_timeline(
            **common, ward=_ward(timer_mode="preserve"),
        )
        reset = boss_ward.assess_ward_timeline(
            **common, ward=_ward(timer_mode="reset"),
        )
        self.assertEqual(40, preserve.high_roll.hit_steps[2].ward_absorbed)
        self.assertEqual(0, reset.high_roll.hit_steps[2].ward_absorbed)
        self.assertEqual(2, reset.high_roll.ward_state.broken_timer_reset_count)
        self.assertEqual(
            ["break", "timer_reset", "timer_reset"],
            [row.kind for row in reset.high_roll.state_transitions],
        )

    def test_zero_residual_hit_does_not_reset_broken_timer(self) -> None:
        window = MitigationWindow(
            name="第二击 Guard", start_seconds=1, duration_seconds=0.5,
            scope="hit", guard_capacity=100,
        )
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0, 50), _hit(1, 50), _hit(2, 50)],
            dot_windows=[_dot(duration=3)], recovery_events=[],
            config=_config(), ward=_ward(delay=2, timer_mode="reset"),
            mitigation_windows=[window],
        )
        steps = result.high_roll.hit_steps
        self.assertEqual(0, steps[1].damage_before_ward)
        self.assertFalse(steps[1].broken_timer_reset)
        self.assertTrue(steps[2].restored_before_hit)

    def test_initial_broken_can_restore_by_timeline_end_without_later_hit(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0, 20)], dot_windows=[_dot(duration=2)],
            recovery_events=[], config=_config(),
            ward=_ward(initial="broken", initial_restore=1),
        )
        self.assertEqual(0, result.high_roll.ward_absorbed)
        self.assertEqual("ready", result.high_roll.ward_state.ending_state)
        self.assertEqual(1, result.high_roll.ward_state.restoration_count)
        final_restore = result.high_roll.state_transitions[-1]
        self.assertEqual("restore", final_restore.kind)
        self.assertIsNone(final_restore.hit_index)
        self.assertEqual("计划结束前到达恢复时点", final_restore.reason)

    def test_initial_broken_restores_at_explicit_absolute_time(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0.5, 30), _hit(1, 30)],
            dot_windows=[_dot(duration=2)], recovery_events=[],
            config=_config(), ward=_ward(initial="broken", initial_restore=1),
        )
        self.assertEqual(0, result.high_roll.hit_steps[0].ward_absorbed)
        self.assertEqual(30, result.high_roll.hit_steps[1].ward_absorbed)
        self.assertTrue(result.high_roll.hit_steps[1].restored_before_hit)

    def test_unbreakable_ward_absorbs_each_hit_without_timer(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0, 50), _hit(0.1, 50), _hit(0.2, 50)],
            dot_windows=[_dot(duration=1)], recovery_events=[],
            config=_config(), ward=_ward(break_mode="unbreakable"),
        )
        state = result.high_roll.ward_state
        self.assertEqual(120, result.high_roll.ward_absorbed)
        self.assertEqual(0, state.break_count)
        self.assertEqual("ready", state.ending_state)
        self.assertEqual(100, state.ready_uptime_pct)

    def test_multiplier_and_guard_run_before_ward(self) -> None:
        window = MitigationWindow(
            name="前置临时防御", start_seconds=0, duration_seconds=1,
            taken_multiplier=0.5, guard_capacity=20, priority=10,
        )
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0, 100)], dot_windows=[_dot(duration=1)],
            recovery_events=[], config=_config(), ward=_ward(capacity=25),
            mitigation_windows=[window],
        )
        step = result.high_roll.hit_steps[0]
        self.assertEqual(30, step.damage_before_ward)
        self.assertEqual(25, step.ward_absorbed)
        self.assertEqual(5, step.damage_after_ward)

    def test_expired_mitigation_window_does_not_reduce_hit_before_ward(self) -> None:
        window = MitigationWindow(
            name="结束边界", start_seconds=0, duration_seconds=1,
            scope="hit", taken_multiplier=0.5,
        )
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(1, 100)], dot_windows=[_dot(duration=1)],
            recovery_events=[], config=_config(), ward=_ward(capacity=25),
            mitigation_windows=[window],
        )
        step = result.high_roll.hit_steps[0]
        self.assertEqual(100, step.damage_before_ward)
        self.assertEqual(75, step.damage_after_ward)

    def test_low_and_high_tracks_can_have_different_ward_state(self) -> None:
        window = MitigationWindow(
            name="Guard", start_seconds=0, duration_seconds=1,
            guard_capacity=75,
        )
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0, 50, high=100)],
            dot_windows=[_dot(duration=1)], recovery_events=[],
            config=_config(), ward=_ward(capacity=50),
            mitigation_windows=[window],
        )
        self.assertEqual(0, result.low_roll.ward_absorbed)
        self.assertEqual("ready", result.low_roll.ward_state.ending_state)
        self.assertEqual(25, result.high_roll.ward_absorbed)
        self.assertEqual("broken", result.high_roll.ward_state.ending_state)

    def test_ward_runs_before_layered_pool_routing(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0, 100, bypass=0)], dot_windows=[_dot(duration=1)],
            recovery_events=[], config=_config(life=100, energy_shield=50),
            ward=_ward(capacity=40),
        )
        timeline = result.high_roll.timeline
        self.assertEqual(50, timeline.total_energy_shield_damage)
        self.assertEqual(10, timeline.total_life_damage)

    def test_ward_impact_compares_same_timeline_without_ward(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0, 120)], dot_windows=[_dot(duration=1)],
            recovery_events=[], config=_config(life=100), ward=_ward(capacity=40),
        )
        self.assertFalse(result.ward_impact.high_roll_survived_without_ward)
        self.assertTrue(result.high_roll.timeline.survived)
        self.assertTrue(result.ward_impact.changed_high_roll_survival)

    def test_threshold_uses_damage_after_ward(self) -> None:
        result = boss_ward.assess_ward_timeline(
            hit_events=[_hit(0, 100)], dot_windows=[_dot(duration=1)],
            recovery_events=[], config=_config(life=200), ward=_ward(capacity=40),
        )
        self.assertEqual(61, result.minimum_life_for_full_range.value)

    def test_comparison_keeps_survival_absorption_and_hits_separate(self) -> None:
        common = dict(
            hit_events=[_hit(0, 80), _hit(2, 80)],
            dot_windows=[_dot(duration=3)], recovery_events=[],
            config=_config(life=100),
        )
        weak = boss_ward.assess_ward_timeline(
            **common, ward=_ward(capacity=20, delay=3),
        )
        strong = boss_ward.assess_ward_timeline(
            **common, ward=_ward(capacity=50, delay=1),
        )
        compared = boss_ward.compare_ward_timelines([
            ("弱 Ward", weak), ("强 Ward", strong),
        ])
        self.assertEqual("强 Ward", compared.best_survival_scenario)
        self.assertEqual("强 Ward", compared.highest_ward_absorption_scenario)
        self.assertEqual("强 Ward", compared.highest_ward_uptime_scenario)
        self.assertEqual("强 Ward", compared.most_protected_hits_scenario)

    def test_validation_rejects_ambiguous_initial_and_unbreakable_states(self) -> None:
        common = dict(
            hit_events=[_hit(0, 10)], dot_windows=[_dot(duration=1)],
            recovery_events=[], config=_config(),
        )
        with self.assertRaisesRegex(boss_ward.BossWardError, "必须提供"):
            boss_ward.assess_ward_timeline(
                **common, ward=_ward(initial="broken"),
            )
        with self.assertRaisesRegex(boss_ward.BossWardError, "不能提供"):
            boss_ward.assess_ward_timeline(
                **common, ward=_ward(initial_restore=0.5),
            )
        with self.assertRaisesRegex(boss_ward.BossWardError, "必须从 ready"):
            boss_ward.assess_ward_timeline(
                **common,
                ward=_ward(
                    initial="broken", initial_restore=0.5,
                    break_mode="unbreakable",
                ),
            )

    def test_validation_rejects_restore_after_timeline_and_missing_hits(self) -> None:
        with self.assertRaisesRegex(boss_ward.BossWardError, "不能超出"):
            boss_ward.assess_ward_timeline(
                hit_events=[_hit(0, 10)], dot_windows=[_dot(duration=1)],
                recovery_events=[], config=_config(),
                ward=_ward(initial="broken", initial_restore=2),
            )
        with self.assertRaisesRegex(boss_ward.BossWardError, "至少需要一次"):
            boss_ward.assess_ward_timeline(
                hit_events=[], dot_windows=[_dot(duration=1)],
                recovery_events=[], config=_config(), ward=_ward(),
            )


class BossWardSurfaceTest(unittest.TestCase):
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
    def _inputs(*, capacity: float = 1000, delay: float = 2) -> dict[str, object]:
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
            "recovery_events": [], "mitigation_windows": [],
            "ward": {
                "capacity": capacity, "restore_delay_seconds": delay,
                "initial_state": "ready", "break_mode": "break",
                "broken_hit_timer_mode": "preserve",
            },
            "level": 84, "life": 15000, "energy_shield": 3000,
            "mana": 1000, "mana_before_life_pct": 30, "armour": 0,
            "fire_resist_pct": 75, "life_recovery_per_second": 100,
            "energy_shield_recovery_per_second": 50,
            "mana_recovery_per_second": 80,
            "energy_shield_recharge_per_second": 990,
        }

    def test_tool_returns_ward_audit_and_without_ward_baseline(self) -> None:
        result = boss_ward_sequence_check(**self._inputs())
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertGreater(result.data["high_roll"]["ward_absorbed"], 0)
        self.assertEqual(2, result.data["high_roll"]["ward_state"]["protected_hit_count"])
        self.assertIn("Boss Ward 破裂与恢复时间轴", result.data["answer"])
        self.assertIn("Ward 状态转换", result.data["answer"])
        self.assertIn("DoT", result.data["boundary"])
        self.assertEqual(2, len(result.citations))

    def test_tool_allows_omitting_optional_mitigation_windows(self) -> None:
        inputs = self._inputs()
        inputs.pop("mitigation_windows")
        result = boss_ward_sequence_check(**inputs)
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(0, result.data["mitigation_window_count"])

    def test_unknown_dot_resistance_preserves_ward_request(self) -> None:
        inputs = self._inputs()
        inputs.pop("fire_resist_pct")
        result = boss_ward_sequence_check(**inputs)
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertEqual(1000, result.data["requested_ward"]["capacity"])
        self.assertNotIn("high_roll", result.data)

    def test_tool_validation_rejects_unknown_and_ambiguous_ward(self) -> None:
        inputs = self._inputs()
        inputs["ward"] = {
            "capacity": 100, "restore_delay_seconds": 2, "cooldown": 4,
        }
        unknown = boss_ward_sequence_check(**inputs)
        self.assertFalse(unknown.ok)
        inputs["ward"] = {
            "capacity": 100, "restore_delay_seconds": 2,
            "initial_state": "broken",
        }
        ambiguous = boss_ward_sequence_check(**inputs)
        self.assertFalse(ambiguous.ok)
        self.assertIn("必须提供", ambiguous.error)

    def test_compare_tool_keeps_absorption_and_protected_hits(self) -> None:
        result = boss_ward_sequence_compare(scenarios=[
            {"name": "慢恢复", "inputs": self._inputs(capacity=500, delay=5)},
            {"name": "快恢复", "inputs": self._inputs(capacity=1000, delay=1)},
        ])
        self.assertTrue(result.ok)
        self.assertEqual("快恢复", result.data["highest_ward_absorption_scenario"])
        self.assertEqual("快恢复", result.data["highest_ward_uptime_scenario"])
        self.assertEqual("快恢复", result.data["most_protected_hits_scenario"])
        self.assertIn("Ward 状态机方案比较", result.data["answer"])

    def test_compare_tool_rejects_duplicate_scenario_names(self) -> None:
        result = boss_ward_sequence_compare(scenarios=[
            {"name": "重复", "inputs": self._inputs()},
            {"name": "重复", "inputs": self._inputs(capacity=2000)},
        ])
        self.assertFalse(result.ok)
        self.assertIn("不重复", result.error)

    def test_http_endpoints_and_openapi_contract(self) -> None:
        request = BossWardSequenceRequest(
            events=[BossLayeredEvent(skill="Test Slam", at_seconds=0)],
            dot_windows=[BossDotWindow(
                name="燃烧", start_seconds=0, duration_seconds=2,
                damage_type="Fire", damage_basis="raw", dps_low=100, dps_high=120,
            )], recovery_events=[], mitigation_windows=[],
            ward=BossWardConfig(capacity=500, restore_delay_seconds=2),
            level=84, life=15000, fire_resist_pct=75,
        )
        response = ward_sequence(request, _caller="test")
        self.assertEqual(500, response.ward["capacity"])
        compared = compare_ward_api(BossWardCompareRequest(scenarios=[
            BossWardScenarioRequest(name="A", inputs=request),
            BossWardScenarioRequest(name="B", inputs=request.model_copy(update={
                "ward": request.ward.model_copy(update={"capacity": 800}),
            })),
        ]), _caller="test")
        self.assertEqual(2, len(compared["scenarios"]))
        from main import create_app

        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/boss/ward-sequence", paths)
        self.assertIn("/poe_agent/boss/ward-sequence/compare", paths)

    def test_registry_knowledge_and_routes_are_wired(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS

        for name in ("boss_ward_sequence_check", "boss_ward_sequence_compare"):
            self.assertIn(name, ALLOWED_TOOLS)
            self.assertIsNotNone(REGISTRY.spec(name))
        self.assertEqual(
            "boss_ward_sequence_check",
            _preferred_tool_route("Ward 破裂时间轴和恢复怎么结算"),
        )
        self.assertEqual(
            "boss_ward_sequence_compare",
            _preferred_tool_route("两套 Ward 状态机方案对比"),
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
            "Ward": 9999,
        }

    def test_build_reads_base_snapshot_but_not_pob_ward_stat(self) -> None:
        inputs = self._inputs(capacity=750)
        result = boss_ward_sequence_from_build(
            PobBuild(player_stats=self._build_stats()),
            events=inputs["events"], dot_windows=inputs["dot_windows"],
            recovery_events=inputs["recovery_events"],
            mitigation_windows=inputs["mitigation_windows"], ward=inputs["ward"],
            level=84, mana_before_life_pct=30,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(
            "explicit_request_not_pob_ward_stat",
            result.data["source_config"]["ward"],
        )
        self.assertNotIn("Ward", result.data["source_fields"])
        response = BuildBossWardSequenceOut(
            **result.data, citations=result.citations,
        )
        self.assertEqual("build_ward_sequence", response.calculation_mode)

    def test_build_unknown_path_preserves_explicit_ward_request(self) -> None:
        inputs = self._inputs(capacity=750)
        result = boss_ward_sequence_from_build(
            PobBuild(player_stats={}), events=inputs["events"],
            dot_windows=inputs["dot_windows"],
            recovery_events=inputs["recovery_events"], ward=inputs["ward"],
            mitigation_windows=[], level=84,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertEqual(750, result.data["requested_ward"]["capacity"])
        self.assertEqual(1, result.data["dot_window_count"])
        self.assertEqual("显式燃烧地面", result.data["requested_dot_windows"][0]["name"])
        self.assertEqual([], result.data["requested_recovery_events"])
        self.assertEqual("unknown", result.data["calculation_mode"])

    def test_build_request_exposes_explicit_ward_contract(self) -> None:
        request = BuildRequest(code="abc", boss_ward_sequence={
            "events": [{"skill": "Test Slam", "at_seconds": 0}],
            "dot_windows": [{
                "name": "燃烧", "start_seconds": 0, "duration_seconds": 2,
                "damage_type": "Fire", "damage_basis": "taken",
                "dps_low": 100, "dps_high": 120,
            }],
            "recovery_events": [], "mitigation_windows": [],
            "ward": {
                "capacity": 500, "restore_delay_seconds": 2,
                "initial_state": "broken", "initial_restore_at_seconds": 1,
                "broken_hit_timer_mode": "reset",
            },
            "level": 84,
        })
        self.assertIsNotNone(request.boss_ward_sequence)
        assert request.boss_ward_sequence is not None
        self.assertEqual("broken", request.boss_ward_sequence.ward.initial_state)
        self.assertEqual(1, request.boss_ward_sequence.ward.initial_restore_at_seconds)


if __name__ == "__main__":
    unittest.main()
