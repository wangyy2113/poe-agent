"""poe.watch HTTP 适配器。

只封装已实测的公开端点 ``/leagues`` 与 ``/get``。业务缓存、名称解析和降级策略在
``economy.service``，避免 HTTP 层同时承担领域判断。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx


class PoeWatchError(RuntimeError):
    """上游不可用、超时或响应结构不符合契约。"""


JsonFetcher = Callable[[str, dict[str, str]], Any]


class PoeWatchClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.poe.watch",
        timeout_seconds: float = 15,
        requests_per_second: float = 0.5,
        fetch_json: JsonFetcher | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = max(float(timeout_seconds), 0.1)
        self.requests_per_second = max(float(requests_per_second), 0.0)
        self._fetch_json = fetch_json
        self._lock = threading.Lock()
        self._last_request = 0.0

    def leagues(self) -> list[dict[str, Any]]:
        payload = self._get("/leagues", {})
        if not isinstance(payload, list) or not all(isinstance(x, dict) for x in payload):
            raise PoeWatchError("poe.watch /leagues 返回的不是对象数组")
        return payload

    def prices(self, league: str, category: str) -> list[dict[str, Any]]:
        payload = self._get("/get", {"league": league, "category": category})
        if not isinstance(payload, list) or not all(isinstance(x, dict) for x in payload):
            raise PoeWatchError("poe.watch /get 返回的不是对象数组")
        return payload

    def _get(self, path: str, params: dict[str, str]) -> Any:
        self._pace()
        try:
            if self._fetch_json is not None:
                return self._fetch_json(path, params)
            with httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "poe1-agent/0.2 (economy price lookup)"},
            ) as client:
                response = client.get(path, params=params)
                response.raise_for_status()
                return response.json()
        except PoeWatchError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise PoeWatchError(f"poe.watch 请求失败：{type(exc).__name__}: {exc}") from exc

    def _pace(self) -> None:
        if self.requests_per_second <= 0:
            return
        interval = 1.0 / self.requests_per_second
        with self._lock:
            remaining = interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()
