"""某底材类别上「一共有哪些词缀」（2026-09-04 第六十三轮）。

来历：地图词缀入库后，「地图词缀有哪些」落在两个工具中间——
`kb_search` 是相关度检索，`top_k` 之外按设计丢掉（实测每次命中 20 条只把 8 条
给模型），所以它**答不了「一共有多少」**；而 `mod_tiers` 要求同时给
`item_class` 与 `mod_query`，答的是**某一条**词缀的梯度。

实测那一轮的后果：模型自己写了「本库共 361 条，本次证据只覆盖 4 条」——
**一句完全正确的话，因为不在任何证据里而被出口闸门拦掉**，
用户看到 4 条词缀却不知道还有 357 条。这个工具把那个总数变成可引用的证据。

这个文件不连网也不连真库：内存 SQLite + 真实 SCHEMA。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from tools.impl import mods as mods_tool  # noqa: E402


def _insert(conn: sqlite3.Connection, *, uid: str, page: str, cls_zh: str,
            group: str, category: str, category_label: str,
            gen_type: int, text: str) -> None:
    conn.execute(
        """
        INSERT INTO mod_affix(
            uid,mod_code,item_class_page,item_class_code,item_class_zh,category,
            category_label,name_zh,gen_type,gen_label,family_list,correct_group,
            ilvl,tier,tier_count,tier_source,weight,weight_pct,text_zh,
            text_template,loose_template,ranges,tags,spawn_tags,fossil_tags,
            variant_id,source,source_priority,url,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        (
            uid, uid, page, cls_zh, cls_zh, category, category_label, group,
            gen_type, {1: "前缀", 2: "后缀"}.get(gen_type, "其他"),
            json.dumps([group], ensure_ascii=False), group, 1, 1, 1,
            "poedb_payload", 100, 1.0, text, text, text, "[]", "[]", "[]", "[]",
            "", "POEDB", 1, f"https://poedb.tw/cn/{page}#ModifiersCalc",
        ),
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 两页同属一个中文类别（POEDB 把地图按 tier 拆页），一条属别的类别
    for i in range(3):
        _insert(conn, uid=f"low{i}", page="Maps_low_tier", cls_zh="异界地图",
                group="MapMonsterPacks", category="normal", category_label="Base",
                gen_type=1, text=f"怪物群规模扩大 {i}%")
    _insert(conn, uid="top0", page="Maps_top_tier", cls_zh="异界地图",
            group="MapGroundEffect", category="normal", category_label="Base",
            gen_type=2, text="区域内有地面效果")
    _insert(conn, uid="cor0", page="Maps_top_tier", cls_zh="异界地图",
            group="MapCorruptVessel", category="corrupted", category_label="已腐化",
            gen_type=0, text="额外有一个瓦尔容器")
    _insert(conn, uid="belt0", page="Belts", cls_zh="腰带", group="IncreasedLife",
            category="normal", category_label="Base", gen_type=1, text="增加最大生命")
    conn.commit()
    return conn


class TestSummary(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        patcher = patch.object(mods_tool.dbmod, "get_conn", return_value=self.conn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, *a, **kw):  # noqa: ANN002, ANN003, ANN201
        return mods_tool.mod_class_summary(*a, **kw)

    def test_多页类别的总数是合起来的(self) -> None:
        """POEDB 把地图按 tier 拆成多页，而玩家只会说「异界地图」。
        只查一页会少算——这正是 `_resolve_pages` 返回列表的理由。"""
        d = self.call("异界地图").data
        self.assertTrue(d["found"])
        self.assertEqual(5, d["mods_total"])
        self.assertEqual(2, len(d["pages_searched"]))

    def test_三个数字各自叫对名字(self) -> None:
        """词缀条数 / 词缀组数 / 梯度条目数是三个不同的量。
        实测真库异界地图是 361 / 99 / 101——混成一个「词缀组数」
        就是一个说不清出处的数字，而它恰好长得很像对的。"""
        d = self.call("异界地图").data
        self.assertEqual(5, d["mods_total"])        # 5 行
        self.assertEqual(3, d["distinct_groups"])   # 3 个 correct_group
        # 梯度 = 组 × 来源 × 生成位：MapMonsterPacks 只出现在一页的同一组合里
        self.assertEqual(3, d["ladders_total"])

    def test_按来源与生成位分别统计(self) -> None:
        d = self.call("异界地图").data
        by_src = {r["label"]: r["rows"] for r in d["by_source"]}
        self.assertEqual({"Base": 4, "已腐化": 1}, by_src)
        by_gen = {r["label"]: r["rows"] for r in d["by_gen"]}
        self.assertEqual({"前缀": 3, "后缀": 1, "其他": 1}, by_gen)

    def test_证据里带总数且可引用(self) -> None:
        """总数必须**在证据正文里**——不然模型说出来就是无绑定断言，
        然后被自己的闸门拦掉，用户就丢了这个信息。"""
        text = self.call("异界地图").citations[0]["text"]
        self.assertIn("5 条", text)
        self.assertIn("异界地图", text)
        self.assertIn("已腐化", text)

    def test_来源限定会缩小统计范围(self) -> None:
        d = self.call("异界地图", "已腐化").data
        self.assertEqual(1, d["mods_total"])

    def test_截断要写出还剩多少(self) -> None:
        """静默截断等同于漏答。键名照 `_answer_notes` 认的那三个来
        （`total` / `returned` / `truncated`），改名就等于附注不再触发。"""
        with patch.object(mods_tool, "MAX_SUMMARY_GROUPS", 2):
            res = self.call("异界地图")
        self.assertTrue(res.data["truncated"])
        self.assertEqual(3, res.data["total"])
        self.assertEqual(2, res.data["returned"])
        self.assertIn("还有 1 个未列出", res.citations[0]["text"])

    def test_没截断时不写截断话术(self) -> None:
        res = self.call("异界地图")
        self.assertFalse(res.data["truncated"])
        self.assertNotIn("未列出", res.citations[0]["text"])

    def test_未知类别不硬答(self) -> None:
        d = self.call("不存在的底材").data
        self.assertFalse(d["found"])
        self.assertIn("known_classes", d)

    def test_类别名取库里的中文名(self) -> None:
        """用户可能给的是页面 slug；照抄进证据标题会让引用读起来像另一件东西。"""
        title = self.call("Maps_low_tier").citations[0]["title"]
        self.assertIn("异界地图", title)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
