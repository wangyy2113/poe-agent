"""Boss 分层生命/能量护盾/魔力时间轴与方案比较工具。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

from calc import boss_layered
from calc.boss_layered import (
    LayeredDamageEvent,
    LayeredPoolConfig,
    LayeredSequenceResult,
)
from calc.boss_timeline import TimedDamageEvent
from calc.derive import fmt
from tools.impl.boss import boss_sequence_from_build
from tools.impl.boss_timeline import boss_timed_sequence_check
from tools.registry import REGISTRY, ToolResult

if TYPE_CHECKING:
    from parsers.pob_build import PobBuild


BOUNDARY = (
    "只结算用户显式排列的 Boss 单类型命中与明确的三池路由。每击能量护盾绕过比例、"
    "魔力承伤比例、各池固定回复、护盾回充速率/延迟与中断模式都来自请求或披露的 Build "
    "PlayerStat，不从伤害类型、职业或装备猜测。低/高伤害轨独立推进；输入时间轴不代表真实"
    "连招。不含 Ward、EB/CI 等池替换、守护技能、格挡/闪避/压制、偷取实例、击中回复、"
    "药剂、DoT、阶段机制或打法"
)


_EVENTS_SCHEMA = {
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


_INPUT_PROPERTIES = {
    "events": _EVENTS_SCHEMA,
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
    "energy_shield_recovery_per_second": {
        "type": "number", "minimum": 0, "default": 0,
    },
    "mana_recovery_per_second": {"type": "number", "minimum": 0, "default": 0},
    "energy_shield_recharge_per_second": {
        "type": "number", "minimum": 0, "default": 0,
    },
    "energy_shield_recharge_delay_seconds": {
        "type": "number", "minimum": 0, "maximum": 3600, "default": 2,
    },
    "energy_shield_recharge_interrupt_mode": {
        "type": "string", "enum": ["energy_shield_damage", "any_hit"],
        "default": "energy_shield_damage",
    },
}


def _validate_events(events: object) -> list[dict[str, Any]]:
    if not isinstance(events, list) or not 1 <= len(events) <= 100:
        raise ValueError("events 必须包含 1..100 次命中")
    normalized: list[dict[str, Any]] = []
    previous = -1.0
    for index, item in enumerate(events):
        if not isinstance(item, dict):
            raise ValueError(f"events[{index}] 必须是对象")
        unknown = set(item) - {
            "skill", "at_seconds", "energy_shield_bypass_pct",
        }
        if unknown:
            raise ValueError(
                f"events[{index}] 包含未知字段：{', '.join(sorted(unknown))}"
            )
        skill = str(item.get("skill") or "").strip()
        if not 2 <= len(skill) <= 120:
            raise ValueError(f"events[{index}].skill 必须为 2..120 个字符")
        at = item.get("at_seconds")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            raise ValueError(f"events[{index}].at_seconds 必须是数字")
        seconds = float(at)
        if not math.isfinite(seconds) or not 0 <= seconds <= 3600:
            raise ValueError(f"events[{index}].at_seconds 必须在 0..3600")
        if seconds < previous:
            raise ValueError("events 必须按 at_seconds 非递减排列")
        bypass = item.get("energy_shield_bypass_pct", 0)
        if isinstance(bypass, bool) or not isinstance(bypass, (int, float)):
            raise ValueError(f"events[{index}].energy_shield_bypass_pct 必须是数字")
        bypass_value = float(bypass)
        if not math.isfinite(bypass_value) or not 0 <= bypass_value <= 100:
            raise ValueError(
                f"events[{index}].energy_shield_bypass_pct 必须在 0..100"
            )
        normalized.append({
            "skill": skill, "at_seconds": seconds,
            "energy_shield_bypass_pct": bypass_value,
        })
        previous = seconds
    return normalized


def _config(
    *, life: float, energy_shield: float, mana: float,
    mana_before_life_pct: float, life_recovery_per_second: float,
    energy_shield_recovery_per_second: float, mana_recovery_per_second: float,
    energy_shield_recharge_per_second: float,
    energy_shield_recharge_delay_seconds: float,
    energy_shield_recharge_interrupt_mode: str,
) -> LayeredPoolConfig:
    return LayeredPoolConfig(
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
    )


def _layered_events(
    requested: list[dict[str, Any]], steps: list[dict[str, Any]],
) -> tuple[LayeredDamageEvent, ...]:
    return tuple(LayeredDamageEvent(
        energy_shield_bypass_pct=float(event["energy_shield_bypass_pct"]),
        damage=TimedDamageEvent(
            at_seconds=float(event["at_seconds"]), skill=str(step["skill"]),
            damage_type=str(step["damage_type"]), raw_low=float(step["raw_low"]),
            raw_high=float(step["raw_high"]), taken_low=float(step["taken_low"]),
            taken_high=float(step["taken_high"]),
        ),
    ) for event, step in zip(requested, steps, strict=True))


def _threshold_text(data: dict[str, Any]) -> str:
    if data["status"] == "not_applicable":
        return "不适用"
    if data["status"] == "unreachable":
        return "单独提高该池不可达"
    return f"{data['value']}（缺口 {fmt(data['shortfall'])}）"


def _render(result: LayeredSequenceResult, *, level: int, uber: bool) -> str:
    config = result.config
    labels = {
        "survives_full_range": "完整伤害区间都能存活",
        "roll_dependent": "取决于伤害掷骰",
        "fails_full_range": "即使最低伤害轨也会耗尽生命",
    }
    lines = [
        "### Boss 分层防御池时间轴" + ("（Uber）" if uber else ""), "",
        f"**结论：{labels[result.outcome]}**",
        f"- 敌人等级 {level}；生命 {fmt(config.life)}；能量护盾 "
        f"{fmt(config.energy_shield)}；魔力 {fmt(config.mana)}；"
        f"受伤害由魔力承担 {fmt(config.mana_before_life_pct)}%",
        f"- 每秒固定回复：生命 {fmt(config.life_recovery_per_second)} / 护盾 "
        f"{fmt(config.energy_shield_recovery_per_second)} / 魔力 "
        f"{fmt(config.mana_recovery_per_second)}；护盾回充 "
        f"{fmt(config.energy_shield_recharge_per_second)}/秒，延迟 "
        f"{fmt(config.energy_shield_recharge_delay_seconds)} 秒", "",
        "| 轨迹 | 存活 | 末尾生命 / 护盾 / 魔力 | 护盾吸收 | 魔力吸收 | 生命承伤 | 护盾回充 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, roll in (("低伤", result.low_roll), ("高伤", result.high_roll)):
        lines.append(
            f"| {label} | {'是' if roll.survived else '否'} | "
            f"{fmt(roll.ending_life)} / {fmt(roll.ending_energy_shield)} / "
            f"{fmt(roll.ending_mana)} | {fmt(roll.total_energy_shield_absorbed)} | "
            f"{fmt(roll.total_mana_absorbed)} | {fmt(roll.total_life_damage)} | "
            f"{fmt(roll.total_energy_shield_recharged)} |"
        )
    life_any = asdict_threshold(result.minimum_life_for_any_roll)
    life_full = asdict_threshold(result.minimum_life_for_full_range)
    es_full = asdict_threshold(result.minimum_energy_shield_for_full_range)
    mana_full = asdict_threshold(result.minimum_mana_for_full_range)
    lines.extend([
        "", f"- 最低初始生命：覆盖部分伤害轨 {_threshold_text(life_any)}；"
        f"覆盖完整伤害区间 {_threshold_text(life_full)}",
        f"- 其余池固定时的单轴门槛：能量护盾 {_threshold_text(es_full)}；"
        f"魔力 {_threshold_text(mana_full)}",
        f"- 高伤轨回充中断 {result.high_roll.recharge_interruption_count} 次；"
        f"实际回充窗口 {fmt(result.high_roll.recharge_active_seconds)} 秒",
    ])
    if result.high_roll.depletion_hit is not None:
        lines.append(f"- 高伤轨在第 {result.high_roll.depletion_hit} 次命中耗尽生命")
    lines.extend([
        "", f"> 路由：{result.routing_model}。", f"> 边界：{BOUNDARY}。",
    ])
    return "\n".join(lines)


def asdict_threshold(value: object) -> dict[str, Any]:
    return {
        "status": getattr(value, "status"), "value": getattr(value, "value"),
        "current": getattr(value, "current"), "shortfall": getattr(value, "shortfall"),
    }


def _clear_layered_metrics(data: dict[str, Any]) -> None:
    for key in LayeredSequenceResult.__dataclass_fields__:
        data.pop(key, None)


@REGISTRY.register(
    name="boss_layered_sequence_check",
    description=(
        "按用户显式时间轴分别推进生命、能量护盾和魔力承伤，结算逐击护盾绕过、MoM 式"
        "魔力分流、各池固定回复、护盾回充延迟/中断、低高伤轨与单轴最低池。"
    ),
    parameters={
        "type": "object", "properties": _INPUT_PROPERTIES,
        "required": ["events", "level", "life"], "additionalProperties": False,
    },
)
def boss_layered_sequence_check(
    events: list[dict[str, Any]], level: int, life: float,
    energy_shield: float = 0.0, mana: float = 0.0,
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
) -> ToolResult:
    try:
        normalized = _validate_events(events)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    # 共享池结果不外泄，只复用技能唯一性、伤害、穿透与抗性/护甲结算。
    preflight = boss_timed_sequence_check(
        events=[{k: item[k] for k in ("skill", "at_seconds")} for item in normalized],
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
        _clear_layered_metrics(base)
        base.update({
            "requested_events": normalized, "boundary": BOUNDARY,
            "answer": f"{base.get('answer', '')}\n> 分层防御池边界：{BOUNDARY}。",
        })
        return ToolResult(
            ok=True, data=base, citations=preflight.citations, meta=preflight.meta
        )
    try:
        assessed = boss_layered.assess_layered_sequence(
            events=_layered_events(normalized, base["steps"]),
            config=_config(
                life=life, energy_shield=energy_shield, mana=mana,
                mana_before_life_pct=mana_before_life_pct,
                life_recovery_per_second=life_recovery_per_second,
                energy_shield_recovery_per_second=(
                    energy_shield_recovery_per_second
                ),
                mana_recovery_per_second=mana_recovery_per_second,
                energy_shield_recharge_per_second=(
                    energy_shield_recharge_per_second
                ),
                energy_shield_recharge_delay_seconds=(
                    energy_shield_recharge_delay_seconds
                ),
                energy_shield_recharge_interrupt_mode=(
                    energy_shield_recharge_interrupt_mode
                ),
            ),
        )
    except (ValueError, TypeError, boss_layered.BossLayeredError) as exc:
        return ToolResult(ok=False, error=str(exc))
    answer = _render(assessed, level=level, uber=uber)
    return ToolResult(ok=True, data={
        "found": True, "assessable": True, "verdict": assessed.outcome,
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized, **assessed.as_dict(),
        "missing_inputs": [], "candidates": [], "boundary": BOUNDARY,
        "answer": answer,
    }, citations=preflight.citations, meta={
        "channel": "derived", "tables": ["pob_boss_skill", "game_constant"],
        "mode": "explicit_layered_pool_timed_sequence",
    })


def _take_stat(
    build: "PobBuild", candidates: tuple[str, ...], *, label: str,
    explicit: float | None, source_fields: dict[str, float],
    source_config: dict[str, str], missing: list[str],
) -> float | None:
    if explicit is not None:
        source_config[label] = "explicit_request"
        return float(explicit)
    key = next((name for name in candidates if name in build.player_stats), None)
    if key is None:
        missing.append(f"{label}（或显式覆盖）")
        return None
    value = build.player_stats[key]
    if not math.isfinite(value) or value < 0:
        missing.append(f"有效的 {key}")
        return None
    source_fields[key] = float(value)
    source_config[label] = f"pob_player_stat:{key}"
    return float(value)


def boss_layered_sequence_from_build(
    build: "PobBuild", *, events: list[dict[str, Any]], level: int,
    uber: bool = False, roll_range: float | None = None,
    mana_before_life_pct: float = 0.0,
    life_recovery_per_second: float | None = None,
    energy_shield_recovery_per_second: float | None = None,
    mana_recovery_per_second: float | None = None,
    energy_shield_recharge_per_second: float | None = None,
    energy_shield_recharge_delay_seconds: float = 2.0,
    energy_shield_recharge_interrupt_mode: Literal[
        "energy_shield_damage", "any_hit"
    ] = "energy_shield_damage",
) -> ToolResult:
    try:
        normalized = _validate_events(events)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    # life 模式只借用基础防御字段，不把能量护盾合并进共享池，也不会触发混沌绕盾猜测。
    base = boss_sequence_from_build(
        build,
        sequence=[{"skill": item["skill"], "count": 1} for item in normalized],
        level=level, uber=uber, roll_range=roll_range, pool_mode="life",
    )
    if not base.ok:
        return base
    base_data = dict(base.data or {})
    if not base_data.get("assessable"):
        _clear_layered_metrics(base_data)
        base_data.update({
            "requested_events": normalized, "boundary": BOUNDARY,
            "answer": f"{base_data.get('answer', '')}\n> 分层防御池边界：{BOUNDARY}。",
        })
        return ToolResult(ok=True, data=base_data, citations=base.citations, meta=base.meta)

    source_fields = dict(base_data.get("source_fields") or {})
    source_config = dict(base_data.get("source_config") or {})
    missing: list[str] = []
    life = float(base_data["pool"])
    energy_shield = _take_stat(
        build, ("EnergyShield",), label="energy_shield", explicit=None,
        source_fields=source_fields, source_config=source_config, missing=missing,
    )
    mana = 0.0
    if mana_before_life_pct > 0:
        mana_value = _take_stat(
            build, ("ManaUnreserved", "Mana"), label="mana", explicit=None,
            source_fields=source_fields, source_config=source_config, missing=missing,
        )
        if mana_value is not None:
            mana = mana_value
    else:
        source_config["mana"] = "not_applicable"

    life_rate = _take_stat(
        build, ("NetLifeRegen", "LifeRegenRecovery", "LifeRegen"),
        label="life_recovery_per_second", explicit=life_recovery_per_second,
        source_fields=source_fields, source_config=source_config, missing=missing,
    )
    es_rate = recharge_rate = 0.0
    if energy_shield is not None and energy_shield > 0:
        value = _take_stat(
            build,
            ("NetEnergyShieldRegen", "EnergyShieldRegenRecovery", "EnergyShieldRegen"),
            label="energy_shield_recovery_per_second",
            explicit=energy_shield_recovery_per_second,
            source_fields=source_fields, source_config=source_config, missing=missing,
        )
        if value is not None:
            es_rate = value
        value = _take_stat(
            build, ("EnergyShieldRecharge",),
            label="energy_shield_recharge_per_second",
            explicit=energy_shield_recharge_per_second,
            source_fields=source_fields, source_config=source_config, missing=missing,
        )
        if value is not None:
            recharge_rate = value
    else:
        source_config["energy_shield_recovery_per_second"] = "not_applicable"
        source_config["energy_shield_recharge_per_second"] = "not_applicable"
    mana_rate = 0.0
    if mana_before_life_pct > 0:
        value = _take_stat(
            build, ("NetManaRegen", "ManaRegenRecovery", "ManaRegen"),
            label="mana_recovery_per_second", explicit=mana_recovery_per_second,
            source_fields=source_fields, source_config=source_config, missing=missing,
        )
        if value is not None:
            mana_rate = value
    else:
        source_config["mana_recovery_per_second"] = "not_applicable"
    if missing:
        reason = "Build PlayerStat 缺少分层防御池或回复字段，且请求未显式覆盖"
        _clear_layered_metrics(base_data)
        base_data.update({
            "assessable": False, "verdict": "unknown", "outcome": None,
            "requested_events": normalized, "steps": [], "reason": reason,
            "missing_inputs": missing, "boundary": BOUNDARY,
            "source_fields": source_fields, "source_config": source_config,
            "calculation_mode": "unknown",
            "answer": (
                "### Build 的 Boss 分层防御池时间轴\n\n**结论：无法安全计算**\n"
                f"- 原因：{reason}\n- 缺少：{'、'.join(missing)}\n\n> {BOUNDARY}。"
            ),
        })
        return ToolResult(ok=True, data=base_data, citations=base.citations, meta={
            **base.meta, "verdict": "unknown", "mode": "build_layered_sequence",
        })

    try:
        assessed = boss_layered.assess_layered_sequence(
            events=_layered_events(normalized, base_data["steps"]),
            config=_config(
                life=life, energy_shield=float(energy_shield), mana=mana,
                mana_before_life_pct=mana_before_life_pct,
                life_recovery_per_second=float(life_rate),
                energy_shield_recovery_per_second=float(es_rate),
                mana_recovery_per_second=mana_rate,
                energy_shield_recharge_per_second=float(recharge_rate),
                energy_shield_recharge_delay_seconds=(
                    energy_shield_recharge_delay_seconds
                ),
                energy_shield_recharge_interrupt_mode=(
                    energy_shield_recharge_interrupt_mode
                ),
            ),
        )
    except (ValueError, TypeError, boss_layered.BossLayeredError) as exc:
        return ToolResult(ok=False, error=str(exc))
    answer = _render(assessed, level=level, uber=uber)
    answer += (
        "\n> Build 字段来源：" + "；".join(
            f"{key}={value}" for key, value in source_config.items()
        ) + "。PoB 单击 MaximumHitTaken 未用于分层时间轴。"
    )
    return ToolResult(ok=True, data={
        "found": True, "assessable": True, "verdict": assessed.outcome,
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized, **assessed.as_dict(),
        "input_source": "pob_player_stat", "source_fields": source_fields,
        "source_config": source_config,
        "calculation_mode": "build_layered_pool_timed_sequence",
        "fallback_reason": (
            "PoB *MaximumHitTaken 是单击阈值，不能拆分为多池逐击状态"
        ),
        "missing_inputs": [], "candidates": [], "boundary": BOUNDARY,
        "answer": answer,
    }, citations=base.citations, meta={
        **base.meta, "mode": "build_layered_pool_timed_sequence",
    })


@REGISTRY.register(
    name="boss_layered_sequence_compare",
    description=(
        "比较 2–10 套生命/能量护盾/魔力分层 Boss 时间轴，分别排序存活结论、高伤生命"
        "余量、高伤生命承伤与其余层固定时的最低生命。"
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
                            "required": ["events", "level", "life"],
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
def boss_layered_sequence_compare(scenarios: list[dict[str, Any]]) -> ToolResult:
    if not isinstance(scenarios, list) or not 2 <= len(scenarios) <= 10:
        return ToolResult(ok=False, error="scenarios 必须包含 2..10 个方案")
    names: list[str] = []
    results: list[dict[str, Any]] = []
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
        checked = boss_layered_sequence_check(**inputs)
        if not checked.ok:
            return ToolResult(ok=False, error=f"方案「{name}」：{checked.error}")
        data = dict(checked.data or {})
        if not data.get("assessable"):
            return ToolResult(ok=False, error=f"方案「{name}」无法安全计算")
        names.append(name)
        results.append(data)
        for citation in checked.citations:
            key = (citation.get("chunk_id"), citation.get("url"))
            if key not in seen:
                seen.add(key)
                citations.append(citation)
    rank = {"fails_full_range": 0, "roll_dependent": 1, "survives_full_range": 2}
    rows = [{
        "name": name, "outcome": data["outcome"],
        "ending_life_high_roll": data["high_roll"]["ending_life"],
        "lowest_life_high_roll": data["high_roll"]["lowest_life"],
        "total_life_damage_high_roll": data["high_roll"]["total_life_damage"],
        "minimum_life_for_full_range": data["minimum_life_for_full_range"]["value"],
        "result": data,
    } for name, data in zip(names, results, strict=True)]
    best_survival = max(rows, key=lambda row: (
        rank[row["outcome"]], row["lowest_life_high_roll"],
    ))["name"]
    best_margin = max(rows, key=lambda row: row["lowest_life_high_roll"])["name"]
    lowest_damage = min(rows, key=lambda row: row["total_life_damage_high_roll"])["name"]
    reachable = [row for row in rows if row["minimum_life_for_full_range"] is not None]
    lowest_life = (
        min(reachable, key=lambda row: row["minimum_life_for_full_range"])["name"]
        if reachable else None
    )
    lines = [
        "### Boss 分层防御池方案比较", "",
        "| 方案 | 结论 | 高伤末尾生命 | 高伤最低生命 | 高伤生命承伤 | 完整区间最低生命 |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['name']} | {row['outcome']} | {fmt(row['ending_life_high_roll'])} | "
            f"{fmt(row['lowest_life_high_roll'])} | "
            f"{fmt(row['total_life_damage_high_roll'])} | "
            f"{row['minimum_life_for_full_range'] if row['minimum_life_for_full_range'] is not None else '不可达'} |"
        )
    lines.extend([
        "", f"- 最佳存活结论：{best_survival}",
        f"- 最高高伤生命余量：{best_margin}",
        f"- 最低高伤生命承伤：{lowest_damage}",
        f"- 最低可达初始生命：{lowest_life or '全部不可达'}",
        "", f"> 边界：{BOUNDARY}。",
    ])
    return ToolResult(ok=True, data={
        "kind": "boss_layered_sequence_comparison", "scenarios": rows,
        "best_survival_scenario": best_survival,
        "best_high_roll_life_margin_scenario": best_margin,
        "lowest_high_roll_life_damage_scenario": lowest_damage,
        "lowest_required_life_scenario": lowest_life,
        "boundary": BOUNDARY, "answer": "\n".join(lines),
    }, citations=citations, meta={
        "channel": "derived", "mode": "layered_pool_sequence_comparison",
    })
