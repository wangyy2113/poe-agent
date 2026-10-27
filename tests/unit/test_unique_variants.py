"""PoB 历史变体隔离、差异与发布原子性。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.unique_variants import router as variant_router
from core.db import SCHEMA
from kb.sources.pob import PoBRepo
from kb.sources.pob_uniques import BaseTypeLine, PoBUnique, PoBUniqueMod
from kb.unique_variants import (
    VariantQueryError, compare_variants, list_variants, variant_detail,
)
from scripts.import_pob_uniques import publish_snapshot
from tools.impl.unique_variants import unique_variant_history


def sample_item() -> PoBUnique:
    return PoBUnique(
        name_en="Example Heart", source_file="Data/Uniques/body.lua",
        base_types=[BaseTypeLine("Old Armour", (1,)), BaseTypeLine("New Armour", (2,))],
        variant_names=["Pre 3.25.0", "Current"],
        all_mods=[
            PoBUniqueMod("Has no Sockets", "Has no Sockets"),
            PoBUniqueMod("+500 to maximum Life", "", (1,)),
            PoBUniqueMod("+1000 to maximum Life", "", (2,)),
        ],
    )


class UniqueVariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self.repo = PoBRepo(
            root=Path("."), origin="test", repo_url="https://github.com/example/pob",
            commit="a" * 40, manifest_version="2.0", tree_version="3_29",
        )
        self.item = sample_item()
        publish_snapshot(self.conn, [self.item], self.repo, 2,
                         min_items=1, min_variant_rows=2)

    def tearDown(self) -> None:
        self.conn.close()

    def test_history_is_isolated_and_complete(self) -> None:
        current = json.loads(self.conn.execute("SELECT mods FROM pob_unique").fetchone()[0])
        self.assertEqual(["Has no Sockets", "+1000 to maximum Life"],
                         [mod["text"] for mod in current])
        history = variant_detail(self.conn, "Example Heart", 1)["variant"]
        self.assertEqual(["Has no Sockets", "+500 to maximum Life"],
                         [mod["text"] for mod in history["mods"]])
        self.assertEqual("3.25.0 之前", history["label_meaning"])
        self.assertEqual("Old Armour", history["base_type_en"])
        self.assertIn("/blob/" + "a" * 40 + "/", history["url"])
        self.assertEqual(0, self.conn.execute("SELECT count(*) FROM chunk").fetchone()[0])
        self.assertEqual(0, self.conn.execute("SELECT count(*) FROM alias").fetchone()[0])

    def test_compare_preserves_common_and_reports_base_change(self) -> None:
        diff = compare_variants(self.conn, "Example Heart", 1, 2)
        self.assertEqual(["Has no Sockets"], [m["text"] for m in diff["shared_mods"]])
        self.assertEqual(["+500 to maximum Life"], [m["text"] for m in diff["removed_mods"]])
        self.assertEqual(["+1000 to maximum Life"], [m["text"] for m in diff["added_mods"]])
        self.assertTrue(diff["base_changed"])

    def test_unknown_old_base_and_ambiguous_name(self) -> None:
        item = sample_item()
        item.base_types = [BaseTypeLine("New Armour", (2,))]
        self.assertIsNone(item.base_type_for_variant(1))
        item.name_en = "Example Heart"
        item.base_types = [BaseTypeLine("Other Armour", (2,))]
        publish_snapshot(self.conn, [self.item, item], self.repo, 2,
                         min_items=1, min_variant_rows=2)
        with self.assertRaisesRegex(VariantQueryError, "多个"):
            list_variants(self.conn, "Example Heart")
        self.assertEqual(2, len(list_variants(self.conn, self.item.uid)["variants"]))

    def test_rollback_keeps_both_tables_and_audit(self) -> None:
        old_mods = self.conn.execute("SELECT mods FROM pob_unique").fetchone()[0]
        old_count = self.conn.execute("SELECT count(*) FROM ingest_run").fetchone()[0]
        self.conn.execute(
            "CREATE TRIGGER reject_variant BEFORE INSERT ON pob_unique_variant "
            "BEGIN SELECT RAISE(ABORT, 'reject variant'); END"
        )
        self.conn.commit()
        new_item = sample_item()
        new_item.all_mods.append(PoBUniqueMod("new current line", "", (2,)))
        with self.assertRaises(sqlite3.IntegrityError):
            publish_snapshot(self.conn, [new_item], self.repo, 2,
                             min_items=1, min_variant_rows=2)
        self.assertEqual(old_mods, self.conn.execute("SELECT mods FROM pob_unique").fetchone()[0])
        self.assertEqual(2, self.conn.execute("SELECT count(*) FROM pob_unique_variant").fetchone()[0])
        self.assertEqual(old_count, self.conn.execute("SELECT count(*) FROM ingest_run").fetchone()[0])

    def test_preflight_fails_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backups"
            with self.assertRaisesRegex(ValueError, "抽取有"):
                publish_snapshot(self.conn, [self.item], self.repo, 2,
                                 problems=["bad row"], min_items=1, min_variant_rows=2,
                                 backup_dir=backup_dir)
            self.assertFalse(backup_dir.exists())

    def test_tool_and_route_use_explicit_history(self) -> None:
        with patch("tools.impl.unique_variants.dbmod.get_conn", return_value=self.conn):
            result = unique_variant_history("Example Heart", 1, 2)
        self.assertTrue(result.data["found"])
        self.assertEqual(1, len(result.citations))
        self.assertEqual(self.repo.commit, result.data["left"]["source_commit"])
        from orchestrator.loop import _preferred_tool_route
        self.assertEqual("unique_variant_history", _preferred_tool_route("Example Heart 历史变体"))
        self.assertNotEqual("unique_variant_history", _preferred_tool_route("哪些唯一装备给生命"))

    def test_http_list_detail_and_compare(self) -> None:
        app = FastAPI()
        app.include_router(variant_router)
        with patch("api.unique_variants.dbmod.get_conn", return_value=self.conn):
            client = TestClient(app)
            catalog = client.get("/unique-variants", params={"name": "Example Heart"})
            self.assertEqual(200, catalog.status_code)
            self.assertEqual(2, len(catalog.json()["variants"]))
            detail = client.get("/unique-variants/detail", params={"name": "Example Heart", "index": 1})
            self.assertEqual("3.25.0 之前", detail.json()["variant"]["label_meaning"])
            diff = client.get("/unique-variants/compare", params={
                "name": "Example Heart", "left_index": 1, "right_index": 2,
            })
            self.assertEqual(200, diff.status_code)
            self.assertEqual("+500 to maximum Life", diff.json()["removed_mods"][0]["text"])
            bad = client.get("/unique-variants/compare", params={
                "name": "Example Heart", "left_index": 1, "right_index": 1,
            })
            self.assertEqual(422, bad.status_code)

    def test_mismatched_source_snapshot_is_rejected(self) -> None:
        self.conn.execute("UPDATE pob_unique SET url='https://example.invalid/new-version'")
        self.conn.commit()
        with self.assertRaisesRegex(VariantQueryError, "来源版本不一致"):
            list_variants(self.conn, "Example Heart")


if __name__ == "__main__":
    unittest.main()
