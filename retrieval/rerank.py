"""Optional remote ReRank adapter with a complete, bounded response contract.

The service only changes candidate order.  It cannot create evidence, change
source metadata, or decide whether a result is sufficient to answer.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class RerankError(RuntimeError):
    """The optional remote ranker did not return a usable complete ranking."""


class RerankTimeout(RerankError):
    """The complete request, including retries, exceeded its time budget."""


MAX_DOCUMENTS = 64
MAX_DOCUMENT_CHARS = 4096
MAX_QUERY_CHARS = 500
MAX_RETRIES = 2


def _endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    try:
        parsed = urlsplit(base)
    except ValueError:
        raise RerankError("ReRank 服务地址必须是无凭证的 HTTP(S) URL") from None
    if (
        parsed.scheme not in ("http", "https") or not parsed.netloc
        or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise RerankError("ReRank 服务地址必须是无凭证的 HTTP(S) URL")
    if base.endswith(("/v1/rerank", "/v2/rerank", "/rerank")):
        return base
    return base + ("/rerank" if base.endswith("/v1") else "/v1/rerank")


@dataclass(frozen=True)
class RerankClient:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 8.0

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Return one score per input document, in input order."""
        if not self.api_key.strip() or not self.model.strip():
            raise RerankError("ReRank API Key 或模型未配置")
        if not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise RerankError("ReRank 查询为空或过长")
        if not 1 <= len(documents) <= MAX_DOCUMENTS or any(
            not item.strip() or len(item) > MAX_DOCUMENT_CHARS for item in documents
        ):
            raise RerankError("ReRank 文档数量或长度超出上限")
        endpoint = _endpoint(self.base_url)
        if self.timeout_seconds <= 0:
            raise RerankTimeout("ReRank 请求总时限已耗尽")
        deadline = time.monotonic() + self.timeout_seconds
        for attempt in range(MAX_RETRIES + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RerankTimeout("ReRank 请求总时限已耗尽")
            try:
                response = httpx.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model, "query": query,
                        "documents": documents, "top_n": len(documents),
                    },
                    timeout=remaining,
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    error_type = RerankTimeout if isinstance(exc, httpx.TimeoutException) else RerankError
                    raise error_type(
                        f"ReRank 请求失败：{type(exc).__name__}"
                    ) from None
                time.sleep(min(0.25 * (2 ** attempt), max(0, deadline - time.monotonic())))
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                time.sleep(min(0.25 * (2 ** attempt), max(0, deadline - time.monotonic())))
                continue
            break
        if time.monotonic() >= deadline:
            raise RerankTimeout("ReRank 请求总时限已耗尽")
        if response.status_code != 200:
            raise RerankError(f"ReRank 服务返回 HTTP {response.status_code}")
        try:
            payload = response.json()
            results = payload["results"]
            if not isinstance(results, list) or len(results) != len(documents):
                raise ValueError("重排条数与请求不一致")
            scores: dict[int, float] = {}
            for item in results:
                index, value = item["index"], item["relevance_score"]
                if (
                    isinstance(index, bool) or not isinstance(index, int)
                    or index < 0 or index >= len(documents) or index in scores
                    or isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1
                ):
                    raise ValueError("重排序号或相关度无效")
                scores[index] = float(value)
            return [scores[index] for index in range(len(documents))]
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankError(f"ReRank 响应格式无效：{exc}") from None


def configured_rerank_client(
    requested: bool, *, timeout_seconds: float = 8.0
) -> tuple[RerankClient | None, str]:
    """Resolve ReRank credentials only when the optional channel is requested."""
    if not requested:
        return None, "配置关闭"
    try:
        from core.config import get_secrets  # noqa: PLC0415

        secrets = get_secrets()
        base = secrets.rerank_base_url.strip()
        key = secrets.rerank_api_key.strip()
        model = secrets.rerank_model.strip()
    except Exception:  # noqa: BLE001  可选通道的配置错误不得阻断词法检索
        return None, "ReRank 配置读取失败"
    if not (base and key and model):
        return None, "ReRank 服务地址、Key 或模型未配置"
    try:
        _endpoint(base)
    except RerankError as exc:
        return None, str(exc)
    return RerankClient(base, key, model, timeout_seconds), "已配置；远程可用性尚未验证"
