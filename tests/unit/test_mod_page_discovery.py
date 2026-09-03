"""词缀枢纽页的两类页发现（2026-09-02 第六十轮）。

这一轮修的是一个**静默漏页**：`discover_mod_class_pages` 的正则只认
`href="/cn/XXX#ModifiersCalc"`，而枢纽页把地图 / 保险箱 / 六分仪 /
瓦尔容器等页按**不带片段**的写法列在同一个 `<li>` 清单里，于是它们
从没被抓过、词缀一条都没入库——而「地图词缀有哪些」在评测里被记成
**检索缺口**，读起来像是上游没有这份数据。

旧代码唯一的守卫是「一个都没发现就报错」，它拦得住整页改版，
**拦不住少认了 46 个**。所以这里成对钉住两件事：
声明页照旧全部认出（不放宽），残差页不再被丢掉（不漏认）。

全部离线：HTML 在内存里构造。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kb.sources import poedb_index  # noqa: E402


def li(href: str, label: str) -> str:
    return f'<li><a  href="{href}" class="">{label}</a></li>'


def hub(*items: str) -> str:
    return f"<html><body><ul>{''.join(items)}</ul></body></html>"


#: 实测形态：地图页与已入库的「贤主之邀」是并排的兄弟节点，只差一个片段。
REAL_SHAPE = hub(
    li("/cn/Mavens_Invitation%3A_The_Feared#ModifiersCalc", "贤主之邀：恐惧"),
    li("/cn/Maps_low_tier", "异界地图(Low)"),
    li("/cn/Maps_uber_tier", "异界地图(Uber)"),
    li("/cn/Sextant", "六分仪"),
    li("/cn/Bows#ModifiersCalc", "弓"),
    li("/cn/Currency", "通货"),          # INDEX_PAGES 已认领
    li("/cn/Body_Armours", "胸甲"),      # PARENT_CLASS_PAGES 已认领
    li("/cn/patreon", "赞助"),           # 站点功能链接
)


class TestDeclaredPages(unittest.TestCase):
    def test_只认带片段的链接(self) -> None:
        self.assertEqual(
            poedb_index._declared_mod_pages(REAL_SHAPE),
            ["Bows", "Mavens_Invitation%3A_The_Feared"],
        )

    def test_一个都没有时拒绝继续(self) -> None:
        """全量守卫必须留着：整页改版时不能把 101 页静默清空。"""
        with self.assertRaises(RuntimeError):
            poedb_index._declared_mod_pages(hub(li("/cn/Maps_low_tier", "异界地图")))


class TestCandidatePages(unittest.TestCase):
    def test_不带片段的兄弟节点不再被丢掉(self) -> None:
        """本轮的核心回归：这三页先前被正则静默吃掉。"""
        slugs = [c.slug for c in poedb_index._candidate_mod_pages(REAL_SHAPE)]
        self.assertEqual(slugs, ["Maps_low_tier", "Maps_uber_tier", "Sextant"])

    def test_保留中文标签(self) -> None:
        """标签用于登记与日志——slug 是英文的，只有它能说明这页是什么。"""
        labels = {c.slug: c.label_zh for c in poedb_index._candidate_mod_pages(REAL_SHAPE)}
        self.assertEqual(labels["Maps_uber_tier"], "异界地图(Uber)")

    def test_已被其它域认领的索引页不进残差(self) -> None:
        """否则每次跑都会把通货、胸甲这些当成「没人认领的新页」报一遍。"""
        slugs = {c.slug for c in poedb_index._candidate_mod_pages(REAL_SHAPE)}
        self.assertNotIn("Currency", slugs)      # INDEX_PAGES
        self.assertNotIn("Body_Armours", slugs)  # PARENT_CLASS_PAGES
        self.assertNotIn("patreon", slugs)       # 站点功能

    def test_两类页不重叠(self) -> None:
        declared = set(poedb_index._declared_mod_pages(REAL_SHAPE))
        candidates = {c.slug for c in poedb_index._candidate_mod_pages(REAL_SHAPE)}
        self.assertFalse(declared & candidates)

    def test_同一页出现多次只算一个(self) -> None:
        doubled = hub(
            li("/cn/Bows#ModifiersCalc", "弓"),
            li("/cn/Sextant", "六分仪"),
            li("/cn/Sextant", "六分仪"),
        )
        self.assertEqual([c.slug for c in poedb_index._candidate_mod_pages(doubled)], ["Sextant"])

    def test_带查询串或锚点的链接不算候选(self) -> None:
        """`/cn/marked?id=` 这类站内功能链接遍布页面，混进来会每轮误报。"""
        noisy = hub(
            li("/cn/Bows#ModifiersCalc", "弓"),
            li("/cn/marked?id=Bows", "收藏"),
            li("/cn/Bows#OtherAnchor", "别的锚点"),
        )
        self.assertEqual(poedb_index._candidate_mod_pages(noisy), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
