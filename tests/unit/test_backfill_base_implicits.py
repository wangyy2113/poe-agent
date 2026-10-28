"""中文固有隐式的现有库回填：精确身份、预检漂移与原子发布。"""

from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace

from core.db import _migrate_base_implicit_zh
from kb.sources.pob_bases import BaseStructureError
from scripts.backfill_base_implicits import apply_plan, build_plan
from scripts.import_pob_bases import replace_snapshot
from tests.unit.test_craft_pool import _bases, _conn, _repo


class BackfillBaseImplicitsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _conn()
        self.repo = _repo()
        self.bases = _bases()
        replace_snapshot(self.conn, self.bases, self.repo, 2)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_preview_and_apply_update_only_expected_base(self) -> None:
        changed = replace(self.bases[0], implicit_zh="+(8-12)% 所有元素抗性")
        incoming = [changed, *self.bases[1:]]
        self.conn.execute(
            "UPDATE pob_base_item SET content_hash=? WHERE base_id=?",
            (self.bases[0].legacy_content_hash, changed.base_id),
        )
        self.conn.commit()
        plan = build_plan(self.conn, incoming, self.repo.commit)
        self.assertEqual((changed.base_id,), plan.changed_ids)
        self.assertEqual(1, plan.chinese_count)
        self.assertEqual(1, apply_plan(self.conn, plan, self.repo.commit))
        row = self.conn.execute(
            "SELECT implicit_zh,content_hash FROM pob_base_item WHERE base_id=?",
            (changed.base_id,),
        ).fetchone()
        self.assertEqual(changed.implicit_zh, row["implicit_zh"])
        self.assertEqual(changed.content_hash, row["content_hash"])
        self.assertEqual(0, len(build_plan(self.conn, incoming, self.repo.commit).changed_ids))
        audit = self.conn.execute(
            "SELECT succeeded,note FROM ingest_run WHERE category='base_implicit_zh'"
        ).fetchone()
        self.assertEqual(1, audit["succeeded"])
        self.assertIn("中文固有隐式 1 条", audit["note"])

    def test_changed_identity_or_commit_is_rejected_before_writes(self) -> None:
        changed = replace(self.bases[0], implicit_zh="固定隐式")
        incoming = [changed, *self.bases[1:]]
        for field, value in (("poedb_slug", "Other_Base"),
                             ("source_commit", "0" * 40),
                             ("implicit_text", "另一词条")):
            with self.subTest(field=field):
                self.conn.execute(
                    f"UPDATE pob_base_item SET {field}=? WHERE base_id=?",
                    (value, changed.base_id),
                )
                with self.assertRaisesRegex(BaseStructureError, "漂移"):
                    build_plan(self.conn, incoming, self.repo.commit)
                self.conn.rollback()
        with self.assertRaisesRegex(BaseStructureError, "快照"):
            build_plan(self.conn, incoming[:-1], self.repo.commit)

    def test_old_translation_skips_only_that_base(self) -> None:
        changed = replace(self.bases[0], implicit_zh="固定隐式")
        incoming = [changed, *self.bases[1:]]
        self.conn.execute(
            "UPDATE pob_base_item SET name_zh='旧版译名',content_hash=? WHERE base_id=?",
            (replace(self.bases[0], name_zh="旧版译名").legacy_content_hash,
             changed.base_id),
        )
        self.conn.commit()
        plan = build_plan(self.conn, incoming, self.repo.commit)
        self.assertEqual(1, plan.translation_drift_count)
        self.assertEqual(0, plan.chinese_count)
        published = next(base for base in plan.bases if base.base_id == changed.base_id)
        self.assertEqual("旧版译名", published.name_zh)
        self.assertIsNone(published.implicit_zh)

    def test_change_after_preview_rolls_back_without_partial_publication(self) -> None:
        changed = replace(self.bases[0], implicit_zh="固定隐式")
        incoming = [changed, *self.bases[1:]]
        plan = build_plan(self.conn, incoming, self.repo.commit)
        self.conn.execute(
            "UPDATE pob_base_item SET content_hash='drift' WHERE base_id=?",
            (changed.base_id,),
        )
        self.conn.commit()
        with self.assertRaisesRegex(BaseStructureError, "哈希漂移"):
            apply_plan(self.conn, plan, self.repo.commit)
        row = self.conn.execute(
            "SELECT implicit_zh FROM pob_base_item WHERE base_id=?", (changed.base_id,)
        ).fetchone()
        self.assertIsNone(row["implicit_zh"])
        count = self.conn.execute(
            "SELECT count(*) FROM ingest_run WHERE category='base_implicit_zh'"
        ).fetchone()[0]
        self.assertEqual(0, count)

    def test_additive_migration_keeps_existing_rows(self) -> None:
        legacy = sqlite3.connect(":memory:")
        try:
            legacy.execute("CREATE TABLE pob_base_item(base_id TEXT PRIMARY KEY, name_en TEXT)")
            legacy.execute("INSERT INTO pob_base_item VALUES('one','Astral Plate')")
            _migrate_base_implicit_zh(legacy)
            _migrate_base_implicit_zh(legacy)
            row = legacy.execute(
                "SELECT base_id,name_en,implicit_zh FROM pob_base_item"
            ).fetchone()
            self.assertEqual(("one", "Astral Plate", None), row)
        finally:
            legacy.close()


if __name__ == "__main__":
    unittest.main()
