"""经济分析网页入口：接口接线、鉴权清理和真实 JS 行为回归。"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendEconomyWorkbenchTest(unittest.TestCase):
    def test_four_workflows_are_discoverable_and_key_changes_clear_state(self) -> None:
        page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        app = (ROOT / "static/app.js").read_text(encoding="utf-8")
        for marker in (
            'id="economy-open"', 'id="economy-dialog"',
            '<option value="price">当前价格</option>',
            '<option value="trend">历史趋势</option>',
            '<option value="materials">材料清单成本</option>',
            '<option value="flip">双边价差</option>',
            '<script src="economy-workbench.js"></script>',
        ):
            self.assertIn(marker, page)
        self.assertEqual(3, app.count('window.clearEconomyWorkbench?.()'))

    @unittest.skipUnless(shutil.which("node"), "Node.js unavailable")
    def test_requests_boundaries_safe_rendering_and_stale_results(self) -> None:
        subprocess.run(
            ["node", str(ROOT / "tests/frontend/economy_workbench.cjs")],
            cwd=ROOT, check=True, timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
