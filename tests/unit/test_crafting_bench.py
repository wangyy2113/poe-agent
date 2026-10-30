"""POEDB 工艺工作台目录：抽取、入库、覆盖声明与工具边界测试。

fixture 取自 2026-08 的真实 Crafting_Bench 缓存并只裁剪无关行；数值、费用、
ItemClasses、解锁文本和链接保持原样。测试不补写任何页面未声明的操作语义。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.db import SCHEMA  # noqa: E402
from kb.coverage import probe  # noqa: E402
from kb.pipeline import (  # noqa: E402
    MIN_BENCH_SNAPSHOT_RECIPES,
    _commit_bench_snapshot,
    _replace_bench_recipes,
    _validate_bench_snapshot_size,
    run_poedb_crafting,
)
from kb.sources.poedb_crafting import (  # noqa: E402
    CRAFTING_BENCH_URL,
    CraftingBenchStructureError,
    extract_bench_recipes,
)
from tools.impl.bench import bench_recipe_catalog  # noqa: E402
from tools.registry import REGISTRY  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "poedb_crafting_bench_sample.html"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _populated_conn() -> sqlite3.Connection:
    conn = _conn()
    recipes, _ = extract_bench_recipes(_load_fixture())
    _replace_bench_recipes(conn, recipes, "2026-08-04T00:00:00+00:00")
    conn.commit()
    return conn


class TestCraftingBenchExtractor(unittest.TestCase):
    def test_真实裁剪样本完整抽取(self):
        recipes, stats = extract_bench_recipes(_load_fixture())

        self.assertEqual(len(recipes), 4)
        self.assertEqual(stats.total, 4)
        self.assertEqual(stats.succeeded, 4)
        self.assertEqual(stats.costs, 5)
        self.assertEqual(stats.multi_cost_recipes, 1)
        self.assertEqual(len(stats.currencies), 5)

        life = recipes[0]
        self.assertEqual(life.effect_text, "+(15-25) 最大生命")
        self.assertIn("项链", life.item_classes)
        self.assertEqual(life.costs[0].amount, 1)
        self.assertEqual(life.costs[0].currency_slug, "Orb_of_Alteration")
        self.assertEqual(life.unlock_text, "Default")
        self.assertIsNone(life.unlock_url)

        fire = recipes[1]
        self.assertEqual(fire.unlock_hint, "元素抗性 - 等级 2")
        self.assertEqual(
            fire.unlock_url,
            "https://poedb.tw/cn/The_Belly_of_the_Beast_Level_2",
        )

        flask = recipes[3]
        self.assertEqual(
            [(c.amount, c.currency_name_zh) for c in flask.costs],
            [(5, "灌顶石"), (5, "玻璃弹珠")],
        )
        self.assertEqual(flask.unlock_hint, "移动速度和药剂附魔 - 等级 3")

    def test_完整行哈希稳定且不同适用类别不会误去重(self):
        html = _load_fixture()
        first, _ = extract_bench_recipes(html)
        second, _ = extract_bench_recipes(html)
        self.assertEqual(first[0].recipe_id, second[0].recipe_id)
        self.assertEqual(first[0].content_hash, second[0].content_hash)

        changed = html.replace(
            "护甲 · 手套 · 鞋子 · 头盔 · 盾 · 戒指 · 项链 · 腰带 · 箭袋",
            "双手近战",
            1,
        )
        altered, _ = extract_bench_recipes(changed)
        self.assertEqual(first[0].effect_text, altered[0].effect_text)
        self.assertNotEqual(first[0].recipe_id, altered[0].recipe_id)

    def test_费用变化保留配方身份并更新内容哈希(self):
        html = _load_fixture()
        original, _ = extract_bench_recipes(html)
        changed, _ = extract_bench_recipes(html.replace("1x <a", "2x <a", 1))
        self.assertEqual(original[0].recipe_id, changed[0].recipe_id)
        self.assertNotEqual(original[0].content_hash, changed[0].content_hash)

    def test_关键字段缺失整批拒绝(self):
        cases = (
            _load_fixture().replace("1x <a class=\"item_currency", "<a class=\"item_currency", 1),
            _load_fixture().replace("<td>药剂</td>", "<td></td>", 1),
            _load_fixture().replace("<th>Require</th>", "<th>Cost</th>", 1),
        )
        for html in cases:
            with self.subTest():
                with self.assertRaises(CraftingBenchStructureError):
                    extract_bench_recipes(html)

    def test_完全重复行拒绝静默去重(self):
        html = _load_fixture()
        row_start = html.index("    <tr>\n      <td>+(15")
        row_end = html.index("    </tr>", row_start) + len("    </tr>\n")
        duplicated = html[:row_end] + html[row_start:row_end] + html[row_end:]
        with self.assertRaisesRegex(CraftingBenchStructureError, "完整重复"):
            extract_bench_recipes(duplicated)

    def test_非HTTPS来源拒绝(self):
        with self.assertRaisesRegex(CraftingBenchStructureError, "HTTPS"):
            extract_bench_recipes(_load_fixture(), "http://poedb.tw/cn/Crafting_Bench")


class TestCraftingBenchStorageAndCoverage(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _populated_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_配方与多费用规范化入库(self):
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM craft_bench_recipe").fetchone()[0],
            4,
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM craft_bench_recipe_cost").fetchone()[0],
            5,
        )
        amounts = self.conn.execute(
            "SELECT amount, currency_name_zh FROM craft_bench_recipe_cost "
            "WHERE recipe_id=(SELECT recipe_id FROM craft_bench_recipe "
            "WHERE effect_text='跟接邻的药剂一起自动使用') ORDER BY position"
        ).fetchall()
        self.assertEqual([tuple(row) for row in amounts], [(5, "灌顶石"), (5, "玻璃弹珠")])

    def test_快照替换会删除旧配方与旧费用(self):
        recipes, _ = extract_bench_recipes(_load_fixture())
        _replace_bench_recipes(
            self.conn,
            recipes[:1],
            "2026-08-05T00:00:00+00:00",
        )
        self.conn.commit()
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM craft_bench_recipe").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM craft_bench_recipe_cost").fetchone()[0],
            1,
        )

    def test_结构合法但截短的快照被数量护栏拒绝(self):
        # 绝对下限只约束首次导入；已有可信快照由相对降幅护栏判断。
        fresh = _conn()
        try:
            with self.assertRaisesRegex(CraftingBenchStructureError, "安全下限"):
                _validate_bench_snapshot_size(fresh, 4)
        finally:
            fresh.close()

        # 构造一个超过首次导入下限的历史快照，再验证相对骤降护栏。
        for index in range(4, MIN_BENCH_SNAPSHOT_RECIPES + 20):
            self.conn.execute(
                "INSERT INTO craft_bench_recipe VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"dummy:{index}", f"效果{index}", '["护甲"]', "Default", None,
                    None, "POEDB", 1, CRAFTING_BENCH_URL, f"hash:{index}",
                    "2026-08-04T00:00:00+00:00", "2026-08-04T00:00:00+00:00",
                ),
            )
        self.conn.commit()
        with self.assertRaisesRegex(CraftingBenchStructureError, "骤降"):
            _validate_bench_snapshot_size(self.conn, MIN_BENCH_SNAPSHOT_RECIPES)

    def test_显式缩减覆盖返回完整审计字段(self):
        audit = _validate_bench_snapshot_size(self.conn, 1, allow_shrink=True)
        self.assertTrue(audit["allow_shrink_requested"])
        self.assertTrue(audit["allow_shrink_applied"])
        self.assertEqual(audit["existing_recipe_count"], 4)
        self.assertEqual(audit["incoming_recipe_count"], 1)
        self.assertEqual(audit["guard_reason"], "relative_drop_exceeded")
        self.assertAlmostEqual(audit["drop_ratio"], 0.75)

    def test_相对缩减按真实比例比较不被整数取整放宽(self):
        for index in range(4, 106):
            self.conn.execute(
                "INSERT INTO craft_bench_recipe VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"ratio:{index}", f"效果{index}", '["护甲"]', "Default", None,
                    None, "POEDB", 1, CRAFTING_BENCH_URL, f"ratio-hash:{index}",
                    "2026-08-04T00:00:00+00:00", "2026-08-04T00:00:00+00:00",
                ),
            )
        self.conn.commit()
        with self.assertRaisesRegex(CraftingBenchStructureError, "骤降"):
            _validate_bench_snapshot_size(self.conn, 100)
        audit = _validate_bench_snapshot_size(self.conn, 100, allow_shrink=True)
        self.assertEqual(audit["minimum_allowed_count"], 101)
        self.assertEqual(audit["guard_reason"], "relative_drop_exceeded")
        self.assertAlmostEqual(audit["drop_ratio"], 0.056604)

    def test_已有快照恰好缩减百分之五允许再多一条拒绝(self):
        for index in range(4, 100):
            self.conn.execute(
                "INSERT INTO craft_bench_recipe VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"boundary:{index}", f"效果{index}", '["护甲"]', "Default", None,
                    None, "POEDB", 1, CRAFTING_BENCH_URL, f"boundary-hash:{index}",
                    "2026-08-04T00:00:00+00:00", "2026-08-04T00:00:00+00:00",
                ),
            )
        self.conn.commit()
        audit = _validate_bench_snapshot_size(self.conn, 95)
        self.assertEqual(audit["minimum_allowed_count"], 95)
        self.assertEqual(audit["drop_ratio"], 0.05)
        self.assertIsNone(audit["guard_reason"])
        with self.assertRaisesRegex(CraftingBenchStructureError, "骤降"):
            _validate_bench_snapshot_size(self.conn, 94)

    def test_显式缩减覆盖写入持久ingest审计(self):
        class FakeConfig:
            db_path = Path("unused.db")

            @staticmethod
            def get(_key, default=None):
                return default

        response = SimpleNamespace(
            ok=True,
            text=_load_fixture(),
            status_code=200,
            content_hash="fixture-hash",
            from_cache=True,
            fetched_at="2026-08-10T00:00:00+00:00",
        )
        fetcher = SimpleNamespace(fetch=lambda _url, force=False: response)
        self.conn.execute("DELETE FROM craft_bench_recipe")
        self.conn.commit()
        with (
            patch("kb.pipeline.get_config", return_value=FakeConfig()),
            patch("kb.pipeline.dbmod.init_db", return_value=self.conn),
            patch("kb.pipeline._make_fetcher", return_value=fetcher),
        ):
            self.assertEqual(run_poedb_crafting(allow_shrink=True), 0)

        row = self.conn.execute(
            "SELECT note FROM ingest_run WHERE category='crafting_bench' "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        note = json.loads(row["note"])
        self.assertTrue(note["allow_shrink_requested"])
        self.assertTrue(note["allow_shrink_applied"])
        self.assertEqual(note["existing_recipe_count"], 0)
        self.assertEqual(note["incoming_recipe_count"], 4)
        self.assertEqual(note["guard_reason"], "below_absolute_minimum")
        self.assertEqual(note["boundary"], "catalog_only_no_execution_semantics")

    def test_默认护栏拒绝也写入同构决策审计(self):
        class FakeConfig:
            db_path = Path("unused.db")

            @staticmethod
            def get(_key, default=None):
                return default

        response = SimpleNamespace(
            ok=True,
            text=_load_fixture(),
            status_code=200,
            content_hash="fixture-hash",
            from_cache=True,
            fetched_at="2026-08-10T00:00:00+00:00",
        )
        fetcher = SimpleNamespace(fetch=lambda _url, force=False: response)
        self.conn.execute("DELETE FROM craft_bench_recipe")
        self.conn.commit()
        with (
            patch("kb.pipeline.get_config", return_value=FakeConfig()),
            patch("kb.pipeline.dbmod.init_db", return_value=self.conn),
            patch("kb.pipeline._make_fetcher", return_value=fetcher),
        ):
            with self.assertRaises(CraftingBenchStructureError):
                run_poedb_crafting(allow_shrink=False)

        row = self.conn.execute(
            "SELECT note FROM ingest_run WHERE category='crafting_bench' "
            "ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        note = json.loads(row["note"])
        self.assertFalse(note["allow_shrink_requested"])
        self.assertFalse(note["allow_shrink_applied"])
        self.assertEqual(note["existing_recipe_count"], 0)
        self.assertEqual(note["incoming_recipe_count"], 4)
        self.assertEqual(note["drop_ratio"], 0.0)
        self.assertEqual(note["guard_reason"], "below_absolute_minimum")
        self.assertEqual(note["status"], "structure_rejected")

    def test_快照与成功审计任一步失败会一起回滚(self):
        recipes, _ = extract_bench_recipes(_load_fixture())
        run_id = self.conn.execute(
            "INSERT INTO ingest_run(source,category,started_at) "
            "VALUES ('POEDB','crafting_bench',datetime('now'))"
        ).lastrowid
        self.conn.execute(
            "CREATE TRIGGER fail_bench_audit BEFORE UPDATE ON ingest_run "
            "WHEN NEW.finished_at IS NOT NULL BEGIN "
            "SELECT RAISE(ABORT, 'audit failure'); END"
        )
        self.conn.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "audit failure"):
            _commit_bench_snapshot(
                self.conn,
                recipes[:1],
                fetched_at="2026-08-05T00:00:00+00:00",
                run_id=run_id,
                total=1,
                elapsed_ms=1,
                note="{}",
            )

        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM craft_bench_recipe").fetchone()[0],
            4,
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM craft_bench_recipe_cost").fetchone()[0],
            5,
        )
        audit = self.conn.execute(
            "SELECT finished_at,succeeded FROM ingest_run WHERE run_id=?", (run_id,)
        ).fetchone()
        self.assertIsNone(audit["finished_at"])
        self.assertEqual(audit["succeeded"], 0)

    def test_coverage由真实目录表探测且craft_rule仍未采集(self):
        statuses = {status.key: status for status in probe(self.conn)}
        bench = statuses["crafting_bench"]
        rules = statuses["craft_rule"]

        self.assertEqual(bench.status, "partial")
        self.assertEqual(bench.item_count, 4)
        self.assertEqual(set(bench.channels), {"改造石", "点金石", "神圣石", "灌顶石", "玻璃弹珠"})
        self.assertIn("bench_recipe_catalog", bench.spec.tools)
        self.assertIn("状态转移", bench.spec.partial_note)
        # 断言**不可用**而不是某个具体状态字：第三十九轮起 `probe()` 会消费
        # `pob_files`，桩库里 craft_rule 没数据但本机若有 PoB 检出就报 harvested。
        # 这条用例要钉的是「入库工艺台目录不会让 craft_rule 看起来已采集」，
        # 而 harvested 与 not_collected 都满足这一点；写死状态字会让它随本机
        # 有没有 PoB 检出而红绿摇摆。
        self.assertFalse(rules.usable)
        self.assertEqual(rules.item_count, 0)


class TestBenchRecipeCatalogTool(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _populated_conn()

    def tearDown(self) -> None:
        self.conn.close()

    @staticmethod
    def _all_keys(value):
        if isinstance(value, dict):
            yield from value.keys()
            for child in value.values():
                yield from TestBenchRecipeCatalogTool._all_keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from TestBenchRecipeCatalogTool._all_keys(child)

    def test_目录查询返回原文证据但不越界为步骤(self):
        with patch("tools.impl.bench.dbmod.get_conn", return_value=self.conn):
            result = bench_recipe_catalog("最大生命", item_class="项链")

        self.assertTrue(result.ok)
        self.assertTrue(result.data["found"])
        self.assertEqual(result.data["total"], 1)
        self.assertEqual(len(result.citations), 1)
        evidence = result.citations[0]
        self.assertEqual(evidence["url"], CRAFTING_BENCH_URL)
        self.assertEqual(evidence["claim_scope"], "bench_catalog")
        self.assertEqual(evidence["claim_data"]["effect_text"], "+(15-25) 最大生命")
        self.assertEqual(
            evidence["claim_data"]["raw_costs"],
            [{"amount": 1, "currency": "改造石"}],
        )
        self.assertIn("1x 改造石", evidence["text"])
        self.assertIn("适用 ItemClasses", evidence["text"])
        self.assertIn("不证明", evidence["text"])
        self.assertFalse({"steps", "probability", "cost"} & set(self._all_keys(result.data)))
        self.assertNotIn("url", set(self._all_keys(result.data)))

    def test_双费用按页面顺序完整呈现(self):
        with patch("tools.impl.bench.dbmod.get_conn", return_value=self.conn):
            result = bench_recipe_catalog("跟接邻", item_class="药剂")
        text = result.citations[0]["text"]
        self.assertIn("5x 灌顶石 + 5x 玻璃弹珠", text)

    def test_ItemClass是精确过滤而非猜测映射(self):
        with patch("tools.impl.bench.dbmod.get_conn", return_value=self.conn):
            result = bench_recipe_catalog("最大生命", item_class="胸甲")
        self.assertTrue(result.ok)
        self.assertFalse(result.data["found"])
        self.assertEqual(result.data["total"], 0)

    def test_LIKE通配符不能绕过关键词且合法百分号可查询(self):
        with patch("tools.impl.bench.dbmod.get_conn", return_value=self.conn):
            self.assertFalse(bench_recipe_catalog("%").ok)
            self.assertFalse(bench_recipe_catalog("_").ok)
            result = bench_recipe_catalog("% 火焰抗性")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["found"])
        self.assertEqual(result.data["total"], 1)
        self.assertIn("火焰抗性", result.citations[0]["text"])

    def test_参数边界与schema一致(self):
        with patch("tools.impl.bench.dbmod.get_conn", return_value=self.conn):
            self.assertFalse(bench_recipe_catalog("").ok)
            self.assertFalse(bench_recipe_catalog("生命", limit=0).ok)
        spec = REGISTRY.spec("bench_recipe_catalog")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec.parameters["properties"]["limit"]["maximum"], 20)
        self.assertFalse(spec.parameters.get("additionalProperties", True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
