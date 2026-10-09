"""Boss Ward 破裂、恢复与命中吸收混合时间轴工具。"""

from __future__ import annotations

import copy
import math
from typing import TYPE_CHECKING, Any, Literal

from calc import boss_ward
from calc.boss_ward import WardConfig, WardTimelineResult
from tools.impl.boss_dot import (
    _INPUT_PROPERTIES as _DOT_INPUT_PROPERTIES,
    _config,
    _layered_events,
    _recovery_events,
    _resolve_dots,
    _validate_dot_requests,
    _validate_hits,
    _validate_recovery_requests,
    boss_timed_sequence_check,
)
from tools.impl.boss_layered import boss_layered_sequence_from_build
from tools.impl.boss_mitigation import (
    _MITIGATION_WINDOWS_SCHEMA,
    _mitigation_windows,
    _validate_mitigation_requests,
)
from tools.registry import REGISTRY, ToolResult

if TYPE_CHECKING:
    from parsers.pob_build import PobBuild


BOUNDARY = (
    "Ward 只按请求中的状态机吸收命中残余，顺序固定为临时乘数→Guard→Ward→分层池；DoT "
    "不消耗 Ward。不会从 Ward 面板值、装备、药剂、技能、职业或阶段推断当前是否就绪、"
    "恢复延迟、无法破裂效果或计时重置，也不模拟药剂充能、Buff 启停和真实服务器事件顺序"
)


_WARD_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "capacity": {
            "type": "number", "exclusiveMinimum": 0, "maximum": 1_000_000_000,
        },
        "restore_delay_seconds": {
            "type": "number", "minimum": 0, "maximum": 3600,
        },
        "initial_state": {
            "type": "string", "enum": ["ready", "broken"], "default": "ready",
        },
        "initial_restore_at_seconds": {
            "type": ["number", "null"], "minimum": 0, "maximum": 3600,
        },
        "break_mode": {
            "type": "string", "enum": ["break", "unbreakable"], "default": "break",
        },
        "broken_hit_timer_mode": {
            "type": "string", "enum": ["preserve", "reset"], "default": "preserve",
        },
    },
    "required": ["capacity", "restore_delay_seconds"],
}

_OPTIONAL_MITIGATION_SCHEMA = copy.deepcopy(_MITIGATION_WINDOWS_SCHEMA)
_OPTIONAL_MITIGATION_SCHEMA["minItems"] = 0
_OPTIONAL_MITIGATION_SCHEMA["default"] = []

_INPUT_PROPERTIES = {
    **_DOT_INPUT_PROPERTIES,
    "mitigation_windows": _OPTIONAL_MITIGATION_SCHEMA,
    "ward": _WARD_SCHEMA,
}


