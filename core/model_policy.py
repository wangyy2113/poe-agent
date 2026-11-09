"""One upstream model for every generation path, including image transcription."""

from __future__ import annotations

DEEPSEEK_MODEL = "deepseek-v4-1-flash-1017"
AUXILIARY_MODEL_REASON = "DeepSeek 单模型模式未启用 Embedding / ReRank；使用 BM25、实体精确匹配与关键词检索"


class ModelPolicyError(ValueError):
    """An unsupported model was requested; never include raw configuration values."""


def require_model(model: str | None = None) -> str:
    """Accept only the pinned model or its single, provider-neutral role alias."""
    if model is None or (isinstance(model, str) and model.strip() in (DEEPSEEK_MODEL, "deepseek")):
        return DEEPSEEK_MODEL
    raise ModelPolicyError(
        f"只允许模型 {DEEPSEEK_MODEL}；请移除旧模型、别名或逐 Key 模型配置"
    )


def capabilities() -> dict[str, bool]:
    """Capabilities enabled by this application, not a live gateway health probe."""
    return {"chat": True, "stream": True, "tools": True, "vision": True,
            "embedding": False, "rerank": False}
