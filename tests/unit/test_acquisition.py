"""获取途径（POEDB `Acquisition` 区块，第五十四轮）。全部离线。

这一维的关键不在解析（三列很简单），在**两条边界**：
命运卡与升级配方是两种东西；覆盖是部分的，「查不到」不等于「没有」。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kb.sources.poedb_acquisition import (  # noqa: E402
    AcquisitionError,
    parse_acquisition,
)


def table(rows: list[tuple[str, str, str]], *, link: str = "") -> str:
    body = ""
    for src, ing, offer in rows:
        first = f'<a href="/cn/{link}">{src}</a>' if link else src
        body += f"<tr><td>{first}</td><td>{ing}</td><td>{offer}</td></tr>"
    return ("<table><thead><tr><th>来源</th><th>Ingredient</th><th>Offer</th></tr>"
            f"</thead><tbody>{body}</tbody></table>")


class ParseTest(unittest.TestCase):
    def test_命运卡与升级配方是两种(self) -> None:
        """`ingredient` 为空是命运卡，非空是合成/升级。混成一类会让
        「托沃卧怎么来」得到错答案。"""
        got = parse_acquisition(table([
            ("浑沌性情", "", "混沌石"),
            ("托沃崩", "托沃的祝福", "托沃卧"),
        ]))
        self.assertEqual(got[0].kind, "divination_card")
        self.assertEqual(got[1].kind, "recipe")
        self.assertEqual(got[1].ingredient, "托沃的祝福")

    def test_来源_slug_取自链接(self) -> None:
        got = parse_acquisition(table([("浑沌性情", "", "混沌石")], link="Chaotic_Disposition"))
        self.assertEqual(got[0].source_slug, "Chaotic_Disposition")

    def test_没有链接时_slug_留空而不是猜(self) -> None:
        self.assertEqual(parse_acquisition(table([("某卡", "", "某物")]))[0].source_slug, "")

    def test_没有这一段返回空而不是报错(self) -> None:
        """八成的页面本来就没有 Acquisition，那是数据缺口不是结构错误。"""
        self.assertEqual(parse_acquisition("<div>这页没有</div>"), ())

    def test_缺来源或缺产出就跳过(self) -> None:
        got = parse_acquisition(table([("", "", "混沌石"), ("某卡", "", "")]))
        self.assertEqual(got, ())

    def test_列数不对就报错(self) -> None:
        html = ("<table><thead><tr><th>来源</th><th>Ingredient</th><th>Offer</th></tr>"
                "</thead><tbody><tr><td>a</td><td>b</td></tr></tbody></table>")
        with self.assertRaises(AcquisitionError):
            parse_acquisition(html)


class RouteTest(unittest.TestCase):
    def route(self, q: str) -> str:
        from orchestrator.loop import _preferred_tool_route

        return _preferred_tool_route(q)

    def test_命运卡与合成问法路由过来(self) -> None:
        for q in ("哪张命运卡能出头猎", "托沃卧怎么合成", "什么卡能换崇高石"):
            with self.subTest(q=q):
                self.assertEqual("acquisition_routes", self.route(q))

    def test_裸的怎么获得不能被抢过来(self) -> None:
        """「怎么获得」是 `stat_sources` 的意图词（第五十一轮实测「混沌石怎么获得」
        被它抢走）；而通货的主要来源是掉落、那一维本库没有，
        放开会拿次要途径顶替主要途径。"""
        self.assertNotEqual("acquisition_routes", self.route("混沌石怎么获得"))
        self.assertNotEqual("acquisition_routes", self.route("混沌石怎么换"))


if __name__ == "__main__":
    unittest.main()


class CardTest(unittest.TestCase):
    """卡片必须真的写出来——「数据入库但没有出口」是这个项目反复踩的坑。

    第五十四轮入库了 387 行，却只有工具出口；实测「猎首怎么获得」这个最自然的问法
    路由到了 `stat_sources`（「某属性能从哪来」的工具），而卡上一个字都没有。
    """

    def _card(self, routes: list[dict]) -> str:
        from kb.render import acquisition_block

        return "\n".join(acquisition_block({"acquisition": routes}))

    def test_命运卡与升级配方分开写(self) -> None:
        card = self._card([
            {"source_zh": "疯医", "ingredient": ""},
            {"source_zh": "托沃崩", "ingredient": "托沃的祝福"},
        ])
        self.assertIn("命运卡：疯医", card)
        self.assertIn("升级配方：托沃崩 + 托沃的祝福", card)

    def test_没有这一段就不出区块(self) -> None:
        """1078 件唯一装备的详情页本来就没有 Acquisition 区块。
        写「未采集」会把「页面没给」说成「我们没采」，写「无法获得」更是假话——
        所以什么都不写，覆盖的诚实说明由工具在被问到时给。"""
        self.assertEqual(self._card([]), "")

    def test_超过上限要写出还剩多少(self) -> None:
        from kb.render import MAX_CARD_ACQUISITION

        many = [{"source_zh": f"卡{i}", "ingredient": ""}
                for i in range(MAX_CARD_ACQUISITION + 3)]
        self.assertIn("另有 3 条", self._card(many))
