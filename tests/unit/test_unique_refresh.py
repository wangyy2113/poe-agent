"""Complete-source staging and atomic publication for unique items."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import db as dbmod
from core.backup import BackupError
from entities.models import ModLine
from entities.taxonomy import ModKind
from kb.fetcher import FetchResult
from kb.pipeline import _entity_hash, _upsert_entity, run_poedb_unique
from kb.render import to_chunks
from kb.sources import poedb_index, poedb_unique
from kb.unique_refresh import (
    UniqueRefreshError,
    apply_refresh,
    entity_fingerprint,
    prepare_refresh,
)
from retrieval import alias as alias_mod
from retrieval import fts as fts_mod
from scripts.refresh_unique_items import main as refresh_cli, proof, status


def card(mods: tuple[str, ...], *, name: str = "测试之戒") -> str:
    lines = "".join(f'<div class="explicitMod">{text}</div>' for text in mods)
    return (
        '<div class="col"><div class="d-flex"><div class="flex-grow-1">'
        '<div><a class="UniqueItem" href="/cn/Test_Ring">'
        f'<span class="uniqueName">{name}</span>'
        '<span class="uniqueTypeLine">潜能之戒</span></a></div>'
        '<div class="requirements">需求 等级 40</div>'
        f'{lines}</div></div></div>'
    )


def main_html(mods: tuple[str, ...] = ("+20 最大生命",)) -> str:
    return (f'<div id="Weapon传奇">{card(mods)}</div>'
            '<div id="Armour传奇"></div><div id="Other传奇"></div>')


def class_html(mods: tuple[str, ...] = ("+20 最大生命",)) -> str:
    return f'<div class="tab-pane" id="戒指传奇">{card(mods)}</div>'


def detail_html(mods: tuple[str, ...] = ("+20 最大生命", "不可改变"),
                *, name: str = "测试之戒") -> str:
    lines = "".join(f'<div class="explicitMod">{text}</div>' for text in mods)
    return (
        '<div class="newItemPopup UniquePopup"><div class="itemHeader">'
        f'<div class="itemName"><span class="lc">{name}</span></div>'
        '<div class="itemName typeLine"><span class="lc">潜能之戒</span></div>'
        '</div><div class="content"><div class="property">戒指</div>'
        '<div class="requirements">需求 等级 40</div>'
        f'{lines}<div>Test Ring</div></div></div>'
    )


class FakeSource:
    def __init__(self, *, main: str | None = None, detail: str | None = None,
                 class_page: str | None = None) -> None:
        self.pages = {
            poedb_unique.LIST_URL: main or main_html(),
            f"{poedb_index.BASE}/{poedb_index.MODIFIERS_HUB}": "<a href=\"/cn/Rings#ModifiersCalc\">戒指</a>",
            f"{poedb_index.BASE}/Rings": class_page or class_html(),
            poedb_unique.EN_LIST_URL: (
                '<div class="col"><a href="/us/Test_Ring">'
                '<span class="uniqueName">Test Ring</span>'
                '<span class="uniqueTypeLine">Unset Ring</span></a></div>'
            ),
            f"{poedb_index.BASE}/Test_Ring": detail or detail_html(),
        }
        self.calls: list[str] = []

    def fetch(self, url: str, *, force: bool = False) -> FetchResult:
        self.calls.append(url)
        if url not in self.pages:
            raise UniqueRefreshError(f"缺少页 {url}")
        body = self.pages[url]
        return FetchResult(
            url=url, text=body,
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
            from_cache=not force, fetched_at="2026-10-27T00:00:00+00:00",
            final_url=url,
        )


class UniqueRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(dbmod.SCHEMA)
        self.conn.executescript(dbmod.FTS_SCHEMA)
        self.addCleanup(self.conn.close)
        self.vocab = patch("kb.unique_refresh.vocab_mod.build_vocab", return_value=0)
        self.vocab.start()
        self.addCleanup(self.vocab.stop)
        self.parent_pages = patch.object(poedb_index, "PARENT_CLASS_PAGES", ())
        self.parent_pages.start()
        self.addCleanup(self.parent_pages.stop)

    def _prepare(self, source: FakeSource | None = None):  # noqa: ANN202
        return prepare_refresh(
            self.conn, source or FakeSource(), min_main_items=1,
            min_class_only_items=0, min_english_items=1,
        )

    def _old_entity(self) -> None:
        item = poedb_unique.extract_uniques(main_html())[0][0]
        item.mods.append(ModLine(kind=ModKind.EXPLICIT, text="不可改变"))
        item.content_hash = _entity_hash(item)
        _upsert_entity(self.conn, item)
        fts_mod.index_chunks(self.conn, to_chunks(item))
        alias_mod.index_aliases(self.conn, [item])
        self.conn.commit()

    def test_preflight_is_read_only_and_apply_keeps_detail_only_line(self) -> None:
        before = entity_fingerprint(self.conn)
        source = FakeSource()
        plan = self._prepare(source)
        self.assertEqual(before, entity_fingerprint(self.conn))
        self.assertEqual(plan.detail_added_lines, 1)
        self.assertEqual(plan.class_only_count, 0)
        self.assertEqual(plan.detail_pages, 1)
        self.assertEqual(len(plan.reviews), 0)
        self.assertEqual(len(source.calls), 5)

        result = apply_refresh(self.conn, plan)
        self.assertEqual(result["published_entities"], 1)
        self.assertEqual(result["indexed_chunks"], 1)
        row = self.conn.execute("SELECT attributes,item_class FROM entity").fetchone()
        self.assertIn("不可改变", row["attributes"])
        self.assertEqual(row["item_class"], "戒指")
        self.assertIn("不可改变", self.conn.execute("SELECT text FROM chunk").fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alias").fetchone()[0], 2)
        self.assertEqual(
            self.conn.execute("SELECT category FROM ingest_run ORDER BY run_id DESC LIMIT 1").fetchone()[0],
            "unique_refresh",
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM unique_refresh_page").fetchone()[0], 5
        )
        detail_proof = proof(self.conn, "Test_Ring")["proof"]
        self.assertEqual(detail_proof["role"], "detail")
        self.assertEqual(detail_proof["run_id"], result["run_id"])
        self.assertEqual(len(detail_proof["content_hash"]), 64)

    def test_mismatch_preserves_prior_detail_and_records_review(self) -> None:
        self._old_entity()
        before = entity_fingerprint(self.conn)
        source = FakeSource(main=main_html(("+30 最大生命",)))
        plan = self._prepare(source)
        self.assertEqual(len(plan.reviews), 1)
        self.assertIn("列表页有而详情页没有", plan.reviews[0].reason)
        apply_refresh(self.conn, plan)
        self.assertEqual(entity_fingerprint(self.conn), before)
        self.assertIn("不可改变", self.conn.execute("SELECT text FROM chunk").fetchone()[0])
        review = self.conn.execute("SELECT kind,note FROM source_conflict").fetchone()
        self.assertEqual(review["kind"], "unique_review")

    def test_missing_detail_aborts_before_writing(self) -> None:
        source = FakeSource()
        del source.pages[f"{poedb_index.BASE}/Test_Ring"]
        before = entity_fingerprint(self.conn)
        with self.assertRaisesRegex(UniqueRefreshError, "缺少页"):
            self._prepare(source)
        self.assertEqual(before, entity_fingerprint(self.conn))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM ingest_run").fetchone()[0], 0)

    def test_supplement_count_guard_rejects_empty_class_inventory(self) -> None:
        with self.assertRaisesRegex(UniqueRefreshError, "类别页独有 0"):
            prepare_refresh(
                self.conn, FakeSource(), min_main_items=1,
                min_class_only_items=1, min_english_items=1,
            )
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entity").fetchone()[0], 0)

    def test_concurrent_edit_rejects_plan(self) -> None:
        self._old_entity()
        plan = self._prepare()
        self.conn.execute("UPDATE entity SET name_zh='人工更新' WHERE slug='Test_Ring'")
        self.conn.commit()
        with self.assertRaisesRegex(UniqueRefreshError, "预检后已改变"):
            apply_refresh(self.conn, plan)
        self.assertEqual(self.conn.execute("SELECT name_zh FROM entity").fetchone()[0], "人工更新")

    def test_index_failure_rolls_back_entity_and_review(self) -> None:
        self._old_entity()
        plan = self._prepare(FakeSource(main=main_html(("+30 最大生命",))))
        before_entity = entity_fingerprint(self.conn)
        before_index = list(self.conn.execute("SELECT chunk_id,text FROM chunk"))
        with patch("kb.unique_refresh.fts_mod.index_chunks", side_effect=RuntimeError("写索引失败")):
            with self.assertRaisesRegex(RuntimeError, "写索引失败"):
                apply_refresh(self.conn, plan)
        self.assertEqual(entity_fingerprint(self.conn), before_entity)
        self.assertEqual(list(self.conn.execute("SELECT chunk_id,text FROM chunk")), before_index)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM source_conflict").fetchone()[0], 0)

    def test_legacy_list_import_refuses_to_erase_detail_merge(self) -> None:
        self.conn.execute(
            "INSERT INTO ingest_run(source,category,started_at,finished_at,succeeded) "
            "VALUES ('POEDB','unique_detail',datetime('now'),datetime('now'),1)"
        )
        self.conn.commit()
        cfg = SimpleNamespace(db_path=Path("unused"), get=lambda _key, default=0: default)
        with patch("kb.pipeline.get_config", return_value=cfg), patch(
            "kb.pipeline.dbmod.init_db", return_value=self.conn
        ):
            with self.assertRaisesRegex(poedb_unique.StructureError, "会覆盖详情词条"):
                run_poedb_unique()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM ingest_run").fetchone()[0], 1)

    def test_proof_requires_canonical_slug(self) -> None:
        with self.assertRaisesRegex(UniqueRefreshError, "不得包含路径分隔符"):
            proof(self.conn, "../other")

    def test_status_detects_card_and_source_ledger_drift(self) -> None:
        apply_refresh(self.conn, self._prepare())
        self.assertEqual(status(self.conn)["last_complete_refresh"]["integrity"], "valid")
        original = self.conn.execute("SELECT text FROM chunk LIMIT 1").fetchone()[0]
        self.conn.execute("UPDATE chunk SET text='被篡改' WHERE entity_id='unique_item:Test_Ring'")
        self.conn.commit()
        self.assertEqual(status(self.conn)["last_complete_refresh"]["integrity"], "drifted")
        self.conn.execute(
            "UPDATE chunk SET text=? WHERE entity_id='unique_item:Test_Ring'", (original,)
        )
        self.conn.execute(
            "UPDATE unique_refresh_page SET fetched_at='bad' WHERE role='detail'"
        )
        self.conn.commit()
        last = status(self.conn)["last_complete_refresh"]
        self.assertTrue(last["bundle_ok"])
        self.assertFalse(last["source_ledger_ok"])


class UniqueRefreshCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "poe.db"
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(dbmod.SCHEMA)
            conn.executescript(dbmod.FTS_SCHEMA)
        self.cfg = SimpleNamespace(
            db_path=self.path, cache_dir=Path(self.temp.name),
            backup_dir=Path(self.temp.name), logs_dir=Path(self.temp.name),
            get=lambda _key, default=0: default,
        )

    def test_cached_preview_does_not_fetch_or_backup(self) -> None:
        plan = SimpleNamespace(as_dict=lambda: {"status": "ready"})
        with patch("scripts.refresh_unique_items.get_config", return_value=self.cfg), patch(
            "scripts.refresh_unique_items.setup_logging"
        ), patch("scripts.refresh_unique_items.prepare_refresh", return_value=plan), patch(
            "scripts.refresh_unique_items._make_fetcher"
        ) as live, patch("scripts.refresh_unique_items.create_backup") as backup:
            with redirect_stdout(io.StringIO()) as output:
                code = refresh_cli(["--json", "preview"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "cached")
        live.assert_not_called()
        backup.assert_not_called()

    def test_backup_failure_stops_before_any_publication(self) -> None:
        with patch("scripts.refresh_unique_items.get_config", return_value=self.cfg), patch(
            "scripts.refresh_unique_items.setup_logging"
        ), patch("scripts.refresh_unique_items._make_fetcher", return_value=FakeSource()), patch(
            "scripts.refresh_unique_items.prepare_refresh", return_value=object()
        ), patch("scripts.refresh_unique_items.create_backup",
                 side_effect=BackupError("磁盘不可写")), patch(
            "scripts.refresh_unique_items.apply_refresh"
        ) as publish:
            with redirect_stdout(io.StringIO()) as output:
                code = refresh_cli(["apply"])
        self.assertEqual(code, 2)
        self.assertIn("磁盘不可写", output.getvalue())
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
