"""Boss 风险网页入口、凭证清理与真实 JavaScript 交互回归。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendBossWorkbenchTest(unittest.TestCase):
    def test_audit_threshold_inputs_boundaries_and_key_cleanup_are_wired(self) -> None:
        page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for marker in (
            'id="boss-open"', 'id="boss-dialog"', 'id="boss-form"',
            'id="boss-pool"', 'id="boss-armour"', 'id="boss-uber"',
            'id="boss-summary"', 'id="boss-skills"', 'id="boss-detail"',
            '<script src="boss-workbench.js"></script>',
            '未知技能不会排成低风险',
        ):
            self.assertIn(marker, page)
        self.assertEqual(3, app.count('window.clearBossWorkbench?.()'))

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_explicit_inputs_unknowns_threshold_and_stale_responses(self) -> None:
        subprocess.run(
            ["node", str(ROOT / "tests/frontend/boss_workbench.cjs")],
            cwd=ROOT, check=True, timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
