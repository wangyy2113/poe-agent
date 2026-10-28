"""历史变体浏览器的入口、安全渲染与显式选择契约。"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendUniqueHistoryContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "static/unique-history.js").read_text(encoding="utf-8")

    def test_browser_has_search_and_two_explicit_variant_selectors(self) -> None:
        for control in (
            'id="unique-history-open"', 'id="unique-history-dialog"',
            'id="history-query"', 'id="history-only"',
            'id="history-left" required', 'id="history-right"',
            'id="history-compare-form"',
        ):
            self.assertIn(control, self.page)
        self.assertIn('<script src="unique-history.js"></script>', self.page)
        self.assertIn("select.value = ''", self.script)
        self.assertIn("if (right && left === right)", self.script)

    def test_scope_and_version_boundary_are_visible(self) -> None:
        self.assertIn("Pre X 表示 X 之前", self.page)
        self.assertIn("历史变体库尚未导入", self.script)
        self.assertIn("底材：${variant.base_type_en || '未知", self.script)
        self.assertIn("data.base_changed === null", self.script)

    def test_external_content_never_becomes_html(self) -> None:
        self.assertIn("node.textContent = String(value)", self.script)
        self.assertNotIn("innerHTML", self.script)
        self.assertIn("if (url.protocol !== 'https:') return", self.script)
        self.assertIn("link.rel = 'noopener noreferrer'", self.script)

    def test_stale_requests_cannot_repaint_closed_or_changed_dialog(self) -> None:
        self.assertIn("historySearchEpoch += 1", self.script)
        self.assertIn("historyDetailEpoch += 1", self.script)
        self.assertGreaterEqual(self.script.count("epoch !== historySearchEpoch"), 2)
        self.assertGreaterEqual(self.script.count("epoch !== historyDetailEpoch"), 4)
        self.assertIn("!historyUi.dialog.open", self.script)


if __name__ == "__main__":
    unittest.main()
