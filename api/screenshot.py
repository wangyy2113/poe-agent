"""截图上传与视觉转录入口（需求 6.5 / 8.1）。

这里只返回待确认的 OCR 结果，不直接分析。用户确认后，装备文本进入 `/item`，
Build 可见字段进入 `/build/screenshot`；因此视觉模型的误识别不会静默成为分析输入。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import require_user
from core.config import get_config
from core.llm import LLMError, get_llm_client
from parsers.screenshot import (
    MAX_DATA_URL_CHARS,
    ScreenshotError,
    recognize_screenshot,
    validate_image_data_url,
)


logger = logging.getLogger(__name__)
router = APIRouter(tags=["截图"])


class ScreenshotRequest(BaseModel):
    data_url: str = Field(min_length=1, max_length=MAX_DATA_URL_CHARS)
    filename: str = Field(default="", max_length=255)


class ScreenshotBuildOut(BaseModel):
    level: int | None = None
    class_name: str = ""
    ascend_class_name: str = ""
    main_skill: str = ""
    player_stats: dict[str, float] = {}
    player_stat_text: dict[str, str] = {}
    enemy_is_boss: str = ""


class ScreenshotResponse(BaseModel):
    kind: str
    transcript: str
    build: ScreenshotBuildOut
    uncertain: list[str] = []
    model: str
    requires_confirmation: bool = True
    storage_note: str = "图片仅在本次请求中转给视觉模型，不写磁盘、不进数据库。"


def _raise_screenshot_error(exc: ScreenshotError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"error": exc.code, "reason": exc.reason},
    ) from exc


@router.post("/screenshot/recognize", response_model=ScreenshotResponse)
def screenshot_recognize(
    req: ScreenshotRequest,
    caller: str = Depends(require_user),
) -> ScreenshotResponse:
    try:
        media_type, raw = validate_image_data_url(req.data_url)
        model = str(get_config().get("llm.vision_model", "") or "").strip()
        if not model:
            raise ScreenshotError(
                "vision_model_not_configured",
                "未配置 llm.vision_model，截图识别不可用",
                status_code=503,
            )
        result = recognize_screenshot(
            req.data_url,
            client=get_llm_client(),
            model=model,
        )
    except ScreenshotError as exc:
        _raise_screenshot_error(exc)
    except LLMError as exc:
        # 不返回异常正文；某些兼容网关会把请求片段放进错误消息，图片不应进日志/API。
        logger.warning("截图识别客户端不可用 caller=%s error=%s", caller, type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "vision_client_unavailable",
                "reason": "视觉模型客户端不可用；请检查 LLM 凭证与网关配置",
            },
        ) from exc

    logger.info(
        "截图转录完成 caller=%s 文件=%s MIME=%s 字节=%d 类型=%s 模型=%s",
        caller,
        req.filename.replace("\r", " ").replace("\n", " ")[:80],
        media_type,
        len(raw),
        result.kind,
        result.model,
    )
    return ScreenshotResponse(
        kind=result.kind,
        transcript=result.transcript,
        build=ScreenshotBuildOut(**result.build.__dict__),
        uncertain=list(result.uncertain),
        model=result.model,
    )
