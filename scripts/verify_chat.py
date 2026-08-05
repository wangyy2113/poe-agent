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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import normalize_base_path  # noqa: E402
from core.config import get_config  # noqa: E402

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
        check("  缺据声明含「查不到」表述", "查不到" in d["answer"])
        check("  缺据声明给出查证途径", "poedb.tw" in d["answer"])
        check("  未伪造引用", not d["citations"])

    if args.gap_only:
        print("\n（--gap-only，跳过需要凭证的部分）")
        print(f"\n{'=' * 74}\n结果：{PASS} 通过，{FAIL} 失败")
        return 0 if FAIL == 0 else 1

    section("2. 有据路径（需要可用 LLM 凭证）")
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
    check("Token 计量含 reasoning 字段", "reasoning" in d["tokens"],
          f"model={d['model']} tokens={d['tokens']}")
    if d["model"].startswith("glm"):
        # 只有推理模型才该有非零 reasoning。故障转移可能切到非推理模型，
        # 此时 reasoning=0 是正确的，不该判失败。
        check("GLM 是推理模型，reasoning 应大于 0",
              d["tokens"].get("reasoning", 0) > 0,
              f"reasoning={d['tokens'].get('reasoning')}")
    else:
        print(f"  [跳过] 推理 token 检查 —— 实际模型 {d['model']} 非推理模型")
    check("答案里没有模型自写的来源行（引用由代码装配）",
          "来源：" not in d["answer"])
    if d["blocked_claims"]:
        print(f"\n  注意：闸门拦截了 {d['blocked_claims']} 句无据断言 —— "
              f"这是闸门在起作用，可用 /admin/trace/{d['trace_id']} 查原因")

    section("3. 轨迹可追溯")
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
