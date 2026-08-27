"""`DomainSpec.pob_files` 的两个消费者（2026-08-27 第三十九轮）。

## 这个字段坑了两次

`pob_files` 从一开始就是**纯文档字段、零运行时消费者**，于是连着两轮被同一种方式坑：

* 第三十五轮 · 万神殿：spec 上写着「缺的是**来源**」，而它自己的
  `pob_files=("Data/Pantheons.lua",)` 指着一个一直在本机检出里的文件；
* 第三十八轮 · Boss：同一句结论，而它的 `pob_files` 是**空元组**，
  `Data/BossSkills.lua` / `Data/Bosses.lua` 一直都在。

**一个没有消费者的字段，写了等于没写，写错更没人发现。** 所以第三十九轮给它两个消费者：

1. `coverage.probe()` 用它分辨 `harvested` 与 `not_collected`——
   PoB 侧有文件而库里没数据，该报「已抓取未入库」（缺的是抽取器），不是「未采集」；
2. 这个文件：声明的路径必须真的存在，**声明错了要红**。

## 顺带记下一件事：信号当时其实已经有了

`scripts/probe_pob.py` 的 `SPECS` 表里**早就写着**
`Spec("万神殿", "Data/Pantheons.lua", ...)` 与 `Spec("Boss 技能", "Data/BossSkills.lua", ...)`，
连对应的知识域路径都标了。两次都不是「没人记录」，是**记录在一个没人例行跑的脚本里**。

那张表与 `pob_files` 是**同一关系的两份手工映射**，而且用的是两套词汇
（实测 34 条 Spec 里只有 14 条能自动对上 coverage 的域）。合并成一份真源是对的，
但那是一次独立的重构——这一轮先把「字段没有消费者」这条堵上，
把重构记进 BASELINE 的待办，**不在同一轮里既改语义又搬家**。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kb.coverage import DOMAINS, pob_data_root  # noqa: E402


class PobFilesDeclarationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pob_data_root()
        if self.root is None:
            self.skipTest("本机没有 PoB 检出")

    def test_声明的路径必须真的存在(self) -> None:
        """声明错了要红。含 `{}` 的是写给人看的模板，跳过。"""
        missing: list[str] = []
        for spec in DOMAINS:
            for raw in spec.pob_files:
                if "{" in raw:
                    continue
                found = any(self.root.glob(raw)) if "*" in raw else (self.root / raw).is_file()
                if not found:
                    missing.append(f"{spec.key} → {raw}")
        self.assertEqual([], missing, "这些 pob_files 声明在本机检出里找不到")

    def test_至少有几个域真的声明了(self) -> None:
        """防止「字段全空所以永远通过」——那正是它坑了两次的形态。"""
        declared = [s.key for s in DOMAINS if s.pob_files]
        self.assertGreaterEqual(len(declared), 8, f"只有 {declared} 声明了 pob_files")
        # 两次踩坑的那两个域必须在里面
        self.assertIn("pantheon", declared)
        self.assertIn("boss", declared)

    def test_一条数据都没有的域必须说得出为什么(self) -> None:
        """没有数据的域，要么两侧确实没东西可拿，要么就该把来源声明填上。

        判据只看**没有数据**的域：已经有数据的域（`fossil` 的词缀池随词缀一起入库、
        `craft_feasibility` 复用 `mod_affix`）本来就不靠自己的采集入口，
        没有来源声明是正常的——把它们也算进来，这条断言就会变成一句永远在响的噪音。
        """
        for spec in DOMAINS:
            if spec.count_sql or spec.pob_files or spec.poedb_pages:
                continue
            self.assertIn(
                spec.key, {"economy_price", "league_mechanic"},
                f"{spec.key} 一条数据都没有，也没声明任何来源——"
                "如果上游其实有，现在就是把它填上的时候",
            )


if __name__ == "__main__":
    unittest.main()
