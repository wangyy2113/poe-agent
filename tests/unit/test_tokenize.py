"""分词的停用判定：疑问构式与实义词的分界。

这个判据坏过两次，方向**相反**，所以两边都要钉住：

* 漏掉疑问词 → 它在任何知识卡里都不存在，于是永久压低覆盖率、并在
  `missing_terms` 里冒充「知识库缺这个」，把**有据的问题拒答掉**
  （`几级`：爪的攻击速度后缀；`几位`/`哪张`：万神殿）。
* 过度过滤 → 覆盖率被抬高、充分依据闸门被放松，**无据推断重新漏出**
  （需求 1.3 明确要求两个方向都防）。

所以新增量词时必须同时验证反例：`几率` 是词缀原文里的高频实义词，
`哪` 系构式不得吃掉任何真实词条。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from retrieval.tokenize import is_stopword  # noqa: E402


class InterrogativeConstructionTest(unittest.TestCase):
    def test_疑问数量构式被停用(self) -> None:
        for token in ("几", "几个", "几级", "几条", "几档", "几点", "几种", "几层"):
            self.assertTrue(is_stopword(token), token)

    def test_多少构式被停用(self) -> None:
        for token in ("多少", "多少条", "多少个", "多少级"):
            self.assertTrue(is_stopword(token), token)

    def test_哪系构式被停用(self) -> None:
        """2026-08-21 第三十五轮补：`哪` 光杆在词表里，带量词的组合此前会漏。

        「格鲁丝克之魂要抓获哪个头目，在**哪张**地图」曾因 `哪张` 全库为零被拒答。
        """
        for token in ("哪", "哪个", "哪些", "哪里", "哪张", "哪条", "哪位", "哪类"):
            self.assertTrue(is_stopword(token), token)

    def test_称人量词被停用(self) -> None:
        """「万神殿的主要神力有**哪几位**神祇」曾因 `几位` 全库为零被拒答。"""
        for token in ("几位", "几名", "几类", "几项", "几处"):
            self.assertTrue(is_stopword(token), token)

    def test_实义词不得被停用(self) -> None:
        """反例。放宽停用等于放松充分依据闸门，会让无据推断重新漏出。"""
        for token in (
            "几率",      # 词缀原文里的高频词：「有 50% 几率…」
            "位移",      # 「位」开头但不是量词构式
            "层数",      # 中毒层数、创伤层数
            "点燃",
            "名额",      # 前后缀名额
            "多重",      # 多重投射
            "项链",
            "处决",
        ):
            self.assertFalse(is_stopword(token), token)

    def test_领域实义词不得被停用(self) -> None:
        for token in ("神祇", "神力", "万神殿", "混沌抗性", "反射伤害"):
            self.assertFalse(is_stopword(token), token)


if __name__ == "__main__":
    unittest.main()
