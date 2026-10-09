"""Boss 显式 DoT 暴露窗口、命中与主动恢复的统一工具表面。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

from calc import boss_dot
from calc.boss_dot import DotTimelineConfig, DotTimelineResult, DotWindow
from calc.boss_recovery import ActiveRecoveryEvent, RecoveryPoolConfig
from tools.impl.boss_layered import (
    _layered_events,
    _validate_events,
    boss_layered_sequence_from_build,
)
from tools.impl.boss_recovery import _validate_recovery_requests
from tools.impl.boss_timeline import boss_timed_sequence_check
from tools.registry import REGISTRY, ToolResult

if TYPE_CHECKING:
    from parsers.pob_build import PobBuild


BOUNDARY = (
    "只结算用户显式提供的命中、DoT 暴露窗口和恢复事件。不会从技能名、异常、装备、"
    "阶段或地面效果推断 DoT，不模拟异常施加/刷新/叠层。raw_dps 只应用显式抗性、固定物理"
    "减伤和 taken_mult；物理 DoT 不使用护甲，DoT 不使用穿透/压倒，Uber DoT 倍率也不"
    "自动猜测。窗口不是官方技能机制或实战保证"
)


_HIT_EVENTS_SCHEMA = {
    "type": "array", "minItems": 0, "maxItems": 100,
    "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "skill": {"type": "string", "minLength": 2, "maxLength": 120},
            "at_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
            "energy_shield_bypass_pct": {
                "type": "number", "minimum": 0, "maximum": 100, "default": 0,
            },
        },
        "required": ["skill", "at_seconds"],
    },
}


_DOT_WINDOWS_SCHEMA = {
    "type": "array", "minItems": 1, "maxItems": 100,
    "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "start_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
            "duration_seconds": {
                "type": "number", "exclusiveMinimum": 0, "maximum": 3600,
            },
            "damage_type": {
                "type": "string",
                "enum": ["Physical", "Fire", "Cold", "Lightning", "Chaos"],
            },
            "damage_basis": {"type": "string", "enum": ["raw", "taken"]},
            "dps_low": {"type": "number", "minimum": 0},
            "dps_high": {"type": "number", "minimum": 0},
            "energy_shield_bypass_pct": {
                "type": "number", "minimum": 0, "maximum": 100, "default": 0,
            },
        },
        "required": [
            "name", "start_seconds", "duration_seconds", "damage_type",
            "damage_basis", "dps_low", "dps_high",
        ],
    },
}


_RECOVERY_EVENTS_SCHEMA = {
    "type": "array", "minItems": 0, "maxItems": 100,
    "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 80},
            "at_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
            "pool": {"type": "string", "enum": ["life", "energy_shield", "mana"]},
            "kind": {"type": "string", "enum": ["instant", "timed_rate", "leech"]},
            "amount": {"type": "number", "minimum": 0, "default": 0},
            "rate_per_second": {"type": "number", "minimum": 0, "default": 0},
            "duration_seconds": {"type": "number", "minimum": 0, "maximum": 3600, "default": 0},
            "phase": {
                "type": "string", "enum": ["before_hit", "after_hit"],
                "default": "before_hit",
            },
        },
        "required": ["name", "at_seconds", "pool", "kind"],
    },
}


_INPUT_PROPERTIES = {
    "events": _HIT_EVENTS_SCHEMA,
    "dot_windows": _DOT_WINDOWS_SCHEMA,
    "recovery_events": _RECOVERY_EVENTS_SCHEMA,
    "level": {"type": "integer", "minimum": 1, "maximum": 100},
    "life": {"type": "number", "exclusiveMinimum": 0},
    "energy_shield": {"type": "number", "minimum": 0, "default": 0},
    "mana": {"type": "number", "minimum": 0, "default": 0},
    "mana_before_life_pct": {"type": "number", "minimum": 0, "maximum": 100, "default": 0},
    "uber": {"type": "boolean", "default": False},
    "roll_range": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
    "fire_resist_pct": {"type": ["number", "null"], "minimum": -200, "maximum": 90},
    "cold_resist_pct": {"type": ["number", "null"], "minimum": -200, "maximum": 90},
    "lightning_resist_pct": {"type": ["number", "null"], "minimum": -200, "maximum": 90},
    "chaos_resist_pct": {"type": ["number", "null"], "minimum": -200, "maximum": 90},
    "armour": {"type": "number", "minimum": 0, "default": 0},
    "flat_physical_reduction_pct": {"type": "number", "minimum": 0, "maximum": 90, "default": 0},
    "taken_mult": {"type": "number", "exclusiveMinimum": 0, "default": 1},
    "overwhelm_pct": {"type": "number", "minimum": 0, "maximum": 100, "default": 0},
    "life_recovery_per_second": {"type": "number", "minimum": 0, "default": 0},
    "energy_shield_recovery_per_second": {"type": "number", "minimum": 0, "default": 0},
    "mana_recovery_per_second": {"type": "number", "minimum": 0, "default": 0},
    "energy_shield_recharge_per_second": {"type": "number", "minimum": 0, "default": 0},
    "energy_shield_recharge_delay_seconds": {"type": "number", "minimum": 0, "maximum": 3600, "default": 2},
    "energy_shield_recharge_interrupt_mode": {
        "type": "string", "enum": ["energy_shield_damage", "any_hit"],
        "default": "energy_shield_damage",
    },
    "dot_recharge_interrupt_mode": {
        "type": "string",
        "enum": ["energy_shield_damage", "any_damage", "does_not_interrupt"],
        "default": "energy_shield_damage",
    },
    "life_leech_rate_cap_pct": {"type": "number", "minimum": 0, "default": 20},
    "energy_shield_leech_rate_cap_pct": {"type": "number", "minimum": 0, "default": 10},
    "mana_leech_rate_cap_pct": {"type": "number", "minimum": 0, "default": 20},
}


def _finite(value: object, label: str, *, maximum: float = 1_000_000_000) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= maximum:
        raise ValueError(f"{label} 超出安全范围")
    return result


def _validate_hits(events: object) -> list[dict[str, Any]]:
    if not isinstance(events, list) or len(events) > 100:
        raise ValueError("events 必须包含 0..100 次命中")
    if not events:
        return []
    return _validate_events(events)


def _validate_dot_requests(windows: object) -> list[dict[str, Any]]:
    if not isinstance(windows, list) or not 1 <= len(windows) <= 100:
        raise ValueError("dot_windows 必须包含 1..100 个窗口")
    allowed = {
        "name", "start_seconds", "duration_seconds", "damage_type",
        "damage_basis", "dps_low", "dps_high", "energy_shield_bypass_pct",
    }
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(windows):
        if not isinstance(item, dict):
            raise ValueError(f"dot_windows[{index}] 必须是对象")
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(f"dot_windows[{index}] 包含未知字段：{', '.join(sorted(unknown))}")
        name = str(item.get("name") or "").strip()
        if not 1 <= len(name) <= 80 or name in names:
            raise ValueError("DoT 窗口名称必须为不重复的 1..80 个字符")
        names.add(name)
        damage_type = item.get("damage_type")
        basis = item.get("damage_basis")
        if damage_type not in {"Physical", "Fire", "Cold", "Lightning", "Chaos"}:
            raise ValueError(f"dot_windows[{index}].damage_type 无效")
        if basis not in {"raw", "taken"}:
            raise ValueError(f"dot_windows[{index}].damage_basis 无效")
        start = _finite(item.get("start_seconds"), f"dot_windows[{index}].start_seconds", maximum=3600)
        duration = _finite(item.get("duration_seconds"), f"dot_windows[{index}].duration_seconds", maximum=3600)
        low = _finite(item.get("dps_low"), f"dot_windows[{index}].dps_low")
        high = _finite(item.get("dps_high"), f"dot_windows[{index}].dps_high")
        bypass = _finite(
            item.get("energy_shield_bypass_pct", 0),
            f"dot_windows[{index}].energy_shield_bypass_pct", maximum=100,
        )
        if duration <= 0 or start + duration > 3600:
            raise ValueError(f"dot_windows[{index}] 持续时间或结束时间无效")
        if low > high:
            raise ValueError(f"dot_windows[{index}] 的低 DPS 不能大于高 DPS")
        normalized.append({
            "name": name, "start_seconds": start, "duration_seconds": duration,
            "damage_type": damage_type, "damage_basis": basis,
            "dps_low": low, "dps_high": high,
            "energy_shield_bypass_pct": bypass,
        })
    return sorted(normalized, key=lambda row: (row["start_seconds"], row["name"]))


def _resolve_dots(
    requests: list[dict[str, Any]], *, fire_resist_pct: float | None,
    cold_resist_pct: float | None, lightning_resist_pct: float | None,
    chaos_resist_pct: float | None, flat_physical_reduction_pct: float,
    taken_mult: float,
) -> tuple[tuple[DotWindow, ...], list[str], list[dict[str, Any]]]:
    resistances = {
        "Fire": fire_resist_pct, "Cold": cold_resist_pct,
        "Lightning": lightning_resist_pct, "Chaos": chaos_resist_pct,
    }
    missing: list[str] = []
    windows: list[DotWindow] = []
    audit: list[dict[str, Any]] = []
    for item in requests:
        factor = 1.0
        if item["damage_basis"] == "raw":
            if item["damage_type"] == "Physical":
                factor = (1 - flat_physical_reduction_pct / 100) * taken_mult
            else:
                resist = resistances[item["damage_type"]]
                if resist is None:
                    label = {
                        "Fire": "火焰抗性", "Cold": "冰霜抗性",
                        "Lightning": "闪电抗性", "Chaos": "混沌抗性",
                    }[item["damage_type"]]
                    if label not in missing:
                        missing.append(label)
                    continue
                factor = (1 - float(resist) / 100) * taken_mult
        low = item["dps_low"] * factor
        high = item["dps_high"] * factor
        windows.append(DotWindow(
            name=item["name"], start_seconds=item["start_seconds"],
            duration_seconds=item["duration_seconds"],
            damage_type=item["damage_type"], taken_dps_low=low,
            taken_dps_high=high,
            energy_shield_bypass_pct=item["energy_shield_bypass_pct"],
        ))
        audit.append({
            **item, "taken_dps_low": round(low, 6),
            "taken_dps_high": round(high, 6),
            "mitigation_factor": round(factor, 8),
            "armour_applied": False, "penetration_applied": False,
        })
    return tuple(windows), missing, audit


def _recovery_events(events: list[dict[str, Any]]) -> tuple[ActiveRecoveryEvent, ...]:
    return tuple(ActiveRecoveryEvent(**event) for event in events)  # type: ignore[arg-type]


def _config(
    *, life: float, energy_shield: float, mana: float,
    mana_before_life_pct: float, life_recovery_per_second: float,
    energy_shield_recovery_per_second: float, mana_recovery_per_second: float,
    energy_shield_recharge_per_second: float,
    energy_shield_recharge_delay_seconds: float,
    energy_shield_recharge_interrupt_mode: str,
    dot_recharge_interrupt_mode: str, life_leech_rate_cap_pct: float,
    energy_shield_leech_rate_cap_pct: float, mana_leech_rate_cap_pct: float,
) -> DotTimelineConfig:
    return DotTimelineConfig(
        pools=RecoveryPoolConfig(
            life=life, energy_shield=energy_shield, mana=mana,
            mana_before_life_pct=mana_before_life_pct,
            life_recovery_per_second=life_recovery_per_second,
            energy_shield_recovery_per_second=energy_shield_recovery_per_second,
            mana_recovery_per_second=mana_recovery_per_second,
            energy_shield_recharge_per_second=energy_shield_recharge_per_second,
            energy_shield_recharge_delay_seconds=energy_shield_recharge_delay_seconds,
            energy_shield_recharge_interrupt_mode=energy_shield_recharge_interrupt_mode,  # type: ignore[arg-type]
            life_leech_rate_cap_pct=life_leech_rate_cap_pct,
            energy_shield_leech_rate_cap_pct=energy_shield_leech_rate_cap_pct,
            mana_leech_rate_cap_pct=mana_leech_rate_cap_pct,
        ),
        dot_recharge_interrupt_mode=dot_recharge_interrupt_mode,  # type: ignore[arg-type]
    )


def _threshold_text(value: object) -> str:
    status = getattr(value, "status")
    if status == "not_applicable":
        return "不适用"
    if status == "unreachable":
        return "单独提高该池不可达"
    return f"{getattr(value, 'value')}（缺口 {getattr(value, 'shortfall'):g}）"


def _render(result: DotTimelineResult, *, level: int, uber: bool) -> str:
    labels = {
        "survives_full_range": "完整低/高伤区间都能存活",
        "roll_dependent": "取决于命中或 DoT 的伤害区间",
        "fails_full_range": "即使低伤轨也会耗尽生命",
    }
    lines = [
        "### Boss DoT 暴露混合时间轴" + ("（Uber 命中档）" if uber else ""), "",
        f"**结论：{labels[result.outcome]}**",
        f"- 敌人等级 {level}；命中 {result.hit_count} 次；DoT 窗口 {result.dot_window_count} 个；"
        f"总时长 {result.duration_seconds:g} 秒",
        "", "| 轨迹 | 存活 | 首次耗尽 | 末尾生命 / 护盾 / 魔力 | 命中承伤 | DoT 承伤 | 恢复抵消 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, roll in (("低伤", result.low_roll), ("高伤", result.high_roll)):
        depletion = (
            f"{roll.depletion_time_seconds:g}s（{roll.depletion_cause}）"
            if roll.depletion_time_seconds is not None else "—"
        )
        lines.append(
            f"| {label} | {'是' if roll.survived else '否'} | {depletion} | "
            f"{roll.ending_life:g} / {roll.ending_energy_shield:g} / {roll.ending_mana:g} | "
            f"{roll.total_hit_damage:g} | {roll.total_dot_damage:g} | "
            f"{roll.total_recovery_applied:g} |"
        )
    lines.extend(["", "#### 高伤轨 DoT 来源"])
    for source in result.high_roll.dot_sources:
        lines.append(
            f"- {source.name}（{source.damage_type}）：{source.taken_dps:g}/s × "
            f"{source.end_seconds - source.start_seconds:g}s，计划 {source.scheduled_damage:g}，"
            f"耗尽前实际推进 {source.processed_damage:g}"
        )
    impact = result.dot_impact
    lines.extend([
        "", f"- 去掉 DoT 后高伤轨末尾生命 {impact.high_roll_ending_life_without_dot:g}；"
        f"DoT 带来的末尾生命差 {impact.high_roll_life_loss_from_dot:g}",
        f"- DoT 是否改变高伤轨存活结论：{'是' if impact.changed_high_roll_survival else '否'}",
        f"- 其余层固定时的完整区间门槛：生命 {_threshold_text(result.minimum_life_for_full_range)}；"
        f"护盾 {_threshold_text(result.minimum_energy_shield_for_full_range)}；"
        f"魔力 {_threshold_text(result.minimum_mana_for_full_range)}",
        "", f"> 连续流：{result.continuous_flow_model}。",
        f"> 同刻顺序：{result.event_ordering}。", f"> 路由：{result.routing_model}。",
        f"> 边界：{BOUNDARY}。",
    ])
    return "\n".join(lines)


def _empty_hit_base(*, level: int, uber: bool, roll_range: float | None) -> dict[str, Any]:
    return {
        "found": True, "assessable": True, "level": level, "uber": uber,
        "roll_range": roll_range, "steps": [], "missing_inputs": [], "candidates": [],
    }


@REGISTRY.register(
    name="boss_dot_sequence_check",
    description=(
        "按显式 Boss 命中、DoT 暴露窗口和恢复事件推进生命/护盾/魔力，给出连续区间、"
        "精确首次耗尽时间、来源归属、恢复抵消、无 DoT 基线差与单轴最低池。"
    ),
    parameters={
        "type": "object", "properties": _INPUT_PROPERTIES,
        "required": ["events", "dot_windows", "recovery_events", "level", "life"],
        "additionalProperties": False,
    },
)
def boss_dot_sequence_check(
    events: list[dict[str, Any]], dot_windows: list[dict[str, Any]],
    recovery_events: list[dict[str, Any]], level: int, life: float,
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
        normalized_dots = _validate_dot_requests(dot_windows)
        normalized_recovery = _validate_recovery_requests(recovery_events)
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
    meta: dict[str, Any] = {"channel": "derived", "mode": "explicit_dot_exposure_sequence"}
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
        meta = {**preflight.meta, "mode": "explicit_dot_exposure_sequence"}
    else:
        base = _empty_hit_base(level=level, uber=uber, roll_range=roll_range)
    missing = list(base.get("missing_inputs") or []) + dot_missing
    if not base.get("assessable") or missing:
        reason = "命中或 raw DoT 缺少完成减伤所需的显式输入"
        return ToolResult(ok=True, data={
            "found": True, "assessable": False, "verdict": "unknown",
            "level": level, "uber": uber, "roll_range": roll_range,
            "requested_events": normalized_hits, "requested_dot_windows": normalized_dots,
            "resolved_dot_windows": dot_audit, "requested_recovery_events": normalized_recovery,
            "hit_count": len(normalized_hits), "dot_window_count": len(normalized_dots),
            "reason": reason, "missing_inputs": missing,
            "candidates": base.get("candidates", []), "boundary": BOUNDARY,
            "answer": (
                "### Boss DoT 暴露混合时间轴\n\n**结论：无法安全计算**\n"
                f"- 原因：{reason}\n- 缺少：{'、'.join(missing)}\n\n> 边界：{BOUNDARY}。"
            ),
        }, citations=citations, meta={**meta, "verdict": "unknown"})
    try:
        layered_hits = (
            _layered_events(normalized_hits, base["steps"]) if normalized_hits else tuple()
        )
        assessed = boss_dot.assess_dot_timeline(
            hit_events=layered_hits, dot_windows=resolved_dots,
            recovery_events=_recovery_events(normalized_recovery),
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
    except (ValueError, TypeError, boss_dot.BossDotError) as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(ok=True, data={
        "found": True, "assessable": True, "verdict": assessed.outcome,
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized_hits, "requested_dot_windows": normalized_dots,
        "resolved_dot_windows": dot_audit,
        "requested_recovery_events": normalized_recovery, **assessed.as_dict(),
        "missing_inputs": [], "candidates": [], "boundary": BOUNDARY,
        "answer": _render(assessed, level=level, uber=uber),
    }, citations=citations, meta=meta)


def boss_dot_sequence_from_build(
    build: "PobBuild", *, events: list[dict[str, Any]],
    dot_windows: list[dict[str, Any]], recovery_events: list[dict[str, Any]],
    level: int, uber: bool = False, roll_range: float | None = None,
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
        return ToolResult(ok=False, error="Build DoT 时间轴至少需要一次显式命中以绑定 Boss 与基础防御链")
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
            normalized_dots = _validate_dot_requests(dot_windows)
            normalized_recovery = _validate_recovery_requests(recovery_events)
        except ValueError as exc:
            return ToolResult(ok=False, error=str(exc))
        base.update({
            "requested_dot_windows": normalized_dots,
            "resolved_dot_windows": [],
            "requested_recovery_events": normalized_recovery,
            "dot_window_count": len(normalized_dots),
            "boundary": BOUNDARY,
            "input_source": "pob_player_stat_and_explicit_dot_windows",
            "source_config": {
                **dict(base.get("source_config") or {}),
                "dot_windows": "explicit_request",
                "recovery_events": "explicit_request",
            },
            "calculation_mode": "unknown",
            "answer": f"{base.get('answer', '')}\n> DoT 暴露边界：{BOUNDARY}。",
        })
        return ToolResult(ok=True, data=base, citations=layered.citations, meta={
            **layered.meta, "mode": "build_dot_exposure_sequence",
        })
    stats = build.player_stats
    config = base["config"]
    result = boss_dot_sequence_check(
        events=events, dot_windows=dot_windows, recovery_events=recovery_events,
        level=level, life=float(config["life"]),
        energy_shield=float(config["energy_shield"]), mana=float(config["mana"]),
        mana_before_life_pct=float(config["mana_before_life_pct"]), uber=uber,
        roll_range=roll_range, fire_resist_pct=stats.get("FireResist"),
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
    for window in dot_windows:
        damage_type = window.get("damage_type")
        if window.get("damage_basis") != "raw" or damage_type not in resist_fields:
            continue
        field = resist_fields[str(damage_type)]
        if field in stats:
            source_fields[field] = float(stats[field])
            source_config[f"dot_{str(damage_type).lower()}_resist_pct"] = (
                f"pob_player_stat:{field}"
            )
    source_config.update({
        "dot_windows": "explicit_request", "recovery_events": "explicit_request",
        "dot_recharge_interrupt_mode": "explicit_request_or_default",
        "raw_dot_flat_physical_reduction_pct": "explicit_zero_default",
        "raw_dot_taken_mult": "explicit_one_default",
    })
    data.update({
        "input_source": "pob_player_stat_and_explicit_dot_windows",
        "source_fields": source_fields,
        "source_config": source_config,
        "calculation_mode": "build_dot_exposure_sequence",
        "fallback_reason": "PoB 快照不含 DoT 暴露窗口、持续时间或同刻事件顺序",
    })
    data["answer"] += "\n> Build 只提供已标注的基础池、抗性、净回复与回充；DoT 窗口仍全部来自请求。"
    return ToolResult(ok=True, data=data, citations=result.citations, meta={
        **result.meta, "mode": "build_dot_exposure_sequence",
    })


@REGISTRY.register(
    name="boss_dot_sequence_compare",
    description=(
        "比较 2–10 套 Boss 命中/DoT/恢复混合时间轴，分别排序存活、生命余量、耗尽时间、"
        "DoT 总伤害与最低生命门槛。"
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
                            "required": ["events", "dot_windows", "recovery_events", "level", "life"],
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
def boss_dot_sequence_compare(scenarios: list[dict[str, Any]]) -> ToolResult:
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
        checked = boss_dot_sequence_check(**inputs)
        if not checked.ok:
            return ToolResult(ok=False, error=f"方案「{name}」：{checked.error}")
        data = dict(checked.data or {})
        if not data.get("assessable"):
            return ToolResult(ok=False, error=f"方案「{name}」无法安全计算")
        names.append(name)
        payloads.append(data)
        # 重新构造计算对象会丢表面审计字段；比较只读取确定性标量。
        for citation in checked.citations:
            key = (citation.get("chunk_id"), citation.get("url"))
            if key not in seen:
                seen.add(key)
                citations.append(citation)
    rank = {"fails_full_range": 0, "roll_dependent": 1, "survives_full_range": 2}
    rows = [{
        "name": name, "outcome": data["outcome"],
        "ending_life_high_roll": data["high_roll"]["ending_life"],
        "depletion_time_high_roll": data["high_roll"]["depletion_time_seconds"],
        "total_dot_damage_high_roll": data["high_roll"]["total_dot_damage"],
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
        "longest_time_to_depletion_scenario": max(
            rows, key=lambda row: (
                row["depletion_time_high_roll"] is None,
                row["depletion_time_high_roll"] or 0,
            ),
        )["name"],
        "lowest_dot_damage_scenario": min(
            rows, key=lambda row: row["total_dot_damage_high_roll"]
        )["name"],
        "lowest_required_life_scenario": (
            min(reachable, key=lambda row: row["minimum_life_for_full_range"])["name"]
            if reachable else None
        ),
        "boundary": BOUNDARY,
    }
    payload["answer"] = (
        "### Boss DoT 暴露方案比较\n\n"
        f"- 存活结论最佳：**{payload['best_survival_scenario']}**\n"
        f"- 高伤轨生命余量最佳：**{payload['best_high_roll_life_margin_scenario']}**\n"
        f"- 高伤轨耗尽时间最长：**{payload['longest_time_to_depletion_scenario']}**\n"
        f"- DoT 总承伤最低：**{payload['lowest_dot_damage_scenario']}**\n\n"
        f"> 边界：{BOUNDARY}。"
    )
    return ToolResult(ok=True, data=payload, citations=citations, meta={
        "channel": "derived", "mode": "explicit_dot_exposure_comparison",
    })
