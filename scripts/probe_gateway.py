#!/usr/bin/env python3
"""探测 LLM 网关能力（对应 tasks.md 任务 0.6）。

验证 poe-agent 架构所依赖的网关能力是否真实可用：
  1. 可用模型清单（目标模型是否存在、有无 embedding / rerank 模型）
  2. 基础 chat completion
  3. tool calling —— 编排器的地基（需求 4.1-4.3）
  4. 多轮 tool calling —— 工具结果回传后能否继续
  5. 流式 SSE —— 断言绑定校验按句缓冲的前提（需求 5.9）
  6. embeddings 及其输出维度 —— 决定 vector.enabled 与建表维度
  7. rerank

凭证与网关地址来源优先级：环境变量 → ~/.claude/settings.json → 本项目 .env
  → SIBLING_ENV_PATH 指向的 .env。网关地址不写死在代码里（属内部基础设施）。
Token 一律脱敏输出，完整值不会出现在任何位置。

用法：
    python3 scripts/probe_gateway.py
    python3 scripts/probe_gateway.py --model glm-5.2
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: 网关地址**不写死在代码里**——它是内部基础设施，不进版本库。
#: 由 `.env` 的 `LLM_BASE_URL`（或 `--base-url`）提供，缺失即明确报错，
#: 不给一个默认值让脚本静默连到错误的地方。
DEFAULT_BASE_URL = ""
DEFAULT_MODEL = "glm-5.2"
TIMEOUT = 120


# ── 输出helpers ────────────────────────────────────────────────

class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))

    def summary(self) -> int:
        print("\n" + "=" * 72)
        passed = sum(1 for _, ok, _ in self.results if ok)
        total = len(self.results)
        print(f"结果：{passed}/{total} 项通过")
        failed = [n for n, ok, _ in self.results if not ok]
        if failed:
            print("未通过：" + ", ".join(failed))
        return 0 if not failed else 1


def mask(token: str) -> str:
    """脱敏标签，永不输出完整 token：只留 前7位…后4位。"""
    if not token:
        return "<empty>"
    return f"{token[:7]}…{token[-4:]}" if len(token) > 14 else f"{token[:4]}…"


def section(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


# ── 凭证解析 ──────────────────────────────────────────────────

def _split_keys(value: str) -> list[str]:
    """解析 `key[:model]` 逗号分隔格式，只取 key 部分。"""
    out = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        # key 本身不含冒号；`:model` 后缀才含
        cand = item.split(":", 1)[0] if not item.startswith("sk-") or ":" in item else item
        cand = item if item.startswith("sk-") and ":" not in item else item.split(":")[0]
        if cand.startswith("sk-"):
            out.append(cand)
    return out


def collect_candidates() -> tuple[str, list[tuple[str, str]]]:
    """收集所有候选凭证，返回 (base_url, [(token, 来源), ...])。不打印 token 值。"""
    base_url = ""
    cands: list[tuple[str, str]] = []
    seen: set[str] = set()

    def push(token: str, origin: str) -> None:
        if token and token not in seen:
            seen.add(token)
            cands.append((token, origin))

    # 1) 环境变量
    for var in ("LLM_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if os.environ.get(var):
            push(os.environ[var], f"env:{var}")
    base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL") or base_url

    # 2) ~/.claude/settings.json
    settings = Path.home() / ".claude" / "settings.json"
    if settings.is_file():
        try:
            env = (json.loads(settings.read_text(encoding="utf-8")).get("env") or {})
            for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
                if env.get(var):
                    push(env[var], f"~/.claude/settings.json:{var}")
            base_url = base_url or (env.get("ANTHROPIC_BASE_URL") or "")
        except (json.JSONDecodeError, OSError):
            pass

    # 3) 本项目自己的 .env —— 按项目约定，密钥与网关地址就放这里（已 gitignore）
    base_url = _scan_dotenv(Path(".env"), ".env", push, base_url)

    # 4) 邻近项目的 .env（含多 Key）。路径由环境变量给出，不硬编码——
    #    硬编码本机绝对路径既违反项目的跨平台规则，也会把本地目录结构写进版本库。
    #    用法：SIBLING_ENV_PATH=/path/to/other-project/.env python scripts/probe_gateway.py
    sibling = os.environ.get("SIBLING_ENV_PATH", "")
    if sibling:
        base_url = _scan_dotenv(Path(sibling), "SIBLING_ENV_PATH", push, base_url)

    return (base_url or DEFAULT_BASE_URL).rstrip("/"), cands


def _scan_dotenv(path: Path, label: str, push, base_url: str) -> str:  # noqa: ANN001
    """从一个 .env 文件里收集候选凭证与网关地址，返回（可能更新过的）base_url。

    只读不写，任何解析失败都静默跳过——探测脚本不该因为某个可选来源格式怪异而挂掉。
    """
    if not path.is_file():
        return base_url
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return base_url
    for line in lines:
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        if not v:
            continue
        if k in ("LLM_API_KEYS", "ANTHROPIC_API_KEYS"):
            for idx, key in enumerate(_split_keys(v)):
                push(key, f"{label}:{k}[{idx}]")
        elif k in ("LLM_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            push(v, f"{label}:{k}")
        elif k in ("LLM_BASE_URL", "ANTHROPIC_BASE_URL"):
            base_url = base_url or v
    return base_url


def pick_working_credential(base_url: str, cands: list[tuple[str, str]]) -> tuple[str, str]:
    """逐个试用候选凭证，返回第一个可用的 (token, 来源)。全部失败返回 ('','')。"""
    print(f"\n  发现 {len(cands)} 个候选凭证，逐个验活：")
    for token, origin in cands:
        try:
            request(base_url, token, "/v1/models", method="GET")
            print(f"    [可用]   {mask(token):22} ← {origin}")
            return token, origin
        except Exception as e:
            reason = describe_error(e)
            short = "已过期" if "expired" in reason.lower() else reason[:70]
            print(f"    [不可用] {mask(token):22} ← {origin}  ({short})")
    return "", ""


# ── HTTP ─────────────────────────────────────────────────────

def request(
    base_url: str, token: str, path: str, payload: dict | None = None,
    method: str = "POST", stream: bool = False,
):
    """发请求。stream=True 时返回原始 response 对象供逐行读取。"""
    url = f"{base_url}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    if stream:
        req.add_header("Accept", "text/event-stream")
    resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    if stream:
        return resp
    return json.loads(resp.read().decode("utf-8"))


def describe_error(exc: Exception) -> str:
    """把异常压成一行可读信息，并抹掉可能回显的密钥。"""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        body = re.sub(r"sk-[A-Za-z0-9_\-]{6,}", "sk-***", body)
        return f"HTTP {exc.code} {body}".strip()
    return f"{type(exc).__name__}: {str(exc)[:200]}"


# ── 各项探测 ──────────────────────────────────────────────────

def probe_models(base_url, token, rpt, target_model):
    section("1. 可用模型清单  GET /v1/models")
    try:
        data = request(base_url, token, "/v1/models", method="GET")
    except Exception as e:
        rpt.add("models 列表", False, describe_error(e))
        return [], []

    ids = sorted(m.get("id", "") for m in data.get("data", []))
    rpt.add("models 列表", True, f"共 {len(ids)} 个模型")

    print(f"\n  全部模型（{len(ids)}）：")
    for i in ids:
        print(f"    - {i}")

    rpt.add(
        f"目标模型 {target_model} 存在",
        target_model in ids,
        "命中" if target_model in ids else "未在清单中",
    )

    emb_pat = re.compile(r"embed|bge|gte|text-embedding|m3e|conan|qwen3?-?embedding", re.I)
    rr_pat = re.compile(r"rerank|reranker", re.I)
    embs = [i for i in ids if emb_pat.search(i) and not rr_pat.search(i)]
    rrs = [i for i in ids if rr_pat.search(i)]

    print(f"\n  疑似 embedding 模型：{embs or '（无）'}")
    print(f"  疑似 rerank 模型：{rrs or '（无）'}")
    return embs, rrs


def probe_chat(base_url, token, rpt, model):
    section("2. 基础 chat completion")
    # GLM 5.2 是推理模型：max_tokens 必须给足，否则推理 token 吃光预算、正文为空
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "只回答两个字：收到"}],
        "max_tokens": 2048,
    }
    try:
        t0 = time.time()
        data = request(base_url, token, "/v1/chat/completions", payload)
        el = time.time() - t0
    except Exception as e:
        rpt.add("chat completion", False, describe_error(e))
        return False
    msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    usage = data.get("usage") or {}
    rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
    rpt.add("chat completion", bool(content), f"{el:.1f}s, 回复={content[:40]!r}")
    print(f"    token 用量: {usage}")
    if rt:
        rpt.add(
            "推理模型特征（reasoning_tokens）", True,
            f"推理 {rt} tokens，正文 {len(content)} 字"
            + ("，且返回 reasoning_content 字段" if reasoning else "，未单列 reasoning_content"),
        )
        print("    注意：max_tokens 必须留足推理预算，否则正文为空")
    return bool(content)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "kb_search",
            "description": "在 POE1 知识库中检索条目。任何游戏机制问题都必须先调用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词"},
                    "category": {
                        "type": "string",
                        "enum": ["unique_item", "skill", "mod", "boss"],
                        "description": "条目分类",
                    },
                },
                "required": ["query"],
            },
        },
    }
]


def probe_tool_calling(base_url, token, rpt, model):
    section("3. tool calling（编排器地基，需求 4.1-4.3）")
    messages = [
        {
            "role": "system",
            "content": "你是 POE1 知识助手。禁止依赖记忆作答，任何游戏机制问题必须先调用 kb_search 检索。",
        },
        {"role": "user", "content": "猎首（Headhunter）这件装备有哪些词缀？"},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 4096,
    }
    try:
        data = request(base_url, token, "/v1/chat/completions", payload)
    except Exception as e:
        rpt.add("tool calling", False, describe_error(e))
        return None, None

    msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    calls = msg.get("tool_calls") or []
    if not calls:
        rpt.add(
            "tool calling", False,
            f"未返回 tool_calls，直接作答={((msg.get('content') or '')[:60])!r}",
        )
        return None, None

    fn = calls[0].get("function", {}) or {}
    name = fn.get("name")
    raw_args = fn.get("arguments") or "{}"
    try:
        args = json.loads(raw_args)
        args_ok = True
    except json.JSONDecodeError:
        args, args_ok = {}, False

    rpt.add("tool calling", name == "kb_search", f"调用 {name}，共 {len(calls)} 个")
    rpt.add("tool 参数为合法 JSON", args_ok, f"args={args}")
    return messages + [msg], calls


def probe_multi_turn(base_url, token, rpt, model, messages, calls):
    section("4. 多轮 tool calling（回传工具结果后能否继续）")
    if not messages or not calls:
        rpt.add("多轮 tool calling", False, "前置 tool calling 未通过，跳过")
        return
    fake_result = json.dumps(
        {
            "results": [
                {
                    "entity_id": "unique_item:headhunter",
                    "name_zh": "猎首",
                    "name_en": "Headhunter",
                    "mods": [
                        "+(40-50) 力量",
                        "+(40-55) 最大生命",
                        "击杀稀有怪物时，获得其词缀，持续 20 秒",
                    ],
                    "url": "https://poedb.tw/cn/Headhunter",
                    "source": "POEDB",
                }
            ]
        },
        ensure_ascii=False,
    )
    convo = list(messages) + [
        {
            "role": "tool",
            "tool_call_id": calls[0].get("id"),
            "content": fake_result,
        }
    ]
    payload = {
        "model": model,
        "messages": convo,
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 4096,
    }
    try:
        data = request(base_url, token, "/v1/chat/completions", payload)
    except Exception as e:
        rpt.add("多轮 tool calling", False, describe_error(e))
        return
    msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    content = (msg.get("content") or "").strip()
    ok = bool(content) and "猎首" in content or "Headhunter" in content
    rpt.add("多轮 tool calling", bool(content), f"基于工具结果生成 {len(content)} 字")
    if content:
        print(f"\n  ── 模型回复（前 400 字）──\n{content[:400]}\n")
    # 附带观察：是否引用了工具结果里的来源
    if content:
        cited = "POEDB" in content or "poedb" in content.lower()
        rpt.add("引用了工具结果中的来源", cited,
                "回复中出现 POEDB" if cited else "未提及来源（需靠提示词与出口校验强制）")


def probe_streaming(base_url, token, rpt, model):
    section("5. 流式 SSE（按句校验的前提，需求 5.9）")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "用三句话介绍流放之路的赛季机制。"}],
        "stream": True,
        "max_tokens": 4096,
    }
    try:
        resp = request(base_url, token, "/v1/chat/completions", payload, stream=True)
    except Exception as e:
        rpt.add("流式 SSE", False, describe_error(e))
        return
    chunks = 0          # 正文增量数
    reason_chunks = 0   # 推理增量数（推理模型会先吐这个）
    text_parts: list[str] = []
    first_content_after = -1  # 正文首个分片之前收到了多少推理分片
    try:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta", {}) or {}
            if delta.get("reasoning_content"):
                reason_chunks += 1
            piece = delta.get("content")
            if piece:
                if first_content_after < 0:
                    first_content_after = reason_chunks
                chunks += 1
                text_parts.append(piece)
    finally:
        resp.close()

    full = "".join(text_parts)
    rpt.add("流式 SSE", chunks >= 2, f"正文 {chunks} 个增量分片，共 {len(full)} 字")
    if reason_chunks:
        rpt.add(
            "推理流与正文流可区分", True,
            f"推理 {reason_chunks} 分片（正文前 {first_content_after} 个），"
            f"字段 reasoning_content 与 content 分离",
        )
        print("    按句缓冲只需处理 delta.content，reasoning_content 不参与出口校验")
    if full:
        print(f"\n  ── 流式拼接结果（前 200 字）──\n{full[:200]}\n")
    rpt.add(
        "增量粒度足够按句缓冲", chunks >= 5,
        f"{chunks} 个正文分片" + ("" if chunks >= 5 else "（分片过少，可能是伪流式）"),
    )


def probe_embeddings(base_url, token, rpt, candidates):
    section("6. embeddings（决定 vector.enabled 与建表维度）")
    if not candidates:
        rpt.add("embeddings 可用", False, "模型清单中没有 embedding 模型")
        return None
    for model in candidates:
        payload = {"model": model, "input": ["猎首", "怪物死亡后偷取词缀的头盔"]}
        try:
            data = request(base_url, token, "/v1/embeddings", payload)
        except Exception as e:
            print(f"    {model}: {describe_error(e)}")
            continue
        vecs = data.get("data") or []
        if not vecs:
            print(f"    {model}: 返回空结果")
            continue
        dim = len(vecs[0].get("embedding") or [])
        rpt.add("embeddings 可用", dim > 0, f"模型={model}，维度={dim}，返回 {len(vecs)} 条")
        return {"model": model, "dim": dim}
    rpt.add("embeddings 可用", False, "所有候选模型均调用失败")
    return None


def probe_rerank(base_url, token, rpt, candidates):
    section("7. rerank")
    if not candidates:
        rpt.add("rerank 可用", False, "模型清单中没有 rerank 模型（非阻塞，重排本身可关闭）")
        return None
    for model in candidates:
        payload = {
            "model": model,
            "query": "怪物死亡后偷取词缀的头盔",
            "documents": ["猎首：击杀稀有怪物时获得其词缀", "生命之泉：回复生命", "感电effect"],
            "top_n": 2,
        }
        for path in ("/v1/rerank", "/rerank", "/v2/rerank"):
            try:
                data = request(base_url, token, path, payload)
            except Exception as e:
                print(f"    {model} @ {path}: {describe_error(e)}")
                continue
            results = data.get("results") or []
            if results:
                rpt.add("rerank 可用", True, f"模型={model}，端点={path}，返回 {len(results)} 条")
                return {"model": model, "path": path}
    rpt.add("rerank 可用", False, "所有候选与端点均失败（非阻塞）")
    return None


# ── main ─────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="探测 LLM 网关能力（tasks 0.6）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"目标模型，默认 {DEFAULT_MODEL}")
    ap.add_argument("--base-url", default="", help="覆盖网关地址")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    base_url, cands = collect_candidates()
    if args.base_url:
        base_url = args.base_url.rstrip("/")

    print("=" * 72)
    print("LLM 网关能力探测（poe-agent tasks 0.6）")
    print("=" * 72)

    if not base_url:
        print("\n未提供网关地址。它是内部基础设施，不写在代码里，请二选一：")
        print("  1) 在 .env 里配置 LLM_BASE_URL=<网关地址>")
        print("  2) python3 scripts/probe_gateway.py --base-url <网关地址>")
        return 2

    print(f"  网关     : {base_url}")
    print(f"  目标模型 : {args.model}")

    if not cands:
        print("\n未找到任何候选凭证。请设置环境变量后重试：")
        print("  export LLM_API_KEY=<virtual key>")
        return 2

    token, origin = pick_working_credential(base_url, cands)
    if not token:
        print("\n所有候选凭证均不可用（多为已过期）。请提供有效的 virtual key：")
        print("  export LLM_API_KEY=<virtual key>")
        print("  python3 scripts/probe_gateway.py")
        return 2

    print(f"\n  采用凭证 : {mask(token)}  ← {origin}")

    rpt = Report()
    embs, rrs = probe_models(base_url, token, rpt, args.model)
    if not probe_chat(base_url, token, rpt, args.model):
        print("\n基础 chat 不通，后续测试无意义，提前结束。")
        return rpt.summary()

    messages, calls = probe_tool_calling(base_url, token, rpt, args.model)
    probe_multi_turn(base_url, token, rpt, args.model, messages, calls)
    probe_streaming(base_url, token, rpt, args.model)
    emb_info = probe_embeddings(base_url, token, rpt, embs)
    probe_rerank(base_url, token, rpt, rrs)

    code = rpt.summary()

    section("对 poe-agent 设计的结论")
    tool_ok = any(n == "tool calling" and ok for n, ok, _ in rpt.results)
    stream_ok = any(n == "流式 SSE" and ok for n, ok, _ in rpt.results)
    print(f"  tool calling 可用 : {'是' if tool_ok else '否 → 编排器需改用结构化输出驱动，可靠性下降'}")
    print(f"  流式可用          : {'是' if stream_ok else '否 → 按句缓冲的出口校验无法实现'}")
    if emb_info:
        print(f"  向量通道          : 可启用，vector.enabled=true，chunk_vec 维度设为 {emb_info['dim']}")
        print(f"  EMBEDDING_MODEL   : {emb_info['model']}")
    else:
        print("  向量通道          : 不可用，vector.enabled=false，走 BM25 + 实体精确 + search_keywords")
    return code


if __name__ == "__main__":
    sys.exit(main())
