#!/usr/bin/env python3
"""评测入口：黄金问答集 → 指标（任务 2.7，需求 3.7 / 3.9 / 16.7）。

为什么要有这个脚本：`config.yaml` 自己写着 `min_coverage` 需用黄金问答集调优、
「过高导致错误拒答，过低导致无据断言漏出，成对监控」；design.md 15 节也把
「禁止推断导致过度拒答」列为主要风险，对策是把错误拒答率与漏出率成对纳入评测。
在此之前这套监控并不存在，于是改 `orchestrator/binding.py` 或检索阈值都没有判据。

本版实现**检索侧**指标，全程离线（只读 `data/poe.db`，不调模型、不发网络）：

    Recall@5 / Recall@20     期望实体是否被召回，考的是召回质量
    充分性判定正确率          正负分列 —— 正类判成不足=错误拒答上游，
                             负类判成充分=无据漏出上游
    证据召回率                expect_evidence_text 是否出现在候选原文里

出口闸门那三项（引用正确率、无据断言漏出率、错误拒答率）要有生成结果才能算，
由后续的 `--record` / `--audit` 接入；本脚本对 `tool_route` 类只清点不判定。

    python scripts/eval_run.py                      # 全量
    python scripts/eval_run.py --kind gap -v        # 只跑拒答类，逐条明细
    python scripts/eval_run.py --sweep 0.3,0.5,0.7  # 扫 min_coverage，看成对代价
    python scripts/eval_run.py --json out.json      # 机器可读，供回归对比
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.coverage import dimension_gap  # noqa: E402
from orchestrator.loop import poe2_marker  # noqa: E402
from retrieval import alias as alias_mod  # noqa: E402
from retrieval.hybrid import retrieve  # noqa: E402
from tests.golden.loader import (  # noqa: E402
    KIND_ZH,
    KINDS,
    Case,
    load_cases,
    matches_slug,
)
from tests.golden.recording import (  # noqa: E402
    Recording,
    StaleRecording,
    evidence_fingerprint,
    load_all as load_recordings,
    now_iso,
)

# 导入即注册工具（kb_search / entity_lookup / mod_tiers / …），与 main.py 一致。
# 少了这一句，`run_turn` 的强制检索会拿到空注册表，录出来的全是
# 「检索不可用（未注册的工具 'kb_search'）」——评测骨架必须和应用同样初始化。
import tools.impl  # noqa: E402,F401

#: Recall 报两个 k：@5 反映排序质量（模型上下文里前几条最有效），
#: @20 与 `retrieval.top_k` 一致，反映召回天花板。
RECALL_KS = (5, 20)


@dataclass
class CaseOutcome:
    case: Case
    sufficient: bool
    reason: str
    channels: list[str] = field(default_factory=list)
    top_coverage: float = 0.0
    top_entity: str = ""
    hit_rank: int | None = None          # 期望实体在候选里的名次（1 起），None=未召回
    evidence_recalled: bool | None = None  # expect_evidence_text 是否出现在候选原文里
    total_hits: int = 0
    #: 是否由维度覆盖闸门改判为依据不足（`kb.coverage.dimension_gap`）
    dimension_gate: bool = False
    #: 是否被 POE1/POE2 边界拦下（`orchestrator.loop.poe2_marker`）
    out_of_scope: bool = False

    # ── 判定 ──
    @property
    def sufficiency_ok(self) -> bool:
        return self.sufficient == self.case.expects_sufficient

    @property
    def is_wrong_refusal(self) -> bool:
        """正类被判为依据不足 —— 错误拒答的上游。"""
        return self.case.expects_sufficient and not self.sufficient

    @property
    def is_leakage(self) -> bool:
        """负类被判为依据充分 —— 无据漏出的上游。"""
        return self.case.expect_gap and self.sufficient

    @property
    def recall_ok(self) -> bool | None:
        if not self.case.has_retrieval_target:
            return None
        return self.hit_rank is not None

    @property
    def passed(self) -> bool:
        """这条用例整体算不算通过。"""
        if not self.sufficiency_ok:
            return False
        if self.case.has_retrieval_target and self.hit_rank is None:
            return False
        if self.case.expect_evidence_text and self.evidence_recalled is False:
            return False
        return True


def run_case(conn, case: Case, params: dict) -> CaseOutcome:
    # POE1/POE2 边界在 `loop.py` 里位于强制检索**之前**，所以这里也要先判——
    # 少了这一步，一个已经被线上拦住的 POE2 问题会在评测里显示为「漏出」。
    # 与维度闸门同一个道理：评测必须量到线上真正生效的那条路径。
    if poe2_marker(case.query):
        return CaseOutcome(
            case=case, sufficient=False,
            reason="POE1/POE2 边界：问题指向《流放之路 2》，本库只覆盖 POE1",
            out_of_scope=True,
        )

    r = retrieve(conn, case.query, **params)
    top = r.hits[0] if r.hits else None

    # 与 `orchestrator/loop.py._apply_dimension_gate` 同一道闸门、同一个函数。
    # 评测必须量到线上真正生效的那条路径——各测一半会让指标与线上行为分叉。
    #
    # 别名精确命中的实体也要一起传：领域识别有两条通道（意图词 + 点名了库里的实体），
    # 少传这一个参数，凡是靠专名识别领域的用例都会在评测里显示为漏出，
    # 而线上其实拦住了（2026-08-19 实测三条宝石用例就是这么假报的）。
    sufficient, reason, gated = r.sufficient, r.reason, False
    if sufficient:
        verdict = dimension_gap(
            conn,
            case.query,
            exact_entity_ids=alias_mod.exact_entity_ids(
                (h.channel, h.entity_id or "") for h in r.hits
            ),
        )
        if verdict.is_gap:
            sufficient, reason, gated = False, verdict.reason(), True

    rank: int | None = None
    if case.expect_slug:
        for i, h in enumerate(r.hits, start=1):
            if matches_slug(h.entity_id, case.expect_slug):
                rank = i
                break

    evidence_recalled: bool | None = None
    if case.expect_evidence_text:
        evidence_recalled = any(case.expect_evidence_text in h.text for h in r.hits)

    return CaseOutcome(
        case=case,
        sufficient=sufficient,
        reason=reason,
        channels=list(r.channels),
        top_coverage=r.top_coverage,
        top_entity=(top.entity_id or "") if top else "",
        hit_rank=rank,
        evidence_recalled=evidence_recalled,
        total_hits=len(r.hits),
        dimension_gate=gated,
    )


def summarize(outcomes: list[CaseOutcome]) -> dict:
    """汇总指标。分母都写进结果里——比率脱离分母无法解读。"""
    scored = [o for o in outcomes if o.case.retrieval_scorable]
    positives = [o for o in scored if o.case.expects_sufficient]
    negatives = [o for o in scored if o.case.expect_gap]
    targeted = [o for o in scored if o.case.has_retrieval_target]
    with_evidence = [o for o in scored if o.case.expect_evidence_text]

    recall: dict[str, dict] = {}
    for k in RECALL_KS:
        hit = sum(1 for o in targeted if o.hit_rank is not None and o.hit_rank <= k)
        recall[f"recall@{k}"] = {
            "hit": hit,
            "total": len(targeted),
            "rate": (hit / len(targeted)) if targeted else None,
        }

    wrong_refusals = [o for o in positives if o.is_wrong_refusal]
    leakages = [o for o in negatives if o.is_leakage]
    ev_ok = sum(1 for o in with_evidence if o.evidence_recalled)

    return {
        "cases": {
            "total": len(outcomes),
            "scored": len(scored),
            "skipped_needs_generation": len(outcomes) - len(scored),
            "passed": sum(1 for o in scored if o.passed),
        },
        "recall": recall,
        "sufficiency": {
            "positive": {
                "total": len(positives),
                "correct": len(positives) - len(wrong_refusals),
                "wrong_refusal": len(wrong_refusals),
                "wrong_refusal_rate": (
                    len(wrong_refusals) / len(positives) if positives else None
                ),
                "failed_ids": [o.case.id for o in wrong_refusals],
            },
            "negative": {
                "total": len(negatives),
                "correct": len(negatives) - len(leakages),
                "leakage": len(leakages),
                "leakage_rate": (len(leakages) / len(negatives) if negatives else None),
                "failed_ids": [o.case.id for o in leakages],
            },
        },
        "evidence_recall": {
            "hit": ev_ok,
            "total": len(with_evidence),
            "rate": (ev_ok / len(with_evidence)) if with_evidence else None,
            "failed_ids": [
                o.case.id for o in with_evidence if not o.evidence_recalled
            ],
        },
    }


# ── 输出 ───────────────────────────────────────────────────

def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def print_report(outcomes: list[CaseOutcome], summary: dict, *, verbose: bool) -> None:
    for kind in KINDS:
        group = [o for o in outcomes if o.case.kind == kind]
        if not group:
            continue
        print(f"\n{'=' * 76}")
        print(f"{KIND_ZH[kind]}（{len(group)} 条）")
        print("=" * 76)
        for o in group:
            if not o.case.retrieval_scorable:
                why = (f"判路由（期望 {o.case.expect_tool}）" if o.case.expect_tool
                       else f"判答案内容（须含 {'、'.join(o.case.expect_in_answer)}）")
                print(f"  [跳过] {o.case.id}：需要生成结果才能{why}")
                continue
            mark = "PASS" if o.passed else "FAIL"
            detail = []
            if o.case.expect_slug:
                detail.append(
                    f"命中名次={o.hit_rank}" if o.hit_rank else
                    f"**未召回** {o.case.expect_slug}"
                )
            if o.case.expect_gap:
                detail.append("正确拒答" if not o.sufficient else "**漏出**：判为有依据")
            if o.case.expect_evidence_text:
                detail.append(
                    "证据已召回" if o.evidence_recalled else
                    f"**证据未召回**「{o.case.expect_evidence_text}」"
                )
            print(f"  [{mark}] {o.case.id}：{o.case.note}")
            print(f"         查询「{o.case.query}」")
            print(f"         {' / '.join(detail) or '—'}")
            if verbose or not o.passed:
                print(f"         候选 {o.total_hits} 条，通道={o.channels}，"
                      f"覆盖率={_pct(o.top_coverage)}，充分={o.sufficient}")
                print(f"         判定理由：{o.reason}")
                if o.top_entity:
                    print(f"         top={o.top_entity}")

    s = summary
    print(f"\n{'=' * 76}")
    print("指标（检索侧，离线）")
    print("=" * 76)
    for k in RECALL_KS:
        r = s["recall"][f"recall@{k}"]
        print(f"  Recall@{k:<3}          {_pct(r['rate'])}  ({r['hit']}/{r['total']})")
    pos, neg = s["sufficiency"]["positive"], s["sufficiency"]["negative"]
    print(f"  错误拒答率           {_pct(pos['wrong_refusal_rate'])}  "
          f"({pos['wrong_refusal']}/{pos['total']} 正类被判依据不足)")
    print(f"  无据漏出率（上游）   {_pct(neg['leakage_rate'])}  "
          f"({neg['leakage']}/{neg['total']} 负类被判依据充分)  目标 0")
    ev = s["evidence_recall"]
    print(f"  证据召回率           {_pct(ev['rate'])}  ({ev['hit']}/{ev['total']})")
    c = s["cases"]
    print(f"\n  用例：{c['passed']}/{c['scored']} 通过"
          f"（另有 {c['skipped_needs_generation']} 条需生成结果才能判，本轮跳过）")
    if pos["failed_ids"]:
        print(f"  错误拒答：{', '.join(pos['failed_ids'])}")
    if neg["failed_ids"]:
        print(f"  漏出    ：{', '.join(neg['failed_ids'])}")
    if ev["failed_ids"]:
        print(f"  证据未召回：{', '.join(ev['failed_ids'])}")
    print("\n  两项须成对看：只压漏出会退化成什么都不答，只压拒答会让推断重新漏出。")


# ── 出口闸门：录制与回放 ────────────────────────────────────

@dataclass
class GateOutcome:
    """一条用例在出口闸门上的表现。全部由录制回放得出，不发网络。"""

    case: Case
    rec: Recording
    binding: Any = None
    stale: str = ""
    #: 流式与批量的**判定**不一致（严重：两套规则分叉）
    stream_mismatch: str = ""
    #: 仅输出空白字符不一致（判定相同，用户看到的内容一样）
    stream_whitespace_diff: str = ""
    #: 确定误拦：被拦的句子其实是 Markdown 排版元素（`binding.is_structural`）
    wrong_blocks: list[str] = field(default_factory=list)
    #: 期望出现但答案里没有的数值 / 表述
    missing_values: list[str] = field(default_factory=list)
    missing_phrases: list[str] = field(default_factory=list)
    #: 明令禁止却出现了的表述
    forbidden_phrases: list[str] = field(default_factory=list)
    #: 工具路由是否命中期望；None 表示这条用例不考路由
    tool_ok: bool | None = None
    #: 缺据路径的契约：不得调模型、不得伪造引用
    gap_contract: list[str] = field(default_factory=list)

    @property
    def factual(self) -> list[Any]:
        if not self.binding:
            return []
        return [v for v in self.binding.verdicts if v.kind == "factual"]

    @property
    def blocked(self) -> list[Any]:
        if not self.binding:
            return []
        return [v for v in self.binding.verdicts if not v.ok]

    @property
    def answer_text(self) -> str:
        if self.rec.evidence_gap:
            return self.rec.gap_answer
        return self.binding.text if self.binding else ""

    @property
    def passed(self) -> bool:
        """空白差异不计入失败：判定一致，用户看到的内容也一样。"""
        return not any((
            self.stale, self.stream_mismatch, self.wrong_blocks,
            self.missing_values, self.missing_phrases, self.forbidden_phrases,
            self.gap_contract, self.tool_ok is False,
        ))


def record_case(case: Case) -> Recording:
    """跑一次真实链路并录下来。缺据路径同样录——它也要回归，且 tokens 必须为空。"""
    from orchestrator.loop import run_turn  # noqa: PLC0415  只有 --record 才需要凭证

    r = run_turn(case.query)
    return Recording(
        case_id=case.id,
        query=case.query,
        raw_answer=r.raw_answer,
        evidence=list(r.evidence),
        evidence_fingerprint=evidence_fingerprint(list(r.evidence)),
        evidence_gap=r.evidence_gap,
        gap_answer=r.answer if r.evidence_gap else "",
        model=r.model,
        tokens=dict(r.tokens),
        # 键是 `tool` 不是 `name`（见 `orchestrator.loop._tool_record`）。
        # 取错键会让每条工具调用都录成空串，路由指标恒为 0——那是量错了，不是路由坏了。
        tool_calls=[str(tc.get("tool") or "") for tc in r.tool_calls],
        blocked_claims=r.blocked,
        elapsed_ms=r.elapsed_ms,
        recorded_at=now_iso(),
    )


def replay_case(conn, case: Case, rec: Recording, params: dict) -> GateOutcome:
    """离线回放：重跑出口闸门，量它拦得对不对。"""
    from orchestrator.binding import StreamingGate, is_structural, verify  # noqa: PLC0415
    from orchestrator.loop import _domain_terms  # noqa: PLC0415  与线上同一份词表

    out = GateOutcome(case=case, rec=rec)
    cfg = get_config()
    factual_terms = list(cfg.get("retrieval.factual_terms", []) or [])
    domain_terms = _domain_terms()

    # 录制新鲜度：证据正文变了就不能拿旧录制算指标
    if rec.evidence:
        try:
            rec.check_fresh(conn)
        except StaleRecording as exc:
            out.stale = str(exc)

    if rec.evidence_gap:
        # 缺据路径的契约由代码保证，逐条查
        if rec.tokens.get("total"):
            out.gap_contract.append(f"缺据路径消耗了 {rec.tokens['total']} token（应为 0）")
        if "查不到" not in rec.gap_answer:
            out.gap_contract.append("缺据声明里没有「查不到」这类明确表述")
        if "poedb.tw" not in rec.gap_answer and "poewiki" not in rec.gap_answer:
            out.gap_contract.append("缺据声明没有给出查证途径")
    else:
        out.binding = verify(
            rec.raw_answer, rec.evidence,
            factual_terms=factual_terms, domain_terms=domain_terms,
        )
        # 确定误拦：排版元素被当成事实句拦掉。这一类有确定判据，应为 0
        out.wrong_blocks = [
            v.text.strip()[:60] for v in out.binding.verdicts
            if not v.ok and is_structural(v.text)
        ]
        # 流式与批量必须给出同一结果（与 test_streaming_gate 同一条不变量，
        # 区别是这里喂的是真实模型输出而不是手写样本）。
        #
        # **判定差异与空白差异要分开报。** 前者是两套规则分叉（同一个回答走流式和
        # 非流式得到不同拦截结果，严重）；后者只是分句时换行的归属不同，用户看到的
        # 内容一样。混成一个指标会让真问题被空白噪声盖住。
        batch_calls = [(v.kind, v.ok) for v in out.binding.verdicts]
        for size in (1, 7, 40):
            gate = StreamingGate(
                rec.evidence, factual_terms=factual_terms, domain_terms=domain_terms
            )
            for i in range(0, len(rec.raw_answer), size):
                gate.feed(rec.raw_answer[i:i + size])
            gate.flush()
            streamed = gate.result()
            stream_calls = [(v.kind, v.ok) for v in streamed.verdicts if v.text.strip()]
            if (
                streamed.blocked != out.binding.blocked
                or stream_calls != [c for c, v in zip(batch_calls, out.binding.verdicts)
                                    if v.text.strip()]
            ):
                out.stream_mismatch = (
                    f"切片 {size} 字时**判定**与批量不一致："
                    f"拦截 {streamed.blocked} vs {out.binding.blocked}"
                )
                break
            if streamed.text != out.binding.text:
                out.stream_whitespace_diff = (
                    f"切片 {size} 字时输出差 {abs(len(streamed.text) - len(out.binding.text))} "
                    "个空白字符（判定一致）"
                )

    text = out.answer_text
    out.missing_values = [v for v in case.expect_values if v not in text]
    out.missing_phrases = [p for p in case.expect_in_answer if p not in text]
    out.forbidden_phrases = [p for p in case.forbid_in_answer if p in text]
    if case.expect_tool:
        out.tool_ok = case.expect_tool in rec.tool_calls
    return out


def summarize_gate(outs: list[GateOutcome]) -> dict:
    generated = [o for o in outs if not o.rec.evidence_gap]
    gaps = [o for o in outs if o.rec.evidence_gap]
    factual = [v for o in generated for v in o.factual]
    blocked = [v for o in generated for v in o.blocked]
    routed = [o for o in outs if o.tool_ok is not None]
    return {
        "recordings": {
            "total": len(outs),
            "generated": len(generated),
            "gap_path": len(gaps),
            "passed": sum(1 for o in outs if o.passed),
            "stale": [o.case.id for o in outs if o.stale],
        },
        "citation_validity": {
            "ok": sum(1 for v in factual if v.ok),
            "total": len(factual),
            "rate": (sum(1 for v in factual if v.ok) / len(factual)) if factual else None,
        },
        "blocked": {
            "total": len(blocked),
            "definitely_wrong": sum(len(o.wrong_blocks) for o in generated),
            "reasons": sorted({v.reason[:60] for v in blocked}),
        },
        "stream_parity": {
            "consistent": sum(1 for o in generated if not o.stream_mismatch),
            "total": len(generated),
            "failed_ids": [o.case.id for o in generated if o.stream_mismatch],
            "whitespace_only_ids": [
                o.case.id for o in generated if o.stream_whitespace_diff
            ],
        },
        "answer_assertions": {
            "missing_values": {o.case.id: o.missing_values for o in outs if o.missing_values},
            "missing_phrases": {o.case.id: o.missing_phrases for o in outs if o.missing_phrases},
            "forbidden": {o.case.id: o.forbidden_phrases for o in outs if o.forbidden_phrases},
        },
        "gap_contract": {o.case.id: o.gap_contract for o in gaps if o.gap_contract},
        "tool_route": {
            "ok": sum(1 for o in routed if o.tool_ok),
            "total": len(routed),
            "failed": {o.case.id: o.rec.tool_calls for o in routed if not o.tool_ok},
        },
        "models": sorted({o.rec.model for o in generated if o.rec.model}),
        "tokens": sum(int(o.rec.tokens.get("total") or 0) for o in outs),
    }


def print_gate_report(outs: list[GateOutcome], s: dict, *, verbose: bool) -> None:
    print(f"\n{'=' * 76}")
    print("出口闸门（录制回放，离线）")
    print("=" * 76)
    for o in outs:
        mark = "PASS" if o.passed else "FAIL"
        kind = "缺据路径" if o.rec.evidence_gap else (
            f"{len(o.factual)} 事实句 / 拦 {len(o.blocked)}"
        )
        print(f"  [{mark}] {o.case.id}（{kind}）")
        for label, items in (
            ("录制过期", [o.stale] if o.stale else []),
            ("流式判定不一致", [o.stream_mismatch] if o.stream_mismatch else []),
            ("确定误拦", o.wrong_blocks),
            ("缺失数值", o.missing_values),
            ("缺失表述", o.missing_phrases),
            ("禁止表述出现", o.forbidden_phrases),
            ("缺据契约", o.gap_contract),
        ):
            for it in items:
                print(f"         **{label}**：{it}")
        if o.tool_ok is False:
            print(f"         **路由未命中**：期望 {o.case.expect_tool}，"
                  f"实际 {o.rec.tool_calls}")
        if verbose and o.binding:
            for v in o.binding.verdicts:
                flag = " " if v.ok else "×"
                print(f"         {flag} [{v.kind}] {v.text.strip()[:56]}"
                      + (f"  ← {v.reason[:40]}" if not v.ok else ""))

    cv = s["citation_validity"]
    print(f"\n  引用有效率           {_pct(cv['rate'])}  ({cv['ok']}/{cv['total']} 事实句"
          f"绑定有效且数值可溯源)")
    b = s["blocked"]
    print(f"  拦截                 {b['total']} 句，其中确定误拦 {b['definitely_wrong']}（应为 0）")
    sp = s["stream_parity"]
    print(f"  流式与批量判定一致   {sp['consistent']}/{sp['total']}"
          + (f"，另有 {len(sp['whitespace_only_ids'])} 条仅空白字符不同"
             if sp["whitespace_only_ids"] else ""))
    tr = s["tool_route"]
    if tr["total"]:
        print(f"  工具路由命中         {tr['ok']}/{tr['total']}（附加观测，不设门槛）")
    r = s["recordings"]
    print(f"  录制                 {r['passed']}/{r['total']} 通过"
          f"（{r['generated']} 条经模型 / {r['gap_path']} 条走缺据路径）")
    print(f"  模型                 {', '.join(s['models']) or '—'}"
          f"   录制期间累计 {s['tokens']} token")
    if b["reasons"]:
        print("\n  拦截原因分布：")
        for reason in b["reasons"]:
            print(f"    - {reason}")


# ── 人工审计：两项无法自动判定的指标 ──────────────────────────

#: 每个池抽多少条。初版建基线够用；人工核对是瓶颈，加大之前先想清楚谁来判。
#: 每池抽样上限。12 → 24 的理由：前三轮每修好一类错误拒答，blocked 池就换一批更难的
#: 进来，12 条的分母让「错误拒答率」在 9%↔33% 之间大幅摆动，读不出趋势。
#: 抽样是确定性轮转（不随机），扩大上限只会**追加**句子，已填结论按内容哈希保留。
AUDIT_SAMPLE_PER_POOL = 24

AUDIT_PATH = Path(__file__).resolve().parent.parent / "tests" / "golden" / "audit.yaml"

#: 两个池各自的合法结论。
AUDIT_VERDICTS = {
    "passed": ("supported", "unsupported"),
    "blocked": ("correct_block", "wrong_block"),
}


def _sentence_key(text: str) -> str:
    """句子的稳定标识：归一化空白后取哈希前 8 位。"""
    normalized = " ".join(text.split())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]


def _sample_round_robin(items: list[tuple[str, Any]], limit: int) -> list[Any]:
    """按 case 轮转抽样，保证覆盖多个用例而不是全挤在一条回答里。

    不用随机：评测要可回归，同一批录制每次抽到的必须是同一批句子。
    """
    buckets: dict[str, list[Any]] = {}
    for key, item in items:
        buckets.setdefault(key, []).append(item)
    out: list[Any] = []
    round_no = 0
    while len(out) < limit and any(len(v) > round_no for v in buckets.values()):
        for key in sorted(buckets):
            if len(out) >= limit:
                break
            if len(buckets[key]) > round_no:
                out.append(buckets[key][round_no])
        round_no += 1
    return out


def build_audit_items(outs: list[GateOutcome]) -> list[dict[str, Any]]:
    """从回放结果里挑出需要人判的句子。

    两个池对应两项无法自动判定的指标：

      passed  已放行的事实句 —— 绑定的证据**真的支撑**这句话吗？
                                判「不支撑」即为无据断言漏出（目标 0）
      blocked 被拦下的句子   —— 这一拦**是不是拦错了**？判「不该拦」即错误拒答

    自动层能查「编号有效」「数值能在证据里找到」，查不了「这条证据是否回答了这句话」。
    所以脚本只负责把材料摆好，结论由人填回 `audit.yaml`，再由脚本算比率。
    """
    passed_pool: list[tuple[str, dict[str, Any]]] = []
    blocked_pool: list[tuple[str, dict[str, Any]]] = []
    for o in outs:
        if not o.binding:
            continue
        by_label = {e.label.upper(): e for e in o.rec.evidence}
        for v in o.binding.verdicts:
            if not v.text.strip():
                continue
            excerpt = {
                lb: by_label[lb].text[:400] for lb in v.labels if lb in by_label
            }
            item = {
                # id 按**句子内容**取哈希，不用序号：改一条闸门规则就会让某些句子不再
                # 被拦，序号随之错位，已填的结论会整片错配到别的句子上。
                # 内容哈希让结论跟着句子走，规则改动后仍可对比。
                "id": f"{o.case.id}#{_sentence_key(v.text)}",
                "case": o.case.id,
                "sentence": v.text.strip(),
                "labels": list(v.labels),
                "evidence_excerpt": excerpt,
                "verdict": "",
                "note": "",
            }
            if v.ok and v.kind == "factual" and v.labels:
                passed_pool.append((o.case.id, {**item, "pool": "passed"}))
            elif not v.ok:
                blocked_pool.append((
                    o.case.id, {**item, "pool": "blocked", "gate_reason": v.reason}
                ))
    return (
        _sample_round_robin(passed_pool, AUDIT_SAMPLE_PER_POOL)
        + _sample_round_robin(blocked_pool, AUDIT_SAMPLE_PER_POOL)
    )


def load_audit() -> dict[str, dict[str, Any]]:
    """读已有结论，按 id 索引。文件不存在返回空。"""
    if not AUDIT_PATH.is_file():
        return {}
    raw = yaml.safe_load(AUDIT_PATH.read_text(encoding="utf-8")) or {}
    return {
        str(it["id"]): it for it in (raw.get("items") or []) if isinstance(it, dict)
    }


def write_audit_skeleton(items: list[dict[str, Any]]) -> tuple[int, int]:
    """写审计清单，**保留已填的结论**。返回 (总条数, 待判条数)。"""
    existing = load_audit()
    merged: list[dict[str, Any]] = []
    for item in items:
        old = existing.get(item["id"])
        if old and str(old.get("verdict") or "").strip():
            item = {**item, "verdict": old["verdict"], "note": old.get("note", "")}
        merged.append(item)
    pending = sum(1 for it in merged if not str(it.get("verdict") or "").strip())
    raw = yaml.safe_load(AUDIT_PATH.read_text(encoding="utf-8")) if AUDIT_PATH.is_file() else {}
    meta = (raw or {}).get("meta") or {}
    payload = {
        "meta": {
            "auditor": meta.get("auditor", ""),
            "audited_at": meta.get("audited_at", ""),
            "note": "verdict 取值：passed 池 supported/unsupported；"
                    "blocked 池 correct_block/wrong_block。留空表示待判。",
        },
        "items": merged,
    }
    AUDIT_PATH.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return len(merged), pending


def summarize_audit(items: list[dict[str, Any]]) -> dict:
    """算三项需人判的指标。分母只算**已填结论**的条目，并如实报出抽样量。"""
    filled = [it for it in items if str(it.get("verdict") or "").strip()]
    passed = [it for it in filled if it.get("pool") == "passed"]
    blocked = [it for it in filled if it.get("pool") == "blocked"]
    bad_verdicts = [
        it["id"] for it in filled
        if it["verdict"] not in AUDIT_VERDICTS.get(str(it.get("pool")), ())
    ]
    supported = sum(1 for it in passed if it["verdict"] == "supported")
    leaked = sum(1 for it in passed if it["verdict"] == "unsupported")
    wrong = sum(1 for it in blocked if it["verdict"] == "wrong_block")
    return {
        "sample": {
            "total": len(items),
            "judged": len(filled),
            "pending": len(items) - len(filled),
            "invalid_verdicts": bad_verdicts,
        },
        "citation_correctness": {
            "ok": supported, "total": len(passed),
            "rate": (supported / len(passed)) if passed else None,
        },
        "leakage": {
            "leaked": leaked, "total": len(passed),
            "rate": (leaked / len(passed)) if passed else None,
            "ids": [it["id"] for it in passed if it["verdict"] == "unsupported"],
        },
        "wrong_refusal": {
            "wrong": wrong, "total": len(blocked),
            "rate": (wrong / len(blocked)) if blocked else None,
            "ids": [it["id"] for it in blocked if it["verdict"] == "wrong_block"],
        },
    }


def print_audit_report(s: dict, *, auditor: str = "") -> None:
    sm = s["sample"]
    print(f"\n{'=' * 76}")
    print("人工审计指标（抽样，非全量）")
    print("=" * 76)
    cc, lk, wr = s["citation_correctness"], s["leakage"], s["wrong_refusal"]
    print(f"  引用正确率           {_pct(cc['rate'])}  ({cc['ok']}/{cc['total']} 已放行事实句"
          f"，绑定证据真的支撑该句)")
    print(f"  无据断言漏出率       {_pct(lk['rate'])}  ({lk['leaked']}/{lk['total']})  目标 0")
    print(f"  错误拒答率           {_pct(wr['rate'])}  ({wr['wrong']}/{wr['total']} 被拦句中"
          f"其实该放行的)")
    print(f"\n  抽样 {sm['total']} 条，已判 {sm['judged']}，待判 {sm['pending']}"
          + (f"　判定人：{auditor}" if auditor else ""))
    print("  **这三项是抽样结果，不代表全量。** 分母就是上面的已判条数。")
    if lk["ids"]:
        print(f"  漏出：{', '.join(lk['ids'])}")
    if wr["ids"]:
        print(f"  错误拒答：{', '.join(wr['ids'])}")
    if sm["invalid_verdicts"]:
        print(f"  **结论取值非法**：{', '.join(sm['invalid_verdicts'])}")


def print_sweep(conn, cases: list[Case], base_params: dict, values: list[float]) -> None:
    """扫 min_coverage，把「错误拒答 ↔ 漏出」的成对代价摆在一张表里。"""
    print(f"\n{'=' * 76}")
    print("min_coverage 扫描（其余参数不变）")
    print("=" * 76)
    print(f"  {'min_cov':>8} {'错误拒答':>10} {'漏出':>8} {'Recall@20':>11}  失败用例")
    for v in values:
        params = dict(base_params, min_coverage=v)
        outs = [run_case(conn, c, params) for c in cases if c.kind != "tool_route"]
        s = summarize(outs)
        pos, neg = s["sufficiency"]["positive"], s["sufficiency"]["negative"]
        ids = (pos["failed_ids"] + neg["failed_ids"])[:4]
        print(f"  {v:>8.2f} {pos['wrong_refusal']:>6}/{pos['total']:<3} "
              f"{neg['leakage']:>4}/{neg['total']:<3} "
              f"{_pct(s['recall']['recall@20']['rate']):>11}  {', '.join(ids)}")
    print("\n  覆盖率判据只影响「无实体精确命中」的那部分查询：别名表命中是最强证据，"
          "不受该阈值影响。")


def main() -> int:
    ap = argparse.ArgumentParser(description="黄金问答集评测（检索侧，离线）")
    ap.add_argument("--kind", default="", help=f"只跑某几类，逗号分隔：{','.join(KINDS)}")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条")
    ap.add_argument("--min-coverage", type=float, default=None,
                    help="覆盖 config.yaml 的 retrieval.min_coverage")
    ap.add_argument("--sweep", default="", help="扫 min_coverage，逗号分隔，如 0.3,0.5,0.7")
    ap.add_argument("--json", dest="json_path", default="", help="指标写入该 JSON 文件")
    ap.add_argument("-v", "--verbose", action="store_true", help="每条都打明细")
    ap.add_argument("--record", action="store_true",
                    help="跑真实链路并录制响应（**需要 LLM 凭证、会消耗 token**）")
    ap.add_argument("--gate", action="store_true",
                    help="用已有录制回放出口闸门指标（离线）")
    ap.add_argument("--audit", action="store_true",
                    help="生成/刷新人工审计清单（tests/golden/audit.yaml）并算三项人判指标")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    conn = dbmod.init_db(cfg.db_path)

    cases = load_cases()
    if args.kind:
        wanted = {k.strip() for k in args.kind.split(",") if k.strip()}
        unknown = wanted - set(KINDS)
        if unknown:
            print(f"未知类别：{sorted(unknown)}，可选 {KINDS}")
            return 2
        cases = [c for c in cases if c.kind in wanted]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("没有匹配的用例")
        return 2

    min_coverage = (
        args.min_coverage if args.min_coverage is not None
        else float(cfg.get("retrieval.min_coverage", 0.5))
    )
    params = {
        "top_k": int(cfg.get("retrieval.top_k", 20)),
        "min_score": float(cfg.get("retrieval.min_score", 0.35)),
        "min_coverage": min_coverage,
        "rrf_k": int(cfg.get("retrieval.rrf_k", 60)),
        "priority_decay": float(cfg.get("retrieval.priority_decay", 0.08)),
        "vector_enabled": bool(cfg.get("retrieval.vector.enabled", False)),
        "rerank_enabled": bool(cfg.get("retrieval.rerank.enabled", False)),
    }

    counts = {
        t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("entity", "chunk", "mod_affix")
    }
    print("=" * 76)
    print("POE1-Agent 评测：黄金问答集")
    print("=" * 76)
    print(f"  用例 {len(cases)} 条 | 库内 entity {counts['entity']} / "
          f"chunk {counts['chunk']} / mod_affix {counts['mod_affix']}")
    print(f"  min_coverage={min_coverage:.2f} top_k={params['top_k']} "
          f"vector={params['vector_enabled']} rerank={params['rerank_enabled']}")

    if args.sweep:
        values = [float(x) for x in args.sweep.split(",") if x.strip()]
        print_sweep(conn, cases, params, values)
        dbmod.close_db()
        return 0

    if args.record:
        print(f"\n  录制 {len(cases)} 条（真实调用，会消耗 token）…")
        for i, case in enumerate(cases, start=1):
            try:
                rec = record_case(case)
            except Exception as exc:  # noqa: BLE001  单条失败不该中断整批
                print(f"  [{i}/{len(cases)}] {case.id} 录制失败：{type(exc).__name__}: {exc}")
                continue
            path = rec.save()
            kind = "缺据路径（未调模型）" if rec.evidence_gap else (
                f"{rec.model} / {rec.tokens.get('total', 0)} token / 拦 {rec.blocked_claims}"
            )
            print(f"  [{i}/{len(cases)}] {case.id} → {path.name}　{kind}")
        args.gate = True

    if args.audit:
        recs = load_recordings()
        pending_cases = [c for c in cases if c.id in recs]
        if not pending_cases:
            print("\n  没有可回放的录制。先跑 `--record`。")
            dbmod.close_db()
            return 2
        outs = [replay_case(conn, c, recs[c.id], params) for c in pending_cases]
        items = build_audit_items(outs)
        total, pending = write_audit_skeleton(items)
        merged = list(load_audit().values())
        summary = summarize_audit([it for it in merged if it["id"] in
                                   {x["id"] for x in items}])
        raw = yaml.safe_load(AUDIT_PATH.read_text(encoding="utf-8")) or {}
        print_audit_report(summary, auditor=str((raw.get("meta") or {}).get("auditor") or ""))
        print(f"\n  清单已写入 {AUDIT_PATH}（共 {total} 条，待判 {pending}）")
        print("  填法：把每条的 verdict 改成对应池的取值，note 写判断理由。")
        dbmod.close_db()
        return 0

    if args.gate:
        recs = load_recordings()
        pending = [c for c in cases if c.id in recs]
        if not pending:
            print("\n  没有可回放的录制。先跑 `--record`（需要 LLM 凭证）。")
            dbmod.close_db()
            return 2
        outs = [replay_case(conn, c, recs[c.id], params) for c in pending]
        gate_summary = summarize_gate(outs)
        print_gate_report(outs, gate_summary, verbose=args.verbose)
        missing = [c.id for c in cases if c.id not in recs]
        if missing:
            print(f"\n  未录制（不计入上面的指标）：{len(missing)} 条 "
                  f"→ {', '.join(missing[:6])}{' …' if len(missing) > 6 else ''}")
        dbmod.close_db()
        stale = gate_summary["recordings"]["stale"]
        wrong = gate_summary["blocked"]["definitely_wrong"]
        parity = gate_summary["stream_parity"]
        failed = parity["total"] - parity["consistent"]
        return 1 if (stale or wrong or failed) else 0

    outcomes = [run_case(conn, c, params) for c in cases]
    summary = summarize(outcomes)
    print_report(outcomes, summary, verbose=args.verbose)

    if args.json_path:
        payload = {
            "params": params,
            "db": counts,
            "summary": summary,
            "cases": [
                {
                    "id": o.case.id, "kind": o.case.kind, "passed": o.passed,
                    "sufficient": o.sufficient, "hit_rank": o.hit_rank,
                    "top_coverage": round(o.top_coverage, 4),
                    "evidence_recalled": o.evidence_recalled, "reason": o.reason,
                }
                for o in outcomes
            ],
        }
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n  指标已写入 {args.json_path}")

    dbmod.close_db()
    # 漏出是硬指标（tasks 2.7 写明目标 0）；错误拒答先建基线，只报告不判失败。
    return 1 if summary["sufficiency"]["negative"]["leakage"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
