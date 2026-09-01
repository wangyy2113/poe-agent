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


# ── 需求 8.4 体检报告（第四十九轮）────────────────────────────


class RequirementCoverageTest(unittest.TestCase):
    """「检查并报告下列全部项目，**不得遗漏**」是需求原文，靠代码保证。"""

    def test_十六项一个不缺(self) -> None:
        from parsers.build_report import REQUIREMENT_8_4_ITEMS, uncovered_requirements

        self.assertEqual(len(REQUIREMENT_8_4_ITEMS), 16, "需求 8.4 点名 16 项")
        self.assertEqual(uncovered_requirements(), (), "有需求项没有任何检查覆盖")

    def test_判据落在需求那一侧(self) -> None:
        """**不能写成 `REQUIRED = len(_CHECKS)`**——那等于「要求的项数 = 我实现了几项」，
        恒等于自己、永远不会失败。第一版就是这么写的，实测它报 18 而需求是 16。"""
        import inspect

        from parsers import build_report

        src = inspect.getsource(build_report)
        self.assertNotIn("= len(_CHECKS)", src)


class HealthReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.files = _fixtures()
        if not self.files:
            self.skipTest("PoB 检出不可用，跳过夹具用例")
        import sqlite3

        from calc import constants
        from core.db import SCHEMA

        constants.reset_cache()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # 判据用的三个上限——与 `test_resistance.fixture_conn` 同一套打桩方式
        self.conn.executemany(
            "INSERT INTO game_constant VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            [
                (key, "pobMisc", None, 0, "scalar", value, None,
                 "Path of Building", 2, "https://example/blob/Data.lua",
                 "PoB 2.66.2 / tree 3.29", "3.29")
                for key, value in (
                    (constants.MAX_RESIST_CAP, 90.0),
                    (constants.BLOCK_CHANCE_CAP, 90.0),
                    (constants.SUPPRESSION_CHANCE_CAP, 100.0),
                )
            ],
        )
        self.conn.commit()
        constants.load(self.conn, force=True)

    def tearDown(self) -> None:
        from calc import constants

        constants.reset_cache()
        self.conn.close()

    def _report(self, path: Path):
        from parsers.build_report import build_health_report
        from parsers.pob_build import parse_build

        return build_health_report(self.conn, parse_build(path.read_text(encoding="utf-8")))

    def test_缺失项标未知且不填估计值(self) -> None:
        """8.4.7 的原文：缺失 SHALL 标注为「未知」**而非填入估计值**。

        3.13 夹具里法术压制/EHP/MaxHit/保留都没有（压制是 3.16 才加的），
        正好是这条路径的天然样本。
        """
        report = self._report(self.files[-1])
        for item in report.items:
            if item.known:
                continue
            with self.subTest(item=item.key):
                self.assertEqual(item.value, "", "未知项必须留空，不许填估计值")
                self.assertTrue(item.missing_reason, "未知必须说清缺的是哪一项")

    def test_法术压制在_313_夹具上是未知(self) -> None:
        item = self._report(self.files[-1]).by_key("spell_suppression")
        self.assertIsNotNone(item)
        self.assertFalse(item.known)
        self.assertIn("SpellSuppressionChance", item.missing_reason)

    def test_超抗要写进抗性(self) -> None:
        """超抗顶得住减抗与诅咒、顶不住穿透，是 `calc/resistance` 的既有结论，
        报告里不能把它吞掉。"""
        item = self._report(self.files[-1]).by_key("fire_resist")
        self.assertIn("超抗", item.value)

    def test_护甲不给减伤百分比(self) -> None:
        """护甲不是固定百分比减伤，不给这一击多大就没有减伤可言——
        与 `num_armour_reduction` / `gap_armour_without_damage` 那对用例同一条规矩。"""
        item = self._report(self.files[-1]).by_key("armour")
        self.assertNotIn("%", item.value)
        self.assertIn("这一击", item.basis)

    def test_dps_只复述不计算(self) -> None:
        item = self._report(self.files[-1]).by_key("dps")
        self.assertIn("不计算 DPS", item.basis)

    def test_珠宝按树上插槽而不是装备栏(self) -> None:
        """实测 OccVortex：装备 Slot 里一件珠宝都没有，而树上插着 3 颗。
        按 Slot 名找会得出「没有珠宝」这个错结论。"""
        occ = next((p for p in self.files if p.stem == "OccVortex"), None)
        if occ is None:
            self.skipTest("没有 OccVortex 夹具")
        item = self._report(occ).by_key("jewels")
        self.assertIn("3", item.value)

    def test_万神殿没开不等于未知(self) -> None:
        item = self._report(self.files[-1]).by_key("pantheon")
        self.assertTrue(item.known, "没开是一个确定的事实，不是「未知」")
        self.assertIn("未启用", item.value)

    def test_五个夹具都能出完整报告(self) -> None:
        from parsers.build_report import uncovered_requirements

        for path in self.files:
            with self.subTest(build=path.stem):
                report = self._report(path)
                self.assertEqual(uncovered_requirements(), ())
                self.assertTrue(report.notes, "报告必须带来源说明")
                self.assertTrue(
                    any("没有重算" in n for n in report.notes),
                    "必须说清本库没有重算任何一项",
                )


