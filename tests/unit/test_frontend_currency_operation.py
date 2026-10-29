"""基础通货网页入口接线与真实 JS 行为回归。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendCurrencyOperationTest(unittest.TestCase):
    def test_page_paste_card_and_key_cleanup_are_wired(self) -> None:
        page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for marker in (
            'id="currency-operation-open"', 'id="currency-operation-dialog"',
            'id="currency-operation-form"', 'id="currency-operation-mode"',
            'id="currency-operation-manual"', 'id="currency-operation-item-wrap"',
            'id="currency-operation-guard"', 'id="currency-operation-result"',
            '<script src="currency-operation.js"></script>',
        ):
            self.assertIn(marker, page)
        self.assertIn('class="pc-craft">核对通货操作', app)
        self.assertIn('window.openCurrencyOperation?.(text)', app)
        self.assertEqual(3, app.count('window.clearCurrencyOperation?.()'))

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_request_rendering_and_stale_response_flow(self) -> None:
        subprocess.run(
            ["node", str(ROOT / "tests/frontend/currency_operation.cjs")],
            cwd=ROOT, check=True, timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
