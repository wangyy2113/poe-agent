"""属性来源枚举的未覆盖声明（2026-08-19）。

## 修的是什么

`scan_all` 原先用「领域 usable」当「它的属性能被本枚举列出来」。两者不等价，
实测反例：`base_item` 已入库 1,022 条、`provides_stats=True`、**却没有注册枚举渠道**
（`_CHANNELS` 里只有装备词缀 / 唯一装备 / 天赋树 / 升华 / Atlas 天赋），
于是底材的隐式词缀既不出现在清单里、也不再被声明为缺口——一份看起来完整的枚举。
技能石与辅助宝石入库后会再多两个这样的领域，所以判据必须改成
「usable **且**有注册渠道」，并把两种成因分开措辞。

措辞不能混：说底材「未采集」是假话（库里有 1,022 条），
说它「已采集」又会让人以为枚举里有它。
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import kb.coverage as cov  # noqa: E402
import kb.stat_channels as sc  # noqa: E402
from core.db import SCHEMA  # noqa: E402

_BY_KEY = {d.key: d for d in cov.DOMAINS}


def _status(key: str, status: str, count: int) -> cov.DomainStatus:
    return cov.DomainStatus(spec=_BY_KEY[key], status=status, item_count=count)


class UncoveredCauseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._real_load = cov.load
        # 只有「装备词缀」这一个领域既已入库又有注册渠道；其余按成因分成两类
        self.rows = [
            _status("mod_affix", "ingested", 93897),   # 有渠道 + 有数据 → 不该声明
            _status("base_item", "partial", 1022),     # 有数据、无渠道 → not_enumerable
            _status("skill_gem", "partial", 548),      # 同上
            _status("support_gem", "partial", 262),    # 同上
            _status("cluster_jewel", "not_collected", 0),  # 无数据 → not_collected
            # 有数据 + 有注册渠道 → 与 mod_affix 同类，不该声明。
            # 第三十五轮入库万神殿时差点漏掉注册渠道：那样 cause 只会从
            # not_collected 变成 not_enumerable，声明换个措辞、枚举照旧列不出神力。
            _status("pantheon", "partial", 12),
        ]
        cov.load = lambda conn: self.rows  # type: ignore[assignment]

    def tearDown(self) -> None:
        cov.load = self._real_load  # type: ignore[assignment]
        self.conn.close()

    def _scan(self) -> sc.ScanResult:
        return sc.scan_all(self.conn, "最大生命")

    def test_有数据但无枚举渠道也要被声明(self) -> None:
        """这是原先漏掉的那一类：枚举里没有它，却也不说缺它。"""
        by_key = {c.display_zh: c.cause for c in self._scan().uncovered}
        self.assertEqual("not_enumerable", by_key["底材（基础物品）"])
        self.assertEqual("not_enumerable", by_key["技能石"])
        self.assertEqual("not_enumerable", by_key["辅助宝石"])

    def test_未采集与未接入枚举分开措辞(self) -> None:
        note = self._scan().coverage_note()
        self.assertIn("**本知识库尚未采集**", note)
        self.assertIn("**已采集但其属性尚未接入枚举**".replace("接入枚举", "接入本枚举"), note)
        # 底材有 1,022 条，绝不能出现在「尚未采集」那一段里
        missing_part = note.split("已采集但其属性尚未接入本枚举")[0]
        self.assertNotIn("底材", missing_part)
        self.assertIn("星团珠宝", missing_part)

    def test_已接入渠道的领域不被声明(self) -> None:
        declared = [c.display_zh for c in self._scan().uncovered]
        self.assertNotIn("装备词缀", declared)
        # 万神殿同样是「有数据 + 有渠道」。漏注册渠道时它会以 not_enumerable
        # 重新出现在这里——这条断言就是那个漏洞的探针。
        self.assertNotIn("万神殿", declared)

    def test_判据是派生的不是手写开关(self) -> None:
        """注册了渠道之后，同一个领域当场从声明里消失——不需要改任何清单。"""
        before = {c.display_zh for c in self._scan().uncovered}
        self.assertIn("技能石", before)
        spec = sc.ChannelSpec(
            key="skill_gem_stats", label_zh="技能石属性", domain_key="skill_gem",
            scan=lambda conn, q: None,
        )
        sc._CHANNELS.append(spec)
        try:
            self.assertNotIn("技能石", {c.display_zh for c in self._scan().uncovered})
        finally:
            sc._CHANNELS.remove(spec)

    def test_全部接入时给肯定声明(self) -> None:
        """声明遍历的是**静态 DOMAINS 清单**，不是探测结果——所以「全部接入」
        必须连清单一起收窄才成立。这也说明一件事：`kb_coverage` 里查不到某个
        声明过的属性来源时，它按「未采集」计，不会被静默忽略。
        """
        real_domains = cov.DOMAINS
        cov.DOMAINS = (_BY_KEY["mod_affix"],)  # type: ignore[assignment]
        self.rows = [_status("mod_affix", "ingested", 93897)]
        try:
            self.assertIn("均已接入本枚举", self._scan().coverage_note())
        finally:
            cov.DOMAINS = real_domains  # type: ignore[assignment]

    def test_声明过的来源在探测结果里缺失时按未采集计(self) -> None:
        """漏进 kb_coverage 的领域不能变成「枚举完整」——那是静默漏洞的另一面。"""
        self.rows = [_status("mod_affix", "ingested", 93897)]
        causes = {c.display_zh: c.cause for c in self._scan().uncovered}
        self.assertEqual("not_collected", causes["技能石"])


if __name__ == "__main__":
    unittest.main()
