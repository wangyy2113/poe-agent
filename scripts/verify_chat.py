"""验证 `/chat` 的两条路径与出口闸门。

    python scripts/verify_chat.py --gap-only   # 只测缺据路径，**不需要可用凭证**
    python scripts/verify_chat.py              # 全部（需要可用 LLM 凭证）

缺据路径不调用 LLM：检索判定依据不足时，缺据声明由代码生成。这条路径必须
完全可预测，所以刻意不经模型（见 orchestrator/loop.py）。

需要服务已启动：python main.py
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api import normalize_base_path  # noqa: E402
from core.config import get_config
from core.model_policy import DEEPSEEK_MODEL  # noqa: E402

PASS = FAIL = 0


def check(label: str, ok: bool, note: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [通过] {label}" + (f" —— {note}" if note else ""))
    else:
        FAIL += 1
        print(f"  [失败] {label}" + (f" —— {note}" if note else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


def post(base: str, path: str, payload: dict, timeout: float = 300.0) -> dict:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def post_status(base: str, path: str, payload: dict, timeout: float = 60.0) -> tuple[int, dict]:
    """POST 并把错误码也当成正常结果返回——校验失败契约时要断言状态码。"""
    try:
        return 200, post(base, path, payload, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except Exception:  # noqa: BLE001
            return exc.code, {}


def sse(base: str, path: str, payload: dict, timeout: float = 300.0) -> list[dict]:
    """消费 SSE 流，返回全部事件。"""
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    out: list[dict] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                out.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                continue
    return out


def _summarize(kinds: list[str]) -> str:
    """把帧类型序列压成 `meta thinking sentence×6 citations done` 这种形式。"""
    parts: list[str] = []
    for k in kinds:
        if parts and parts[-1].split("×")[0] == k:
            n = int(parts[-1].split("×")[1]) if "×" in parts[-1] else 1
            parts[-1] = f"{k}×{n + 1}"
        else:
            parts.append(k)
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap-only", action="store_true", help="只测不需要凭证的缺据路径")
    ap.add_argument("--host", default=None)
    args = ap.parse_args()

    cfg = get_config()
    base_path = normalize_base_path(cfg.get("server.base_path", "/poe_agent"))
    host = args.host or (
        f"http://{cfg.get('server.host', '127.0.0.1')}:{cfg.get('server.port', 8787)}"
    )
    base = host + base_path
    print(f"服务地址：{base}")

    section("1. 缺据路径（不调用 LLM）")
    for q in (
        "流放之路2 的升华机制是什么",
        "请你猜一下下个赛季会加什么新机制",
    ):
        try:
            d = post(base, "/chat", {"message": q})
        except urllib.error.URLError as exc:
            check(f"请求「{q}」", False, f"{exc}（服务没起来？）")
            return 1
        gap = d["evidence_gap"]
        print(f"\n  问：{q}")
        print(f"  evidence_gap={gap}  blocked_claims={d['blocked_claims']}  "
              f"tokens={d['tokens'].get('total', 0)}")
        print(f"  答（前 160 字）：{d['answer'][:160].replace(chr(10), ' ')}")
        check(f"「{q[:14]}…」判为依据不足", gap)
        check("  未消耗 LLM token（缺据路径不经模型）",
              d["tokens"].get("total", 0) == 0)
        # POE2 属于服务范围外：应明确拒答，不需要假称“知识库查不到”。
        check("  缺据或越界原因明确", (
            "只覆盖" in d["answer"] and "流放之路 1" in d["answer"]
            if "流放之路2" in q else "查不到" in d["answer"]
        ))
        check("  缺据声明给出查证途径", "poedb.tw" in d["answer"])
        check("  未伪造引用", not d["citations"])

    section("2. 装备解析与分析（/item，同样不调用 LLM）")
    fixtures = REPO_ROOT / "tests" / "unit" / "fixtures" / "items"
    for name, expect in (
        ("rare_amulet_zh.txt", {"class": "项链", "matched": 5, "total": 6}),
        ("rare_bow_fractured_en.txt", {"class": "Bows", "matched": 5, "total": 5}),
        ("unique_nebulis_advanced_en.txt", {"class": "Sceptres", "advanced": True}),
    ):
        path = fixtures / name
        if not path.is_file():
            check(f"{name} 存在", False, "fixture 缺失")
            continue
        try:
            d = post(base, "/item", {"text": path.read_text(encoding="utf-8")})
        except urllib.error.HTTPError as exc:
            check(f"{name} 分析", False, f"HTTP {exc.code}：{exc.read()[:200]!r}")
            continue
        print(f"\n  {name}：{d['rarity']} {d['name']}｜类别 {d['item_class']}"
              f"｜已定档 {d['matched_count']}/{len(d['mods'])}")
        check(f"  {name} 类别解析正确", d["item_class"] == expect["class"],
              f"实得 {d['item_class']!r}")
        if "matched" in expect:
            check(f"  {name} 定档数符合预期",
                  d["matched_count"] == expect["matched"] and len(d["mods"]) == expect["total"],
                  f"实得 {d['matched_count']}/{len(d['mods'])}")
        if expect.get("advanced"):
            check(f"  {name} 识别为进阶复制", d["advanced_copy"])
            from_text = [m for m in d["mods"] if m["tier_source"] == "item_text"]
            check("  进阶复制的 T 级标注为游戏原文", bool(from_text),
                  f"{len(from_text)} 条")
        # 未定档的词缀必须给出原因，且不得出现推测的 T 级（需求 7.9）
        unmatched = [m for m in d["mods"] if m["tier"] is None]
        check(f"  {name} 未定档词缀都带原因",
              all(m["reason"] for m in unmatched), f"{len(unmatched)} 条未定档")
        check(f"  {name} 未定档词缀不给 T 级",
              all(m["tier_label"] == "未匹配" for m in unmatched))
        check(f"  {name} 不给市场价格", d["market_available"] is False)
        check(f"  {name} 三项价值判断均为数据不足",
              len(d["judgements"]) == 3
              and all("数据不足" in j["verdict"] for j in d["judgements"]))

    print()
    bad = post_status(base, "/item", {"text": "今天天气不错\n随便贴点东西"})
    check("非装备文本返回 422", bad[0] == 422, f"HTTP {bad[0]}")
    detail = (bad[1] or {}).get("detail") or {}
    check("解析失败带原因与期望格式",
          bool(detail.get("reason")) and "Item Class:" in str(detail.get("expected_sample")))

    if args.gap_only:
        print("\n（--gap-only，跳过需要凭证的部分）")
        print(f"\n{'=' * 74}\n结果：{PASS} 通过，{FAIL} 失败")
        return 0 if FAIL == 0 else 1

    section("3. 有据路径（需要可用 LLM 凭证）")
    q = "猎首（Headhunter）有哪些词缀？"
    try:
        d = post(base, "/chat", {"message": q})
    except urllib.error.HTTPError as exc:
        check("有据路径", False, f"HTTP {exc.code}：{exc.read()[:200]!r}")
        print(f"\n{'=' * 74}\n结果：{PASS} 通过，{FAIL} 失败")
        return 1

    print(f"\n  问：{q}")
    print(f"  evidence_gap={d['evidence_gap']}  blocked_claims={d['blocked_claims']}")
    print(f"  tokens={d['tokens']}  耗时={d['elapsed_ms']}ms")
    print(f"  trace_id={d['trace_id']}")
    print("\n  ── 答案 ──")
    print("  " + d["answer"][:900].replace("\n", "\n  "))
    print("\n  ── 引用 ──")
    for c in d["citations"]:
        print(f"  [{c['label']}] {c['source']} P{c['priority']} → {c['title']}")
        print(f"        {c['url']}")

    check("拿到非空答案", bool(d["answer"].strip()))
    check("不是缺据声明", not d["evidence_gap"])
    check("答案带引用", bool(d["citations"]))
    check("引用的 URL 指向真实站点",
          all(c["url"].startswith("http") for c in d["citations"]))
    check("引用来源可支撑事实断言（P<=6）",
          all(c["priority"] <= 6 for c in d["citations"]))
    check("单实体问答只引用猎首本体",
          len(d["citations"]) == 1
          and d["citations"][0]["url"] == "https://poedb.tw/cn/Headhunter")
    check("单实体问答未混入仿品且无拦截",
          "仿品" not in d["answer"] and d["blocked_claims"] == 0)
    check("单实体问答不发送全量工具 schema",
          d["tokens"].get("prompt", 0) < 3000,
          f"prompt_tokens={d['tokens'].get('prompt', 0)}")
    check("Token 计量含 reasoning 字段", "reasoning" in d["tokens"],
          f"model={d['model']} tokens={d['tokens']}")
    check("上游固定为 DeepSeek", d["model"] == DEEPSEEK_MODEL)
    check("推理用量允许为零但不得为负", d["tokens"].get("reasoning", 0) >= 0)
    check("答案里没有模型自写的来源行（引用由代码装配）",
          "来源：" not in d["answer"])
    if d["blocked_claims"]:
        print(f"\n  注意：闸门拦截了 {d['blocked_claims']} 句无据断言 —— "
              f"这是闸门在起作用，可用 /admin/trace/{d['trace_id']} 查原因")

    section("4. 流式（SSE）")
    frames = sse(base, "/chat/stream", {"message": q})
    kinds = [f.get("type") for f in frames]
    print(f"  收到 {len(frames)} 帧：{_summarize(kinds)}")
    check("首帧是 meta", kinds[:1] == ["meta"], f"实得 {kinds[:1]}")
    check("末帧是 done", kinds[-1:] == ["done"], f"实得 {kinds[-1:]}")
    check("有逐句下发的正文", kinds.count("sentence") > 1,
          f"sentence 帧 {kinds.count('sentence')} 个")
    check("有引用帧", "citations" in kinds)

    sentences = [f for f in frames if f.get("type") == "sentence"]
    check("每句都带分类与编号字段",
          all("kind" in f and "labels" in f for f in sentences))
    check("推理正文未外发（只有 thinking 状态，无推理文本）",
          all(not f.get("text") for f in frames if f.get("type") == "thinking"))
    blocked = [f for f in frames if f.get("type") == "blocked"]
    check("被拦截的句子下发的是拦截说明而非原文",
          all("已被拦截" in f.get("text", "") for f in blocked),
          f"blocked 帧 {len(blocked)} 个")

    section("5. 流式与非流式判定一致")
    stream_text = "".join(
        f.get("text", "") for f in frames
        if f.get("type") in ("sentence", "blocked", "gap")
    )
    done = frames[-1]
    print(f"  非流式拦截 {d['blocked_claims']} 句 ｜ 流式拦截 {done.get('blocked_claims')} 句")
    print(f"  非流式模型 {d.get('model') or '（空）'} ｜ 流式模型 {done.get('model') or '（空）'}")
    check("正文非空", bool(stream_text.strip()), f"{len(stream_text)} 字")
    check("两条路径都不是缺据声明",
          not d["evidence_gap"] and not done.get("evidence_gap"))
    check("流式使用相同的固定模型", done.get("model") == d.get("model") == DEEPSEEK_MODEL)

    section("6. 缺据路径的流式呈现")
    gap_frames = sse(base, "/chat/stream", {"message": "流放之路2 的升华机制是什么"})
    gap_kinds = [f.get("type") for f in gap_frames]
    print(f"  收到 {len(gap_frames)} 帧：{_summarize(gap_kinds)}")
    check("缺据时下发 gap 帧", "gap" in gap_kinds)
    check("缺据时不下发正文句", "sentence" not in gap_kinds)
    check("缺据时不伪造引用", "citations" not in gap_kinds)
    check("缺据时未消耗 token",
          not (gap_frames[-1].get("tokens") or {}).get("total"))

    section("7. 静态前端")
    for path, needle in (
        ("/", "paste-card"),      # 粘贴识别卡片的容器
        ("/app.js", "chat/stream"),
        ("/app.js", "detectPasteKind"),
        ("/style.css", "--accent"),
    ):
        try:
            with urllib.request.urlopen(host + base_path + path, timeout=20) as resp:
                body = resp.read().decode("utf-8", "replace")
            check(f"{path} 可访问且内容正确", needle in body)
        except urllib.error.URLError as exc:
            check(f"{path} 可访问", False, str(exc))

    section("8. 轨迹可追溯")
    try:
        req = urllib.request.Request(base + f"/admin/trace/{d['trace_id']}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            _ = json.load(resp)
        check("管理端点需凭证", False, "无凭证竟然读到了轨迹")
    except urllib.error.HTTPError as exc:
        check("管理端点需凭证", exc.code in (401, 403), f"HTTP {exc.code}")

    print(f"\n{'=' * 74}\n结果：{PASS} 通过，{FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