def _finite(
    value: object, label: str, *, minimum: float = 0,
    maximum: float = 1_000_000_000,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{label} 必须在 {minimum:g}..{maximum:g} 之间")
    return result


def _validate_ward_request(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("ward 必须是对象")
    allowed = {
        "capacity", "restore_delay_seconds", "initial_state",
        "initial_restore_at_seconds", "break_mode", "broken_hit_timer_mode",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"ward 包含未知字段：{', '.join(sorted(unknown))}")
    capacity = _finite(value.get("capacity"), "ward.capacity", minimum=1e-9)
    delay = _finite(
        value.get("restore_delay_seconds"), "ward.restore_delay_seconds",
        maximum=3600,
    )
    initial_state = value.get("initial_state", "ready")
    if initial_state not in {"ready", "broken"}:
        raise ValueError("ward.initial_state 无效")
    initial_restore = value.get("initial_restore_at_seconds")
    if initial_state == "broken":
        if initial_restore is None:
            raise ValueError("初始 broken 时必须提供 initial_restore_at_seconds")
        initial_restore = _finite(
            initial_restore, "ward.initial_restore_at_seconds", maximum=3600,
        )
    elif initial_restore is not None:
        raise ValueError("初始 ready 时不能提供 initial_restore_at_seconds")
    break_mode = value.get("break_mode", "break")
    timer_mode = value.get("broken_hit_timer_mode", "preserve")
    if break_mode not in {"break", "unbreakable"}:
        raise ValueError("ward.break_mode 无效")
    if timer_mode not in {"preserve", "reset"}:
        raise ValueError("ward.broken_hit_timer_mode 无效")
    if break_mode == "unbreakable" and (initial_state != "ready" or timer_mode != "preserve"):
        raise ValueError("unbreakable Ward 必须为 ready 且使用 preserve 计时口径")
    return {
        "capacity": capacity, "restore_delay_seconds": delay,
        "initial_state": initial_state,
        "initial_restore_at_seconds": initial_restore,
        "break_mode": break_mode, "broken_hit_timer_mode": timer_mode,
    }


def _ward_config(value: dict[str, Any]) -> WardConfig:
    return WardConfig(**value)  # type: ignore[arg-type]


def _threshold_text(value: object) -> str:
    status = getattr(value, "status")
    if status == "not_applicable":
        return "不适用"
    if status == "unreachable":
        return "单独提高该池不可达"
    return f"{getattr(value, 'value')}（缺口 {getattr(value, 'shortfall'):g}）"


def _render(result: WardTimelineResult, *, level: int, uber: bool) -> str:
    labels = {
        "survives_full_range": "完整低/高伤区间都能存活",
        "roll_dependent": "取决于命中或 DoT 的伤害区间",
        "fails_full_range": "即使低伤轨也会耗尽生命",
    }
    ward = result.ward
    lines = [
        "### Boss Ward 破裂与恢复时间轴" + ("（Uber 命中档）" if uber else ""), "",
        f"**结论：{labels[result.outcome]}**",
        f"- 敌人等级 {level}；命中 {result.hit_count} 次；DoT {result.dot_window_count} 个；"
        f"临时防御窗口 {result.mitigation_window_count} 个；时长 {result.duration_seconds:g}s",
        f"- Ward 容量 {ward.capacity:g}；恢复延迟 {ward.restore_delay_seconds:g}s；"
        f"初始 {ward.initial_state}；破裂模式 {ward.break_mode}；broken 命中计时 "
        f"{ward.broken_hit_timer_mode}",
        "", "| 轨迹 | 存活 | 临时防御后 | Ward 吸收 | Ward 后 | 保护命中/破裂/恢复 | Ward 就绪率 | 末尾生命/护盾/魔力 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, roll in (("低伤", result.low_roll), ("高伤", result.high_roll)):
        state = roll.ward_state
        timeline = roll.timeline
        lines.append(
            f"| {label} | {'是' if timeline.survived else '否'} | "
            f"{roll.damage_after_temporary_mitigation:g} | {roll.ward_absorbed:g} | "
            f"{roll.damage_after_ward:g} | {state.protected_hit_count}/{state.break_count}/"
            f"{state.restoration_count} | {state.ready_uptime_pct:g}% | {timeline.ending_life:g}/"
            f"{timeline.ending_energy_shield:g}/{timeline.ending_mana:g} |"
        )
    lines.extend(["", "#### 高伤轨逐击 Ward 审计"])
    for step in result.high_roll.hit_steps:
        state = "ready" if step.ward_ready_before else "broken"
        suffix = []
        if step.restored_before_hit:
            suffix.append("命中前恢复")
        if step.ward_broke:
            suffix.append("本击破裂")
        if step.broken_timer_reset:
            suffix.append("重置计时")
        lines.append(
            f"- #{step.hit_index} {step.at_seconds:g}s {step.skill}：{state}；"
            f"{step.damage_before_ward:g}→吸收 {step.ward_absorbed:g}→"
            f"{step.damage_after_ward:g}" + (f"（{'、'.join(suffix)}）" if suffix else "")
        )
    lines.extend(["", "#### 高伤轨 Ward 状态转换"])
    if not result.high_roll.state_transitions:
        lines.append("- 计划内没有 Ward 状态转换")
    for transition in result.high_roll.state_transitions:
        hit = f"，关联命中 #{transition.hit_index}" if transition.hit_index else ""
        restore = (
            f"，下次恢复 {transition.restore_at_after:g}s"
            if transition.restore_at_after is not None else ""
        )
        lines.append(
            f"- {transition.at_seconds:g}s {transition.kind}："
            f"{transition.state_before}→{transition.state_after}{hit}{restore}；"
            f"{transition.reason}"
        )
    impact = result.ward_impact
    lines.extend([
        "", f"- 移除 Ward 后高伤轨末尾生命：{impact.high_roll_ending_life_without_ward:g}；"
        f"当前 Ward 带来的末尾生命差：{impact.high_roll_ending_life_gain:g}",
        f"- Ward 是否改变高伤轨存活结论：{'是' if impact.changed_high_roll_survival else '否'}",
        f"- 其余层固定时门槛：生命 {_threshold_text(result.minimum_life_for_full_range)}；"
        f"护盾 {_threshold_text(result.minimum_energy_shield_for_full_range)}；"
        f"魔力 {_threshold_text(result.minimum_mana_for_full_range)}",
        "", f"> 顺序：{result.ordering_model}。",
        f"> 同刻命中：{result.simultaneous_hit_model}。",
        f"> 恢复：{result.restoration_model}。", f"> 边界：{BOUNDARY}。",
    ])
    return "\n".join(lines)


@REGISTRY.register(
    name="boss_ward_sequence_check",
    description=(
        "在显式临时减伤/Guard 之后、分层池之前推进 Ward ready/broken 状态；逐击审计吸收、"
        "破裂、恢复、broken 命中计时和无 Ward 基线，DoT 明确跳过 Ward。"
    ),
    parameters={
        "type": "object", "properties": _INPUT_PROPERTIES,
        "required": [
            "events", "dot_windows", "recovery_events", "ward", "level", "life",
        ],
        "additionalProperties": False,
    },
)
def boss_ward_sequence_check(
    events: list[dict[str, Any]], dot_windows: list[dict[str, Any]],
    recovery_events: list[dict[str, Any]], ward: dict[str, Any],
    level: int, life: float, mitigation_windows: list[dict[str, Any]] | None = None,
    energy_shield: float = 0.0, mana: float = 0.0,
    mana_before_life_pct: float = 0.0, uber: bool = False,
    roll_range: float | None = None, fire_resist_pct: float | None = None,
    cold_resist_pct: float | None = None, lightning_resist_pct: float | None = None,
    chaos_resist_pct: float | None = None, armour: float = 0.0,
    flat_physical_reduction_pct: float = 0.0, taken_mult: float = 1.0,
    overwhelm_pct: float = 0.0, life_recovery_per_second: float = 0.0,
    energy_shield_recovery_per_second: float = 0.0,
    mana_recovery_per_second: float = 0.0,
    energy_shield_recharge_per_second: float = 0.0,
    energy_shield_recharge_delay_seconds: float = 2.0,
    energy_shield_recharge_interrupt_mode: str = "energy_shield_damage",
    dot_recharge_interrupt_mode: str = "energy_shield_damage",
    life_leech_rate_cap_pct: float = 20.0,
    energy_shield_leech_rate_cap_pct: float = 10.0,
    mana_leech_rate_cap_pct: float = 20.0,
) -> ToolResult:
    try:
        normalized_hits = _validate_hits(events)
        if not normalized_hits:
            raise ValueError("Ward 时间轴至少需要一次显式命中")
        normalized_dots = _validate_dot_requests(dot_windows)
        normalized_recovery = _validate_recovery_requests(recovery_events)
        normalized_mitigation = _validate_mitigation_requests(
            mitigation_windows or [], allow_empty=True,
        )
        normalized_ward = _validate_ward_request(ward)
        resolved_dots, dot_missing, dot_audit = _resolve_dots(
            normalized_dots, fire_resist_pct=fire_resist_pct,
            cold_resist_pct=cold_resist_pct,
            lightning_resist_pct=lightning_resist_pct,
            chaos_resist_pct=chaos_resist_pct,
            flat_physical_reduction_pct=flat_physical_reduction_pct,
            taken_mult=taken_mult,
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    preflight = boss_timed_sequence_check(
        events=[{key: item[key] for key in ("skill", "at_seconds")} for item in normalized_hits],
        level=level, pool=max(1.0, life + energy_shield + mana), uber=uber,
        roll_range=roll_range, fire_resist_pct=fire_resist_pct,
        cold_resist_pct=cold_resist_pct,
        lightning_resist_pct=lightning_resist_pct,
        chaos_resist_pct=chaos_resist_pct, armour=armour,
        flat_physical_reduction_pct=flat_physical_reduction_pct,
        taken_mult=taken_mult, overwhelm_pct=overwhelm_pct,
    )
    if not preflight.ok:
        return preflight
    base = dict(preflight.data or {})
    missing = list(base.get("missing_inputs") or []) + dot_missing
    meta = {**preflight.meta, "mode": "explicit_ward_sequence"}
    if not base.get("assessable") or missing:
        reason = "命中或 raw DoT 缺少完成减伤所需的显式输入"
        return ToolResult(ok=True, data={
            "found": True, "assessable": False, "verdict": "unknown",
            "level": level, "uber": uber, "roll_range": roll_range,
            "requested_events": normalized_hits,
            "requested_dot_windows": normalized_dots,
            "resolved_dot_windows": dot_audit,
            "requested_recovery_events": normalized_recovery,
            "requested_mitigation_windows": normalized_mitigation,
            "requested_ward": normalized_ward,
            "hit_count": len(normalized_hits),
            "dot_window_count": len(normalized_dots),
            "mitigation_window_count": len(normalized_mitigation),
            "reason": reason, "missing_inputs": missing,
            "candidates": base.get("candidates", []), "boundary": BOUNDARY,
            "answer": (
                "### Boss Ward 破裂与恢复时间轴\n\n**结论：无法安全计算**\n"
                f"- 原因：{reason}\n- 缺少：{'、'.join(missing)}\n\n> 边界：{BOUNDARY}。"
            ),
        }, citations=preflight.citations, meta={**meta, "verdict": "unknown"})
    try:
        assessed = boss_ward.assess_ward_timeline(
            hit_events=_layered_events(normalized_hits, base["steps"]),
            dot_windows=resolved_dots,
            recovery_events=_recovery_events(normalized_recovery),
            mitigation_windows=_mitigation_windows(normalized_mitigation),
            ward=_ward_config(normalized_ward),
            config=_config(
                life=life, energy_shield=energy_shield, mana=mana,
                mana_before_life_pct=mana_before_life_pct,
                life_recovery_per_second=life_recovery_per_second,
                energy_shield_recovery_per_second=energy_shield_recovery_per_second,
                mana_recovery_per_second=mana_recovery_per_second,
                energy_shield_recharge_per_second=energy_shield_recharge_per_second,
                energy_shield_recharge_delay_seconds=energy_shield_recharge_delay_seconds,
                energy_shield_recharge_interrupt_mode=energy_shield_recharge_interrupt_mode,
                dot_recharge_interrupt_mode=dot_recharge_interrupt_mode,
                life_leech_rate_cap_pct=life_leech_rate_cap_pct,
                energy_shield_leech_rate_cap_pct=energy_shield_leech_rate_cap_pct,
                mana_leech_rate_cap_pct=mana_leech_rate_cap_pct,
            ),
        )
    except (ValueError, TypeError, boss_ward.BossWardError) as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(ok=True, data={
        "found": True, "assessable": True, "verdict": assessed.outcome,
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized_hits,
        "requested_dot_windows": normalized_dots,
        "resolved_dot_windows": dot_audit,
        "requested_recovery_events": normalized_recovery,
        "requested_mitigation_windows": normalized_mitigation,
        "requested_ward": normalized_ward,
        **assessed.as_dict(), "missing_inputs": [], "candidates": [],
        "boundary": BOUNDARY, "answer": _render(assessed, level=level, uber=uber),
    }, citations=preflight.citations, meta=meta)


def boss_ward_sequence_from_build(
    build: "PobBuild", *, events: list[dict[str, Any]],
    dot_windows: list[dict[str, Any]], recovery_events: list[dict[str, Any]],
    ward: dict[str, Any], level: int,
    mitigation_windows: list[dict[str, Any]] | None = None,
    uber: bool = False, roll_range: float | None = None,
    mana_before_life_pct: float = 0.0,
    life_recovery_per_second: float | None = None,
    energy_shield_recovery_per_second: float | None = None,
    mana_recovery_per_second: float | None = None,
    energy_shield_recharge_per_second: float | None = None,
    energy_shield_recharge_delay_seconds: float = 2.0,
    energy_shield_recharge_interrupt_mode: Literal["energy_shield_damage", "any_hit"] = "energy_shield_damage",
    dot_recharge_interrupt_mode: Literal["energy_shield_damage", "any_damage", "does_not_interrupt"] = "energy_shield_damage",
    life_leech_rate_cap_pct: float = 20.0,
    energy_shield_leech_rate_cap_pct: float = 10.0,
    mana_leech_rate_cap_pct: float = 20.0,
) -> ToolResult:
    if not events:
        return ToolResult(ok=False, error="Build Ward 时间轴至少需要一次显式命中")
    try:
        normalized_ward = _validate_ward_request(ward)
        normalized_mitigation = _validate_mitigation_requests(
            mitigation_windows or [], allow_empty=True,
        )
        normalized_dots = _validate_dot_requests(dot_windows)
        normalized_recovery = _validate_recovery_requests(recovery_events)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    layered = boss_layered_sequence_from_build(
        build, events=events, level=level, uber=uber, roll_range=roll_range,
        mana_before_life_pct=mana_before_life_pct,
        life_recovery_per_second=life_recovery_per_second,
        energy_shield_recovery_per_second=energy_shield_recovery_per_second,
        mana_recovery_per_second=mana_recovery_per_second,
        energy_shield_recharge_per_second=energy_shield_recharge_per_second,
        energy_shield_recharge_delay_seconds=energy_shield_recharge_delay_seconds,
        energy_shield_recharge_interrupt_mode=energy_shield_recharge_interrupt_mode,
    )
    if not layered.ok:
        return layered
    base = dict(layered.data or {})
    if not base.get("assessable"):
        base.update({
            "requested_dot_windows": normalized_dots,
            "requested_recovery_events": normalized_recovery,
            "requested_mitigation_windows": normalized_mitigation,
            "requested_ward": normalized_ward,
            "dot_window_count": len(normalized_dots),
            "mitigation_window_count": len(normalized_mitigation),
            "boundary": BOUNDARY, "calculation_mode": "unknown",
            "answer": f"{base.get('answer', '')}\n> Ward 边界：{BOUNDARY}。",
        })
        return ToolResult(ok=True, data=base, citations=layered.citations, meta={
            **layered.meta, "mode": "build_ward_sequence",
        })
    config = base["config"]
    stats = build.player_stats
    result = boss_ward_sequence_check(
        events=events, dot_windows=normalized_dots,
        recovery_events=normalized_recovery,
        mitigation_windows=mitigation_windows or [], ward=ward, level=level,
        life=float(config["life"]), energy_shield=float(config["energy_shield"]),
        mana=float(config["mana"]),
        mana_before_life_pct=float(config["mana_before_life_pct"]),
        uber=uber, roll_range=roll_range,
        fire_resist_pct=stats.get("FireResist"),
        cold_resist_pct=stats.get("ColdResist"),
        lightning_resist_pct=stats.get("LightningResist"),
        chaos_resist_pct=stats.get("ChaosResist"),
        armour=float(stats.get("Armour", 0)),
        life_recovery_per_second=float(config["life_recovery_per_second"]),
        energy_shield_recovery_per_second=float(config["energy_shield_recovery_per_second"]),
        mana_recovery_per_second=float(config["mana_recovery_per_second"]),
        energy_shield_recharge_per_second=float(config["energy_shield_recharge_per_second"]),
        energy_shield_recharge_delay_seconds=energy_shield_recharge_delay_seconds,
        energy_shield_recharge_interrupt_mode=energy_shield_recharge_interrupt_mode,
        dot_recharge_interrupt_mode=dot_recharge_interrupt_mode,
        life_leech_rate_cap_pct=life_leech_rate_cap_pct,
        energy_shield_leech_rate_cap_pct=energy_shield_leech_rate_cap_pct,
        mana_leech_rate_cap_pct=mana_leech_rate_cap_pct,
    )
    if not result.ok:
        return result
    data = dict(result.data or {})
    source_fields = dict(base.get("source_fields") or {})
    source_config = dict(base.get("source_config") or {})
    resist_fields = {
        "Fire": "FireResist", "Cold": "ColdResist",
        "Lightning": "LightningResist", "Chaos": "ChaosResist",
    }
    for window in normalized_dots:
        damage_type = window.get("damage_type")
        if window.get("damage_basis") != "raw" or damage_type not in resist_fields:
            continue
        field = resist_fields[str(damage_type)]
        if field in stats:
            source_fields[field] = float(stats[field])
            source_config[f"dot_{str(damage_type).lower()}_resist_pct"] = f"pob_player_stat:{field}"
    source_config.update({
        "dot_windows": "explicit_request",
        "recovery_events": "explicit_request",
        "mitigation_windows": "explicit_request",
        "ward": "explicit_request_not_pob_ward_stat",
        "ward_current_state": "not_inferred",
    })
    data.update({
        "input_source": "pob_player_stat_and_explicit_ward_state",
        "source_fields": source_fields, "source_config": source_config,
        "calculation_mode": "build_ward_sequence",
        "fallback_reason": (
            "PoB 快照不能冻结当前 Ward ready/broken、恢复计时和 Buff 启停"
        ),
    })
    data["answer"] += (
        "\n> Build 只提供基础池、抗性、回复、回充与命中护甲；Ward 状态机全部来自请求。"
    )
    return ToolResult(ok=True, data=data, citations=result.citations, meta={
        **result.meta, "mode": "build_ward_sequence",
    })


@REGISTRY.register(
    name="boss_ward_sequence_compare",
    description=(
        "比较 2–10 套显式 Ward 破裂/恢复混合时间轴，分别排序存活、生命余量、Ward 吸收、"
        "受保护命中数、耗尽时间和最低生命门槛。"
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
                            "required": [
                                "events", "dot_windows", "recovery_events", "ward",
                                "level", "life",
                            ],
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
def boss_ward_sequence_compare(scenarios: list[dict[str, Any]]) -> ToolResult:
    if not isinstance(scenarios, list) or not 2 <= len(scenarios) <= 10:
        return ToolResult(ok=False, error="scenarios 必须包含 2..10 个方案")
    names: list[str] = []
    payloads: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    seen: set[tuple[object, object]] = set()
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict) or set(scenario) != {"name", "inputs"}:
            return ToolResult(ok=False, error=f"scenarios[{index}] 必须只包含 name 和 inputs")
        name = str(scenario["name"]).strip()
        if not name or len(name) > 80 or name in names:
            return ToolResult(ok=False, error="方案名称必须为不重复的 1..80 个字符")
        inputs = scenario["inputs"]
        if not isinstance(inputs, dict):
            return ToolResult(ok=False, error=f"scenarios[{index}].inputs 必须是对象")
        checked = boss_ward_sequence_check(**inputs)
        if not checked.ok:
            return ToolResult(ok=False, error=f"方案「{name}」：{checked.error}")
        data = dict(checked.data or {})
        if not data.get("assessable"):
            return ToolResult(ok=False, error=f"方案「{name}」无法安全计算")
        names.append(name)
        payloads.append(data)
        for citation in checked.citations:
            key = (citation.get("chunk_id"), citation.get("url"))
            if key not in seen:
                seen.add(key)
                citations.append(citation)
    rank = {"fails_full_range": 0, "roll_dependent": 1, "survives_full_range": 2}
    rows = [{
        "name": name, "outcome": data["outcome"],
        "ending_life_high_roll": data["high_roll"]["timeline"]["ending_life"],
        "ward_absorbed_high_roll": data["high_roll"]["ward_absorbed"],
        "protected_hits_high_roll": data["high_roll"]["ward_state"]["protected_hit_count"],
        "depletion_time_high_roll": data["high_roll"]["timeline"]["depletion_time_seconds"],
        "minimum_life_for_full_range": data["minimum_life_for_full_range"]["value"],
        "result": data,
    } for name, data in zip(names, payloads, strict=True)]
    reachable = [row for row in rows if row["minimum_life_for_full_range"] is not None]
    payload = {
        "scenarios": rows,
        "best_survival_scenario": max(
            rows, key=lambda row: (rank[row["outcome"]], row["ending_life_high_roll"])
        )["name"],
        "best_high_roll_life_margin_scenario": max(
            rows, key=lambda row: row["ending_life_high_roll"]
        )["name"],
        "highest_ward_absorption_scenario": max(
            rows, key=lambda row: row["ward_absorbed_high_roll"]
        )["name"],
        "highest_ward_uptime_scenario": max(
            rows, key=lambda row: row["result"]["high_roll"]["ward_state"]["ready_uptime_pct"]
        )["name"],
        "most_protected_hits_scenario": max(
            rows, key=lambda row: row["protected_hits_high_roll"]
        )["name"],
        "longest_time_to_depletion_scenario": max(
            rows, key=lambda row: (
                row["depletion_time_high_roll"] is None,
                row["depletion_time_high_roll"] or 0,
            ),
        )["name"],
        "lowest_required_life_scenario": (
            min(reachable, key=lambda row: row["minimum_life_for_full_range"])["name"]
            if reachable else None
        ),
        "boundary": BOUNDARY,
    }
    payload["answer"] = (
        "### Boss Ward 状态机方案比较\n\n"
        f"- 存活结论最佳：**{payload['best_survival_scenario']}**\n"
        f"- 高伤轨生命余量最佳：**{payload['best_high_roll_life_margin_scenario']}**\n"
        f"- Ward 吸收最高：**{payload['highest_ward_absorption_scenario']}**\n"
        f"- Ward 就绪率最高：**{payload['highest_ward_uptime_scenario']}**\n"
        f"- 受保护命中最多：**{payload['most_protected_hits_scenario']}**\n\n"
        f"> 边界：{BOUNDARY}。"
    )
    return ToolResult(ok=True, data=payload, citations=citations, meta={
        "channel": "derived", "mode": "explicit_ward_comparison",
    })
