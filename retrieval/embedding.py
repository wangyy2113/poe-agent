"""OpenAI-compatible remote embeddings with a strict response contract.

No local inference service is started.  Credentials are used only in request
headers and are never included in exceptions or logs.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import httpx


class EmbeddingError(RuntimeError):
    """The remote embedding channel cannot safely supply a vector."""


MAX_BATCH = 32
MAX_DIM = 8192
MAX_RETRIES = 2


def _endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise EmbeddingError("Embedding 服务地址必须是 HTTP(S) URL")
    if base.endswith("/v1/embeddings"):
        return base
    return base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings")


@dataclass(frozen=True)
class EmbeddingClient:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 20.0

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key.strip() or not self.model.strip():
            raise EmbeddingError("Embedding API Key 或模型未配置")
        if not 1 <= len(texts) <= MAX_BATCH or any(not item.strip() for item in texts):
            raise EmbeddingError(f"Embedding 输入必须是 1..{MAX_BATCH} 条非空文本")
        endpoint = _endpoint(self.base_url)
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = httpx.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "input": texts},
                    timeout=self.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES:
                    raise EmbeddingError(
                        f"Embedding 请求失败：{type(exc).__name__}"
                    ) from exc
                time.sleep(0.25 * (2 ** attempt))
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                time.sleep(0.25 * (2 ** attempt))
                continue
            break
        if response.status_code != 200:
            raise EmbeddingError(f"Embedding 服务返回 HTTP {response.status_code}")
        try:
            payload = response.json()
            data = payload["data"]
            if not isinstance(data, list) or len(data) != len(texts):
                raise ValueError("向量条数与请求不一致")
            by_index = {item["index"]: item["embedding"] for item in data}
            if set(by_index) != set(range(len(texts))):
                raise ValueError("向量序号缺失或重复")
            vectors = [by_index[index] for index in range(len(texts))]
            dimension = len(vectors[0])
            if not 1 <= dimension <= MAX_DIM:
                raise ValueError("向量维度超出支持范围")
            for vector in vectors:
                if len(vector) != dimension or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in vector
                ):
                    raise ValueError("向量维度不一致或含非有限数字")
            return [[float(value) for value in vector] for vector in vectors]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"Embedding 响应格式无效：{exc}") from exc
