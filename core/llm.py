"""DeepSeek gateway client with one model, credential failover and safe streaming.

Reasoning is measured but never returned as answer text or persisted. Credential
failover changes only the key, never the upstream model. All callers, including
image transcription, use the same model policy.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Sequence

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from core.config import Config, Secrets, get_config, get_secrets, mask
from core.model_policy import DEEPSEEK_MODEL, ModelPolicyError, require_model

logger = logging.getLogger(__name__)


#: Reserve output headroom for tool JSON and gateways that emit reasoning first.
MIN_MAX_TOKENS = 2048

#: 失败分类。决定「重试同一 Key」还是「冷却并换 Key」还是「直接失败」。
FailureKind = Literal["transient", "budget", "auth", "fatal"]

#: 额度类关键词（冷却较短：额度是带外补充的，值得定期重试）
_BUDGET_HINTS = (
    "insufficient_quota", "insufficient quota", "exceeded your current quota",
    "budget", "quota", "billing", "credit", "余额", "额度", "配额",
)
#: 鉴权 / 模型权限类关键词（配置错误不会自愈，冷却较长）
_AUTH_HINTS = (
    "invalid api key", "invalid_api_key", "incorrect api key", "authentication",
    "unauthorized", "auth_error", "no access", "not allowed", "not permitted",
    "model_not_found", "does not exist", "key not found",
)
#: Compatible gateways may reject optional sampling parameters with HTTP 400.
#: 这类错误不该让整次调用失败——去掉该参数重试一次即可。
_PARAM_UNSUPPORTED_HINTS = (
    "deprecated for this model", "unsupported parameter", "unsupported_parameter",
    "does not support", "not supported with this model", "unrecognized request argument",
)
#: 可被丢弃重试的采样参数。只列真正可选的——丢掉 max_tokens 会让推理吃光预算。
_DROPPABLE_PARAMS = ("temperature", "top_p", "presence_penalty", "frequency_penalty")


# ── 异常 ───────────────────────────────────────────────────

class LLMError(RuntimeError):
    """LLM 调用失败的基类。消息已脱敏，可安全写日志。"""


class AllCredentialsUnavailable(LLMError):
    """凭证池里所有 Key 都在冷却中。"""


class TokenBudgetExceeded(LLMError):
    """单轮 Token 预算耗尽（需求 16.4 的成本闸门）。"""


class EmptyContentError(LLMError):
    """上游没有返回正文或工具调用，可能是输出预算不足。

    单列成一类异常而非返回空串，是为了让这个失败模式无法被静默忽略。
    """


class IncompleteResponseError(LLMError):
    """The response ended without a successful completion or complete tool call."""


# ── 数据结构 ───────────────────────────────────────────────

@dataclass(frozen=True)
class Usage:
    """Token 用量。

    注意 `total_tokens` 已包含推理消耗（OpenAI 语义下 `reasoning_tokens`
    是 `completion_tokens` 的子集，实测 117 里含 114）。`reasoning_tokens`
    单列仅用于观测，**不要再加一遍**，那会让成本翻倍高估。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt": self.prompt_tokens, "output": self.completion_tokens,
            "reasoning": self.reasoning_tokens, "total": self.total_tokens,
        }

    @property
    def reasoning_ratio(self) -> float:
        return self.reasoning_tokens / self.completion_tokens if self.completion_tokens else 0.0


@dataclass(frozen=True)
class ToolCall:
    """一次工具调用请求。`arguments` 保留原始 JSON 字符串——
    解析与校验交给 tools 层，这样非法 JSON 也能被记录而不是在此处炸掉。"""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = ""
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    #: 推理内容的字符数，仅用于诊断与告警。**不保存推理正文本身**，
    #: 从数据结构上断掉「不小心把推理渲染给用户」的可能。
    reasoning_chars: int = 0
    elapsed_ms: int = 0


StreamKind = Literal["thinking", "content", "tool_calls", "usage", "error"]


