"""跨来源核对网页入口、凭证切换清理与真实 JS 行为回归。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendConflictLedgerTest(unittest.TestCase):
    def test_entry_filters_detail_and_key_changes_are_wired(self) -> None:
        page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for marker in (
            'id="conflict-open"', 'id="conflict-dialog"',
            '<option value="confirmed">已确认冲突</option>',
            '<option value="review">待核对差异</option>',
            'id="conflict-resolution"', 'id="conflict-detail"',
            '<script src="conflict-ledger.js"></script>',
        ):
            self.assertIn(marker, page)
        self.assertEqual(3, app.count('window.clearConflictLedger?.()'))

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_default_review_pagination_detail_and_stale_responses(self) -> None:
        subprocess.run(
            ["node", str(ROOT / "tests/frontend/conflict_ledger.cjs")],
            cwd=ROOT, check=True, timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
