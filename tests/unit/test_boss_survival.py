"""Boss 重复命中与 Max Hit 合流的领域、工具、API 和路由契约。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from api.boss import (
    BossDefenceThresholdRequest,
    BossRiskAuditRequest,
    BossSequenceEntry,
    BossSequenceRequest,
    BossSurvivalRequest,
    defence_threshold,
    risk_audit,
    sequence,
    survival,
)
from api.build import BuildBossRiskAuditOut, BuildBossSequenceOut, BuildRequest
from calc import boss_survival, constants, maxhit
from core import db as dbmod
from core.db import SCHEMA
from orchestrator import loop
from parsers.pob_build import PobBuild
from tools.impl.boss import (
    boss_defence_threshold,
    boss_risk_audit,
    boss_risk_audit_from_build,
    boss_sequence_check,
    boss_sequence_from_build,
    boss_survival_check,
    boss_survival_from_build,
)
from tools.registry import REGISTRY


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    table = [0.0] * 100
    table[83] = 821.73
    conn.execute(
        "INSERT INTO game_constant(key,group_name,kind,value_json,source,source_priority,"
        "url,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            constants.MONSTER_DAMAGE_TABLE, "monsterDamageTable", "level_table",
            json.dumps(table), "Path of Building", 2, "https://example.test/pob/data", "now",
        ),
    )
    conn.execute(
        "INSERT INTO game_constant(key,group_name,kind,value_num,source,source_priority,"
        "url,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            constants.MAX_PHYS_DAMAGE_REDUCTION, "characterConstants", "scalar", 90,
            "Path of Building", 2, "https://example.test/pob/defence", "now",
        ),
    )
    common = (
        1400, None, 0, 0, "Path of Building", 2,
        "https://example.test/pob/boss-skills", "a" * 40, "now",
    )
    conn.execute(
        "INSERT INTO pob_boss_skill(name,damage_type,damage_mults,penetrations,"
        "uber_penetrations,uber_damage_mult,speed,uber_speed,crit_chance,earlier_uber,"
        "tooltip,source,source_priority,pob_url,source_commit,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "Test Slam", "Attack",
            json.dumps({"Physical": [10, 0.02]}), "{}", "{}", None,
            1000, None, 0, 0, "Physical test hit", *common[4:],
        ),
    )
    conn.execute(
        "INSERT INTO pob_boss_skill(name,damage_type,damage_mults,penetrations,"
        "uber_penetrations,uber_damage_mult,speed,uber_speed,crit_chance,earlier_uber,"
        "tooltip,source,source_priority,pob_url,source_commit,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "Test Chaos Hit", "Spell",
            json.dumps({"Chaos": [10, 0.02]}), "{}", "{}", None,
            1000, None, 0, 0, "Chaos test hit", *common[4:],
        ),
    )
    conn.execute(
        "INSERT INTO pob_boss_skill(name,damage_type,damage_mults,penetrations,"
        "uber_penetrations,uber_damage_mult,speed,uber_speed,crit_chance,earlier_uber,"
        "tooltip,source,source_priority,pob_url,source_commit,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "Shaper Ball", "SpellProjectile",
            json.dumps({"Cold": [11.668066430448, 0.058340332152239]}),
            json.dumps({"ColdPen": 25}), json.dumps({"ColdPen": 40}), None,
            common[0], common[1], common[2], common[3],
            "Uber has 40 Cold penetration", *common[4:],
        ),
    )
    conn.execute(
        "INSERT INTO pob_boss_skill(name,damage_type,damage_mults,penetrations,"
        "uber_penetrations,uber_damage_mult,speed,uber_speed,crit_chance,earlier_uber,"
        "tooltip,source,source_priority,pob_url,source_commit,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "Shaper Beam", "DamageOverTime",
            json.dumps({"Cold": [9.1, 0]}), "{}", "{}", None,
            1000, None, 0, 0, "Damage Over Time skill", *common[4:],
        ),
    )
    conn.commit()
    return conn


class BossSurvivalFormulaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patcher = patch.object(dbmod, "get_conn", return_value=self.conn)
        self.patcher.start()
        constants.reset_cache()

    def tearDown(self) -> None:
        constants.reset_cache()
        self.patcher.stop()
        self.conn.close()

    def test_元素穿透先扣抗性且区间跨过池子(self) -> None:
        result = boss_survival.assess_single_hit(
            damage_type="Cold", raw_low=8000, raw_high=12000, pool=5000,
            resist_pct=75, penetration_pct=25,
        )
        self.assertEqual(50, result.effective_resist_pct)
        self.assertEqual(4000, result.taken_low)
        self.assertEqual(6000, result.taken_high)
        self.assertEqual("roll_dependent", result.outcome)

    def test_等于池子按耗尽而不是存活(self) -> None:
        result = boss_survival.assess_single_hit(
            damage_type="Fire", raw_low=10000, raw_high=12000, pool=5000,
            resist_pct=75, penetration_pct=25,
        )
        self.assertEqual("fails_full_range", result.outcome)

    def test_连续命中按逐击承伤累加并保留三态(self) -> None:
        survives = boss_survival.assess_repeated_hits(
            damage_type="Fire", raw_low=1000, raw_high=2000, pool=5000,
            resist_pct=0, hit_count=2,
        )
        dependent = boss_survival.assess_repeated_hits(
            damage_type="Fire", raw_low=2000, raw_high=3000, pool=5000,
            resist_pct=0, hit_count=2,
        )
        fails = boss_survival.assess_repeated_hits(
            damage_type="Fire", raw_low=2500, raw_high=3000, pool=5000,
            resist_pct=0, hit_count=2,
        )
        self.assertEqual("survives_full_range", survives.outcome)
        self.assertEqual("roll_dependent", dependent.outcome)
        self.assertEqual("fails_full_range", fails.outcome)
        self.assertEqual(4000, dependent.total_taken_low)
        self.assertEqual(6000, dependent.total_taken_high)
        self.assertEqual(1000, dependent.remaining_low)
        self.assertEqual(-1000, dependent.remaining_high)

    def test_单击兼容入口与重复命中默认结果一致(self) -> None:
        kwargs = {
            "damage_type": "Cold", "raw_low": 8000, "raw_high": 12000,
            "pool": 5000, "resist_pct": 75, "penetration_pct": 25,
        }
        single = boss_survival.assess_single_hit(**kwargs)
        repeated = boss_survival.assess_repeated_hits(**kwargs)
        self.assertEqual(single, repeated)
        self.assertEqual(1, single.hit_count)
        self.assertEqual(single.taken_high, single.total_taken_high)

    def test_反解可承受击数区间且等于池子的那击耗尽(self) -> None:
        result = boss_survival.assess_repeated_hits(
            damage_type="Fire", raw_low=2000, raw_high=3000, pool=6000,
            resist_pct=0,
        )
        self.assertEqual(2, result.earliest_depletion_hit)
        self.assertEqual(3, result.latest_depletion_hit)
        self.assertEqual(1, result.guaranteed_survivable_hits)
        self.assertEqual(2, result.maximum_survivable_hits)
        zero = boss_survival.assess_repeated_hits(
            damage_type="Chaos", raw_low=0, raw_high=0, pool=6000,
            resist_pct=0,
        )
        self.assertIsNone(zero.earliest_depletion_hit)
        self.assertIsNone(zero.latest_depletion_hit)

    def test_反解防御池与抗性门槛(self) -> None:
        result = boss_survival.assess_repeated_hits(
            damage_type="Fire", raw_low=2000, raw_high=3000, pool=5000,
            resist_pct=0, hit_count=2,
        )
        self.assertEqual(4001, result.minimum_pool_for_any_roll)
        self.assertEqual(6001, result.minimum_pool_for_full_range)
        self.assertEqual(0, result.pool_shortfall_for_any_roll)
        self.assertEqual(1001, result.pool_shortfall_for_full_range)
        self.assertEqual("reachable", result.required_resist_status)
        self.assertEqual(17, result.required_resist_for_full_range_pct)
        self.assertEqual(17, result.resist_shortfall_pct)
        self.assertEqual("not_applicable", result.required_armour_status)

    def test_抗性上限不足时不伪造可达门槛(self) -> None:
        result = boss_survival.assess_repeated_hits(
            damage_type="Cold", raw_low=100000, raw_high=100000, pool=5000,
            resist_pct=75,
        )
        self.assertEqual("unreachable_at_90", result.required_resist_status)
        self.assertIsNone(result.required_resist_for_full_range_pct)
        self.assertIsNone(result.resist_shortfall_pct)

    def test_反解物理护甲门槛并尊重减伤上限(self) -> None:
        reachable = boss_survival.assess_repeated_hits(
            damage_type="Physical", raw_low=1000, raw_high=1000, pool=1500,
            resist_pct=0, hit_count=2,
        )
        self.assertEqual("reachable", reachable.required_armour_status)
        self.assertEqual(1667, reachable.required_armour_for_full_range)
        self.assertEqual(1667, reachable.armour_shortfall)
        self.assertLess(
            maxhit.taken_from(1000, armour=1667) * 2,
            1500,
        )
        impossible = boss_survival.assess_repeated_hits(
            damage_type="Physical", raw_low=10000, raw_high=10000, pool=1000,
            resist_pct=0,
        )
        self.assertEqual("unreachable", impossible.required_armour_status)
        self.assertIsNone(impossible.required_armour_for_full_range)

    def test_非物理伤害不误用护甲(self) -> None:
        plain = boss_survival.assess_single_hit(
            damage_type="Lightning", raw_low=10000, raw_high=10000, pool=6000,
            resist_pct=50,
        )
        armoured = boss_survival.assess_single_hit(
            damage_type="Lightning", raw_low=10000, raw_high=10000, pool=6000,
            resist_pct=50, armour=1_000_000, flat_physical_reduction_pct=90,
        )
        self.assertEqual(plain.taken_high, armoured.taken_high)

    def test_混合伤害序列逐步累计并区分可能与必然耗尽(self) -> None:
        result = boss_survival.assess_hit_sequence(
            pool=2500,
            hits=[
                boss_survival.BossSequenceHit(
                    skill="Fire Hit", damage_type="Fire", raw_low=1000,
                    raw_high=2000, resist_pct=0,
                ),
                boss_survival.BossSequenceHit(
                    skill="Physical Hit", damage_type="Physical", raw_low=1000,
                    raw_high=1000, resist_pct=0,
                ),
            ],
        )
        self.assertEqual("roll_dependent", result.outcome)
        self.assertEqual(2, result.earliest_possible_depletion_hit)
        self.assertIsNone(result.guaranteed_depletion_hit)
        self.assertEqual(2001, result.minimum_pool_for_any_roll)
        self.assertEqual(3001, result.minimum_pool_for_full_range)
        self.assertEqual("Physical Hit", result.steps[1].skill)
        self.assertEqual(-500, result.steps[1].remaining_worst_case)
        self.assertEqual(1, result.most_dangerous_hit_index)
        self.assertEqual("Fire Hit", result.most_dangerous_skill)
        self.assertEqual(1000, result.largest_single_hit_taken_low)
        self.assertEqual(2000, result.largest_single_hit_taken_high)
        self.assertEqual([
            {
                "damage_type": "Fire", "hit_count": 1,
                "raw_low": 1000.0, "raw_high": 2000.0,
                "taken_low": 1000.0, "taken_high": 2000.0,
            },
            {
                "damage_type": "Physical", "hit_count": 1,
                "raw_low": 1000.0, "raw_high": 1000.0,
                "taken_low": 1000.0, "taken_high": 1000.0,
            },
        ], [item.as_dict() for item in result.damage_type_totals])

    def test_混合序列累计恰好等于池子时判耗尽(self) -> None:
        result = boss_survival.assess_hit_sequence(
            pool=2000,
            hits=[
                boss_survival.BossSequenceHit(
                    skill="A", damage_type="Fire", raw_low=1000,
                    raw_high=2000, resist_pct=0,
                ),
                boss_survival.BossSequenceHit(
                    skill="B", damage_type="Physical", raw_low=1000,
                    raw_high=1000, resist_pct=0,
                ),
            ],
        )
        self.assertEqual("fails_full_range", result.outcome)
        self.assertEqual(1, result.earliest_possible_depletion_hit)
        self.assertEqual(2, result.guaranteed_depletion_hit)

    def test_Shaper球把技能伤害与MaxHit合成一个有界答案(self) -> None:
        result = boss_survival_check(
            "Shaper Ball", level=84, pool=6000, cold_resist_pct=75,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual("roll_dependent", result.data["verdict"])
        self.assertEqual(25, result.data["penetration_pct"])
        self.assertEqual(50, result.data["effective_resist_pct"])
        self.assertIn("实际扣除池子", result.data["answer"])
        self.assertIn("不是整场 Boss 战", result.data["answer"])
        self.assertEqual(2, len(result.citations))
        self.assertFalse(REGISTRY.spec("boss_survival_check").parameters["additionalProperties"])

    def test_工具累加两次并拒绝非法次数(self) -> None:
        result = boss_survival_check(
            "Shaper Ball", level=84, pool=10000, hit_count=2,
            cold_resist_pct=75,
        )
        self.assertEqual("roll_dependent", result.data["verdict"])
        self.assertEqual(2, result.data["hit_count"])
        self.assertEqual(
            round(result.data["taken_high"] * 2, 2),
            result.data["total_taken_high"],
        )
        self.assertIn("每次都命中、期间无恢复", result.data["answer"])
        self.assertEqual(2, result.data["earliest_depletion_hit"])
        self.assertEqual(3, result.data["latest_depletion_hit"])
        self.assertIn("最坏掷骰下保证承受 1 击", result.data["answer"])
        self.assertEqual(9589, result.data["minimum_pool_for_any_roll"])
        self.assertEqual(14383, result.data["minimum_pool_for_full_range"])
        self.assertEqual("unreachable_at_90", result.data["required_resist_status"])
        self.assertIn("90% 上限内无法达成", result.data["answer"])
        for count in (0, 101, True):
            with self.subTest(hit_count=count):
                invalid = boss_survival_check(
                    "Shaper Ball", level=84, pool=10000, hit_count=count,
                    cold_resist_pct=75,
                )
                self.assertFalse(invalid.ok)

    def test_Uber使用Uber穿透而非普通穿透(self) -> None:
        result = boss_survival_check(
            "Shaper Ball", level=84, pool=7000, uber=True, cold_resist_pct=75,
        )
        self.assertEqual(40, result.data["penetration_pct"])
        self.assertEqual(35, result.data["effective_resist_pct"])

    def test_缺对应抗性保持unknown(self) -> None:
        result = boss_survival_check("Shaper Ball", level=84, pool=6000)
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertEqual("unknown", result.data["verdict"])
        self.assertEqual(["冰霜抗性"], result.data["missing_inputs"])

    def test_持续伤害不冒充单次命中(self) -> None:
        result = boss_survival_check(
            "Shaper Beam", level=84, pool=6000, cold_resist_pct=75,
        )
        self.assertFalse(result.data["assessable"])
        self.assertEqual("unknown", result.data["verdict"])
        self.assertIn("持续伤害", result.data["reason"])

    def test_API复用同一确定性结果(self) -> None:
        response = survival(BossSurvivalRequest(
            skill="Shaper Ball", level=84, pool=10000, hit_count=2,
            cold_resist_pct=75,
        ), _caller="test")
        self.assertEqual("roll_dependent", response.verdict)
        self.assertEqual(25, response.penetration_pct)
        self.assertEqual(2, response.hit_count)
        self.assertIsNotNone(response.total_taken_high)
        self.assertEqual(2, response.earliest_depletion_hit)
        self.assertEqual(3, response.latest_depletion_hit)
        self.assertEqual(14383, response.minimum_pool_for_full_range)
        from main import create_app

        self.assertIn("/poe_agent/boss/survival", create_app().openapi()["paths"])

    def test_独立门槛工具无需当前池子即可回答需要多少血(self) -> None:
        result = boss_defence_threshold(
            "Shaper Ball", level=84, hit_count=3, cold_resist_pct=75,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertIsNone(result.data["target_pool"])
        self.assertIsNone(result.data["pool"])
        self.assertIsNone(result.data["verdict"])
        self.assertIsNone(result.data["max_raw_hit"])
        self.assertEqual(21574, result.data["minimum_pool_for_full_range"])
        self.assertEqual(
            "not_requested_without_target_pool", result.data["required_resist_status"]
        )
        self.assertIn("至少需要 21,574 防御池", result.data["answer"])
        spec = REGISTRY.spec("boss_defence_threshold")
        self.assertEqual(["skill", "level"], spec.parameters["required"])
        self.assertFalse(spec.parameters["additionalProperties"])

    def test_独立门槛工具有目标池时反解抗性与不可达状态(self) -> None:
        reachable = boss_defence_threshold(
            "Shaper Ball", level=84, target_pool=10000, cold_resist_pct=75,
        )
        self.assertEqual("reachable", reachable.data["required_resist_status"])
        self.assertEqual(56, reachable.data["required_resist_for_full_range_pct"])
        self.assertEqual(10000, reachable.data["target_pool"])
        self.assertIn("至少需要穿透前抗性 56%", reachable.data["answer"])
        impossible = boss_defence_threshold(
            "Shaper Ball", level=84, target_pool=1000, cold_resist_pct=75,
        )
        self.assertEqual("unreachable_at_90", impossible.data["required_resist_status"])
        self.assertIn("90% 抗性上限内仍不可达", impossible.data["answer"])

    def test_门槛API与OpenAPI公开独立契约(self) -> None:
        response = defence_threshold(BossDefenceThresholdRequest(
            skill="Test Slam", level=84, target_pool=15000, hit_count=2,
            armour=0,
        ), _caller="test")
        self.assertEqual("Physical", response.damage_type)
        self.assertEqual(15000, response.target_pool)
        self.assertEqual("reachable", response.required_armour_status)
        from main import create_app

        schema = create_app().openapi()
        self.assertIn("/poe_agent/boss/threshold", schema["paths"])

    def test_显式混合技能序列工具返回逐击轨迹(self) -> None:
        result = boss_sequence_check(
            sequence=[
                {"skill": "Shaper Ball", "count": 2},
                {"skill": "Test Slam", "count": 1},
            ],
            level=84, pool=25000, cold_resist_pct=75, armour=10000,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual(3, result.data["hit_count"])
        self.assertEqual(
            ["Shaper Ball", "Shaper Ball", "Test Slam"],
            [step["skill"] for step in result.data["steps"]],
        )
        self.assertEqual(3, len(result.citations))
        self.assertEqual(2, len(result.data["damage_type_totals"]))
        self.assertEqual("Test Slam", result.data["most_dangerous_skill"])
        self.assertGreater(result.data["largest_single_hit_taken_high"], 0)
        self.assertIn("按伤害类型汇总", result.data["answer"])
        self.assertIn("系统没有声称这是 Boss 的真实连招", result.data["answer"])
        self.assertFalse(REGISTRY.spec("boss_sequence_check").parameters["additionalProperties"])
        too_long = boss_sequence_check(
            sequence=[
                {"skill": "Shaper Ball", "count": 100},
                {"skill": "Test Slam", "count": 1},
            ],
            level=84, pool=25000, cold_resist_pct=75,
        )
        self.assertFalse(too_long.ok)
        self.assertIn("最多包含 100 次", too_long.error)

    def test_显式序列任一步缺抗性则整体unknown(self) -> None:
        result = boss_sequence_check(
            sequence=[{"skill": "Test Slam"}, {"skill": "Shaper Ball"}],
            level=84, pool=25000, armour=10000,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertEqual(2, result.data["failed_sequence_index"])
        self.assertEqual(["冰霜抗性"], result.data["missing_inputs"])

    def test_显式序列API公开逐击响应(self) -> None:
        response = sequence(BossSequenceRequest(
            sequence=[
                BossSequenceEntry(skill="Shaper Ball", count=1),
                BossSequenceEntry(skill="Test Slam", count=1),
            ],
            level=84, pool=25000, cold_resist_pct=75, armour=10000,
        ), _caller="test")
        self.assertEqual(2, response.hit_count)
        self.assertEqual("Shaper Ball", response.steps[0].skill)
        from main import create_app

        self.assertIn("/poe_agent/boss/sequence", create_app().openapi()["paths"])

    def test_Boss风险审计排序已知单击并保留未知项(self) -> None:
        result = boss_risk_audit(
            level=84, pool=10000, cold_resist_pct=75,
            chaos_resist_pct=0, armour=10000,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual("partial", result.data["assessment_status"])
        self.assertEqual(4, result.data["total_skill_count"])
        self.assertEqual(3, result.data["assessed_skill_count"])
        self.assertEqual(1, result.data["unknown_skill_count"])
        self.assertEqual("unknown", result.data["verdict"])
        self.assertEqual("Test Chaos Hit", result.data["most_dangerous_skill"])
        self.assertEqual(
            [1, 2, 3],
            [item["risk_rank"] for item in result.data["skills"] if item["assessable"]],
        )
        self.assertEqual("Shaper Beam", result.data["skills"][-1]["skill"])
        self.assertIn("不是全部 Boss/技能清单", result.data["boundary"])
        self.assertEqual(5, len(result.citations))
        self.assertFalse(REGISTRY.spec("boss_risk_audit").parameters["additionalProperties"])

    def test_Boss风险审计缺部分防御时只报告部分覆盖(self) -> None:
        result = boss_risk_audit(
            level=84, pool=10000, cold_resist_pct=75,
        )
        self.assertTrue(result.data["assessable"])
        self.assertEqual(1, result.data["assessed_skill_count"])
        self.assertEqual(3, result.data["unknown_skill_count"])
        self.assertEqual(["armour", "chaos_resist_pct"], result.data["missing_inputs"])
        self.assertEqual("Shaper Ball", result.data["most_dangerous_skill"])
        self.assertIn("未知项不会被当成低风险", result.data["answer"])

    def test_Boss风险审计批量规划连续命中的防御缺口(self) -> None:
        result = boss_risk_audit(
            level=84, pool=5000, hit_count=2, cold_resist_pct=75,
            chaos_resist_pct=0, armour=10000,
        )
        self.assertTrue(result.ok)
        self.assertEqual(2, result.data["hit_count"])
        self.assertEqual(19723, result.data["minimum_pool_for_all_assessed_skills"])
        self.assertEqual(14723, result.data["pool_shortfall_for_all_assessed_skills"])
        self.assertEqual("Test Chaos Hit", result.data["pool_bottleneck_skill"])
        self.assertEqual(3, result.data["defence_gap_skill_count"])
        self.assertEqual(2, result.data["single_axis_reachable_gap_count"])
        self.assertEqual(1, result.data["single_axis_unreachable_gap_count"])
        chaos = result.data["skills"][0]
        self.assertEqual(19722, chaos["total_taken_high"])
        self.assertEqual(75, chaos["required_resist_for_full_range_pct"])
        self.assertEqual(75, chaos["resist_shortfall_pct"])
        cold = next(
            item for item in result.data["skills"] if item["skill"] == "Shaper Ball"
        )
        self.assertEqual("unreachable_at_90", cold["required_resist_status"])
        self.assertIn("统一池子门槛：19,723", result.data["answer"])
        self.assertIn("累计承伤", result.data["answer"])
        for hit_count in (0, 101, True):
            with self.subTest(hit_count=hit_count):
                invalid = boss_risk_audit(level=84, pool=5000, hit_count=hit_count)
                self.assertFalse(invalid.ok)

    def test_Boss风险审计API公开结构化排名(self) -> None:
        response = risk_audit(BossRiskAuditRequest(
            level=84, pool=10000, hit_count=2, cold_resist_pct=75,
            chaos_resist_pct=0, armour=10000,
        ), _caller="test")
        self.assertEqual("partial", response.assessment_status)
        self.assertEqual("Test Chaos Hit", response.most_dangerous_skill)
        self.assertEqual(2, response.hit_count)
        self.assertEqual(19723, response.minimum_pool_for_all_assessed_skills)
        self.assertEqual(19722, response.most_dangerous_total_taken_high)
        from main import create_app

        self.assertIn("/poe_agent/boss/audit", create_app().openapi()["paths"])

    def test_Boss风险审计自然问法确定性直达(self) -> None:
        question = "84级，10000血、75冰抗、0混抗、10000护甲，所有 Boss 技能连续2次哪个最危险"
        self.assertEqual("boss_risk_audit", loop._preferred_tool_route(question))
        self.assertEqual({
            "level": 84, "pool": 10000, "hit_count": 2, "cold_resist_pct": 75,
            "chaos_resist_pct": 0, "armour": 10000,
        }, loop._boss_risk_audit_args(question))
        self.assertIn("boss_risk_audit", loop._CODE_INVOKED_TOOLS)
        self.assertIn("boss_risk_audit", loop._DETERMINISTIC_DIRECT_TOOLS)
        self.assertIsNone(loop._boss_risk_audit_args(
            "84级，所有 Boss 技能哪个最危险"
        ))

    def test_聊天路由只在参数能唯一冻结时直接执行(self) -> None:
        question = "84级 Shaper Ball，6000血、75冰抗，能扛得住吗"
        self.assertEqual("boss_survival_check", loop._preferred_tool_route(question))
        self.assertEqual({
            "skill": "Shaper Ball", "level": 84, "pool": 6000,
            "cold_resist_pct": 75,
        }, loop._boss_survival_args(question))
        self.assertIsNone(loop._boss_survival_args("Shaper Ball 能扛得住吗"))

    def test_聊天路由提取明确连续次数但不猜多解或越界(self) -> None:
        base = "84级 Shaper Ball，10000血、75冰抗，"
        for phrase in ("连续3次能扛吗", "3发能扛吗", "3下命中能扛吗"):
            with self.subTest(phrase=phrase):
                args = loop._boss_survival_args(base + phrase)
                self.assertEqual(3, args["hit_count"])
        self.assertIsNone(loop._boss_survival_args(base + "连续3次、4发能扛吗"))
        self.assertIsNone(loop._boss_survival_args(base + "连续101次能扛吗"))
        capacity = loop._boss_survival_args(base + "能扛几下")
        self.assertNotIn("hit_count", capacity)
        self.assertEqual(
            "boss_survival_check", loop._preferred_tool_route(base + "能扛几下")
        )

    def test_防御门槛问法走独立工具并允许没有目标池(self) -> None:
        pool_question = "84级 Shaper Ball，75冰抗，连续3次需要多少血"
        self.assertEqual(
            "boss_defence_threshold", loop._preferred_tool_route(pool_question)
        )
        self.assertEqual({
            "skill": "Shaper Ball", "level": 84, "hit_count": 3,
            "cold_resist_pct": 75,
        }, loop._boss_defence_threshold_args(pool_question))
        resist_question = "84级 Shaper Ball，5000血，连续2次需要多少冰抗"
        self.assertEqual(
            "boss_defence_threshold", loop._preferred_tool_route(resist_question)
        )
        self.assertEqual(5000, loop._boss_defence_threshold_args(
            resist_question
        )["target_pool"])
        self.assertNotEqual(
            "boss_defence_threshold", loop._preferred_tool_route("需要多少护甲")
        )

    def test_显式混合技能序列可确定性直达(self) -> None:
        question = (
            "84级 Shaper Ball 2发，然后 Test Slam 1下，"
            "25000血、75冰抗、10000护甲，这套连招扛得住吗"
        )
        self.assertEqual("boss_sequence_check", loop._preferred_tool_route(question))
        self.assertIn("boss_sequence_check", loop._CODE_INVOKED_TOOLS)
        self.assertIn("boss_sequence_check", loop._DETERMINISTIC_DIRECT_TOOLS)
        self.assertEqual({
            "sequence": [
                {"skill": "Shaper Ball", "count": 2},
                {"skill": "Test Slam", "count": 1},
            ],
            "level": 84,
            "pool": 25000,
            "cold_resist_pct": 75,
            "armour": 10000,
        }, loop._boss_sequence_args(question))


class BuildBossSurvivalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.patcher = patch.object(dbmod, "get_conn", return_value=self.conn)
        self.patcher.start()
        constants.reset_cache()

    def tearDown(self) -> None:
        constants.reset_cache()
        self.patcher.stop()
        self.conn.close()

    def test_Build请求可携带Boss校验且默认生命加护盾(self) -> None:
        req = BuildRequest(code="abc", boss_survival={
            "skill": "Shaper Ball", "level": 84,
        })
        self.assertEqual("life_and_energy_shield", req.boss_survival.pool_mode)
        from main import create_app

        schema = create_app().openapi()
        self.assertIn("boss_survival", schema["components"]["schemas"]["BuildRequest"]["properties"])

    def test_Build请求可携带显式Boss序列并限制展开后命中数(self) -> None:
        req = BuildRequest(code="abc", boss_sequence={
            "sequence": [
                {"skill": "Shaper Ball", "count": 2},
                {"skill": "Test Slam"},
            ],
            "level": 84,
        })
        self.assertEqual("life_and_energy_shield", req.boss_sequence.pool_mode)
        self.assertEqual(2, req.boss_sequence.sequence[0].count)
        with self.assertRaises(ValueError):
            BuildRequest(code="abc", boss_sequence={
                "sequence": [
                    {"skill": "Shaper Ball", "count": 100},
                    {"skill": "Test Slam", "count": 1},
                ],
                "level": 84,
            })
        from main import create_app

        props = create_app().openapi()["components"]["schemas"]["BuildRequest"]["properties"]
        self.assertIn("boss_sequence", props)

    def test_Build请求可携带Boss风险审计(self) -> None:
        req = BuildRequest(code="abc", boss_risk_audit={"level": 84, "hit_count": 3})
        self.assertEqual("life_and_energy_shield", req.boss_risk_audit.pool_mode)
        self.assertEqual(3, req.boss_risk_audit.hit_count)
        from main import create_app

        props = create_app().openapi()["components"]["schemas"]["BuildRequest"]["properties"]
        self.assertIn("boss_risk_audit", props)

    def test_Build显式混合序列只读取实际需要的基础防御字段(self) -> None:
        build = PobBuild(player_stats={
            "Life": 12000, "LifeUnreserved": 10000, "EnergyShield": 2000,
            "ColdResist": 75, "FireResist": 10, "Armour": 10000,
            "ColdMaximumHitTaken": 999999,
        })
        result = boss_sequence_from_build(
            build,
            sequence=[
                {"skill": "Shaper Ball", "count": 2},
                {"skill": "Test Slam", "count": 1},
            ],
            level=84,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual("basic_defence_sequence", result.data["calculation_mode"])
        self.assertEqual(12000, result.data["pool"])
        self.assertEqual({
            "LifeUnreserved": 10000.0, "EnergyShield": 2000.0,
            "ColdResist": 75.0, "Armour": 10000.0,
        }, result.data["source_fields"])
        self.assertNotIn("ColdMaximumHitTaken", result.data["source_fields"])
        self.assertNotIn("FireResist", result.data["source_fields"])
        self.assertIn("不能拆分", result.data["fallback_reason"])
        self.assertEqual(3, result.data["hit_count"])
        self.assertEqual(2, len(result.data["damage_type_totals"]))
        self.assertIn("Build 快照输入", result.data["answer"])
        response = BuildBossSequenceOut(
            **result.data, citations=result.citations
        )
        self.assertEqual("Test Slam", response.most_dangerous_skill)
        self.assertEqual("pob_player_stat", response.input_source)

    def test_Build显式序列缺任一所需防御字段则整体unknown(self) -> None:
        result = boss_sequence_from_build(
            PobBuild(player_stats={
                "Life": 10000, "EnergyShield": 0, "ColdResist": 75,
            }),
            sequence=[{"skill": "Shaper Ball"}, {"skill": "Test Slam"}],
            level=84,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.data["assessable"])
        self.assertEqual("unknown", result.data["calculation_mode"])
        self.assertEqual(["Armour"], result.data["missing_inputs"])
        self.assertEqual({
            "Life": 10000.0, "EnergyShield": 0.0, "ColdResist": 75.0,
        }, result.data["source_fields"])
        response = BuildBossSequenceOut(
            **result.data, citations=result.citations
        )
        self.assertEqual(10000, response.pool)
        unknown_skill = boss_sequence_from_build(
            PobBuild(player_stats={"Life": 10000, "EnergyShield": 0}),
            sequence=[{"skill": "Not A Real Boss Skill"}], level=84,
        )
        unknown_response = BuildBossSequenceOut(
            **unknown_skill.data, citations=unknown_skill.citations
        )
        self.assertFalse(unknown_response.found)
        self.assertIsNone(unknown_response.pool)

    def test_Build显式序列保留混沌与护盾机制歧义(self) -> None:
        result = boss_sequence_from_build(
            PobBuild(player_stats={
                "Life": 5000, "EnergyShield": 1000, "ChaosResist": 0,
            }),
            sequence=[{"skill": "Test Chaos Hit"}], level=84,
        )
        self.assertFalse(result.data["assessable"])
        self.assertTrue(any(
            "混沌伤害是否绕过能量护盾" in item
            for item in result.data["missing_inputs"]
        ))
        life_only = boss_sequence_from_build(
            PobBuild(player_stats={
                "Life": 5000, "EnergyShield": 1000, "ChaosResist": 0,
            }),
            sequence=[{"skill": "Test Chaos Hit"}], level=84,
            pool_mode="life",
        )
        self.assertTrue(life_only.data["assessable"])
        self.assertEqual(5000, life_only.data["pool"])

    def test_Build风险审计逐字段披露并隔离单击MaxHit(self) -> None:
        result = boss_risk_audit_from_build(
            PobBuild(player_stats={
                "Life": 10000, "EnergyShield": 0, "ColdResist": 75,
                "ChaosResist": 0, "Armour": 10000,
                "FireResist": 75, "LightningResist": 75,
                "PhysicalMaximumHitTaken": 999999,
            }),
            level=84, hit_count=2,
        )
        self.assertTrue(result.ok)
        self.assertEqual("partial", result.data["assessment_status"])
        self.assertEqual(2, result.data["hit_count"])
        self.assertEqual(19723, result.data["minimum_pool_for_all_assessed_skills"])
        self.assertEqual("basic_defence_risk_audit", result.data["calculation_mode"])
        self.assertEqual({
            "Life": 10000.0, "EnergyShield": 0.0, "ColdResist": 75.0,
            "ChaosResist": 0.0, "Armour": 10000.0,
        }, result.data["source_fields"])
        self.assertNotIn("PhysicalMaximumHitTaken", result.data["source_fields"])
        self.assertIn("不使用", result.data["fallback_reason"])
        response = BuildBossRiskAuditOut(
            **result.data, citations=result.citations
        )
        self.assertEqual("Test Chaos Hit", response.most_dangerous_skill)

    def test_Build风险审计不把缺失护甲当作零护甲(self) -> None:
        result = boss_risk_audit_from_build(
            PobBuild(player_stats={
                "Life": 10000, "EnergyShield": 0,
                "ColdResist": 75, "ChaosResist": 0,
            }),
            level=84,
        )
        physical = next(
            item for item in result.data["skills"] if item["skill"] == "Test Slam"
        )
        self.assertFalse(physical["assessable"])
        self.assertEqual(["armour"], physical["missing_inputs"])
        self.assertNotIn("Armour", result.data["source_fields"])
        missing_pool = boss_risk_audit_from_build(
            PobBuild(player_stats={"ColdResist": 75}), level=84,
        )
        self.assertFalse(missing_pool.data["assessable"])
        self.assertEqual(4, missing_pool.data["total_skill_count"])
        self.assertEqual(4, missing_pool.data["unknown_skill_count"])
        BuildBossRiskAuditOut(
            **missing_pool.data, citations=missing_pool.citations
        )

    def test_Build风险审计保留混沌与护盾歧义但继续已知项(self) -> None:
        result = boss_risk_audit_from_build(
            PobBuild(player_stats={
                "Life": 10000, "EnergyShield": 1000, "ColdResist": 75,
                "ChaosResist": 0, "Armour": 10000,
            }),
            level=84,
        )
        chaos = next(
            item for item in result.data["skills"] if item["skill"] == "Test Chaos Hit"
        )
        self.assertFalse(chaos["assessable"])
        self.assertIn("绕过能量护盾", chaos["reason"])
        self.assertTrue(result.data["assessable"])
        self.assertEqual("partial", result.data["assessment_status"])
        self.assertNotIn("ChaosResist", result.data["source_fields"])

    def test_直接使用Build快照的未保留生命护盾和对应抗性(self) -> None:
        build = PobBuild(player_stats={
            "Life": 5000, "LifeUnreserved": 4000, "EnergyShield": 2000,
            "ColdResist": 75, "Armour": 999999,
        })
        result = boss_survival_from_build(
            build, skill="Shaper Ball", level=84,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["assessable"])
        self.assertEqual("roll_dependent", result.data["verdict"])
        self.assertEqual(6000, result.data["pool"])
        self.assertEqual({
            "LifeUnreserved": 4000.0, "EnergyShield": 2000.0, "ColdResist": 75.0,
        }, result.data["source_fields"])
        self.assertEqual("pob_player_stat", result.data["input_source"])
        self.assertIn("本库没有重算 Build", result.data["answer"])
        self.assertNotIn("Armour", result.data["source_fields"])
        self.assertEqual("basic_defence_snapshot", result.data["calculation_mode"])
        self.assertIn("presetBossSkills", result.data["fallback_reason"])

    def test_配置精确匹配时优先直接比较PoB_Max_Hit(self) -> None:
        build = PobBuild(
            config={"presetBossSkills": "Shaper Ball", "enemyIsBoss": "Pinnacle"},
            player_stats={
                "Life": 100, "EnergyShield": 0, "ColdResist": -200,
                "ColdMaximumHitTaken": 12000,
            },
        )
        result = boss_survival_from_build(
            build, skill="Shaper Ball", level=84,
        )
        self.assertTrue(result.data["assessable"])
        self.assertEqual("roll_dependent", result.data["verdict"])
        self.assertEqual("pob_max_hit_snapshot", result.data["calculation_mode"])
        self.assertIsNone(result.data["pool"])
        self.assertIsNone(result.data["taken_low"])
        self.assertIsNone(result.data["earliest_depletion_hit"])
        self.assertIn("单击阈值", result.data["capacity_reason"])
        self.assertIsNone(result.data["minimum_pool_for_full_range"])
        self.assertEqual(
            "unknown_from_pob_max_hit", result.data["required_resist_status"]
        )
        self.assertEqual(12000, result.data["max_raw_hit"])
        self.assertEqual({"ColdMaximumHitTaken": 12000.0}, result.data["source_fields"])
        self.assertEqual({
            "presetBossSkills": "Shaper Ball", "enemyIsBoss": "Pinnacle",
            "enemyLevel（PoB 默认）": "84",
        }, result.data["source_config"])
        self.assertEqual(2, len(result.citations))
        self.assertIn("完整 Max Hit 快照", result.data["answer"])

    def test_调用方可禁用PoB_Max_Hit并强制基础模式(self) -> None:
        build = PobBuild(
            config={"presetBossSkills": "Shaper Ball", "enemyIsBoss": "Pinnacle"},
            player_stats={
                "Life": 4000, "EnergyShield": 2000, "ColdResist": 75,
                "ColdMaximumHitTaken": 50000,
            },
        )
        result = boss_survival_from_build(
            build, skill="Shaper Ball", level=84, use_pob_max_hit=False,
        )
        self.assertEqual("basic_defence_snapshot", result.data["calculation_mode"])
        self.assertIn("use_pob_max_hit=false", result.data["fallback_reason"])
        self.assertNotEqual(50000, result.data["max_raw_hit"])

    def test_连续多击禁用单击Max_Hit并降级累加基础承伤(self) -> None:
        build = PobBuild(
            config={"presetBossSkills": "Shaper Ball", "enemyIsBoss": "Pinnacle"},
            player_stats={
                "Life": 10000, "EnergyShield": 0, "ColdResist": 75,
                "ColdMaximumHitTaken": 50000,
            },
        )
        result = boss_survival_from_build(
            build, skill="Shaper Ball", level=84, hit_count=2,
        )
        self.assertEqual("basic_defence_snapshot", result.data["calculation_mode"])
        self.assertEqual("roll_dependent", result.data["verdict"])
        self.assertEqual(2, result.data["hit_count"])
        self.assertEqual(2, result.data["earliest_depletion_hit"])
        self.assertEqual(3, result.data["latest_depletion_hit"])
        self.assertIn("MaximumHitTaken 只是单击阈值", result.data["fallback_reason"])
        self.assertNotEqual(50000, result.data["max_raw_hit"])
        self.assertEqual(14383, result.data["minimum_pool_for_full_range"])

    def test_连续多击降级后缺基础字段保持unknown(self) -> None:
        build = PobBuild(
            config={"presetBossSkills": "Shaper Ball", "enemyIsBoss": "Pinnacle"},
            player_stats={"ColdMaximumHitTaken": 50000},
        )
        result = boss_survival_from_build(
            build, skill="Shaper Ball", level=84, hit_count=2,
        )
        self.assertEqual("unknown", result.data["calculation_mode"])
        self.assertEqual(2, result.data["hit_count"])
        self.assertIn("MaximumHitTaken 只是单击阈值", result.data["fallback_reason"])
        self.assertIn("Life（或 LifeUnreserved）", result.data["missing_inputs"])

    def test_PoB元素三项相等时可用唯一导出的闪电Max_Hit(self) -> None:
        build = PobBuild(
            config={
                "presetBossSkills": "Shaper Ball", "enemyIsBoss": "Boss",
                "enemyLevel": "84",
            },
            player_stats={"LightningMaximumHitTaken": 15000},
        )
        result = boss_survival_from_build(
            build, skill="Shaper Ball", level=84,
        )
        self.assertEqual("pob_max_hit_snapshot", result.data["calculation_mode"])
        self.assertEqual(
            {"LightningMaximumHitTaken": 15000.0},
            result.data["source_fields"],
        )
        self.assertIn("Elemental Max Hit", result.data["max_hit_field_note"])
        self.assertEqual("survives_full_range", result.data["verdict"])

    def test_普通Boss默认敌人等级受角色等级限制(self) -> None:
        build = PobBuild(
            level=80,
            config={"presetBossSkills": "Shaper Ball", "enemyIsBoss": "Boss"},
            player_stats={"ColdMaximumHitTaken": 12000},
        )
        exact = boss_survival_from_build(
            build, skill="Shaper Ball", level=80,
        )
        self.assertEqual("pob_max_hit_snapshot", exact.data["calculation_mode"])
        self.assertEqual("80", exact.data["source_config"]["enemyLevel（PoB 默认）"])

        mismatch = boss_survival_from_build(
            build, skill="Shaper Ball", level=83,
        )
        self.assertEqual("unknown", mismatch.data["calculation_mode"])
        self.assertIn("enemyLevel", mismatch.data["fallback_reason"])

    def test_Boss预设或Uber档位不匹配时降级到基础字段模式(self) -> None:
        stats = {
            "Life": 4000, "EnergyShield": 2000, "ColdResist": 75,
            "ColdMaximumHitTaken": 50000,
        }
        wrong_skill = boss_survival_from_build(
            PobBuild(config={"presetBossSkills": "Maven Fireball"}, player_stats=stats),
            skill="Shaper Ball", level=84,
        )
        self.assertEqual("basic_defence_snapshot", wrong_skill.data["calculation_mode"])
        self.assertIn("presetBossSkills", wrong_skill.data["fallback_reason"])
        self.assertNotEqual(50000, wrong_skill.data["max_raw_hit"])

        wrong_tier = boss_survival_from_build(
            PobBuild(
                config={"presetBossSkills": "Shaper Ball", "enemyIsBoss": "Uber"},
                player_stats=stats,
            ),
            skill="Shaper Ball", level=84, uber=False,
        )
        self.assertEqual("basic_defence_snapshot", wrong_tier.data["calculation_mode"])
        self.assertIn("普通/Uber", wrong_tier.data["fallback_reason"])

        wrong_level = boss_survival_from_build(
            PobBuild(
                config={"presetBossSkills": "Shaper Ball", "enemyIsBoss": "Pinnacle"},
                player_stats=stats,
            ),
            skill="Shaper Ball", level=85,
        )
        self.assertEqual("basic_defence_snapshot", wrong_level.data["calculation_mode"])
        self.assertIn("enemyLevel", wrong_level.data["fallback_reason"])

    def test_显式穿透覆盖与技能预设不一致时不复用Max_Hit(self) -> None:
        build = PobBuild(
            config={
                "presetBossSkills": "Shaper Ball", "enemyIsBoss": "Pinnacle",
                "enemyColdPen": "10",
            },
            player_stats={
                "Life": 4000, "EnergyShield": 2000, "ColdResist": 75,
                "ColdMaximumHitTaken": 50000,
            },
        )
        result = boss_survival_from_build(
            build, skill="Shaper Ball", level=84,
        )
        self.assertEqual("basic_defence_snapshot", result.data["calculation_mode"])
        self.assertIn("enemyColdPen", result.data["fallback_reason"])
        self.assertEqual("10", result.data["source_config"]["enemyColdPen"])

    def test_物理压制覆盖不匹配时不复用Max_Hit(self) -> None:
        result = boss_survival_from_build(
            PobBuild(
                config={
                    "presetBossSkills": "Test Slam", "enemyIsBoss": "Boss",
                    "enemyPhysicalOverwhelm": "10",
                },
                player_stats={
                    "Life": 5000, "EnergyShield": 0, "Armour": 10000,
                    "PhysicalMaximumHitTaken": 50000,
                },
            ),
            skill="Test Slam", level=83,
        )
        self.assertEqual("basic_defence_snapshot", result.data["calculation_mode"])
        self.assertIn("enemyPhysicalOverwhelm", result.data["fallback_reason"])

    def test_非整数或非有限Boss配置不复用Max_Hit(self) -> None:
        stats = {
            "Life": 4000, "EnergyShield": 2000, "ColdResist": 75,
            "ColdMaximumHitTaken": 50000,
        }
        for raw_level in ("84.5", "NaN", "Infinity"):
            with self.subTest(enemyLevel=raw_level):
                result = boss_survival_from_build(
                    PobBuild(
                        config={
                            "presetBossSkills": "Shaper Ball",
                            "enemyIsBoss": "Pinnacle",
                            "enemyLevel": raw_level,
                        },
                        player_stats=stats,
                    ),
                    skill="Shaper Ball", level=84,
                )
                self.assertEqual(
                    "basic_defence_snapshot", result.data["calculation_mode"]
                )
                self.assertIn("有效整数", result.data["fallback_reason"])

        for raw_penetration in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(enemyColdPen=raw_penetration):
                result = boss_survival_from_build(
                    PobBuild(
                        config={
                            "presetBossSkills": "Shaper Ball",
                            "enemyIsBoss": "Pinnacle",
                            "enemyColdPen": raw_penetration,
                        },
                        player_stats=stats,
                    ),
                    skill="Shaper Ball", level=84,
                )
                self.assertEqual(
                    "basic_defence_snapshot", result.data["calculation_mode"]
                )
                self.assertIn("有效数值", result.data["fallback_reason"])

    def test_可显式只用生命池并在未保留生命缺席时回退总生命(self) -> None:
        build = PobBuild(player_stats={
            "Life": 4000, "EnergyShield": 9000, "ColdResist": 75,
        })
        result = boss_survival_from_build(
            build, skill="Shaper Ball", level=84, pool_mode="life",
        )
        self.assertEqual(4000, result.data["pool"])
        self.assertEqual("fails_full_range", result.data["verdict"])
        self.assertEqual({"Life": 4000.0, "ColdResist": 75.0}, result.data["source_fields"])

    def test_缺池或对应抗性时保持unknown且列出原始字段名(self) -> None:
        missing_es = boss_survival_from_build(
            PobBuild(player_stats={"Life": 5000, "ColdResist": 75}),
            skill="Shaper Ball", level=84,
        )
        self.assertFalse(missing_es.data["assessable"])
        self.assertEqual("unknown", missing_es.data["verdict"])
        self.assertEqual(["EnergyShield"], missing_es.data["missing_inputs"])
        self.assertEqual(1, len(missing_es.citations))

        missing_resist = boss_survival_from_build(
            PobBuild(player_stats={"Life": 5000, "EnergyShield": 0}),
            skill="Shaper Ball", level=84,
        )
        self.assertEqual(["ColdResist"], missing_resist.data["missing_inputs"])

    def test_混沌命中不能擅自认定护盾保护生命(self) -> None:
        result = boss_survival_from_build(
            PobBuild(player_stats={
                "Life": 5000, "EnergyShield": 1000, "ChaosResist": 0,
            }),
            skill="Test Chaos Hit", level=84,
        )
        self.assertFalse(result.data["assessable"])
        self.assertTrue(any(
            "混沌伤害是否绕过能量护盾" in item
            for item in result.data["missing_inputs"]
        ))

        life_only = boss_survival_from_build(
            PobBuild(player_stats={
                "Life": 5000, "EnergyShield": 1000, "ChaosResist": 0,
            }),
            skill="Test Chaos Hit", level=84, pool_mode="life",
        )
        self.assertTrue(life_only.data["assessable"])
        self.assertEqual(5000, life_only.data["pool"])

    def test_物理命中只映射护甲不误用面板物理减伤(self) -> None:
        result = boss_survival_from_build(
            PobBuild(player_stats={
                "Life": 5000, "EnergyShield": 0, "Armour": 10000,
                "PhysicalDamageReduction": 78,
            }),
            skill="Test Slam", level=84,
        )
        self.assertTrue(result.data["assessable"])
        self.assertEqual(10000, result.data["source_fields"]["Armour"])
        self.assertNotIn("PhysicalDamageReduction", result.data["source_fields"])
        self.assertIn("不会把 PhysicalDamageReduction", result.data["boundary"])

    def test_持续伤害沿用核心unknown且不伪造防御池(self) -> None:
        result = boss_survival_from_build(
            PobBuild(player_stats={"Life": 5000, "EnergyShield": 0, "ColdResist": 75}),
            skill="Shaper Beam", level=84,
        )
        self.assertFalse(result.data["assessable"])
        self.assertEqual("unknown", result.data["verdict"])
        self.assertIsNone(result.data["pool"])
        self.assertIn("持续伤害", result.data["reason"])


if __name__ == "__main__":
    unittest.main()
