"""Boss 技能伤害：解析、tooltip 抽验、公式与边界（2026-08-27 第三十八轮）。

补的是**伤害建模**不是打法——这条分界要在测试里也钉住，否则下一个人会以为
这个域已经能回答「谢勒怎么打」。

公式钉住上游原文（`Modules/ConfigOptions.lua:2258`）：数据漂移有
`check_doc_facts.py` 拦，**公式漂移只能靠钉原文**（与 `test_resistance.py` 同一做法）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import boss_damage  # noqa: E402
from calc import constants  # noqa: E402
from core.db import SCHEMA  # noqa: E402
from kb.sources.pob_bosses import (  # noqa: E402
    BossSourceError,
    parse_boss_skills,
    parse_bosses,
)

_SKILLS_LUA = '''
return {
	["Shaper Ball"] = {
		DamageType = "SpellProjectile",
		DamageMultipliers = {
			Cold = { 11.668066430448, 0.058340332152239 }
		},
		DamagePenetrations = {
			ColdPen = 25
		},
		UberDamagePenetrations = {
			ColdPen = 40
		},
		speed = 1400,
		tooltip = "Allocating Cosmic Wounds increases the penetration to 40% (Applied on Uber)"
	},
	["Maven Fireball"] = {
		DamageType = "SpellProjectile",
		DamageMultipliers = {
			Fire = { 14.977416270256, 0.074887081351278 }
		},
		UberDamageMultiplier = 2.0273275862069,
		DamagePenetrations = {
			FirePen = ""
		},
		UberDamagePenetrations = {
			FirePen = 30
		},
		speed = 3000,
	},
}
'''

_BOSSES_LUA = '''
local bosses = ...
bosses["Shaper"] = {
	armourMult = 0,
	evasionMult = 33,
	isUber = true,
}
bosses["Maven"] = {
	armourMult = 25,
	evasionMult = 0,
	isUber = true,
}
'''

#: 怪物伤害表的桩：只放用例需要的等级，其余填 0 占位。
_TABLE = [0.0] * 100
_TABLE[83] = 821.73  # 84 级（下标从 0 起）


class ParseTest(unittest.TestCase):
    def test_解析技能与倍率(self) -> None:
        skills, stats = parse_boss_skills(_SKILLS_LUA)
        self.assertEqual(2, stats.skills)
        ball = next(s for s in skills if s.name == "Shaper Ball")
        self.assertEqual("SpellProjectile", ball.damage_type)
        self.assertAlmostEqual(11.668066430448, ball.damage_mults["Cold"][0])
        self.assertEqual({"ColdPen": 25.0}, ball.penetrations)
        self.assertEqual({"ColdPen": 40.0}, ball.uber_penetrations)

    def test_上游写成空串的穿透按没有这一项处理并报数(self) -> None:
        """`FirePen = ""` 在 BossSkills.lua 里是真实写法（PoB 拿它当「这一栏不填」）。

        写成 0 会让人以为我们确认过「不穿透」，而那是上游留空。
        """
        skills, stats = parse_boss_skills(_SKILLS_LUA)
        maven = next(s for s in skills if s.name == "Maven Fireball")
        self.assertEqual({}, maven.penetrations)
        self.assertEqual({"FirePen": 30.0}, maven.uber_penetrations)
        self.assertEqual(1, len(stats.blank_penetrations))
        self.assertIn("Maven Fireball", stats.blank_penetrations[0])

    def test_tooltip抽验对得上(self) -> None:
        """tooltip 里复述了穿透数字，与结构化字段比一比。**抽验不是全量。**"""
        _skills, stats = parse_boss_skills(_SKILLS_LUA)
        self.assertEqual(1, stats.tooltip_checked)  # 只有 Shaper Ball 那条能抽验
        self.assertEqual(1, stats.tooltip_matched)
        self.assertEqual([], stats.tooltip_mismatch)

    def test_tooltip与字段不一致要报出来(self) -> None:
        broken = _SKILLS_LUA.replace("penetration to 40%", "penetration to 55%")
        _skills, stats = parse_boss_skills(broken)
        self.assertEqual(1, len(stats.tooltip_mismatch))
        self.assertIn("Shaper Ball", stats.tooltip_mismatch[0])

    def test_倍率不是两项时报错(self) -> None:
        broken = _SKILLS_LUA.replace(
            "Cold = { 11.668066430448, 0.058340332152239 }", "Cold = { 11.668066430448 }"
        )
        with self.assertRaisesRegex(BossSourceError, "预期 2 项"):
            parse_boss_skills(broken)

    def test_解析boss受击系数(self) -> None:
        bosses = parse_bosses(_BOSSES_LUA)
        by_key = {b.key: b for b in bosses}
        self.assertEqual(33.0, by_key["Shaper"].evasion_mult)
        self.assertEqual(25.0, by_key["Maven"].armour_mult)
        self.assertTrue(by_key["Shaper"].is_uber)


class FormulaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        import json

        self.conn.execute(
            "INSERT INTO game_constant(key, group_name, kind, value_json, source,"
            " source_priority, url, updated_at)"
            " VALUES ('monsterDamageTable','monsterDamageTable','level_table',?,"
            "'Path of Building',2,'https://example/blob',datetime('now'))",
            (json.dumps(_TABLE),),
        )
        self.conn.commit()
        self._real = constants.get_conn if hasattr(constants, "get_conn") else None
        from core import db as dbmod

        self._real_conn = dbmod.get_conn
        dbmod.get_conn = lambda: self.conn  # type: ignore[assignment]
        constants.invalidate_cache() if hasattr(constants, "invalidate_cache") else None

    def tearDown(self) -> None:
        from core import db as dbmod

        dbmod.get_conn = self._real_conn  # type: ignore[assignment]
        self.conn.close()

    def test_按上游原式算一击(self) -> None:
        hit = boss_damage.hit_damage(
            level=84, base_mult=11.668066430448, roll_mult=0.058340332152239,
            damage_type="Cold", roll_range=100,
        )
        self.assertEqual(14382, hit.damage)

    def test_掷骰区间两端且下界不高于上界(self) -> None:
        """「打多少伤害」的诚实答案是区间——不替用户挑一个点。"""
        low, high = boss_damage.hit_range(
            level=84, base_mult=11.668066430448, roll_mult=0.058340332152239,
            damage_type="Cold",
        )
        self.assertLess(low.damage, high.damage)
        self.assertEqual(0, low.roll_range)
        self.assertEqual(100, high.roll_range)

    def test_uber不低于非uber(self) -> None:
        kw = dict(level=84, base_mult=14.977416270256, roll_mult=0.074887081351278,
                  damage_type="Fire", roll_range=100)
        plain = boss_damage.hit_damage(**kw)
        uber = boss_damage.hit_damage(**kw, uber_mult=2.0273275862069)
        self.assertGreater(uber.damage, plain.damage)

    def test_等级越界抛异常不外推(self) -> None:
        """外推出来的数字看起来完全正常，而它没有任何依据。"""
        with self.assertRaisesRegex(boss_damage.BossDamageError, "不外推"):
            boss_damage.hit_damage(
                level=200, base_mult=1, roll_mult=0, damage_type="Fire", roll_range=0
            )

    def test_掷骰区间越界抛异常(self) -> None:
        with self.assertRaises(boss_damage.BossDamageError):
            boss_damage.hit_damage(
                level=84, base_mult=1, roll_mult=0, damage_type="Fire", roll_range=150
            )


class UpstreamAnchorTest(unittest.TestCase):
    """公式必须钉住上游原文——数据漂移有工具拦，公式漂移没有。"""

    def _pob_root(self) -> Path | None:
        raw = os.environ.get("POB_PATH")
        candidates = [Path(raw)] if raw else []
        candidates.append(Path(__file__).resolve().parents[3] / "PathOfBuilding")
        for path in candidates:
            if (path / "src" / "Modules" / "ConfigOptions.lua").is_file():
                return path
        return None

    def test_上游原式仍在且行号锚点指向它(self) -> None:
        root = self._pob_root()
        if root is None:
            self.skipTest("本机没有 PoB 检出")
        text = (root / "src" / "Modules" / "ConfigOptions.lua").read_text(encoding="utf-8")
        self.assertIn("data.monsterDamageTable[build.configTab.enemyLevel]", text)
        self.assertIn("damageMult[1] + rollRangeMult * damageMult[2]", text)
        # `calc/boss_damage.FORMULA_SOURCE` 里写死的行号要真的指向那一行
        line_no = int(boss_damage.FORMULA_SOURCE.rsplit(":", 1)[1])
        line = text.splitlines()[line_no - 1]
        self.assertIn("damageMult[1] + rollRangeMult * damageMult[2]", line)


if __name__ == "__main__":
    unittest.main()
