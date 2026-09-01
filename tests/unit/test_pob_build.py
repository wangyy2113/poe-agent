"""PoB 分享码解析（第四十八轮）。

夹具用 PoB 检出里的 `spec/TestBuilds/3.13/*.xml`（5 个真实 build、100 件装备）。
**检出不在时整类跳过而不是失败**：`POB_PATH` 不一定存在，
`scripts/probe_pob.py` 早有这个先例。

这一轮的核心回归是**PoB 存档方言**：那种格式不带 `--------`，改用 `Implicits: N`
标边界，不认它的话 100 件里有 99 件词缀全丢——名字/底材/稀有度都对，
偏偏丢的是后面 T 级匹配与 build 分析要用的那一半。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from parsers.item_text import parse_item  # noqa: E402
from parsers.pob_build import (  # noqa: E402
    PobParseError,
    decode_share_code,
    encode_share_code,
    parse_build,
)

_FIXTURES = Path("../PathOfBuilding/spec/TestBuilds/3.13")


def _fixtures() -> list[Path]:
    base = (Path(__file__).resolve().parents[2] / _FIXTURES).resolve()
    return sorted(base.glob("*.xml")) if base.is_dir() else []


class ShareCodeTest(unittest.TestCase):
    """编码方式取自 PoB `ImportTab.lua:504/631`：zlib deflate + URL-safe base64。"""

    def test_往返一致(self) -> None:
        xml = "<PathOfBuilding><Build level='90' className='Witch'/></PathOfBuilding>"
        self.assertEqual(decode_share_code(encode_share_code(xml)), xml)

    def test_url_safe_字符能解(self) -> None:
        """PoB 把 `+`/`/` 换成 `-`/`_`，解码要换回来。"""
        xml = "<PathOfBuilding><Build level='1'/></PathOfBuilding>" * 20
        code = encode_share_code(xml)
        self.assertNotIn("+", code)
        self.assertNotIn("/", code)
        self.assertEqual(decode_share_code(code), xml)

    def test_链接给出明确指引而不是_base64_报错(self) -> None:
        with self.assertRaises(PobParseError) as ctx:
            decode_share_code("https://pobb.in/abcdef")
        self.assertIn("链接", str(ctx.exception))

    def test_三种失败各给各的原因(self) -> None:
        """「解析失败」四个字对用户没有任何帮助。"""
        with self.assertRaises(PobParseError) as empty:
            decode_share_code("   ")
        self.assertIn("空", str(empty.exception))

        with self.assertRaises(PobParseError) as bad_char:
            decode_share_code("不是base64的中文")
        self.assertIn("base64", str(bad_char.exception))

        with self.assertRaises(PobParseError) as not_zlib:
            decode_share_code("aGVsbG8gd29ybGQ")  # 合法 base64，但不是 zlib
        self.assertIn("zlib", str(not_zlib.exception))

    def test_不是_pob_的_xml_要说清楚(self) -> None:
        with self.assertRaises(PobParseError) as ctx:
            parse_build("<Foo><Bar/></Foo>")
        self.assertIn("PathOfBuilding", str(ctx.exception))


class PobItemDialectTest(unittest.TestCase):
    """PoB 存档方言：没有 `--------`，用 `Implicits: N` 标边界。"""

    BODY = (
        "Rarity: RARE\n"
        "Loath Sanctuary\n"
        "Vaal Spirit Shield\n"
        "Item Level: 83\n"
        "Sockets: B-B-B\n"
        "LevelReq: 70\n"
        "Implicits: 1\n"
        "9% increased Spell Damage\n"
        "+54 to Intelligence\n"
        "+87 to maximum Life\n"
        "Corrupted\n"
    )

    def test_隐式与显式按计数切开(self) -> None:
        item = parse_item(self.BODY)
        self.assertEqual([m.text for m in item.implicits], ["9% increased Spell Damage"])
        self.assertEqual(
            [m.text for m in item.explicits],
            ["+54 to Intelligence", "+87 to maximum Life"],
        )

    def test_头部属性不再被吞掉(self) -> None:
        """PoB 把 `Item Level` / `Sockets` 和名字挤在同一段，
        而 `parse_item` 只解析 `sections[1:]` 的属性——不把头部再切一刀就全留空。"""
        item = parse_item(self.BODY)
        self.assertEqual(item.item_level, 83)
        self.assertTrue(item.sockets.raw)
        self.assertEqual(item.requirements.level, 70, "LevelReq 是 PoB 对同一概念的键名")

    def test_旗标仍然识别(self) -> None:
        self.assertTrue(parse_item(self.BODY).flags.corrupted)

    def test_零隐式(self) -> None:
        body = self.BODY.replace("Implicits: 1\n9% increased Spell Damage\n", "Implicits: 0\n")
        item = parse_item(body)
        self.assertEqual(item.implicits, [])
        self.assertEqual(len(item.explicits), 2)

    def test_游戏原文格式不受影响(self) -> None:
        """带 `--------` 的照旧走老路径——同一个解析器认两种方言，不是换掉一种。"""
        game = (
            "Item Class: Gloves\nRarity: RARE\nFoo Bar\nDragonscale Gauntlets\n"
            "--------\nItem Level: 84\n--------\n+42 to maximum Life\n"
        )
        item = parse_item(game)
        self.assertEqual(item.item_class, "Gloves")
        self.assertEqual(item.item_level, 84)
        self.assertEqual([m.text for m in item.explicits], ["+42 to maximum Life"])


class FixtureTest(unittest.TestCase):
    """拿 PoB 自己的 5 个 build 夹具做端到端验证。"""

    def setUp(self) -> None:
        self.files = _fixtures()
        if not self.files:
            self.skipTest("PoB 检出不可用（POB_PATH 未配置），跳过夹具用例")

    def test_五个夹具都能往返并解析(self) -> None:
        for path in self.files:
            with self.subTest(build=path.stem):
                xml = path.read_text(encoding="utf-8")
                self.assertEqual(decode_share_code(encode_share_code(xml)), xml)
                build = parse_build(xml)
                self.assertTrue(build.class_name, "职业必须解析出来")
                self.assertTrue(build.skill_groups, "技能组不能为空")
                self.assertTrue(build.items, "装备不能为空")

    def test_一百件装备全部解析出词缀(self) -> None:
        """**这一轮的核心回归**：改之前 100 件里有 99 件一条词缀都取不到。"""
        total = without_mods = 0
        for path in self.files:
            for item in parse_build(path.read_text(encoding="utf-8")).items:
                total += 1
                self.assertIsNotNone(item.parsed, f"{item.slot} 解析失败：{item.error}")
                if item.parsed is not None and not item.parsed.all_mods:
                    without_mods += 1
        self.assertGreaterEqual(total, 100)
        self.assertEqual(without_mods, 0, "PoB 存档方言下不该有装备丢掉全部词缀")

    def test_playerstat_被原样收下(self) -> None:
        """它是 PoB 自己算的快照——收下但要标清来源（见 `api/build.py`）。"""
        build = parse_build(self.files[0].read_text(encoding="utf-8"))
        self.assertTrue(build.player_stats, "夹具里带着 PlayerStat")
        for value in build.player_stats.values():
            self.assertIsInstance(value, float)


class BridgeTest(unittest.TestCase):
    """宝石桥接：桥不上**留原文并记 warning**，不猜。"""

    def test_桥不上的留原文并告警(self) -> None:
        import sqlite3

        from parsers.pob_build import bridge_gems

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE pob_skill_gem(gem_id TEXT, name_en TEXT, name_zh TEXT)")
        conn.execute("INSERT INTO pob_skill_gem VALUES('IceNova','Ice Nova','冰霜新星')")
        xml = (
            "<PathOfBuilding><Build level='90' className='Witch'/><Skills>"
            "<Skill mainActiveSkill='1'>"
            "<Gem nameSpec='Ice Nova' level='20' quality='20'/>"
            "<Gem nameSpec='Nonexistent Gem' level='1'/>"
            "</Skill></Skills></PathOfBuilding>"
        )
        build = bridge_gems(conn, parse_build(xml))
        gems = build.skill_groups[0].gems
        self.assertEqual(gems[0].gem_id, "IceNova")
        self.assertEqual(gems[0].name_zh, "冰霜新星")
        self.assertIsNone(gems[1].gem_id, "桥不上就是 None")
        self.assertEqual(gems[1].name_en, "Nonexistent Gem", "原文要留着")
        self.assertTrue(any("未能与本库桥接" in w for w in build.warnings))


if __name__ == "__main__":
    unittest.main()
