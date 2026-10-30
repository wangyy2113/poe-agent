"""Wiki Boss 页面身份、攻略章节、原子索引与接口边界。"""

from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.deps import require_user
from core import db as dbmod
from kb import boss_guides
from kb.fetcher import FetchResult
from kb.sync import snapshot_category, validate_categories
from main import create_app
from tools.impl.boss_guides import boss_guide_lookup


HTML = """<html><h1 id='firstHeading'>The Maven</h1>
<div id='mw-content-text' lang='en'><div class='mw-parser-output'>
<table><tr><td>Infobox text never indexed</td></tr></table>
<h2>Skills</h2><h3>Maven Phase</h3>
<p>Move away from the marked arena zone when the projectile appears.</p>
<ul><li>Memory Game: follow the lit sequence of arena sectors.</li></ul>
<h2>Strategy</h2><p>Keep track of the degen pools and the arena position.</p>
<p>Observe the telegraph before moving to the next safe zone.</p>
<h2>Drops</h2><p>Fake drop rate 100 percent.</p>
<h2>Version history</h2><p>Old patch claim.</p>
</div></div><div id='comments'>Unverified player comment.</div></html>"""


class BossGuideTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(dbmod.SCHEMA)
        self.conn.executescript(dbmod.FTS_SCHEMA)
        self.guide = boss_guides.parse_page(HTML, "maven", "2026-10-30T00:00:00Z")

    def tearDown(self) -> None:
        self.conn.close()

    def test_selected_sections_only_and_identity(self) -> None:
        self.assertEqual(["Skills", "Strategy"], [x.heading for x in self.guide.sections])
        body = " ".join(line for section in self.guide.sections for line in section.lines)
        self.assertIn("Memory Game", body)
        for missing in ("Infobox", "Fake drop", "Old patch", "player comment"):
            self.assertNotIn(missing, body)
        with self.assertRaisesRegex(ValueError, "身份"):
            boss_guides.parse_page(HTML.replace("The Maven</h1>", "Other Boss</h1>"),
                                   "maven", "now")
        with self.assertRaisesRegex(ValueError, "身份"):
            boss_guides.parse_page(HTML.replace("lang='en'", "lang='other'"),
                                   "maven", "now")

    def test_publish_idempotent_changes_fts_and_rolls_back(self) -> None:
        self.assertEqual(1, boss_guides.publish(self.conn, [self.guide])["changed"])
        self.assertEqual(0, boss_guides.publish(self.conn, [self.guide])["changed"])
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH 'Memory'"
        ).fetchone()[0])
        changed = boss_guides.parse_page(
            HTML.replace("Memory Game", "Sequence Game"), "maven", "2026-10-31"
        )
        self.assertEqual(1, boss_guides.publish(self.conn, [changed])["changed"])
        self.assertEqual(0, self.conn.execute(
            "SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH 'Memory'"
        ).fetchone()[0])
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH 'Sequence'"
        ).fetchone()[0])
        before = boss_guides.detail(self.conn, "maven")["content_hash"]
        with patch.object(boss_guides, "index_chunks", side_effect=RuntimeError("fts down")):
            with self.assertRaisesRegex(RuntimeError, "fts down"):
                boss_guides.publish(self.conn, [self.guide])
        self.assertEqual(before, boss_guides.detail(self.conn, "maven")["content_hash"])
        with self.assertRaises(ValueError):
            boss_guides.publish(self.conn, [self.guide, self.guide])

    def test_sync_and_api_auth(self) -> None:
        class FetcherStub:
            def __init__(self, **_kwargs) -> None:
                pass

            def fetch(self, url: str, *, force: bool = False) -> FetchResult:
                return FetchResult(url=url, text=HTML, content_hash="x", from_cache=False,
                                   fetched_at="2026-10-30", final_url=url)

        self.assertEqual(("boss_guides",), validate_categories(["boss_guides"]))
        before = snapshot_category(self.conn, "boss_guides")
        with patch.object(boss_guides, "Fetcher", FetcherStub), patch.object(
            boss_guides, "fetch_guides", side_effect=lambda _fetcher, force=False: [self.guide]
        ):
            boss_guides.sync_latest(self.conn)
        self.assertEqual(0, before.total)
        self.assertEqual(1, snapshot_category(self.conn, "boss_guides").total)
        app = create_app()
        with patch.object(dbmod, "get_conn", return_value=self.conn), patch(
            "api.deps.user_keys", return_value=["test-key"]
        ):
            with TestClient(app) as client:
                self.assertEqual(401, client.get("/poe_agent/boss/guides").status_code)
            app.dependency_overrides[require_user] = lambda: "test"
            with TestClient(app) as client:
                result = client.get("/poe_agent/boss/guides/maven")
                self.assertEqual(200, result.status_code)
                self.assertEqual("PoE Wiki (P3)", result.json()["source"])
                self.assertEqual(2, len(result.json()["sections"]))
                self.assertEqual(404, client.get("/poe_agent/boss/guides/cortex").status_code)

    def test_redirect_host_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "重定向"):
            boss_guides._verify_url("https://evil.example/wiki/The_Maven",
                                    boss_guides.BASE + "The_Maven")

    def test_tool_requires_section_and_cites_only_selected_lines(self) -> None:
        boss_guides.publish(self.conn, [self.guide])
        with patch.object(dbmod, "get_conn", return_value=self.conn):
            listing = boss_guide_lookup("maven")
            self.assertTrue(listing.data["found"])
            self.assertEqual([], listing.citations)
            result = boss_guide_lookup("maven", section="Skills")
            self.assertTrue(result.data["found"])
            self.assertEqual(1, len(result.citations))
            self.assertIn("Memory Game", result.citations[0]["text"])
            self.assertNotIn("Fake drop", result.citations[0]["text"])
            self.assertEqual(3, result.citations[0]["source_priority"])
            self.assertFalse(boss_guide_lookup(
                "maven", section="Skills", offset=1000,
            ).data["found"])


if __name__ == "__main__":
    unittest.main()
