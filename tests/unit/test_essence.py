"""精华物品：抽取、渲染、工具与路由（2026-09-02 第五十九轮）。

判据来自实测（`/cn/Essence` 的 `精华` 标签页，105 件 / 632 条分组行）：

* 这一页有 **9 个 `tab-pane`**，其中 4 个都含精华卡，而**选中的是 `MinimapIcons`**
  （0 张卡）——所以按标签页 **id** 取，既不按位置也不按选中态；
* 条数由标签页标题自证（`#精华 /105`），对不上整页拒绝；
* 分组行的类别名**最短 1 字**（`弓` `爪`），正则下界写 2 会把
  `弓: 此物品上装备的弓技能石等级+2` 并进基础效果文本；
* `level_cap` 为 NULL 是「**无等级上限**」这个确定事实，不是「未知」。

这个文件不连网：HTML 是内联构造的最小样例，库用内存 SQLite + 真实 SCHEMA。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from entities.models import Entity, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, Source  # noqa: E402
from kb.render_stackable import render_card  # noqa: E402
from kb.sources.poedb_essence import (  # noqa: E402
    CLASSES_PER_ESSENCE,
    EssenceStructureError,
    parse_essences,
)

_HOVER = "?s=Data%5CBaseItemTypes%2FMetadata%2FItems%2FCurrency%2FCurrencyEssence"

#: 22 个装备类别，按页面真实的四组切分
_G1 = "箭袋, 头部, 胸甲, 鞋子, 手套, 腰带, 盾牌"          # 7
_G2 = "弓, 长杖, 双手剑, 双手斧, 双手锤"                   # 5
_G3 = "法杖, 爪, 匕首, 单手剑, 细剑, 单手斧, 单手锤, 短杖"  # 8
_G4 = "项链, 戒指"                                        # 2


def card(name: str, slug: str, tier: int, *, level_cap: int | None = 35,
         extra: str = "", base: str = "升级一件普通物品至稀有物品并且确保一项特定属性。") -> str:
    mods = [f'<div class="explicitMod">{base}</div>']
    if level_cap is not None:
        mods.append(f'<div class="explicitMod">属性限制等级需求{level_cap}与以下物品</div>')
    if extra:
        mods.append(f'<div class="explicitMod">{extra}</div>')
    # `extra` 是「把某个类别单独拎出来」的写法（页面上 `弓:` 就是这样），
    # 所以双手组要相应去掉「弓」——总数必须仍是 22
    two_hand = _G2.replace("弓, ", "") if extra else _G2
    mods += [
        f'<div class="explicitMod">{_G1}: +(6—11)% 冰霜抗性</div>',
        f'<div class="explicitMod">{two_hand}: 该装备附加 (2—3) - (6—7) 基础冰霜伤害</div>',
        f'<div class="explicitMod">{_G3}: 该装备附加 (1—2) - (3—4) 基础冰霜伤害</div>',
        f'<div class="explicitMod">{_G4}: 冰霜伤害提高 (6—10)%</div>',
    ]
    link = (f'<a class="item_currency StackableCurrency" data-hover="{_HOVER}{slug}" '
            f'href="{slug}">{name}</a>')
    return (
        '<div class="d-flex border-top rounded"><div class="flex-shrink-0">'
        f'<a class="item_currency StackableCurrency" data-hover="{_HOVER}{slug}" '
        f'href="{slug}"><img alt="i"></a></div>'
        f'<div class="flex-grow-1 ms-2">{link}<div>'
        '<div class="property">堆叠数量: <span>1 / 10</span></div>'
        f'<div class="property">Essence Tier: <span>{tier}</span></div>'
        + "".join(mods) + "</div></div></div>"
    )


def page(*cards: str, declared: int | None = None, pane: str = "精华",
         decoy: str = "") -> str:
    n = len(cards) if declared is None else declared
    return (
        '<html><body>'
        '<a class="nav-link active" href="#MinimapIcons">Minimap Icons /1</a>'
        f'<a class="nav-link" href="#{pane}">精华 /{n}</a>'
        '<div class="tab-pane fade show active" id="MinimapIcons"></div>'
        f'<div class="tab-pane fade" id="{pane}">{"".join(cards)}</div>'
        f'<div class="tab-pane fade" id="精华物品">{decoy}</div>'
        '</body></html>'
    )


class TestParseEssences(unittest.TestCase):
    def test_tooltip哈希沿用已验证精华身份(self) -> None:
        slug = "Whispering_Essence_of_Hatred"
        html = page(card("憎恨之低语精华", slug, 1))
        html = html.replace(_HOVER + slug, "https://cdn.poedb.tw/cache/hash")
        known = {slug: "Metadata/Items/Currency/CurrencyEssenceHatred1"}
        got = parse_essences(html, known_keys_by_slug=known)
        self.assertEqual(known[slug], got[0].key)

    def test_按标签页_id_取而不是选中态(self) -> None:
        """选中的是 `MinimapIcons`（0 张卡）。按选中态取会得到空，
        按位置取会撞上同页另外三个近似区块。"""
        html = page(card("憎恨之低语精华", "Whispering_Essence_of_Hatred", 1),
                    decoy=card("不该被取到", "Decoy", 9))
        got = parse_essences(html)
        self.assertEqual(["憎恨之低语精华"], [e.name_zh for e in got])

    def test_条数与页面声明对不上就整页拒绝(self) -> None:
        """静默少收等于漏答，而这一维的用户问题正是「有没有这个精华」。"""
        html = page(card("憎恨之低语精华", "Whispering_Essence_of_Hatred", 1), declared=105)
        with self.assertRaises(EssenceStructureError):
            parse_essences(html)

    def test_装备类别数不足即拒绝(self) -> None:
        html = (
            '<html><body><a class="nav-link" href="#精华">精华 /1</a>'
            '<div class="tab-pane" id="精华">'
            '<div class="d-flex border-top rounded"><div class="flex-grow-1 ms-2">'
            f'<a class="item_currency" data-hover="{_HOVER}X" href="X">残缺精华</a><div>'
            '<div class="property">堆叠数量: <span>1 / 10</span></div>'
            '<div class="property">Essence Tier: <span>1</span></div>'
            '<div class="explicitMod">升级一件普通物品至稀有物品并且确保一项特定属性。</div>'
            f'<div class="explicitMod">{_G4}: 冰霜伤害提高 (6—10)%</div>'
            '</div></div></div></div></body></html>'
        )
        with self.assertRaisesRegex(EssenceStructureError, str(CLASSES_PER_ESSENCE)):
            parse_essences(html)

    def test_单个装备类别的行不算基础效果(self) -> None:
        """`弓` `爪` 都是单字类别名。正则下界写 2 会把这一行并进基础效果文本，
        实测页面上有 3 条这种写法（忌惮之破空精华的弓技能石等级）。"""
        got = parse_essences(page(card(
            "忌惮之破空精华", "Deafening_Essence_of_Dread", 7,
            level_cap=None, extra="弓: 此物品上装备的弓技能石等级+2",
        )))[0]
        self.assertNotIn("弓技能石等级", got.base_effect)
        self.assertIn(("弓",), [m.item_classes for m in got.mods])
        self.assertEqual(CLASSES_PER_ESSENCE, got.item_class_count)

    def test_没有等级上限时留空而不是填零(self) -> None:
        """tier 5 起本来就没有上限。填 0 会把一条确定的事实说成「上限是 0」。"""
        got = parse_essences(page(card("憎恨之破空精华", "Deafening_Essence_of_Hatred", 7,
                                       level_cap=None)))[0]
        self.assertIsNone(got.level_cap)

    def test_等级上限与堆叠上限分别取到(self) -> None:
        got = parse_essences(page(card("憎恨之低语精华", "Whispering_Essence_of_Hatred", 1)))[0]
        self.assertEqual(35, got.level_cap)
        self.assertEqual(10, got.stack_max)
        self.assertEqual(1, got.tier)
        self.assertEqual(CLASSES_PER_ESSENCE, got.item_class_count)

    def test_tier_不是数字就拒绝(self) -> None:
        html = page(card("坏精华", "Bad", 1)).replace(
            '<span>1</span>', '<span>一</span>')
        with self.assertRaises(EssenceStructureError):
            parse_essences(html)

    def test_tier_零是上游真值不是错误(self) -> None:
        """实测 `哀恸精华` 的 `Essence Tier` 就是 0（PoB 侧写 8）。
        建模不能把「我以为的范围」写成校验——那会把上游真值判成脏数据。"""
        got = parse_essences(page(card("哀恸精华", "Essence_of_Desolation", 0,
                                       level_cap=15)))[0]
        self.assertEqual(0, got.tier)
        self.assertEqual(15, got.level_cap)


class TestRenderCard(unittest.TestCase):
    def _entity(self, **attrs) -> Entity:  # noqa: ANN003
        base = {
            "metadata_id": "Metadata/Items/Currency/CurrencyEssenceHatred1",
            "stack_max": 10, "tier": 1, "level_cap": 35,
            "effect_zh": "升级一件普通物品至稀有物品并且确保一项特定属性。",
            "essence_mods": [{"item_classes": "项链、戒指", "text_zh": "冰霜伤害提高 (6—10)%"}],
        }
        base.update(attrs)
        return Entity(
            entity_id="essence:Whispering_Essence_of_Hatred",
            entity_type=EntityType.ESSENCE,
            name_zh="憎恨之低语精华", name_en="Whispering Essence of Hatred",
            slug="Whispering_Essence_of_Hatred", item_class="StackableCurrency",
            attributes=base,
            ref=SourceRef(source=Source.POEDB.value, url="https://poedb.tw/cn/Essence#x",
                          priority=1, fetched_at="2026-09-02"),
        )

    def test_卡上有等阶与逐类别保底词缀(self) -> None:
        """**没有保底词缀这一段，105 张卡里有 64 张一模一样**——tier 5–8 的
        基础效果原文逐字相同。那正是收获目录当初决定不做卡片的理由。"""
        text = render_card(self._entity())
        self.assertIn("精华等阶 T1", text)
        self.assertIn("物品等级需求 35 及以下", text)
        self.assertIn("项链、戒指：冰霜伤害提高 (6—10)%", text)

    def test_无等级上限写成确定事实而不是未知(self) -> None:
        text = render_card(self._entity(level_cap=None))
        self.assertIn("无等级上限", text)
        self.assertNotIn("未知", text)

    def test_非精华实体不出这两段(self) -> None:
        """同一个渲染器还服务通货与圣甲虫，多写两段会污染它们的卡。"""
        entity = self._entity()
        entity.attributes.pop("tier")
        entity.attributes.pop("essence_mods")
        text = render_card(entity)
        self.assertNotIn("适用范围", text)
        self.assertNotIn("保底词缀", text)


class TestEssenceTool(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        rows = [
            ("m1", "憎恨之低语精华", "Whispering Essence of Hatred", "s1", 1, 10, 35, "升级白装"),
            ("m2", "憎恨之破空精华", "Deafening Essence of Hatred", "s2", 7, 10, None, "可重铸"),
            ("m3", "悲痛之破空精华", "Deafening Essence of Woe", "s3", 7, 10, None, "可重铸"),
        ]
        self.conn.executemany(
            "INSERT INTO poedb_essence(metadata_id,name_zh,name_en,slug,tier,stack_max,"
            "level_cap,base_effect,source,source_priority,url,fetched_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,'POEDB',1,'https://poedb.tw/cn/Essence','x','x')", rows)
        self.conn.executemany(
            "INSERT INTO poedb_essence_mod(metadata_id,group_index,item_classes,text_zh) "
            "VALUES (?,?,?,?)",
            [("m1", 0, "项链、戒指", "冰霜伤害提高 (6—10)%")])
        self.conn.commit()

    def _call(self, query: str):  # noqa: ANN202
        import core.db as dbmod
        from tools.impl.essence import essence_lookup
        original = dbmod.get_conn
        dbmod.get_conn = lambda: self.conn  # type: ignore[assignment]
        try:
            return essence_lookup(query)
        finally:
            dbmod.get_conn = original  # type: ignore[assignment]

    def test_按名查带出逐类别保底词缀(self) -> None:
        res = self._call("憎恨之低语精华")
        self.assertTrue(res.data["found"])
        self.assertIn("项链、戒指：冰霜伤害提高 (6—10)%", res.citations[0]["text"])

    def test_按等阶枚举(self) -> None:
        res = self._call("破空")
        self.assertEqual(2, res.data["total"])
        self.assertIn("T7（破空）精华", res.citations[0]["text"])

    def test_具名精华不被按阶枚举抢走(self) -> None:
        """「憎恨之破空精华」含「破空」，但它问的是一件具体的精华。"""
        res = self._call("憎恨之破空精华")
        self.assertEqual(1, res.data["total"])
        self.assertEqual("憎恨之破空精华", res.data["essences"][0]["name_zh"])

    def test_按有无等级上限枚举(self) -> None:
        res = self._call("哪些精华没有等级上限")
        self.assertEqual(2, res.data["total"])
        self.assertIn("无等级上限", res.citations[0]["text"])

    def test_查不到时说清是哪一种缺(self) -> None:
        res = self._call("不存在的精华")
        self.assertFalse(res.data["found"])
        self.assertIn("词缀组卡", res.data["reason"])

    def test_标题里的条数是截断前的(self) -> None:
        """拿 `len(shown)` 当标题里的数会把「共 64 件」写成「共 24 件」，
        那就是「静默截断等同于漏答」的一种。"""
        import tools.impl.essence as mod
        original = mod.MAX_ROWS
        mod.MAX_ROWS = 1
        try:
            res = self._call("破空")
        finally:
            mod.MAX_ROWS = original
        self.assertEqual(2, res.data["total"])
        self.assertEqual(1, res.data["shown"])
        self.assertTrue(res.data["truncated"])
        self.assertIn("共 2 件", res.citations[0]["text"])
        self.assertIn("还有 1 件未列出", res.citations[0]["text"])


class TestRouting(unittest.TestCase):
    def test_枚举问法路由到精华工具(self) -> None:
        from orchestrator.loop import _preferred_tool_route
        for query in ("哪些精华没有等级上限", "破空精华有哪些", "精华的等阶是怎么分的"):
            self.assertEqual("essence_lookup", _preferred_tool_route(query), query)

    def test_词缀阶梯问法不被抢走(self) -> None:
        """**判据里没有裸的「精华」**：那两个字既是物品名也是词缀来源。"""
        from orchestrator.loop import _preferred_tool_route
        self.assertEqual(
            "mod_tiers",
            _preferred_tool_route("戒指上冰霜伤害提高的精华词缀有哪些档位"),
        )

    def test_具名精华走普通检索(self) -> None:
        """105 张卡自己答得了「这个精华是什么」，不必进工具。"""
        from orchestrator.loop import _preferred_tool_route
        self.assertEqual("", _preferred_tool_route("憎恨之低语精华有什么用"))


if __name__ == "__main__":
    unittest.main()


class TestContentHash(unittest.TestCase):
    """逐件内容哈希（2026-09-03 第六十二轮补）。

    第五十九轮入库时这一列写的是 `res.content_hash`——那是**整页**哈希，
    于是 105 行全写成同一个值（实测 `count(distinct)=1`，而通货 113/113、
    圣甲虫 118/118、地图 159/159 都是逐行不同）；实体那一侧更彻底，
    `Entity(...)` 整个漏了 `content_hash=`，105 个精华实体全是空串，
    增量判据把它们当成「每次都变了」，`selfcheck.py` 一直在报。

    两条判据都要钉住：**逐件不同**，以及**保底词缀参与哈希**——
    后者是最容易漏的那一半，因为它渲染在卡片上却不是标量字段。
    """

    def test_不同精华的哈希不同(self) -> None:
        items = parse_essences(page(
            card("憎恨之低语精华", "Whispered_Essence_of_Hatred", 1),
            card("憎恨之呢喃精华", "Muttering_Essence_of_Hatred", 2),
            card("憎恨之啼泣精华", "Weeping_Essence_of_Hatred", 3),
            declared=3,
        ))
        hashes = {i.content_hash for i in items}
        self.assertEqual(3, len(hashes), "整页哈希会让这三件写成同一个值")

    def test_保底词缀变了哈希就变(self) -> None:
        """词缀渲染在卡片上却不是标量字段——只哈希标量就会漏掉它。"""
        before = parse_essences(page(card(
            "憎恨之低语精华", "Whispered_Essence_of_Hatred", 1,
            extra="弓: 此物品上装备的弓技能石等级+2"), declared=1))[0]
        after = parse_essences(page(card(
            "憎恨之低语精华", "Whispered_Essence_of_Hatred", 1,
            extra="弓: 此物品上装备的弓技能石等级+3"), declared=1))[0]
        self.assertEqual(before.name_zh, after.name_zh, "只有词缀该变")
        self.assertNotEqual(before.content_hash, after.content_hash)

    def test_同样的输入哈希稳定(self) -> None:
        """否则每次采集都会被判成「变了」，增量就永远不生效。"""
        made = [parse_essences(page(card("憎恨之低语精华",
                                         "Whispered_Essence_of_Hatred", 1),
                                    declared=1))[0].content_hash
                for _ in range(2)]
        self.assertEqual(made[0], made[1])

    def test_实体哈希把回填的英文名算进去(self) -> None:
        """`name_en` 由 `verify_essence.py --write` 事后回填，而卡片要渲染它——
        只用源哈希的话，回填之后卡片变了、哈希没变。"""
        from kb.pipeline import _essence_entity_hash  # noqa: PLC0415

        self.assertNotEqual(
            _essence_entity_hash("abc", None),
            _essence_entity_hash("abc", "Whispering Essence of Hatred"),
        )
        self.assertNotEqual(_essence_entity_hash("abc", "X"),
                            _essence_entity_hash("def", "X"))
