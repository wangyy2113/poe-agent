"""技能基础伤害：effectiveness 公式与两个必须照上游来的细节（2026-08-20 第二十九轮）。

这一轮解掉了项目最老的一条自我限制（`pob_skill_level` 从第十二轮起就刻意没有伤害列）。
放行的条件不是「写出公式」，而是**有对账口径**：POEDB 的技能页把区间写成
「造成 (25 — 1122) - (38 — 1683) 冰霜伤害」，括号里正好是宝石 1 级 → 自然最高级，
`scripts/verify_gem_damage.py` 逐个对账，20 个技能 20/20 一致。

两个细节是实测撞出来的，都在用例里钉住：

1. **同名 stat 要累加**——「传输之赤炼魔光」把最小伤害写了两次（0.8 与 −0.64）。
   只取第一项会算大 5 倍。
2. **先各自 round 再相加**——同一颗宝石：round 后相加得 4 / 268 / 401（与页面一致），
   相加后 round 得 6 / 267（两处都错）。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import skill_damage as sd  # noqa: E402
from core.db import SCHEMA  # noqa: E402
from tests.unit.test_resistance import POB_ROOT_ENV  # noqa: E402

#: 两个 gameConstants 的真值（`Data/Misc.lua`）。
GAME_BASE = 3.885209
GAME_INCREMENTAL = 0.360246

#: 冰霜新星：baseEffectiveness / incrementalEffectiveness 与两个乘数。
ICE_NOVA = dict(
    stat_names={1: "spell_minimum_base_cold_damage", 2: "spell_maximum_base_cold_damage",
                3: "active_skill_base_area_of_effect_radius"},
    level_values={1: 0.80000001192093, 2: 1.2000000476837, 3: 4},
    interpolation={1: 3, 2: 3, 3: 1},
    base_effectiveness=2.5009999275208,
    incremental_effectiveness=0.043999999761581,
)
#: 传输之赤炼魔光：同名 stat 出现两次（第 4、5 项是负的）。
EXSANGUINATE = dict(
    stat_names={
        1: "spell_minimum_base_physical_damage", 2: "spell_maximum_base_physical_damage",
        3: "base_physical_damage_to_deal_per_minute",
        4: "spell_minimum_base_physical_damage", 5: "spell_maximum_base_physical_damage",
        6: "number_of_chains",
    },
    level_values={1: 0.80000001192093, 2: 1.2000000476837, 3: 76.666666790843,
                  4: -0.63999998569489, 5: -0.95999997854233, 6: 10},
    interpolation={1: 3, 2: 3, 3: 3, 4: 3, 5: 3, 6: 1},
    base_effectiveness=1.7024999856949,
    incremental_effectiveness=0.052499998360872,
)


def damage(spec: dict, actor_level: int):  # noqa: ANN201
    return sd.base_damage(
        spec["stat_names"], spec["level_values"], spec["interpolation"],
        actor_level=actor_level,
        base_effectiveness=spec["base_effectiveness"],
        incremental_effectiveness=spec["incremental_effectiveness"],
        game_base=GAME_BASE, game_incremental=GAME_INCREMENTAL,
    )


class ReconcileWithPoedbTest(unittest.TestCase):
    """与 POEDB 技能页对账过的真值。"""

    def test_冰霜新星两端都对得上(self) -> None:
        """页面：造成 (25 — 1122) - (38 — 1683) 冰霜伤害。"""
        low = damage(ICE_NOVA, 12)   # 1 级的 levelRequirement
        high = damage(ICE_NOVA, 70)  # 20 级的 levelRequirement
        self.assertEqual((25, 38), (low.minimum, low.maximum))
        self.assertEqual((1122, 1683), (high.minimum, high.maximum))
        self.assertEqual("cold", high.damage_type)

    def test_同名stat要累加(self) -> None:
        """页面：(4 — 268) - (5 — 401)。只取第一项会得到 19/1337——大 5 倍。"""
        low = damage(EXSANGUINATE, 12)
        high = damage(EXSANGUINATE, 70)
        self.assertEqual((4, 5), (low.minimum, low.maximum))
        self.assertEqual((268, 401), (high.minimum, high.maximum))

    def test_先round再相加而不是反过来(self) -> None:
        """相加后 round 会得到 6 / 267，两处都与页面不符。"""
        spec = EXSANGUINATE
        eff = sd.effectiveness(
            70, spec["base_effectiveness"], spec["incremental_effectiveness"],
            game_base=GAME_BASE, game_incremental=GAME_INCREMENTAL,
        )
        naive = round(eff * (spec["level_values"][1] + spec["level_values"][4]))
        self.assertEqual(267, naive)
        self.assertEqual(268, damage(spec, 70).minimum)


class FormulaShapeTest(unittest.TestCase):
    def test_只认插值模式三(self) -> None:
        """模式 1/2 是线性插值，走另一条路——混进来会把半径当成伤害。"""
        spec = dict(ICE_NOVA)
        spec["interpolation"] = {1: 1, 2: 1, 3: 1}
        self.assertIsNone(damage(spec, 70))

    def test_没有最大伤害那一项就不算(self) -> None:
        spec = dict(ICE_NOVA)
        spec["stat_names"] = {1: "spell_minimum_base_cold_damage"}
        self.assertIsNone(damage(spec, 70))

    def test_等级越高伤害越高(self) -> None:
        values = [damage(ICE_NOVA, lvl).minimum for lvl in (12, 40, 70)]
        self.assertEqual(values, sorted(values))

    def test_伤害类型从stat名里取(self) -> None:
        for stat, expect in (("spell_minimum_base_fire_damage", "fire"),
                             ("attack_minimum_base_lightning_damage", "lightning"),
                             ("minimum_base_chaos_damage", "chaos")):
            with self.subTest(stat=stat):
                spec = dict(ICE_NOVA)
                spec["stat_names"] = {1: stat, 2: stat.replace("minimum", "maximum")}
                self.assertEqual(expect, damage(spec, 70).damage_type)


@unittest.skipUnless(POB_ROOT_ENV.is_dir(), "本机没有 PathOfBuilding 检出")
class FormulaPinnedToUpstream(unittest.TestCase):
    def test_公式原文还在给出的行号范围内(self) -> None:
        text = (POB_ROOT_ENV / "src" / "Modules" / "CalcTools.lua").read_text(encoding="utf-8")
        block = "\n".join(text.splitlines()[sd.FORMULA_LINES[0] - 1: sd.FORMULA_LINES[1]])
        self.assertIn('data.gameConstants["SkillDamageBaseEffectiveness"]', block)
        self.assertIn('data.gameConstants["SkillDamageIncrementalEffectiveness"]', block)
        self.assertIn("(grantedEffect.baseEffectiveness or 1)", block)
        self.assertIn("(1 + (grantedEffect.incrementalEffectiveness or 0)) ^ (actorLevel - 1)", block)
        self.assertIn("statValue = round(availableEffectiveness * level[index])", block)

    def test_同名stat累加那一行还在(self) -> None:
        text = (POB_ROOT_ENV / "src" / "Modules" / "CalcTools.lua").read_text(encoding="utf-8")
        self.assertIn("stats[stat] = (stats[stat] or 0) + statValue", text)


class IngestedColumnsTest(unittest.TestCase):
    """入库的是**算好的显示值**，不是乘数。"""

    def test_表里有三列而且注释说清了理由(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pob_skill_level)")}
        for column in ("base_damage_min", "base_damage_max", "damage_type"):
            self.assertIn(column, cols)
        conn.close()

    def test_卡片把基础伤害写出来(self) -> None:
        """数据入库但没有出口等于没入库（第二十四轮的教训）。"""
        from kb.render_skills import level_line

        line = level_line({
            "gem_level": 20, "base_damage_min": 1659.0, "base_damage_max": 2488.0,
            "damage_type": "cold",
        })
        self.assertIn("基础冰霜伤害 1659–2488", line)


if __name__ == "__main__":
    unittest.main()
