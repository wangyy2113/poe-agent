"""精华网页入口、凭证切换清理与真实 JS 行为回归。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendEssenceOperationTest(unittest.TestCase):
    def test_page_paste_card_and_key_cleanup_are_wired(self) -> None:
        page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for marker in (
            'id="essence-operation-open"', 'id="essence-operation-dialog"',
            'id="essence-operation-query"', 'id="essence-operation-name"',
            'id="essence-operation-catalog-status"', 'id="essence-operation-mode"',
            'id="essence-operation-manual"', 'id="essence-operation-item-wrap"',
            'id="essence-operation-result"',
            '<script src="essence-operation.js"></script>',
        ):
            self.assertIn(marker, page)
        self.assertIn('class="pc-essence">核对精华操作', app)
        self.assertIn('window.openEssenceOperation?.(text)', app)
        self.assertEqual(3, app.count('window.clearEssenceOperation?.()'))

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_catalog_requests_rendering_and_stale_responses(self) -> None:
        subprocess.run(
            ["node", str(ROOT / "tests/frontend/essence_operation.cjs")],
            cwd=ROOT, check=True, timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