@dataclass(frozen=True)
class StreamEvent:
    """流式事件。

    `text` **只允许** `kind == "content"` 时非空，由 `__post_init__` 强制。
    这样任何消费方（含前端）渲染 `text` 都不可能渲染到推理内容——
    把安全要求落成类型约束，而不是靠调用方自觉。
    """

    kind: StreamKind
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None
    detail: str = ""
    #: 实际服务本次请求的模型，只在 `kind == "usage"` 时填。
    #: 非流式走 `LLMResponse.model`，流式使用同一固定模型。
    model: str = ""

    def __post_init__(self) -> None:
        if self.kind != "content" and self.text:
            raise ValueError(
                f"StreamEvent(kind={self.kind!r}) 不得携带 text："
                "只有正文可以外发，推理内容禁止流出"
            )


@dataclass
class TurnBudget:
    """单轮 Token 预算。跨多次工具调用累计，超限即中断本轮。"""

    limit: int
    used: Usage = field(default_factory=Usage)

    def add(self, usage: Usage) -> None:
        self.used = self.used + usage
        if self.limit and self.used.total_tokens > self.limit:
            raise TokenBudgetExceeded(
                f"单轮 Token 预算耗尽：已用 {self.used.total_tokens}"
                f"（其中推理 {self.used.reasoning_tokens}）> 上限 {self.limit}"
            )

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used.total_tokens) if self.limit else 0


# ── 凭证池 ─────────────────────────────────────────────────

@dataclass
class _Credential:
    index: int
    key: str
    cooldown_until: float = 0.0
    reason: str = ""
    failures: int = 0

    @property
    def label(self) -> str:
        return f"#{self.index}({mask(self.key)})"

    def available(self, now: float) -> bool:
        return now >= self.cooldown_until


class CredentialPool:
    """按优先级粘性选择第一个健康凭证。

    「粘性」体现在始终从下标 0 起扫：只要高优先级 Key 健康就一直用它，
    不做轮询——轮询会把流量摊到低优先级 Key 上，与配置里的优先级意图相反。
    """

    def __init__(
        self,
        keys: Sequence[str],
        *,
        budget_cooldown: float,
        auth_cooldown: float,
    ) -> None:
        if not keys:
            raise LLMError("未配置任何 LLM 凭证：请在 .env 设置 LLM_API_KEY 或 LLM_API_KEYS")
        self._creds = [
            _Credential(index=i, key=key) for i, key in enumerate(keys)
        ]
        self._budget_cooldown = budget_cooldown
        self._auth_cooldown = auth_cooldown
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        return len(self._creds)

    def keys(self) -> tuple[str, ...]:
        return tuple(c.key for c in self._creds)

    def acquire(self) -> _Credential:
        now = time.monotonic()
        with self._lock:
            for cred in self._creds:
                if cred.available(now):
                    return cred
            waits = ", ".join(
                f"{c.label} 还需 {c.cooldown_until - now:.0f}s（{c.reason}）"
                for c in self._creds
            )
        raise AllCredentialsUnavailable(f"全部 {len(self._creds)} 个凭证均在冷却：{waits}")

    def penalize(self, cred: _Credential, kind: FailureKind, detail: str) -> None:
        """按失败类型分级冷却。瞬时错误不该走到这里（只重试不降级）。"""
        seconds = self._budget_cooldown if kind == "budget" else self._auth_cooldown
        with self._lock:
            cred.cooldown_until = time.monotonic() + seconds
            cred.reason = f"{kind}: {detail[:120]}"
            cred.failures += 1
        logger.warning(
            "凭证 %s 因 %s 冷却 %.0f 分钟：%s",
            cred.label, kind, seconds / 60, detail[:200],
        )

    def snapshot(self) -> list[dict[str, Any]]:
        """供 /healthz 与 /admin 观测。只输出脱敏标签。"""
        now = time.monotonic()
        with self._lock:
            return [
                {
                    "index": c.index,
                    "key": mask(c.key),
                    "model": DEEPSEEK_MODEL,
                    "healthy": c.available(now),
                    "cooldown_seconds": max(0, round(c.cooldown_until - now)),
                    "reason": c.reason,
                    "failures": c.failures,
                }
                for c in self._creds
            ]


# ── 失败分类 ───────────────────────────────────────────────

