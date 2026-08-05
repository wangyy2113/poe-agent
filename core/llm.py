"""LLM 客户端：`openai` SDK 直连内部 LiteLLM 网关的 `/v1/chat/completions`。

设计要点（对应 design.md 3.2.2 / 3.2.4，tasks 0.6b + 3.1）：

1. **只实现 OpenAI 兼容一种**。网关已提供 OpenAI 协议，不引入 `anthropic` SDK，
   也不走 `claude` CLI 子进程（拿不到增量流会破坏出口的按句校验）。
2. **推理模型适配**。GLM 5.2 返回 `reasoning_content` / `reasoning_tokens`，
   实测推理占输出可达 98%。因此：`max_tokens` 有强制下限；Token 计量含推理；
   流式只把 `delta.content` 作为文本外发；推理内容**在类型层面就无法流出**。
3. **多 Key 故障转移**。按优先级粘性选第一个健康凭证；额度类冷却 30 分钟、
   鉴权/模型权限类冷却 60 分钟；5xx/429 只重试不降级 Key；切 Key 同步切模型。

安全约束：完整密钥不得进入任何日志或异常消息（见 `_scrub`）；
`reasoning_content` 不得作为答案外发——那正是模型凭参数记忆猜测的地方，
透出去等于绕过「无据不答」的全部闸门。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Sequence

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from core.config import Config, Secrets, get_config, get_secrets, mask

logger = logging.getLogger(__name__)


#: `max_tokens` 硬下限。实测 GLM 5.2 回答「收到」两个字消耗 completion_tokens=117，
#: 其中 reasoning_tokens=114。若 max_tokens 设小（首测 64），推理会吃光预算导致
#: **正文为空且不报错**——最隐蔽的失败模式。故在客户端层强制抬到该下限，
#: 不信任调用方与配置文件。
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


# ── 异常 ───────────────────────────────────────────────────

class LLMError(RuntimeError):
    """LLM 调用失败的基类。消息已脱敏，可安全写日志。"""


class AllCredentialsUnavailable(LLMError):
    """凭证池里所有 Key 都在冷却中。"""


class TokenBudgetExceeded(LLMError):
    """单轮 Token 预算耗尽（需求 16.4 的成本闸门）。"""


class EmptyContentError(LLMError):
    """正文为空但推理消耗了 token —— 典型是 max_tokens 给少了。

    单列成一类异常而非返回空串，是为了让这个失败模式无法被静默忽略。
    """


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
    #: 绑定模型。受限 Key 访问别的模型只会 401，所以切 Key 必须同步切模型。
    model: str = ""
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
        specs: Sequence[tuple[str, str]],
        *,
        budget_cooldown: float,
        auth_cooldown: float,
    ) -> None:
        if not specs:
            raise LLMError("未配置任何 LLM 凭证：请在 .env 设置 LLM_API_KEY 或 LLM_API_KEYS")
        self._creds = [
            _Credential(index=i, key=key, model=model)
            for i, (key, model) in enumerate(specs)
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
                    "model": c.model or "(沿用默认)",
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
            # 网关上没有这个模型 / 该 Key 无权访问 → 换 Key 时会同步换模型
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
        self._default_model = self._cfg.resolve_model(self._sec.llm_model)
        self._timeout = float(self._cfg.get("llm.timeout_seconds", 180))
        self._temperature = self._cfg.get("llm.temperature", None)
        self._retry_delays: tuple[float, ...] = tuple(
            float(d) for d in (self._cfg.get("llm.retry_delays", [1, 2, 4, 8, 15]) or [])
        )
        self._max_tokens = _clamp_max_tokens(int(self._cfg.get("llm.max_tokens", 8192)))
        self._pool = CredentialPool(
            self._sec.credential_specs(),
            budget_cooldown=float(self._cfg.get("llm.cooldown_budget_seconds", 1800)),
            auth_cooldown=float(self._cfg.get("llm.cooldown_auth_seconds", 3600)),
        )
        self._clients: dict[int, OpenAI] = {}
        self._lock = threading.Lock()
        logger.info(
            "LLM 客户端就绪：%s 模型=%s 凭证=%d max_tokens=%d",
            self._base_url, self._default_model, self._pool.size, self._max_tokens,
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
        allow_empty_content: bool = False,
    ) -> LLMResponse:
        """一次非流式调用。

        `allow_empty_content=False` 时，若正文为空而推理消耗了 token，抛
        `EmptyContentError`——这是 max_tokens 不足的典型症状，不能静默当成
        「模型没话说」。只在明确期待纯 tool_call 回复时才置 True。
        """
        started = time.monotonic()
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

        choice = raw.choices[0] if raw.choices else None
        msg = getattr(choice, "message", None)
        content = (getattr(msg, "content", "") or "").strip()
        reasoning = _extra(msg, "reasoning_content") or ""
        usage = _usage_of(raw)
        calls = tuple(
            ToolCall(
                id=tc.id or "",
                name=(getattr(tc, "function", None) and tc.function.name) or "",
                arguments=(getattr(tc, "function", None) and tc.function.arguments) or "",
            )
            for tc in (getattr(msg, "tool_calls", None) or [])
        )
        finish = getattr(choice, "finish_reason", "") or ""

        logger.info(
            "LLM 调用完成：模型=%s 耗时=%dms finish=%s token=%d(正文%d/推理%d) "
            "正文=%d字 tool_calls=%d",
            model_used, elapsed_ms, finish, usage.total_tokens,
            usage.completion_tokens - usage.reasoning_tokens, usage.reasoning_tokens,
            len(content), len(calls),
        )
        self._check_empty(
            content, calls, usage, finish, model_used,
            self._effective_max_tokens(max_tokens), allow_empty_content,
        )
        if budget is not None:
            budget.add(usage)

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
        #: 流式 tool_calls 按 index 分片到达，需要在此拼装
        partial: dict[int, dict[str, str]] = {}

        try:
            for chunk in raw:
                chunk_usage = _usage_of(chunk)
                if chunk_usage.total_tokens:
                    usage = chunk_usage  # 网关在末尾给累计值，直接覆盖而非相加

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
        except Exception as exc:  # noqa: BLE001
            _, detail = classify_failure(exc)
            logger.error("流式输出中断（已产出 %d 字正文）：%s", content_chars, _scrub(detail, self._pool))
            yield StreamEvent(kind="error", detail=_scrub(detail, self._pool))
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
            model_used, elapsed_ms, finish, usage.total_tokens, usage.reasoning_tokens,
            content_chars, reasoning_chars, len(partial),
        )
        if not content_chars and not partial and reasoning_chars:
            logger.warning(
                "流式正文为空但推理产出 %d 字：本次 max_tokens=%d 可能不足，"
                "推理吃光了预算（这是静默失败，不会报错）",
                reasoning_chars, self._effective_max_tokens(max_tokens),
            )
        if budget is not None:
            budget.add(usage)
        yield StreamEvent(kind="usage", usage=usage)

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
        effective_max = self._effective_max_tokens(max_tokens)
        attempt = 0
        last: Exception | None = None

        while True:
            cred = self._pool.acquire()
            # 切 Key 必须同步切模型：受限 Key 访问别的模型只会 401
            model_used = self._cfg.resolve_model(model or cred.model or self._default_model)
            payload: dict[str, Any] = {
                "model": model_used,
                "messages": list(messages),
                "max_tokens": effective_max,
                "stream": stream,
            }
            temp = temperature if temperature is not None else self._temperature
            if temp is not None:
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
                last = exc
                kind, detail = classify_failure(exc)
                detail = _scrub(detail, self._pool)

                if kind == "fatal":
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

    def _effective_max_tokens(self, max_tokens: int | None) -> int:
        return _clamp_max_tokens(max_tokens) if max_tokens else self._max_tokens

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
        allow_empty: bool,
    ) -> None:
        if content or calls:
            return
        if not usage.reasoning_tokens:
            return
        # 报本次实际生效的 max_tokens，不是配置默认值——这条日志的唯一用途
        # 就是定位「预算给少了」，数字失真等于没有这条日志。
        msg = (
            f"模型 {model} 正文为空，但消耗了 {usage.reasoning_tokens} 个推理 token"
            f"（finish_reason={finish or '未给出'}）：本次 max_tokens={max_tokens} "
            "很可能不足，推理吃光了输出预算"
        )
        if allow_empty:
            logger.warning("%s", msg)
            return
        logger.error("%s", msg)
        raise EmptyContentError(msg)


# ── 辅助 ───────────────────────────────────────────────────

def _normalize_base_url(base_url: str) -> str:
    """网关地址补 `/v1`。`.env` 里配的是根地址，而 SDK 会直接在 base_url
    后拼 `/chat/completions`，不补就会 404。"""
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise LLMError("LLM_BASE_URL 未配置")
    return url if url.endswith("/v1") else f"{url}/v1"


def _clamp_max_tokens(value: int) -> int:
    if value >= MIN_MAX_TOKENS:
        return value
    logger.warning(
        "max_tokens=%d 低于推理模型下限 %d，已抬升。GLM 5.2 的推理会先消耗预算，"
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


def reset_llm_client() -> None:
    global _client
    with _client_lock:
        _client = None
