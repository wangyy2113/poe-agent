"""游戏常量访问层的单元测试。

重点验证「缺失即抛异常，绝不给默认值」——静默的错误常量会让整条防御结论反向
且不报错，是本项目第一原则要拦的失败模式。

用内存 SQLite 打桩，不依赖真实库。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from calc import constants  # noqa: E402

SCHEMA = """
CREATE TABLE game_constant (
    key TEXT PRIMARY KEY, group_name TEXT NOT NULL, dat_source TEXT,
    dat_source_inherited INTEGER DEFAULT 0, kind TEXT NOT NULL,
    value_num REAL, value_json TEXT, source TEXT NOT NULL,
    source_priority INTEGER NOT NULL, url TEXT NOT NULL,
    pob_version TEXT, game_version TEXT, updated_at TEXT NOT NULL
);
"""


def fixture_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    rows = [
        (
            constants.MAX_ALL_RESISTANCES, "characterConstants",
            "Metadata/Characters/Character.ot", 0, "scalar", 75.0, None,
            "Path of Building", 2, "https://github.com/x/blob/abc/src/Data/Misc.lua#L62",
            "PoB 2.66.2 / tree 3.29", "3.29",
        ),
        (
            constants.ES_RATIO_OF_LIFE, "gameConstants", "GameConstants.dat", 0,
            "scalar", 40.0, None, "Path of Building", 2,
            "https://github.com/x/blob/abc/src/Data/Misc.lua#L17",
            "PoB 2.66.2 / tree 3.29", "3.29",
        ),
        (
            constants.MONSTER_LIFE_TABLE, "monsterLifeTable",
            "DefaultMonsterStats.dat", 1, "level_table", None,
            json.dumps([22.0, 26.0, 31.0]), "Path of Building", 2,
            "https://github.com/x/blob/abc/src/Data/Misc.lua#L7",
            "PoB 2.66.2 / tree 3.29", "3.29",
        ),
    ]
    conn.executemany(
        "INSERT INTO game_constant VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        rows,
    )
    conn.commit()
    return conn


class ConstantsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        constants.reset_cache()
        self.conn = fixture_conn()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        constants.reset_cache()
        self.conn.close()


class TestNoSilentDefaults(ConstantsTestCase):
    """最关键的一组：绝不能有默认值。"""

    def test_缺失常量抛异常而非返回默认值(self):
        with self.assertRaises(constants.MissingConstant):
            constants.get("characterConstants.不存在的常量")

    def test_异常消息提示不要用默认值兜底(self):
        try:
            constants.get("nope")
        except constants.MissingConstant as exc:
            self.assertIn("不要用默认值兜底", str(exc))

    def test_等级越界抛异常而非截断(self):
        """静默返回最后一级的值会让「超出表范围」这个事实消失。"""
        with self.assertRaises(constants.MissingConstant):
            constants.at_level(constants.MONSTER_LIFE_TABLE, 99)
        with self.assertRaises(constants.MissingConstant):
            constants.at_level(constants.MONSTER_LIFE_TABLE, 0)

    def test_标量当数组取会抛类型异常(self):
        with self.assertRaises(constants.ConstantTypeError):
            constants.table(constants.MAX_ALL_RESISTANCES)

    def test_数组当标量取会抛类型异常(self):
        with self.assertRaises(constants.ConstantTypeError):
            constants.get(constants.MONSTER_LIFE_TABLE)


class TestValues(ConstantsTestCase):
    def test_取标量(self):
        self.assertEqual(constants.get(constants.MAX_ALL_RESISTANCES), 75.0)
        self.assertEqual(constants.get(constants.ES_RATIO_OF_LIFE), 40.0)

    def test_取等级表(self):
        self.assertEqual(constants.table(constants.MONSTER_LIFE_TABLE), [22.0, 26.0, 31.0])

    def test_按等级取值是1based(self):
        """等级从 1 开始，与游戏一致。"""
        self.assertEqual(constants.at_level(constants.MONSTER_LIFE_TABLE, 1), 22.0)
        self.assertEqual(constants.at_level(constants.MONSTER_LIFE_TABLE, 3), 31.0)


class TestProvenance(ConstantsTestCase):
    def test_每个常量都带GGG原始表名(self):
        p = constants.provenance(constants.MAX_ALL_RESISTANCES)
        self.assertEqual(p.dat_source, "Metadata/Characters/Character.ot")
        self.assertFalse(p.inherited)
        self.assertEqual(p.game_version, "3.29")

    def test_引用URL钉住commit(self):
        p = constants.provenance(constants.ES_RATIO_OF_LIFE)
        self.assertIn("/blob/abc/", p.url)
        self.assertTrue(p.url.startswith("http"))

    def test_推断出处会被标记(self):
        """出处是从上文注释延续推断的要能识别——错的出处比没有出处更糟。"""
        p = constants.provenance(constants.MONSTER_LIFE_TABLE)
        self.assertTrue(p.inherited)
        self.assertIn("出处为推断", p.render())

    def test_渲染含来源与版本(self):
        text = constants.provenance(constants.MAX_ALL_RESISTANCES).render()
        self.assertIn("Path of Building", text)
        self.assertIn("Character.ot", text)
        self.assertIn("3.29", text)


class TestCompleteness(ConstantsTestCase):
    def test_missing列出缺失的必需常量(self):
        gaps = constants.missing()
        self.assertNotIn(constants.MAX_ALL_RESISTANCES, gaps)
        self.assertIn(constants.MAX_BLOCK, gaps)

    def test_REQUIRED里没有重复(self):
        self.assertEqual(len(constants.REQUIRED), len(set(constants.REQUIRED)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
