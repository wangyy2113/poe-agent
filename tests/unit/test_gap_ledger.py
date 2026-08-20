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

#: 现行结构：`kind`（第十一轮补）+ `source`（第三十轮补）。
#: 两个 schema 并存是为了钉住**迁移**——旧库不能因为少列就写不进台账。
_NEW_SCHEMA = _OLD_SCHEMA.replace(
    "guessed_category TEXT,", "guessed_category TEXT,\n    kind TEXT,\n    source TEXT,"
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




class PriorityIsNotContaminatedTest(unittest.TestCase):
    """采集优先级要排除两类噪声（2026-08-20 第三十轮）。

    实测：台账按 `hit_count` 排序的前两名是

        20 次  流放之路2 的升华机制是什么          → 永久越界，没有待办
        14 次  请你猜一下下个赛季会加什么新机制    → 本来就该拒答

    两条都来自黄金问答集（评测每跑一次加一次），而且第一条还被归到了
    `not_recalled/ascendancy`——照着它去补升华数据是纯浪费。
    需求 2.12 说「按频次排序即采集优先级」，那这张表必须先能分清
    **评测流量 vs 真实流量**、**该采集 vs 永不采集**。
    """

    def setUp(self) -> None:
        self.conn = _conn(_NEW_SCHEMA)
        # `_record_gaps` 用 `ON CONFLICT(question, missing_point)`，没有这个唯一索引
        # 就会抛「no matching constraint」并被那层 except 吞掉（一行都写不进去）
        self.conn.execute(
            "CREATE UNIQUE INDEX idx_gap_unique ON knowledge_gap(question, missing_point)"
        )
        self.patch = patch.object(dbmod, "get_conn", return_value=self.conn)
        self.patch.start()
        looplib.GAP_SOURCE.set("chat")

    def tearDown(self) -> None:
        self.patch.stop()
        looplib.GAP_SOURCE.set("chat")
        self.conn.close()

    def _priority(self) -> list[sqlite3.Row]:
        from core.db import COLLECTION_GAP_KINDS

        marks = ",".join("?" for _ in COLLECTION_GAP_KINDS)
        return self.conn.execute(
            f"SELECT question, kind, source FROM knowledge_gap "  # noqa: S608
            f"WHERE COALESCE(kind,'retrieval_gap') IN ({marks}) "
            "AND COALESCE(source,'chat') <> 'eval' ORDER BY hit_count DESC",
            COLLECTION_GAP_KINDS,
        ).fetchall()

    def test_poe2越界不进采集优先级(self) -> None:
        looplib._record_gaps(  # noqa: SLF001
            "流放之路2 的升华机制是什么", ["越界：《流放之路 2》问题"],
            category="out_of_scope/poe2", kind="out_of_scope",
        )
        row = self.conn.execute("SELECT kind FROM knowledge_gap").fetchone()
        self.assertEqual("out_of_scope", row["kind"])
        self.assertEqual([], self._priority())

    def test_评测流量不进采集优先级(self) -> None:
        looplib.GAP_SOURCE.set("eval")
        looplib._record_gaps("黄金集里的问题", ["缺据"], kind="retrieval_gap")  # noqa: SLF001
        self.assertEqual("eval", self.conn.execute(
            "SELECT source FROM knowledge_gap").fetchone()["source"])
        self.assertEqual([], self._priority())

    def test_真实流量的缺口照旧进优先级(self) -> None:
        """成对点：排除噪声不能把真实需求一起排掉。"""
        looplib._record_gaps("某个真实问题", ["缺据"], kind="retrieval_gap")  # noqa: SLF001
        self.assertEqual(["某个真实问题"], [r["question"] for r in self._priority()])

    def test_导入脚本写的ingest_gap也算待办(self) -> None:
        """它在用、却一直没在 `/admin/stats` 的优先级视图里露出来，
        而「这 51 条 POEDB 有而 PoB 没有」恰恰是最可执行的一类。"""
        from core.db import COLLECTION_GAP_KINDS

        self.assertIn("ingest_gap", COLLECTION_GAP_KINDS)
        looplib._record_gaps("底材清单", ["未桥接 7 条"], kind="ingest_gap")  # noqa: SLF001
        self.assertEqual(["底材清单"], [r["question"] for r in self._priority()])

    def test_同一问题既被评测又被真实问过时按真实记(self) -> None:
        """宁可高估真实需求，也不要把它藏进 eval。"""
        looplib.GAP_SOURCE.set("eval")
        looplib._record_gaps("两边都问过", ["缺据"], kind="retrieval_gap")  # noqa: SLF001
        looplib.GAP_SOURCE.set("chat")
        looplib._record_gaps("两边都问过", ["缺据"], kind="retrieval_gap")  # noqa: SLF001
        row = self.conn.execute("SELECT source, hit_count FROM knowledge_gap").fetchone()
        self.assertEqual(("chat", 2), (row["source"], row["hit_count"]))


class GapSourceMigrationTest(unittest.TestCase):
    """迁移只重新归类、不删行——台账是审计记录。"""

    def test_迁移把poe2条目移出优先级并标记评测流量(self) -> None:
        from core.db import _migrate_gap_source  # noqa: PLC0415

        conn = _conn(_OLD_SCHEMA.replace(
            "guessed_category TEXT,", "guessed_category TEXT,\n    kind TEXT,"
        ))
        _insert(conn, "流放之路2 的升华机制是什么", "缺据",
                category="not_recalled/ascendancy", hit_count=20)
        _insert(conn, "某个真实问题", "缺据", hit_count=1)
        conn.execute("UPDATE knowledge_gap SET kind='retrieval_gap'")
        _migrate_gap_source(conn)
        rows = {
            r["question"]: (r["kind"], r["source"])
            for r in conn.execute("SELECT question, kind, source FROM knowledge_gap")
        }
        self.assertEqual("out_of_scope", rows["流放之路2 的升华机制是什么"][0])
        self.assertEqual(("retrieval_gap", "chat"), rows["某个真实问题"])
        self.assertEqual(2, conn.execute(
            "SELECT count(*) FROM knowledge_gap").fetchone()[0], "不许删行")
        conn.close()

    def test_迁移把导入脚本写的行归到import但不动命中次数(self) -> None:
        """第三十一轮：四个导入脚本都没写 `source`，于是 `COALESCE(source,'chat')`
        把脚本自己的写入算成用户流量。命中次数**不在迁移里猜**——那些数是
        「导入跑了几次」，得由导入脚本下次重写时纠正。"""
        from core.db import _migrate_gap_source  # noqa: PLC0415

        conn = _conn(_NEW_SCHEMA)
        _insert(conn, "技能石与辅助宝石的完整清单", "[no_zh_name] 11 颗未桥接", hit_count=6)
        conn.execute("UPDATE knowledge_gap SET kind='ingest_gap'")
        _migrate_gap_source(conn)
        row = conn.execute("SELECT source, hit_count FROM knowledge_gap").fetchone()
        self.assertEqual(("import", 6), (row["source"], row["hit_count"]))
        conn.close()


class IngestGapTest(unittest.TestCase):
    """采集类缺口：整组替换、`hit_count` 量条数、来源标 `import`（第三十一轮）。

    起因是照着台账排序去干活时发现头部那条**已经过期**：写着「11 颗未能桥接」，
    实际只剩 1 颗，而它仍以 6 次命中排第一（那 6 次是导入跑了 6 次）。
    这是本项目第六次撞上「测量工具本身失真」。
    """

    def setUp(self) -> None:
        self.conn = _conn(_NEW_SCHEMA)

    def tearDown(self) -> None:
        self.conn.close()

    def _rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT missing_point, kind, source, hit_count, first_seen "
            "FROM knowledge_gap ORDER BY missing_point"
        ).fetchall()

    def _write(self, points: list[tuple[str, int]]) -> int:
        return dbmod.record_ingest_gaps(
            self.conn, question="技能石与辅助宝石的完整清单",
            category="partial/skill_gem", points=points,
        )

    def test_旧快照整组退场(self) -> None:
        """条数一变就是新的一行，旧行留着会继续以更高的命中次数占住优先级头部。"""
        self._write([("[no_zh_name] 11 颗未桥接", 11)])
        self._write([("[no_zh_name] 1 颗未桥接", 1)])
        self.assertEqual(["[no_zh_name] 1 颗未桥接"], [r["missing_point"] for r in self._rows()])

    def test_命中次数量的是缺多少条而不是跑了几次(self) -> None:
        for _ in range(3):
            self._write([("[pob_missing] 50 颗未桥接", 50)])
        row = self._rows()[0]
        self.assertEqual(50, row["hit_count"])

    def test_来源标import(self) -> None:
        self._write([("[pob_missing] 50 颗未桥接", 50)])
        self.assertEqual(("ingest_gap", "import"), (self._rows()[0]["kind"], self._rows()[0]["source"]))

    def test_首次记录时间承接下来(self) -> None:
        """「这个缺口最早什么时候记下的」是审计信息，不能因为重写而丢。"""
        self._write([("[no_zh_name] 11 颗未桥接", 11)])
        self.conn.execute("UPDATE knowledge_gap SET first_seen='2026-08-18 00:00:00'")
        self._write([("[no_zh_name] 1 颗未桥接", 1)])
        self.assertEqual("2026-08-18 00:00:00", self._rows()[0]["first_seen"])

    def test_别的问题下的行不受影响(self) -> None:
        """成对点：整组替换的单位是 question，不能顺手清掉别的采集脚本写的行。"""
        dbmod.record_ingest_gaps(
            self.conn, question="怪物词缀的完整清单", category="partial/monster_mod",
            points=[("[no_mod_text] 248 条未入库", 248)],
        )
        self._write([("[no_zh_name] 1 颗未桥接", 1)])
        self.assertEqual(2, len(self._rows()))

    def test_真实流量的行不受影响(self) -> None:
        """同一 question 下用户问过的记录（source='chat'）不许被导入清掉。"""
        _insert(self.conn, "技能石与辅助宝石的完整清单", "用户问过的缺据", hit_count=4)
        self.conn.execute("UPDATE knowledge_gap SET kind='retrieval_gap', source='chat'")
        self._write([("[no_zh_name] 1 颗未桥接", 1)])
        self.assertEqual(
            {("retrieval_gap", "chat", 4), ("ingest_gap", "import", 1)},
            {(r["kind"], r["source"], r["hit_count"]) for r in self._rows()},
        )

    def test_全部桥接上时台账清空(self) -> None:
        """缺口补完就该消失——留着一行「0 条未桥接」等于给自己一个假待办。"""
        self._write([("[no_zh_name] 11 颗未桥接", 11)])
        self.assertEqual(0, self._write([]))
        self.assertEqual([], self._rows())


