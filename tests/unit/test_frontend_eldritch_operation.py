"""古灵通货网页入口、凭证切换清理与真实 JS 行为回归。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendEldritchOperationTest(unittest.TestCase):
    def test_page_paste_card_and_key_cleanup_are_wired(self) -> None:
        page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for marker in (
            'id="eldritch-operation-open"', 'id="eldritch-operation-dialog"',
            'id="eldritch-operation-name"', 'id="eldritch-operation-catalog-status"',
            'id="eldritch-operation-mode"', 'id="eldritch-operation-dominance"',
            'id="eldritch-operation-searing"', 'id="eldritch-operation-eater"',
            'id="eldritch-operation-item-wrap"', 'id="eldritch-operation-result"',
            '<script src="eldritch-operation.js"></script>',
        ):
            self.assertIn(marker, page)
        self.assertIn('class="pc-eldritch">核对古灵操作', app)
        self.assertIn('window.openEldritchOperation?.(text)', app)
        self.assertEqual(3, app.count('window.clearEldritchOperation?.()'))

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_catalog_modes_rendering_and_stale_responses(self) -> None:
        subprocess.run(
            ["node", str(ROOT / "tests/frontend/eldritch_operation.cjs")],
            cwd=ROOT, check=True, timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
