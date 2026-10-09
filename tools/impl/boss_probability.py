"""Boss 显式时间轴上的概率防御、生存分布与方案比较工具。"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

from calc import boss_probability
from calc.boss_probability import (
    DefenceProfile,
    ProbabilisticEvent,
    ProbabilisticSequenceResult,
)
from calc.boss_timeline import TimedDamageEvent
from calc.derive import fmt
from tools.impl.boss_timeline import (
    boss_timed_sequence_check,
    boss_timed_sequence_from_build,
)
from tools.registry import REGISTRY, ToolResult

if TYPE_CHECKING:
    from parsers.pob_build import PobBuild


BOUNDARY = (
    "只对用户显式标注为 attack/spell 的定时 Boss 命中建模。概率事件按每击独立处理；"
    "攻击依次结算未命中与格挡，法术依次结算格挡与压制，格挡和压制不叠加。低/高伤害"
    "轨分开计算，不猜区间内掷骰分布。离散池桶会产生响应所列量化误差；概率不是保证。"
    "不含幸运/不幸、熵式闪避相关性、不可闪避/不可格挡、恢复延迟的分支重置、DoT、"
    "Ward、MoM、生命/护盾分池、药剂、阶段机制或真实连招推断"
)


_EVENTS_SCHEMA = {
    "type": "array", "minItems": 1, "maxItems": 100,
    "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "skill": {"type": "string", "minLength": 2, "maxLength": 120},
            "at_seconds": {"type": "number", "minimum": 0, "maximum": 3600},
            "hit_type": {"type": "string", "enum": ["attack", "spell"]},
        },
        "required": ["skill", "at_seconds", "hit_type"],
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
    "recovery_delay_seconds": {
        "type": "number", "minimum": 0, "maximum": 3600, "default": 0,
    },
    "attack_hit_chance_pct": {
        "type": "number", "minimum": 0, "maximum": 100, "default": 100,
    },
    "attack_block_chance_pct": {
        "type": "number", "minimum": 0, "maximum": 100, "default": 0,
    },
    "spell_block_chance_pct": {
        "type": "number", "minimum": 0, "maximum": 100, "default": 0,
    },
    "spell_suppression_chance_pct": {
        "type": "number", "minimum": 0, "maximum": 100, "default": 0,
    },
    "blocked_damage_taken_pct": {
        "type": "number", "minimum": 0, "maximum": 100, "default": 0,
    },
    "suppressed_damage_taken_pct": {
        "type": "number", "minimum": 0, "maximum": 100, "default": 60,
    },
    "bucket_size": {"type": ["number", "null"], "exclusiveMinimum": 0},
}


def _validate_events(events: object) -> list[dict[str, Any]]:
    if not isinstance(events, list) or not 1 <= len(events) <= 100:
        raise ValueError("events 必须包含 1..100 次命中")
    normalized: list[dict[str, Any]] = []
    previous = -1.0
    for index, item in enumerate(events):
        if not isinstance(item, dict):
            raise ValueError(f"events[{index}] 必须是对象")
        unknown = set(item) - {"skill", "at_seconds", "hit_type"}
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
        hit_type = item.get("hit_type")
        if hit_type not in {"attack", "spell"}:
            raise ValueError(f"events[{index}].hit_type 只支持 attack 或 spell")
        normalized.append({
            "skill": skill, "at_seconds": seconds, "hit_type": hit_type,
        })
        previous = seconds
    return normalized


def _profile(
    *, attack_hit_chance_pct: float, attack_block_chance_pct: float,
    spell_block_chance_pct: float, spell_suppression_chance_pct: float,
    blocked_damage_taken_pct: float, suppressed_damage_taken_pct: float,
) -> DefenceProfile:
    return DefenceProfile(
        attack_hit_chance_pct=attack_hit_chance_pct,
        attack_block_chance_pct=attack_block_chance_pct,
        spell_block_chance_pct=spell_block_chance_pct,
        spell_suppression_chance_pct=spell_suppression_chance_pct,
        blocked_damage_taken_pct=blocked_damage_taken_pct,
        suppressed_damage_taken_pct=suppressed_damage_taken_pct,
    )


def _probabilistic_events(
    requested: list[dict[str, Any]], steps: list[dict[str, Any]],
) -> tuple[ProbabilisticEvent, ...]:
    return tuple(ProbabilisticEvent(
        hit_type=event["hit_type"],
        damage=TimedDamageEvent(
            at_seconds=float(event["at_seconds"]), skill=str(step["skill"]),
            damage_type=str(step["damage_type"]), raw_low=float(step["raw_low"]),
            raw_high=float(step["raw_high"]), taken_low=float(step["taken_low"]),
            taken_high=float(step["taken_high"]),
        ),
    ) for event, step in zip(requested, steps, strict=True))


def _render(result: ProbabilisticSequenceResult, *, level: int, uber: bool) -> str:
    profile = result.profile
    lines = [
        "### Boss 概率防御与生存分布" + ("（Uber）" if uber else ""), "",
        f"- 敌人等级 {level}；防御池 {fmt(result.pool)}；命中数 {result.hit_count}",
        f"- 攻击命中 {fmt(profile.attack_hit_chance_pct)}%；攻击格挡 "
        f"{fmt(profile.attack_block_chance_pct)}%；法术格挡 "
        f"{fmt(profile.spell_block_chance_pct)}%；法术压制 "
        f"{fmt(profile.spell_suppression_chance_pct)}%",
        f"- 格挡后承伤 {fmt(profile.blocked_damage_taken_pct)}%；压制后承伤 "
        f"{fmt(profile.suppressed_damage_taken_pct)}%", "",
        "| 伤害轨 | 存活概率 | 耗尽概率 | 期望末尾池 | P50 末尾池 | P10 / P05 末尾池 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, roll in (("低伤", result.low_roll), ("高伤", result.high_roll)):
        lines.append(
            f"| {label} | {fmt(roll.survival_probability_pct)}% | "
            f"{fmt(roll.depletion_probability_pct)}% | "
            f"{fmt(roll.expected_remaining_pool)} | {fmt(roll.remaining_pool_p50)} | "
            f"{fmt(roll.remaining_pool_p10)} / {fmt(roll.remaining_pool_p05)} |"
        )
    deterministic = "能存活" if result.deterministic_worst_case_survives else "会耗尽"
    lines.extend([
        "", f"- 全部命中、全部未格挡/未压制且全为高伤时：**{deterministic}**；"
        f"末尾池 {fmt(result.deterministic_worst_case_ending_pool)}",
        f"- 高伤轨 P95 损失：{fmt(result.high_roll.loss_from_full_pool_p95)}；"
        f"最可能耗尽于第 {result.high_roll.most_likely_depletion_hit or '—'} 击",
        f"- 离散桶宽 {fmt(result.bucket_size)}，最多 {result.bucket_count} 桶；"
        f"累计量化误差上界 {fmt(result.quantization_error_bound)} 池值",
        "", f"> 概率模型：{result.probability_model}。", f"> 边界：{BOUNDARY}。",
    ])
    return "\n".join(lines)


def _clear_probability_metrics(data: dict[str, Any]) -> None:
    for key in ProbabilisticSequenceResult.__dataclass_fields__:
        data.pop(key, None)


@REGISTRY.register(
    name="boss_probabilistic_sequence_check",
    description=(
        "对用户显式标注 attack/spell 的定时 Boss 命中做闪避/命中、攻击/法术格挡、法术压制"
        "离散动态规划，输出低/高伤害轨存活概率、逐击耗尽概率、分位数和确定性最坏情况。"
    ),
    parameters={
        "type": "object", "properties": _INPUT_PROPERTIES,
        "required": ["events", "level", "pool"], "additionalProperties": False,
    },
)
def boss_probabilistic_sequence_check(
    events: list[dict[str, Any]], level: int, pool: float,
    uber: bool = False, roll_range: float | None = None,
    fire_resist_pct: float | None = None, cold_resist_pct: float | None = None,
    lightning_resist_pct: float | None = None, chaos_resist_pct: float | None = None,
    armour: float = 0.0, flat_physical_reduction_pct: float = 0.0,
    taken_mult: float = 1.0, overwhelm_pct: float = 0.0,
    recovery_per_second: float = 0.0, recovery_delay_seconds: float = 0.0,
    attack_hit_chance_pct: float = 100.0,
    attack_block_chance_pct: float = 0.0,
    spell_block_chance_pct: float = 0.0,
    spell_suppression_chance_pct: float = 0.0,
    blocked_damage_taken_pct: float = 0.0,
    suppressed_damage_taken_pct: float = 60.0,
    bucket_size: float | None = None,
) -> ToolResult:
    try:
        normalized = _validate_events(events)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    timed = boss_timed_sequence_check(
        events=[{k: item[k] for k in ("skill", "at_seconds")} for item in normalized],
        level=level, pool=pool, uber=uber, roll_range=roll_range,
        fire_resist_pct=fire_resist_pct, cold_resist_pct=cold_resist_pct,
        lightning_resist_pct=lightning_resist_pct, chaos_resist_pct=chaos_resist_pct,
        armour=armour, flat_physical_reduction_pct=flat_physical_reduction_pct,
        taken_mult=taken_mult, overwhelm_pct=overwhelm_pct,
        recovery_per_second=recovery_per_second,
        recovery_delay_seconds=recovery_delay_seconds,
    )
    if not timed.ok:
        return timed
    base = dict(timed.data or {})
    if not base.get("assessable"):
        _clear_probability_metrics(base)
        base.update({
            "requested_events": normalized, "boundary": BOUNDARY,
            "answer": f"{base.get('answer', '')}\n> 概率防御边界：{BOUNDARY}。",
        })
        return ToolResult(ok=True, data=base, citations=timed.citations, meta=timed.meta)
    profile = _profile(
        attack_hit_chance_pct=attack_hit_chance_pct,
        attack_block_chance_pct=attack_block_chance_pct,
        spell_block_chance_pct=spell_block_chance_pct,
        spell_suppression_chance_pct=spell_suppression_chance_pct,
        blocked_damage_taken_pct=blocked_damage_taken_pct,
        suppressed_damage_taken_pct=suppressed_damage_taken_pct,
    )
    try:
        assessed = boss_probability.assess_probabilistic_sequence(
            events=_probabilistic_events(normalized, base["steps"]), pool=pool,
            profile=profile, recovery_per_second=recovery_per_second,
            recovery_delay_seconds=recovery_delay_seconds, bucket_size=bucket_size,
        )
    except (ValueError, TypeError, boss_probability.BossProbabilityError) as exc:
        return ToolResult(ok=False, error=str(exc))
    answer = _render(assessed, level=level, uber=uber)
    return ToolResult(ok=True, data={
        "found": True, "assessable": True, "verdict": (
            "survives_worst_case" if assessed.deterministic_worst_case_survives
            else "probabilistic_survival_only"
        ),
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized, **assessed.as_dict(),
        "missing_inputs": [], "candidates": [], "boundary": BOUNDARY,
        "answer": answer,
    }, citations=timed.citations, meta={
        "channel": "derived", "tables": ["pob_boss_skill", "game_constant"],
        "mode": "explicit_probabilistic_timed_hit_sequence",
    })


def _take_build_probability(
    build: "PobBuild", *, hit_types: set[str],
    attack_hit_chance_pct: float | None,
    attack_block_chance_pct: float | None,
    spell_block_chance_pct: float | None,
    spell_suppression_chance_pct: float | None,
) -> tuple[dict[str, float], dict[str, str], list[str]]:
    values: dict[str, float] = {}
    sources: dict[str, str] = {}
    missing: list[str] = []

    def explicit_or_stat(
        label: str, explicit: float | None, candidates: tuple[str, ...],
        *, invert: bool = False,
    ) -> None:
        if explicit is not None:
            values[label] = float(explicit)
            sources[label] = "explicit_request"
            return
        key = next((name for name in candidates if name in build.player_stats), None)
        if key is None:
            missing.append(f"{label}（或显式覆盖）")
            return
        raw = build.player_stats[key]
        value = 100 - raw if invert else raw
        if not math.isfinite(value) or not 0 <= value <= 100:
            missing.append(f"有效的 {key}")
            return
        values[label] = float(value)
        sources[label] = f"pob_player_stat:{key}" + (":100-minus" if invert else "")

    if "attack" in hit_types:
        explicit_or_stat(
            "attack_hit_chance_pct", attack_hit_chance_pct,
            ("EvadeChance", "EvasionChance"), invert=True,
        )
        explicit_or_stat(
            "attack_block_chance_pct", attack_block_chance_pct,
            ("EffectiveBlockChance", "BlockChance"),
        )
    else:
        values.update(attack_hit_chance_pct=100.0, attack_block_chance_pct=0.0)
        sources.update(attack_hit_chance_pct="not_applicable",
                       attack_block_chance_pct="not_applicable")
    if "spell" in hit_types:
        explicit_or_stat(
            "spell_block_chance_pct", spell_block_chance_pct,
            ("EffectiveSpellBlockChance", "SpellBlockChance"),
        )
        explicit_or_stat(
            "spell_suppression_chance_pct", spell_suppression_chance_pct,
            ("EffectiveSpellSuppressionChance", "SpellSuppressionChance"),
        )
    else:
        values.update(spell_block_chance_pct=0.0,
                      spell_suppression_chance_pct=0.0)
        sources.update(spell_block_chance_pct="not_applicable",
                       spell_suppression_chance_pct="not_applicable")
    return values, sources, missing


def boss_probabilistic_sequence_from_build(
    build: "PobBuild", *, events: list[dict[str, Any]], level: int,
    uber: bool = False, roll_range: float | None = None,
    pool_mode: Literal["life", "life_and_energy_shield"] = "life",
    recovery_per_second: float | None = None, recovery_delay_seconds: float = 0.0,
    attack_hit_chance_pct: float | None = None,
    attack_block_chance_pct: float | None = None,
    spell_block_chance_pct: float | None = None,
    spell_suppression_chance_pct: float | None = None,
    blocked_damage_taken_pct: float = 0.0,
    suppressed_damage_taken_pct: float = 60.0,
    bucket_size: float | None = None,
) -> ToolResult:
    try:
        normalized = _validate_events(events)
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))
    timed = boss_timed_sequence_from_build(
        build,
        events=[{k: item[k] for k in ("skill", "at_seconds")} for item in normalized],
        level=level, uber=uber, roll_range=roll_range, pool_mode=pool_mode,
        recovery_per_second=recovery_per_second,
        recovery_delay_seconds=recovery_delay_seconds,
    )
    if not timed.ok:
        return timed
    base = dict(timed.data or {})
    if not base.get("assessable"):
        _clear_probability_metrics(base)
        base.update({
            "requested_events": normalized, "boundary": BOUNDARY,
            "answer": f"{base.get('answer', '')}\n> 概率防御边界：{BOUNDARY}。",
        })
        return ToolResult(ok=True, data=base, citations=timed.citations, meta=timed.meta)
    values, sources, missing = _take_build_probability(
        build, hit_types={item["hit_type"] for item in normalized},
        attack_hit_chance_pct=attack_hit_chance_pct,
        attack_block_chance_pct=attack_block_chance_pct,
        spell_block_chance_pct=spell_block_chance_pct,
        spell_suppression_chance_pct=spell_suppression_chance_pct,
    )
    if missing:
        reason = "Build PlayerStat 缺少概率防御字段，且请求未显式覆盖"
        _clear_probability_metrics(base)
        base.update({
            "assessable": False, "verdict": "unknown", "outcome": None,
            "requested_events": normalized, "steps": [], "reason": reason,
            "missing_inputs": missing, "boundary": BOUNDARY,
            "answer": (
                "### Build 的 Boss 概率防御与生存分布\n\n**结论：无法安全计算**\n"
                f"- 原因：{reason}\n- 缺少：{'、'.join(missing)}\n\n> {BOUNDARY}。"
            ),
        })
        return ToolResult(ok=True, data=base, citations=timed.citations, meta={
            **timed.meta, "verdict": "unknown",
            "mode": "build_probabilistic_timed_sequence",
        })
    profile = _profile(
        **values, blocked_damage_taken_pct=blocked_damage_taken_pct,
        suppressed_damage_taken_pct=suppressed_damage_taken_pct,
    )
    try:
        assessed = boss_probability.assess_probabilistic_sequence(
            events=_probabilistic_events(normalized, base["steps"]),
            pool=float(base["pool"]), profile=profile,
            recovery_per_second=float(base["recovery_per_second"]),
            recovery_delay_seconds=recovery_delay_seconds, bucket_size=bucket_size,
        )
    except (ValueError, TypeError, boss_probability.BossProbabilityError) as exc:
        return ToolResult(ok=False, error=str(exc))
    source_fields = dict(base.get("source_fields") or {})
    for label, source in sources.items():
        if source.startswith("pob_player_stat:"):
            key = source.split(":", 2)[1]
            source_fields[key] = float(build.player_stats[key])
    answer = _render(assessed, level=level, uber=uber)
    answer += (
        "\n> Build 概率字段来源：" + "；".join(
            f"{key}={value}" for key, value in sources.items()
        ) + "。PoB 单击 MaximumHitTaken 未用于本时间轴。"
    )
    return ToolResult(ok=True, data={
        "found": True, "assessable": True,
        "verdict": (
            "survives_worst_case" if assessed.deterministic_worst_case_survives
            else "probabilistic_survival_only"
        ),
        "level": int(level), "uber": bool(uber), "roll_range": roll_range,
        "requested_events": normalized, **assessed.as_dict(),
        "pool_mode": pool_mode, "input_source": "pob_player_stat",
        "source_fields": source_fields,
        "source_config": {**dict(base.get("source_config") or {}), **sources},
        "calculation_mode": "build_probabilistic_timed_sequence",
        "fallback_reason": (
            "PoB *MaximumHitTaken 是单击阈值，不能拆分为概率防御时间轴"
        ),
        "missing_inputs": [], "candidates": [], "boundary": BOUNDARY,
        "answer": answer,
    }, citations=timed.citations, meta={
        **timed.meta, "mode": "build_probabilistic_timed_sequence",
    })


@REGISTRY.register(
    name="boss_probabilistic_sequence_compare",
    description=(
        "比较 2–10 套 Boss 概率防御时间轴，按高伤轨存活概率、P05 末尾池、期望末尾池和"
        "确定性最坏存活分别排序。"
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
def boss_probabilistic_sequence_compare(
    scenarios: list[dict[str, Any]],
) -> ToolResult:
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
        checked = boss_probabilistic_sequence_check(**inputs)
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
    rows = [{
        "name": name,
        "high_roll_survival_probability_pct": data["high_roll"]["survival_probability_pct"],
        "high_roll_remaining_pool_p05": data["high_roll"]["remaining_pool_p05"],
        "high_roll_expected_remaining_pool": data["high_roll"]["expected_remaining_pool"],
        "deterministic_worst_case_survives": data["deterministic_worst_case_survives"],
        "result": data,
    } for name, data in zip(names, results, strict=True)]
    best_survival = max(rows, key=lambda row: (
        row["high_roll_survival_probability_pct"], row["high_roll_remaining_pool_p05"],
    ))["name"]
    best_p05 = max(rows, key=lambda row: row["high_roll_remaining_pool_p05"])["name"]
    best_expected = max(
        rows, key=lambda row: row["high_roll_expected_remaining_pool"]
    )["name"]
    deterministic = [
        row["name"] for row in rows if row["deterministic_worst_case_survives"]
    ]
    lines = [
        "### Boss 概率防御方案比较", "",
        "| 方案 | 高伤轨存活 | 高伤 P05 池 | 高伤期望池 | 确定性最坏 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['name']} | {fmt(row['high_roll_survival_probability_pct'])}% | "
            f"{fmt(row['high_roll_remaining_pool_p05'])} | "
            f"{fmt(row['high_roll_expected_remaining_pool'])} | "
            f"{'存活' if row['deterministic_worst_case_survives'] else '耗尽'} |"
        )
    lines.extend([
        "", f"- 最高高伤轨存活概率：{best_survival}",
        f"- 最高高伤轨 P05 余量：{best_p05}",
        f"- 最高高伤轨期望余量：{best_expected}",
        f"- 确定性最坏仍存活：{'、'.join(deterministic) if deterministic else '无'}",
        "", f"> 边界：{BOUNDARY}。",
    ])
    return ToolResult(ok=True, data={
        "kind": "boss_probabilistic_sequence_comparison", "scenarios": rows,
        "best_high_roll_survival_scenario": best_survival,
        "best_high_roll_p05_margin_scenario": best_p05,
        "best_high_roll_expected_pool_scenario": best_expected,
        "deterministic_survivors": deterministic, "boundary": BOUNDARY,
        "answer": "\n".join(lines),
    }, citations=citations, meta={
        "channel": "derived", "mode": "probabilistic_sequence_comparison",
    })
