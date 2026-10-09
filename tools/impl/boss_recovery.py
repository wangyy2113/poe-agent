"""Boss 分层池主动恢复事件时间轴与方案比较工具。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

from calc import boss_recovery
from calc.boss_recovery import (
    ActiveRecoveryEvent,
    RecoveryPoolConfig,
    RecoverySequenceResult,
)
from tools.impl.boss_layered import (
    _layered_events,
    _validate_events,
    boss_layered_sequence_from_build,
)
from tools.impl.boss_timeline import boss_timed_sequence_check
from tools.registry import REGISTRY, ToolResult

if TYPE_CHECKING:
    from parsers.pob_build import PobBuild


BOUNDARY = (
    "只结算用户显式排列的 Boss 命中和恢复事件。瞬回、持续恢复与偷取实例的时间、池、"
    "数值和持续时间都来自请求；Build 仅提供基础池、固定净回复与护盾回充快照。不会从"
    "攻击频率、伤害、装备或药剂推断恢复事件，不模拟药剂充能、自动喝药、偷取生成/移除、"
    "过量偷取保留、击回触发条件、恢复阻止效果、Ward、守护技能、DoT、阶段或打法"
)


_DAMAGE_EVENTS_SCHEMA = {
    "type": "array", "minItems": 1, "maxItems": 100,
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
    "events": _DAMAGE_EVENTS_SCHEMA,
    "recovery_events": _RECOVERY_EVENTS_SCHEMA,
    "level": {"type": "integer", "minimum": 1, "maximum": 100},
    "life": {"type": "number", "exclusiveMinimum": 0},
    "energy_shield": {"type": "number", "minimum": 0, "default": 0},
    "mana": {"type": "number", "minimum": 0, "default": 0},
    "mana_before_life_pct": {
        "type": "number", "minimum": 0, "maximum": 100, "default": 0,
    },
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
    "life_recovery_per_second": {"type": "number", "minimum": 0, "default": 0},
    "energy_shield_recovery_per_second": {"type": "number", "minimum": 0, "default": 0},
    "mana_recovery_per_second": {"type": "number", "minimum": 0, "default": 0},
    "energy_shield_recharge_per_second": {"type": "number", "minimum": 0, "default": 0},
    "energy_shield_recharge_delay_seconds": {
        "type": "number", "minimum": 0, "maximum": 3600, "default": 2,
    },
    "energy_shield_recharge_interrupt_mode": {
        "type": "string", "enum": ["energy_shield_damage", "any_hit"],
        "default": "energy_shield_damage",
    },
    "life_leech_rate_cap_pct": {"type": "number", "minimum": 0, "default": 20},
    "energy_shield_leech_rate_cap_pct": {"type": "number", "minimum": 0, "default": 10},
    "mana_leech_rate_cap_pct": {"type": "number", "minimum": 0, "default": 20},
}


def _validate_recovery_requests(events: object) -> list[dict[str, Any]]:
    if not isinstance(events, list) or len(events) > 100:
        raise ValueError("recovery_events 必须包含 0..100 个事件")
    normalized: list[dict[str, Any]] = []
    allowed = {
        "name", "at_seconds", "pool", "kind", "amount", "rate_per_second",
        "duration_seconds", "phase",
    }
    for index, item in enumerate(events):
        if not isinstance(item, dict):
            raise ValueError(f"recovery_events[{index}] 必须是对象")
        unknown = set(item) - allowed
        if unknown:
            raise ValueError(
                f"recovery_events[{index}] 包含未知字段：{', '.join(sorted(unknown))}"
            )
        name = str(item.get("name") or "").strip()
        if not 1 <= len(name) <= 80:
            raise ValueError(f"recovery_events[{index}].name 必须为 1..80 个字符")
        pool = item.get("pool")
        kind = item.get("kind")
        phase = item.get("phase", "before_hit")
        if pool not in {"life", "energy_shield", "mana"}:
            raise ValueError(f"recovery_events[{index}].pool 无效")
        if kind not in {"instant", "timed_rate", "leech"}:
            raise ValueError(f"recovery_events[{index}].kind 无效")
        if phase not in {"before_hit", "after_hit"}:
            raise ValueError(f"recovery_events[{index}].phase 无效")
        numbers: dict[str, float] = {}
        for field, default, maximum in (
            ("at_seconds", None, 3600.0), ("amount", 0, 1_000_000_000.0),
            ("rate_per_second", 0, 1_000_000_000.0),
            ("duration_seconds", 0, 3600.0),
        ):
            value = item.get(field, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"recovery_events[{index}].{field} 必须是数字")
            result = float(value)
            if not math.isfinite(result) or not 0 <= result <= maximum:
                raise ValueError(
                    f"recovery_events[{index}].{field} 必须在 0..{maximum:g}"
                )
            numbers[field] = result
        if kind == "instant":
            if (
                numbers["amount"] <= 0 or numbers["rate_per_second"] != 0
                or numbers["duration_seconds"] != 0
            ):
                raise ValueError(
                    f"recovery_events[{index}] 瞬回必须给正 amount，且不能给速率或持续时间"
                )
        else:
            if (
                numbers["rate_per_second"] <= 0
                or numbers["duration_seconds"] <= 0 or numbers["amount"] != 0
            ):
                raise ValueError(
                    f"recovery_events[{index}] 持续事件必须给正速率和持续时间，且不能给 amount"
                )
            if phase != "before_hit":
                raise ValueError(
                    f"recovery_events[{index}] 只有瞬回可以使用 after_hit"
                )
            if numbers["at_seconds"] + numbers["duration_seconds"] > 3600:
                raise ValueError(f"recovery_events[{index}] 的结束时间不能超过 3600")
        normalized.append({
            "name": name, "at_seconds": numbers["at_seconds"], "pool": pool,
            "kind": kind, "amount": numbers["amount"],
            "rate_per_second": numbers["rate_per_second"],
            "duration_seconds": numbers["duration_seconds"], "phase": phase,
        })
    return sorted(normalized, key=lambda row: (row["at_seconds"], row["name"]))


def _recovery_events(events: list[dict[str, Any]]) -> tuple[ActiveRecoveryEvent, ...]:
    return tuple(ActiveRecoveryEvent(**event) for event in events)  # type: ignore[arg-type]


def _damage_events_from_layered_result(
    requested: list[dict[str, Any]], data: dict[str, Any],
):
    low_steps = data["low_roll"]["steps"]
    high_steps = data["high_roll"]["steps"]
    merged = [{
        "skill": low["skill"], "damage_type": low["damage_type"],
        "raw_low": low["raw_damage"], "raw_high": high["raw_damage"],
        "taken_low": low["taken_damage"], "taken_high": high["taken_damage"],
    } for low, high in zip(low_steps, high_steps, strict=True)]
    return _layered_events(requested, merged)


def _config(
    *, life: float, energy_shield: float, mana: float,
    mana_before_life_pct: float, life_recovery_per_second: float,
    energy_shield_recovery_per_second: float, mana_recovery_per_second: float,
    energy_shield_recharge_per_second: float,
    energy_shield_recharge_delay_seconds: float,
    energy_shield_recharge_interrupt_mode: str,
    life_leech_rate_cap_pct: float, energy_shield_leech_rate_cap_pct: float,
    mana_leech_rate_cap_pct: float,
) -> RecoveryPoolConfig:
    return RecoveryPoolConfig(
        life=life, energy_shield=energy_shield, mana=mana,
        mana_before_life_pct=mana_before_life_pct,
        life_recovery_per_second=life_recovery_per_second,
        energy_shield_recovery_per_second=energy_shield_recovery_per_second,
        mana_recovery_per_second=mana_recovery_per_second,
        energy_shield_recharge_per_second=energy_shield_recharge_per_second,
        energy_shield_recharge_delay_seconds=energy_shield_recharge_delay_seconds,
        energy_shield_recharge_interrupt_mode=(
            energy_shield_recharge_interrupt_mode  # type: ignore[arg-type]
        ),
        life_leech_rate_cap_pct=life_leech_rate_cap_pct,
        energy_shield_leech_rate_cap_pct=energy_shield_leech_rate_cap_pct,
        mana_leech_rate_cap_pct=mana_leech_rate_cap_pct,
    )


def _threshold_text(data: object) -> str:
    status = getattr(data, "status")
    if status == "not_applicable":
        return "不适用"
    if status == "unreachable":
        return "单独提高该池不可达"
    return f"{getattr(data, 'value')}（缺口 {getattr(data, 'shortfall'):g}）"


def _render(result: RecoverySequenceResult, *, level: int, uber: bool) -> str:
    labels = {
        "survives_full_range": "完整伤害区间都能存活",
        "roll_dependent": "取决于伤害掷骰",
        "fails_full_range": "即使最低伤害轨也会耗尽生命",
    }
    config = result.config
    lines = [
        "### Boss 主动恢复事件时间轴" + ("（Uber）" if uber else ""), "",
        f"**结论：{labels[result.outcome]}**",
        f"- 敌人等级 {level}；命中 {result.hit_count} 次；恢复事件 "
        f"{len(result.recovery_events)} 个",
        f"- 生命 {config.life:g} / 护盾 {config.energy_shield:g} / 魔力 "
        f"{config.mana:g}；MoM 式魔力承伤 {config.mana_before_life_pct:g}%",
        f"- 偷取速率上限：生命 {config.life_leech_rate_cap_pct:g}% / 护盾 "
        f"{config.energy_shield_leech_rate_cap_pct:g}% / 魔力 "
        f"{config.mana_leech_rate_cap_pct:g}% 最大池每秒", "",
        "| 轨迹 | 存活 | 末尾生命 / 护盾 / 魔力 | 瞬回 | 定时恢复 | 偷取 | 限速抑制 | 溢出 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, roll in (("低伤", result.low_roll), ("高伤", result.high_roll)):
        lines.append(
            f"| {label} | {'是' if roll.survived else '否'} | {roll.ending_life:g} / "
            f"{roll.ending_energy_shield:g} / {roll.ending_mana:g} | "
            f"{roll.total_instant_recovery_applied:g} | "
            f"{roll.total_timed_rate_recovery_applied:g} | "
            f"{roll.total_leech_recovery_applied:g} | "
            f"{roll.total_leech_suppressed_by_rate_cap:g} | "
            f"{roll.total_recovery_overflow:g} |"
        )
    impact = result.active_recovery_impact
    lines.extend([
        "", f"- 高伤轨不含主动恢复时末尾生命："
        f"{impact.high_roll_ending_life_without_active_recovery:g}；主动恢复带来的末尾生命差："
        f"{impact.high_roll_life_gain:g}",
        f"- 主动恢复是否改变高伤轨存活结论："
        f"{'是' if impact.changed_high_roll_survival else '否'}",
        f"- 其余层固定时的完整伤害区间门槛：生命 "
        f"{_threshold_text(result.minimum_life_for_full_range)}；护盾 "
        f"{_threshold_text(result.minimum_energy_shield_for_full_range)}；魔力 "
        f"{_threshold_text(result.minimum_mana_for_full_range)}",
        "", f"> 同刻顺序：{result.event_ordering}。",
        f"> 恢复归属：{result.recovery_attribution_model}。",
        f"> 伤害路由：{result.routing_model}。", f"> 边界：{BOUNDARY}。",
    ])
    return "\n".join(lines)


def _clear_metrics(data: dict[str, Any]) -> None:
    for key in RecoverySequenceResult.__dataclass_fields__:
        data.pop(key, None)


@REGISTRY.register(
    name="boss_recovery_sequence_check",
    description=(
        "按显式 Boss 命中与主动恢复事件推进生命、护盾、魔力；支持命中前后瞬回、药剂式"
        "持续恢复、多个偷取实例的合计速率上限、逐击审计和单轴最低池。"
    ),
    parameters={
        "type": "object", "properties": _INPUT_PROPERTIES,
        "required": ["events", "recovery_events", "level", "life"],
        "additionalProperties": False,
    },
)
def boss_recovery_sequence_check(
    events: list[dict[str, Any]], recovery_events: list[dict[str, Any]],
    level: int, life: float, energy_shield: float = 0.0, mana: float = 0.0,
    mana_before_life_pct: float = 0.0, uber: bool = False,
    roll_range: float | None = None, fire_resist_pct: float | None = None,
    cold_resist_pct: float | None = None,
    lightning_resist_pct: float | None = None,
    chaos_resist_pct: float | None = None, armour: float = 0.0,
    flat_physical_reduction_pct: float = 0.0, taken_mult: float = 1.0,
    overwhelm_pct: float = 0.0, life_recovery_per_second: float = 0.0,
    energy_shield_recovery_per_second: float = 0.0,
    mana_recovery_per_second: float = 0.0,
    energy_shield_recharge_per_second: float = 0.0,
    energy_shield_recharge_delay_seconds: float = 2.0,
    energy_shield_recharge_interrupt_mode: str = "energy_shield_damage",
    life_leech_rate_cap_pct: float = 20.0,
    energy_shield_leech_rate_cap_pct: float = 10.0,
    mana_leech_rate_cap_pct: float = 20.0,
) -> ToolResult:
    try:
        normalized_damage = _validate_events(events)
        normalized_recovery = _validate_recovery_requests(recovery_events)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    preflight = boss_timed_sequence_check(
        events=[
            {key: item[key] for key in ("skill", "at_seconds")}
            for item in normalized_damage
        ],
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
    if not base.get("assessable"):
        _clear_metrics(base)
        base.update({
            "requested_events": normalized_damage,
            "requested_recovery_events": normalized_recovery,
            "boundary": BOUNDARY,
            "answer": f"{base.get('answer', '')}\n> 主动恢复边界：{BOUNDARY}。",
        })
        return ToolResult(
            ok=True, data=base, citations=preflight.citations, meta=preflight.meta,
        )
    try:
        assessed = boss_recovery.assess_recovery_sequence(
            damage_events=_layered_events(normalized_damage, base["steps"]),
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
                life_leech_rate_cap_pct=life_leech_rate_cap_pct,
                energy_shield_leech_rate_cap_pct=energy_shield_leech_rate_cap_pct,
                mana_leech_rate_cap_pct=mana_leech_rate_cap_pct,
            ),
        )
    except (ValueError, TypeError, boss_recovery.BossRecoveryError) as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(ok=True, data={
        "found": True, "assessable": True, "verdict": assessed.outcome,
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized_damage,
        "requested_recovery_events": normalized_recovery, **assessed.as_dict(),
        "missing_inputs": [], "candidates": [], "boundary": BOUNDARY,
        "answer": _render(assessed, level=level, uber=uber),
    }, citations=preflight.citations, meta={
        "channel": "derived", "tables": ["pob_boss_skill", "game_constant"],
        "mode": "explicit_active_recovery_timed_sequence",
    })


def boss_recovery_sequence_from_build(
    build: "PobBuild", *, events: list[dict[str, Any]],
    recovery_events: list[dict[str, Any]], level: int, uber: bool = False,
    roll_range: float | None = None, mana_before_life_pct: float = 0.0,
    life_recovery_per_second: float | None = None,
    energy_shield_recovery_per_second: float | None = None,
    mana_recovery_per_second: float | None = None,
    energy_shield_recharge_per_second: float | None = None,
    energy_shield_recharge_delay_seconds: float = 2.0,
    energy_shield_recharge_interrupt_mode: Literal[
        "energy_shield_damage", "any_hit"
    ] = "energy_shield_damage",
    life_leech_rate_cap_pct: float = 20.0,
    energy_shield_leech_rate_cap_pct: float = 10.0,
    mana_leech_rate_cap_pct: float = 20.0,
) -> ToolResult:
    try:
        normalized_damage = _validate_events(events)
        normalized_recovery = _validate_recovery_requests(recovery_events)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    layered = boss_layered_sequence_from_build(
        build, events=normalized_damage, level=level, uber=uber,
        roll_range=roll_range, mana_before_life_pct=mana_before_life_pct,
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
        _clear_metrics(base)
        base.update({
            "requested_recovery_events": normalized_recovery,
            "boundary": BOUNDARY,
            "answer": f"{base.get('answer', '')}\n> 主动恢复边界：{BOUNDARY}。",
        })
        return ToolResult(
            ok=True, data=base, citations=layered.citations, meta=layered.meta,
        )
    config = base["config"]
    try:
        assessed = boss_recovery.assess_recovery_sequence(
            damage_events=_damage_events_from_layered_result(normalized_damage, base),
            recovery_events=_recovery_events(normalized_recovery),
            config=_config(
                life=float(config["life"]),
                energy_shield=float(config["energy_shield"]),
                mana=float(config["mana"]),
                mana_before_life_pct=float(config["mana_before_life_pct"]),
                life_recovery_per_second=float(config["life_recovery_per_second"]),
                energy_shield_recovery_per_second=float(
                    config["energy_shield_recovery_per_second"]
                ),
                mana_recovery_per_second=float(config["mana_recovery_per_second"]),
                energy_shield_recharge_per_second=float(
                    config["energy_shield_recharge_per_second"]
                ),
                energy_shield_recharge_delay_seconds=float(
                    config["energy_shield_recharge_delay_seconds"]
                ),
                energy_shield_recharge_interrupt_mode=str(
                    config["energy_shield_recharge_interrupt_mode"]
                ),
                life_leech_rate_cap_pct=life_leech_rate_cap_pct,
                energy_shield_leech_rate_cap_pct=energy_shield_leech_rate_cap_pct,
                mana_leech_rate_cap_pct=mana_leech_rate_cap_pct,
            ),
        )
    except (ValueError, TypeError, boss_recovery.BossRecoveryError) as exc:
        return ToolResult(ok=False, error=str(exc))
    source_config = dict(base.get("source_config") or {})
    source_config.update({
        "recovery_events": "explicit_request",
        "life_leech_rate_cap_pct": "explicit_request_or_base_default",
        "energy_shield_leech_rate_cap_pct": "explicit_request_or_base_default",
        "mana_leech_rate_cap_pct": "explicit_request_or_base_default",
    })
    answer = _render(assessed, level=level, uber=uber)
    answer += (
        "\n> Build 只提供分层池、固定净回复与护盾回充；主动恢复事件仍全部来自请求。"
    )
    return ToolResult(ok=True, data={
        "found": True, "assessable": True, "verdict": assessed.outcome,
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized_damage,
        "requested_recovery_events": normalized_recovery, **assessed.as_dict(),
        "input_source": "pob_player_stat_and_explicit_recovery_events",
        "source_fields": dict(base.get("source_fields") or {}),
        "source_config": source_config,
        "calculation_mode": "build_active_recovery_timed_sequence",
        "fallback_reason": (
            "PoB 快照不含主动恢复事件的发生时间，时间、持续与触发顺序必须显式提供"
        ),
        "missing_inputs": [], "candidates": [], "boundary": BOUNDARY,
        "answer": answer,
    }, citations=layered.citations, meta={
        **layered.meta, "mode": "build_active_recovery_timed_sequence",
    })


@REGISTRY.register(
    name="boss_recovery_sequence_compare",
    description=(
        "比较 2–10 套 Boss 主动恢复事件时间轴，分别排序存活、高伤生命余量、实际主动"
        "恢复量和其余层固定时的最低生命。"
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
                                "events", "recovery_events", "level", "life",
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
def boss_recovery_sequence_compare(scenarios: list[dict[str, Any]]) -> ToolResult:
    if not isinstance(scenarios, list) or not 2 <= len(scenarios) <= 10:
        return ToolResult(ok=False, error="scenarios 必须包含 2..10 个方案")
    names: list[str] = []
    assessed: list[tuple[str, dict[str, Any]]] = []
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
        result = boss_recovery_sequence_check(**inputs)
        if not result.ok:
            return ToolResult(ok=False, error=f"方案「{name}」：{result.error}")
        data = dict(result.data or {})
        if not data.get("assessable"):
            return ToolResult(ok=False, error=f"方案「{name}」无法安全计算")
        names.append(name)
        assessed.append((name, data))
        for citation in result.citations:
            key = (citation.get("chunk_id"), citation.get("url"))
            if key not in seen:
                seen.add(key)
                citations.append(citation)
    rank = {"fails_full_range": 0, "roll_dependent": 1, "survives_full_range": 2}
    rows = [{
        "name": name, "outcome": data["outcome"],
        "ending_life_high_roll": data["high_roll"]["ending_life"],
        "total_active_recovery_high_roll": round(
            data["high_roll"]["total_timed_rate_recovery_applied"]
            + data["high_roll"]["total_leech_recovery_applied"]
            + data["high_roll"]["total_instant_recovery_applied"], 6,
        ),
        "minimum_life_for_full_range": data["minimum_life_for_full_range"]["value"],
        "result": data,
    } for name, data in assessed]
    reachable = [row for row in rows if row["minimum_life_for_full_range"] is not None]
    payload = {
        "scenarios": rows,
        "best_survival_scenario": max(
            rows, key=lambda row: (rank[row["outcome"]], row["ending_life_high_roll"])
        )["name"],
        "best_high_roll_life_margin_scenario": max(
            rows, key=lambda row: row["ending_life_high_roll"]
        )["name"],
        "highest_active_recovery_scenario": max(
            rows, key=lambda row: row["total_active_recovery_high_roll"]
        )["name"],
        "lowest_required_life_scenario": (
            min(reachable, key=lambda row: row["minimum_life_for_full_range"])["name"]
            if reachable else None
        ),
        "boundary": BOUNDARY,
    }
    payload["answer"] = (
        "### Boss 主动恢复方案比较\n\n"
        f"- 存活结论最佳：**{payload['best_survival_scenario']}**\n"
        f"- 高伤轨生命余量最佳：**{payload['best_high_roll_life_margin_scenario']}**\n"
        f"- 实际主动恢复量最高：**{payload['highest_active_recovery_scenario']}**\n\n"
        f"> 边界：{BOUNDARY}。"
    )
    return ToolResult(ok=True, data=payload, citations=citations, meta={
        "channel": "derived", "mode": "explicit_active_recovery_comparison",
    })
