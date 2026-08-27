"""万神殿：Lua 解析、逐神页抽取、桥接判据与逐条对账（2026-08-21 第三十五轮）。

判据全部来自实测（12 位神祇 / 32 层神力 / 41 条词缀，两侧 41/41 一致）：

* **stat id 在 Lua 注释里**，`pob_lua` 的词法器按设计丢弃它，用正则捞回来；
  按 `line` 原文建映射而不是按文件顺序 zip——顺序 zip 在上游插入一条没有注释的
  词缀时会整体错位，而错位后每一条**仍然「有 stat id」**，是最难发现的一类错。
* **两道桥接判据**：页面 Attr 表的 `ID` 必须等于 PoB 表键，`Name` 必须等于 PoB
  英文名。slug 是由英文名推出来的，推错时 POEDB 给的是**别的条目而不是 404**。
* **中文词缀与 stat id 不逐项对应**：同一层里页面正文的顺序与 PoB 不一致
  （艾贝拉斯第二层两侧正好相反），按位置或按数值配对都会把中文挂到错误的 stat 上。

这个文件不连库、不连网：Lua 与 HTML 都是内联构造的最小样例。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from entities.models import Entity, ModLine, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, ModKind, Source  # noqa: E402
from kb import render_pantheon  # noqa: E402
from kb.sources.poedb_pantheon import (  # noqa: E402
    PantheonPageError,
    parse_pantheon_page,
    reconcile,
)
from kb.sources.pob_pantheon import (  # noqa: E402
    PantheonStructureError,
    parse_pantheons,
    stat_ids_by_line,
)

# ── 样例：一位主神（4 层）+ 七位次神（凑够 4 主 8 次的规则护栏）──────────

_MAJOR = """
	["Arakaali"] = {
		isMajorGod = true,
		souls = {
			[1] = { name = "Soul of Arakaali",
				mods = {
					-- degen_effect_+%
					[1] = { line = "10% reduced Damage taken from Damage Over Time", value = { -10 }, },
				},
			},
			[2] = { name = "Maligaro the Mutilator",
				mods = {
					-- recovery_rate_+%
					[1] = { line = "20% increased Recovery rate", value = { 20 }, },
				},
			},
			[3] = { name = "Armala, the Widow",
				mods = {
					-- debuff_time_passed_+%
					[1] = { line = "Debuffs on you expire 20% faster", value = { 20 }, },
				},
			},
			[4] = { name = "Drought-Maddened Rhoa",
				mods = {
					-- additional_chaos_resistance_against_damage_over_time_%
					[1] = { line = "+40% Chaos Resistance against Damage Over Time", value = { 40 }, },
				},
			},
		},
	},
"""


def _minor(key: str, n: int) -> str:
    """一位两层次神。`n` 用来让每条 line 唯一（映射按 line 建）。"""
    return f"""
	["{key}"] = {{
		isMajorGod = false,
		souls = {{
			[1] = {{ name = "Soul of {key}",
				mods = {{
					-- stat_base_{n}
					[1] = {{ line = "Base effect {n}", value = {{ {n} }}, }},
				}},
			}},
			[2] = {{ name = "Champion of {key}",
				mods = {{
					-- stat_upgrade_{n}
					[1] = {{ line = "Upgrade effect {n}", value = {{ {n * 2} }}, }},
				}},
			}},
		}},
	}},
