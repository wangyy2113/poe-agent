"""GGG 公告只能从已核验的官方首帖进入可引用索引。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import db as dbmod
from kb import patch_notes
from kb.fetcher import FetchResult
from kb.sync import validate_categories, snapshot_category
from main import create_app
from parsers.pob_build import encode_share_code
from tools.impl.patch_notes import patch_note_lookup


LISTING = """<html><title>Patch Notes - Forum - Path of Exile</title><table>
<tr><td class='thread_title'><span class='title'><a href='/forum/view-thread/4001117'>3.29.3b Patch Notes</a></span></td>
<td class='postBy'><span class='post_date'>Sep 05, 2026, 7:30:00 PM</span></td></tr>
<tr><td class='thread_title'><span class='title'><a href='/forum/view-thread/4001118'>Path of Exile 2 Patch Notes</a></span></td>
<td class='postBy'><span class='post_date'>Sep 06, 2026, 7:30:00 PM</span></td></tr>
</table></html>"""


def thread(*, staff: bool = True, title: str = "3.29.3b Patch Notes",
           date: str = "Sep 05, 2026, 7:30:00 PM") -> str:
    role = '<span class="roleLabel staffText">Grinding Gear Games</span>' if staff else "Player"
    cls = "staff" if staff else ""
    return f"""<html><div class='breadcrumb'>Forum / Patch Notes</div>
