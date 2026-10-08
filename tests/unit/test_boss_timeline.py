"""Boss 定时命中、持续恢复、门槛反解、比较与 HTTP/工具契约。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from api.boss import (
    BossTimedCompareRequest,
    BossTimedEvent,
    BossTimedScenarioRequest,
    BossTimedSequenceRequest,
    compare_timed_sequences as compare_timed_sequences_api,
    timed_sequence,
)
from api.build import (
    BuildBossTimedSequenceOut,
    BuildRequest,
)
from calc import boss_timeline, constants
from calc.boss_timeline import TimedDamageEvent
from core import db as dbmod
from orchestrator.loop import _preferred_tool_route
from parsers.pob_build import PobBuild
from tests.unit.test_boss_survival import _conn
from tools.impl.boss_timeline import (
    boss_timed_sequence_check,
    boss_timed_sequence_compare,
    boss_timed_sequence_from_build,
)
from tools.registry import REGISTRY


def _event(
    at: float, low: float = 60, high: float | None = None, name: str = "命中",
) -> TimedDamageEvent:
    upper = low if high is None else high
    return TimedDamageEvent(
        at_seconds=at, skill=name, damage_type="Physical",
        raw_low=low, raw_high=upper, taken_low=low, taken_high=upper,
    )


class BossTimedSequenceFormulaTest(unittest.TestCase):
    def test_recovery_is_capped_and_applied_only_between_hits(self) -> None:
        result = boss_timeline.assess_timed_sequence(
            events=[_event(0), _event(2)], pool=100, recovery_per_second=20,
        )
        self.assertEqual("survives_full_range", result.outcome)
        self.assertEqual(40, result.total_recovered_worst_case)
        self.assertEqual(20, result.ending_remaining_worst_case)
        self.assertEqual(81, result.minimum_pool_for_full_range)
        self.assertAlmostEqual(10, result.required_recovery_for_full_range or 0)
        self.assertTrue(result.required_recovery_for_full_range_exclusive)
        self.assertEqual("reachable", result.required_recovery_for_full_range_status)
        self.assertEqual(2, len(result.steps))
        self.assertEqual(0, result.steps[0].best_case_recovered)
        self.assertEqual(40, result.steps[1].worst_case_recovered)

    def test_recovery_delay_restarts_after_each_hit(self) -> None:
        result = boss_timeline.assess_timed_sequence(
            events=[_event(0), _event(2), _event(4)], pool=140,
            recovery_per_second=20, recovery_delay_seconds=1,
        )
        self.assertEqual([0, 1, 1], [step.recovery_eligible_seconds for step in result.steps])
        self.assertEqual(40, result.total_recovered_worst_case)
        self.assertEqual(0, result.ending_remaining_worst_case)
        self.assertEqual("fails_full_range", result.outcome)
        self.assertEqual(3, result.guaranteed_depletion_hit)

    def test_low_and_high_roll_paths_produce_roll_dependent_result(self) -> None:
        result = boss_timeline.assess_timed_sequence(
            events=[_event(0, 30, 70), _event(1, 30, 70)], pool=100,
        )
        self.assertEqual("roll_dependent", result.outcome)
        self.assertEqual(40, result.ending_remaining_best_case)
        self.assertEqual(-40, result.ending_remaining_worst_case)
        self.assertIsNone(result.guaranteed_depletion_hit)
        self.assertEqual(2, result.earliest_possible_depletion_hit)

    def test_simultaneous_lethal_group_cannot_be_solved_by_recovery(self) -> None:
        result = boss_timeline.assess_timed_sequence(
            events=[_event(1), _event(1)], pool=100, recovery_per_second=999999,
        )
        self.assertEqual("unreachable", result.required_recovery_for_full_range_status)
        self.assertIsNone(result.required_recovery_for_full_range)
        self.assertEqual(0, result.steps[1].recovery_eligible_seconds)

    def test_exact_zero_is_depletion_and_cannot_recover_later(self) -> None:
        result = boss_timeline.assess_timed_sequence(
            events=[_event(0, 100), _event(10, 1)], pool=100,
            recovery_per_second=100,
        )
        self.assertEqual("fails_full_range", result.outcome)
        self.assertEqual(1, result.guaranteed_depletion_hit)
        self.assertEqual(0, result.total_recovered_best_case)
        self.assertTrue(result.steps[1].best_case_depleted)

    def test_input_validation_rejects_reordered_or_invalid_events(self) -> None:
        with self.assertRaisesRegex(boss_timeline.BossTimelineError, "非递减"):
            boss_timeline.assess_timed_sequence(
                events=[_event(2), _event(1)], pool=100,
            )
        with self.assertRaisesRegex(boss_timeline.BossTimelineError, "pool 必须大于 0"):
            boss_timeline.assess_timed_sequence(events=[_event(0)], pool=0)
        with self.assertRaisesRegex(boss_timeline.BossTimelineError, "必须是数字"):
            boss_timeline.assess_timed_sequence(
                events=[_event(0)], pool=100, recovery_per_second=True,
            )

    def test_comparison_ranks_survival_margin_pool_and_recovery(self) -> None:
        weak = boss_timeline.assess_timed_sequence(
            events=[_event(0), _event(2)], pool=100,
        )
        strong = boss_timeline.assess_timed_sequence(
            events=[_event(0), _event(2)], pool=100, recovery_per_second=20,
        )
        comparison = boss_timeline.compare_timed_sequences([
            ("无恢复", weak), ("持续恢复", strong),
        ])
        self.assertEqual("持续恢复", comparison.best_survival_scenario)
        self.assertEqual("持续恢复", comparison.best_worst_case_margin_scenario)
        self.assertEqual("持续恢复", comparison.lowest_required_pool_scenario)
        self.assertEqual("无恢复", comparison.lowest_required_recovery_scenario)


class BossTimedSequenceSurfaceTest(unittest.TestCase):
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
    def _inputs(recovery: float = 0) -> dict[str, object]:
        return {
            "events": [
                {"skill": "Test Slam", "at_seconds": 0},
                {"skill": "Test Slam", "at_seconds": 2},
            ],
            "level": 84, "pool": 15000, "armour": 0,
            "recovery_per_second": recovery,
        }

    def test_tool_reuses_boss_damage_chain_and_returns_recovery_thresholds(self) -> None:
        result = boss_timed_sequence_check(**self._inputs(6000))
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(2, result.data["hit_count"])
        self.assertEqual("survives_full_range", result.data["outcome"])
        self.assertGreater(result.data["total_recovered_worst_case"], 0)
        self.assertGreater(result.data["minimum_pool_for_full_range"], 0)
        self.assertIn("恢复时间轴", result.data["answer"])
        self.assertIn("不是游戏真实连招", result.data["boundary"])
        self.assertEqual(2, len(result.citations))

    def test_tool_preserves_unknown_when_damage_input_is_not_safe(self) -> None:
        result = boss_timed_sequence_check(
            events=[{"skill": "Shaper Ball", "at_seconds": 0}],
            level=84, pool=10000,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertEqual(["冰霜抗性"], result.data["missing_inputs"])
        self.assertIn("定时时间轴边界", result.data["answer"])

    def test_tool_rejects_unknown_fields_and_reordered_time(self) -> None:
        unknown = boss_timed_sequence_check(
            events=[{"skill": "Test Slam", "at_seconds": 0, "count": 2}],
            level=84, pool=10000,
        )
        self.assertFalse(unknown.ok)
        reordered = boss_timed_sequence_check(
            events=[
                {"skill": "Test Slam", "at_seconds": 2},
                {"skill": "Test Slam", "at_seconds": 1},
            ],
            level=84, pool=10000,
        )
        self.assertFalse(reordered.ok)
        self.assertIn("非递减", reordered.error)

    def test_compare_tool_returns_best_scenarios(self) -> None:
        result = boss_timed_sequence_compare(scenarios=[
            {"name": "无恢复", "inputs": self._inputs(0)},
            {"name": "高恢复", "inputs": self._inputs(6000)},
        ])
        self.assertTrue(result.ok)
        self.assertEqual("高恢复", result.data["best_survival_scenario"])
        self.assertEqual(2, len(result.data["scenarios"]))
        self.assertIn("Boss 定时命中恢复方案比较", result.data["answer"])

    def test_http_endpoints_and_openapi_contract(self) -> None:
        request = BossTimedSequenceRequest(
            events=[
                BossTimedEvent(skill="Test Slam", at_seconds=0),
                BossTimedEvent(skill="Test Slam", at_seconds=2),
            ],
            level=84, pool=15000, recovery_per_second=6000,
        )
        response = timed_sequence(request, _caller="test")
        self.assertEqual(2, response.hit_count)
        self.assertEqual("survives_full_range", response.outcome)
        compared = compare_timed_sequences_api(BossTimedCompareRequest(scenarios=[
            BossTimedScenarioRequest(name="A", inputs=request),
            BossTimedScenarioRequest(name="B", inputs=request.model_copy(
                update={"recovery_per_second": 0},
            )),
        ]), _caller="test")
        self.assertEqual(2, len(compared["scenarios"]))
        from main import create_app

        paths = create_app().openapi()["paths"]
        self.assertIn("/poe_agent/boss/timed-sequence", paths)
        self.assertIn("/poe_agent/boss/timed-sequence/compare", paths)

    def test_registry_knowledge_and_natural_language_routes_are_wired(self) -> None:
        from agents.knowledge import ALLOWED_TOOLS

        for name in ("boss_timed_sequence_check", "boss_timed_sequence_compare"):
            self.assertIn(name, ALLOWED_TOOLS)
            self.assertIsNotNone(REGISTRY.spec(name))
        self.assertEqual(
            "boss_timed_sequence_check",
            _preferred_tool_route("第一发后 2 秒再吃第二发，期间每秒恢复能不能扛"),
        )
        self.assertEqual(
            "boss_timed_sequence_compare",
            _preferred_tool_route("两套 Boss 恢复方案对比"),
        )

    @staticmethod
    def _build_stats(*, include_recovery: bool = True) -> dict[str, float]:
        stats = {
            "LifeUnreserved": 15000,
            "EnergyShield": 1000,
            "Armour": 0,
        }
        if include_recovery:
            stats["NetLifeRegen"] = 6000
        return stats

    def test_build_life_pool_uses_disclosed_net_life_regen(self) -> None:
        result = boss_timed_sequence_from_build(
            PobBuild(player_stats=self._build_stats()),
            events=self._inputs()["events"], level=84, pool_mode="life",
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(6000, result.data["recovery_per_second"])
        self.assertEqual(6000, result.data["source_fields"]["NetLifeRegen"])
        self.assertEqual(
            "pob_player_stat:NetLifeRegen",
            result.data["source_config"]["recovery_source"],
        )
        self.assertIn("MaximumHitTaken 未用于", result.data["answer"])

    def test_build_combined_pool_requires_explicit_shared_recovery(self) -> None:
        build = PobBuild(player_stats=self._build_stats())
        unknown = boss_timed_sequence_from_build(
            build, events=self._inputs()["events"], level=84,
            pool_mode="life_and_energy_shield",
        )
        self.assertTrue(unknown.ok)
        self.assertFalse(unknown.data["assessable"])
        self.assertIn("独立池", unknown.data["reason"])
        unknown_response = BuildBossTimedSequenceOut(
            **unknown.data, citations=unknown.citations,
        )
        self.assertFalse(unknown_response.assessable)
        self.assertIsNone(unknown_response.total_taken_high)
        explicit = boss_timed_sequence_from_build(
            build, events=self._inputs()["events"], level=84,
            pool_mode="life_and_energy_shield", recovery_per_second=6000,
        )
        self.assertTrue(explicit.data["assessable"])
        self.assertEqual(16000, explicit.data["pool"])
        self.assertEqual("explicit_request", explicit.data["source_config"]["recovery_source"])

    def test_build_missing_recovery_stays_unknown(self) -> None:
        result = boss_timed_sequence_from_build(
            PobBuild(player_stats=self._build_stats(include_recovery=False)),
            events=self._inputs()["events"], level=84, pool_mode="life",
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertIn("NetLifeRegen", result.data["missing_inputs"][0])

    def test_build_negative_net_recovery_stays_unknown(self) -> None:
        stats = self._build_stats()
        stats["NetLifeRegen"] = -50
        result = boss_timed_sequence_from_build(
            PobBuild(player_stats=stats), events=self._inputs()["events"],
            level=84, pool_mode="life",
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertIn("固定正恢复模型", result.data["reason"])

    def test_build_request_and_response_expose_timed_sequence_contract(self) -> None:
        request = BuildRequest(code="abc", boss_timed_sequence={
            "events": [
                {"skill": "Test Slam", "at_seconds": 0},
                {"skill": "Test Slam", "at_seconds": 2},
            ],
            "level": 84, "pool_mode": "life",
        })
        self.assertIsNotNone(request.boss_timed_sequence)
        assert request.boss_timed_sequence is not None
        self.assertIsNone(request.boss_timed_sequence.recovery_per_second)
        checked = boss_timed_sequence_from_build(
            PobBuild(player_stats=self._build_stats()),
            events=[item.model_dump() for item in request.boss_timed_sequence.events],
            level=84, pool_mode="life",
        )
        response = BuildBossTimedSequenceOut(
            **checked.data, citations=checked.citations,
        )
        self.assertEqual("pob_player_stat", response.input_source)
        self.assertEqual(2, response.hit_count)
