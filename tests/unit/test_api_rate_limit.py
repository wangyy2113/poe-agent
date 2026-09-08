"""普通 API 鉴权后的按 Key 令牌桶限流。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api.deps import _TokenBucketLimiter, _rate_limit_settings, require_user
from core.config import Config


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TokenBucketTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.limiter = _TokenBucketLimiter(self.clock)

    def test_burst_is_bounded_and_retry_after_uses_refill_rate(self) -> None:
        self.assertEqual(
            [self.limiter.retry_after("key-a", requests_per_minute=60, burst=2)
             for _ in range(2)],
            [0, 0],
        )
        self.assertEqual(
            self.limiter.retry_after("key-a", requests_per_minute=60, burst=2),
            1,
        )
        self.clock.now += 1
        self.assertEqual(
            self.limiter.retry_after("key-a", requests_per_minute=60, burst=2),
            0,
        )

    def test_each_key_has_an_independent_bucket(self) -> None:
        self.assertEqual(
            self.limiter.retry_after("key-a", requests_per_minute=1, burst=1), 0
        )
        self.assertGreater(
            self.limiter.retry_after("key-a", requests_per_minute=1, burst=1), 0
        )
        self.assertEqual(
            self.limiter.retry_after("key-b", requests_per_minute=1, burst=1), 0
        )


class RequireUserRateLimitTest(unittest.TestCase):
    def test_secret_configuration_failure_does_not_disable_authentication(self) -> None:
        with (
            patch("api.deps.get_secrets", side_effect=RuntimeError("broken")),
            self.assertLogs("api.deps", level="ERROR"),
            self.assertRaises(HTTPException) as cm,
        ):
            require_user("")
        self.assertEqual(cm.exception.status_code, 503)
        self.assertNotIn("broken", str(cm.exception.detail))

    def test_invalid_key_is_rejected_before_rate_limiter(self) -> None:
        with (
            patch("api.deps.user_keys", return_value=["valid-key"]),
            patch("api.deps._enforce_user_rate_limit") as enforce,
            self.assertRaises(HTTPException) as cm,
        ):
            require_user("Bearer invalid-key")
        self.assertEqual(cm.exception.status_code, 401)
        enforce.assert_not_called()

    def test_valid_keys_use_distinct_non_secret_bucket_ids(self) -> None:
        identities: list[str] = []
        with (
            patch("api.deps.user_keys", return_value=["alpha-key", "bravo-key"]),
            patch("api.deps._enforce_user_rate_limit", side_effect=identities.append),
        ):
            self.assertEqual(require_user("Bearer alpha-key"), "key")
            self.assertEqual(require_user("Bearer bravo-key"), "key")
        self.assertEqual(len(set(identities)), 2)
        self.assertNotIn("alpha-key", identities)
        self.assertNotIn("bravo-key", identities)

    def test_anonymous_mode_is_limited_and_429_has_retry_after(self) -> None:
        limiter = _TokenBucketLimiter(_Clock())
        config = Config(raw={"server": {"rate_limit": {
            "enabled": True, "requests_per_minute": 20, "burst": 1,
        }}})
        with (
            patch("api.deps.user_keys", return_value=[]),
            patch("api.deps.get_config", return_value=config),
            patch("api.deps._user_limiter", limiter),
        ):
            self.assertEqual(require_user(""), "anonymous")
            with self.assertRaises(HTTPException) as cm:
                require_user("")
        self.assertEqual(cm.exception.status_code, 429)
        self.assertEqual(cm.exception.headers["Retry-After"], "3")

    def test_rate_limit_can_be_disabled_explicitly(self) -> None:
        config = Config(raw={"server": {"rate_limit": {"enabled": False}}})
        with (
            patch("api.deps.get_config", return_value=config),
            patch("api.deps._user_limiter.retry_after") as retry,
            patch("api.deps.user_keys", return_value=[]),
        ):
            self.assertEqual(require_user(""), "anonymous")
        retry.assert_not_called()

    def test_invalid_settings_fall_back_to_safe_defaults(self) -> None:
        for rate, burst in (("bad", 10), (float("nan"), 10), (60, 0)):
            config = Config(raw={"server": {"rate_limit": {
                "enabled": True, "requests_per_minute": rate, "burst": burst,
            }}})
            with self.subTest(rate=rate, burst=burst), self.assertLogs(
                "api.deps", level="ERROR"
            ), patch("api.deps.get_config", return_value=config):
                self.assertEqual(_rate_limit_settings(), (60.0, 10))


if __name__ == "__main__":
    unittest.main()