class IngestGapWriterTest(unittest.TestCase):
    """四个采集脚本都走同一个写入口（第三十一轮把四份重复的 UPSERT 收成一份）。

    这四处此前**一条单测都没有**，而它们正是「该采集什么」这个信号的唯一来源。
    每个写入口都钉三件事：`kind='ingest_gap'`、`source='import'`、
    `hit_count` = 缺多少条。
    """

    def setUp(self) -> None:
        from core.db import SCHEMA  # noqa: PLC0415

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def tearDown(self) -> None:
        self.conn.close()

    def _rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT question, missing_point, kind, source, hit_count FROM knowledge_gap"
        ).fetchall()

    def _assert_shape(self, *, magnitude: int) -> sqlite3.Row:
        row = self._rows()[0]
        self.assertEqual(("ingest_gap", "import"), (row["kind"], row["source"]))
        self.assertEqual(magnitude, row["hit_count"])
        return row

    def test_技能石导入(self) -> None:
        from kb.sources.pob_skills import SKIP_NO_ZH, SkippedGem  # noqa: PLC0415
        from scripts.import_pob_skills import record_skipped  # noqa: PLC0415

        record_skipped(self.conn, [SkippedGem("SupportEclipse", "Eclipse", SKIP_NO_ZH, "页面不存在")])
        row = self._assert_shape(magnitude=1)
        self.assertIn("Eclipse", row["missing_point"])

    def test_底材导入按类别分组(self) -> None:
        """底材的 question 是**类别**，所以两个类别是两行，替换互不影响。"""
        from kb.sources.pob_bases import SkippedBase  # noqa: PLC0415
        from scripts.import_pob_bases import record_skipped  # noqa: PLC0415

        record_skipped(self.conn, [
            SkippedBase("Hedron Bow", "bow", "no_poedb_page", "实验底材"),
            SkippedBase("Flare Mace", "mace", "no_poedb_page", "实验底材"),
            SkippedBase("Blunt Force Condenser", "mace", "no_poedb_page", "实验底材"),
        ])
        rows = {r["question"]: r["hit_count"] for r in self._rows()}
        self.assertEqual({"bow 底材的完整清单": 1, "mace 底材的完整清单": 2}, rows)

    def test_圣甲虫采集(self) -> None:
        from kb.pipeline import _record_scarab_gaps  # noqa: PLC0415
        from kb.sources.poedb_cards import ItemCard  # noqa: PLC0415

        _record_scarab_gaps(self.conn, [
            ItemCard("k1", "迷踪孕育石", "s1", "IncubatorStackable"),
        ])
        row = self._assert_shape(magnitude=1)
        self.assertIn("迷踪孕育石", row["missing_point"])

    def test_怪物词缀采集(self) -> None:
        from kb.pipeline import _record_monster_gaps  # noqa: PLC0415
        from kb.sources.poedb_monsters import MonsterExtract  # noqa: PLC0415

        _record_monster_gaps(self.conn, MonsterExtract(
            skipped=[("no_mod_text", "法瑞尔的狂野之灵"), ("no_mod_text", "法瑞尔的丰沃之灵")],
        ))
        row = self._assert_shape(magnitude=2)
        self.assertIn("2 条怪物词缀未入库", row["missing_point"])


if __name__ == "__main__":
    unittest.main()
