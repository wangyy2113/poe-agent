"""POE1 / POE2 边界（2026-08-18）。

**本项目只覆盖《流放之路 1》。** 这不是一句声明而是必须由代码执行的边界：两款游戏
大量概念同名（升华、词缀、通货、地图、暴击），拿 POE1 的数值回答 POE2 的问题，
用户看不出来，而「一个错误的小前提会让整条结论反向」正是第一原则要防的。

实测的漏出：「poe2 词缀怎么洗」被判**依据充分**（覆盖率 100%）。原因是分词把数字标记
吃掉了 —— `poe2` → `['poe', '词缀']`，问题退化成一个纯 POE1 问题。所以判据必须在
**原始问题串**上匹配专指标记，靠覆盖率是拦不住的。

两条对称的用例都在：该拦的拦住（各种写法的 POE2 问题），该放的放行（POE1 里名字含
「降临」的天赋节点）。
"""

from __future__ import annotations

import unittest

from kb.fetcher import Poe2SourceRejected, reject_poe2_host
from orchestrator.loop import _out_of_scope_answer, poe2_marker


class Poe2MarkerTest(unittest.TestCase):
    def test_chinese_forms_detected(self) -> None:
        for q in (
            "流放之路2 的升华机制是什么",
            "流放之路 2 的通货",
            "流放之路二的地图机制",
            "流放之路：降临的暴击怎么算",
            "流放之路:降临有哪些职业",
            "第二部的暴击机制怎么算",
            "二代的词缀池",
        ):
            with self.subTest(q=q):
                self.assertTrue(poe2_marker(q), f"未识别 POE2 问题：{q}")

    def test_latin_forms_detected_case_insensitively(self) -> None:
        """实测的漏出就在这里：分词会把 `poe2` 的数字吃掉，覆盖率因此变成 100%。"""
        for q in ("poe2 词缀怎么洗", "PoE2 的通货有哪些", "POE 2 ascendancy",
                  "Path of Exile 2 的天赋树", "pathofexile2 有什么新机制"):
            with self.subTest(q=q):
                self.assertTrue(poe2_marker(q), f"未识别 POE2 问题：{q}")

    def test_poe1_questions_untouched(self) -> None:
        for q in (
            "猎首（Headhunter）有哪些词缀？",
            "腰带上的力量词缀一共有几档",
            "流放之路的升华机制是什么",          # 没有「2」，就是 POE1
            "抗性上限是多少",
        ):
            with self.subTest(q=q):
                self.assertEqual("", poe2_marker(q))

    def test_bare_descent_word_is_not_a_marker(self) -> None:
        """**单独的「降临」不能当标记。**

        POE1 有天赋节点「先祖降临」（Ancestral Reach）与「惊寒降临」（Chilling Presence），
        拿它当标记会把这两个正常问题拒掉 —— 与 boss 领域曾把「怪物」当意图词、
        路由曾把「知识库」当意图词是同一种错：意图词必须专指。
        """
        for q in ("先祖降临这个天赋给什么", "惊寒降临的效果是什么", "降临的效果"):
            with self.subTest(q=q):
                self.assertEqual("", poe2_marker(q))

    def test_answer_states_the_boundary_and_points_somewhere(self) -> None:
        """越界声明必须说清边界并指路——不指路会让用户以为「游戏里没这东西」。"""
        text = _out_of_scope_answer("poe2 词缀怎么洗", "poe2")
        self.assertIn("流放之路 1", text)
        self.assertIn("poe2", text)
        self.assertIn("poe2db.tw", text, "必须给出 POE2 的权威来源")
        self.assertNotIn("一般来说", text, "越界声明里不得出现模糊限定语")


class Poe2HostGuardTest(unittest.TestCase):
    """抓取入口的硬护栏。`poedb.tw`（POE1）与 `poe2db.tw`（POE2）只差一个字符。"""

    def test_poe2_hosts_rejected(self) -> None:
        for url in ("https://poe2db.tw/cn/Bows", "http://www.poe2db.tw/us/x",
                    "https://poe2.ninja/economy", "https://poe2wiki.net/wiki/X"):
            with self.subTest(url=url):
                with self.assertRaises(Poe2SourceRejected):
                    reject_poe2_host(url)

    def test_poe1_hosts_allowed(self) -> None:
        for url in (
            "https://poedb.tw/cn/Bows",
            "https://poedb.tw/us/Body_Armours",
            "https://github.com/PathOfBuildingCommunity/PathOfBuilding/blob/abc/x.lua",
            "https://www.pathofexile.com/api/leagues",
            "https://poe.ninja/challenge/builds",
        ):
            with self.subTest(url=url):
                reject_poe2_host(url)  # 不抛即通过

    def test_redirect_to_poe2_is_rejected_before_caching(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import httpx
        from kb.fetcher import Fetcher

        with tempfile.TemporaryDirectory() as directory:
            fetcher = Fetcher(Path(directory), "poe-agent-test", respect_robots=False)
            response = httpx.Response(200, text="POE2 data", request=httpx.Request("GET", "https://poe2db.tw/cn/Bows"))
            with patch("kb.fetcher.httpx.get", return_value=response):
                with self.assertRaises(Poe2SourceRejected):
                    fetcher.fetch("https://poedb.tw/cn/Bows")
            self.assertEqual([], list(Path(directory).iterdir()))


if __name__ == "__main__":
    unittest.main()