def classify_failure(exc: Exception) -> tuple[FailureKind, str]:
    """把异常映射为失败类型 + 可写日志的说明。"""
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return "transient", f"{type(exc).__name__}: {exc}"

    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        body = _body_text(exc).lower()
        detail = f"HTTP {status}: {_body_text(exc)[:200]}"

        # 429 既可能是限流（瞬时）也可能是额度耗尽，必须看 body 才能区分。
        # 误判成瞬时会对着一个没额度的 Key 重试 5 次并白等 30 秒。
        if status == 429:
            kind: FailureKind = "budget" if _hit(body, _BUDGET_HINTS) else "transient"
            return kind, detail
        if status >= 500:
            return "transient", detail
        if status == 408:
            return "transient", detail
        if status == 402:
            return "budget", detail
        if status in (401, 403):
            return "auth", detail
        if status == 404:
            # 固定模型不可见 / 该 Key 无权访问，只允许尝试同模型的下一把 Key
            return "auth", detail
        if _hit(body, _BUDGET_HINTS):
            return "budget", detail
        if _hit(body, _AUTH_HINTS):
            return "auth", detail
        # 400 等：请求本身有问题，重试与换 Key 都无用
        return "fatal", detail

    return "fatal", f"{type(exc).__name__}: {exc}"


def _hit(text: str, hints: Sequence[str]) -> bool:
    return any(h in text for h in hints)


def _body_text(exc: APIStatusError) -> str:
    for attr in ("message", "body"):
        value = getattr(exc, attr, None)
        if value:
            return str(value)
    return str(exc)


# ── 客户端 ─────────────────────────────────────────────────