"""


def lua(majors: int = 4, minors: int = 8) -> str:
    body = ""
    for i in range(majors):
        body += _MAJOR.replace('["Arakaali"]', f'["Major{i}"]').replace(
            "Soul of Arakaali", f"Soul of Major{i}"
        ).replace("degen_effect_+%", f"degen_effect_{i}_+%").replace(
            "10% reduced Damage taken from Damage Over Time",
            f"Major{i} base line",
        ).replace("recovery_rate_+%", f"recovery_rate_{i}_+%").replace(
            "20% increased Recovery rate", f"Major{i} second line"
        ).replace("debuff_time_passed_+%", f"debuff_time_passed_{i}_+%").replace(
            "Debuffs on you expire 20% faster", f"Major{i} third line"
        ).replace(
            "additional_chaos_resistance_against_damage_over_time_%",
            f"chaos_res_{i}_%",
        ).replace(
            "+40% Chaos Resistance against Damage Over Time", f"Major{i} fourth line"
        )
    for i in range(minors):
        body += _minor(f"Minor{i}", i + 1)
    return "return {" + body + "}"


def attr_table(rows: dict[str, str]) -> str:
    body = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows.items())
    return f'<table class="table"><tbody>{body}</tbody></table>'


def popup(segments: list[dict]) -> str:
    """按页面真实结构拼 `div.PantheonPopup`：段间以 `div.separator` 分隔。"""
    chunks: list[str] = []
    for i, seg in enumerate(segments):
        part = ""
        if i == 0:
            part += f'<span class="text-value">{seg.get("act", "第 7 章")}</span>'
            part += f'<a class="questitem" href="#">{seg.get("quest", "任务")}</a><br/>'
        else:
            part += f'<span class="text-value">抓获<a href="#">{seg["boss"]}</a></span>'
            part += f'<a class="itemclass_map Map" href="#">{seg["map"]}</a><br>'
        for text in seg["mods"]:
            part += f'<div class="explicitMod">{text}</div>'
        chunks.append(part)
    inner = '<div class="separator"></div>'.join(chunks)
    return (
        '<div class="newItemPopup PantheonPopup"><div class="content"><div class="Stats">'
        f'<div class="implicitMod">{inner}</div></div></div></div>'
    )


def page(
    *,
    god_id: str = "Arakaali",
    name_en: str = "Soul of Arakaali",
    is_major: str = "1",
    tiers: list[dict] | None = None,
) -> str:
    tiers = tiers or [
        {"key": "Main", "name": "暗影女皇 阿拉卡力之魂",
         "stats": [("degen_effect_+%", "-10")],
         "mods": ["受到的持续性伤害降低 <span class='mod-value'>10</span>%"]},
        {"key": "Defeat1", "name": "切断者马雷格罗",
         "stats": [("recovery_rate_+%", "20")], "map": "密草神殿",
         "mods": ["生命回复速度加快 <span class='mod-value'>20</span>%"]},
    ]
    rows = {"ID": god_id, "Name": name_en, "IsMajor": is_major}
    segments = []
    for i, tier in enumerate(tiers):
        lis = "".join(f"<li>{s} {v}</li>" for s, v in tier["stats"])
        rows[tier["key"]] = f'{tier["name"]}{lis}'
        seg = {"mods": tier["mods"]}
        if i:
            seg["boss"] = tier["name"]
            seg["map"] = tier.get("map", "某地图")
        segments.append(seg)
    return f"<html><body>{attr_table(rows)}{popup(segments)}</body></html>"


class LuaParseTest(unittest.TestCase):
    def test_解析出神祇层数与词缀(self) -> None:
        gods = parse_pantheons(lua())
        self.assertEqual(12, len(gods))
        self.assertEqual(4, sum(1 for g in gods if g.is_major))
        self.assertEqual(4 * 4 + 8 * 2, sum(len(g.souls) for g in gods))

    def test_stat_id_从注释里捞回来(self) -> None:
        """词法器丢弃注释，而 stat id 正是对账键。"""
        gods = parse_pantheons(lua())
        first = gods[0].souls[0].mods[0]
        self.assertEqual("degen_effect_0_+%", first.stat_id)
        self.assertEqual(-10, first.value)

    def test_按行文建映射而不是按顺序(self) -> None:
        mapping = stat_ids_by_line(lua())
        self.assertEqual("degen_effect_0_+%", mapping["Major0 base line"])
        self.assertEqual("chaos_res_3_%", mapping["Major3 fourth line"])

    def test_缺注释的词缀直接报错而不是留空(self) -> None:
        broken = lua().replace("-- degen_effect_0_+%\n", "")
        with self.assertRaises(PantheonStructureError):
            parse_pantheons(broken)

    def test_神祇数量不符游戏规则时报错(self) -> None:
        """万神殿固定 4 主 8 次，少一位说明解析或上游出了问题。"""
        with self.assertRaises(PantheonStructureError):
            parse_pantheons(lua(majors=3))

    def test_同一行文对应两个stat_id时报错(self) -> None:
        # 两个**不同**的 stat id 指向同一句行文 → 映射有歧义，必须报错而不是后者覆盖前者
        dup = lua().replace("Major3 fourth line", "Major0 base line")
        with self.assertRaises(PantheonStructureError):
            stat_ids_by_line(dup)


class PageParseTest(unittest.TestCase):
    def test_抽出中文名头目与地图(self) -> None:
        parsed = parse_pantheon_page(
            page(), pob_key="Arakaali", name_en="Soul of Arakaali", soul_count=2
        )
        self.assertEqual("暗影女皇 阿拉卡力之魂", parsed.name_zh)
        self.assertTrue(parsed.is_major)
        self.assertEqual("切断者马雷格罗", parsed.tiers[1].boss_zh)
        self.assertEqual("密草神殿", parsed.tiers[1].map_zh)
        self.assertEqual("第 7 章", parsed.tiers[0].act_zh)

    def test_数值与文字之间的空格照页面来(self) -> None:
        """`降低 <span>10</span>%` 要渲成「降低 10%」，不是「降低 10 %」或「降低10%」。

        卡片措辞必须与唯一装备那边一致，否则出口的数值回填会在两种说法之间对不上。
        """
        parsed = parse_pantheon_page(
            page(), pob_key="Arakaali", name_en="Soul of Arakaali", soul_count=2
        )
        self.assertEqual("受到的持续性伤害降低 10%", parsed.tiers[0].mods_zh[0])

    def test_ID对不上直接拒绝(self) -> None:
        """slug 是推出来的，推错时 POEDB 给的是别的条目而不是 404。"""
        with self.assertRaises(PantheonPageError):
            parse_pantheon_page(
                page(god_id="Lunaris"), pob_key="Arakaali",
                name_en="Soul of Arakaali", soul_count=2,
            )

    def test_英文名对不上直接拒绝(self) -> None:
        with self.assertRaises(PantheonPageError):
            parse_pantheon_page(
                page(name_en="Soul of Lunaris"), pob_key="Arakaali",
                name_en="Soul of Arakaali", soul_count=2,
            )

    def test_层数不足直接拒绝(self) -> None:
        with self.assertRaises(PantheonPageError):
            parse_pantheon_page(
                page(), pob_key="Arakaali", name_en="Soul of Arakaali", soul_count=4
            )

    def test_正文与Attr表条数不符时报错(self) -> None:
        """两处都在同一页上，条数对不上说明分段判据错了。"""
        html = page().replace(
            '<div class="explicitMod">生命回复速度加快 '
            "<span class='mod-value'>20</span>%</div>",
            "",
        )
        with self.assertRaises(PantheonPageError):
            parse_pantheon_page(
                html, pob_key="Arakaali", name_en="Soul of Arakaali", soul_count=2
            )


class _FakeMod:
    def __init__(self, stat_id: str, value: float) -> None:
        self.stat_id = stat_id
        self.value = value
        self.line_en = f"line for {stat_id}"


class _FakeSoul:
    def __init__(self, mods: list[_FakeMod]) -> None:
        self.mods = mods


class _FakeGod:
    def __init__(self, key: str, is_major: bool, souls: list[_FakeSoul]) -> None:
        self.key = key
        self.is_major = is_major
        self.souls = souls


def _god(**kw) -> _FakeGod:  # noqa: ANN003
    return _FakeGod(
        kw.get("key", "Arakaali"),
        kw.get("is_major", True),
        kw.get("souls", [
            _FakeSoul([_FakeMod("degen_effect_+%", -10)]),
            _FakeSoul([_FakeMod("recovery_rate_+%", 20)]),
        ]),
    )


class ReconcileTest(unittest.TestCase):
    def _page(self, **kw):  # noqa: ANN003, ANN201
        return parse_pantheon_page(
            page(**kw), pob_key="Arakaali", name_en="Soul of Arakaali", soul_count=2
        )

    def test_两侧一致时零分歧(self) -> None:
        self.assertEqual([], reconcile(self._page(), _god()))

    def test_数值对不上要报出来(self) -> None:
        god = _god(souls=[
            _FakeSoul([_FakeMod("degen_effect_+%", -15)]),   # 页面是 -10
            _FakeSoul([_FakeMod("recovery_rate_+%", 20)]),
        ])
        found = reconcile(self._page(), god)
        self.assertEqual(1, len(found))
        self.assertIn("degen_effect_+%", str(found[0]))

    def test_stat_id对不上要报出来(self) -> None:
        god = _god(souls=[
            _FakeSoul([_FakeMod("other_stat", -10)]),
            _FakeSoul([_FakeMod("recovery_rate_+%", 20)]),
        ])
        found = reconcile(self._page(), god)
        self.assertEqual("stat id", found[0].field)

    def test_主神标记对不上要报出来(self) -> None:
        found = reconcile(self._page(), _god(is_major=False))
        self.assertEqual("IsMajor", found[0].field)


class CardTest(unittest.TestCase):
    def _entity(self) -> Entity:
        tiers = [
            {"tier": 1, "name_zh": "暗影女皇 阿拉卡力之魂", "name_en": "Soul of Arakaali",
             "boss_zh": "", "map_zh": "", "act_zh": "第 7 章", "quest_zh": "阿拉卡力",
             "mods_zh": ["受到的持续性伤害降低 10%"],
             "mods_en": ["10% reduced Damage taken from Damage Over Time"]},
            {"tier": 2, "name_zh": "切断者马雷格罗", "name_en": "Maligaro the Mutilator",
             "boss_zh": "切断者马雷格罗", "map_zh": "密草神殿", "act_zh": "", "quest_zh": "",
             "mods_zh": ["生命回复速度加快 20%"],
             "mods_en": ["20% increased Recovery rate"]},
        ]
        return Entity(
            entity_id="pantheon:Soul_of_Arakaali",
            entity_type=EntityType.PANTHEON,
            name_zh="暗影女皇 阿拉卡力之魂",
            name_en="Soul of Arakaali",
            slug="Soul_of_Arakaali",
            mods=[ModLine(kind=ModKind.PANTHEON, text=t)
                  for tier in tiers for t in tier["mods_zh"]],
            attributes={"is_major": True, "tiers": tiers,
                        "name_zh_alt": ["切断者马雷格罗"]},
            ref=SourceRef(
                source=Source.POEDB.value,
                url="https://poedb.tw/cn/Soul_of_Arakaali",
                priority=1,
                fetched_at="2026-08-21T00:00:00Z",
            ),
        )

    def test_卡片写明主次与逐层来源(self) -> None:
        card = render_pantheon.render_card(self._entity())
        self.assertIn("主要神力", card)
        self.assertIn("由第 7 章·任务「阿拉卡力」给予", card)
        self.assertIn("抓获 切断者马雷格罗（地图：密草神殿）", card)
        self.assertIn("受到的持续性伤害降低 10%", card)

    def test_不在卡片上写这一页没有的规则(self) -> None:
        """「同时只能开一主一次」在 `/cn/Pantheon` 的散文里，不在逐神页上。

        卡片末尾的来源指向逐神页，在上面写一句该页没有的话，
        就是把断言挂到了不含它的引用下。
        """
        card = render_pantheon.render_card(self._entity())
        self.assertNotIn("同时只能", card)
        self.assertNotIn("神之容器", card)

    def test_英文整层列出而不逐条对照(self) -> None:
        """中文与英文**不逐项对应**，排成两列会让人以为同一行是同一条。"""
        card = render_pantheon.render_card(self._entity())
        english = card.split("英文原文")[1]
        self.assertIn("10% reduced Damage taken from Damage Over Time", english)
        self.assertNotIn("受到的持续性伤害", english)

    def test_出切片且带引用(self) -> None:
        chunks = render_pantheon.to_chunks(self._entity())
        self.assertEqual(1, len(chunks))
        self.assertEqual("https://poedb.tw/cn/Soul_of_Arakaali", chunks[0].metadata.url)
        self.assertEqual("万神殿", chunks[0].metadata.subcategory)

    def test_缺来源的实体不出切片(self) -> None:
        entity = self._entity().model_copy(
            update={"ref": SourceRef(source="", url="", priority=1, fetched_at="")}
        )
        self.assertEqual([], render_pantheon.to_chunks(entity))


if __name__ == "__main__":
    unittest.main()
