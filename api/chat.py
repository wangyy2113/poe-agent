"""`/chat`：问答接口（任务 3.8 的非流式部分）。

流式（SSE）留到下一步：按需求 5.1 与设计 11.1，流式必须**按句缓冲、校验通过才下发**，
不能把未校验内容先推给用户。那需要把 `orchestrator/loop.py` 的出口闸门改造成
可增量消费的形式，与本接口分开做更稳。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.deps import require_user
from core import db as dbmod
from orchestrator.loop import run_turn

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
