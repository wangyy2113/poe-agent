"""验证 `core/llm.py`：离线断言 + 真实网关调用。

    python scripts/verify_llm.py --offline   # 只跑不联网的部分，无需凭证
    python scripts/verify_llm.py             # 含真实调用，需要可用凭证

在线部分要求 `.env` 里的 `LLM_API_KEY`（或 `LLM_API_KEYS`）是**可用**的 Key。
临时用别处的 Key 验证可以走环境变量，它优先于 `.env`：

    LLM_API_KEYS='<过期Key>,<可用Key>' python scripts/verify_llm.py

把已知过期的 Key 放在第一位，还能顺带验证多 Key 故障转移的真实路径。

离线部分覆盖失败分类、预算闸门、推理内容不可外发、max_tokens 下限；
在线部分对着网关实测非流式 / 流式 / tool calling，并检查推理模型特征。
密钥全程脱敏，不会输出完整值。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import APIConnectionError, APIStatusError, APITimeoutError  # noqa: E402

from core import llm as m  # noqa: E402
from core.model_policy import DEEPSEEK_MODEL
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, ok: bool, note: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [通过] {label}" + (f" —— {note}" if note else ""))
    else:
        FAIL += 1
        print(f"  [失败] {label}" + (f" —— {note}" if note else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


class _FakeStatusError(APIStatusError):
    """构造带指定状态码与消息的错误，用于验证分类逻辑。"""

    def __init__(self, status: int, message: str) -> None:
        self.status_code = status
        self.message = message
        Exception.__init__(self, message)


# ── 离线 ───────────────────────────────────────────────────

def offline() -> None:
    section("1. 失败分类（决定重试 / 冷却换 Key / 直接失败）")
    cases = [
        (_FakeStatusError(500, "internal error"), "transient", "5xx 只重试不降级 Key"),
        (_FakeStatusError(503, "upstream unavailable"), "transient", ""),
        (_FakeStatusError(429, "Rate limit reached"), "transient", "限流 → 重试"),
        (_FakeStatusError(429, "You exceeded your current quota"), "budget", "额度耗尽 → 冷却 30 分钟"),
        (_FakeStatusError(402, "payment required"), "budget", ""),
        (_FakeStatusError(401, "invalid api key"), "auth", "鉴权 → 冷却 60 分钟"),
        (_FakeStatusError(403, "forbidden"), "auth", ""),
        (_FakeStatusError(404, "model_not_found"), "auth", "模型无权访问 → 换 Key 时同步换模型"),
        (_FakeStatusError(400, "messages: invalid role"), "fatal", "请求有问题，重试无用"),
        # 实测：网关对过期 Key 返回的是 400 而非 401（type=expired_key）。
        # 若按「400 一律 fatal」处理，就不会故障转移到下一个 Key。
        (_FakeStatusError(400, "Authentication Error - Expired Key. Key Expiry time "
                               "2026-07-31 15:59:00+00:00"), "auth",
         "过期 Key 走 400，必须仍能识别为鉴权类并换 Key"),
        (APITimeoutError(request=None), "transient", ""),  # type: ignore[arg-type]
    ]
    for exc, expect, note in cases:
        kind, _ = m.classify_failure(exc)
        check(f"{type(exc).__name__} {getattr(exc, 'status_code', '')} → {expect}",
              kind == expect, note or f"实得 {kind}")

    check("未知异常 → fatal", m.classify_failure(ValueError("x"))[0] == "fatal")

    section("2. 429 的两义性（最容易踩的分类错误）")
    limit_kind, _ = m.classify_failure(_FakeStatusError(429, "Rate limit reached for deepseek-v4-1-flash-1017"))
    quota_kind, _ = m.classify_failure(_FakeStatusError(429, "insufficient_quota"))
    check("同为 429，限流与额度耗尽被区分开", limit_kind != quota_kind,
          f"限流={limit_kind} 额度={quota_kind}")

    section("3. 推理内容不可外发（类型层面的约束）")
    for kind in ("thinking", "usage", "tool_calls", "error"):
        try:
            m.StreamEvent(kind=kind, text="推理内容")  # type: ignore[arg-type]
            check(f"kind={kind} 携带 text 被拒绝", False, "竟然允许了")
        except ValueError:
            check(f"kind={kind} 携带 text 被拒绝", True)
    check("kind=content 可携带 text", m.StreamEvent(kind="content", text="正文").text == "正文")
    check("LLMResponse 只留推理字符数、不留推理正文",
          "reasoning_chars" in m.LLMResponse.__dataclass_fields__
          and not any("reasoning_content" in f for f in m.LLMResponse.__dataclass_fields__))

    section("4. max_tokens 下限（推理吃光预算 → 正文为空且不报错）")
    check("64 被抬到下限", m._clamp_max_tokens(64) == m.MIN_MAX_TOKENS,
          f"→ {m._clamp_max_tokens(64)}")
    check("8192 保持原值", m._clamp_max_tokens(8192) == 8192)
    check("下限高于实测极简任务的 117 token 消耗", m.MIN_MAX_TOKENS > 117 * 4)

    section("5. Token 预算（含推理，且不重复计）")
    u = m.Usage(prompt_tokens=10, completion_tokens=117, reasoning_tokens=114, total_tokens=127)
    check("total 未把推理加第二遍", u.total_tokens == 127, f"推理占比 {u.reasoning_ratio:.1%}")
    b = m.TurnBudget(limit=200)
    b.add(u)
    check("累计后剩余正确", b.remaining == 73, f"remaining={b.remaining}")
    try:
        b.add(u)
        check("超限抛 TokenBudgetExceeded", False, "未抛出")
    except m.TokenBudgetExceeded:
        check("超限抛 TokenBudgetExceeded", True)
    check("limit=0 视为不限", m.TurnBudget(limit=0).remaining == 0)

    section("6. 凭证池：粘性选择 + 分级冷却 + 脱敏")
    pool = m.CredentialPool(
        ["sk-aaaaaaaaaaaaaaaaaaaa", "sk-bbbbbbbbbbbbbbbbbbbb"],
        budget_cooldown=1800, auth_cooldown=3600,
    )
    first = pool.acquire()
    check("粘性：始终先取优先级最高的", first.index == 0)
    check("再次取仍是同一个（不轮询）", pool.acquire().index == 0)
    pool.penalize(first, "auth", "invalid api key")
    second = pool.acquire()
    check("首个冷却后转到下一个", second.index == 1)
    check("换 Key 后仍为同一 DeepSeek 模型", all(item["model"] == DEEPSEEK_MODEL for item in pool.snapshot()))
    pool.penalize(second, "budget", "quota exceeded")
    try:
        pool.acquire()
        check("全部冷却时抛 AllCredentialsUnavailable", False, "未抛出")
    except m.AllCredentialsUnavailable as exc:
        check("全部冷却时抛 AllCredentialsUnavailable", True)
        check("异常消息不含完整密钥", "sk-aaaaaaaaaaaaaaaaaaaa" not in str(exc))
    snap = pool.snapshot()
    check("冷却时长分级：鉴权 60 分钟 > 额度 30 分钟",
          snap[1]["cooldown_seconds"] < snap[0]["cooldown_seconds"],
          f"auth={snap[0]['cooldown_seconds']}s budget={snap[1]['cooldown_seconds']}s")
    check("快照里密钥已脱敏",
          all("sk-aaaaaaaaaaaaaaaaaaaa" not in json.dumps(s) for s in snap),
          snap[0]["key"])
    leaked = m._scrub("boom with sk-aaaaaaaaaaaaaaaaaaaa inside", pool)
    check("_scrub 兜底替换混入消息的密钥", "sk-aaaaaaaaaaaaaaaaaaaa" not in leaked, leaked)

    section("7. base_url 归一化（不补 /v1 会 404）")
    check("根地址补 /v1", m._normalize_base_url("https://gw.example.com") == "https://gw.example.com/v1")
    check("已带 /v1 不重复补", m._normalize_base_url("https://gw.example.com/v1/") == "https://gw.example.com/v1")
    try:
        m._normalize_base_url("")
        check("空地址明确报错", False, "未抛出")
    except m.LLMError:
        check("空地址明确报错", True)


# ── 在线 ───────────────────────────────────────────────────

def online(client: m.LLMClient | None = None) -> None:
    section("8. 真实网关：非流式")
    client = client or m.LLMClient()
    print(f"  模型={client.default_model} 凭证={len(client.credential_status())}")
    for s in client.credential_status():
        print(f"    - {s['key']} 模型={s['model']} 健康={s['healthy']}")

    budget = m.TurnBudget(limit=int(get_config().get("llm.max_tokens_per_turn", 60000)))
    resp = client.chat(
        [{"role": "user", "content": "只回答两个字：收到"}],
        budget=budget,
    )
    print(f"  正文={resp.content!r} 耗时={resp.elapsed_ms}ms")
    print(f"  token: 输入{resp.usage.prompt_tokens} 输出{resp.usage.completion_tokens} "
          f"(推理{resp.usage.reasoning_tokens}) 合计{resp.usage.total_tokens}")
    check("拿到非空正文", bool(resp.content), resp.content[:40])
    check("只调用固定 DeepSeek 模型", resp.model == DEEPSEEK_MODEL)
    check("用量包含有效总量且不重复累计推理", resp.usage.total_tokens > 0
          and 0 <= resp.usage.reasoning_tokens <= resp.usage.completion_tokens)
    check("预算已累计", budget.used.total_tokens == resp.usage.total_tokens)
    check("响应不携带推理正文", not hasattr(resp, "reasoning_content"),
          f"仅记录字符数 {resp.reasoning_chars}")

    section("9. 真实网关：流式（按句缓冲的前提）")
    events: list[m.StreamEvent] = list(
        client.stream([{"role": "user", "content": "用一句话说明流放之路的赛季机制是什么。"}])
    )
    kinds = [e.kind for e in events]
    content = "".join(e.text for e in events if e.kind == "content")
    thinking = kinds.count("thinking")
    print(f"  事件 {len(events)} 个：正文分片 {kinds.count('content')}，thinking {thinking}")
    print(f"  正文：{content[:80]}...")
    check("产出多个正文分片（足够按句缓冲）", kinds.count("content") > 1)
    check("推理事件最多一次，模型不返回推理时允许为零", thinking <= 1)
    check("thinking 事件不含任何文本",
          all(not e.text for e in events if e.kind == "thinking"))
    check("末尾给出 usage（含推理）", kinds[-1] == "usage" and events[-1].usage is not None,
          f"合计 {events[-1].usage.total_tokens} token" if events[-1].usage else "")
    check("推理分片先于正文",
          "thinking" not in kinds[kinds.index("content"):] if "content" in kinds else False)

    section("10. 真实网关：tool calling")
    tools = [{
        "type": "function",
        "function": {
            "name": "kb_search",
            "description": "检索 POE1 知识库。任何事实性问题都必须先调用它，不得凭记忆回答。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "category": {"type": "string", "description": "实体分类，如 unique_item"},
                },
                "required": ["query"],
            },
        },
    }]
    tool_resp = client.chat(
        [
            {"role": "system", "content": "你只能依据 kb_search 的返回作答，禁止凭记忆回答。"},
            {"role": "user", "content": "猎首（Headhunter）的词缀是什么？"},
        ],
        tools=tools,
    )
    check("模型发起了工具调用", len(tool_resp.tool_calls) > 0,
          f"finish={tool_resp.finish_reason}")
    if tool_resp.tool_calls:
        continuation = [
            {"role": "user", "content": "调用 kb_search 查猎首，然后只复述工具结果。"},
            {"role": "assistant", "content": tool_resp.content or None,
             "tool_calls": [{"id": tc.id, "type": "function", "function": {
                 "name": tc.name, "arguments": tc.arguments,
             }} for tc in tool_resp.tool_calls]},
            *[{"role": "tool", "tool_call_id": tc.id,
               "content": "测试知识库返回：猎首是传奇腰带。"} for tc in tool_resp.tool_calls],
        ]
        followup = client.chat(continuation)
        check("工具结果可续答且保持固定模型", bool(followup.content)
              and followup.model == DEEPSEEK_MODEL)
        tool_events = list(client.stream(
            [{"role": "user", "content": "必须先调用 kb_search 查询猎首。"}], tools=tools,
        ))
        streamed_calls = [tc for e in tool_events for tc in e.tool_calls]
        check("流式工具参数拼接为合法 JSON", bool(streamed_calls) and all(
            isinstance(json.loads(tc.arguments), dict) for tc in streamed_calls
        ))
        check("流式只报告固定模型", all(
            e.model == DEEPSEEK_MODEL for e in tool_events if e.kind == "usage"
        ))
        call = tool_resp.tool_calls[0]
        print(f"  调用 {call.name}({call.arguments})")
        check("工具名正确", call.name == "kb_search")
        try:
            args = json.loads(call.arguments)
            check("参数是合法 JSON", isinstance(args, dict), str(args))
        except json.JSONDecodeError as exc:
            check("参数是合法 JSON", False, str(exc))

    section("11. max_tokens 给不足时不把空正文或截断响应当成功")
    # 绕过 clamp 直接构造极小预算，验证告警/异常路径真的会触发
    original = m.MIN_MAX_TOKENS
    try:
        m.MIN_MAX_TOKENS = 16
        tiny = m.LLMClient()
        try:
            short = tiny.chat(
                [{"role": "user", "content": "详细说明流放之路的抗性上限机制。"}],
                max_tokens=16,
            )
            # Short content is usable only when the upstream finished normally.
            check("极小预算未静默返回空正文或截断响应",
                  bool(short.content.strip()) and short.finish_reason == "stop",
                  f"正文字符数={len(short.content)}")
        except (m.EmptyContentError, m.IncompleteResponseError) as exc:
            check("极小预算未静默返回空正文或截断响应", True, str(exc)[:100])
        except m.LLMError as exc:
            check("极小预算未静默返回空正文", False, f"抛的是别的 LLMError：{exc}")
    finally:
        m.MIN_MAX_TOKENS = original


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="跳过真实网关调用")
    args = ap.parse_args()

    cfg = get_config()
    setup_logging(cfg.logs_dir, "INFO", 14)

    offline()
    if args.offline:
        print("\n（已跳过真实网关调用）")
    else:
        try:
            online()
        except Exception as exc:  # noqa: BLE001
            global FAIL
            FAIL += 1
            print(f"\n  [失败] 在线验证中断：{type(exc).__name__}: {exc}")

    print(f"\n{'=' * 68}")
    print(f"结果：{PASS} 通过，{FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
