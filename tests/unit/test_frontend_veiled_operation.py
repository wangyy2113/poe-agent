"""加密通货网页接线、凭证清理与真实 JS 行为回归。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendVeiledOperationTest(unittest.TestCase):
    def test_page_paste_card_and_key_cleanup_are_wired(self) -> None:
        page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for marker in (
            'id="veiled-operation-open"', 'id="veiled-operation-dialog"',
            'id="veiled-operation-name"', 'id="veiled-operation-catalog-status"',
            'id="veiled-operation-mode"', 'id="veiled-operation-rarity"',
            'id="veiled-operation-class"', 'id="veiled-operation-prefix"',
            'id="veiled-operation-suffix"', 'id="veiled-operation-item-wrap"',
            'id="veiled-operation-result"',
            '<script src="veiled-operation.js"></script>',
        ):
            self.assertIn(marker, page)
        self.assertIn('class="pc-veiled">核对加密通货', app)
        self.assertIn('window.openVeiledOperation?.(text)', app)
        self.assertEqual(3, app.count('window.clearVeiledOperation?.()'))

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_catalog_modes_pagination_rendering_and_stale_responses(self) -> None:
        subprocess.run(
            ["node", str(ROOT / "tests/frontend/veiled_operation.cjs")],
            cwd=ROOT, check=True, timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
