"""怪物词缀与幽魂倍率：抽取、身份键、卡片与工具边界（2026-08-19）。

全部离线：HTML 在内存里构造，不读 `data/http_cache`、不连网。

判据几乎每条都来自实测反例：
`<br>` 不规范化会让属性粘成一句、未翻译的内部 stat id 不能当中文答案、
tooltip 哈希不是种类 id（会撞）、同名同来源的词缀有多档（只用名字做 id 会覆盖掉）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from entities.models import Entity, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, Source  # noqa: E402
from kb import render_monsters  # noqa: E402
from kb.sources.poedb_monsters import (  # noqa: E402
    SKIP_NO_TEXT,
    SKIP_RAW_ONLY,
    MonsterStructureError,
    extract,
    parse_monster_mods,
    parse_spectres,
)
from tools.impl.monsters import MAX_ROWS, spectre_stats  # noqa: E402


def mod_table(rows: str) -> str:
    return (
        "<table><thead><tr><th>类型</th><th>等级</th><th>名字</th><th>词缀</th></tr>"
        f"</thead><tbody>{rows}</tbody></table>"
    )


def mod_row(kind: str, level: str, name: str, cell: str) -> str:
    return f"<tr><td>{kind}</td><td>{level}</td><td>{name}</td><td>{cell}</td></tr>"


def spectre_table(rows: str) -> str:
    return (
        "<table><thead><tr><th>名字</th><th>伤害%</th><th>生命%</th><th>幽魂</th></tr>"
        f"</thead><tbody>{rows}</tbody></table>"
    )


def spectre_row(name: str, slug: str, hover: str, damage: str, life: str) -> str:
    return (
        f'<tr><td><a data-hover="{hover}" href="/cn/{slug}">{name}</a></td>'
        f'<td>{damage}</td><td>{life}</td>'
        '<td><span class="fa-solid fa-check"></span></td></tr>'
    )


VARIETY = "?s=Data%5CMonsterVarieties%2F53074"
VARIETY2 = "?s=Data%5CMonsterVarieties%2F26210"
HOVER_HASH = "https://cdn.poedb.tw/cache1/cn/Poe_Data_MonsterVarieties_hover/e2656fefc7f9fcd1"

MINIMAL_SPECTRES = spectre_table(
    spectre_row("不朽精锐", "Abhorred_Elitist", VARIETY, "150", "120")
)


class MonsterModParseTest(unittest.TestCase):
    def test_取中文文本并把br规范成斜杠(self) -> None:
        """`<br>` 是两条属性的分隔。吞掉它会让「60%命中值提高 100%」粘成一句，
        既没法读，也会让出口的数值回填对不上原文。"""
        cell = ('<span class="explicitMod">全局物理伤害提高 <span class="mod-value">60</span>%'
                '<br>命中值提高 <span class="mod-value">100</span>%</span>')
        mods, skipped = parse_monster_mods(mod_table(mod_row("罪恶枷锁 传奇", "24", "碎骨者", cell)))
        self.assertEqual([], skipped)
        self.assertEqual("全局物理伤害提高 60% / 命中值提高 100%", mods[0].text_zh)
        self.assertEqual(("罪恶枷锁 传奇", 24, "碎骨者"),
                         (mods[0].kind, mods[0].monster_level, mods[0].name_zh))

    def test_未翻译的stat_id单独存不混进正文(self) -> None:
        cell = ('<span class="explicitMod">生命上限提高 <span class="mod-value">50</span>%'
                '<br><span class="secondary">monster dropped item rarity +% [0]</span></span>')
        mods, skipped = parse_monster_mods(mod_table(mod_row("猎魔", "1", "地狱犬之势", cell)))
        self.assertEqual([], skipped)
        self.assertEqual("生命上限提高 50%", mods[0].text_zh)
        self.assertEqual(("monster dropped item rarity +% [0]",), mods[0].raw_stats)

    def test_只有未翻译stat_id的行跳过并登记(self) -> None:
        """把内部 stat id 当中文答案输出，等于拿调试字段冒充游戏文本。"""
        cell = '<span class="explicitMod"><span class="secondary">maven map boss buffed [50]</span></span>'
        rows = mod_row("传奇", "1", "镀金的",
                       '<span class="explicitMod">30% 额外物理伤害减免</span>')
        rows += mod_row("传奇", "1", "塑界", cell)
        mods, skipped = parse_monster_mods(mod_table(rows))
        self.assertEqual(["镀金的"], [m.name_zh for m in mods])
        self.assertEqual([(SKIP_RAW_ONLY, "塑界")], skipped)

    def test_没有效果文本的行跳过并登记(self) -> None:
        rows = mod_row("传奇", "1", "镀金的",
                       '<span class="explicitMod">30% 额外物理伤害减免</span>')
        rows += mod_row("罪恶枷锁 传奇", "1", "法瑞尔的狂野之灵", "")
        mods, skipped = parse_monster_mods(mod_table(rows))
        self.assertEqual(["镀金的"], [m.name_zh for m in mods])
        self.assertEqual([(SKIP_NO_TEXT, "法瑞尔的狂野之灵")], skipped)

    def test_同名不同档都保留且id不同(self) -> None:
        """实测 12 组同 (来源, 名字) 有多档（「死眼」伤害 30% 与 20%）。
        只用 (来源, 名字) 做 id 会让后者覆盖前者——实测曾悄悄丢掉 12 张卡。"""
        rows = mod_row("罪恶枷锁 传奇", "1", "死眼",
                       '<span class="explicitMod">伤害提高 30%</span>')
        rows += mod_row("罪恶枷锁 传奇", "1", "死眼",
                        '<span class="explicitMod">伤害提高 20%</span>')
        mods, _ = parse_monster_mods(mod_table(rows))
        self.assertEqual(2, len(mods))
        self.assertNotEqual(mods[0].mod_id, mods[1].mod_id)

    def test_逐字完全相同的行合并(self) -> None:
        row = mod_row("传奇", "1", "镀金的",
                      '<span class="explicitMod">30% 额外物理伤害减免</span>')
        mods, _ = parse_monster_mods(mod_table(row + row))
        self.assertEqual(1, len(mods))

    def test_一条中文都没有时拒绝入库(self) -> None:
        cell = '<span class="explicitMod"><span class="secondary">x [0]</span></span>'
        with self.assertRaises(MonsterStructureError):
            parse_monster_mods(mod_table(mod_row("传奇", "1", "塑界", cell)))

    def test_按表头定位而不是按下标(self) -> None:
        """页面加一张表就会让下标全错，而那种错会静默把别的表当成本表入库。"""
        other = "<table><thead><tr><th>怪物等级</th><th>生命</th></tr></thead><tbody>" \
                "<tr><td>1</td><td>22</td></tr></tbody></table>"
        html = other + mod_table(mod_row("传奇", "1", "镀金的",
                                         '<span class="explicitMod">30% 额外物理伤害减免</span>'))
        mods, _ = parse_monster_mods(html)
        self.assertEqual(["镀金的"], [m.name_zh for m in mods])

    def test_找不到目标表就报错(self) -> None:
        with self.assertRaises(MonsterStructureError):
            parse_monster_mods("<table><tr><th>别的</th></tr></table>")


class SpectreParseTest(unittest.TestCase):
    def test_种类id形态的键(self) -> None:
        spectres, dups = parse_spectres(MINIMAL_SPECTRES)
        self.assertEqual((), dups)
        self.assertEqual("v:53074", spectres[0].variety_key)
        self.assertEqual(("不朽精锐", "Abhorred_Elitist", 150, 120),
                         (spectres[0].name_zh, spectres[0].poedb_slug,
                          spectres[0].damage_pct, spectres[0].life_pct))

    def test_哈希形态的键要并进倍率(self) -> None:
        """tooltip 哈希是**内容**的缓存键，不是种类 id：实测同名同 slug 的两个种类
        （不同区域版本）会撞同一个哈希。只用哈希会把它们合并成一条。"""
        rows = spectre_row("无名猛兽", "Nameless_Beast", HOVER_HASH, "276", "360")
        rows += spectre_row("无名猛兽", "Nameless_Beast", HOVER_HASH, "374", "585")
        spectres, dups = parse_spectres(spectre_table(rows))
        self.assertEqual(2, len(spectres))
        self.assertEqual({"h:e2656fefc7f9fcd1:276:360", "h:e2656fefc7f9fcd1:374:585"},
                         {s.variety_key for s in spectres})
        self.assertEqual(("无名猛兽",), dups)

    def test_同名不同种类都保留并记下重名(self) -> None:
        rows = spectre_row("不朽精锐", "Abhorred_Elitist", VARIETY, "150", "120")
        rows += spectre_row("不朽精锐", "Abhorred_Elitist", VARIETY2, "125", "110")
        spectres, dups = parse_spectres(spectre_table(rows))
        self.assertEqual(2, len(spectres))
        self.assertEqual(("不朽精锐",), dups)

    def test_同键不同内容整批拒绝(self) -> None:
        """同一个种类 id 给两套数值 = 键选错了，必须暴露而不是挑一条。"""
        rows = spectre_row("甲", "A", VARIETY, "150", "120")
        rows += spectre_row("乙", "B", VARIETY, "125", "110")
        with self.assertRaises(MonsterStructureError):
            parse_spectres(spectre_table(rows))

    def test_幽魂列不入库(self) -> None:
        """那一列实测 1948 行全是勾——这张表本身就是幽魂清单。
        存一个恒为真的布尔值会让人以为库里还有「不可作幽魂」的怪物。"""
        spectres, _ = parse_spectres(MINIMAL_SPECTRES)
        for field in ("is_spectre", "spectre", "can_be_spectre"):
            self.assertFalse(hasattr(spectres[0], field), field)


class MonsterCardTest(unittest.TestCase):
    def _entity(self, **attrs) -> Entity:
        base = {
            "kind": "传奇", "monster_level": 1,
            "text_zh": "30% 额外物理伤害减免", "raw_stats": [],
        }
        base.update(attrs)
        return Entity(
            entity_id="monster_mod:传奇_镀金的_abcd1234",
            entity_type=EntityType.MONSTER_MOD,
            name_zh="镀金的",
            attributes=base,
            ref=SourceRef(source=Source.POEDB.value,
                          url="https://poedb.tw/cn/Monsters", priority=1),
        )

    def test_卡片带来源分类(self) -> None:
        card = render_monsters.render_card(self._entity())
        self.assertIn("# 镀金的", card)
        self.assertIn("来源：传奇", card)
        self.assertIn("- 30% 额外物理伤害减免", card)
        self.assertLessEqual(len(card), render_monsters.MAX_CHUNK_CHARS)

    def test_未翻译部分单列并标注(self) -> None:
        """不混进词缀正文：写清楚「没有中文」比假装它是词缀文本诚实。"""
        card = render_monsters.render_card(
            self._entity(raw_stats=["monster dropped item rarity +% [0]"])
        )
        self.assertIn("没有中文文本", card)
        self.assertIn("monster dropped item rarity +% [0]", card)

    def test_出卡片(self) -> None:
        chunks = render_monsters.to_chunks(self._entity())
        self.assertEqual(1, len(chunks))
        self.assertEqual("怪物词缀", chunks[0].metadata.subcategory)


def _tool_conn(rows: int = 3) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO poedb_spectre(variety_key,name_zh,poedb_slug,damage_pct,life_pct,"
        "source,source_priority,url,content_hash,fetched_at,updated_at) "
        "VALUES (?,?,?,?,?,'POEDB',1,'https://poedb.tw/cn/x',?,datetime('now'),datetime('now'))",
        [(f"v:{i}", "恶魔" if i else "不朽精锐", "Daemon" if i else "Abhorred_Elitist",
          100 + i, 100 + i, f"h{i}") for i in range(rows)],
    )
    conn.commit()
    return conn


class SpectreToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _tool_conn(rows=MAX_ROWS + 5)
        self.patch = patch("tools.impl.monsters.dbmod.get_conn", return_value=self.conn)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_按名查到并写出库内总数(self) -> None:
        result = spectre_stats("不朽精锐")
        self.assertTrue(result.data["found"])
        self.assertEqual(MAX_ROWS + 5, result.data["spectres_in_library"])
        self.assertIn("本库共收录", result.citations[0]["text"])

    def test_同名多条时截断并写出还剩多少(self) -> None:
        """静默截断等同于漏答：同名怪物是不同区域版本，倍率各不相同。"""
        result = spectre_stats("恶魔")
        self.assertTrue(result.data["truncated"])
        self.assertIn(f"以上仅列前 {MAX_ROWS} 条", result.citations[0]["text"])

    def test_查不到时说清两种可能(self) -> None:
        """「不能当幽魂」与「名字写法不同」是两件事，本库分不清就得说出来。"""
        result = spectre_stats("不存在的怪物")
        self.assertFalse(result.data["found"])
        self.assertIn("也可能是名字写法", result.data["hint"])
        self.assertEqual([], result.citations)

    def test_返回里带边界声明且不承诺排名(self) -> None:
        for result in (spectre_stats("不朽精锐"), spectre_stats("不存在的怪物")):
            self.assertIn("不做倍率排名", result.data["boundary"])

    def test_边界声明走既有的附注机制(self) -> None:
        from orchestrator.loop import _NOTE_FIELDS

        self.assertIn("boundary", _NOTE_FIELDS)

    def test_空名直接失败(self) -> None:
        self.assertFalse(spectre_stats("  ").ok)


class SpectreRouteTest(unittest.TestCase):
    """幽魂表没有卡片，只能靠意图词路由到达——不挂上去就是一个死了的能力。"""

    def test_幽魂问法路由到工具(self) -> None:
        from orchestrator.loop import _preferred_tool_route

        self.assertEqual("spectre_stats", _preferred_tool_route("不朽精锐的幽魂倍率是多少"))

    def test_不含幽魂的问题不被抢路由(self) -> None:
        from orchestrator.loop import _preferred_tool_route

        self.assertNotEqual("spectre_stats", _preferred_tool_route("猎首提供多少力量"))


class ExtractTest(unittest.TestCase):
    def test_两张表一起抽(self) -> None:
        html = mod_table(mod_row("传奇", "1", "镀金的",
                                 '<span class="explicitMod">30% 额外物理伤害减免</span>'))
        html += MINIMAL_SPECTRES
        result = extract(html)
        self.assertEqual(1, len(result.mods))
        self.assertEqual(1, len(result.spectres))

    def test_抓到英文页时拒绝(self) -> None:
        """词缀文本里没有中文 = 抓错了语言版本，整批拒绝而不是入库一堆英文。"""
        html = mod_table(mod_row("Unique", "1", "Gilded",
                                 '<span class="explicitMod">30% additional reduction</span>'))
        html += MINIMAL_SPECTRES
        with self.assertRaises(MonsterStructureError):
            extract(html)


if __name__ == "__main__":
    unittest.main()
