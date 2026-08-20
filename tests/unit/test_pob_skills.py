"""技能石与辅助宝石：桥接、口径换算、卡片边界与逐级查询工具（2026-08-19）。

全部离线：Lua 与 HTML 都在内存/临时目录里构造，不读 `data/http_cache`、不连网、
不碰真实 PoB 检出。

这一轮的判据几乎每条都来自实测反例，用例按反例写：
`_of_` 不能当蜕变宝石判据（23 条误判）、同键两译名不能整条丢（会丢掉高频宝石）、
`reqStr` 不是属性需求（是换算系数）、卡片里不许出现「伤害」（会给伤害类问题送覆盖率）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from entities.models import Entity, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, Source, make_entity_id  # noqa: E402
from kb import render_skills  # noqa: E402
from kb.sources.pob_skills import (  # noqa: E402
    SKILL_FILES,
    SKIP_COLOR_CONFLICT,
    SKIP_NO_ZH,
    SKIP_POB_MISSING,
    GemStructureError,
    extract_gems,
    parse_poedb_detail_name,
    parse_poedb_gems,
    poedb_detail_slug,
)
from tools.impl.gems import MAX_LEVEL_ROWS, skill_gem_levels  # noqa: E402

# ── 构造 POEDB 页面 ───────────────────────────────────────────────


def gem_anchor(gem_key: str, slug: str, name: str, cls: str, tags: str | None) -> str:
    """一颗宝石在页面上的一格：图标链接（空文本）+ 名字链接 + 可选的中文标签格。

    结构照抄 POEDB 真实页面：标签格只出现在主清单那一处。
    """
    tag_div = f'<div class="gem_tags small">{tags}</div>' if tags else ""
    return (
        f'<td><a class="{cls}" data-hover="?s=Data%5CGemEffects%2F{gem_key}" '
        f'href="/cn/{slug}"></a>'
        f'<a class="{cls}" data-hover="?s=Data%5CGemEffects%2F{gem_key}" '
        f'href="/cn/{slug}">{name}</a>{tag_div}</td>'
    )


def gem_page(*anchors: str) -> str:
    return f"<html><body><table><tr>{''.join(anchors)}</tr></table></body></html>"


# ── 构造 PoB 侧 ───────────────────────────────────────────────────

COSTS_LUA = """
return {
	[1] = { Resource = "Mana", Stat = "base_mana_cost", ResourceString = "{0} Mana", Divisor = 1, },
	[2] = { Resource = "ManaPerMinute", Stat = "x", ResourceString = "{0} Mana per second", Divisor = 60, },
}
"""

GEMS_LUA = """
return {
	["Metadata/Items/Gems/SkillGemIceNova"] = {
		name = "Ice Nova",
		grantedEffectId = "IceNova",
		tags = { intelligence = true, spell = true, },
		tagString = "Spell, AoE, Cold",
		reqInt = 100,
		naturalMaxLevel = 20,
	},
	["Metadata/Items/Gems/SupportCastOnMeleeKill"] = {
		name = "Cast on Melee Kill",
		grantedEffectId = "SupportCastOnMeleeKill",
		tags = { strength = true, support = true, },
		tagString = "Support, Trigger",
		naturalMaxLevel = 20,
	},
}
"""

SKILLS_LUA = """
skills["IceNova"] = {
	name = "Ice Nova",
	color = 3,
	description = "A circle of ice expands from the caster.",
	castTime = 0.7,
	skillTypes = { [SkillType.Spell] = true, [SkillType.Cold] = true, },
	levels = {
		[1] = { 0.8, 1.2, critChance = 7.5, damageEffectiveness = 2.3, levelRequirement = 12, cost = { Mana = 8, }, },
		[2] = { 0.8, 1.2, critChance = 7.5, damageEffectiveness = 2.5, levelRequirement = 70, cost = { Mana = 23, }, },
	},
}
skills["SupportCastOnMeleeKill"] = {
	name = "Cast on Melee Kill",
	color = 1,
	support = true,
	skillTypes = { [SkillType.Support] = true, },
	levels = {
		[1] = { manaMultiplier = 150, cooldown = 0.25, storedUses = 1, levelRequirement = 38, },
	},
}
"""


#: `*Instant` 变体：与本体同名（因此详情页 slug 相同），PoB 侧却是两个 granted effect。
#: 实测原型是 `VaalLightningWarpInstant` 与清单页上的 `VaalLightningWarp`。
GEMS_LUA_WITH_INSTANT = GEMS_LUA.rstrip().rstrip("}") + """
	["Metadata/Items/Gems/SupportCastOnMeleeKillInstant"] = {
		name = "Cast on Melee Kill",
		grantedEffectId = "SupportCastOnMeleeKillInstant",
		tags = { strength = true, support = true, },
		tagString = "Support, Trigger",
		naturalMaxLevel = 20,
	},
}
"""
SKILLS_LUA_WITH_INSTANT = SKILLS_LUA + """
skills["SupportCastOnMeleeKillInstant"] = {
	name = "Cast on Melee Kill",
	color = 1,
	support = true,
	skillTypes = { [SkillType.Support] = true, },
	levels = {
		[1] = { manaMultiplier = 150, levelRequirement = 38, },
	},
}
"""


def pob_root(gems: str = GEMS_LUA, skills: str = SKILLS_LUA, costs: str = COSTS_LUA) -> Path:
    """临时 PoB 检出。**十个技能文件都要存在**——少一个就该报错，
    那是「上游改了目录结构」的信号，不该被静默当成少了几颗宝石。"""
    root = Path(tempfile.mkdtemp())
    src = root / "src"
    (src / "Data" / "Skills").mkdir(parents=True)
    (src / "Data" / "Gems.lua").write_text(gems, encoding="utf-8")
    (src / "Data" / "Costs.lua").write_text(costs, encoding="utf-8")
    for rel in SKILL_FILES:
        body = skills if rel.endswith("act_int.lua") else ""
        (src / Path(rel)).write_text(body, encoding="utf-8")
    return root


ICE_NOVA_ANCHOR = gem_anchor("IceNova", "Ice_Nova", "冰霜新星", "gem_blue", "法术, 效果区域, 冰霜")
#: POEDB 写 `Caston`、PoB 写 `CastOn` —— 实测 8 颗宝石靠不分大小写才桥接上
CASE_DIFF_ANCHOR = gem_anchor(
    "SupportCastonMeleeKill", "Cast_on_Melee_Kill_Support", "近战击败时施放(辅)",
    "gem_red", "辅助, 触发",
)


class BridgeTest(unittest.TestCase):
    """桥接键是 grantedEffectId，不分大小写，且成因必须分类。"""

    def test_大小写不同也能桥接(self) -> None:
        gems, skipped = extract_gems(pob_root(), gem_page(ICE_NOVA_ANCHOR, CASE_DIFF_ANCHOR))
        self.assertEqual([], skipped)
        self.assertEqual(
            {"IceNova": "冰霜新星", "SupportCastOnMeleeKill": "近战击败时施放(辅)"},
            {g.gem_id: g.name_zh for g in gems},
        )
        # gem_id 保留 **PoB 侧**的大小写：它是 Data/Skills 里的键，
        # 拿 POEDB 的拼法当主键会让后续按 gem_id 回查 PoB 时对不上
        self.assertIn("SupportCastOnMeleeKill", {g.gem_id for g in gems})

    def test_主动与辅助分开且颜色取自POEDB(self) -> None:
        gems, _ = extract_gems(pob_root(), gem_page(ICE_NOVA_ANCHOR, CASE_DIFF_ANCHOR))
        by_id = {g.gem_id: g for g in gems}
        self.assertEqual("active", by_id["IceNova"].gem_kind)
        self.assertEqual("int", by_id["IceNova"].attr_color)
        self.assertEqual("support", by_id["SupportCastOnMeleeKill"].gem_kind)
        self.assertEqual("str", by_id["SupportCastOnMeleeKill"].attr_color)

    def test_POEDB有而PoB未发布记pob_missing(self) -> None:
        legacy = gem_anchor("VaalSweep", "Vaal_Sweep", "瓦尔：横扫", "gem_red", "攻击")
        gems, skipped = extract_gems(
            pob_root(), gem_page(ICE_NOVA_ANCHOR, CASE_DIFF_ANCHOR, legacy)
        )
        self.assertEqual({"IceNova", "SupportCastOnMeleeKill"}, {g.gem_id for g in gems})
        self.assertEqual([("瓦尔：横扫", SKIP_POB_MISSING)],
                         [(s.name, s.reason) for s in skipped])
        self.assertIn("PoB 当前版本未发布", skipped[0].detail)

    def test_PoB有而POEDB无中文名记no_zh_name(self) -> None:
        """取不到中文名就不入库，**不许用英文名或 slug 顶替**。"""
        gems, skipped = extract_gems(pob_root(), gem_page(ICE_NOVA_ANCHOR))
        self.assertEqual(["IceNova"], [g.gem_id for g in gems])
        self.assertEqual([(SKIP_NO_ZH, "Cast on Melee Kill")],
                         [(s.reason, s.name) for s in skipped])

    def test_两侧颜色冲突只跳这一条(self) -> None:
        """跨来源分歧不静默取一侧，也不整批拒绝——只跳这颗并带上双方判定。"""
        wrong = gem_anchor("IceNova", "Ice_Nova", "冰霜新星", "gem_red", "法术")
        gems, skipped = extract_gems(pob_root(), gem_page(wrong, CASE_DIFF_ANCHOR))
        self.assertEqual(["SupportCastOnMeleeKill"], [g.gem_id for g in gems])
        conflicts = [s for s in skipped if s.reason == SKIP_COLOR_CONFLICT]
        self.assertEqual(["IceNova"], [s.gem_key for s in conflicts])
        self.assertIn("POEDB 判为 str", conflicts[0].detail)
        self.assertIn("PoB 判为 int", conflicts[0].detail)


def detail_page(name_zh: str, name_en: str) -> str:
    """POEDB 详情页：中文名在 `<title>` 里，`<h1>` 反而是英文（实测形态）。"""
    return (
        f"<html><head><title>{name_zh} - 流亡编年史, Path of Exile Wiki cn</title></head>"
        f"<body><h1>{name_en}</h1><div class=\"gem_red\"></div>"
        f"<div class=\"gem_blue\"></div><p>{name_en}</p>"
        "<p>放入物品插槽以获得此技能。</p></body></html>"
    )


class DetailFallbackTest(unittest.TestCase):
    """清单页没收录的宝石去详情页取中文名（第三十一轮）。

    实测 11 颗未桥接里 10 颗有自己的详情页，中文名就在标题里。这条兜底**没有放宽
    「不许用英文名或 slug 顶替」**：名字仍然来自 POEDB，只是多看了一页。
    所以这里成对钉两侧——**该桥的桥上了**，**该跳的仍然跳**（页面不存在、
    标题里没有中文名、以及一页被两个 granted effect 抢的情形）。
    """

    def _detail(self, pages: dict[str, str]):  # noqa: ANN202
        seen: list[str] = []

        def read(slug: str) -> str | None:
            seen.append(slug)
            return pages.get(slug)

        self.seen = seen
        return read

    def test_清单页没有但详情页有中文名就桥接(self) -> None:
        gems, skipped = extract_gems(
            pob_root(), gem_page(ICE_NOVA_ANCHOR),
            detail_html=self._detail(
                {"Cast_on_Melee_Kill": detail_page("近战击败时施放(辅)", "Cast on Melee Kill")}
            ),
        )
        self.assertEqual([], skipped)
        by_id = {g.gem_id: g for g in gems}
        bridged = by_id["SupportCastOnMeleeKill"]
        self.assertEqual("近战击败时施放(辅)", bridged.name_zh)
        self.assertEqual("Cast_on_Melee_Kill", bridged.poedb_slug)
        # 颜色只能取 PoB 那一侧：详情页上三种 gem_ class 都在（那是相关宝石的链接）
        self.assertEqual("str", bridged.attr_color)
        # 详情页给不出中文标签，留空而不是拿英文标签顶替
        self.assertEqual((), bridged.tags_zh)
        # 每级字面值照旧从 PoB 取，兜底只换了名字这一维
        self.assertEqual(150.0, bridged.levels[0].mana_multiplier)

    def test_详情页不存在时仍然跳过并写明查过了(self) -> None:
        """成对点：**取不到就是取不到**，不许退回英文名。"""
        gems, skipped = extract_gems(
            pob_root(), gem_page(ICE_NOVA_ANCHOR), detail_html=self._detail({}),
        )
        self.assertEqual(["IceNova"], [g.gem_id for g in gems])
        self.assertEqual([SKIP_NO_ZH], [s.reason for s in skipped])
        self.assertIn("清单页与详情页", skipped[0].detail)
        self.assertIn("Cast_on_Melee_Kill", skipped[0].detail)

    def test_标题里没有中文名时跳过(self) -> None:
        """英文标题说明这一页没有译名——那也是「取不到」。"""
        page = detail_page("Cast on Melee Kill", "Cast on Melee Kill")
        _, skipped = extract_gems(
            pob_root(), gem_page(ICE_NOVA_ANCHOR),
            detail_html=self._detail({"Cast_on_Melee_Kill": page}),
        )
        self.assertEqual([SKIP_NO_ZH], [s.reason for s in skipped])

    def test_没给回调时记成未查详情页而不是查了没有(self) -> None:
        """两种成因指向完全不同的动作：一个是补一次采集，一个是 POEDB 真的没有。"""
        _, skipped = extract_gems(pob_root(), gem_page(ICE_NOVA_ANCHOR))
        self.assertEqual([SKIP_NO_ZH], [s.reason for s in skipped])
        self.assertIn("未查详情页", skipped[0].detail)
        self.assertIn("--fetch", skipped[0].detail)

    def test_一页被两个条目抢时不重复占用(self) -> None:
        """实测 PoB 的 `*Instant` 变体与本体共用一张详情页。硬写会撞
        `UNIQUE(poedb_slug)`，而挑一个留下就是替用户断言两者是同一颗宝石。"""
        same_name = gem_anchor(
            "SupportCastOnMeleeKill", "Cast_on_Melee_Kill", "近战击败时施放(辅)",
            "gem_red", "辅助",
        )
        gems, skipped = extract_gems(
            pob_root(GEMS_LUA_WITH_INSTANT, SKILLS_LUA_WITH_INSTANT),
            gem_page(ICE_NOVA_ANCHOR, same_name),
            detail_html=self._detail(
                {"Cast_on_Melee_Kill": detail_page("近战击败时施放(辅)", "Cast on Melee Kill")}
            ),
        )
        self.assertEqual(
            {"IceNova", "SupportCastOnMeleeKill"}, {g.gem_id for g in gems}
        )
        self.assertEqual(
            ["SupportCastOnMeleeKillInstant"], [s.gem_key for s in skipped]
        )
        self.assertIn("已属于另一颗已入库的宝石", skipped[0].detail)

    def test_兜底上来之后清单页那一侧不再算缺口(self) -> None:
        """同一颗宝石不能既算已入库、又算「PoB 未发布」——那是台账里的重复计数。
        实测 1 条：清单页的 `VaalLightningWarp` 对上 PoB 的 `*Instant`。"""
        alias_anchor = gem_anchor(
            "SupportCastOnMeleeKillOld", "Cast_on_Melee_Kill", "近战击败时施放(辅)",
            "gem_red", "辅助",
        )
        gems, skipped = extract_gems(
            pob_root(), gem_page(ICE_NOVA_ANCHOR, alias_anchor),
            detail_html=self._detail(
                {"Cast_on_Melee_Kill": detail_page("近战击败时施放(辅)", "Cast on Melee Kill")}
            ),
        )
        self.assertEqual(
            {"IceNova", "SupportCastOnMeleeKill"}, {g.gem_id for g in gems}
        )
        self.assertEqual([], [s for s in skipped if s.reason == SKIP_POB_MISSING])

    def test_只有未桥接的才去查详情页(self) -> None:
        """已经桥上的不该再发请求——810 颗各来一次就是 810 个多余请求。"""
        extract_gems(
            pob_root(), gem_page(ICE_NOVA_ANCHOR),
            detail_html=self._detail({}),
        )
        self.assertEqual(["Cast_on_Melee_Kill"], self.seen)


class DetailNameParseTest(unittest.TestCase):
    """标题解析的两道校验，缺一条都可能把别的东西的名字装到这颗宝石上。"""

    def test_取标题里破折号前那一段(self) -> None:
        self.assertEqual(
            "时空之门",
            parse_poedb_detail_name(detail_page("时空之门", "Portal"), name_en="Portal"),
        )

    def test_页面里没有这颗宝石的英文名就拒绝(self) -> None:
        """slug 猜错时 POEDB 会给别的条目或搜索页，那时英文名不会出现。"""
        self.assertIsNone(
            parse_poedb_detail_name(detail_page("号召", "Convocation"), name_en="Portal")
        )

    def test_标题不是那个形状就拒绝(self) -> None:
        html = "<html><head><title>时空之门</title></head><body>Portal</body></html>"
        self.assertIsNone(parse_poedb_detail_name(html, name_en="Portal"))

    def test_空页面不报错(self) -> None:
        self.assertIsNone(parse_poedb_detail_name("", name_en="Portal"))

    def test_slug由英文名下划线化(self) -> None:
        self.assertEqual("Pact_of_Beidat", poedb_detail_slug("Pact of Beidat"))
        self.assertEqual("Portal", poedb_detail_slug(" Portal "))


class DualTranslationTest(unittest.TestCase):
    """同一颗宝石的两种官方译名：取正名 + 另译进别名，不整条丢掉。"""

    def _page(self) -> str:
        # 国服译名在主清单（带标签格），繁中译名在页面后段（无标签格）
        return gem_page(
            gem_anchor("RaiseZombie", "Raise_Zombie", "魔卫复苏", "gem_blue", "法术, 召唤生物"),
            gem_anchor("RaiseZombie", "Raise_Zombie", "僵尸复苏", "gem_blue", None),
        )

    def test_文档顺序第一个是正名其余进另译(self) -> None:
        parsed, ambiguous = parse_poedb_gems(self._page())
        self.assertEqual(set(), ambiguous)
        entry = parsed["RaiseZombie"]
        self.assertEqual("魔卫复苏", entry.name_zh)
        self.assertEqual(("僵尸复苏",), entry.name_zh_alt)

    def test_标签从带标签的那一处取到(self) -> None:
        """两处只有一处有标签格，另一处不能把它覆盖成空。"""
        parsed, _ = parse_poedb_gems(self._page())
        self.assertEqual(("法术", "召唤生物"), parsed["RaiseZombie"].tags_zh)

    def test_同名重复出现不会被当成第二个译名(self) -> None:
        page = gem_page(ICE_NOVA_ANCHOR, ICE_NOVA_ANCHOR)
        parsed, _ = parse_poedb_gems(page)
        self.assertEqual((), parsed["IceNova"].name_zh_alt)

    def test_slug不同才是真歧义_跳过并登记(self) -> None:
        page = gem_page(
            gem_anchor("IceNova", "Ice_Nova", "冰霜新星", "gem_blue", "法术"),
            gem_anchor("IceNova", "Ice_Nova_Other", "另一颗", "gem_blue", "法术"),
            CASE_DIFF_ANCHOR,
        )
        parsed, ambiguous = parse_poedb_gems(page)
        self.assertEqual({"IceNova"}, ambiguous)
        self.assertNotIn("IceNova", parsed)
        gems, skipped = extract_gems(pob_root(), page)
        self.assertEqual(["SupportCastOnMeleeKill"], [g.gem_id for g in gems])
        self.assertIn("IceNova", [s.gem_key for s in skipped if s.reason == SKIP_NO_ZH])

    def test_整页都是歧义时报错指向数据而不是抽取器(self) -> None:
        """两种空要分开说：解析不到链接是结构变了，全是歧义是数据问题。
        混成一句会让下一个人去改抽取器，而该看的是页面内容。"""
        page = gem_page(
            gem_anchor("IceNova", "Ice_Nova", "冰霜新星", "gem_blue", "法术"),
            gem_anchor("IceNova", "Ice_Nova_Other", "另一颗", "gem_blue", "法术"),
        )
        with self.assertRaises(GemStructureError) as ctx:
            parse_poedb_gems(page)
        self.assertIn("无法唯一命名", str(ctx.exception))

    def test_另译入别名表(self) -> None:
        """别名由 `retrieval.alias.aliases_of` 统一决定，导入器不自己插表。"""
        from retrieval.alias import aliases_of

        entity = Entity(
            entity_id="skill:RaiseZombie",
            entity_type=EntityType.SKILL,
            name_zh="魔卫复苏",
            name_en="Raise Zombie",
            slug="Raise_Zombie",
            attributes={"name_zh_alt": ["僵尸复苏"]},
            ref=SourceRef(source=Source.POB.value, url="https://example/x", priority=2),
        )
        self.assertIn(("僵尸复苏", "name_zh_alt"), aliases_of(entity))


class LiteralValueTest(unittest.TestCase):
    """只取字面值，且口径以 PoB 自己的 tooltip 为准。"""

    def _gems(self) -> dict:
        gems, _ = extract_gems(pob_root(), gem_page(ICE_NOVA_ANCHOR, CASE_DIFF_ANCHOR))
        return {g.gem_id: g for g in gems}

    def test_每级字面值原样入库(self) -> None:
        """抽取层**不做**加 100 / 乘 100 的换算：换算只在渲染层做一次。"""
        ice = self._gems()["IceNova"]
        first, second = ice.levels
        self.assertEqual((1, 12, {"Mana": 8.0}, 7.5, 2.3),
                         (first.gem_level, first.level_requirement, first.cost,
                          first.crit_chance, first.damage_effectiveness))
        self.assertEqual(70, second.level_requirement)
        support = self._gems()["SupportCastOnMeleeKill"]
        self.assertEqual(150, support.levels[0].mana_multiplier)
        self.assertEqual(0.25, support.levels[0].cooldown)

    def test_没有属性需求字段(self) -> None:
        """`reqStr/reqDex/reqInt` 是换算系数不是显示值，所以整个维度不入库。

        PoB 算的是 round((20 + (等级需求 - 3) × 3) × (系数/100)^0.9 × statType)，
        把系数当需求写出去就是个带引用的错数字。
        """
        ice = self._gems()["IceNova"]
        for field in ("req_str", "req_dex", "req_int", "requirements"):
            self.assertFalse(hasattr(ice, field), field)

    def test_没有伤害字段(self) -> None:
        """PoB 的每级伤害是乘数，要按公式算才是显示值 —— 本轮不算也不存。"""
        ice = self._gems()["IceNova"]
        for field in ("damage", "damage_min", "damage_max", "base_damage"):
            self.assertFalse(hasattr(ice.levels[0], field), field)

    def test_消耗按Costs的除数换算(self) -> None:
        skills = SKILLS_LUA.replace("cost = { Mana = 8, }", "cost = { ManaPerMinute = 120, }")
        gems, _ = extract_gems(pob_root(skills=skills), gem_page(ICE_NOVA_ANCHOR))
        # 存每分钟、显示每秒（Divisor=60）：120 → 2
        self.assertEqual({"ManaPerMinute": 2.0}, gems[0].levels[0].cost)

    def test_未知资源直接报错而不是丢掉(self) -> None:
        """静默丢掉会让某些技能的消耗凭空消失，而「空」与「无消耗」无法区分。"""
        skills = SKILLS_LUA.replace("cost = { Mana = 8, }", "cost = { Chaos = 3, }")
        with self.assertRaises(GemStructureError) as ctx:
            extract_gems(pob_root(skills=skills), gem_page(ICE_NOVA_ANCHOR))
        self.assertIn("Chaos", str(ctx.exception))

    def test_技能类型取枚举名而不是内部表示(self) -> None:
        ice = self._gems()["IceNova"]
        self.assertEqual(("Cold", "Spell"), ice.skill_types)


class TransfiguredTest(unittest.TestCase):
    """蜕变宝石判据只看 `AltX` 后缀。"""

    def test_AltX后缀判为蜕变(self) -> None:
        gems_lua = GEMS_LUA.replace('grantedEffectId = "IceNova"',
                                    'grantedEffectId = "IceNovaAltX"')
        skills_lua = SKILLS_LUA.replace('skills["IceNova"]', 'skills["IceNovaAltX"]')
        anchor = gem_anchor("IceNovaAltX", "Ice_Nova_of_Frostbite", "霜咬之冰霜新星",
                            "gem_blue", "法术")
        gems, _ = extract_gems(pob_root(gems=gems_lua, skills=skills_lua), gem_page(anchor))
        self.assertTrue(gems[0].is_transfigured)

    def test_名字里带of的常规宝石不被误判(self) -> None:
        """实测 23 条误判：火焰净化（Purity_of_Fire）、箭雨（Rain_of_Arrows）、
        灰烬之捷（Herald_of_Ash）都是常规宝石，英文名里本来就有 of。"""
        anchor = gem_anchor("IceNova", "Purity_of_Fire", "火焰净化", "gem_blue", "法术")
        gems, _ = extract_gems(pob_root(), gem_page(anchor))
        self.assertFalse(gems[0].is_transfigured)


# ── 卡片 ──────────────────────────────────────────────────────────


def _entity(levels: list[dict], **overrides) -> Entity:
    attrs = {
        "gem_kind": "active",
        "attr_color": "int",
        "name_zh_alt": [],
        "tags": ["spell"],
        "tags_zh": ["法术", "冰霜"],
        "tag_string": "Spell, Cold",
        "natural_max_level": 20,
        "cast_time": 0.7,
        "skill_types": ["Spell"],
        "is_vaal": False,
        "is_transfigured": False,
        "description_en": "A circle of ice expands from the caster.",
        "source_file": "Data/Skills/act_int.lua",
        "poedb_url": "https://poedb.tw/cn/Ice_Nova",
        "levels": levels,
    }
    attrs.update(overrides)
    return Entity(
        entity_id=make_entity_id(EntityType.SKILL, "IceNova"),
        entity_type=EntityType.SKILL,
        name_zh="冰霜新星",
        name_en="Ice Nova",
        slug="Ice_Nova",
        attributes=attrs,
        ref=SourceRef(source=Source.POB.value, url="https://example/blob/act_int.lua", priority=2),
    )


def _level(gem_level: int, **kwargs) -> dict:
    row = {
        "gem_level": gem_level, "level_requirement": None, "cost": {}, "cooldown": None,
        "stored_uses": None, "crit_chance": None, "mana_multiplier": None,
        "attack_speed_multiplier": None, "reservation_percent": None,
        "damage_effectiveness": None,
    }
    row.update(kwargs)
    return row


class CardTest(unittest.TestCase):
    LEVELS = [
        _level(1, level_requirement=12, cost={"Mana": 8}, crit_chance=7.5, damage_effectiveness=2.3),
        _level(20, level_requirement=70, cost={"Mana": 23}, crit_chance=7.5, damage_effectiveness=2.5),
        _level(40, level_requirement=100, cost={"Mana": 31}, crit_chance=7.5, damage_effectiveness=2.5),
    ]

    def test_卡片里不出现伤害二字(self) -> None:
        """这是架构判据不是文风：充分依据的判据是查询实义词在 `chunk.text` 里的
        覆盖率，把「伤害」写进 810 张卡片等于给每个含「伤害」的问题送一份覆盖率，
        而上一轮刚把检索侧漏出压到 0%。边界声明属于覆盖层。
        """
        card = render_skills.render_card(_entity(self.LEVELS))
        self.assertNotIn("伤害", card)
        # 附加伤害的效果（damageEffectiveness）也不进卡片：它是加成系数不是技能伤害，
        # 放进检索证据里模型很容易把它当伤害答出去，而那个数字确实在证据里
        self.assertNotIn("230", card)

    def test_卡片只列三个关键等级(self) -> None:
        card = render_skills.render_card(_entity(self.LEVELS))
        self.assertIn("- 1 级：需角色 12 级，消耗 8 魔力，暴击率 7.5%", card)
        self.assertIn("20 级（自然最高）", card)
        self.assertIn("40 级（PoB 数据上限，需腐化或 +等级）", card)

    def test_卡片不超过切片上限(self) -> None:
        card = render_skills.render_card(_entity(self.LEVELS))
        self.assertLessEqual(len(card), render_skills.MAX_CHUNK_CHARS)
        self.assertEqual(1, len(render_skills.to_chunks(_entity(self.LEVELS))))

    def test_另译写在卡片上(self) -> None:
        card = render_skills.render_card(_entity(self.LEVELS, name_zh_alt=["僵尸复苏"]))
        self.assertIn("另译：僵尸复苏", card)

    def test_倍率按游戏显示口径渲染(self) -> None:
        """PoB 存增量，游戏显示 100 + 增量（GemTooltip.lua 的口径）。"""
        line = render_skills.level_line(_level(1, mana_multiplier=150))
        self.assertIn("消耗与保留倍率 250%", line)
        line = render_skills.level_line(_level(1, attack_speed_multiplier=-20))
        self.assertIn("攻击速度为基础的 80%", line)

    def test_缺的字段不写成0(self) -> None:
        """「没有冷却」与「冷却 0 秒」不是一回事。"""
        line = render_skills.level_line(_level(5, level_requirement=27))
        self.assertEqual("需角色 27 级", line)

    def test_可存次数只在大于1时写(self) -> None:
        self.assertNotIn("可存", render_skills.level_line(_level(1, cooldown=0.25, stored_uses=1)))
        self.assertIn("可存 3 次", render_skills.level_line(_level(1, cooldown=4, stored_uses=3)))


# ── 工具 ──────────────────────────────────────────────────────────

def _tool_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO pob_skill_gem(
            gem_id,name_en,name_zh,name_zh_alt,poedb_slug,gem_kind,attr_color,tags,
            tag_string,tags_zh,natural_max_level,cast_time,skill_types,is_vaal,
            is_transfigured,description_en,source_file,source,source_priority,pob_url,
            poedb_url,source_commit,pob_version,game_version,content_hash,updated_at
        ) VALUES ('IceNova','Ice Nova','冰霜新星','["霜华"]','Ice_Nova','active','int',
            '["spell"]','Spell, Cold','["法术","冰霜"]',20,0.7,'["Spell"]',0,0,
            'desc','Data/Skills/act_int.lua','Path of Building',2,
            'https://github.com/x/blob/abc/src/Data/Skills/act_int.lua',
            'https://poedb.tw/cn/Ice_Nova',?,'PoB 2.66.2','3.29','hash',datetime('now'))
        """,
        ("a" * 40,),
    )
    conn.executemany(
        "INSERT INTO pob_skill_level(gem_id,gem_level,level_requirement,cost,crit_chance,"
        "damage_effectiveness) VALUES ('IceNova',?,?,?,7.5,2.5)",
        [(lv, 10 + lv * 3, json.dumps({"Mana": 7 + lv})) for lv in range(1, 41)],
    )
    conn.commit()
    return conn


class GemToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _tool_conn()
        self.patch = patch("tools.impl.gems.dbmod.get_conn", return_value=self.conn)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.conn.close()

    def test_按中文名查到并写出总级数(self) -> None:
        result = skill_gem_levels("冰霜新星")
        self.assertTrue(result.data["found"])
        self.assertEqual(40, result.data["gems"][0]["levels_total"])
        self.assertIn("本库共 40 级", result.citations[0]["text"])

    def test_截断必须写出还剩多少(self) -> None:
        """静默截断等同于漏答：模型会把截断处当成「这颗宝石只有这么多级」。"""
        result = skill_gem_levels("冰霜新星")
        shown = result.data["gems"][0]["levels_shown"]
        self.assertLessEqual(shown, MAX_LEVEL_ROWS)
        self.assertIn(f"以上仅列 {shown} 级", result.citations[0]["text"])

    def test_指定等级只给那一级(self) -> None:
        result = skill_gem_levels("冰霜新星", level=20)
        text = result.citations[0]["text"]
        self.assertIn("- 20 级：需角色 70 级，消耗 27 魔力", text)
        self.assertNotIn("- 19 级", text)

    def test_越界等级给缺据而不是空表(self) -> None:
        result = skill_gem_levels("冰霜新星", level=99)
        self.assertFalse(result.data["found"])
        self.assertEqual("1-40", result.data["available_levels"])
        self.assertEqual([], result.citations)

    def test_查不到给缺据与换词提示(self) -> None:
        result = skill_gem_levels("不存在的宝石")
        self.assertFalse(result.data["found"])
        self.assertIn("hint", result.data)
        self.assertEqual([], result.citations)

    def test_另译也能查到(self) -> None:
        self.assertTrue(skill_gem_levels("霜华").data["found"])

    def test_返回里带边界声明且走既有的附注机制(self) -> None:
        """字段名必须是 `boundary`：`orchestrator/loop._NOTE_FIELDS` 认的是这个名字，
        它会把这句话由**代码**逐字附在答案末尾。让模型自己说会被出口闸门拦掉
        （同一个模式已经撞过四次）。证据正文里也要有，这样模型引用它是有据的。
        """
        from orchestrator.loop import _NOTE_FIELDS

        result = skill_gem_levels("冰霜新星")
        self.assertIn("boundary", _NOTE_FIELDS)
        self.assertIn("伤害数值", result.data["boundary"])
        self.assertIn("本库未采集", result.citations[0]["text"])

    def test_引用指向PoB钉commit地址(self) -> None:
        """数值出自 PoB 的那个文件，引用就得指向它，而不是中文名对照页。"""
        citation = skill_gem_levels("冰霜新星", level=1).citations[0]
        self.assertIn("/blob/", citation["url"])
        self.assertEqual(2, citation["source_priority"])
        self.assertTrue(citation["can_support_facts"])

    def test_附加伤害效果按百分数给且标签写全(self) -> None:
        """工具里可以给它——用户问的是那一级的完整数据；但标签必须写全名，
        免得被当成「技能伤害」。口径 ×100 来自 GemTooltip.lua。"""
        text = skill_gem_levels("冰霜新星", level=20).citations[0]["text"]
        self.assertIn("附加伤害的效果 250%", text)

    def test_空查询直接失败(self) -> None:
        self.assertFalse(skill_gem_levels("  ").ok)


if __name__ == "__main__":
    unittest.main()
