"""唯一装备详情页补词条：比对判据与合并前提（第三十四轮）。

入库主源是列表页（一页 2.5MB、1342 件全在里面），而**详情页偶尔更全**：
实测 195 张缓存页里 7 件详情页多出词条，多出来的全是「不可改变」——
那是「这件不能被改造」的事实，制作类问题正要用它。

用例按「该合的合、该拒的拒」成对写：

* 该合：详情页是列表页的**超集**，多出来的并进去，关键词跟着重算；
* 该拒：名字/底材/需求任一不一致，或列表页有而详情页没有的词条——
  那说明两侧可能不是同一版本的物品（实测撞到变体鞋、未翻译条目）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from entities.models import Entity, ModLine, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, ModKind, Source, make_entity_id  # noqa: E402
from kb.sources.poedb_unique import (  # noqa: E402
    DetailPage,
    compare_detail,
    parse_detail_page,
)

LIST_MODS = ["你地图中找到的地图数量提高 2%", "你的地图不会包含先祖秘藏遭遇战"]


def detail_page(mods: list[tuple[str, str]], **overrides) -> DetailPage:
    kinds = {"implicit": ModKind.IMPLICIT, "explicit": ModKind.EXPLICIT,
             "description": ModKind.DESCRIPTION}
    page = DetailPage(
        name_zh=overrides.get("name_zh", "贸易禁运"),
        name_en=overrides.get("name_en", "Trade Embargo"),
        base_type=overrides.get("base_type", "小型雕像"),
        requirement_raw=overrides.get("requirement_raw", "需求 等级 68"),
        mods=[ModLine(kind=kinds[k], text=t) for k, t in mods],
    )
    return page


def compare(page: DetailPage, *, mod_texts: list[str] | None = None, **overrides):  # noqa: ANN201
    return compare_detail(
        page,
        name_zh=overrides.get("name_zh", "贸易禁运"),
        base_type=overrides.get("base_type", "小型雕像"),
        requirement_raw=overrides.get("requirement_raw", "需求 等级 68"),
        mod_texts=LIST_MODS if mod_texts is None else mod_texts,
    )


class CompareTest(unittest.TestCase):
    def test_两侧一致时没有差异(self) -> None:
        diff = compare(detail_page([("implicit", LIST_MODS[0]), ("explicit", LIST_MODS[1])]))
        self.assertEqual((), diff.blocking)
        self.assertEqual((), diff.detail_only)
        self.assertFalse(diff.mergeable)

    def test_详情页多出词条时可合并(self) -> None:
        """实测原型：7 件物品的「不可改变」只在详情页上。"""
        diff = compare(detail_page([
            ("implicit", LIST_MODS[0]), ("explicit", LIST_MODS[1]), ("explicit", "不可改变"),
        ]))
        self.assertEqual(("不可改变",), diff.detail_only)
        self.assertTrue(diff.mergeable)

    def test_列表页有而详情页没有就拒绝合并(self) -> None:
        """实测撞到「秽生之提灯」：列表页有 4 条详情页没有，其中还有未翻译的英文。
        这时两侧讲的可能不是同一版本，合并会把词条换少。"""
        diff = compare(detail_page([("implicit", LIST_MODS[0])]))
        self.assertEqual((LIST_MODS[1],), diff.list_only)
        self.assertFalse(diff.mergeable)
        self.assertIn("列表页有而详情页没有", diff.blocking[0])

    def test_底材不一致就拒绝合并(self) -> None:
        """实测：阿伯瑞斯列表页写「暗影弓」、详情页写「刺客弓」。"""
        page = detail_page([("implicit", LIST_MODS[0]), ("explicit", LIST_MODS[1]),
                            ("explicit", "不可改变")], base_type="刺客弓")
        diff = compare(page)
        self.assertFalse(diff.mergeable)
        self.assertIn("底材不一致", diff.blocking[0])

    def test_需求不一致就拒绝合并(self) -> None:
        page = detail_page([("implicit", LIST_MODS[0]), ("explicit", LIST_MODS[1]),
                            ("explicit", "不可改变")],
                           requirement_raw="需求 等级 68, 8 力量")
        self.assertFalse(compare(page).mergeable)

    def test_需求只有排版差异不算不一致(self) -> None:
        """压缩空白后再比：把排版差异当数据差异会造出一堆假冲突。"""
        page = detail_page([("implicit", LIST_MODS[0]), ("explicit", LIST_MODS[1]),
                            ("explicit", "不可改变")],
                           requirement_raw="需求  等级 68")
        self.assertTrue(compare(page).mergeable)

    def test_中文名不一致就拒绝合并(self) -> None:
        page = detail_page([("implicit", LIST_MODS[0]), ("explicit", LIST_MODS[1]),
                            ("explicit", "不可改变")], name_zh="另一件装备")
        self.assertFalse(compare(page).mergeable)

    def test_描述性说明不参与比对(self) -> None:
        """括号说明两侧换行与截断方式不同，拿它比会造出假差异。"""
        page = detail_page([
            ("implicit", LIST_MODS[0]), ("explicit", LIST_MODS[1]),
            ("description", "（深渊、菌潮遭遇战、裂隙……均为可关闭的额外内容）"),
        ])
        diff = compare(page)
        self.assertEqual((), diff.detail_only)
        self.assertEqual((), diff.list_only)

    def test_缺一侧的字段不算不一致(self) -> None:
        """详情页没给需求时不比需求——缺值不是分歧。"""
        page = detail_page([("implicit", LIST_MODS[0]), ("explicit", LIST_MODS[1]),
                            ("explicit", "不可改变")], requirement_raw="")
        self.assertTrue(compare(page).mergeable)


class MergeTest(unittest.TestCase):
    """合并是**整份换成详情页的词缀**，而且关键词要跟着重算。"""

    def _entity(self) -> Entity:
        return Entity(
            entity_id=make_entity_id(EntityType.UNIQUE_ITEM, "Trade_Embargo"),
            entity_type=EntityType.UNIQUE_ITEM,
            name_zh="贸易禁运",
            name_en="Trade Embargo",
            slug="Trade_Embargo",
            base_type="小型雕像",
            mods=[ModLine(kind=ModKind.EXPLICIT, text=t) for t in LIST_MODS],
            search_keywords=["贸易禁运", "地图"],
            ref=SourceRef(source=Source.POEDB.value,
                          url="https://poedb.tw/cn/Trade_Embargo", priority=1),
        )

    def test_词缀整份替换并保留顺序(self) -> None:
        from scripts.import_unique_detail import merged_entity

        detail_mods = [
            ModLine(kind=ModKind.IMPLICIT, text=LIST_MODS[0]),
            ModLine(kind=ModKind.EXPLICIT, text=LIST_MODS[1]),
            ModLine(kind=ModKind.EXPLICIT, text="不可改变"),
        ]
        merged = merged_entity(self._entity(), detail_mods)
        self.assertEqual(
            [LIST_MODS[0], LIST_MODS[1], "不可改变"], [m.text for m in merged.mods]
        )
        # 详情页还给出了 implicit/explicit 的正确归属，列表页那份是全 explicit
        self.assertEqual(ModKind.IMPLICIT, merged.mods[0].kind)

    def test_关键词跟着重算(self) -> None:
        """新词条不进关键词就等于「入库了但检索不到」——这个项目撞过三次的坑。"""
        from scripts.import_unique_detail import merged_entity

        detail_mods = [
            ModLine(kind=ModKind.EXPLICIT, text=t) for t in [*LIST_MODS, "不可改变"]
        ]
        merged = merged_entity(self._entity(), detail_mods)
        self.assertIn("改变", " ".join(merged.search_keywords))

    def test_原有字段不被动(self) -> None:
        from scripts.import_unique_detail import merged_entity

        merged = merged_entity(self._entity(), [ModLine(kind=ModKind.EXPLICIT, text="不可改变")])
        self.assertEqual("贸易禁运", merged.name_zh)
        self.assertEqual("Trade_Embargo", merged.slug)
        self.assertEqual("https://poedb.tw/cn/Trade_Embargo", merged.ref.url)


class ContentHashTest(unittest.TestCase):
    """`content_hash` 是增量更新的判断依据，重渲染路径**不许把它写空**。

    第三十四轮实测到这条：唯一装备合并之后那一行的 `content_hash` 变成了空字符串，
    顺着查下去发现库里已经有 **931 行**是空的（820 颗宝石 + 111 件 L2 补进来的
    唯一装备）——起因是 `kb.pipeline._entity_from_row` 没有还原这个字段，
    而它的 docstring 写着「供只改个别字段后重新渲染卡片使用」。
    「只改个别字段」把增量判据清空了，而且**没有任何东西会报**，
    所以这一轮同时给 `selfcheck.py` 加了一条检查。
    """

    def test_从行还原时保留哈希(self) -> None:
        import sqlite3

        from core.db import SCHEMA
        from kb.pipeline import _entity_from_row

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO entity(entity_id,entity_type,name_zh,name_en,slug,attributes,"
            "source,source_priority,url,content_hash,fetched_at,updated_at) "
            "VALUES ('unique_item:X','unique_item','某装备','X','X','{}','POEDB',1,"
            "'https://poedb.tw/cn/X','abc123',datetime('now'),datetime('now'))"
        )
        row = conn.execute("SELECT * FROM entity WHERE entity_id='unique_item:X'").fetchone()
        self.assertEqual("abc123", _entity_from_row(row).content_hash)
        conn.close()

    def test_合并不动哈希(self) -> None:
        """合并只是把详情页那份词缀装进来，源快照没变——保留原值，
        这样管线下次比对时不会误判成「上游改了」。"""
        from scripts.import_unique_detail import merged_entity

        entity = Entity(
            entity_id=make_entity_id(EntityType.UNIQUE_ITEM, "X"),
            entity_type=EntityType.UNIQUE_ITEM, name_zh="某装备", slug="X",
            mods=[ModLine(kind=ModKind.EXPLICIT, text="A")],
            content_hash="abc123",
            ref=SourceRef(source=Source.POEDB.value, url="https://poedb.tw/cn/X", priority=1),
        )
        merged = merged_entity(entity, [ModLine(kind=ModKind.EXPLICIT, text="A"),
                                        ModLine(kind=ModKind.EXPLICIT, text="不可改变")])
        self.assertEqual("abc123", merged.content_hash)


class DetailPageParseTest(unittest.TestCase):
    """详情页解析本身早就存在（`parse_detail_page`），这里只钉住合并要用的两点。"""

    HTML = """
    <html><body>
    <div class="newItemPopup UniquePopup">
      <div class="itemHeader">
        <div class="itemName"><span class="lc">贸易禁运</span></div>
        <div class="itemName typeLine"><span class="lc">小型雕像</span></div>
      </div>
      <div class="content">
        <div class="property">小型雕像</div>
        <div class="requirements">需求 等级 68</div>
        <div class="implicitMod">你地图中找到的地图数量提高 2%</div>
        <div class="separator"></div>
        <div class="explicitMod">你的地图不会包含先祖秘藏遭遇战</div>
        <div class="explicitMod">不可改变</div>
        <div>Trade Embargo</div>
      </div>
    </div></body></html>
    """

    def test_按DOM顺序取词缀且保留隐式归属(self) -> None:
        page = parse_detail_page(self.HTML)
        self.assertEqual(
            [("implicit", "你地图中找到的地图数量提高 2%"),
             ("explicit", "你的地图不会包含先祖秘藏遭遇战"),
             ("explicit", "不可改变")],
            [(m.kind.value, m.text) for m in page.mods],
        )

    def test_权威英文名也取到(self) -> None:
        """列表页拿不到带撇号的英文名（`Abberath's Horn`），只有详情页有。"""
        self.assertEqual("Trade Embargo", parse_detail_page(self.HTML).name_en)

    def test_没有弹窗的页面抛结构错误(self) -> None:
        """**这条是给导入脚本立的契约**：1342 张页面里一定有不长这样的，
        导入必须捕获它并计数，而不是整批崩在中间（实测第一版就没捕获）。"""
        from kb.sources.poedb_unique import StructureError

        with self.assertRaises(StructureError):
            parse_detail_page("<html><body><p>没有弹窗</p></body></html>")

    def test_导入脚本会捕获结构错误(self) -> None:
        from scripts.import_unique_detail import parse_or_none

        self.assertIsNone(parse_or_none("<html><body><p>没有弹窗</p></body></html>"))
        self.assertIsNotNone(parse_or_none(self.HTML))


if __name__ == "__main__":
    unittest.main()