# ── 分享链接（第五十轮）─────────────────────────────────────


class BuildLinkTest(unittest.TestCase):
    """站点表逐条抄自 PoB `Modules/BuildSiteTools.lua`，不凭记忆。"""

    def resolve(self, link: str) -> str:
        from parsers.pob_link import resolve_download_url

        return resolve_download_url(link)

    def test_pobb_in_的下载地址不是_raw_结尾(self) -> None:
        """**照记忆写会错**：pobb.in 的下载地址是 `/pob/<id>`，不是 `/<id>/raw`。
        这条正是「能钉住上游原文就别凭印象」的现成例子。"""
        self.assertEqual(
            self.resolve("https://pobb.in/abc123"), "https://pobb.in/pob/abc123")

    def test_各站点按_pob_的表解析(self) -> None:
        cases = {
            "https://pastebin.com/AbC123": "https://pastebin.com/raw/AbC123",
            "https://pob.codes/b/xyz": "https://api.pob.codes/xyz/raw",
            "https://rentry.co/abc": "https://rentry.co/paste/abc/raw",
            "https://maxroll.gg/poe/pob/deadbeef": "https://maxroll.gg/poe/api/pob/deadbeef",
            "https://poedb.tw/pob/zzz": "https://poedb.tw/pob/zzz/raw",
        }
        for link, expected in cases.items():
            with self.subTest(link=link):
                self.assertEqual(self.resolve(link), expected)

    def test_没有协议头也能认(self) -> None:
        self.assertEqual(self.resolve("pobb.in/abc"), "https://pobb.in/pob/abc")

    def test_poe_ninja_只认_poe1_路径(self) -> None:
        """poe.ninja 两款游戏都服务。POE1/POE2 边界顺带落在这张表上：
        `poe2` 那一段匹配不上，自然被挡在外面。"""
        from parsers.pob_link import PobLinkError

        self.assertEqual(
            self.resolve("https://poe.ninja/poe1/pob/abc123"),
            "https://poe.ninja/poe1/pob/raw/abc123")
        with self.assertRaises(PobLinkError):
            self.resolve("https://poe.ninja/poe2/pob/abc123")

    def test_白名单之外一律拒绝(self) -> None:
        """**这是白名单不是黑名单**：URL 由用户给、服务端拿它去发请求，
        放开等于允许指使本服务访问任意地址（内网、云元数据端点都在射程内）。"""
        from parsers.pob_link import PobLinkError

        for bad in (
            "https://evil.example.com/x",
            "http://169.254.169.254/latest/meta-data/",
            "https://localhost:8787/admin",
            "file:///etc/passwd",
        ):
            with self.subTest(url=bad), self.assertRaises(PobLinkError):
                self.resolve(bad)

    def test_拒绝时列出支持的站点(self) -> None:
        from parsers.pob_link import PobLinkError

        with self.assertRaises(PobLinkError) as ctx:
            self.resolve("https://evil.example.com/x")
        self.assertIn("pobb.in", str(ctx.exception))

    def test_分享码不会被当成链接(self) -> None:
        from parsers.pob_link import looks_like_link

        self.assertFalse(looks_like_link("eJy9XVtzm8i2fh7_CspV-7yMbPcdOifZu"))
        self.assertTrue(looks_like_link("https://pobb.in/abc"))
        self.assertTrue(looks_like_link("pobb.in/abc"))