class LLMClient:
    """线程安全的同步客户端。FastAPI 的同步端点跑在线程池里，故加锁保护凭证池。"""

    def __init__(self, cfg: Config | None = None, secrets: Secrets | None = None) -> None:
        self._cfg = cfg or get_config()
        self._sec = secrets or get_secrets()
        self._base_url = _normalize_base_url(self._sec.llm_base_url)
        try:
            self._default_model = require_model(self._sec.llm_model)
            keys = self._sec.credential_keys()
            # Reject obsolete alternate routing, even if a caller never reaches it.
            for value in (self._cfg.get("llm.model_aliases", {}) or {}).values():
                require_model(value)
            for key in ("llm.gate_model", "llm.vision_model"):
                if self._cfg.get(key):
                    require_model(self._cfg.get(key))
        except ModelPolicyError as exc:
            raise LLMError(str(exc)) from None
        self._timeout = float(self._cfg.get("llm.timeout_seconds", 180))
        self._temperature = self._cfg.get("llm.temperature", None)
        self._retry_delays: tuple[float, ...] = tuple(
            float(d) for d in (self._cfg.get("llm.retry_delays", [1, 2, 4, 8, 15]) or [])
        )
        self._max_tokens = _clamp_max_tokens(int(self._cfg.get("llm.max_tokens", 8192)))
        self._pool = CredentialPool(
            keys,
            budget_cooldown=float(self._cfg.get("llm.cooldown_budget_seconds", 1800)),
            auth_cooldown=float(self._cfg.get("llm.cooldown_auth_seconds", 3600)),
        )
        self._clients: dict[int, OpenAI] = {}
        #: Sampling parameters rejected by this gateway are no longer sent.
        self._unsupported_params: set[str] = set()
        self._lock = threading.Lock()
        logger.info(
            "LLM 客户端就绪：provider=deepseek 模型=%s 凭证=%d max_tokens=%d",
            self._default_model, self._pool.size, self._max_tokens,
        )

    # ── 公开 API ──

    @property
    def default_model(self) -> str:
        return self._default_model

    def credential_status(self) -> list[dict[str, Any]]:
        return self._pool.snapshot()

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        budget: TurnBudget | None = None,
    ) -> LLMResponse:
        """一次非流式调用。

        Pure tool_call replies are valid. An HTTP success with neither content
        nor tools always raises EmptyContentError.
        """
        started = time.monotonic()
        max_tokens = self._request_tokens(messages, tools, max_tokens, budget)
        raw, model_used = self._invoke(
            stream=False,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)

        usage = _usage_of(raw)
        if budget is not None:
            budget.add(usage)
        self._check_served_model(getattr(raw, "model", ""))
        choice = raw.choices[0] if raw.choices else None
        msg = getattr(choice, "message", None)
        content = (getattr(msg, "content", "") or "").strip()
        reasoning = _extra(msg, "reasoning_content") or ""
        calls = tuple(
            ToolCall(
                id=tc.id or "",
                name=(getattr(tc, "function", None) and tc.function.name) or "",
                arguments=(getattr(tc, "function", None) and tc.function.arguments) or "",
            )
            for tc in (getattr(msg, "tool_calls", None) or [])
        )
        finish = getattr(choice, "finish_reason", "") or ""
        self._check_finish(finish)

        logger.info(
            "LLM 调用完成：模型=%s 耗时=%dms finish=%s token=%d(正文%d/推理%d) "
            "正文=%d字 tool_calls=%d",
            model_used, elapsed_ms, finish, usage.total_tokens,
            usage.completion_tokens - usage.reasoning_tokens, usage.reasoning_tokens,
            len(content), len(calls),
        )
        self._check_empty(
            content, calls, usage, finish, model_used,
            self._effective_max_tokens(max_tokens),
        )

        return LLMResponse(
            content=content,
            tool_calls=calls,
            finish_reason=finish,
            model=model_used,
            usage=usage,
            reasoning_chars=len(reasoning),
            elapsed_ms=elapsed_ms,
        )

    def stream(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        budget: TurnBudget | None = None,
    ) -> Iterator[StreamEvent]:
        """流式调用，产出 `StreamEvent`。

        只有 `kind="content"` 的事件携带文本；推理阶段只发一个 `thinking`
        事件供前端显示「思考中」，推理正文既不外发也不留存。

        重试只覆盖**建立流**的阶段：一旦开始产出分片就无法透明重试，
        中途失败会以 `kind="error"` 事件结束。
        """
        started = time.monotonic()
        max_tokens = self._request_tokens(messages, tools, max_tokens, budget)
        raw, model_used = self._invoke(
            stream=True,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        usage = Usage()
        content_chars = 0
        reasoning_chars = 0
        thinking_announced = False
        finish = ""
        #: Validate the reported model before exposing any response fragment.
        served_model = ""
        #: 流式 tool_calls 按 index 分片到达，需要在此拼装
        partial: dict[int, dict[str, str]] = {}
        failure = ""

        try:
            for chunk in raw:
                chunk_usage = _usage_of(chunk)
                if chunk_usage.total_tokens:
                    usage = chunk_usage  # 网关在末尾给累计值，直接覆盖而非相加
                self._check_served_model(getattr(chunk, "model", ""))
                served_model = getattr(chunk, "model", "") or served_model

                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                finish = getattr(choices[0], "finish_reason", "") or finish
                if delta is None:
                    continue

                reasoning_piece = _extra(delta, "reasoning_content")
                if reasoning_piece:
                    reasoning_chars += len(reasoning_piece)
                    if not thinking_announced:
                        thinking_announced = True
                        yield StreamEvent(kind="thinking")

                piece = getattr(delta, "content", None)
                if piece:
                    content_chars += len(piece)
                    yield StreamEvent(kind="content", text=piece)

                for tc in getattr(delta, "tool_calls", None) or []:
                    slot = partial.setdefault(
                        getattr(tc, "index", 0) or 0, {"id": "", "name": "", "arguments": ""}
                    )
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["arguments"] += fn.arguments
            self._check_finish(finish)
        except Exception as exc:  # noqa: BLE001
            _, detail = classify_failure(exc)
            failure = self._sanitize(detail)
            logger.error("流式输出中断（已产出 %d 字正文）：%s", content_chars, failure)
        finally:
            # Also runs on downstream cancellation. Never emit from this block.
            try:
                close = getattr(raw, "close", None)
                if close is not None:
                    close()
            finally:
                if budget is not None:
                    try:
                        budget.add(usage)
                    except TokenBudgetExceeded as exc:
                        failure = str(exc)

        if failure:
            yield StreamEvent(kind="error", detail=failure)
            return

        if partial:
            yield StreamEvent(
                kind="tool_calls",
                tool_calls=tuple(
                    ToolCall(id=v["id"], name=v["name"], arguments=v["arguments"])
                    for _, v in sorted(partial.items())
                ),
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "LLM 流式完成：模型=%s 耗时=%dms finish=%s token=%d(推理%d) "
            "正文=%d字 推理=%d字 tool_calls=%d",
            served_model or model_used, elapsed_ms, finish, usage.total_tokens,
            usage.reasoning_tokens, content_chars, reasoning_chars, len(partial),
        )
        yield StreamEvent(kind="usage", usage=usage, model=served_model or model_used)
        if not content_chars and not partial:
            yield StreamEvent(
                kind="error",
                detail="DeepSeek 未返回正文或工具调用；请重试或检查输出预算",
            )

    @staticmethod
    def _check_finish(finish: str) -> None:
        if finish not in {"stop", "tool_calls"}:
            raise IncompleteResponseError("DeepSeek 响应未完整结束；请重试或检查输出预算")

    def _check_served_model(self, model: str | None) -> None:
        if model and model != self._default_model:
            raise LLMError("网关返回的模型与固定 DeepSeek 模型不一致，响应已拒绝")

    # ── 内部 ──

    def _invoke(
        self,
        *,
        stream: bool,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        model: str | None,
        max_tokens: int | None,
        temperature: float | None,
    ) -> tuple[Any, str]:
        """带重试与故障转移的调用。返回 (原始响应或流, 实际使用的模型)。"""
        try:
            model_used = require_model(model)
        except ModelPolicyError as exc:
            raise LLMError(str(exc)) from None
        effective_max = self._effective_max_tokens(max_tokens)
        attempt = 0
        while True:
            cred = self._pool.acquire()
            # Failover selects another credential for the same model only.
            payload: dict[str, Any] = {
                "model": model_used,
                "messages": list(messages),
                "max_tokens": effective_max,
                "stream": stream,
            }
            temp = temperature if temperature is not None else self._temperature
            if temp is not None and "temperature" not in self._unsupported_params:
                payload["temperature"] = float(temp)
            if tools:
                payload["tools"] = list(tools)
                if tool_choice is not None:
                    payload["tool_choice"] = tool_choice
            if stream:
                # 不加这个网关不会在流末尾给 usage，成本就统计不到
                payload["stream_options"] = {"include_usage": True}

            try:
                return self._client_for(cred).chat.completions.create(**payload), model_used
            except Exception as exc:  # noqa: BLE001
                kind, detail = classify_failure(exc)
                detail = self._sanitize(detail)

                if kind == "fatal":
                    # 「这个模型不支持某个采样参数」不该让整次调用失败：
                    # 去掉该参数重试。记进实例级集合，后续请求不再发送——
                    # 同一模型每次都会拒，每轮重试一次等于白花一次调用。
                    dropped_now = self._drop_unsupported(detail)
                    if dropped_now:
                        logger.warning(
                            "模型 %s 不支持参数 %s，已停止发送并重试：%s",
                            model_used, ", ".join(sorted(dropped_now)), detail[:160],
                        )
                        continue
                    raise LLMError(f"LLM 调用失败（不可重试）：{detail}") from exc

                if kind == "transient":
                    # 约定：5xx / 429 只重试，不降级 Key
                    if attempt >= len(self._retry_delays):
                        raise LLMError(
                            f"LLM 调用失败：瞬时错误重试 {attempt} 次仍未成功：{detail}"
                        ) from exc
                    delay = self._retry_delays[attempt]
                    attempt += 1
                    logger.warning(
                        "凭证 %s 遇瞬时错误，%.0fs 后第 %d 次重试（不换 Key）：%s",
                        cred.label, delay, attempt, detail,
                    )
                    time.sleep(delay)
                    continue

                # budget / auth：冷却当前 Key 并换下一个
                self._pool.penalize(cred, kind, detail)

    def _sanitize(self, detail: str) -> str:
        return (_scrub(detail, self._pool)
                .replace(self._base_url, "[gateway]")
                .replace(self._sec.llm_base_url.strip().rstrip("/"), "[gateway]"))

    def _request_tokens(self, messages, tools, requested, budget) -> int:
        maximum = self._effective_max_tokens(requested)
        if budget is None or not budget.limit:
            return maximum
        # A conservative local estimate; actual provider usage is still charged.
        payload = json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False)
        estimated_input = (len(payload.encode("utf-8")) + 2) // 3 + 64
        available = budget.remaining - estimated_input
        if available < MIN_MAX_TOKENS:
            raise TokenBudgetExceeded("剩余 Token 预算不足以发送本轮上下文，请缩小问题范围")
        return min(maximum, available)

    def _effective_max_tokens(self, max_tokens: int | None) -> int:
        return _clamp_max_tokens(max_tokens) if max_tokens else self._max_tokens

    def _drop_unsupported(self, detail: str) -> set[str]:
        """从 400 的错误消息里识别「该模型不支持的采样参数」并登记停发。

        返回本次新登记的参数名；空集表示这个 400 不属于该类，应按 fatal 处理。
        """
        low = detail.lower()
        if not any(h in low for h in _PARAM_UNSUPPORTED_HINTS):
            return set()
        found = {p for p in _DROPPABLE_PARAMS if p in low} - self._unsupported_params
        if not found:
            # 提到了「不支持」但没指到可丢弃的参数 —— 不能瞎猜，按 fatal 处理
            return set()
        with self._lock:
            self._unsupported_params |= found
        return found

    def _client_for(self, cred: _Credential) -> OpenAI:
        with self._lock:
            client = self._clients.get(cred.index)
            if client is None:
                client = OpenAI(
                    base_url=self._base_url,
                    api_key=cred.key,
                    timeout=self._timeout,
                    max_retries=0,  # 重试由本模块统一管理，避免两层退避叠加
                )
                self._clients[cred.index] = client
            return client

    def _check_empty(
        self,
        content: str,
        calls: tuple[ToolCall, ...],
        usage: Usage,
        finish: str,
        model: str,
        max_tokens: int,
    ) -> None:
        if content or calls:
            return
        # Empty tool-only responses are valid only when tool_calls is nonempty.
        # A successful HTTP status without either result must never look like success.
        msg = (
            f"模型 {model} 未返回正文或工具调用（finish_reason={finish or '未给出'}，"
            f"max_tokens={max_tokens}，reasoning_tokens={usage.reasoning_tokens}）"
        )
        logger.error("%s", msg)
        raise EmptyContentError(msg)


