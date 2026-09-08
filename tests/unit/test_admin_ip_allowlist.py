"""管理端真实连接 IP 白名单与管理令牌的组合边界。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from api.deps import _admin_allowed_networks, require_admin
from core.config import Config


def _request(host: str, *, forwarded_for: str = "") -> Request:
    headers = []
    if forwarded_for:
        headers.append((b"x-forwarded-for", forwarded_for.encode("ascii")))
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/admin/stats",
        "headers": headers,
        "client": (host, 12345),
        "server": ("127.0.0.1", 8787),
        "scheme": "http",
    })


class AdminIpAllowlistTest(unittest.TestCase):
    def _call(self, host: str, allowed, token: str = "admin-secret") -> None:  # noqa: ANN001
        config = Config(raw={"server": {"admin_allowed_ips": allowed}})
        secrets = SimpleNamespace(admin_token="admin-secret")
        with (
            patch("api.deps.get_config", return_value=config),
            patch("api.deps.get_secrets", return_value=secrets),
        ):
            require_admin(_request(host), f"Bearer {token}")

    def test_empty_allowlist_keeps_token_only_mode(self) -> None:
        self._call("203.0.113.7", [])

    def test_exact_ip_cidr_and_ipv4_mapped_address_are_allowed(self) -> None:
        cases = (
            ("127.0.0.1", ["127.0.0.1"]),
            ("10.2.3.4", ["10.0.0.0/8"]),
            ("2001:db8::8", ["2001:db8::/32"]),
            ("::ffff:127.0.0.1", ["127.0.0.0/8"]),
        )
        for host, allowed in cases:
            with self.subTest(host=host, allowed=allowed):
                self._call(host, allowed)

    def test_forwarded_for_is_not_trusted(self) -> None:
        config = Config(raw={"server": {"admin_allowed_ips": ["127.0.0.1"]}})
        secrets = SimpleNamespace(admin_token="admin-secret")
        request = _request("203.0.113.7", forwarded_for="127.0.0.1")
        with (
            patch("api.deps.get_config", return_value=config),
            patch("api.deps.get_secrets", return_value=secrets),
            self.assertLogs("api.deps", level="WARNING"),
            self.assertRaises(HTTPException) as cm,
        ):
            require_admin(request, "Bearer admin-secret")
        self.assertEqual(cm.exception.status_code, 403)

    def test_invalid_allowlist_fails_closed(self) -> None:
        config = Config(raw={"server": {"admin_allowed_ips": ["not-an-ip"]}})
        with (
            patch("api.deps.get_config", return_value=config),
            self.assertLogs("api.deps", level="ERROR"),
            self.assertRaises(HTTPException) as cm,
        ):
            _admin_allowed_networks()
        self.assertEqual(cm.exception.status_code, 503)

    def test_token_is_still_required_after_ip_check(self) -> None:
        with self.assertRaises(HTTPException) as cm:
            self._call("127.0.0.1", ["127.0.0.1"], token="wrong")
        self.assertEqual(cm.exception.status_code, 401)

    def test_admin_secret_configuration_failure_is_503(self) -> None:
        with (
            patch("api.deps.get_secrets", side_effect=RuntimeError("broken")),
            self.assertLogs("api.deps", level="ERROR"),
            self.assertRaises(HTTPException) as cm,
        ):
            require_admin(_request("127.0.0.1"), "Bearer admin-secret")
        self.assertEqual(cm.exception.status_code, 503)
        self.assertNotIn("broken", str(cm.exception.detail))


if __name__ == "__main__":
    unittest.main()
