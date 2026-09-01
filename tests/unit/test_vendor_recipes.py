"""商店配方（POEDB `/cn/Vendor_recipe_system`，第五十一轮）。

全部离线：HTML 在内存里构造。这一维补的是通货域「获取途径未采集」的**一半**——
**怎么换**；掉落来源（怎么刷）仍未采集，两者的边界由工具的 `boundary` 与
覆盖声明一起守住。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kb.sources.poedb_vendor import (  # noqa: E402
    VendorRecipeError,
    parse_vendor_recipes,
)


def block(title: str, rows: list[tuple[str, str, str]], *, declared: int | None = None) -> str:
    n = declared if declared is not None else len(rows)
    body = "".join(
        f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows)
    return (
        f'<div class="card"><h5 class="card-header">{title} /{n} </h5>'
        "<table><thead><tr><th>贡品</th><th>你的物品</th><th>Note</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


class ParseTest(unittest.TestCase):
    def test_三列按页面原文取出(self) -> None:
        got = parse_vendor_recipes(block(
            "直接從商店買 Recipe", [("点金石", "1x 后悔石", "章节 3 卡尔莉萨")]))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].category, "直接從商店買 Recipe")
        self.assertEqual(got[0].result, "点金石")
        self.assertEqual(got[0].ingredients, "1x 后悔石")
        self.assertEqual(got[0].note, "章节 3 卡尔莉萨")

    def test_方向不能弄反(self) -> None:
        """`贡品` 是**换到手的**，`你的物品` 是**付出去的**。
        写反会把「2 个重铸石换 1 个后悔石」说成反过来——一个读起来通顺的错答案。"""
        got = parse_vendor_recipes(block(
            "直接從商店買 Recipe", [("后悔石", "2x 重铸石", "")]))
        self.assertEqual(got[0].result, "后悔石", "result 必须是换到手的那个")
        self.assertEqual(got[0].ingredients, "2x 重铸石", "ingredients 是付出去的")

    def test_行数与标题声明数不符就整页拒绝(self) -> None:
        """静默少收等于漏答，而这一维的用户问题正是「有没有这个配方」。"""
        with self.assertRaises(VendorRecipeError) as ctx:
            parse_vendor_recipes(block("装备 Recipe", [("a", "b", "")], declared=9))
        self.assertIn("声明 9 行", str(ctx.exception))

    def test_类别名不唯一时用_block_index_区分(self) -> None:
        """实测页面上 `賣物品給商人 Recipe` 出现两次（5 行与 2 行两张表）。
        主键只用类别名会丢掉其中一张。"""
        html = (block("賣物品給商人 Recipe", [("增幅石", "1x 6 詞綴稀有物品", "")])
                + block("賣物品給商人 Recipe", [("护甲片", "護甲", "")]))
        got = parse_vendor_recipes(html)
        self.assertEqual(len(got), 2)
        self.assertNotEqual(got[0].block_index, got[1].block_index)
        keys = {(r.category, r.block_index, r.row_index) for r in got}
        self.assertEqual(len(keys), 2, "主键必须唯一")

    def test_区块标题不合形状就报错(self) -> None:
        html = ('<div class="card"><h5 class="card-header">随便什么</h5>'
                "<table><thead><tr><th>贡品</th><th>你的物品</th><th>Note</th></tr>"
                "</thead><tbody><tr><td>a</td><td>b</td><td></td></tr></tbody>"
                "</table></div>")
        with self.assertRaises(VendorRecipeError):
            parse_vendor_recipes(html)

    def test_产出或投入缺一个就跳过(self) -> None:
        """缺一边就说不出「拿什么换什么」，收进来只会是半条配方。"""
        got = parse_vendor_recipes(block(
            "装备 Recipe", [("点金石", "", ""), ("", "1x 后悔石", "")]))
        self.assertEqual(got, ())

    def test_没有配方表就报错(self) -> None:
        with self.assertRaises(VendorRecipeError):
            parse_vendor_recipes("<div>这页没有配方</div>")


class RouteTest(unittest.TestCase):
    """判据是**问句形式**：裸的「换」「配方」会把工艺台/收获抢过来。"""

    def route(self, q: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(q)

    def test_换的问法路由到商店配方(self) -> None:
        for q in ("混沌石怎么换", "6 词缀稀有物品能换什么", "装备怎么换混沌石",
                  "商店配方有哪些"):
            with self.subTest(q=q):
                self.assertEqual("vendor_recipe_lookup", self.route(q))

    def test_不抢别的工具(self) -> None:
        """`工艺台配方` 与 `收获工艺` 都含「配方/工艺」，不能被这条抢走。"""
        for q in ("多重投射辅助 20 级的消耗倍率", "工艺工作台能加什么词缀"):
            with self.subTest(q=q):
                self.assertNotEqual("vendor_recipe_lookup", self.route(q))


if __name__ == "__main__":
    unittest.main()