# ── 辅助 ───────────────────────────────────────────────────

def _normalize_base_url(base_url: str) -> str:
    """网关地址补 `/v1`。`.env` 里配的是根地址，而 SDK 会直接在 base_url
    后拼 `/chat/completions`，不补就会 404。"""
    from urllib.parse import urlsplit

    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("LLM_BASE_URL 未配置")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError
    except ValueError:
        raise LLMError("LLM_BASE_URL 必须是无凭证、查询参数和片段的 HTTP(S) 地址") from None
    return url if url.endswith("/v1") else f"{url}/v1"


def _clamp_max_tokens(value: int) -> int:
    if value >= MIN_MAX_TOKENS:
        return value
    logger.warning(
        "max_tokens=%d 低于推理模型下限 %d，已抬升。推理可能先消耗输出预算，"
        "给少了会导致正文为空且不报错", value, MIN_MAX_TOKENS,
    )
    return MIN_MAX_TOKENS


def _extra(obj: Any, name: str) -> str:
    """读取 OpenAI schema 之外的字段（如 `reasoning_content`）。

    SDK 会把非标准字段塞进 `model_extra`，但不同版本也可能直接挂成属性，
    两条路都试。
    """
    if obj is None:
        return ""
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get(name)
    return str(value) if value else ""


def _usage_of(raw: Any) -> Usage:
    u = getattr(raw, "usage", None)
    if u is None:
        return Usage()
    details = getattr(u, "completion_tokens_details", None)
    if isinstance(details, dict):
        reasoning = int(details.get("reasoning_tokens") or 0)
    else:
        reasoning = int(getattr(details, "reasoning_tokens", 0) or 0)
    return Usage(
        prompt_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(u, "completion_tokens", 0) or 0),
        reasoning_tokens=reasoning,
        total_tokens=int(getattr(u, "total_tokens", 0) or 0),
    )


def _scrub(text: str, pool: CredentialPool) -> str:
    """把可能混入消息的完整密钥替换成脱敏标签。

    上游异常一般不带 Key，但这条兜底很便宜：一旦哪天带了，
    密钥就会直接写进日志与错误响应。
    """
    out = text
    for key in pool.keys():
        if key and key in out:
            out = out.replace(key, mask(key))
    return out


_client: LLMClient | None = None
_client_lock = threading.Lock()


def get_llm_client() -> LLMClient:
    """进程内单例。不用 lru_cache，方便测试里重置。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = LLMClient()
        return _client


