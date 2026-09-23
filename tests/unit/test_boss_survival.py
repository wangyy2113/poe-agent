"""Boss 单次命中与 Max Hit 合流的领域、工具、API 和路由契约。"""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from api.boss import BossSurvivalRequest, survival
from api.build import BuildRequest
from calc import boss_survival, constants
from core import db as dbmod
from core.db import SCHEMA
from orchestrator import loop
from parsers.pob_build import PobBuild
from tools.impl.boss import boss_survival_check, boss_survival_from_build
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
            skill="Shaper Ball", level=84, pool=6000, cold_resist_pct=75,
        ), _caller="test")
        self.assertEqual("roll_dependent", response.verdict)
        self.assertEqual(25, response.penetration_pct)
        from main import create_app

        self.assertIn("/poe_agent/boss/survival", create_app().openapi()["paths"])

    def test_聊天路由只在参数能唯一冻结时直接执行(self) -> None:
        question = "84级 Shaper Ball，6000血、75冰抗，能扛得住吗"
        self.assertEqual("boss_survival_check", loop._preferred_tool_route(question))
        self.assertEqual({
            "skill": "Shaper Ball", "level": 84, "pool": 6000,
            "cold_resist_pct": 75,
        }, loop._boss_survival_args(question))
        self.assertIsNone(loop._boss_survival_args("Shaper Ball 能扛得住吗"))


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
