"""Web 长期记忆管理器的静态安全契约。"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendMemoryContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        cls.app = (ROOT / "static/app.js").read_text(encoding="utf-8")

    def test_manager_exposes_explicit_crud_form(self) -> None:
        self.assertIn('id="manage-memories"', self.page)
        self.assertIn('id="memory-dialog"', self.page)
        self.assertIn('id="memory-category"', self.page)
        self.assertIn('id="memory-label" maxlength="80"', self.page)
        self.assertIn('id="memory-content" required maxlength="500"', self.page)
        self.assertIn('id="memory-active" type="checkbox" checked', self.page)

    def test_list_includes_inactive_memories_for_reactivation(self) -> None:
        self.assertIn(
            "apiFetch('/memories?include_inactive=true&limit=100')", self.app
        )
        self.assertIn("item.active ? '停用' : '启用'", self.app)
        self.assertIn("JSON.stringify({ active: !item.active })", self.app)

    def test_create_and_edit_share_validated_form(self) -> None:
        self.assertIn("memoryId ? 'PATCH' : 'POST'", self.app)
        self.assertIn("memoryId ? `/memories/${encodeURIComponent(memoryId)}`", self.app)
        self.assertIn("const content = el.memoryContent.value.trim()", self.app)
        self.assertIn("if (!content)", self.app)

    def test_delete_is_confirmed_and_uses_api_wrapper(self) -> None:
        self.assertIn("window.confirm('永久删除这条长期记忆？')", self.app)
        self.assertIn("method: deleting ? 'DELETE' : 'PATCH'", self.app)
        self.assertIn("apiFetch(`/memories/${encodeURIComponent(memoryId)}`", self.app)

    def test_user_content_is_rendered_as_text_not_html(self) -> None:
        start = self.app.index("function renderMemories()")
        end = self.app.index("async function loadMemories(", start)
        render = self.app[start:end]
        self.assertIn("label.textContent = item.label", render)
        self.assertIn("content.textContent = item.content", render)
        self.assertNotIn("innerHTML", render)

    def test_authentication_changes_clear_private_memory_ui(self) -> None:
        self.assertGreaterEqual(self.app.count("clearMemoryUi();"), 3)
        clear_start = self.app.index("function clearMemoryUi()")
        clear_end = self.app.index("function setMemoryBusy", clear_start)
        clear = self.app[clear_start:clear_end]
        self.assertIn("memoryItems = []", clear)
        self.assertIn("memoryList.replaceChildren()", clear)
        self.assertIn("memoryDialog.close()", clear)

    def test_stale_responses_cannot_restore_another_identity_data(self) -> None:
        self.assertIn("let memoryEpoch = 0", self.app)
        self.assertGreaterEqual(self.app.count("epoch !== memoryEpoch"), 3)
        self.assertIn("memoryEpoch += 1", self.app)

    def test_copy_states_memory_is_not_game_evidence(self) -> None:
        self.assertIn("不会作为游戏事实证据", self.page)
        self.assertIn("只会保存你在这里主动提交的内容", self.app)


if __name__ == "__main__":
    unittest.main()
