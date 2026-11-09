#!/usr/bin/env python3
"""Exercise real question understanding, grounded answers and conversation updates.

Run against a local service. This diagnostic set is not an estimate of accuracy
for all player questions. Review complete answers as well as automatic checks.
Expected source values refer to the current knowledge snapshot.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.deps import user_keys
from core.model_policy import DEEPSEEK_MODEL


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    expected: tuple[str, ...]
    route: str = "answer"
    parent: str = ""
    forbidden: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


CASES = [
    Case("definition", "猎首是什么？", ("皮革腰带", "稀有怪物")),
    Case("concise", "猎首是什么？请简短说明。", ("皮革腰带",)),
    Case("mods", "猎首（Headhunter）有哪些词缀？", ("25[–-]40", "40[–-]55", "60秒"), forbidden=("仿品",)),
    Case("acquisition_followup", "它怎么获得？", ("疯医", "恶魔"), parent="mods"),
    Case("upper_bound", "猎首的隐式词缀最多加多少生命？", ("(?:上限|最多|最大值).{0,8}40", "25[–-]40")),
    Case("range", "猎首的隐式生命词缀数值范围是多少？", ("25[–-]40",)),
    Case("suppression", "法术压制达到100%后，会免疫法术伤害吗？", ("不.{0,5}免疫|不是免疫", "40%", "100%"), tools=("mitigation_calc",)),
    Case("compound", "冰霜新星20级需要多少魔力？从哪里获得？", ("23(?:点)?魔力|魔力.{0,6}23", "海妖之歌", "奈莎")),
    Case("build", "我是新手，想玩旋风斩，预算5神圣石，推荐一套开荒BD。", ("赛季", "职业|升华", "PoB|等级|目标"), "clarification"),
    Case("poe2", "流放之路2的升华机制是什么？", ("只覆盖", "流放之路1"), "gap"),
    Case("unknown", "不存在的测试装备「量子彩虹腰带ZQ9187」有哪些词缀？", ("未.{0,5}找到|未.{0,4}匹配|查不到|无.{0,5}依据|没有.{0,8}(?:依据|条目)",), "gap", forbidden=("装备不存在", "查无此物")),
    Case("future", "下个赛季会加入什么新机制？帮我预测一下。", ("依据|尚未|无法|不能",), "gap"),
    Case("acquisition", "猎首怎么获得？", ("疯医", "恶魔")),
    Case("max_hit", "我有5000生命、75%火焰抗性，没有其他减伤，最多能承受多大的单次火焰击中？", ("20000", "5000", "75%", "临界|耗尽"), tools=("max_hit_calc",)),
    Case("max_hit_reworded", "5000生命，75%火焰抗性，一发最多能挨多少火焰伤害？", ("20000", "75%", "临界|耗尽"), tools=("max_hit_calc",)),
    Case("max_hit_overcap", "我有5000生命、99%火焰抗性，没有其他减伤，最多能承受多大的单次火焰击中？", ("20000", "75%", "90%", "临界|耗尽"), forbidden=("500000",), tools=("max_hit_calc",)),
    Case("numeric_followup", "那如果最大火抗提高到90%呢？", ("50000", "5000", "99%", "90%", "临界|耗尽"), parent="max_hit_overcap", tools=("max_hit_calc",)),
    Case("max_hit_cap80", "5000生命，总火抗99%，最大火抗80%，没有其他减伤，一发最多能挨多少火焰伤害？", ("25000", "80%"), tools=("max_hit_calc",)),
    Case("max_hit_cap90", "5000生命，总火抗99%，最大火抗90%，没有其他减伤，一发最多能挨多少火焰伤害？", ("50000", "90%"), tools=("max_hit_calc",)),
    Case("max_hit_actual90", "5000生命，实际生效火焰抗性90%，没有其他减伤，一发最多能挨多少火焰伤害？", ("50000", "90%"), tools=("max_hit_calc",)),
    Case("max_hit_bonus", "5000生命，总火抗99%，+5%最大火焰抗性，没有其他减伤，一发最多能挨多少火焰伤害？", ("25000", "80%", "5%"), tools=("max_hit_calc",)),
    Case("max_hit_penetration", "5000生命，总火抗99%，没有最大抗性加成，敌人25%火焰穿透，没有其他减伤，一发最多能挨多少火焰伤害？", ("10000", "75%", "25%", "50%"), tools=("max_hit_calc",)),
    Case("max_hit_invalid_actual", "5000生命，实际生效火焰抗性99%，没有其他减伤，一发最多能挨多少火焰伤害？", ("90%", "核对|确认|澄清|不合法|不能"), "clarification", forbidden=("20000", "50000", "500000"), tools=("max_hit_calc",)),
    Case("max_hit_invalid_cap", "5000生命，总火抗120%，最大火抗95%，没有其他减伤，一发最多能挨多少火焰伤害？", ("90%|硬上限", "核对|确认|澄清|不合法|不能"), "clarification", forbidden=("20000", "50000", "100000"), tools=("max_hit_calc",)),
    Case("max_hit_mom", "5000生命，总火抗75%，有Mind over Matter把部分伤害转给魔力，一发最多能挨多少火焰伤害？", ("MindoverMatter|MoM", "无法|不能|不支持|未支持|未覆盖|未提供|请补充|确认"), "clarification", forbidden=("20000", "40%")),
    Case("crit", "技能基础暴击率5%，增加100%暴击率，暴击伤害加成300%，最终暴击率是多少？", ("暴击率(?:为|是)?[^。]{0,8}10(?:[.]0+)?%", "5%", "100%|增加的暴击率.{0,15}100/100"), tools=("crit_calc",)),
    Case("negation", "不是问猎首，法血有哪些词缀？", ("法师之血|Mageblood", "魔法非恢复类药剂", "2[–-]4"), forbidden=("当你击败稀有怪物",)),
    Case("comparison", "猎首和法血有什么区别？", ("猎首", "法师之血|Mageblood", "稀有怪物", "魔法非恢复类药剂")),
    Case("multiple_subjects", "猎首有哪些词缀，冰霜新星20级需要多少魔力？", ("猎首", "稀有怪物", "冰霜新星", "20级", "23(?:点)?魔力|魔力.{0,6}23")),
    Case("description", "偷取词缀的腰带有哪些？", ("猎首", "稀有怪物", "仿品", "魔法怪物", "20%", "60秒"), forbidden=("生命偷取上限", "每秒偷取")),
    Case("description_reworded", "有什么腰带能窃取怪物的词缀？", ("猎首", "稀有怪物", "仿品", "魔法怪物"), forbidden=("生命偷取上限", "每秒偷取")),
    Case("description_jewel", "偷取词缀的珠宝有哪些？", ("求知的热情|InspiredLearning", "4个关键天赋", "20秒"), forbidden=("生命偷取上限",)),
    Case("description_leech", "哪些腰带有生命偷取？请给两个例子。", ("龙蜕|Wurm", "呕吐|TheRetch", "生命偷取"), forbidden=("击败稀有怪物",)),
    Case("description_leech_affix", "生命偷取词缀的腰带有哪些？请给两个例子。", ("龙蜕|Wurm", "呕吐|TheRetch", "生命偷取"), forbidden=("击败稀有怪物",)),
    Case("description_negation", "不要偷取词缀的腰带，要生命偷取的腰带，请举两个例子。", ("龙蜕|Wurm", "呕吐|TheRetch", "生命偷取"), forbidden=("击败稀有怪物",)),
    Case("unsupported_condition", "10000护甲受到5000物理击中，另有20%物理减伤，敌人无视10%物理减伤，最终受到多少伤害？", ("10%", "不能|无法|不支持|未覆盖|未纳入"), "clarification", forbidden=("2571",)),
    Case("recharge", "我有3000能量护盾，50%护盾回充速度加成，护盾回充每秒恢复多少？", ("1485", "3000", "50%|增加的回充速度.{0,15}50/100"), tools=("recovery_calc",)),
    Case("partial_gap", "猎首怎么获得？能告诉我具体掉落概率吗？", ("疯医", "概率", "无.{0,6}依据|未.{0,6}收录|无法|不能")),
]


def check(case: Case, answer: str, citations: list, done: dict, events: list) -> list[str]:
    """Check key premises/conclusions, not a fixed answer template or latency."""
    text = re.sub(r"\[\[E\d+\]\]|[\s*,]", "", answer)
    failures = []
    if not done or any(e["type"] == "error" for e in events) or re.search("生成失败|本轮未完成|未能在调用上限", text):
        failures.append("未正常完成")
    if done.get("blocked_claims", 0):
        failures.append("回答存在拦截")
    failures.extend(f"缺少结论/前提：{p}" for p in case.expected if not re.search(p, text, re.I))
    failures.extend(f"出现错误结论：{p}" for p in case.forbidden if re.search(p, text, re.I))
    if bool(done.get("evidence_gap")) != (case.route == "gap"):
        failures.append("拒答路径不符")
    if case.route == "answer" and not citations:
        failures.append("缺少引用")
    if case.id != "poe2" and (done.get("model") != DEEPSEEK_MODEL or not done.get("tokens", {}).get("total")):
        failures.append("未验证当前模型")
    if events:
        invoked = {e.get("name") for e in events if e["type"] == "tool"}
        failures.extend(f"未调用 {name}" for name in case.tools if name not in invoked)
    return failures


def selected_cases(only: str) -> list[Case]:
    wanted = set(only.split(",")) if only else {c.id for c in CASES}
    known = {c.id: c for c in CASES}
    if wanted - known.keys():
        raise ValueError("未知场景：" + ",".join(sorted(wanted - known.keys())))
    for name in list(wanted):
        while known[name].parent:
            name = known[name].parent
            wanted.add(name)
    return [c for c in CASES if c.id in wanted]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8787/poe_agent")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--only", default="", help="Comma-separated case IDs; required parent turns are included")
    parser.add_argument("--mode", choices=("stream", "batch"), default="stream")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    cases = selected_cases(args.only)
    keys = user_keys()
    headers = {"Authorization": f"Bearer {keys[0]}"} if keys else {}
    results, conversations = [], {}
    with httpx.Client(timeout=240, trust_env=False, headers=headers) as client:
        for case in cases:
            body = {"message": case.question}
            if case.parent:
                body["conversation_id"] = conversations[case.parent]
            events = []
            if args.mode == "stream":
                with client.stream("POST", args.base.rstrip("/") + "/chat/stream", json=body) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            events.append(json.loads(line[6:]))
                meta = next((e for e in events if e["type"] == "meta"), {})
                done = events[-1] if events and events[-1]["type"] == "done" else {}
                answer = "".join(e.get("text", "") for e in events if e["type"] in ("sentence", "gap", "note"))
                citations = [c for e in events if e["type"] == "citations" for c in e["items"]]
            else:
                response = client.post(args.base.rstrip("/") + "/chat", json=body)
                response.raise_for_status()
                done = meta = response.json()
                answer, citations = done["answer"], done.get("citations", [])
            conversations[case.id] = meta.get("conversation_id", "")
            failures = check(case, answer, citations, done, events)
            results.append({"id": case.id, "question": case.question, "passed": not failures,
                            "failures": failures, "answer": answer, "citations": citations,
                            "completion": done, "events": events})
            if args.json:
                args.json.parent.mkdir(parents=True, exist_ok=True)
                args.json.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"{'FAIL' if failures else 'PASS'} {case.id}: {', '.join(failures) or case.route}", flush=True)
            time.sleep(max(0, args.interval))
    passed = sum(r["passed"] for r in results)
    print(f"{passed}/{len(results)} 场景通过；包括澄清与拒答，仍需人工核对完整答案。")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
