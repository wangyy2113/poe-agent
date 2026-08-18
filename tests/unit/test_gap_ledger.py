"""缺口台账：分类、归一化与迁移（需求 2.12）。

台账的用途是**按频次排序即采集优先级**。它先前被出口闸门的拦截句原文污染：
库里实测有 `缺少可引用依据：---`、`缺少可引用依据：）` 这类条目，那不是知识缺口
而是闸门的分类误判。这些用例把修复后的三条不变量钉住：

  1. 排版元素被拦时**不入台账**（它属于「确定误拦」这个观测量）；
  2. 其余拦截按实义词归一化后入账，同一缺口不因数值或措辞不同散成多行；
  3. 三类来源分开（retrieval_gap / tool_gap / binding_block），只有前两类排优先级。

用内存 SQLite，不碰真实库。
"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from core import db as dbmod
from orchestrator import loop as looplib
from orchestrator.binding import BindingResult, SentenceVerdict

_OLD_SCHEMA = """
CREATE TABLE knowledge_gap (
    gap_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    question     TEXT NOT NULL,
    missing_point TEXT NOT NULL,
    guessed_category TEXT,
    hit_count    INTEGER DEFAULT 1,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);
"""

_NEW_SCHEMA = _OLD_SCHEMA.replace(
    "guessed_category TEXT,", "guessed_category TEXT,\n    kind TEXT,"
)


def _conn(schema: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    return conn


def _insert(conn: sqlite3.Connection, question: str, point: str, **kw) -> None:
    cols = "question, missing_point, guessed_category, hit_count, first_seen, last_seen"
    vals = [question, point, kw.get("category"), kw.get("hit_count", 1),
            "2026-08-01 00:00:00", "2026-08-01 00:00:00"]
    conn.execute(f"INSERT INTO knowledge_gap({cols}) VALUES (?,?,?,?,?,?)", vals)
    conn.commit()


def _verdict(text: str, *, ok: bool, kind: str = "factual") -> SentenceVerdict:
    return SentenceVerdict(text=text, kind=kind, labels=(), ok=ok,
                           reason="" if ok else "事实句未绑定引用")


class MigrationTest(unittest.TestCase):
    def test_adds_kind_column_and_is_idempotent(self) -> None:
        conn = _conn(_OLD_SCHEMA)
        dbmod._migrate_gap_kind(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(knowledge_gap)")}
        self.assertIn("kind", cols)
        dbmod._migrate_gap_kind(conn)  # 再跑一次不能报错
        self.assertIn("kind", {r[1] for r in conn.execute("PRAGMA table_info(knowledge_gap)")})

    def test_backfills_historical_noise_as_legacy(self) -> None:
        """历史闸门噪声只标记不删除 —— 台账是审计记录，删掉就无法回溯。"""
        conn = _conn(_OLD_SCHEMA)
        _insert(conn, "猎首有哪些词缀？", "缺少可引用依据：---")
        _insert(conn, "猎首有哪些词缀？", "缺少可引用依据：**隐式词缀**")
        _insert(conn, "流放之路2 的升华机制是什么", "仅命中 1 个查询词（下限 2）")
        _insert(conn, "某组合能否共存", "[affix_capacity] 前后缀容量无来源")
        dbmod._migrate_gap_kind(conn)
        rows = {r["missing_point"]: r["kind"] for r in conn.execute(
            "SELECT missing_point, kind FROM knowledge_gap")}
        self.assertEqual("binding_block_legacy", rows["缺少可引用依据：---"])
        self.assertEqual("binding_block_legacy", rows["缺少可引用依据：**隐式词缀**"])
        self.assertEqual("retrieval_gap", rows["仅命中 1 个查询词（下限 2）"])
        self.assertEqual("tool_gap", rows["[affix_capacity] 前后缀容量无来源"])
        self.assertEqual(4, conn.execute("SELECT count(*) FROM knowledge_gap").fetchone()[0])


class RecordBindingGapsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn(_NEW_SCHEMA)
        self.conn.execute(
            "CREATE UNIQUE INDEX idx_gap_unique ON knowledge_gap(question, missing_point)"
        )
        self._patch = patch.object(dbmod, "get_conn", return_value=self.conn)
        self._patch.start()
        # 归因要连真实库，这里断掉，只测台账写入本身
        self._attr = patch.dict("sys.modules", {})
        self.addCleanup(self._patch.stop)

    def _rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT question, missing_point, kind, hit_count FROM knowledge_gap"
        ))

    def test_structural_blocks_are_not_recorded(self) -> None:
        """排版元素被拦是分类误判，不是知识缺口。"""
        binding = BindingResult(verdicts=[
            _verdict("---", ok=False),
            _verdict("**隐式词缀**", ok=False),
            _verdict("## 猎首（Headhunter）", ok=False),
            _verdict("）", ok=False),
        ])
        looplib._record_binding_gaps("猎首有哪些词缀？", binding)
        self.assertEqual([], self._rows())

    def test_real_unsupported_claim_is_recorded(self) -> None:
        binding = BindingResult(verdicts=[
            _verdict("猎首还会让你免疫冰冻。", ok=False),
        ])
        looplib._record_binding_gaps("猎首有哪些词缀？", binding)
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("binding_block", rows[0]["kind"])
        self.assertIn("无据断言", rows[0]["missing_point"])
        self.assertIn("免疫冰冻", rows[0]["missing_point"])

    def test_numeric_variants_collapse_to_one_row(self) -> None:
        """同一个缺口每次取的具体值不同，留着数值会让它散成 N 行。"""
        for text in (
            "该词缀最高档位是 T2[[E1]]。",
            "该词缀最高档位是 T3[[E1]]。",
            "该词缀最高档位是 T5[[E1]]。",
        ):
            looplib._record_binding_gaps("这条词缀最高几档", BindingResult(
                verdicts=[_verdict(text, ok=False)]
            ))
        rows = self._rows()
        self.assertEqual(1, len(rows), f"应合并为一行，实际 {[dict(r) for r in rows]}")
        self.assertEqual(3, rows[0]["hit_count"])

    def test_passed_sentences_are_not_recorded(self) -> None:
        binding = BindingResult(verdicts=[
            _verdict("猎首提供 +40 到 +55 力量[[E1]]。", ok=True),
        ])
        looplib._record_binding_gaps("猎首力量", binding)
        self.assertEqual([], self._rows())

    def test_same_turn_duplicates_counted_once(self) -> None:
        """一轮里同一句被拦两次（分句重复）不该记成两次命中。"""
        binding = BindingResult(verdicts=[
            _verdict("目标怪物类型不同，且触发概率不同。", ok=False),
            _verdict("目标怪物类型不同，且触发概率不同。", ok=False),
        ])
        looplib._record_binding_gaps("猎首与仿品区别", binding)
        rows = self._rows()
        self.assertEqual(1, len(rows))
        self.assertEqual(1, rows[0]["hit_count"])


class RecordGapsKindTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn(_NEW_SCHEMA)
        self.conn.execute(
            "CREATE UNIQUE INDEX idx_gap_unique ON knowledge_gap(question, missing_point)"
        )
        p = patch.object(dbmod, "get_conn", return_value=self.conn)
        p.start()
        self.addCleanup(p.stop)

    def test_default_kind_is_retrieval_gap(self) -> None:
        looplib._record_gaps("问题", ["缺失点"])
        row = self.conn.execute("SELECT kind FROM knowledge_gap").fetchone()
        self.assertEqual("retrieval_gap", row["kind"])

    def test_tool_gap_kind_is_kept(self) -> None:
        looplib._record_gaps("问题", ["[affix_capacity] 无来源"],
                             category="not_collected/craft_rule", kind="tool_gap")
        row = self.conn.execute("SELECT kind, guessed_category FROM knowledge_gap").fetchone()
        self.assertEqual("tool_gap", row["kind"])
        self.assertEqual("not_collected/craft_rule", row["guessed_category"])

    def test_hit_count_increments_without_duplicating_rows(self) -> None:
        for _ in range(3):
            looplib._record_gaps("问题", ["同一个缺失点"])
        rows = list(self.conn.execute("SELECT hit_count FROM knowledge_gap"))
        self.assertEqual(1, len(rows))
        self.assertEqual(3, rows[0]["hit_count"])


if __name__ == "__main__":
    unittest.main()