<h1 class='layoutBoxTitle'>{title}</h1>
<table class='forumPostListTable'>
<tr class='{cls}'><td><div class='posted-by'>{role}<span class='post_date'>{date}</span></div>
<div class='content'><p>3.29.3b Patch Notes</p><p>Changed Fireball damage.</p></div></td></tr>
<tr><td><div class='content'>Community reply containing false balance changes.</div></td></tr>
</table></html>"""


class PatchNotesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(dbmod.SCHEMA)
        self.conn.executescript(dbmod.FTS_SCHEMA)
        self.listing = patch_notes.parse_listing(LISTING)[0]

    def tearDown(self) -> None:
        self.conn.close()

    def test_listing_filters_poe2_and_rejects_identity_drift(self) -> None:
        self.assertEqual([4001117], [x.thread_id for x in patch_notes.parse_listing(LISTING)])
        with self.assertRaisesRegex(ValueError, "页面身份"):
            patch_notes.parse_listing(LISTING.replace("<title>Patch Notes", "<title>News"))
        with self.assertRaisesRegex(ValueError, "结构变化"):
            patch_notes.parse_listing(LISTING.replace("/forum/view-thread/4001117", "/forum/other/4001117"))

    def test_first_post_only_and_official_identity(self) -> None:
        note = patch_notes.parse_thread(thread(), self.listing, "2026-09-06T00:00:00Z")
        self.assertEqual("Changed Fireball damage.", note.body)
        self.assertNotIn("Community reply", note.body)
        for html in (thread(staff=False), thread(title="3.29.3c Patch Notes"),
                     thread(date="Sep 06, 2026, 7:30:00 PM")):
            with self.assertRaises(ValueError):
                patch_notes.parse_thread(html, self.listing, "2026-09-06T00:00:00Z")

    def test_final_url_and_batch_rejection(self) -> None:
        with self.assertRaisesRegex(ValueError, "重定向"):
            patch_notes._verify_final_url("https://evil.example/forum/view-thread/4001117", self.listing.url)
        with self.assertRaisesRegex(ValueError, "重定向"):
            patch_notes._verify_final_url("https://www.pathofexile.com/forum/view-thread/4001118", self.listing.url)
        note = patch_notes.parse_thread(thread(), self.listing, "2026-09-06T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "重复"):
            patch_notes.publish(self.conn, [note, note])
        self.assertEqual(0, self.conn.execute("SELECT count(*) FROM ggg_patch_note").fetchone()[0])

    def test_publish_is_idempotent_updates_fts_and_rolls_back(self) -> None:
        note = patch_notes.parse_thread(thread(), self.listing, "2026-09-06T00:00:00Z")
        self.assertEqual({"received": 1, "changed": 1, "chunks": 1},
                         patch_notes.publish(self.conn, [note]))
        self.assertEqual(0, patch_notes.publish(self.conn, [note])["changed"])
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH 'Fireball'"
        ).fetchone()[0])
        modified = patch_notes.parse_thread(
            thread().replace("Fireball", "Frostbolt"), self.listing, "2026-09-07T00:00:00Z"
        )
        self.assertEqual(1, patch_notes.publish(self.conn, [modified])["changed"])
        self.assertEqual(0, self.conn.execute(
            "SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH 'Fireball'"
        ).fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH 'Frostbolt'"
        ).fetchone()[0])
        self.assertEqual([4001117], [x["thread_id"] for x in patch_notes.search(
            self.conn, version="3.29.3b", query="Frostbolt")])
        before = self.conn.execute("SELECT content_hash FROM ggg_patch_note").fetchone()[0]
        with patch.object(patch_notes, "index_chunks", side_effect=RuntimeError("index failed")):
            with self.assertRaisesRegex(RuntimeError, "index failed"):
                patch_notes.publish(self.conn, [note])
        self.assertEqual(before, self.conn.execute(
            "SELECT content_hash FROM ggg_patch_note").fetchone()[0])

    def test_mentions_only_literal_words_in_published_lines(self) -> None:
        note = patch_notes.parse_thread(
            thread().replace("Changed Fireball damage.", "Arc damage changed.\nArchmage is unchanged."),
            self.listing, "2026-09-06T00:00:00Z",
        )
        patch_notes.publish(self.conn, [note])
        hits = patch_notes.mentions(self.conn, ["Arc"])
        self.assertEqual(1, len(hits))
        self.assertIn("Arc damage changed", hits[0]["source_excerpt"])
        self.assertEqual([], patch_notes.mentions(self.conn, ["Fireball"]))
        self.assertEqual([], patch_notes.mentions(self.conn, ["Arc"], version="3.28"))

    def test_tool_cites_only_matching_official_lines(self) -> None:
        note = patch_notes.parse_thread(thread(), self.listing, "2026-09-06")
        patch_notes.publish(self.conn, [note])
        with patch.object(dbmod, "get_conn", return_value=self.conn):
            listing = patch_note_lookup(version="3.29.3b")
            self.assertEqual(1, len(listing.data["items"]))
            self.assertEqual([], listing.citations)
            found = patch_note_lookup(term="Fireball")
            self.assertEqual(1, len(found.citations))
            self.assertEqual("GGG Patch Notes", found.citations[0]["source"])
            self.assertIn("Changed Fireball damage", found.citations[0]["text"])
            self.assertFalse(patch_note_lookup(term="Archmage").data["found"])

    def test_discover_rejects_redirect(self) -> None:
        class FetcherStub:
            def fetch(self, url: str, *, force: bool = False) -> FetchResult:
                return FetchResult(url=url, text=LISTING, content_hash="x", from_cache=False,
                                   fetched_at="2026-09-06", final_url="https://evil.example/")

        with self.assertRaisesRegex(ValueError, "重定向"):
            patch_notes.discover(FetcherStub())  # type: ignore[arg-type]

    def test_automatic_sync_category_records_success_and_diff(self) -> None:
        class FetcherStub:
            def __init__(self, **_kwargs) -> None:
                pass

            def fetch(self, url: str, *, force: bool = False) -> FetchResult:
                html = LISTING if "view-forum" in url else thread()
                return FetchResult(url=url, text=html, content_hash="x", from_cache=False,
                                   fetched_at="2026-09-06", final_url=url)

        self.assertEqual(("patch_notes",), validate_categories(["patch_notes"]))
        before = snapshot_category(self.conn, "patch_notes")
        with patch.object(patch_notes, "Fetcher", FetcherStub):
            self.assertEqual(0, patch_notes.sync_latest(self.conn))
        after = snapshot_category(self.conn, "patch_notes")
        self.assertEqual(0, before.total)
        self.assertEqual(1, after.total)
        self.assertEqual(1, self.conn.execute(
            "SELECT succeeded FROM ingest_run WHERE category='ggg_patch_notes'"
        ).fetchone()[0])

    def test_api_requires_user_and_returns_published_note(self) -> None:
        note = patch_notes.parse_thread(thread(), self.listing, "2026-09-06T00:00:00Z")
        patch_notes.publish(self.conn, [note])
        app = create_app()
        with patch.object(dbmod, "get_conn", return_value=self.conn), patch(
            "api.deps.user_keys", return_value=["test-key"]
        ):
            with TestClient(app) as client:
                denied = client.get("/poe_agent/patch-notes")
                self.assertIn(denied.status_code, (401, 403))
            from api.deps import require_user
            app.dependency_overrides[require_user] = lambda: "test"
            with TestClient(app) as client:
                result = client.get("/poe_agent/patch-notes", params={"query": "Fireball"})
                self.assertEqual(200, result.status_code)
                self.assertEqual(4001117, result.json()["items"][0]["thread_id"])
                self.assertEqual("Changed Fireball damage.", client.get(
                    "/poe_agent/patch-notes/4001117").json()["body"])
                self.assertEqual(404, client.get("/poe_agent/patch-notes/42").status_code)
                timeline = client.get("/poe_agent/patch-notes/timeline", params={"query": "Fireball"})
                self.assertEqual(200, timeline.status_code)
                self.assertEqual("mentions", timeline.json()["status"])
                self.assertEqual("unknown", client.get(
                    "/poe_agent/patch-notes/timeline", params={"query": "Archmage"}
                ).json()["status"])
                code = encode_share_code("""<PathOfBuilding>
                <Build className='Witch' ascendClassName='Elementalist' level='90' mainSocketGroup='1'/>
                <Skills><Skill slot='Weapon 1' enabled='true'>
                <Gem nameSpec='Fireball' skillId='Fireball' enabled='true'/>
                </Skill></Skills><Tree><Spec treeVersion='3_29'/></Tree>
                </PathOfBuilding>""")
                impact = client.post("/poe_agent/patch-notes/build-impact", json={"code": code})
                self.assertEqual(200, impact.status_code)
                self.assertEqual("3.29", impact.json()["build_version"])
                self.assertEqual("Fireball", impact.json()["items"][0]["term"])
                self.assertIn("不声称", impact.json()["boundary"])
                self.assertEqual(422, client.post(
                    "/poe_agent/patch-notes/build-impact", json={"code": "broken"}
                ).status_code)


if __name__ == "__main__":
    unittest.main()
