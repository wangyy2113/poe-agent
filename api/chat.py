"""`/chat` 与 `/chat/stream`：问答接口（任务 3.8）。

流式的粒度是**句**而不是字。按需求 5.1 与设计 11.1，出口的断言绑定校验必须
按句缓冲、校验通过才下发——逐字推送等于把未校验内容先给了用户，之后没法撤回。
前端对已通过的句子做打字机效果，观感接近逐字流，但不会显示未校验内容。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.deps import require_user
from core import db as dbmod
from orchestrator.loop import run_turn, stream_turn

logger = logging.getLogger(__name__)

router = APIRouter(tags=["问答"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class CitationOut(BaseModel):
    label: str
    source: str
    title: str
    url: str
    priority: int


class ChatResponse(BaseModel):
    trace_id: str
    conversation_id: str
    #: 已通过出口绑定校验的答案。未通过的句子已被替换为拦截说明。
    answer: str
    #: 答案 + 引用块，可直接展示
    text: str
    #: True 表示知识库依据不足，答案是缺据声明而非回答
    evidence_gap: bool
    #: 被拦截的无据事实句数量。> 0 说明模型试图输出无据内容，已被闸门挡下
    blocked_claims: int
    citations: list[CitationOut]
    #: 实际使用的模型。网关背后挂着多个模型，多 Key 故障转移时可能切到别的模型，
    #: 而不同模型的推理 token 行为不同——不返回这个字段，排查时会误判。
    model: str
    tokens: dict[str, int]
    elapsed_ms: int


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, caller: str = Depends(require_user)) -> ChatResponse:
    conversation_id = req.conversation_id or uuid.uuid4().hex
    _ensure_conversation(conversation_id, req.message)

    result = run_turn(req.message, conversation_id=conversation_id)

    _save_message(conversation_id, "user", req.message)
    _save_message(conversation_id, "assistant", result.full_text())

    logger.info(
        "chat 完成 caller=%s trace=%s 缺据=%s 拦截=%d token=%d 耗时=%dms",
        caller, result.trace_id, result.evidence_gap, result.blocked,
        result.tokens.get("total", 0), result.elapsed_ms,
    )

    return ChatResponse(
        trace_id=result.trace_id,
        conversation_id=conversation_id,
        answer=result.answer,
        text=result.full_text(),
        evidence_gap=result.evidence_gap,
        blocked_claims=result.blocked,
        citations=[
            CitationOut(
                label=c.label, source=c.source, title=c.title,
                url=c.url, priority=c.priority,
            )
            for c in result.citations
        ],
        model=result.model,
        tokens=result.tokens,
        elapsed_ms=result.elapsed_ms,
    )


@router.post("/chat/stream")
def chat_stream(req: ChatRequest, caller: str = Depends(require_user)) -> StreamingResponse:
    """SSE 流式问答。

    每帧是一行 `data: {json}`，JSON 里的 `type` 区分事件：

        meta       首帧，带 trace_id / conversation_id
        thinking   模型进入推理阶段（**不含推理正文**，那是安全要求）
        tool       正在调用某个工具
        sentence   一句**已通过出口校验**的正文
        blocked    一句被拦截的无据断言（下发的是拦截说明，不是原文）
        gap        依据不足的缺据声明（整条，此时不会有 sentence）
        citations  引用列表
        note       降级提示
        error      生成失败
        done       末帧，带统计

    用 `data:` + 内嵌 type 而不是 SSE 的 `event:` 字段，是为了让前端用
    `fetch` + `ReadableStream` 一套解析逻辑处理完，不必区分 event 名。
    """
    conversation_id = req.conversation_id or uuid.uuid4().hex
    _ensure_conversation(conversation_id, req.message)
    _save_message(conversation_id, "user", req.message)

    def gen() -> Iterator[str]:
        parts: list[str] = []
        citations: list[dict[str, Any]] = []
        try:
            for event in stream_turn(req.message, conversation_id=conversation_id):
                if event["type"] in ("sentence", "blocked", "gap"):
                    parts.append(event.get("text", ""))
                elif event["type"] == "citations":
                    citations = event.get("items", [])
                elif event["type"] == "done":
                    logger.info(
                        "chat/stream 完成 caller=%s trace=%s 缺据=%s 拦截=%d 耗时=%dms",
                        caller, event.get("trace_id"), event.get("evidence_gap"),
                        event.get("blocked_claims", 0), event.get("elapsed_ms", 0),
                    )
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001  流已开始，只能以事件形式报错
            logger.exception("流式问答异常")
            payload = {"type": "error", "message": type(exc).__name__}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            # 落盘完整回答，供会话历史回看。引用附在末尾，与非流式一致。
            text = "".join(parts)
            if citations:
                lines = "\n".join(
                    f"[{c['label']}] 来源：{c['source']} → {c['title']}\n    {c['url']}"
                    for c in citations
                )
                text += f"\n\n---\n**引用**\n{lines}"
            if text.strip():
                _save_message(conversation_id, "assistant", text)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反向代理默认会缓冲响应，那样流式就退化成一次性返回
            "X-Accel-Buffering": "no",
        },
    )


def _ensure_conversation(conversation_id: str, first_message: str) -> None:
    try:
        conn = dbmod.get_conn()
        conn.execute(
            "INSERT INTO conversation(conversation_id, title, created_at, updated_at) "
            "VALUES (?,?,datetime('now'),datetime('now')) "
            "ON CONFLICT(conversation_id) DO UPDATE SET updated_at=datetime('now')",
            (conversation_id, first_message[:60]),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001  会话记录失败不该让问答失败
        logger.warning("写会话失败：%s", exc)


def _save_message(conversation_id: str, role: str, content: str) -> None:
    try:
        conn = dbmod.get_conn()
        conn.execute(
            "INSERT INTO message(message_id, conversation_id, role, content, created_at) "
            "VALUES (?,?,?,?,datetime('now'))",
            (uuid.uuid4().hex, conversation_id, role, content),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("写消息失败：%s", exc)


@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: str) -> dict[str, Any]:
    conn = dbmod.get_conn()
    msgs = [
        dict(r)
        for r in conn.execute(
            "SELECT role, content, created_at FROM message "
            "WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        )
    ]
    return {"conversation_id": conversation_id, "messages": msgs}
