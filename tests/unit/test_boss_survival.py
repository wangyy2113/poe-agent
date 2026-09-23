"""Boss 重复命中与 Max Hit 合流的领域、工具、API 和路由契约。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from api.boss import (
    BossDefenceThresholdRequest,
    BossSurvivalRequest,
    defence_threshold,
    survival,
)
from api.build import BuildRequest
from calc import boss_survival, constants, maxhit
from core import db as dbmod
from core.db import SCHEMA
from orchestrator import loop
from parsers.pob_build import PobBuild
from tools.impl.boss import (
    boss_defence_threshold,
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
