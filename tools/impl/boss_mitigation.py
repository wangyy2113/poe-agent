"""Boss 临时减伤窗口、Guard 吸收池与混合时间轴工具。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

from calc import boss_mitigation
from calc.boss_mitigation import MitigationTimelineResult, MitigationWindow
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
from tools.registry import REGISTRY, ToolResult

if TYPE_CHECKING:
    from parsers.pob_build import PobBuild


BOUNDARY = (
    "只结算用户显式声明的临时减伤窗口。taken_multiplier 是已经冻结口径的承伤乘数；Guard "
    "是在护盾/魔力/生命之前的一次性窗口容量，窗口结束即丢弃，不自动恢复、刷新或复用。"
    "不会从守护技能、Ward、装备、职业、阶段或技能名推断窗口，也不模拟真实技能冷却、"
    "施法动作、Buff 移除、Ward 破裂/复原、格挡、闪避或法术压制"
)


_MITIGATION_WINDOWS_SCHEMA = {
    "type": "array", "minItems": 1, "maxItems": 100,
    "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "start_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
            "duration_seconds": {
                "type": "number", "exclusiveMinimum": 0, "maximum": 3600,
            },
            "scope": {"type": "string", "enum": ["hit", "dot", "both"], "default": "both"},
            "damage_types": {
                "type": "array", "maxItems": 5, "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": ["Physical", "Fire", "Cold", "Lightning", "Chaos"],
                },
                "default": [],
            },
            "taken_multiplier": {
                "type": "number", "minimum": 0, "maximum": 1, "default": 1,
            },
            "guard_capacity": {"type": "number", "minimum": 0, "default": 0},
            "priority": {"type": "integer", "minimum": 0, "maximum": 1000, "default": 100},
        },
        "required": ["name", "start_seconds", "duration_seconds"],
    },
}


_INPUT_PROPERTIES = {
    **_DOT_INPUT_PROPERTIES,
    "mitigation_windows": _MITIGATION_WINDOWS_SCHEMA,
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


def _validate_mitigation_requests(windows: object) -> list[dict[str, Any]]:
    if not isinstance(windows, list) or not 1 <= len(windows) <= 100:
        raise ValueError("mitigation_windows 必须包含 1..100 个窗口")
    allowed = {
        "name", "start_seconds", "duration_seconds", "scope", "damage_types",
        "taken_multiplier", "guard_capacity", "priority",
    }
    damage_type_values = {"Physical", "Fire", "Cold", "Lightning", "Chaos"}
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(windows):
        if not isinstance(item, dict):
            raise ValueError(f"mitigation_windows[{index}] 必须是对象")
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(
                f"mitigation_windows[{index}] 包含未知字段：{', '.join(sorted(unknown))}"
            )
        name = str(item.get("name") or "").strip()
        if not 1 <= len(name) <= 80 or name in names:
            raise ValueError("临时减伤窗口名称必须为不重复的 1..80 个字符")
        names.add(name)
        start = _finite(
            item.get("start_seconds"), f"mitigation_windows[{index}].start_seconds",
            maximum=3600,
        )
        duration = _finite(
            item.get("duration_seconds"),
            f"mitigation_windows[{index}].duration_seconds",
            minimum=1e-9, maximum=3600,
        )
        scope = item.get("scope", "both")
        if scope not in {"hit", "dot", "both"}:
            raise ValueError(f"mitigation_windows[{index}].scope 无效")
        types = item.get("damage_types", [])
        if (
            not isinstance(types, list) or len(types) > 5
            or any(value not in damage_type_values for value in types)
            or len(set(types)) != len(types)
        ):
            raise ValueError(f"mitigation_windows[{index}].damage_types 无效或重复")
        multiplier = _finite(
            item.get("taken_multiplier", 1),
            f"mitigation_windows[{index}].taken_multiplier", maximum=1,
        )
        capacity = _finite(
            item.get("guard_capacity", 0),
            f"mitigation_windows[{index}].guard_capacity",
        )
        priority = item.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1000:
            raise ValueError(f"mitigation_windows[{index}].priority 必须是 0..1000 整数")
        if multiplier == 1 and capacity == 0:
            raise ValueError(f"mitigation_windows[{index}] 必须降低承伤或提供正 Guard 容量")
        normalized.append({
            "name": name, "start_seconds": start, "duration_seconds": duration,
            "scope": scope, "damage_types": types,
            "taken_multiplier": multiplier, "guard_capacity": capacity,
            "priority": priority,
        })
    return sorted(normalized, key=lambda row: (
        row["priority"], row["start_seconds"], row["name"],
    ))


def _mitigation_windows(
    windows: list[dict[str, Any]],
) -> tuple[MitigationWindow, ...]:
    return tuple(MitigationWindow(
        name=item["name"], start_seconds=item["start_seconds"],
        duration_seconds=item["duration_seconds"], scope=item["scope"],
        damage_types=tuple(item["damage_types"]),
        taken_multiplier=item["taken_multiplier"],
        guard_capacity=item["guard_capacity"], priority=item["priority"],
    ) for item in windows)  # type: ignore[arg-type]


def _threshold_text(value: object) -> str:
    status = getattr(value, "status")
    if status == "not_applicable":
        return "不适用"
    if status == "unreachable":
        return "单独提高该池不可达"
    return f"{getattr(value, 'value')}（缺口 {getattr(value, 'shortfall'):g}）"


def _render(result: MitigationTimelineResult, *, level: int, uber: bool) -> str:
    labels = {
        "survives_full_range": "完整低/高伤区间都能存活",
        "roll_dependent": "取决于命中或 DoT 的伤害区间",
        "fails_full_range": "即使低伤轨也会耗尽生命",
    }
    lines = [
        "### Boss 临时减伤与 Guard 时间轴" + ("（Uber 命中档）" if uber else ""), "",
        f"**结论：{labels[result.outcome]}**",
        f"- 敌人等级 {level}；命中 {result.hit_count} 次；DoT {result.dot_window_count} 个；"
        f"临时防御窗口 {result.mitigation_window_count} 个；时长 {result.duration_seconds:g}s",
        "", "| 轨迹 | 存活 | 伤害前→后 | 乘数减免 | Guard 吸收 | 总阻止 | 末尾生命/护盾/魔力 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, roll in (("低伤", result.low_roll), ("高伤", result.high_roll)):
        timeline = roll.timeline
        lines.append(
            f"| {label} | {'是' if timeline.survived else '否'} | "
            f"{roll.damage_before_mitigation:g}→{roll.damage_after_mitigation:g} | "
            f"{roll.multiplier_prevented:g} | {roll.guard_absorbed:g} | "
            f"{roll.total_prevented:g}（{roll.prevented_pct:g}%） | "
            f"{timeline.ending_life:g}/{timeline.ending_energy_shield:g}/{timeline.ending_mana:g} |"
        )
    lines.extend(["", "#### 高伤轨窗口审计"])
    for window in result.high_roll.windows:
        depletion = (
            f"{window.guard_depletion_time_seconds:g}s"
            if window.guard_depletion_time_seconds is not None else "未耗尽"
        )
        lines.append(
            f"- {window.name}（优先级 {window.priority}）：乘数减免 "
            f"{window.multiplier_prevented:g}；Guard 吸收 {window.guard_absorbed:g}；"
            f"未使用 {window.unused_guard_capacity:g}；耗尽 {depletion}"
        )
    impact = result.mitigation_impact
    lines.extend([
        "", f"- 移除临时防御后高伤轨末尾生命："
        f"{impact.high_roll_ending_life_without_mitigation:g}；当前窗口带来的末尾生命差："
        f"{impact.high_roll_ending_life_gain:g}",
        f"- 临时防御是否改变高伤轨存活结论："
        f"{'是' if impact.changed_high_roll_survival else '否'}",
        f"- 其余层固定时门槛：生命 {_threshold_text(result.minimum_life_for_full_range)}；"
        f"护盾 {_threshold_text(result.minimum_energy_shield_for_full_range)}；"
        f"魔力 {_threshold_text(result.minimum_mana_for_full_range)}",
        "", f"> 顺序：{result.ordering_model}。",
        f"> 窗口边界：{result.window_boundary_model}。",
        f"> 归属：{result.attribution_model}。", f"> 边界：{BOUNDARY}。",
    ])
    return "\n".join(lines)


@REGISTRY.register(
    name="boss_mitigation_sequence_check",
    description=(
        "在显式 Boss 命中/DoT/恢复时间轴前应用临时承伤乘数与 Guard 容量；精确追踪重叠"
        "优先级、Guard 耗尽时点、逐击/逐段阻止量、无临时防御基线与分层池门槛。"
    ),
    parameters={
        "type": "object", "properties": _INPUT_PROPERTIES,
        "required": [
            "events", "dot_windows", "recovery_events", "mitigation_windows",
            "level", "life",
        ],
        "additionalProperties": False,
    },
)
def boss_mitigation_sequence_check(
    events: list[dict[str, Any]], dot_windows: list[dict[str, Any]],
    recovery_events: list[dict[str, Any]], mitigation_windows: list[dict[str, Any]],
    level: int, life: float, energy_shield: float = 0.0, mana: float = 0.0,
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
        normalized_dots = _validate_dot_requests(dot_windows)
        normalized_recovery = _validate_recovery_requests(recovery_events)
        normalized_mitigation = _validate_mitigation_requests(mitigation_windows)
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
    citations: list[dict[str, Any]] = []
    meta: dict[str, Any] = {
        "channel": "derived", "mode": "explicit_temporary_mitigation_sequence",
    }
    if normalized_hits:
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
        citations = preflight.citations
        meta = {**preflight.meta, "mode": "explicit_temporary_mitigation_sequence"}
    else:
        base = {
            "found": True, "assessable": True, "steps": [],
            "missing_inputs": [], "candidates": [],
        }
    missing = list(base.get("missing_inputs") or []) + dot_missing
    if not base.get("assessable") or missing:
        reason = "命中或 raw DoT 缺少完成减伤所需的显式输入"
        return ToolResult(ok=True, data={
            "found": True, "assessable": False, "verdict": "unknown",
            "level": level, "uber": uber, "roll_range": roll_range,
            "requested_events": normalized_hits, "requested_dot_windows": normalized_dots,
            "resolved_dot_windows": dot_audit,
            "requested_recovery_events": normalized_recovery,
            "requested_mitigation_windows": normalized_mitigation,
            "hit_count": len(normalized_hits), "dot_window_count": len(normalized_dots),
            "mitigation_window_count": len(normalized_mitigation),
            "reason": reason, "missing_inputs": missing,
            "candidates": base.get("candidates", []), "boundary": BOUNDARY,
            "answer": (
                "### Boss 临时减伤与 Guard 时间轴\n\n**结论：无法安全计算**\n"
                f"- 原因：{reason}\n- 缺少：{'、'.join(missing)}\n\n> 边界：{BOUNDARY}。"
            ),
        }, citations=citations, meta={**meta, "verdict": "unknown"})
    try:
        hits = _layered_events(normalized_hits, base["steps"]) if normalized_hits else tuple()
        assessed = boss_mitigation.assess_mitigation_timeline(
            hit_events=hits, dot_windows=resolved_dots,
            recovery_events=_recovery_events(normalized_recovery),
            mitigation_windows=_mitigation_windows(normalized_mitigation),
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
    except (ValueError, TypeError, boss_mitigation.BossMitigationError) as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(ok=True, data={
        "found": True, "assessable": True, "verdict": assessed.outcome,
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized_hits, "requested_dot_windows": normalized_dots,
        "resolved_dot_windows": dot_audit,
        "requested_recovery_events": normalized_recovery,
        "requested_mitigation_windows": normalized_mitigation,
        **assessed.as_dict(), "missing_inputs": [], "candidates": [],
        "boundary": BOUNDARY, "answer": _render(assessed, level=level, uber=uber),
    }, citations=citations, meta=meta)


def boss_mitigation_sequence_from_build(
    build: "PobBuild", *, events: list[dict[str, Any]],
    dot_windows: list[dict[str, Any]], recovery_events: list[dict[str, Any]],
    mitigation_windows: list[dict[str, Any]], level: int,
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
        return ToolResult(ok=False, error="Build 临时减伤时间轴至少需要一次显式命中以绑定 Boss 与基础防御链")
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
        try:
            normalized_mitigation = _validate_mitigation_requests(mitigation_windows)
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))
        base.update({
            "requested_mitigation_windows": normalized_mitigation,
            "mitigation_window_count": len(normalized_mitigation),
            "boundary": BOUNDARY, "calculation_mode": "unknown",
            "answer": f"{base.get('answer', '')}\n> 临时减伤边界：{BOUNDARY}。",
        })
        return ToolResult(ok=True, data=base, citations=layered.citations, meta={
            **layered.meta, "mode": "build_temporary_mitigation_sequence",
        })
    config = base["config"]
    stats = build.player_stats
    result = boss_mitigation_sequence_check(
        events=events, dot_windows=dot_windows, recovery_events=recovery_events,
        mitigation_windows=mitigation_windows, level=level,
        life=float(config["life"]), energy_shield=float(config["energy_shield"]),
        mana=float(config["mana"]),
        mana_before_life_pct=float(config["mana_before_life_pct"]), uber=uber,
        roll_range=roll_range, fire_resist_pct=stats.get("FireResist"),
        cold_resist_pct=stats.get("ColdResist"),
        lightning_resist_pct=stats.get("LightningResist"),
        chaos_resist_pct=stats.get("ChaosResist"), armour=float(stats.get("Armour", 0)),
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
    for window in dot_windows:
        damage_type = window.get("damage_type")
        if window.get("damage_basis") != "raw" or damage_type not in resist_fields:
            continue
        field = resist_fields[str(damage_type)]
        if field in stats:
            source_fields[field] = float(stats[field])
            source_config[f"dot_{str(damage_type).lower()}_resist_pct"] = f"pob_player_stat:{field}"
    source_config.update({
        "dot_windows": "explicit_request", "recovery_events": "explicit_request",
        "mitigation_windows": "explicit_request",
        "temporary_defence_cooldowns": "not_inferred",
    })
    data.update({
        "input_source": "pob_player_stat_and_explicit_mitigation_windows",
        "source_fields": source_fields, "source_config": source_config,
        "calculation_mode": "build_temporary_mitigation_sequence",
        "fallback_reason": (
            "PoB 快照不含临时防御的精确启停、适用范围、Guard 当前容量或事件优先级"
        ),
    })
    data["answer"] += (
        "\n> Build 只提供基础池、抗性、回复、回充与命中护甲；临时防御窗口仍全部来自请求。"
    )
    return ToolResult(ok=True, data=data, citations=result.citations, meta={
        **result.meta, "mode": "build_temporary_mitigation_sequence",
    })


@REGISTRY.register(
    name="boss_mitigation_sequence_compare",
    description=(
        "比较 2–10 套 Boss 临时减伤/Guard 混合时间轴，分别排序存活、高伤生命余量、"
        "总阻止伤害、耗尽时间与最低生命门槛。"
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
                                "events", "dot_windows", "recovery_events",
                                "mitigation_windows", "level", "life",
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
def boss_mitigation_sequence_compare(scenarios: list[dict[str, Any]]) -> ToolResult:
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
        checked = boss_mitigation_sequence_check(**inputs)
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
        "total_prevented_high_roll": data["high_roll"]["total_prevented"],
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
        "highest_prevented_damage_scenario": max(
            rows, key=lambda row: row["total_prevented_high_roll"]
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
        "### Boss 临时减伤与 Guard 方案比较\n\n"
        f"- 存活结论最佳：**{payload['best_survival_scenario']}**\n"
        f"- 高伤轨生命余量最佳：**{payload['best_high_roll_life_margin_scenario']}**\n"
        f"- 阻止伤害最高：**{payload['highest_prevented_damage_scenario']}**\n"
        f"- 耗尽时间最长：**{payload['longest_time_to_depletion_scenario']}**\n\n"
        f"> 边界：{BOUNDARY}。"
    )
    return ToolResult(ok=True, data=payload, citations=citations, meta={
        "channel": "derived", "mode": "explicit_temporary_mitigation_comparison",
    })
