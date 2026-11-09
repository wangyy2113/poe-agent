"""Legacy ReRank interface, disabled under the single DeepSeek model policy."""

from __future__ import annotations

from dataclasses import dataclass

from core.model_policy import AUXILIARY_MODEL_REASON

MAX_DOCUMENTS = 64
MAX_DOCUMENT_CHARS = 4096
MAX_QUERY_CHARS = 500


class RerankError(RuntimeError):
    """The optional ranker cannot return a usable ranking."""


class RerankTimeout(RerankError):
    """A ranking operation exceeded its time budget."""


@dataclass(frozen=True)
class RerankClient:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 8.0

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        raise RerankError(AUXILIARY_MODEL_REASON)


def configured_rerank_client(
    requested: bool, *, timeout_seconds: float = 8.0
) -> tuple[RerankClient | None, str]:
    # Do not load legacy credentials, even if an old config requests this channel.
    return None, AUXILIARY_MODEL_REASON if requested else "配置关闭"
