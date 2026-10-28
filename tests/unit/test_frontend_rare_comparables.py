"""稀有装备网页入口的接线，以及浏览器脚本的实际请求/DOM 行为。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendRareComparablesTest(unittest.TestCase):
    def test_page_and_paste_confirmation_expose_the_same_dialog(self) -> None:
        page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for control in (
            'id="rare-open"', 'id="rare-dialog"', 'id="rare-form"',
            'id="rare-text"', 'id="rare-league"', 'id="rare-quality"',
            'id="rare-sockets"', 'id="rare-submit"', 'id="rare-status"',
        ):
            self.assertIn(control, page)
        self.assertIn('<script src="rare-comparables.js"></script>', page)
        self.assertIn('查询可比挂单', app)
        self.assertIn('window.openRareComparables?.(text)', app)
        self.assertIn('window.clearRareComparables?.()', app)

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_request_rendering_and_stale_response_flow(self) -> None:
        subprocess.run(
            ["node", str(ROOT / "tests/frontend/rare_comparables.cjs")],
            cwd=ROOT, check=True, timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
