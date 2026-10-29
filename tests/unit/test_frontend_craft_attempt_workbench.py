"""独立尝试预算网页入口、凭证清理与真实 JavaScript 行为回归。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendCraftAttemptWorkbenchTest(unittest.TestCase):
    def test_entry_materials_pricing_boundaries_and_key_cleanup_are_wired(self) -> None:
        page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for marker in (
            'id="craft-attempt-open"', 'id="craft-attempt-dialog"',
            'id="craft-attempt-form"', 'id="craft-attempt-material-rows"',
            'id="craft-attempt-price"', 'id="craft-attempt-result"',
            '每次完整重置，恰好进行一次加权选择',
            '不是混沌石、精华、化石或其它真实工艺的一次成功率',
            '<script src="craft-attempt-workbench.js"></script>',
        ):
            self.assertIn(marker, page)
        self.assertEqual(3, app.count('window.clearCraftAttemptWorkbench?.()'))

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_materials_unknown_cap_cost_and_stale_responses(self) -> None:
        subprocess.run(
            ["node", str(ROOT / "tests/frontend/craft_attempt_workbench.cjs")],
            cwd=ROOT, check=True, timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
