"""用户显式 Boss 命中时间轴、持续恢复与恢复门槛工具。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

from calc import boss_timeline
from calc.boss_timeline import TimedDamageEvent, TimedSequenceResult, TimedSequenceStep
from calc.derive import fmt
from tools.impl.boss import boss_sequence_check
from tools.impl.boss import boss_sequence_from_build
from tools.registry import REGISTRY, ToolResult

if TYPE_CHECKING:
    from parsers.pob_build import PobBuild


BOUNDARY = (
    "只结算用户显式排列并标注时间的 PoB Boss 单类型命中。恢复是一个共享防御池上的固定"
    "每秒数值，可选每次受击后恢复延迟；低/高伤害掷骰分别沿独立轨迹计算。输入不是游戏"
    "真实连招，不含持续伤害、格挡、闪避、法术压制、生命/护盾分池、回充中断细节、偷取"
    "实例、药剂、Ward、Mind over Matter、阶段机制或打法"
)


_EVENTS_SCHEMA = {
    "type": "array", "minItems": 1, "maxItems": 100,
    "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "skill": {"type": "string", "minLength": 2, "maxLength": 120},
            "at_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
        },
        "required": ["skill", "at_seconds"],
    },
}


_INPUT_PROPERTIES = {
    "events": _EVENTS_SCHEMA,
    "level": {"type": "integer", "minimum": 1, "maximum": 100},
    "pool": {"type": "number", "exclusiveMinimum": 0},
    "uber": {"type": "boolean", "default": False},
    "roll_range": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
    "fire_resist_pct": {"type": ["number", "null"], "minimum": -200, "maximum": 90},
    "cold_resist_pct": {"type": ["number", "null"], "minimum": -200, "maximum": 90},
    "lightning_resist_pct": {"type": ["number", "null"], "minimum": -200, "maximum": 90},
    "chaos_resist_pct": {"type": ["number", "null"], "minimum": -200, "maximum": 90},
    "armour": {"type": "number", "minimum": 0, "default": 0},
    "flat_physical_reduction_pct": {
        "type": "number", "minimum": 0, "maximum": 90, "default": 0,
    },
    "taken_mult": {"type": "number", "exclusiveMinimum": 0, "default": 1},
    "overwhelm_pct": {"type": "number", "minimum": 0, "maximum": 100, "default": 0},
    "recovery_per_second": {"type": "number", "minimum": 0, "default": 0},
    "recovery_delay_seconds": {"type": "number", "minimum": 0, "maximum": 3600,
                               "default": 0},
}


def _validate_event_rows(events: object) -> list[dict[str, Any]]:
    if not isinstance(events, list) or not 1 <= len(events) <= 100:
        raise ValueError("events 必须包含 1..100 次命中")
    normalized: list[dict[str, Any]] = []
    previous = -1.0
    for index, item in enumerate(events):
        if not isinstance(item, dict):
            raise ValueError(f"events[{index}] 必须是对象")
        unknown = set(item) - {"skill", "at_seconds"}
        if unknown:
            raise ValueError(
                f"events[{index}] 包含未知字段：{', '.join(sorted(unknown))}"
            )
        skill = str(item.get("skill") or "").strip()
        if len(skill) < 2 or len(skill) > 120:
            raise ValueError(f"events[{index}].skill 必须为 2..120 个字符")
        at = item.get("at_seconds")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            raise ValueError(f"events[{index}].at_seconds 必须是数字")
        seconds = float(at)
        if not math.isfinite(seconds) or not 0 <= seconds <= 3600:
            raise ValueError(f"events[{index}].at_seconds 必须在 0..3600")
        if seconds < previous:
            raise ValueError("events 必须按 at_seconds 非递减排列")
        normalized.append({"skill": skill, "at_seconds": seconds})
        previous = seconds
    return normalized


def _clear_timeline_metrics(data: dict[str, Any]) -> None:
    """移除无恢复预检留下的同名数值，避免 unknown 响应泄漏局部结论。"""
    for key in TimedSequenceResult.__dataclass_fields__:
        if key != "pool":
            data.pop(key, None)


def _render_recovery_requirement(
    status: str, value: float | None, *, exclusive: bool,
) -> str:
    if status == "not_needed":
        return "0（不需要恢复也能存活）"
    if status == "unreachable":
        return "不可达（存在恢复间隔无法覆盖的致死命中组）"
    operator = ">" if exclusive else "≥"
    return f"{operator} {fmt(value or 0)}/秒"


def _render(result: TimedSequenceResult, *, level: int, uber: bool) -> str:
    labels = {
        "survives_full_range": "完整伤害区间都能存活",
        "roll_dependent": "取决于伤害掷骰",
        "fails_full_range": "即使最低掷骰也会耗尽防御池",
    }
    lines = [
        "### Boss 定时命中与恢复时间轴" + ("（Uber）" if uber else ""), "",
        f"**结论：{labels[result.outcome]}**",
        f"- 敌人等级 {level}；防御池 {fmt(result.pool)}；"
        f"恢复 {fmt(result.recovery_per_second)}/秒；受击后恢复延迟 "
        f"{result.recovery_delay_seconds:g} 秒",
        f"- 共 {result.hit_count} 次命中，最后事件在 {result.duration_seconds:g} 秒", "",
        "| # | 时间 | 技能 | 类型 | 承伤区间 | 恢复有效窗口 | 受击前池（低/高） | "
        "受击后池（低/高） |",
        "| ---: | ---: | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for step in result.steps:
        lines.append(
            f"| {step.hit_index} | {step.at_seconds:g}s | {step.skill} | "
            f"{step.damage_type} | {fmt(step.taken_low)}–{fmt(step.taken_high)} | "
            f"{step.recovery_eligible_seconds:g}s | "
            f"{fmt(step.best_case_pool_before_hit)} / "
            f"{fmt(step.worst_case_pool_before_hit)} | "
            f"{fmt(step.best_case_remaining)} / {fmt(step.worst_case_remaining)} |"
        )
    lines.extend([
        "", f"- 低掷骰总承伤 / 实际恢复 / 末尾池：{fmt(result.total_taken_low)} / "
        f"{fmt(result.total_recovered_best_case)} / "
        f"{fmt(result.ending_remaining_best_case)}",
        f"- 高掷骰总承伤 / 实际恢复 / 末尾池：{fmt(result.total_taken_high)} / "
        f"{fmt(result.total_recovered_worst_case)} / "
        f"{fmt(result.ending_remaining_worst_case)}",
        f"- 当前恢复下最低初始池：至少应对部分掷骰 {result.minimum_pool_for_any_roll}；"
        f"覆盖完整区间 {result.minimum_pool_for_full_range}",
        f"- 当前池子所需恢复：至少应对部分掷骰 "
        f"{_render_recovery_requirement(result.required_recovery_for_any_roll_status, result.required_recovery_for_any_roll, exclusive=result.required_recovery_for_any_roll_exclusive)}；"
        f"覆盖完整区间 "
        f"{_render_recovery_requirement(result.required_recovery_for_full_range_status, result.required_recovery_for_full_range, exclusive=result.required_recovery_for_full_range_exclusive)}",
    ])
    if result.earliest_possible_depletion_hit is not None:
        lines.append(f"- 最早可能在第 {result.earliest_possible_depletion_hit} 次命中耗尽")
    if result.guaranteed_depletion_hit is not None:
        lines.append(f"- 最迟到第 {result.guaranteed_depletion_hit} 次命中必然耗尽")
    lines.extend(["", f"> 边界：{BOUNDARY}。"])
    return "\n".join(lines)


@REGISTRY.register(
    name="boss_timed_sequence_check",
    description=(
        "按用户显式时间排列的 1–100 次 Boss 命中结算持续恢复、受击后恢复延迟、低/高掷骰"
        "双轨、耗尽时点、最低初始池和所需恢复速率；不推断真实 Boss 连招。"
    ),
    parameters={
        "type": "object", "properties": _INPUT_PROPERTIES,
        "required": ["events", "level", "pool"], "additionalProperties": False,
    },
)
def boss_timed_sequence_check(
    events: list[dict[str, Any]], level: int, pool: float,
    uber: bool = False, roll_range: float | None = None,
    fire_resist_pct: float | None = None, cold_resist_pct: float | None = None,
    lightning_resist_pct: float | None = None, chaos_resist_pct: float | None = None,
    armour: float = 0.0, flat_physical_reduction_pct: float = 0.0,
    taken_mult: float = 1.0, overwhelm_pct: float = 0.0,
    recovery_per_second: float = 0.0, recovery_delay_seconds: float = 0.0,
) -> ToolResult:
    try:
        normalized = _validate_event_rows(events)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    base = boss_sequence_check(
        sequence=[{"skill": item["skill"], "count": 1} for item in normalized],
        level=level, pool=pool, uber=uber, roll_range=roll_range,
        fire_resist_pct=fire_resist_pct, cold_resist_pct=cold_resist_pct,
        lightning_resist_pct=lightning_resist_pct, chaos_resist_pct=chaos_resist_pct,
        armour=armour, flat_physical_reduction_pct=flat_physical_reduction_pct,
        taken_mult=taken_mult, overwhelm_pct=overwhelm_pct,
    )
    if not base.ok:
        return base
    base_data = dict(base.data or {})
    if not base_data.get("assessable"):
        _clear_timeline_metrics(base_data)
        base_data.update({
            "requested_events": normalized,
            "recovery_per_second": recovery_per_second,
            "recovery_delay_seconds": recovery_delay_seconds,
            "boundary": BOUNDARY,
            "answer": f"{base_data.get('answer', '')}\n> 定时时间轴边界：{BOUNDARY}。",
        })
        return ToolResult(
            ok=True, data=base_data, citations=base.citations, meta=base.meta
        )
    try:
        timed_events = tuple(TimedDamageEvent(
            at_seconds=float(event["at_seconds"]), skill=str(step["skill"]),
            damage_type=str(step["damage_type"]),
            raw_low=float(step["raw_low"]), raw_high=float(step["raw_high"]),
            taken_low=float(step["taken_low"]), taken_high=float(step["taken_high"]),
        ) for event, step in zip(normalized, base_data["steps"], strict=True))
        assessed = boss_timeline.assess_timed_sequence(
            events=timed_events, pool=pool,
            recovery_per_second=recovery_per_second,
            recovery_delay_seconds=recovery_delay_seconds,
        )
    except (ValueError, TypeError, boss_timeline.BossTimelineError) as exc:
        return ToolResult(ok=False, error=str(exc))
    answer = _render(assessed, level=level, uber=uber)
    data = {
        "found": True, "assessable": True, "verdict": assessed.outcome,
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized, **assessed.as_dict(),
        "missing_inputs": [], "candidates": [], "boundary": BOUNDARY,
        "answer": answer,
    }
    return ToolResult(
        ok=True, data=data, citations=base.citations,
        meta={
            "channel": "derived", "tables": ["pob_boss_skill", "game_constant"],
            "verdict": assessed.outcome, "mode": "explicit_timed_hit_sequence",
        },
    )


def boss_timed_sequence_from_build(
    build: "PobBuild", *, events: list[dict[str, Any]], level: int,
    uber: bool = False, roll_range: float | None = None,
    pool_mode: Literal["life", "life_and_energy_shield"] = "life",
    recovery_per_second: float | None = None,
    recovery_delay_seconds: float = 0.0,
) -> ToolResult:
    """从 Build 基础防御快照取得逐击承伤，再推进显式恢复时间轴。

    只有纯生命池可从 PlayerStat 自动选择净生命回复；生命+护盾是两个不同池，除非用户
    显式给一个共享恢复值，否则保持 unknown，不把两个回复来源随意相加。
    """
    try:
        normalized = _validate_event_rows(events)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    base = boss_sequence_from_build(
        build,
        sequence=[{"skill": item["skill"], "count": 1} for item in normalized],
        level=level, uber=uber, roll_range=roll_range, pool_mode=pool_mode,
    )
    if not base.ok:
        return base
    base_data = dict(base.data or {})
    if not base_data.get("assessable"):
        _clear_timeline_metrics(base_data)
        base_data.update({
            "requested_events": normalized,
            "recovery_per_second": recovery_per_second or 0.0,
            "recovery_delay_seconds": recovery_delay_seconds,
            "boundary": f"{BOUNDARY}；{base_data.get('boundary', '')}",
            "answer": f"{base_data.get('answer', '')}\n> 定时恢复边界：{BOUNDARY}。",
        })
        return ToolResult(ok=True, data=base_data, citations=base.citations, meta=base.meta)

    source_fields = dict(base_data.get("source_fields") or {})
    recovery_source = "explicit_request"
    rate = recovery_per_second
    if rate is None:
        if pool_mode != "life":
            reason = (
                "生命与能量护盾是独立池，Build 快照不能安全合成为一个共享恢复速率；"
                "请显式提供 recovery_per_second 或改用 life 池"
            )
            _clear_timeline_metrics(base_data)
            base_data.update({
                "assessable": False, "verdict": "unknown", "outcome": None,
                "requested_events": normalized, "recovery_per_second": 0.0,
                "recovery_delay_seconds": recovery_delay_seconds,
                "steps": [], "reason": reason, "missing_inputs": ["共享防御池恢复速率"],
                "boundary": f"{BOUNDARY}；{base_data.get('boundary', '')}",
                "answer": (
                    "### Build 的 Boss 定时命中恢复时间轴\n\n**结论：无法安全计算**\n"
                    f"- 原因：{reason}\n\n> {BOUNDARY}。"
                ),
            })
            return ToolResult(
                ok=True, data=base_data, citations=base.citations,
                meta={**base.meta, "verdict": "unknown", "mode": "build_timed_sequence"},
            )
        recovery_key = next((key for key in (
            "NetLifeRegen", "LifeRegenRecovery", "LifeRegen",
        ) if key in build.player_stats), None)
        if recovery_key is None:
            reason = "Build PlayerStat 缺少可用的净生命回复字段"
            _clear_timeline_metrics(base_data)
            base_data.update({
                "assessable": False, "verdict": "unknown", "outcome": None,
                "requested_events": normalized, "recovery_per_second": 0.0,
                "recovery_delay_seconds": recovery_delay_seconds,
                "steps": [], "reason": reason,
                "missing_inputs": ["NetLifeRegen（或 LifeRegenRecovery / LifeRegen）"],
                "boundary": f"{BOUNDARY}；{base_data.get('boundary', '')}",
                "answer": (
                    "### Build 的 Boss 定时命中恢复时间轴\n\n**结论：无法安全计算**\n"
                    f"- 原因：{reason}\n\n> {BOUNDARY}。"
                ),
            })
            return ToolResult(
                ok=True, data=base_data, citations=base.citations,
                meta={**base.meta, "verdict": "unknown", "mode": "build_timed_sequence"},
            )
        rate = build.player_stats[recovery_key]
        if not math.isfinite(rate) or rate < 0:
            reason = f"Build 的 {recovery_key} 不是可用于固定正恢复模型的非负有限数字"
            _clear_timeline_metrics(base_data)
            base_data.update({
                "assessable": False, "verdict": "unknown", "outcome": None,
                "requested_events": normalized, "recovery_per_second": 0.0,
                "recovery_delay_seconds": recovery_delay_seconds,
                "steps": [], "reason": reason,
                "missing_inputs": [f"有效的 {recovery_key}"],
                "boundary": f"{BOUNDARY}；{base_data.get('boundary', '')}",
                "answer": (
                    "### Build 的 Boss 定时命中恢复时间轴\n\n**结论：无法安全计算**\n"
                    f"- 原因：{reason}\n\n> {BOUNDARY}。"
                ),
            })
            return ToolResult(
                ok=True, data=base_data, citations=base.citations,
                meta={**base.meta, "verdict": "unknown", "mode": "build_timed_sequence"},
            )
        source_fields[recovery_key] = float(rate)
        recovery_source = f"pob_player_stat:{recovery_key}"

    try:
        timed_events = tuple(TimedDamageEvent(
            at_seconds=float(event["at_seconds"]), skill=str(step["skill"]),
            damage_type=str(step["damage_type"]),
            raw_low=float(step["raw_low"]), raw_high=float(step["raw_high"]),
            taken_low=float(step["taken_low"]), taken_high=float(step["taken_high"]),
        ) for event, step in zip(normalized, base_data["steps"], strict=True))
        assessed = boss_timeline.assess_timed_sequence(
            events=timed_events, pool=float(base_data["pool"]),
            recovery_per_second=float(rate),
            recovery_delay_seconds=recovery_delay_seconds,
        )
    except (ValueError, TypeError, boss_timeline.BossTimelineError) as exc:
        return ToolResult(ok=False, error=str(exc))
    answer = _render(assessed, level=level, uber=uber)
    answer += (
        f"\n> Build 快照输入：{', '.join(source_fields)}；恢复来源：{recovery_source}。"
        "PoB 单击 MaximumHitTaken 未用于混合时间轴。"
    )
    data = {
        "found": True, "assessable": True, "verdict": assessed.outcome,
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized, **assessed.as_dict(),
        "pool_mode": pool_mode, "input_source": "pob_player_stat",
        "source_fields": source_fields,
        "source_config": {"recovery_source": recovery_source},
        "calculation_mode": "build_basic_defence_timed_sequence",
        "fallback_reason": (
            "PoB *MaximumHitTaken 是单击阈值，不能拆分为带恢复的混合命中时间轴"
        ),
        "missing_inputs": [], "candidates": [], "boundary": BOUNDARY,
        "answer": answer,
    }
    return ToolResult(
        ok=True, data=data, citations=base.citations,
        meta={
            **base.meta, "verdict": assessed.outcome,
            "mode": "build_explicit_timed_hit_sequence",
        },
    )


@REGISTRY.register(
    name="boss_timed_sequence_compare",
    description=(
        "比较 2–10 套显式 Boss 命中时间轴/恢复方案，按存活结论、最坏余量、最低初始池与"
        "所需恢复速率排序。"
    ),
    parameters={
        "type": "object", "properties": {
            "scenarios": {
                "type": "array", "minItems": 2, "maxItems": 10,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 80},
                        "inputs": {
                            "type": "object", "properties": _INPUT_PROPERTIES,
                            "required": ["events", "level", "pool"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["name", "inputs"],
                },
            },
        },
        "required": ["scenarios"], "additionalProperties": False,
    },
)
def boss_timed_sequence_compare(scenarios: list[dict[str, Any]]) -> ToolResult:
    if not isinstance(scenarios, list) or not 2 <= len(scenarios) <= 10:
        return ToolResult(ok=False, error="scenarios 必须包含 2..10 个方案")
    calculated: list[tuple[str, TimedSequenceResult]] = []
    citations: list[dict[str, Any]] = []
    seen_citations: set[tuple[object, object]] = set()
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict) or set(scenario) != {"name", "inputs"}:
            return ToolResult(ok=False, error=f"scenarios[{index}] 必须只包含 name 和 inputs")
        name = str(scenario["name"]).strip()
        inputs = scenario["inputs"]
        if not isinstance(inputs, dict):
            return ToolResult(ok=False, error=f"scenarios[{index}].inputs 必须是对象")
        checked = boss_timed_sequence_check(**inputs)
        if not checked.ok:
            return ToolResult(ok=False, error=f"方案「{name}」：{checked.error}")
        if not (checked.data or {}).get("assessable"):
            return ToolResult(
                ok=False,
                error=f"方案「{name}」无法安全计算：{(checked.data or {}).get('reason', '未知原因')}",
            )
        result_data = dict(checked.data or {})
        result_data.pop("answer", None)
        result_data.pop("boundary", None)
        result_payload = {
            key: result_data[key] for key in TimedSequenceResult.__dataclass_fields__
        }
        result_payload["steps"] = tuple(
            TimedSequenceStep(**step) for step in result_payload["steps"]
        )
        calculated.append((name, TimedSequenceResult(**result_payload)))
        for citation in checked.citations:
            key = (citation.get("chunk_id"), citation.get("url"))
            if key not in seen_citations:
                seen_citations.add(key)
                citations.append(citation)
    try:
        comparison = boss_timeline.compare_timed_sequences(calculated)
    except boss_timeline.BossTimelineError as exc:
        return ToolResult(ok=False, error=str(exc))
    lines = [
        "### Boss 定时命中恢复方案比较", "",
        "| 方案 | 结论 | 最坏末尾池 | 最坏最低池 | 完整区间最低初始池 | 完整区间所需恢复 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in comparison.scenarios:
        required = (
            "不可达" if row.required_recovery_for_full_range is None
            else f"{'>' if row.required_recovery_for_full_range_exclusive else '≥'} "
            f"{fmt(row.required_recovery_for_full_range)}/秒"
        )
        lines.append(
            f"| {row.name} | {row.outcome} | {fmt(row.ending_remaining_worst_case)} | "
            f"{fmt(row.lowest_remaining_worst_case)} | {row.minimum_pool_for_full_range} | "
            f"{required} |"
        )
    lines.extend([
        "", f"- 最佳存活结论：{comparison.best_survival_scenario}",
        f"- 最佳最坏余量：{comparison.best_worst_case_margin_scenario}",
        f"- 最低完整区间初始池：{comparison.lowest_required_pool_scenario}",
        f"- 最低可达恢复门槛：{comparison.lowest_required_recovery_scenario or '全部不可达'}",
        "", f"> 边界：{BOUNDARY}。",
    ])
    answer = "\n".join(lines)
    data = comparison.as_dict()
    data.update({"kind": "boss_timed_sequence_comparison", "boundary": BOUNDARY,
                 "answer": answer})
    return ToolResult(
        ok=True, data=data, citations=citations,
        meta={"channel": "derived", "mode": "timed_hit_sequence_comparison"},
    )
