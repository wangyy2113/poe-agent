"""七页刷新：来源预检、原子发布、并发变化及真实概率出口。"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.db import FTS_SCHEMA, SCHEMA
from core.backup import BackupError, list_backups, verify_backup
from craft.pool import analyze_affix_pool, verify_affix_page_snapshot
from craft.pool_refresh import (
    POOL_PAGES,
    PoolRefreshError,
    apply_refresh,
    plan_refresh,
    prepare_refresh,
)
from kb.fetcher import FetchResult
from kb.pipeline import _upsert_mods
from retrieval import vocab as vocab_mod
from scripts.refresh_craft_pool import status
from scripts import refresh_craft_pool as refresh_cli
from scripts.import_pob_bases import replace_snapshot
from tests.unit.test_craft_pool import _bases, _insert_mod, _repo

PAGE_TAGS = {
    page: "".join(part.capitalize() for part in page.removeprefix("Body_Armours_").split("_"))
    for page in POOL_PAGES
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(FTS_SCHEMA)
    return conn


def _html(slug: str, *, rows: int = 2, corrupt: bool = False) -> str:
    payload = {
        "baseitem": {
            "href": "Body_Armours", "cn": "Body Armour",
            "tags": PAGE_TAGS[slug], "link_name": "胸甲",
        },
        "opt": {"ItemClassesCode": "Body Armour"},
        "config": {"normal": {"title": "普通"}},
        "gen": {"1": "前缀", "2": "后缀"},
        "normal": [
            {
                "ModGenerationTypeID": "1",
                "ModFamilyList": ["IncreasedLife"], "Level": 1,
                "Code": "TestLife", "DropChance": 100,
                "Name": "生命", "str": "+(10-19) 最大生命",
            },
            {
                "ModGenerationTypeID": "2",
                "ModFamilyList": ["FireResistance"], "Level": 1,
                "Code": "TestFire", "DropChance": 200,
                "Name": "火抗", "str": "+(10-19)% 火焰抗性",
            },
        ][:rows],
    }
    if corrupt:
        payload["normal"][0].pop("DropChance")
    return (
        '<!DOCTYPE html><html lang="cn"><head><title>'
        f'{slug} - POEDB</title></head><body>'
        f'<a href="/cn/{slug}">CN</a>'
        f'<a href="/cn/marked?id={slug}">Edit</a>'
        '<div id="ModifiersCalc"></div>'
        f'<script>new ModsView({json.dumps(payload)})</script>'
        '<footer>Copyright</footer>'
        '<script src="https://cdn.poedb.tw/js/poedb_footer.test.js"></script>'
        '</body></html>'
    )


class FakeFetcher:
    def __init__(self, *, bad_slug: str = "", mode: str = "", days_old: int = 0) -> None:
        self.bad_slug = bad_slug
        self.mode = mode
        self.days_old = days_old
        self.calls: list[tuple[str, bool]] = []

    def fetch(self, url: str, *, force: bool = False) -> FetchResult:
        self.calls.append((url, force))
        slug = url.rsplit("/", 1)[-1]
        html = _html(
            slug,
            rows=1 if slug == self.bad_slug and self.mode == "missing_side" else 2,
            corrupt=slug == self.bad_slug and self.mode == "bad_weight",
        )
        return FetchResult(
            url=url,
            text=html,
            content_hash=(
                "0" * 64 if slug == self.bad_slug and self.mode == "bad_hash"
                else hashlib.sha256(html.encode("utf-8")).hexdigest()
            ),
            from_cache=False,
            fetched_at=(datetime.now(timezone.utc) - timedelta(days=self.days_old))
            .isoformat(timespec="seconds"),
            final_url=(
                "https://poedb.tw/cn/Other"
                if slug == self.bad_slug and self.mode == "redirect" else url
            ),
        )


class TestPoolRefresh(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _db()
        self.temp = tempfile.TemporaryDirectory()
        self.vocab_patch = patch.object(
            vocab_mod, "VOCAB_PATH", Path(self.temp.name) / "vocab.txt"
        )
        self.vocab_patch.start()

    def tearDown(self) -> None:
        self.vocab_patch.stop()
        vocab_mod._cache = None  # 测试不把合成词表留给后续用例
        self.temp.cleanup()
        self.conn.close()

    def _plan(self):  # noqa: ANN202
        pages = prepare_refresh(FakeFetcher())
        return plan_refresh(self.conn, pages)

    def test_七页预检不写库发布后来源证明和索引同批闭合(self):
        fetcher = FakeFetcher()
        pages = prepare_refresh(fetcher)
        self.assertEqual(len(pages), 7)
        self.assertTrue(all(force for _, force in fetcher.calls))
        plan = plan_refresh(self.conn, pages)
        self.assertEqual(plan.to_dict()["new_rows"], 14)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM mod_affix").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM ingest_run").fetchone()[0], 0)

        result = apply_refresh(self.conn, plan)
        self.assertTrue(result["applied"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM mod_affix").fetchone()[0], 14)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM ingest_run").fetchone()[0], 1)
        self.assertEqual(status(self.conn)["ready_pages"], 7)
        self.assertGreater(result["indexed_mod_chunks"], 0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM chunk WHERE entity_id LIKE 'mod_ladder:%'")
            .fetchone()[0], result["indexed_mod_chunks"],
        )
        for page in plan.pages:
            proof, reason = verify_affix_page_snapshot(self.conn, page.slug)
            self.assertIsNotNone(proof, reason)
            self.assertEqual(proof.row_digest, page.row_digest)
            self.assertEqual(proof.scope, "normal")

    def test_刷新真实概率出口从未验证变成精确(self):
        replace_snapshot(self.conn, _bases(), _repo(), 2)
        self.conn.commit()
        args = dict(
            base_type="铁制背心", item_level=50,
            generation_side="prefix", target_query="最大生命",
        )
        before = analyze_affix_pool(self.conn, **args)
        self.assertEqual(before.gaps[0].code, "pool_snapshot_unverified")
        apply_refresh(self.conn, self._plan())
        after = analyze_affix_pool(self.conn, **args)
        self.assertEqual(after.status, "exact")
        self.assertEqual((after.probability_numerator, after.probability_denominator), (1, 1))

    def test_任一页损坏重定向缺侧或过期都阻止整批预检(self):
        slug = POOL_PAGES[3]
        for mode in ("bad_weight", "bad_hash", "redirect", "missing_side"):
            with self.subTest(mode=mode), self.assertRaises(PoolRefreshError):
                prepare_refresh(FakeFetcher(bad_slug=slug, mode=mode))
        with self.assertRaisesRegex(PoolRefreshError, "过期"):
            prepare_refresh(FakeFetcher(days_old=8))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM mod_affix").fetchone()[0], 0)

    def test_预检后旧页变化拒绝发布且不插入新审计(self):
        plan = self._plan()
        _insert_mod(
            self.conn, uid="concurrent", group="ConcurrentLife", weight=1,
            text="+(1-2) 最大生命",
        )
        self.conn.commit()
        with self.assertRaisesRegex(PoolRefreshError, "已改变"):
            apply_refresh(self.conn, plan)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM mod_affix").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM ingest_run").fetchone()[0], 0)

    def test_中途写入失败整批回滚旧页索引和审计(self):
        plan = self._plan()
        from craft import pool_refresh

        original = pool_refresh._replace_normal_page
        calls = 0

        def fail_third(conn, slug, records):  # noqa: ANN001, ANN202
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("注入第三页失败")
            return original(conn, slug, records)

        with patch.object(pool_refresh, "_replace_normal_page", side_effect=fail_third):
            with self.assertRaisesRegex(RuntimeError, "第三页"):
                apply_refresh(self.conn, plan)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM mod_affix").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM ingest_run").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM chunk").fetchone()[0], 0)

    def test_上游hover哈希漂移只刷新normal并保留唯一旧ID与其它来源行(self):
        pages = prepare_refresh(FakeFetcher())
        first_normal = next(
            record for record in pages[0].records if record.category == "normal"
        )
        legacy = replace(first_normal, mod_code="?s=Data%5CMods%2FTestLife")
        other_source = replace(
            first_normal, category="essence", category_label="精华",
            mod_code="?s=Data%5CMods%2FEssenceLife",
        )
        _upsert_mods(self.conn, [legacy, other_source])
        self.conn.commit()
        old_other_uid = other_source.uid
        plan = plan_refresh(self.conn, pages)
        self.assertEqual(plan.changes[0].preserved_legacy_ids, 1)
        self.assertEqual(plan.changes[0].new_opaque_ids, 1)
        apply_refresh(self.conn, plan)
        self.assertEqual(
            self.conn.execute("SELECT mod_code FROM mod_affix WHERE uid=?", (legacy.uid,))
            .fetchone()[0], legacy.mod_code,
        )
        self.assertEqual(
            self.conn.execute("SELECT mod_code FROM mod_affix WHERE uid=?", (old_other_uid,))
            .fetchone()[0], other_source.mod_code,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM mod_affix WHERE item_class_page=? AND category='essence'",
                (POOL_PAGES[0],),
            ).fetchone()[0], 1,
        )
        self.conn.execute(
            "UPDATE mod_affix SET weight=weight+1 WHERE uid=?", (old_other_uid,)
        )
        self.conn.commit()
        self.assertEqual(status(self.conn)["ready_pages"], 7)

    def test_旧normal标识无法唯一对账时拒绝发布(self):
        pages = prepare_refresh(FakeFetcher())
        first_normal = next(
            record for record in pages[0].records if record.category == "normal"
        )
        _upsert_mods(self.conn, [
            replace(first_normal, mod_code="legacy-one"),
            replace(first_normal, mod_code="legacy-two"),
        ])
        self.conn.commit()
        with self.assertRaisesRegex(PoolRefreshError, "无法唯一对账"):
            plan_refresh(self.conn, pages)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM ingest_run").fetchone()[0], 0)

    def test_normal来源审计计数被篡改后拒绝概率(self):
        apply_refresh(self.conn, self._plan())
        row = self.conn.execute("SELECT run_id,note FROM ingest_run").fetchone()
        note = json.loads(row["note"])
        note["pages"][POOL_PAGES[0]]["source_normal_rows"] += 1
        self.conn.execute(
            "UPDATE ingest_run SET note=? WHERE run_id=?",
            (json.dumps(note), row["run_id"]),
        )
        self.conn.commit()
        self.assertEqual(status(self.conn)["ready_pages"], 6)

    def test_发布后人工改权重立即使来源证明失效(self):
        apply_refresh(self.conn, self._plan())
        self.conn.execute(
            "UPDATE mod_affix SET weight=weight+1 WHERE item_class_page=? "
            "AND category='normal' AND gen_type=1", (POOL_PAGES[0],)
        )
        self.conn.commit()
        report = status(self.conn)
        self.assertFalse(report["ready"])
        self.assertEqual(report["ready_pages"], 6)
        self.assertIn("不一致", report["pages"][0]["reason"])


class TestRefreshCli(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "poe.db"
        self.backup_dir = self.root / "backups"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(SCHEMA)
        conn.executescript(FTS_SCHEMA)
        conn.commit()
        conn.close()
        self.patches = [
            patch.object(refresh_cli, "get_config", return_value=SimpleNamespace(
                db_path=self.db_path, backup_dir=self.backup_dir,
            )),
            patch.object(refresh_cli, "_make_fetcher", return_value=FakeFetcher()),
            patch.object(vocab_mod, "VOCAB_PATH", self.root / "vocab.txt"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        vocab_mod._cache = None
        self.temp.cleanup()

    def _run(self, command: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = refresh_cli.main([command])
        return code, output.getvalue()

    def test_preview只读且apply先备份再发布(self) -> None:
        preview_code, preview = self._run("preview")
        self.assertEqual(preview_code, 0)
        self.assertIn("未修改数据库", preview)
        self.assertFalse(self.backup_dir.exists())
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM mod_affix").fetchone()[0], 0)

        apply_code, applied = self._run("apply")
        self.assertEqual(apply_code, 0)
        self.assertIn("已发布", applied)
        backups = list_backups(self.backup_dir)
        self.assertEqual(len(backups), 1)
        self.assertTrue(verify_backup(self.backup_dir, backups[0]["backup_name"]).valid)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            self.assertEqual(status(conn)["ready_pages"], 7)

    def test_backup失败时不修改任一词缀页(self) -> None:
        with patch.object(refresh_cli, "create_backup", side_effect=BackupError("磁盘不可写")):
            code, output = self._run("apply")
        self.assertEqual(code, 2)
        self.assertIn("磁盘不可写", output)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM mod_affix").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM ingest_run").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
