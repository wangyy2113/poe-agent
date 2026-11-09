"""Compatibility types for stored vector indexes; remote embedding is disabled.

The pinned DeepSeek chat model has no verified embedding contract. Existing
indexes remain readable, but this adapter can never send text to another model.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.model_policy import AUXILIARY_MODEL_REASON

MAX_BATCH = 32
MAX_DIM = 8192


class EmbeddingError(RuntimeError):
    """The embedding channel cannot safely supply a vector."""


class EmbeddingTimeout(EmbeddingError):
    """A vector operation exceeded its time budget."""


@dataclass(frozen=True)
class EmbeddingClient:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 20.0

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError(AUXILIARY_MODEL_REASON)
