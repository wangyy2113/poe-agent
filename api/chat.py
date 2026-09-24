"""`/chat`、`/chat/stream` 与 OpenAI Chat Completions 兼容接口（任务 3.8）。

流式的粒度是**句**而不是字。按需求 5.1 与设计 11.1，出口的断言绑定校验必须
按句缓冲、校验通过才下发——逐字推送等于把未校验内容先给了用户，之后没法撤回。
前端对已通过的句子做打字机效果，观感接近逐字流，但不会显示未校验内容。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Iterator, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from api.deps import require_user
from core import db as dbmod
from orchestrator.loop import run_turn, stream_turn

logger = logging.getLogger(__name__)

router = APIRouter(tags=["问答"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=60)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class OpenAITextPart(BaseModel):
    type: Literal["text"]
    text: str


class OpenAIMessage(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    content: str | list[OpenAITextPart]
    name: str | None = Field(default=None, max_length=64)


class OpenAIStreamOptions(BaseModel):
    include_usage: bool = False


class OpenAIChatRequest(BaseModel):
    """OpenAI Chat Completions 的文本子集。

    `model` 是客户端需要填写的逻辑名称；实际使用的上游模型由服务端配置与凭证池
    决定，并通过响应的 `poe_agent.upstream_model` 扩展字段明确返回。
    """

    model: str = Field(default="poe-agent", min_length=1, max_length=200)
    messages: list[OpenAIMessage] = Field(min_length=1, max_length=100)
    stream: bool = False
    stream_options: OpenAIStreamOptions | None = None
    conversation_id: str | None = Field(default=None, max_length=128)


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


def _openai_question(messages: list[OpenAIMessage]) -> str:
    """只取最后一条用户文本；系统提示与历史回答不能覆盖本 Agent 的安全约束。"""
    for message in reversed(messages):
        if message.role != "user":
            continue
        if isinstance(message.content, str):
            question = message.content.strip()
        else:
            question = "".join(part.text for part in message.content).strip()
        if not question:
            break
        if len(question) > 2000:
            raise HTTPException(status_code=400, detail="最后一条用户消息不能超过 2000 个字符")
        return question
    raise HTTPException(status_code=400, detail="messages 必须包含一条非空的 user 文本消息")


def _openai_usage(tokens: dict[str, int]) -> dict[str, Any]:
    return {
        "prompt_tokens": int(tokens.get("prompt", 0)),
        "completion_tokens": int(tokens.get("output", 0)),
        "total_tokens": int(tokens.get("total", 0)),
        "completion_tokens_details": {
            "reasoning_tokens": int(tokens.get("reasoning", 0)),
        },
    }


def _citation_block(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    lines = "\n".join(
        f"[{item['label']}] 来源：{item['source']} → {item['title']}\n    {item['url']}"
        for item in items
    )
    return f"\n\n---\n**引用**\n{lines}"


def _openai_chunk(
    completion_id: str,
    created: int,
    model: str,
    *,
    delta: dict[str, str] | None = None,
    finish_reason: str | None = None,
    choices: bool = True,
    usage: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": ([{
            "index": 0,
            "delta": delta or {},
            "finish_reason": finish_reason,
        }] if choices else []),
    }
    if usage is not None:
        payload["usage"] = usage
    if metadata is not None:
        payload["poe_agent"] = metadata
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _openai_stream(
    req: OpenAIChatRequest,
    question: str,
    conversation_id: str,
    created: int,
    caller: str,
) -> Iterator[str]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    trace_id = ""
    answer_parts: list[str] = []
    citations: list[dict[str, Any]] = []
    done: dict[str, Any] = {}
    try:
        for event in stream_turn(
            question, conversation_id=conversation_id, owner_id=caller
        ):
            kind = event.get("type")
            if kind == "meta":
                trace_id = str(event.get("trace_id") or "")
                if trace_id:
                    completion_id = f"chatcmpl-{trace_id}"
                yield _openai_chunk(
                    completion_id,
                    created,
                    req.model,
                    delta={"role": "assistant", "content": ""},
                    metadata={
                        "trace_id": trace_id,
                        "conversation_id": conversation_id,
                    },
                )
            elif kind in {"sentence", "blocked", "gap"}:
                text = str(event.get("text") or "")
                if text:
                    answer_parts.append(text)
                    yield _openai_chunk(
                        completion_id, created, req.model, delta={"content": text}
                    )
            elif kind == "note":
                text = str(event.get("text") or "")
                if text:
                    rendered = f"\n\n{text}" if text.startswith("推导（取自证据") else f"\n\n> {text}"
                    answer_parts.append(rendered)
                    yield _openai_chunk(
                        completion_id, created, req.model, delta={"content": rendered}
                    )
            elif kind == "citations":
                citations = list(event.get("items") or [])
            elif kind == "error":
                payload = {
                    "error": {
                        "message": str(event.get("message") or "生成失败"),
                        "type": "server_error",
                        "param": None,
                        "code": "generation_failed",
                    }
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            elif kind == "done":
                done = event

        citation_text = _citation_block(citations)
        if citation_text:
            answer_parts.append(citation_text)
            yield _openai_chunk(
                completion_id, created, req.model, delta={"content": citation_text}
            )

        metadata = {
            "trace_id": trace_id or str(done.get("trace_id") or ""),
            "conversation_id": conversation_id,
            "evidence_gap": bool(done.get("evidence_gap", False)),
            "blocked_claims": int(done.get("blocked_claims", 0)),
            "upstream_model": str(done.get("model") or ""),
            "citations": citations,
        }
        yield _openai_chunk(
            completion_id,
            created,
            req.model,
            finish_reason="stop",
            metadata=metadata,
        )
        if req.stream_options and req.stream_options.include_usage:
            yield _openai_chunk(
                completion_id,
                created,
                req.model,
                choices=False,
                usage=_openai_usage(done.get("tokens") or {}),
            )
        yield "data: [DONE]\n\n"
    finally:
        text = "".join(answer_parts)
        if text.strip():
            _save_message(conversation_id, "assistant", text)


@router.post("/v1/chat/completions", response_model=None)
def openai_chat_completions(
    req: OpenAIChatRequest,
    caller: str = Depends(require_user),
) -> Any:
    """OpenAI Chat Completions 文本兼容层；仍走本项目的检索与出口闸门。"""
    question = _openai_question(req.messages)
    conversation_id = req.conversation_id or uuid.uuid4().hex
    created = int(time.time())
    _ensure_conversation(conversation_id, question)

    if req.stream:
        _save_message(conversation_id, "user", question)
        return StreamingResponse(
            _openai_stream(req, question, conversation_id, created, caller),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    result = run_turn(question, conversation_id=conversation_id, owner_id=caller)
    text = result.full_text()
    _save_message(conversation_id, "user", question)
    _save_message(conversation_id, "assistant", text)
    citations = [
        {
            "label": item.label,
            "source": item.source,
            "title": item.title,
            "url": item.url,
            "priority": item.priority,
        }
        for item in result.citations
    ]
    logger.info(
        "v1/chat/completions 完成 caller=%s trace=%s 缺据=%s 拦截=%d token=%d 耗时=%dms",
        caller,
        result.trace_id,
        result.evidence_gap,
        result.blocked,
        result.tokens.get("total", 0),
        result.elapsed_ms,
    )
    return {
        "id": f"chatcmpl-{result.trace_id}",
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": _openai_usage(result.tokens),
        "poe_agent": {
            "trace_id": result.trace_id,
            "conversation_id": conversation_id,
            "evidence_gap": result.evidence_gap,
            "blocked_claims": result.blocked,
            "upstream_model": result.model,
            "citations": citations,
        },
    }


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, caller: str = Depends(require_user)) -> ChatResponse:
    conversation_id = req.conversation_id or uuid.uuid4().hex
    _ensure_conversation(conversation_id, req.message)

    result = run_turn(
        req.message, conversation_id=conversation_id, owner_id=caller
    )

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
            for event in stream_turn(
                req.message, conversation_id=conversation_id, owner_id=caller
            ):
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
            text += _citation_block(citations)
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
def conversation(
    conversation_id: str,
    caller: str = Depends(require_user),
) -> dict[str, Any]:
    conn = dbmod.get_conn()
    conv = conn.execute(
        "SELECT title FROM conversation WHERE conversation_id = ?",
        (conversation_id,),
    ).fetchone()
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = [
        dict(r)
        for r in conn.execute(
            "SELECT role, content, created_at FROM message "
            "WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        )
    ]
    return {
        "conversation_id": conversation_id,
        "title": conv["title"] or "新对话",
        "messages": msgs,
    }


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    req: ConversationUpdate,
    caller: str = Depends(require_user),
) -> dict[str, str]:
    conn = dbmod.get_conn()
    cursor = conn.execute(
        "UPDATE conversation SET title = ?, updated_at = datetime('now') "
        "WHERE conversation_id = ?",
        (req.title, conversation_id),
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="会话不存在")
    conn.commit()
    logger.info("会话重命名 caller=%s conversation=%s", caller, conversation_id)
    return {"conversation_id": conversation_id, "title": req.title}
