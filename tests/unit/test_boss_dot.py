"""Boss 显式 DoT 暴露窗口的公式、边界与表面契约测试。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api.boss import (
    BossDotCompareRequest,
    BossDotScenarioRequest,
    BossDotSequenceRequest,
    BossDotWindow,
    BossLayeredEvent,
    compare_dot_sequences as compare_dot_api,
    dot_sequence,
)
from api.build import BuildBossDotSequenceOut, BuildRequest

from calc import boss_dot, constants
from calc.boss_dot import DotTimelineConfig, DotWindow
from calc.boss_layered import LayeredDamageEvent
from calc.boss_recovery import ActiveRecoveryEvent, RecoveryPoolConfig
from calc.boss_timeline import TimedDamageEvent
from core import db as dbmod
from orchestrator.loop import _preferred_tool_route
from parsers.pob_build import PobBuild
from tests.unit.test_boss_survival import _conn
from tools.impl.boss_dot import (
    boss_dot_sequence_check,
    boss_dot_sequence_compare,
    boss_dot_sequence_from_build,
)
from tools.registry import REGISTRY


def _dot(
    name: str = "燃烧地面", *, start: float = 0, duration: float = 2,
    low: float = 10, high: float | None = None, damage_type: str = "Fire",
    bypass: float = 0,
) -> DotWindow:
    return DotWindow(
        name=name, start_seconds=start, duration_seconds=duration,
        damage_type=damage_type, taken_dps_low=low,
        taken_dps_high=low if high is None else high,
        energy_shield_bypass_pct=bypass,
    )  # type: ignore[arg-type]


def _hit(
    at: float, damage: float, *, high: float | None = None, bypass: float = 0,
    name: str = "重击",
) -> LayeredDamageEvent:
    upper = damage if high is None else high
    return LayeredDamageEvent(
        energy_shield_bypass_pct=bypass,
        damage=TimedDamageEvent(
            at_seconds=at, skill=name, damage_type="Physical",
            raw_low=damage, raw_high=upper, taken_low=damage, taken_high=upper,
        ),
    )


def _config(
    *, life: float = 100, energy_shield: float = 0, mana: float = 0,
    mom: float = 0, life_recovery: float = 0, es_recovery: float = 0,
    mana_recovery: float = 0, recharge: float = 0, recharge_delay: float = 2,
    dot_interrupt: str = "energy_shield_damage",
) -> DotTimelineConfig:
    return DotTimelineConfig(
        pools=RecoveryPoolConfig(
            life=life, energy_shield=energy_shield, mana=mana,
            mana_before_life_pct=mom,
            life_recovery_per_second=life_recovery,
            energy_shield_recovery_per_second=es_recovery,
            mana_recovery_per_second=mana_recovery,
            energy_shield_recharge_per_second=recharge,
            energy_shield_recharge_delay_seconds=recharge_delay,
        ),
        dot_recharge_interrupt_mode=dot_interrupt,  # type: ignore[arg-type]
    )


class BossDotFormulaTest(unittest.TestCase):
    def test_dot_only_timeline_integrates_exact_damage(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[_dot(duration=3, low=12)],
            recovery_events=[], config=_config(life=100),
        )
        self.assertTrue(result.high_roll.survived)
        self.assertEqual(36, result.high_roll.total_dot_damage)
        self.assertEqual(64, result.high_roll.ending_life)
        self.assertEqual(36, result.high_roll.dot_sources[0].processed_damage)

    def test_exact_depletion_time_is_not_sampled(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[_dot(duration=10, low=30)],
            recovery_events=[], config=_config(life=100),
        )
        self.assertFalse(result.high_roll.survived)
        self.assertAlmostEqual(
            100 / 30, result.high_roll.depletion_time_seconds or 0, places=6,
        )
        self.assertEqual("dot", result.high_roll.depletion_cause)
        self.assertAlmostEqual(100, result.high_roll.total_dot_damage)

    def test_overlapping_sources_sum_and_keep_source_attribution(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[
                _dot("地面", duration=3, low=10),
                _dot("减益", start=1, duration=2, low=20),
            ], recovery_events=[], config=_config(life=200),
        )
        self.assertEqual(70, result.high_roll.total_dot_damage)
        self.assertEqual([30, 40], [row.processed_damage for row in result.high_roll.dot_sources])
        self.assertEqual(("地面", "减益"), result.high_roll.segments[-1].active_dot_sources)

    def test_low_and_high_dot_tracks_can_change_outcome(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[_dot(duration=2, low=40, high=60)],
            recovery_events=[], config=_config(life=100),
        )
        self.assertEqual("roll_dependent", result.outcome)
        self.assertTrue(result.low_roll.survived)
        self.assertFalse(result.high_roll.survived)

    def test_dot_bypass_routes_around_energy_shield(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[_dot(duration=2, low=40, bypass=50)],
            recovery_events=[], config=_config(life=100, energy_shield=100),
        )
        roll = result.high_roll
        self.assertEqual(40, roll.total_energy_shield_damage)
        self.assertEqual(40, roll.total_life_damage)
        self.assertEqual(60, roll.ending_life)
        self.assertEqual(60, roll.ending_energy_shield)

    def test_mana_before_life_routes_dot_and_spills_after_mana_depletion(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[_dot(duration=2, low=50, bypass=100)],
            recovery_events=[], config=_config(life=100, mana=20, mom=40),
        )
        roll = result.high_roll
        self.assertEqual(20, roll.total_mana_damage)
        self.assertEqual(80, roll.total_life_damage)
        self.assertEqual(20, roll.ending_life)

    def test_continuous_recovery_offsets_dot_concurrently(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[_dot(duration=5, low=20)],
            recovery_events=[], config=_config(life=100, life_recovery=8),
        )
        roll = result.high_roll
        self.assertEqual(40, roll.total_recovery_applied)
        self.assertEqual(40, roll.ending_life)
        self.assertEqual(100, roll.total_dot_damage)

    def test_full_pool_recovery_offsets_concurrent_damage_without_false_overflow(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[_dot(duration=2, low=5)],
            recovery_events=[], config=_config(life=100, life_recovery=10),
        )
        roll = result.high_roll
        self.assertEqual(100, roll.ending_life)
        self.assertEqual(10, roll.total_recovery_applied)
        self.assertEqual(10, roll.total_recovery_overflow)

    def test_timed_recovery_and_leech_cap_share_continuous_flow(self) -> None:
        events = [
            ActiveRecoveryEvent(
                "药剂", 0, "life", "timed_rate", rate_per_second=5,
                duration_seconds=2,
            ),
            ActiveRecoveryEvent(
                "偷取", 0, "life", "leech", rate_per_second=50,
                duration_seconds=2,
            ),
        ]
        result = boss_dot.assess_dot_timeline(
            hit_events=[_hit(0, 50)], dot_windows=[_dot(duration=2, low=40)],
            recovery_events=events, config=_config(life=100),
        )
        roll = result.high_roll
        self.assertEqual(50, roll.total_recovery_applied)
        self.assertEqual(60, roll.total_leech_suppressed_by_rate_cap)
        self.assertEqual(20, roll.ending_life)

    def test_hit_dot_and_instant_recovery_have_explicit_ordering(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[_hit(1, 50)], dot_windows=[_dot(duration=2, low=20)],
            recovery_events=[ActiveRecoveryEvent(
                "瞬回", 1, "life", "instant", amount=30, phase="before_hit",
            )], config=_config(life=100),
        )
        roll = result.high_roll
        self.assertEqual(20, roll.instant_recoveries[0].applied)
        self.assertEqual(30, roll.ending_life)
        self.assertEqual(1, len(roll.hits))

    def test_after_hit_instant_cannot_rescue_lethal_hit(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[_hit(1, 90)], dot_windows=[_dot(duration=2, low=20)],
            recovery_events=[ActiveRecoveryEvent(
                "命中后", 1, "life", "instant", amount=100, phase="after_hit",
            )], config=_config(life=100),
        )
        self.assertFalse(result.high_roll.survived)
        self.assertEqual("hit", result.high_roll.depletion_cause)
        self.assertTrue(result.high_roll.instant_recoveries[0].skipped_because_depleted)

    def test_dot_interrupt_mode_blocks_recharge_and_records_policy(self) -> None:
        base = dict(
            hit_events=[_hit(0, 50)],
            dot_windows=[_dot(start=2, duration=2, low=10)], recovery_events=[],
        )
        interrupted = boss_dot.assess_dot_timeline(
            **base, config=_config(
                life=100, energy_shield=100, recharge=50, recharge_delay=1,
                dot_interrupt="energy_shield_damage",
            ),
        )
        ignored = boss_dot.assess_dot_timeline(
            **base, config=_config(
                life=100, energy_shield=100, recharge=50, recharge_delay=1,
                dot_interrupt="does_not_interrupt",
            ),
        )
        self.assertLess(
            interrupted.high_roll.ending_energy_shield,
            ignored.high_roll.ending_energy_shield,
        )
        self.assertTrue(interrupted.high_roll.segments[-1].recharge_interrupted_by_dot)
        zero_low = boss_dot.assess_dot_timeline(
            hit_events=[_hit(0, 50)],
            dot_windows=[_dot(start=2, duration=2, low=0, high=10)],
            recovery_events=[], config=_config(
                life=100, energy_shield=100, recharge=50, recharge_delay=1,
                dot_interrupt="energy_shield_damage",
            ),
        )
        self.assertFalse(zero_low.low_roll.segments[-1].recharge_interrupted_by_dot)

    def test_dot_impact_compares_same_timeline_without_dot(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[_hit(1, 70)], dot_windows=[_dot(duration=2, low=20)],
            recovery_events=[], config=_config(life=100),
        )
        self.assertTrue(result.dot_impact.high_roll_survived_without_dot)
        self.assertFalse(result.high_roll.survived)
        self.assertTrue(result.dot_impact.changed_high_roll_survival)

    def test_single_axis_thresholds_are_resolved_with_other_layers_fixed(self) -> None:
        result = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[_dot(duration=2, low=50, bypass=100)],
            recovery_events=[], config=_config(life=150),
        )
        threshold = result.minimum_life_for_full_range
        self.assertEqual("reachable", threshold.status)
        self.assertEqual(101, threshold.value)
        self.assertTrue(threshold.keeps_other_layers_fixed)

    def test_comparison_keeps_survival_time_damage_and_threshold_independent(self) -> None:
        weak = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[_dot(duration=3, low=40)],
            recovery_events=[], config=_config(life=100),
        )
        strong = boss_dot.assess_dot_timeline(
            hit_events=[], dot_windows=[_dot(duration=3, low=20)],
            recovery_events=[], config=_config(life=100),
        )
        compared = boss_dot.compare_dot_timelines([("高暴露", weak), ("低暴露", strong)])
        self.assertEqual("低暴露", compared.best_survival_scenario)
        self.assertEqual("低暴露", compared.longest_time_to_depletion_scenario)
        self.assertEqual("低暴露", compared.lowest_dot_damage_scenario)

    def test_validation_rejects_duplicate_sources_and_ambiguous_recovery(self) -> None:
        with self.assertRaisesRegex(boss_dot.BossDotError, "必须唯一"):
            boss_dot.assess_dot_timeline(
                hit_events=[], dot_windows=[_dot("A"), _dot("A")],
                recovery_events=[], config=_config(),
            )
        with self.assertRaisesRegex(boss_dot.BossDotError, "瞬回形状"):
            boss_dot.assess_dot_timeline(
                hit_events=[], dot_windows=[_dot()], recovery_events=[
                    ActiveRecoveryEvent(
                        "错", 0, "life", "instant", amount=10,
                        rate_per_second=1,
                    )
                ], config=_config(),
            )


class BossDotSurfaceTest(unittest.TestCase):
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
    def _inputs(*, dps: float = 400) -> dict[str, object]:
        return {
            "events": [
                {"skill": "Test Slam", "at_seconds": 0},
                {"skill": "Test Slam", "at_seconds": 3,
                 "energy_shield_bypass_pct": 20},
            ],
            "dot_windows": [{
                "name": "显式燃烧地面", "start_seconds": 0.5,
                "duration_seconds": 2, "damage_type": "Fire",
                "damage_basis": "raw", "dps_low": dps, "dps_high": dps * 1.2,
                "energy_shield_bypass_pct": 10,
            }],
            "recovery_events": [{
                "name": "生命药剂", "at_seconds": 1, "pool": "life",
                "kind": "instant", "amount": 500,
            }],
            "level": 84, "life": 15000, "energy_shield": 3000,
            "mana": 1000, "mana_before_life_pct": 30, "armour": 0,
            "fire_resist_pct": 75, "life_recovery_per_second": 100,
            "energy_shield_recovery_per_second": 50,
            "mana_recovery_per_second": 80,
            "energy_shield_recharge_per_second": 990,
        }

    def test_tool_returns_resolved_dot_audit_segments_and_impact(self) -> None:
        result = boss_dot_sequence_check(**self._inputs())
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(100, result.data["resolved_dot_windows"][0]["taken_dps_low"])
        self.assertGreater(result.data["high_roll"]["total_dot_damage"], 0)
        self.assertTrue(result.data["high_roll"]["segments"])
        self.assertIn("Boss DoT 暴露混合时间轴", result.data["answer"])
        self.assertIn("物理 DoT 不使用护甲", result.data["boundary"])
        self.assertEqual(2, len(result.citations))

    def test_taken_basis_does_not_require_resistance(self) -> None:
        inputs = self._inputs()
        window = dict(inputs["dot_windows"][0])  # type: ignore[index]
        window["damage_basis"] = "taken"
        inputs["dot_windows"] = [window]
        inputs.pop("fire_resist_pct")
        result = boss_dot_sequence_check(**inputs)
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(400, result.data["resolved_dot_windows"][0]["taken_dps_low"])

    def test_raw_basis_missing_resistance_stays_unknown(self) -> None:
        inputs = self._inputs()
        inputs.pop("fire_resist_pct")
        result = boss_dot_sequence_check(**inputs)
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertIn("火焰抗性", result.data["missing_inputs"])
        self.assertNotIn("high_roll", result.data)

    def test_dot_only_tool_does_not_invent_boss_citations(self) -> None:
        inputs = self._inputs()
        inputs["events"] = []
        window = dict(inputs["dot_windows"][0])  # type: ignore[index]
        window["damage_basis"] = "taken"
        inputs["dot_windows"] = [window]
        result = boss_dot_sequence_check(**inputs)
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual([], result.citations)

    def test_compare_tool_returns_independent_best_metrics(self) -> None:
        result = boss_dot_sequence_compare(scenarios=[
            {"name": "高暴露", "inputs": self._inputs(dps=800)},
            {"name": "低暴露", "inputs": self._inputs(dps=100)},
        ])
        self.assertTrue(result.ok)
        self.assertEqual("低暴露", result.data["lowest_dot_damage_scenario"])
        self.assertEqual(2, len(result.data["scenarios"]))
        self.assertIn("DoT 暴露方案比较", result.data["answer"])

    def test_http_endpoints_and_openapi_contract(self) -> None:
        request = BossDotSequenceRequest(
            events=[BossLayeredEvent(skill="Test Slam", at_seconds=0)],
            dot_windows=[BossDotWindow(
                name="燃烧", start_seconds=0, duration_seconds=2,
                damage_type="Fire", damage_basis="raw", dps_low=100, dps_high=120,
            )], recovery_events=[], level=84, life=15000, fire_resist_pct=75,
        )
        response = dot_sequence(request, _caller="test")
        self.assertEqual(1, response.hit_count)
        self.assertIsNotNone(response.high_roll)
        compared = compare_dot_api(BossDotCompareRequest(scenarios=[
            BossDotScenarioRequest(name="A", inputs=request),
            BossDotScenarioRequest(name="B", inputs=request.model_copy(update={
                "dot_windows": [request.dot_windows[0].model_copy(update={"dps_high": 200})],
            })),
        ]), _caller="test")
        self.assertEqual(2, len(compared["scenarios"]))
        from main import create_app

        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/boss/dot-sequence", paths)
        self.assertIn("/poe_agent/boss/dot-sequence/compare", paths)

    def test_registry_knowledge_and_routes_are_wired(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS

        for name in ("boss_dot_sequence_check", "boss_dot_sequence_compare"):
            self.assertIn(name, ALLOWED_TOOLS)
            self.assertIsNotNone(REGISTRY.spec(name))
        self.assertEqual(
            "boss_dot_sequence_check",
            _preferred_tool_route("Boss DoT 暴露窗口和恢复一起怎么算"),
        )
        self.assertEqual(
            "boss_dot_sequence_compare",
            _preferred_tool_route("两套 DoT 暴露方案对比"),
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

    def test_build_reads_snapshot_but_keeps_dot_windows_explicit(self) -> None:
        inputs = self._inputs()
        result = boss_dot_sequence_from_build(
            PobBuild(player_stats=self._build_stats()),
            events=inputs["events"], dot_windows=inputs["dot_windows"],
            recovery_events=inputs["recovery_events"], level=84,
            mana_before_life_pct=30,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(
            "explicit_request", result.data["source_config"]["dot_windows"]
        )
        self.assertEqual(
            "pob_player_stat_and_explicit_dot_windows", result.data["input_source"]
        )
        self.assertEqual(75, result.data["source_fields"]["FireResist"])
        self.assertEqual(
            "pob_player_stat:FireResist",
            result.data["source_config"]["dot_fire_resist_pct"],
        )
        response = BuildBossDotSequenceOut(**result.data, citations=result.citations)
        self.assertEqual("build_dot_exposure_sequence", response.calculation_mode)

    def test_build_request_exposes_dot_contract(self) -> None:
        request = BuildRequest(code="abc", boss_dot_sequence={
            "events": [{"skill": "Test Slam", "at_seconds": 0}],
            "dot_windows": [{
                "name": "燃烧", "start_seconds": 0, "duration_seconds": 2,
                "damage_type": "Fire", "damage_basis": "taken",
                "dps_low": 100, "dps_high": 120,
            }],
            "recovery_events": [], "level": 84,
            "dot_recharge_interrupt_mode": "any_damage",
        })
        self.assertIsNotNone(request.boss_dot_sequence)
        assert request.boss_dot_sequence is not None
        self.assertEqual("any_damage", request.boss_dot_sequence.dot_recharge_interrupt_mode)


if __name__ == "__main__":
    unittest.main()
