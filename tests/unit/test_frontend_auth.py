"""Web API Key 会话鉴权的静态契约。"""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FrontendAuthContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page = (ROOT / "static/index.html").read_text(encoding="utf-8")
        cls.app = (ROOT / "static/app.js").read_text(encoding="utf-8")

    def test_key_input_is_a_password_dialog_not_a_prompt(self) -> None:
        self.assertIn('id="auth-dialog"', self.page)
        self.assertIn('id="auth-input" type="password"', self.page)
        self.assertIn('autocomplete="off"', self.page)
        self.assertNotIn("prompt('输入 API Key", self.app)

    def test_key_is_tab_scoped_and_never_put_in_local_storage(self) -> None:
        self.assertIn("sessionStorage.setItem(AUTH_STORE, key)", self.app)
        self.assertIn("sessionStorage.removeItem(AUTH_STORE)", self.app)
        self.assertNotIn("localStorage.setItem(AUTH_STORE", self.app)

    def test_authenticated_conversation_cache_is_tab_scoped(self) -> None:
        self.assertIn(
            "return storedApiKey() ? sessionStorage : localStorage;", self.app
        )
        self.assertIn("sessionStorage.removeItem(CONVERSATION_STORE)", self.app)
        self.assertIn("localStorage.removeItem(CONVERSATION_STORE)", self.app)

    def test_all_http_calls_flow_through_one_authorization_wrapper(self) -> None:
        self.assertEqual(self.app.count("fetch("), 1)
        self.assertGreaterEqual(self.app.count("apiFetch("), 10)
        self.assertIn("headers.set('Authorization', `Bearer ${key}`)", self.app)

    def test_key_is_validated_before_it_is_stored(self) -> None:
        validate = self.app.index("requestApi('/conversations?limit=1', {}, key)")
        persist = self.app.index("sessionStorage.setItem(AUTH_STORE, key)")
        self.assertLess(validate, persist)
        self.assertIn("if (response.status === 401)", self.app[validate:persist])

    def test_unauthorized_response_clears_rejected_key_and_opens_dialog(self) -> None:
        start = self.app.index("async function apiFetch")
        end = self.app.index("function clearConversationUi", start)
        block = self.app[start:end]
        self.assertIn("response.status === 401", block)
        self.assertIn("sessionStorage.removeItem(AUTH_STORE)", block)
        self.assertIn("showAuthDialog", block)

    def test_logout_clears_key_and_conversation_cache(self) -> None:
        start = self.app.index("function clearApiKey")
        end = self.app.index("/* ── Markdown", start)
        block = self.app[start:end]
        self.assertIn("sessionStorage.removeItem(AUTH_STORE)", block)
        self.assertIn("clearConversationUi()", block)
        self.assertIn("authDialog.addEventListener('close'", self.app)


if __name__ == "__main__":
    unittest.main()
