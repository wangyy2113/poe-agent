#!/usr/bin/env python3
"""Current-model quality smoke: actual answers, follow-ups, boundaries and cost.

Run against a started local service. This is a diagnostic set, not an accuracy
estimate for all player questions. Source-dependent values use the pinned local
knowledge snapshot; investigate upstream changes before changing expectations.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.deps import user_keys
from core.model_policy import DEEPSEEK_MODEL


# id, question, expected answer fragments, route, parent conversation
CASES = [
    ("definition", "猎首是什么？", ("皮革腰带", "稀有怪物"), "answer", ""),
    ("concise", "猎首是什么？请简短说明。", ("皮革腰带",), "answer", ""),
    ("mods", "猎首（Headhunter）有哪些词缀？", ("25-40", "40-55", "60 秒"), "answer", ""),
    ("followup", "它怎么获得？", ("疯医", "恶魔"), "answer", "mods"),
    ("upper_bound", "猎首的隐式词缀最多加多少生命？", ("**40**", "25-40"), "answer", ""),
    ("suppression", "法术压制达到100%后，会免疫法术伤害吗？", ("100", "40%"), "answer", ""),
    ("max_hit", "我有5000生命、75%火焰抗性，没有其他减伤，最多能承受多大的单次火焰击中？", ("20,000", "5000", "75%"), "answer", ""),
    ("compound", "冰霜新星20级需要多少魔力？从哪里获得？", ("23 魔力", "海妖之歌", "奈莎"), "answer", ""),
    ("build", "我是新手，想玩旋风斩，预算5神圣石，推荐一套开荒BD。", ("赛季", "职业", "PoB"), "clarification", ""),
    ("poe2", "流放之路2的升华机制是什么？", ("只覆盖", "流放之路 1"), "gap", ""),
    ("unknown", "不存在的测试装备「量子彩虹腰带ZQ9187」有哪些词缀？", ("核对名称",), "gap", ""),
    ("future", "下个赛季会加入什么新机制？帮我预测一下。", ("依据",), "gap", ""),
    ("acquisition", "猎首怎么获得？", ("疯医", "恶魔"), "answer", ""),
    ("max_hit_reworded", "5000生命，75%火焰抗性，一发最多能挨多少火焰伤害？", ("20,000", "75%"), "answer", ""),
    ("range", "猎首的隐式生命词缀数值范围是多少？", ("25-40",), "answer", ""),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8787/poe_agent")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Minimum seconds between cases; default respects 60 requests/minute")
    args = parser.parse_args()
    keys = user_keys()
    headers = {"Authorization": f"Bearer {keys[0]}"} if keys else {}
    results, conversations = [], {}
    with httpx.Client(timeout=180, trust_env=False, headers=headers) as client:
        last_started = 0.0
        for case_id, question, expected, route, parent in CASES:
            time.sleep(max(0, args.interval - (time.monotonic() - last_started)))
            last_started = time.monotonic()
            body = {"message": question}
            if parent:
                body["conversation_id"] = conversations[parent]
            events = []
            with client.stream("POST", args.base.rstrip("/") + "/chat/stream", json=body) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
            meta = next((e for e in events if e["type"] == "meta"), {})
            conversations[case_id] = meta.get("conversation_id", "")
            done = events[-1] if events and events[-1]["type"] == "done" else {}
            answer = "".join(e.get("text", "") for e in events if e["type"] in ("sentence", "gap", "note"))
            citations = [c for e in events if e["type"] == "citations" for c in e["items"]]
            tokens = done.get("tokens", {})
            failures = []
            if not done or any(e["type"] == "error" for e in events):
                failures.append("未正常完成")
            if done.get("blocked_claims", 0):
                failures.append("回答存在拦截")
            failures.extend(f"缺少 {part}" for part in expected if part not in answer)
            if bool(done.get("evidence_gap")) != (route == "gap"):
                failures.append("拒答路径不符")
            if route == "answer" and not citations:
                failures.append("缺少引用")
            if "猎首" in question and ("仿品" in answer or len(citations) != 1):
                failures.append("混入其他装备")
            if tokens.get("total", 0) > 10000:
                failures.append("上下文/生成成本过高")
            if case_id == "compound" and (done.get("model") != DEEPSEEK_MODEL or not tokens.get("total")):
                failures.append("未验证当前模型")
            record = {"id": case_id, "route": route, "question": question,
                      "passed": not failures, "failures": failures, "answer": answer,
                      "citations": citations, "completion": done, "events": events}
            results.append(record)
            print(f"{'PASS' if not failures else 'FAIL'} {case_id}: {', '.join(failures) or route}", flush=True)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed = sum(r["passed"] for r in results)
    print(f"{passed}/{len(results)} 场景通过；包括澄清与拒答，不等于完整方案交付率。")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
