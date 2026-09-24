"""Boss 技能一击伤害查询工具。

**只给伤害建模，不给打法。** 这个工具回答「谢勒的球在 84 级打多少冰霜伤害」，
不回答「谢勒怎么打」——技能机制、阶段与应对本地没有来源。

## 为什么默认给区间

`rollRange` 是 PoB 的伤害掷骰配置（0..100）。用户不指定时**两端都给**，
因为「打多少伤害」的诚实答案是一个区间——替用户挑一个点，
就是把一个范围偷偷说成了一个确定值。

## 它是给 max_hit_calc 用的

`max_hit_calc` 一直要求用户自报「这一击多少伤害」。有了这一维，
那个数第一次能从库里取到。证据里明说这条衔接，但**不替用户算**——
两个工具各做一件事，串起来的是模型（且每一步都有自己的引用）。

## 数字是 PoB 的建模值

这一维**只有 PoB 一个来源**（P2）：GGG 没有公布 Boss 技能的伤害系数。
证据里要写明，否则它读起来像官方数值。
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import TYPE_CHECKING, Any, Literal

from calc import boss_damage, boss_survival, constants, maxhit
from calc.derive import pob_url, render_steps
from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY, Source
from tools.registry import REGISTRY, ToolResult

if TYPE_CHECKING:
    from parsers.pob_build import PobBuild

MAX_EVIDENCE_CHARS = 2400
MAX_MATCHES = 8

_BOUNDARY = (
    "只给 Boss 技能的**伤害建模**（伤害 / 穿透 / Uber 倍率），"
    "**不含技能机制、阶段与应对打法**——那一维本地没有来源。"
    "数字是 **Path of Building 的建模值**，GGG 未公布这些系数。"
    "要判断「扛不扛得住」，把这里的伤害交给 `max_hit_calc`。"
)

_SURVIVAL_BOUNDARY = (
    "只比较 PoB 预设的同一单伤害类型命中与用户明确提供的防御池；"
    "次数大于 1 时只按每次都命中、期间无恢复累加。不含持续伤害、真实 Boss 连招、"
    "不同技能组合、伤害转化、暴击、格挡、闪避、法术压制、守护技能、"
    "Ward、Mind over Matter、恢复、阶段机制或打法。结论不是整场 Boss 战的生存保证"
)

_SEQUENCE_BOUNDARY = (
    "只累计用户显式排列的 PoB Boss 技能单类型命中；每一步均假设命中，期间无恢复。"
    "该输入不是游戏真实连招数据，不含持续伤害、伤害转化、暴击、格挡、闪避、法术压制、"
    "守护技能、Ward、Mind over Matter、阶段机制、药剂或打法"
)

_DAMAGE_TYPE_ZH = {
    "Physical": "物理",
    "Fire": "火焰",
    "Cold": "冰霜",
    "Lightning": "闪电",
    "Chaos": "混沌",
}

BuildPoolMode = Literal["life", "life_and_energy_shield"]

_BUILD_SNAPSHOT_BOUNDARY = (
    "防御输入逐项取自该 Build 保存的 PoB <PlayerStat> 快照；本库没有重算 Build。"
    "基础模式只纳入生命/未保留生命、可选能量护盾、对应抗性与护甲；"
    "不会把 PhysicalDamageReduction 面板值冒充额外固定物理减伤，也不补猜承伤倍率。"
)

_MAX_HIT_SNAPSHOT_BOUNDARY = (
    "PoB Max Hit 快照会纳入该 Build 当前配置下的伤害转化、承伤修正、守护技能、Ward、"
    "Mind over Matter 等防御；这里只在保存的 Boss 技能预设与普通/Uber 档位和请求一致时"
    "直接比较，不把一种配置的 Max Hit 套到另一种 Boss 命中上。它仍不覆盖多击序列、"
    "持续伤害、恢复、阶段机制或打法"
)


def _rows(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    like = f"%{query.replace('%', '').replace('_', ' ')}%"
    return conn.execute(
        "SELECT * FROM pob_boss_skill WHERE name LIKE ? ORDER BY name LIMIT ?",
        (like, MAX_MATCHES),
    ).fetchall()


def _render(row: sqlite3.Row, level: int, uber: bool, roll: float | None) -> str:
    mults = json.loads(row["damage_mults"])
    uber_mult = row["uber_damage_mult"] if uber else None
    out = [
        f"### {row['name']}（{row['damage_type']}）"
        + ("　**Uber 变体**" if uber else ""),
        "",
        f"敌人等级 {level}。伤害按 PoB 原式复算（`{boss_damage.FORMULA_SOURCE}`）：",
    ]
    for dtype, pair in sorted(mults.items()):
        base, per_roll = float(pair[0]), float(pair[1])
        if roll is None:
            low, high = boss_damage.hit_range(
                level=level, base_mult=base, roll_mult=per_roll,
                damage_type=dtype, uber_mult=uber_mult,
            )
            out += [
                "",
                f"**{dtype}**：{low.damage:,.0f} — {high.damage:,.0f}"
                "（掷骰区间 0%..100%，未指定时给两端）",
                "",
                *render_steps(high.steps),
            ]
        else:
            hit = boss_damage.hit_damage(
                level=level, base_mult=base, roll_mult=per_roll,
                damage_type=dtype, roll_range=roll, uber_mult=uber_mult,
            )
            out += ["", f"**{dtype}**：{hit.damage:,.0f}（掷骰区间 {roll:g}%）", "",
                    *render_steps(hit.steps)]

    pens = json.loads(row["uber_penetrations"] if uber else row["penetrations"])
    out.append("")
    if pens:
        out.append("穿透：" + "、".join(f"{k} {v:g}%" for k, v in sorted(pens.items())))
    else:
        out.append("穿透：无（该变体在 PoB 里没有穿透项）")
    if row["tooltip"]:
        out.append(f"PoB 说明原文：{row['tooltip']}")
    out += [
        "",
        "> 这是**伤害建模**，不是打法：技能机制、阶段与应对未采集。",
        "> 数字来自 Path of Building 的预设系数，GGG 未公布。",
    ]
    return "\n".join(out)


def _citation(row: sqlite3.Row, text: str) -> dict[str, Any]:
    priority = int(row["source_priority"])
    return {
        "chunk_id": f"boss_skill:{row['name']}",
        "entity_id": None,
        "title": f"Boss 技能伤害：{row['name']}",
        "url": row["pob_url"],
        "source": Source.POB.value,
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "derived",
    }


def _maxhit_citation(text: str) -> dict[str, Any]:
    provenance = constants.provenance(constants.MAX_PHYS_DAMAGE_REDUCTION)
    url = pob_url(
        provenance.url, maxhit.DEFENCE_FILE, lines=maxhit.QUADRATIC_LINES
    )
    return {
        "chunk_id": "boss_survival:max_hit_formula",
        "entity_id": None,
        "title": "Boss 单次命中的防御结算与 Max Hit",
        "url": url,
        "source": provenance.source,
        "source_priority": int(provenance.source_priority),
        "can_support_facts": int(provenance.source_priority) <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "derived",
    }


def _unknown_survival_answer(
    skill: str,
    reason: str,
    *,
    level: int,
    uber: bool,
    pool: float | None,
    hit_count: int = 1,
) -> str:
    provided = f"敌人等级 {level}"
    if pool is not None:
        provided += f"；防御池 {pool:g}"
    if hit_count > 1:
        provided += f"；连续命中 {hit_count} 次"
    title = "单次命中" if hit_count == 1 else f"连续 {hit_count} 次命中"
    return "\n".join([
        f"### {skill} {title}生存校验" + ("（Uber）" if uber else ""),
        "",
        "**结论：无法安全计算**",
        f"- 原因：{reason}",
        f"- 已提供：{provided}",
        "",
        f"> 边界：{_SURVIVAL_BOUNDARY}。",
    ])


def _build_unknown(
    skill: str,
    reason: str,
    *,
    level: int,
    uber: bool,
    pool_mode: BuildPoolMode,
    found: bool = True,
    damage_type: str | None = None,
    pool: float | None = None,
    source_fields: dict[str, float] | None = None,
    source_config: dict[str, str] | None = None,
    missing_inputs: list[str] | None = None,
    candidates: list[str] | None = None,
    row: sqlite3.Row | None = None,
    fallback_reason: str = "",
    hit_count: int = 1,
) -> ToolResult:
    answer = _unknown_survival_answer(
        skill, reason, level=level, uber=uber, pool=pool, hit_count=hit_count
    )
    answer += f"\n> Build 快照边界：{_BUILD_SNAPSHOT_BOUNDARY}"
    return ToolResult(
        ok=True,
        data={
            "found": found,
            "assessable": False,
            "verdict": "unknown",
            "skill": skill,
            "level": int(level),
            "uber": bool(uber),
            "hit_count": hit_count,
            "damage_type": damage_type,
            "pool": pool,
            "pool_mode": pool_mode,
            "input_source": "pob_player_stat",
            "source_fields": source_fields or {},
            "source_config": source_config or {},
            "missing_inputs": missing_inputs or [],
            "candidates": candidates or [],
            "calculation_mode": "unknown",
            "fallback_reason": fallback_reason,
            "reason": reason,
            "boundary": f"{_SURVIVAL_BOUNDARY}；{_BUILD_SNAPSHOT_BOUNDARY}",
            "answer": answer,
        },
        citations=[_citation(row, answer)] if row is not None else [],
        meta={"channel": "derived", "table": "pob_boss_skill", "verdict": "unknown"}
        if row is not None else {},
    )


def _snapshot_max_hit_field(
    build: "PobBuild", damage_type: str
) -> tuple[str, float, str] | None:
    """返回 PoB 导出的对应 Max Hit；兼容“元素三抗相同只保存闪电栏”的契约。"""
    key = f"{damage_type}MaximumHitTaken"
    value = build.player_stats.get(key)
    if value is not None and math.isfinite(value) and value > 0:
        return key, float(value), ""
    if (
        damage_type in {"Fire", "Cold"}
        and "FireMaximumHitTaken" not in build.player_stats
        and "ColdMaximumHitTaken" not in build.player_stats
    ):
        value = build.player_stats.get("LightningMaximumHitTaken")
        if value is not None and math.isfinite(value) and value > 0:
            return (
                "LightningMaximumHitTaken", float(value),
                "PoB 在火/冰/闪三项相等时只导出 LightningMaximumHitTaken，界面标作 Elemental Max Hit",
            )
    return None


def _matching_pob_boss_snapshot(
    build: "PobBuild", row: sqlite3.Row, *, level: int, uber: bool, damage_type: str
) -> tuple[bool, str, dict[str, str]]:
    """判断 Max Hit 是否确实由同一 Boss 技能与变体配置生成。"""
    preset = str(build.config.get("presetBossSkills") or "").strip()
    tier = str(build.config.get("enemyIsBoss") or "").strip()
    source = {}
    if preset:
        source["presetBossSkills"] = preset
    if tier:
        source["enemyIsBoss"] = tier
    if preset.casefold() != str(row["name"]).casefold():
        return False, "Build 没有保存与请求相同的 presetBossSkills", source

    if tier not in {"Boss", "Pinnacle", "Uber"}:
        return False, "Build 没有保存可核对的 Boss/Pinnacle/Uber 敌人档位", source

    snapshot_uber = tier == "Uber" or (bool(row["earlier_uber"]) and tier == "Pinnacle")
    if bool(uber) != snapshot_uber:
        return False, "Build 保存的普通/Uber Boss 档位与请求不一致", source

    default_level = {"Boss": 83, "Pinnacle": 84, "Uber": 85}[tier]
    if tier == "Boss" and build.level is not None:
        default_level = min(default_level, int(build.level))
    raw_level = str(build.config.get("enemyLevel") or "").strip()
    if raw_level:
        source["enemyLevel"] = raw_level
        try:
            numeric_level = float(raw_level)
        except ValueError:
            return False, "Build 的 enemyLevel 不是有效整数", source
        if not math.isfinite(numeric_level) or not numeric_level.is_integer():
            return False, "Build 的 enemyLevel 不是有效整数", source
        configured_level = int(numeric_level)
        if tier in {"Pinnacle", "Uber"}:
            configured_level = max(configured_level, default_level)
    else:
        configured_level = default_level
        source["enemyLevel（PoB 默认）"] = str(default_level)
    if int(level) != configured_level:
        return False, "Build 保存或默认的 enemyLevel 与请求不一致", source

    penetration_key = (
        "enemyPhysicalOverwhelm" if damage_type == "Physical"
        else f"enemy{damage_type}Pen"
    )
    if penetration_key in build.config:
        raw = str(build.config[penetration_key]).strip()
        source[penetration_key] = raw
        try:
            configured_penetration = float(raw)
        except ValueError:
            return False, f"Build 的 {penetration_key} 不是有效数值", source
        if not math.isfinite(configured_penetration):
            return False, f"Build 的 {penetration_key} 不是有效数值", source
        penetrations = json.loads(row["uber_penetrations"] if uber else row["penetrations"])
        expected_key = (
            "PhysicalOverwhelm" if damage_type == "Physical"
            else f"{damage_type}Pen"
        )
        expected_penetration = float(penetrations.get(expected_key) or 0)
        if configured_penetration != expected_penetration:
            return False, f"Build 显式 {penetration_key} 与技能预设不一致", source
    return True, "", source


def _render_build_max_hit(
    row: sqlite3.Row,
    *,
    level: int,
    uber: bool,
    roll_range: float | None,
    raw_low: float,
    raw_high: float,
    max_hit: float,
    outcome: boss_survival.Outcome,
    max_hit_field: str,
    max_hit_field_note: str,
    source_config: dict[str, str],
) -> str:
    labels = {
        "survives_full_range": "整个已建模掷骰区间都低于 PoB Max Hit",
        "roll_dependent": "是否超过 PoB Max Hit 取决于伤害掷骰",
        "fails_full_range": "整个已建模掷骰区间都达到或超过 PoB Max Hit",
    }
    roll_note = (
        f"固定 {roll_range:g}% 掷骰点" if roll_range is not None else "0%..100% 掷骰区间"
    )
    config_text = "；".join(f"{key}={value}" for key, value in source_config.items())
    return "\n".join([
        f"### {row['name']} 单次命中生存校验" + ("（Uber）" if uber else ""),
        "",
        f"**结论：{labels[outcome]}**",
        f"- 口径：敌人等级 {level}；{roll_note}",
        f"- 原始{_DAMAGE_TYPE_ZH[str(next(iter(json.loads(row['damage_mults']))))]}伤害："
        f"{raw_low:,.0f} — {raw_high:,.0f}",
        f"- PoB Max Hit：{max_hit:,.0f}（`{max_hit_field}`）"
        + (f"；{max_hit_field_note}" if max_hit_field_note else ""),
        f"- 低 / 高掷骰的原始伤害余量：{max_hit - raw_low:,.0f} / "
        f"{max_hit - raw_high:,.0f}",
        "- 可承受次数：无法从单击 PoB Max Hit 快照安全反解",
        f"- 匹配的 Build 配置：{config_text}",
        "",
        "> 等于 Max Hit 按耗尽处理；这里直接消费 PoB 的完整 Max Hit 快照，不重复拆算防御链。",
        f"> 边界：{_MAX_HIT_SNAPSHOT_BOUNDARY}。",
        f"> Boss 技能来源：[Path of Building]({row['pob_url']})。",
    ])


def boss_survival_from_build(
    build: "PobBuild",
    *,
    skill: str,
    level: int,
    uber: bool = False,
    roll_range: float | None = None,
    hit_count: int = 1,
    use_pob_max_hit: bool = True,
    pool_mode: BuildPoolMode = "life_and_energy_shield",
) -> ToolResult:
    """用 Build 的 PoB 快照填充单次命中校验，不从职业或装备反推缺失字段。

    ``LifeUnreserved`` 只在少于总生命时由 PoB 导出；缺少它时，标准 PoB 导出契约
    表示可用生命等于 ``Life``。能量护盾是否纳入由调用方显式选择。混沌伤害下，
    ES 是否保护生命取决于 CI、Shavronne 等机制，无法从这些数值字段唯一判断，因而
    ``life_and_energy_shield`` 且 ES 大于零时保持 unknown。
    """
    name = (skill or "").strip()
    if len(name) < 2:
        return ToolResult(ok=False, error="skill 至少需要两个字符")
    if (
        isinstance(hit_count, bool)
        or not isinstance(hit_count, int)
        or not 1 <= hit_count <= 100
    ):
        return ToolResult(ok=False, error="hit_count 必须是 1..100 的整数")
    if pool_mode not in {"life", "life_and_energy_shield"}:
        return ToolResult(ok=False, error="pool_mode 只支持 life 或 life_and_energy_shield")

    rows = _rows(dbmod.get_conn(), name)
    if not rows:
        return _build_unknown(
            name, f"PoB 的 Boss 技能预设里没有「{name}」", level=level,
            uber=uber, pool_mode=pool_mode, found=False, hit_count=hit_count,
        )
    if len(rows) > 1:
        candidates = [str(row["name"]) for row in rows]
        return _build_unknown(
            name, f"「{name}」匹配到 {len(rows)} 条，请指明技能", level=level,
            uber=uber, pool_mode=pool_mode, found=False, candidates=candidates,
            hit_count=hit_count,
        )

    row = rows[0]
    mults = json.loads(row["damage_mults"])
    if str(row["damage_type"]) == "DamageOverTime":
        result = boss_survival_check(
            str(row["name"]), level=level, pool=1, uber=uber,
            roll_range=roll_range, hit_count=hit_count, cold_resist_pct=0,
        )
        data = dict(result.data or {})
        data.update({
            "pool": None,
            "pool_mode": pool_mode,
            "input_source": "pob_player_stat",
            "source_fields": {},
            "boundary": f"{data['boundary']}；{_BUILD_SNAPSHOT_BOUNDARY}",
            "answer": f"{data['answer']}\n> Build 快照边界：{_BUILD_SNAPSHOT_BOUNDARY}",
        })
        return ToolResult(ok=True, data=data, citations=result.citations, meta=result.meta)
    if len(mults) != 1 or float(row["crit_chance"] or 0) > 0:
        # 复用核心工具已经审计过的多类型与暴击 unknown 边界。
        result = boss_survival_check(
            str(row["name"]), level=level, pool=1, uber=uber,
            roll_range=roll_range, hit_count=hit_count,
            fire_resist_pct=0, cold_resist_pct=0,
            lightning_resist_pct=0, chaos_resist_pct=0,
        )
        data = dict(result.data or {})
        data.update({
            "pool": None,
            "pool_mode": pool_mode,
            "input_source": "pob_player_stat",
            "source_fields": {},
            "boundary": f"{data['boundary']}；{_BUILD_SNAPSHOT_BOUNDARY}",
            "answer": f"{data['answer']}\n> Build 快照边界：{_BUILD_SNAPSHOT_BOUNDARY}",
        })
        return ToolResult(ok=True, data=data, citations=result.citations, meta=result.meta)

    damage_type = str(next(iter(mults)))
    snapshot_matches, fallback_reason, source_config = _matching_pob_boss_snapshot(
        build, row, level=level, uber=uber, damage_type=damage_type
    )
    snapshot_max_hit = _snapshot_max_hit_field(build, damage_type)
    if hit_count > 1:
        snapshot_matches = False
        fallback_reason = (
            "PoB *MaximumHitTaken 只是单击阈值，不能用于累加"
            f" {hit_count} 次连续命中"
        )
    elif not use_pob_max_hit:
        snapshot_matches = False
        fallback_reason = "请求已通过 use_pob_max_hit=false 禁用 PoB Max Hit 优先路径"
    if snapshot_matches and snapshot_max_hit is not None:
        max_hit_field, max_hit, max_hit_field_note = snapshot_max_hit
        try:
            uber_mult = row["uber_damage_mult"] if uber else None
            if roll_range is None:
                low, high = boss_damage.hit_range(
                    level=int(level), base_mult=float(next(iter(mults.values()))[0]),
                    roll_mult=float(next(iter(mults.values()))[1]),
                    damage_type=damage_type, uber_mult=uber_mult,
                )
            else:
                hit = boss_damage.hit_damage(
                    level=int(level), base_mult=float(next(iter(mults.values()))[0]),
                    roll_mult=float(next(iter(mults.values()))[1]),
                    damage_type=damage_type, roll_range=float(roll_range),
                    uber_mult=uber_mult,
                )
                low = high = hit
        except (boss_damage.BossDamageError, constants.MissingConstant,
                constants.ConstantTypeError) as exc:
            return ToolResult(ok=False, error=str(exc))

        if high.damage < max_hit:
            outcome: boss_survival.Outcome = "survives_full_range"
        elif low.damage >= max_hit:
            outcome = "fails_full_range"
        else:
            outcome = "roll_dependent"
        answer = _render_build_max_hit(
            row, level=int(level), uber=bool(uber), roll_range=roll_range,
            raw_low=low.damage, raw_high=high.damage, max_hit=max_hit,
            outcome=outcome, max_hit_field=max_hit_field,
            max_hit_field_note=max_hit_field_note,
            source_config=source_config,
        )
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "assessable": True,
                "verdict": outcome,
                "skill": str(row["name"]),
                "level": int(level),
                "uber": bool(uber),
                "roll_range": roll_range,
                "damage_type": damage_type,
                "pool": None,
                "raw_low": low.damage,
                "raw_high": high.damage,
                "hit_count": 1,
                "total_raw_low": low.damage,
                "total_raw_high": high.damage,
                "taken_low": None,
                "taken_high": None,
                "total_taken_low": None,
                "total_taken_high": None,
                "remaining_low": None,
                "remaining_high": None,
                "earliest_depletion_hit": None,
                "latest_depletion_hit": None,
                "guaranteed_survivable_hits": None,
                "maximum_survivable_hits": None,
                "minimum_pool_for_any_roll": None,
                "minimum_pool_for_full_range": None,
                "pool_shortfall_for_any_roll": None,
                "pool_shortfall_for_full_range": None,
                "required_resist_for_full_range_pct": None,
                "resist_shortfall_pct": None,
                "required_resist_status": "unknown_from_pob_max_hit",
                "required_armour_for_full_range": None,
                "armour_shortfall": None,
                "required_armour_status": "unknown_from_pob_max_hit",
                "capacity_reason": (
                    "PoB *MaximumHitTaken 只是当前配置的单击阈值，"
                    "无法证明每击后防御状态不变"
                ),
                "max_raw_hit": max_hit,
                "margin_low": round(max_hit - low.damage, 2),
                "margin_high": round(max_hit - high.damage, 2),
                "outcome": outcome,
                "pool_mode": pool_mode,
                "input_source": "pob_player_stat",
                "source_fields": {max_hit_field: max_hit},
                "source_config": source_config,
                "max_hit_field": max_hit_field,
                "max_hit_field_note": max_hit_field_note,
                "calculation_mode": "pob_max_hit_snapshot",
                "fallback_reason": "",
                "missing_inputs": [],
                "candidates": [],
                "boundary": _MAX_HIT_SNAPSHOT_BOUNDARY,
                "answer": answer,
            },
            citations=[_citation(row, answer), _maxhit_citation(answer)],
            meta={
                "channel": "derived",
                "tables": ["pob_boss_skill", "game_constant"],
                "input_source": "pob_player_stat",
                "verdict": outcome,
            },
        )
    if snapshot_matches:
        fallback_reason = f"Build 没有导出有效的 {damage_type} Maximum Hit"

    source_fields: dict[str, float] = {}
    missing: list[str] = []

    life_key = "LifeUnreserved" if "LifeUnreserved" in build.player_stats else "Life"
    life = build.player_stats.get(life_key)
    if life is None:
        missing.append("Life（或 LifeUnreserved）")
    elif not math.isfinite(life) or life <= 0:
        missing.append(f"有效的 {life_key}")
    else:
        source_fields[life_key] = float(life)

    energy_shield = 0.0
    if pool_mode == "life_and_energy_shield":
        energy_shield_value = build.player_stats.get("EnergyShield")
        if energy_shield_value is None:
            missing.append("EnergyShield")
        elif not math.isfinite(energy_shield_value) or energy_shield_value < 0:
            missing.append("有效的 EnergyShield")
        else:
            energy_shield = float(energy_shield_value)
            source_fields["EnergyShield"] = energy_shield

    resist_keys = {
        "Fire": "FireResist", "Cold": "ColdResist",
        "Lightning": "LightningResist", "Chaos": "ChaosResist",
    }
    resist_key = resist_keys.get(damage_type)
    resist: float | None = 0.0 if damage_type == "Physical" else None
    if resist_key:
        value = build.player_stats.get(resist_key)
        if value is None:
            missing.append(resist_key)
        elif not math.isfinite(value) or not -200 <= value <= 90:
            missing.append(f"有效的 {resist_key}")
        else:
            resist = float(value)
            source_fields[resist_key] = resist

    armour = 0.0
    if damage_type == "Physical":
        value = build.player_stats.get("Armour")
        if value is None:
            missing.append("Armour")
        elif not math.isfinite(value) or value < 0:
            missing.append("有效的 Armour")
        else:
            armour = float(value)
            source_fields["Armour"] = armour

    pool = float(life) + energy_shield if life is not None and math.isfinite(life) else None
    if damage_type == "Chaos" and energy_shield > 0:
        missing.append("混沌伤害是否绕过能量护盾的 Build 机制")
    if missing or pool is None or pool <= 0:
        return _build_unknown(
            str(row["name"]), "Build 快照缺少安全计算所需字段或机制", level=level,
            uber=uber, pool_mode=pool_mode, damage_type=damage_type, pool=pool,
            source_fields=source_fields, source_config=source_config,
            missing_inputs=missing, row=row, fallback_reason=fallback_reason,
            hit_count=hit_count,
        )

    resist_args = {
        "fire_resist_pct": resist if damage_type == "Fire" else None,
        "cold_resist_pct": resist if damage_type == "Cold" else None,
        "lightning_resist_pct": resist if damage_type == "Lightning" else None,
        "chaos_resist_pct": resist if damage_type == "Chaos" else None,
    }
    result = boss_survival_check(
        str(row["name"]), level=level, pool=pool, uber=uber,
        roll_range=roll_range, hit_count=hit_count, armour=armour, **resist_args,
    )
    if not result.ok:
        return result
    data = dict(result.data or {})
    data.update({
        "pool_mode": pool_mode,
        "input_source": "pob_player_stat",
        "source_fields": source_fields,
        "source_config": source_config,
        "calculation_mode": "basic_defence_snapshot",
        "fallback_reason": fallback_reason,
        "boundary": f"{data['boundary']}；{_BUILD_SNAPSHOT_BOUNDARY}",
        "answer": (
            f"{data['answer']}\n"
            f"> Build 快照输入：{', '.join(source_fields)}；{_BUILD_SNAPSHOT_BOUNDARY}\n"
            f"> 未采用 PoB Max Hit 快照：{fallback_reason}。"
        ),
    })
    meta = dict(result.meta)
    meta["input_source"] = "pob_player_stat"
    return ToolResult(ok=True, data=data, citations=result.citations, meta=meta)


def _render_survival(
    row: sqlite3.Row,
    result: boss_survival.BossSurvivalResult,
    *,
    level: int,
    uber: bool,
    roll_range: float | None,
    armour: float,
    flat_physical_reduction_pct: float,
    taken_mult: float,
) -> str:
    labels = {
        "survives_full_range": "整个已建模掷骰区间都不会耗尽该池子",
        "roll_dependent": "是否耗尽池子取决于伤害掷骰",
        "fails_full_range": "整个已建模掷骰区间都会耗尽该池子",
    }
    dtype = _DAMAGE_TYPE_ZH[result.damage_type]
    roll_note = (
        f"固定 {roll_range:g}% 掷骰点" if roll_range is not None else "0%..100% 掷骰区间"
    )
    title = "单次命中" if result.hit_count == 1 else f"连续 {result.hit_count} 次命中"
    lines = [
        f"### {row['name']} {title}生存校验" + ("（Uber）" if uber else ""),
        "",
        f"**结论：{labels[result.outcome]}**",
        f"- 口径：敌人等级 {level}；{roll_note}；防御池 {result.pool:g}",
        f"- 原始{dtype}伤害：{result.raw_low:,.0f} — {result.raw_high:,.0f}",
    ]
    if result.damage_type == "Physical":
        lines.append(
            f"- 防御输入：护甲 {armour:g}；额外物理减伤 {flat_physical_reduction_pct:g}%"
            f"；承受倍率 {taken_mult:g}"
        )
    else:
        lines.append(
            f"- 抗性：穿透前 {result.resist_before_penetration:g}%"
            f" − 技能穿透 {result.penetration_pct:g}%"
            f" = 实际结算 {result.effective_resist_pct:g}%"
            f"；承受倍率 {taken_mult:g}"
        )
        if armour or flat_physical_reduction_pct:
            lines.append("- 忽略的输入：护甲与物理减伤不作用于这次非物理命中")
    if result.hit_count == 1:
        lines += [
            f"- 实际扣除池子：{result.taken_low:,.2f} — {result.taken_high:,.2f}",
            f"- 低 / 高掷骰后的池子余量："
            f"{result.margin_low:,.2f} / {result.margin_high:,.2f}",
        ]
    else:
        lines += [
            f"- 每击实际扣除池子：{result.taken_low:,.2f} — {result.taken_high:,.2f}",
            f"- {result.hit_count} 击累计原始伤害："
            f"{result.total_raw_low:,.2f} — {result.total_raw_high:,.2f}",
            f"- {result.hit_count} 击累计承伤："
            f"{result.total_taken_low:,.2f} — {result.total_taken_high:,.2f}",
            f"- 低 / 高掷骰后的池子余量："
            f"{result.remaining_low:,.2f} / {result.remaining_high:,.2f}",
        ]
    lines.append(f"- 同一防御口径下的原始 Max Hit：{result.max_raw_hit:,.0f}")
    if result.earliest_depletion_hit is None:
        lines.append("- 可承受次数：已建模承伤为 0，没有有限的耗尽击数")
    elif result.latest_depletion_hit is None:
        lines.append(
            f"- 可承受次数：最坏掷骰下保证承受 "
            f"{result.guaranteed_survivable_hits} 击，第 {result.earliest_depletion_hit} 击耗尽；"
            "最低掷骰承伤为 0，无有限上界"
        )
    else:
        lines.append(
            f"- 可承受次数：最坏掷骰下保证承受 "
            f"{result.guaranteed_survivable_hits} 击（第 {result.earliest_depletion_hit} 击耗尽）；"
            f"最好掷骰下最多承受 {result.maximum_survivable_hits} 击"
            f"（第 {result.latest_depletion_hit} 击耗尽）"
        )
    lines += [
        f"- 防御池门槛：低掷骰至少 "
        f"{result.minimum_pool_for_any_roll:,}；全掷骰区间至少 "
        f"{result.minimum_pool_for_full_range:,}",
        f"- 当前池子缺口：低掷骰 "
        f"{result.pool_shortfall_for_any_roll:,.2f}；全掷骰区间 "
        f"{result.pool_shortfall_for_full_range:,.2f}",
    ]
    if result.damage_type == "Physical":
        if result.required_armour_status == "reachable":
            lines.append(
                f"- 只调护甲的全区间门槛：至少 "
                f"{result.required_armour_for_full_range:,} 护甲"
                f"（当前还差 {result.armour_shortfall:,.2f}）"
            )
        else:
            lines.append("- 只调护甲的全区间门槛：在已建模减伤上限内无法达成")
    elif result.required_resist_status == "reachable":
        lines.append(
            f"- 只调穿透前抗性的全区间门槛：至少 "
            f"{result.required_resist_for_full_range_pct}%"
            f"（当前还差 {result.resist_shortfall_pct:g} 个百分点）"
        )
    else:
        lines.append("- 只调穿透前抗性的全区间门槛：90% 上限内无法达成")
    if row["tooltip"]:
        lines.append(f"- PoB 技能说明原文：{row['tooltip']}")
    assumption = (
        "“不会耗尽池子”只描述这一记已建模命中"
        if result.hit_count == 1
        else "假设同一 Boss 技能每次都命中、期间无恢复"
    )
    lines += [
        "",
        f"> {assumption}；等于池子按耗尽处理。",
        f"> 边界：{_SURVIVAL_BOUNDARY}。",
        f"> Boss 技能来源：[Path of Building]({row['pob_url']})。",
    ]
    return "\n".join(lines)


def _render_defence_threshold(
    row: sqlite3.Row,
    data: dict[str, Any],
    *,
    level: int,
    uber: bool,
    roll_range: float | None,
    target_pool: float | None,
    armour: float,
    flat_physical_reduction_pct: float,
    taken_mult: float,
) -> str:
    """把生存计算结果收束成防御规划答案，不冒充完整 Boss 战建议。"""
    hit_count = int(data["hit_count"])
    title = "单次命中" if hit_count == 1 else f"连续 {hit_count} 次命中"
    roll_note = (
        f"固定 {roll_range:g}% 掷骰点" if roll_range is not None else "0%..100% 掷骰区间"
    )
    dtype = str(data["damage_type"])
    lines = [
        f"### {row['name']} {title}防御门槛" + ("（Uber）" if uber else ""),
        "",
        f"- 口径：敌人等级 {level}；{roll_note}；每次都命中、期间无恢复",
        f"- 每击原始{_DAMAGE_TYPE_ZH[dtype]}伤害："
        f"{data['raw_low']:,.0f} — {data['raw_high']:,.0f}",
    ]
    if dtype == "Physical":
        lines.append(
            f"- 当前防御输入：护甲 {armour:g}；额外物理减伤 "
            f"{flat_physical_reduction_pct:g}%；承受倍率 {taken_mult:g}"
        )
    else:
        lines.append(
            f"- 当前抗性：穿透前 {data['resist_before_penetration']:g}%"
            f" − 技能穿透 {data['penetration_pct']:g}%"
            f" = 实际结算 {data['effective_resist_pct']:g}%"
            f"；承受倍率 {taken_mult:g}"
        )
    lines += [
        f"- 当前配置下，低掷骰至少需要 {data['minimum_pool_for_any_roll']:,} 防御池",
        f"- 要覆盖完整掷骰区间，至少需要 "
        f"{data['minimum_pool_for_full_range']:,} 防御池",
    ]
    if target_pool is not None:
        lines += [
            f"- 目标防御池：{target_pool:g}",
            f"- 防御池缺口：低掷骰 {data['pool_shortfall_for_any_roll']:,.2f}；"
            f"完整区间 {data['pool_shortfall_for_full_range']:,.2f}",
        ]
        if dtype == "Physical":
            if data["required_armour_status"] == "reachable":
                lines.append(
                    f"- 保持其余输入不变，要覆盖完整区间至少需要 "
                    f"{data['required_armour_for_full_range']:,} 护甲"
                    f"（还差 {data['armour_shortfall']:,.2f}）"
                )
            else:
                lines.append("- 只增加护甲：在已建模物理减伤上限内仍不可达")
        elif data["required_resist_status"] == "reachable":
            lines.append(
                f"- 保持其余输入不变，要覆盖完整区间至少需要穿透前抗性 "
                f"{data['required_resist_for_full_range_pct']}%"
                f"（还差 {data['resist_shortfall_pct']:g} 个百分点）"
            )
        else:
            lines.append("- 只增加穿透前抗性：在 90% 抗性上限内仍不可达")
    lines += [
        "",
        "> 防御池必须严格大于累计承伤；恰好相等仍按耗尽处理。",
        f"> 边界：{_SURVIVAL_BOUNDARY}。",
        f"> Boss 技能来源：[Path of Building]({row['pob_url']})。",
    ]
    return "\n".join(lines)


@REGISTRY.register(
    name="boss_skill_damage",
    description=(
        "查 Boss 技能在指定敌人等级下打多少伤害、穿多少抗性，以及 Uber 变体的差异。"
        "问「谢勒的球打多少伤害」「Uber 阿兹里的火焰爆破多疼」时用。"
        "未指定掷骰区间时给**区间两端**，不挑一个点。"
        "**只给伤害建模，不给技能机制与应对打法**（那一维未采集）；"
        "数字是 Path of Building 的建模值，GGG 未公布这些系数。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "minLength": 2,
                "description": "技能名（英文），如 Shaper Ball、Atziri Flameblast、Maven Fireball。",
            },
            "level": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "description": "敌人等级。终局 Boss 常见 83..85；**必填，不替用户假定**。",
            },
            "uber": {"type": "boolean", "description": "是否 Uber 变体，默认否。"},
            "roll_range": {
                "type": "number",
                "minimum": 0,
                "maximum": 100,
                "description": "伤害掷骰区间 0..100。不给就返回两端。",
            },
        },
        "required": ["skill", "level"],
        "additionalProperties": False,
    },
)
def boss_skill_damage(
    skill: str,
    level: int,
    uber: bool = False,
    roll_range: float | None = None,
) -> ToolResult:
    name = (skill or "").strip()
    if len(name) < 2:
        return ToolResult(ok=False, error="skill 至少需要两个字符")
    conn = dbmod.get_conn()
    rows = _rows(conn, name)
    if not rows:
        available = [
            str(r[0]) for r in conn.execute("SELECT name FROM pob_boss_skill ORDER BY name")
        ]
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": f"PoB 的 Boss 技能预设里没有「{name}」",
                # 只有 10 条，全列出来比让模型猜好
                "available": available,
                "boundary": _BOUNDARY,
            },
        )
    if len(rows) > 1:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": f"「{name}」匹配到 {len(rows)} 条，请指明是哪一条",
                "candidates": [str(r["name"]) for r in rows],
                "boundary": _BOUNDARY,
            },
        )

    row = rows[0]
    try:
        text = _render(row, int(level), bool(uber), roll_range)
    except boss_damage.BossDamageError as exc:
        return ToolResult(ok=False, error=str(exc))

    return ToolResult(
        ok=True,
        data={
            "found": True,
            "skill": str(row["name"]),
            "damage_type": str(row["damage_type"]),
            "level": int(level),
            "uber": bool(uber),
            "has_uber_variant": row["uber_damage_mult"] is not None,
            "boundary": _BOUNDARY,
        },
        citations=[_citation(row, text)],
        meta={"channel": "derived", "table": "pob_boss_skill"},
    )


@REGISTRY.register(
    name="boss_survival_check",
    description=(
        "把 PoB 的 Boss 技能单次原始伤害与角色明确提供的防御池、抗性、护甲和承受倍率"
        "串起来，判断整个伤害掷骰区间都不耗尽池子、取决于掷骰，还是整个区间都会耗尽。"
        "同时反解最坏/最好掷骰下可承受几击，以及第几击会耗尽池子。"
        "还会在其余输入不变的前提下，计算所需整数防御池与元素/混沌抗性或物理护甲门槛。"
        "元素穿透取自 Boss 技能预设。可按每次都命中且期间无恢复，累加同一命中 1..100 次；"
        "持续伤害、真实 Boss 连招、不同技能组合、"
        "伤害转化、暴击、格挡/闪避/压制、守护技能、Ward、MoM、恢复与打法均不在结论内。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string", "minLength": 2,
                "description": "PoB Boss 技能英文名，如 Shaper Ball、Maven Fireball。",
            },
            "level": {"type": "integer", "minimum": 1, "maximum": 100},
            "pool": {
                "type": "number", "exclusiveMinimum": 0,
                "description": "这一次命中可消耗的生命 + 能量护盾池。",
            },
            "hit_count": {
                "type": "integer", "minimum": 1, "maximum": 100, "default": 1,
                "description": "同一技能连续命中次数；假设每次命中且期间无恢复。",
            },
            "uber": {"type": "boolean", "default": False},
            "roll_range": {
                "type": "number", "minimum": 0, "maximum": 100,
                "description": "可选固定掷骰点；省略时比较 0%..100% 整个区间。",
            },
            "fire_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "cold_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "lightning_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "chaos_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "armour": {"type": "number", "minimum": 0, "default": 0},
            "flat_physical_reduction_pct": {
                "type": "number", "minimum": 0, "maximum": 90, "default": 0,
            },
            "taken_mult": {
                "type": "number", "exclusiveMinimum": 0, "default": 1,
                "description": "受到伤害乘算修正；0.8 表示少受 20%。",
            },
            "overwhelm_pct": {
                "type": "number", "minimum": 0, "maximum": 100, "default": 0,
            },
        },
        "required": ["skill", "level", "pool"],
        "additionalProperties": False,
    },
)
def boss_survival_check(
    skill: str,
    level: int,
    pool: float,
    hit_count: int = 1,
    uber: bool = False,
    roll_range: float | None = None,
    fire_resist_pct: float | None = None,
    cold_resist_pct: float | None = None,
    lightning_resist_pct: float | None = None,
    chaos_resist_pct: float | None = None,
    armour: float = 0.0,
    flat_physical_reduction_pct: float = 0.0,
    taken_mult: float = 1.0,
    overwhelm_pct: float = 0.0,
) -> ToolResult:
    name = (skill or "").strip()
    if len(name) < 2:
        return ToolResult(ok=False, error="skill 至少需要两个字符")
    if pool <= 0:
        return ToolResult(ok=False, error="pool 必须大于 0")
    if (
        isinstance(hit_count, bool)
        or not isinstance(hit_count, int)
        or not 1 <= hit_count <= 100
    ):
        return ToolResult(ok=False, error="hit_count 必须是 1..100 的整数")

    rows = _rows(dbmod.get_conn(), name)
    if not rows:
        return ToolResult(ok=True, data={
            "found": False,
            "assessable": False,
            "hit_count": hit_count,
            "reason": f"PoB 的 Boss 技能预设里没有「{name}」",
            "boundary": _SURVIVAL_BOUNDARY,
            "answer": _unknown_survival_answer(
                name, "没有匹配的 PoB Boss 技能预设", level=level, uber=uber,
                pool=pool, hit_count=hit_count
            ),
        })
    if len(rows) > 1:
        candidates = [str(row["name"]) for row in rows]
        return ToolResult(ok=True, data={
            "found": False,
            "assessable": False,
            "hit_count": hit_count,
            "reason": f"「{name}」匹配到 {len(rows)} 条，请指明技能",
            "candidates": candidates,
            "boundary": _SURVIVAL_BOUNDARY,
            "answer": _unknown_survival_answer(
                name, f"匹配到多条：{'、'.join(candidates)}", level=level,
                uber=uber, pool=pool, hit_count=hit_count
            ),
        })

    row = rows[0]
    mults = json.loads(row["damage_mults"])
    unknown_reason = ""
    if str(row["damage_type"]) == "DamageOverTime":
        unknown_reason = "该预设是持续伤害，不能用单次命中 Max Hit 判断"
    elif len(mults) != 1:
        unknown_reason = "该预设含多个伤害类型，当前闭式只支持单一伤害类型且不做转化"
    elif float(row["crit_chance"] or 0) > 0:
        unknown_reason = "该预设有非零暴击率，当前没有足够字段计算暴击命中区间"

    if unknown_reason:
        answer = _unknown_survival_answer(
            str(row["name"]), unknown_reason, level=level, uber=uber, pool=pool,
            hit_count=hit_count
        )
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "assessable": False,
                "verdict": "unknown",
                "skill": str(row["name"]),
                "hit_count": hit_count,
                "reason": unknown_reason,
                "boundary": _SURVIVAL_BOUNDARY,
                "answer": answer,
            },
            citations=[_citation(row, answer)],
            meta={"channel": "derived", "table": "pob_boss_skill", "verdict": "unknown"},
        )

    dtype, pair = next(iter(mults.items()))
    resist_by_type = {
        "Fire": fire_resist_pct,
        "Cold": cold_resist_pct,
        "Lightning": lightning_resist_pct,
        "Chaos": chaos_resist_pct,
        "Physical": 0.0,
    }
    if dtype not in resist_by_type:
        unknown_reason = f"当前不支持伤害类型 {dtype}"
    elif resist_by_type[dtype] is None:
        unknown_reason = f"缺少{_DAMAGE_TYPE_ZH.get(dtype, dtype)}抗性，不能替用户假定"
    if unknown_reason:
        answer = _unknown_survival_answer(
            str(row["name"]), unknown_reason, level=level, uber=uber, pool=pool,
            hit_count=hit_count
        )
        return ToolResult(
            ok=True,
            data={
                "found": True, "assessable": False, "verdict": "unknown",
                "skill": str(row["name"]), "reason": unknown_reason,
                "hit_count": hit_count,
                "missing_inputs": [f"{_DAMAGE_TYPE_ZH.get(dtype, dtype)}抗性"],
                "boundary": _SURVIVAL_BOUNDARY, "answer": answer,
            },
            citations=[_citation(row, answer)],
            meta={"channel": "derived", "table": "pob_boss_skill", "verdict": "unknown"},
        )

    try:
        uber_mult = row["uber_damage_mult"] if uber else None
        if roll_range is None:
            low, high = boss_damage.hit_range(
                level=int(level), base_mult=float(pair[0]), roll_mult=float(pair[1]),
                damage_type=dtype, uber_mult=uber_mult,
            )
        else:
            hit = boss_damage.hit_damage(
                level=int(level), base_mult=float(pair[0]), roll_mult=float(pair[1]),
                damage_type=dtype, roll_range=float(roll_range), uber_mult=uber_mult,
            )
            low = high = hit
        penetrations = json.loads(
            row["uber_penetrations"] if uber else row["penetrations"]
        )
        penetration = float(penetrations.get(f"{dtype}Pen") or 0)
        result = boss_survival.assess_repeated_hits(
            damage_type=dtype,
            raw_low=low.damage,
            raw_high=high.damage,
            pool=float(pool),
            hit_count=hit_count,
            resist_pct=float(resist_by_type[dtype] or 0),
            penetration_pct=penetration,
            armour=float(armour),
            flat_physical_reduction_pct=float(flat_physical_reduction_pct),
            taken_mult=float(taken_mult),
            overwhelm_pct=float(overwhelm_pct),
        )
    except (
        boss_damage.BossDamageError,
        boss_survival.BossSurvivalError,
        constants.MissingConstant,
        constants.ConstantTypeError,
    ) as exc:
        return ToolResult(ok=False, error=str(exc))

    answer = _render_survival(
        row, result, level=int(level), uber=bool(uber), roll_range=roll_range,
        armour=float(armour),
        flat_physical_reduction_pct=float(flat_physical_reduction_pct),
        taken_mult=float(taken_mult),
    )
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "assessable": True,
            "verdict": result.outcome,
            "skill": str(row["name"]),
            "level": int(level),
            "uber": bool(uber),
            "roll_range": roll_range,
            **result.as_dict(),
            "boundary": _SURVIVAL_BOUNDARY,
            "answer": answer,
        },
        citations=[_citation(row, answer), _maxhit_citation(answer)],
        meta={
            "channel": "derived",
            "tables": ["pob_boss_skill", "game_constant"],
            "verdict": result.outcome,
        },
    )


@REGISTRY.register(
    name="boss_defence_threshold",
    description=(
        "规划承受 PoB Boss 技能同一命中 1..100 次所需的防御门槛。"
        "不给 target_pool 时，按明确提供的抗性、护甲和承受倍率回答至少需要多少整数防御池；"
        "给 target_pool 时，还会计算当前防御池缺口，并反解覆盖完整伤害掷骰区间所需的"
        "穿透前元素/混沌抗性或物理护甲。超过 90% 抗性或物理减伤上限时明确返回不可达。"
        "只计算同一技能每次都命中且期间无恢复的局部门槛，不计算真实连招、概率、恢复、"
        "阶段、药剂或打法。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string", "minLength": 2,
                "description": "PoB Boss 技能英文名，如 Shaper Ball、Maven Fireball。",
            },
            "level": {"type": "integer", "minimum": 1, "maximum": 100},
            "target_pool": {
                "type": "number", "exclusiveMinimum": 0,
                "description": "可选目标生命 + 能量护盾池；提供后才反解抗性/护甲门槛。",
            },
            "hit_count": {
                "type": "integer", "minimum": 1, "maximum": 100, "default": 1,
                "description": "同一技能连续命中次数；假设每次命中且期间无恢复。",
            },
            "uber": {"type": "boolean", "default": False},
            "roll_range": {"type": "number", "minimum": 0, "maximum": 100},
            "fire_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "cold_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "lightning_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "chaos_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "armour": {"type": "number", "minimum": 0, "default": 0},
            "flat_physical_reduction_pct": {
                "type": "number", "minimum": 0, "maximum": 90, "default": 0,
            },
            "taken_mult": {"type": "number", "exclusiveMinimum": 0, "default": 1},
            "overwhelm_pct": {
                "type": "number", "minimum": 0, "maximum": 100, "default": 0,
            },
        },
        "required": ["skill", "level"],
        "additionalProperties": False,
    },
)
def boss_defence_threshold(
    skill: str,
    level: int,
    target_pool: float | None = None,
    hit_count: int = 1,
    uber: bool = False,
    roll_range: float | None = None,
    fire_resist_pct: float | None = None,
    cold_resist_pct: float | None = None,
    lightning_resist_pct: float | None = None,
    chaos_resist_pct: float | None = None,
    armour: float = 0.0,
    flat_physical_reduction_pct: float = 0.0,
    taken_mult: float = 1.0,
    overwhelm_pct: float = 0.0,
) -> ToolResult:
    """复用同一伤害/防御闭式，提供面向规划的稳定工具契约。"""
    if target_pool is not None and target_pool <= 0:
        return ToolResult(ok=False, error="target_pool 必须大于 0")
    calculation_pool = float(target_pool) if target_pool is not None else 1.0
    result = boss_survival_check(
        skill=skill,
        level=level,
        pool=calculation_pool,
        hit_count=hit_count,
        uber=uber,
        roll_range=roll_range,
        fire_resist_pct=fire_resist_pct,
        cold_resist_pct=cold_resist_pct,
        lightning_resist_pct=lightning_resist_pct,
        chaos_resist_pct=chaos_resist_pct,
        armour=armour,
        flat_physical_reduction_pct=flat_physical_reduction_pct,
        taken_mult=taken_mult,
        overwhelm_pct=overwhelm_pct,
    )
    if not result.ok:
        return result

    data = dict(result.data or {})
    data["target_pool"] = target_pool
    if not data.get("assessable"):
        if target_pool is None:
            data["pool"] = None
            reason = str(data.get("reason") or "缺少安全计算所需输入")
            data["answer"] = _unknown_survival_answer(
                str(data.get("skill") or skill), reason, level=level, uber=uber,
                pool=None, hit_count=hit_count,
            ).replace("生存校验", "防御门槛")
        return ToolResult(ok=True, data=data, citations=result.citations, meta=result.meta)

    rows = _rows(dbmod.get_conn(), str(data["skill"]))
    if len(rows) != 1:  # 正常路径已唯一命中；保留防御性边界。
        return ToolResult(ok=False, error="Boss 技能门槛结果无法重新定位唯一预设")
    data["answer"] = _render_defence_threshold(
        rows[0], data, level=int(level), uber=bool(uber), roll_range=roll_range,
        target_pool=target_pool, armour=float(armour),
        flat_physical_reduction_pct=float(flat_physical_reduction_pct),
        taken_mult=float(taken_mult),
    )
    if target_pool is None:
        data.update({
            "pool": None,
            "verdict": None,
            "outcome": None,
            "remaining_low": None,
            "remaining_high": None,
            "margin_low": None,
            "margin_high": None,
            "max_raw_hit": None,
            "earliest_depletion_hit": None,
            "latest_depletion_hit": None,
            "guaranteed_survivable_hits": None,
            "maximum_survivable_hits": None,
            "pool_shortfall_for_any_roll": None,
            "pool_shortfall_for_full_range": None,
            "required_resist_for_full_range_pct": None,
            "resist_shortfall_pct": None,
            "required_resist_status": (
                "not_applicable" if data["damage_type"] == "Physical"
                else "not_requested_without_target_pool"
            ),
            "required_armour_for_full_range": None,
            "armour_shortfall": None,
            "required_armour_status": (
                "not_requested_without_target_pool" if data["damage_type"] == "Physical"
                else "not_applicable"
            ),
            "capacity_reason": (
                "未提供目标防御池，因此不计算当前池子的可承受击数或原始 Max Hit"
            ),
        })
    meta = dict(result.meta)
    meta["mode"] = "defence_threshold"
    answer = str(data["answer"])
    return ToolResult(
        ok=True,
        data=data,
        citations=[_citation(rows[0], answer), _maxhit_citation(answer)],
        meta=meta,
    )


def _render_boss_sequence(
    result: boss_survival.BossSequenceResult,
    *,
    level: int,
    uber: bool,
    roll_range: float | None,
) -> str:
    labels = {
        "survives_full_range": "整个显式序列在全部已建模掷骰下都不会耗尽池子",
        "roll_dependent": "序列是否耗尽池子取决于各次伤害掷骰",
        "fails_full_range": "整个显式序列在全部已建模掷骰下都会耗尽池子",
    }
    roll_note = (
        f"每击固定 {roll_range:g}% 掷骰点"
        if roll_range is not None else "每击分别采用 0%..100% 掷骰区间"
    )
    lines = [
        "### Boss 显式命中序列校验" + ("（Uber）" if uber else ""),
        "",
        f"**结论：{labels[result.outcome]}**",
        f"- 口径：敌人等级 {level}；{roll_note}；防御池 {result.pool:g}",
        f"- 总承伤区间：{result.total_taken_low:,.2f} — "
        f"{result.total_taken_high:,.2f}",
        f"- 序列结束后的池子余量：最好 {result.remaining_best_case:,.2f}；"
        f"最坏 {result.remaining_worst_case:,.2f}",
        f"- 所需防御池：至少覆盖一种掷骰 {result.minimum_pool_for_any_roll:,}；"
        f"覆盖完整区间 {result.minimum_pool_for_full_range:,}",
        f"- 最危险一击：第 {result.most_dangerous_hit_index} 击 "
        f"{result.most_dangerous_skill}（"
        f"{_DAMAGE_TYPE_ZH[result.most_dangerous_damage_type]}），承伤 "
        f"{result.largest_single_hit_taken_low:,.2f} — "
        f"{result.largest_single_hit_taken_high:,.2f}",
    ]
    if result.earliest_possible_depletion_hit is None:
        lines.append("- 耗尽位置：最坏掷骰下完成整个序列后仍未耗尽")
    else:
        lines.append(
            f"- 最早可能耗尽：第 {result.earliest_possible_depletion_hit} 击"
        )
    if result.guaranteed_depletion_hit is None:
        lines.append("- 必然耗尽位置：最低掷骰下完成整个序列后仍未耗尽")
    else:
        lines.append(f"- 最迟必然耗尽：第 {result.guaranteed_depletion_hit} 击")
    lines += ["", "按伤害类型汇总："]
    for item in result.damage_type_totals:
        lines.append(
            f"- {_DAMAGE_TYPE_ZH[item.damage_type]}（{item.hit_count} 击）："
            f"原始 {item.raw_low:,.2f}–{item.raw_high:,.2f}；"
            f"承伤 {item.taken_low:,.2f}–{item.taken_high:,.2f}"
        )
    lines += [
        "",
        "| # | 技能 | 类型 | 本击承伤区间 | 累计承伤区间 | 最坏余量 |",
        "| ---: | --- | --- | ---: | ---: | ---: |",
    ]
    for step in result.steps:
        lines.append(
            f"| {step.hit_index} | {step.skill} | {_DAMAGE_TYPE_ZH[step.damage_type]} | "
            f"{step.taken_low:,.2f}–{step.taken_high:,.2f} | "
            f"{step.cumulative_taken_low:,.2f}–{step.cumulative_taken_high:,.2f} | "
            f"{step.remaining_worst_case:,.2f} |"
        )
    lines += [
        "",
        "> 顺序与次数完全来自请求；系统没有声称这是 Boss 的真实连招。每击都视为命中，"
        "期间无恢复，累计承伤等于池子时即耗尽。",
        f"> 边界：{_SEQUENCE_BOUNDARY}。",
    ]
    return "\n".join(lines)


@REGISTRY.register(
    name="boss_sequence_check",
    description=(
        "校验用户显式给出的多个 PoB Boss 技能命中序列，按顺序和次数逐击累计承伤，"
        "返回每一步的承伤/剩余池子、最早可能与最迟必然耗尽位置，以及全序列三态结论。"
        "序列只是用户指定的假设，不代表游戏中的真实连招；不计算命中概率、格挡、闪避、"
        "恢复、阶段、药剂或打法。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "sequence": {
                "type": "array", "minItems": 1, "maxItems": 100,
                "items": {
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string", "minLength": 2},
                        "count": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["skill"],
                    "additionalProperties": False,
                },
                "description": "按发生顺序排列的技能；count 可压缩连续重复命中。",
            },
            "level": {"type": "integer", "minimum": 1, "maximum": 100},
            "pool": {"type": "number", "exclusiveMinimum": 0},
            "uber": {"type": "boolean", "default": False},
            "roll_range": {"type": "number", "minimum": 0, "maximum": 100},
            "fire_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "cold_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "lightning_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "chaos_resist_pct": {"type": "number", "minimum": -200, "maximum": 90},
            "armour": {"type": "number", "minimum": 0, "default": 0},
            "flat_physical_reduction_pct": {
                "type": "number", "minimum": 0, "maximum": 90, "default": 0,
            },
            "taken_mult": {"type": "number", "exclusiveMinimum": 0, "default": 1},
            "overwhelm_pct": {
                "type": "number", "minimum": 0, "maximum": 100, "default": 0,
            },
        },
        "required": ["sequence", "level", "pool"],
        "additionalProperties": False,
    },
)
def boss_sequence_check(
    sequence: list[dict[str, Any]],
    level: int,
    pool: float,
    uber: bool = False,
    roll_range: float | None = None,
    fire_resist_pct: float | None = None,
    cold_resist_pct: float | None = None,
    lightning_resist_pct: float | None = None,
    chaos_resist_pct: float | None = None,
    armour: float = 0.0,
    flat_physical_reduction_pct: float = 0.0,
    taken_mult: float = 1.0,
    overwhelm_pct: float = 0.0,
) -> ToolResult:
    if pool <= 0:
        return ToolResult(ok=False, error="pool 必须大于 0")
    if not isinstance(sequence, list) or not sequence:
        return ToolResult(ok=False, error="sequence 必须是非空数组")

    expanded_count = 0
    parsed: list[tuple[sqlite3.Row, str, float, float, float]] = []
    resist_by_type = {
        "Fire": fire_resist_pct,
        "Cold": cold_resist_pct,
        "Lightning": lightning_resist_pct,
        "Chaos": chaos_resist_pct,
        "Physical": 0.0,
    }
    for step_index, item in enumerate(sequence, start=1):
        if not isinstance(item, dict):
            return ToolResult(ok=False, error=f"sequence[{step_index - 1}] 必须是对象")
        unknown_keys = set(item) - {"skill", "count"}
        if unknown_keys:
            return ToolResult(
                ok=False,
                error=f"sequence[{step_index - 1}] 包含未知字段：{', '.join(sorted(unknown_keys))}",
            )
        name = str(item.get("skill") or "").strip()
        count = item.get("count", 1)
        if len(name) < 2:
            return ToolResult(ok=False, error=f"sequence[{step_index - 1}].skill 至少需要两个字符")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
            return ToolResult(ok=False, error=f"sequence[{step_index - 1}].count 必须是 1..100 的整数")
        expanded_count += count
        if expanded_count > 100:
            return ToolResult(ok=False, error="展开后的命中序列最多包含 100 次命中")
        rows = _rows(dbmod.get_conn(), name)
        if not rows:
            reason = f"第 {step_index} 段没有匹配的 PoB Boss 技能预设「{name}」"
            return ToolResult(ok=True, data={
                "found": False, "assessable": False, "verdict": "unknown",
                "level": level, "uber": uber, "pool": pool,
                "requested_sequence": sequence, "failed_sequence_index": step_index,
                "reason": reason, "boundary": _SEQUENCE_BOUNDARY,
                "answer": f"### Boss 显式命中序列校验\n\n**结论：无法安全计算**\n- 原因：{reason}\n\n> 边界：{_SEQUENCE_BOUNDARY}。",
            })
        if len(rows) > 1:
            candidates = [str(row["name"]) for row in rows]
            reason = f"第 {step_index} 段「{name}」匹配到多条，请指明技能"
            return ToolResult(ok=True, data={
                "found": False, "assessable": False, "verdict": "unknown",
                "level": level, "uber": uber, "pool": pool,
                "requested_sequence": sequence, "failed_sequence_index": step_index,
                "candidates": candidates, "reason": reason,
                "boundary": _SEQUENCE_BOUNDARY,
                "answer": f"### Boss 显式命中序列校验\n\n**结论：无法安全计算**\n- 原因：{reason}\n\n> 边界：{_SEQUENCE_BOUNDARY}。",
            })
        row = rows[0]
        mults = json.loads(row["damage_mults"])
        if str(row["damage_type"]) == "DamageOverTime":
            reason = f"第 {step_index} 段 {row['name']} 是持续伤害，不是单次命中"
        elif len(mults) != 1:
            reason = f"第 {step_index} 段 {row['name']} 含多个伤害类型，当前不做伤害转化"
        elif float(row["crit_chance"] or 0) > 0:
            reason = f"第 {step_index} 段 {row['name']} 有非零暴击率"
        else:
            reason = ""
        if reason:
            answer = (
                "### Boss 显式命中序列校验\n\n**结论：无法安全计算**\n"
                f"- 原因：{reason}\n\n> 边界：{_SEQUENCE_BOUNDARY}。"
            )
            return ToolResult(ok=True, data={
                "found": True, "assessable": False, "verdict": "unknown",
                "level": level, "uber": uber, "pool": pool,
                "requested_sequence": sequence, "failed_sequence_index": step_index,
                "reason": reason, "boundary": _SEQUENCE_BOUNDARY, "answer": answer,
            }, citations=[_citation(row, answer)])
        dtype, pair = next(iter(mults.items()))
        if dtype not in resist_by_type:
            reason = f"第 {step_index} 段使用了当前不支持的伤害类型 {dtype}"
        elif resist_by_type[dtype] is None:
            reason = f"第 {step_index} 段缺少{_DAMAGE_TYPE_ZH.get(dtype, dtype)}抗性"
        else:
            reason = ""
        if reason:
            answer = (
                "### Boss 显式命中序列校验\n\n**结论：无法安全计算**\n"
                f"- 原因：{reason}\n\n> 边界：{_SEQUENCE_BOUNDARY}。"
            )
            return ToolResult(ok=True, data={
                "found": True, "assessable": False, "verdict": "unknown",
                "level": level, "uber": uber, "pool": pool,
                "requested_sequence": sequence, "failed_sequence_index": step_index,
                "missing_inputs": [f"{_DAMAGE_TYPE_ZH.get(dtype, dtype)}抗性"],
                "reason": reason, "boundary": _SEQUENCE_BOUNDARY, "answer": answer,
            }, citations=[_citation(row, answer)])
        try:
            uber_mult = row["uber_damage_mult"] if uber else None
            if roll_range is None:
                low, high = boss_damage.hit_range(
                    level=int(level), base_mult=float(pair[0]), roll_mult=float(pair[1]),
                    damage_type=dtype, uber_mult=uber_mult,
                )
            else:
                hit = boss_damage.hit_damage(
                    level=int(level), base_mult=float(pair[0]), roll_mult=float(pair[1]),
                    damage_type=dtype, roll_range=float(roll_range), uber_mult=uber_mult,
                )
                low = high = hit
        except (boss_damage.BossDamageError, constants.MissingConstant,
                constants.ConstantTypeError) as exc:
            return ToolResult(ok=False, error=str(exc))
        penetrations = json.loads(row["uber_penetrations"] if uber else row["penetrations"])
        penetration = float(penetrations.get(f"{dtype}Pen") or 0)
        parsed.extend(
            (row, dtype, low.damage, high.damage, penetration) for _ in range(count)
        )

    hits = [
        boss_survival.BossSequenceHit(
            skill=str(row["name"]), damage_type=dtype, raw_low=raw_low,
            raw_high=raw_high, resist_pct=float(resist_by_type[dtype] or 0),
            penetration_pct=penetration, armour=float(armour),
            flat_physical_reduction_pct=float(flat_physical_reduction_pct),
            taken_mult=float(taken_mult), overwhelm_pct=float(overwhelm_pct),
        )
        for row, dtype, raw_low, raw_high, penetration in parsed
    ]
    try:
        assessed = boss_survival.assess_hit_sequence(hits=hits, pool=float(pool))
    except (boss_survival.BossSurvivalError, constants.MissingConstant,
            constants.ConstantTypeError) as exc:
        return ToolResult(ok=False, error=str(exc))
    answer = _render_boss_sequence(
        assessed, level=int(level), uber=bool(uber), roll_range=roll_range,
    )
    unique_rows: dict[str, sqlite3.Row] = {}
    for row, *_rest in parsed:
        unique_rows[str(row["name"])] = row
    citations = [_citation(row, answer) for row in unique_rows.values()]
    citations.append(_maxhit_citation(answer))
    return ToolResult(ok=True, data={
        "found": True,
        "assessable": True,
        "verdict": assessed.outcome,
        "level": int(level),
        "uber": bool(uber),
        "roll_range": roll_range,
        "requested_sequence": sequence,
        **assessed.as_dict(),
        "missing_inputs": [],
        "candidates": [],
        "boundary": _SEQUENCE_BOUNDARY,
        "answer": answer,
    }, citations=citations, meta={
        "channel": "derived",
        "tables": ["pob_boss_skill", "game_constant"],
        "verdict": assessed.outcome,
        "mode": "explicit_hit_sequence",
    })


def boss_sequence_from_build(
    build: "PobBuild",
    *,
    sequence: list[dict[str, Any]],
    level: int,
    uber: bool = False,
    roll_range: float | None = None,
    pool_mode: BuildPoolMode = "life_and_energy_shield",
) -> ToolResult:
    """用 PoB PlayerStat 基础字段校验用户给出的混合 Boss 命中序列。

    混合/多击序列绝不消费 ``*MaximumHitTaken``：它只代表一个特定 PoB 配置下的
    单击阈值，不能逐击拆回不同伤害类型。缺少任一实际需要的抗性、护甲或防御池字段时，
    整个序列保持 unknown。
    """
    if pool_mode not in {"life", "life_and_energy_shield"}:
        return ToolResult(ok=False, error="pool_mode 只支持 life 或 life_and_energy_shield")

    # 先用中性防御输入复用工具自身的序列结构、技能唯一性与机制边界校验；这里的
    # 数值结果不会对外返回，只用来取得实际出现的伤害类型和可靠引用。
    preflight = boss_sequence_check(
        sequence=sequence, level=level, pool=1, uber=uber,
        roll_range=roll_range, fire_resist_pct=0, cold_resist_pct=0,
        lightning_resist_pct=0, chaos_resist_pct=0, armour=0,
    )
    if not preflight.ok:
        return preflight
    max_hit_reason = (
        "PoB *MaximumHitTaken 是特定配置的单击阈值，不能拆分为混合/多击序列的逐击承伤"
    )
    if not (preflight.data or {}).get("assessable"):
        data = dict(preflight.data or {})
        data.update({
            "pool": None,
            "pool_mode": pool_mode,
            "input_source": "pob_player_stat",
            "source_fields": {},
            "source_config": {},
            "calculation_mode": "unknown",
            "fallback_reason": max_hit_reason,
            "boundary": f"{data['boundary']}；{_BUILD_SNAPSHOT_BOUNDARY}",
            "answer": f"{data['answer']}\n> Build 快照边界：{_BUILD_SNAPSHOT_BOUNDARY}",
        })
        return ToolResult(
            ok=True, data=data, citations=preflight.citations, meta=preflight.meta
        )

    damage_types = {
        str(item["damage_type"])
        for item in (preflight.data or {}).get("damage_type_totals", [])
    }
    source_fields: dict[str, float] = {}
    missing: list[str] = []
    life_key = "LifeUnreserved" if "LifeUnreserved" in build.player_stats else "Life"
    life = build.player_stats.get(life_key)
    if life is None:
        missing.append("Life（或 LifeUnreserved）")
    elif not math.isfinite(life) or life <= 0:
        missing.append(f"有效的 {life_key}")
    else:
        source_fields[life_key] = float(life)

    energy_shield = 0.0
    if pool_mode == "life_and_energy_shield":
        value = build.player_stats.get("EnergyShield")
        if value is None:
            missing.append("EnergyShield")
        elif not math.isfinite(value) or value < 0:
            missing.append("有效的 EnergyShield")
        else:
            energy_shield = float(value)
            source_fields["EnergyShield"] = energy_shield

    resist_keys = {
        "Fire": "FireResist", "Cold": "ColdResist",
        "Lightning": "LightningResist", "Chaos": "ChaosResist",
    }
    resists: dict[str, float | None] = {
        "Fire": None, "Cold": None, "Lightning": None, "Chaos": None,
    }
    for dtype in ("Fire", "Cold", "Lightning", "Chaos"):
        if dtype not in damage_types:
            continue
        key = resist_keys[dtype]
        value = build.player_stats.get(key)
        if value is None:
            missing.append(key)
        elif not math.isfinite(value) or not -200 <= value <= 90:
            missing.append(f"有效的 {key}")
        else:
            resists[dtype] = float(value)
            source_fields[key] = float(value)

    armour = 0.0
    if "Physical" in damage_types:
        value = build.player_stats.get("Armour")
        if value is None:
            missing.append("Armour")
        elif not math.isfinite(value) or value < 0:
            missing.append("有效的 Armour")
        else:
            armour = float(value)
            source_fields["Armour"] = armour

    pool = float(life) + energy_shield if life is not None and math.isfinite(life) else None
    if "Chaos" in damage_types and energy_shield > 0:
        missing.append("混沌伤害是否绕过能量护盾的 Build 机制")
    if missing or pool is None or pool <= 0:
        reason = "Build 快照缺少完整序列安全计算所需字段或机制"
        answer = "\n".join([
            "### Build 的 Boss 显式命中序列校验", "",
            "**结论：无法安全计算**", f"- 原因：{reason}",
            f"- 缺少：{'、'.join(missing)}", "",
            f"> {_SEQUENCE_BOUNDARY}。", f"> Build 快照边界：{_BUILD_SNAPSHOT_BOUNDARY}",
            f"> 未采用 PoB Max Hit：{max_hit_reason}。",
        ])
        return ToolResult(ok=True, data={
            "found": True, "assessable": False, "verdict": "unknown",
            "level": int(level), "uber": bool(uber), "roll_range": roll_range,
            "pool": pool, "pool_mode": pool_mode,
            "hit_count": int((preflight.data or {})["hit_count"]),
            "requested_sequence": sequence, "steps": [], "damage_type_totals": [],
            "missing_inputs": missing, "candidates": [], "reason": reason,
            "input_source": "pob_player_stat", "source_fields": source_fields,
            "source_config": {}, "calculation_mode": "unknown",
            "fallback_reason": max_hit_reason,
            "boundary": f"{_SEQUENCE_BOUNDARY}；{_BUILD_SNAPSHOT_BOUNDARY}",
            "answer": answer,
        }, citations=preflight.citations, meta={
            "channel": "derived", "input_source": "pob_player_stat",
            "verdict": "unknown", "mode": "build_explicit_hit_sequence",
        })

    checked = boss_sequence_check(
        sequence=sequence, level=level, pool=pool, uber=uber,
        roll_range=roll_range, fire_resist_pct=resists["Fire"],
        cold_resist_pct=resists["Cold"],
        lightning_resist_pct=resists["Lightning"],
        chaos_resist_pct=resists["Chaos"], armour=armour,
    )
    if not checked.ok:
        return checked
    data = dict(checked.data or {})
    data.update({
        "pool_mode": pool_mode,
        "input_source": "pob_player_stat",
        "source_fields": source_fields,
        "source_config": {},
        "calculation_mode": "basic_defence_sequence",
        "fallback_reason": max_hit_reason,
        "boundary": f"{data['boundary']}；{_BUILD_SNAPSHOT_BOUNDARY}",
        "answer": (
            f"{data['answer']}\n> Build 快照输入：{', '.join(source_fields)}；"
            f"{_BUILD_SNAPSHOT_BOUNDARY}\n> 未采用 PoB Max Hit：{max_hit_reason}。"
        ),
    })
    meta = dict(checked.meta)
    meta.update({
        "input_source": "pob_player_stat", "mode": "build_explicit_hit_sequence",
    })
    return ToolResult(ok=True, data=data, citations=checked.citations, meta=meta)
