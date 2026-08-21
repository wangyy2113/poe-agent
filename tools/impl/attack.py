"""攻击侧两个结构化工具：命中率与攻击速度。

与 `tools/impl/defence.py` 的分工对称——那边算「怪物打我」，这边算「我打怪物」，
命中率用的是**同一个** `calcs.hitChance`（判据见 `calc/accuracy.py` 的模块说明）。

两条纪律与其它计算器一致：

* **缺输入就说缺什么，不替用户假定**。没给敌方等级就不猜 83 级——「打谁」这个前提
  决定了闪避有多高（83 级普通怪 7,860，等级差一档结果就变）。
* **算式连同中间值一起给**。数值回填校验要在证据里找到每一个数，所以逐步推导表
  是证据本体，不是装饰。
"""

from __future__ import annotations

from typing import Any

from calc import accuracy as acc
from calc import attack_speed as spd
from calc import constants, mitigation
from calc.derive import fmt, pob_url, render_steps
from core import db as dbmod
from entities.taxonomy import Source
from tools.registry import REGISTRY, ToolResult

#: 引用里给出的 PoB 原文锚点（钉 commit 由 `calc.derive.pob_url` 负责）。
_ACCURACY_BOUNDARY = (
    "命中率只按**普通怪物**的等级闪避表算；传奇/终局头目的闪避倍率是 PoB 运行时"
    "从头目数据算的平均值，本库未采集。也不判断哪些技能「无法被闪避」"
    "（那一维未采集，要由你说明）。命中率是 DPS 的一个乘数，本工具不给 DPS。"
)
_SPEED_BOUNDARY = (
    "只算「武器基础攻速 × 技能倍率 × 攻速加成」这一支：不含双持、行动速度"
    "（冰缓/致盲）、技能覆盖攻击时间与定值加时，也不给 DPS。"
    "PoB 对每秒次数**不设上限**（服务器 tick 只出现在创伤叠层那一段）。"
)


def _rules_citation(text: str, *, url: str, title: str) -> dict[str, Any]:
    return {
        "chunk_id": f"attack_rules:{title}",
        "entity_id": None,
        "title": title,
        "url": url,
        "source": Source.POB.value,
        "source_priority": 2,
        "can_support_facts": True,
        "text": text,
        "channel": "derived",
    }


def _accuracy_url() -> str:
    """钉住 commit 的原文锚点：从常量的引用地址推出同一 commit 下的公式文件。"""
    return pob_url(
        constants.provenance(constants.MONSTER_EVASION_TABLE).url,
        acc.OFFENCE_FILE, lines=acc.HIT_CHANCE_LINES,
    )


def _speed_url() -> str:
    return pob_url(
        constants.provenance(constants.MONSTER_EVASION_TABLE).url,
        spd.OFFENCE_FILE, lines=spd.BASE_TIME_LINES,
    )


@REGISTRY.register(
    name="accuracy_calc",
    description=(
        "算**命中率**：给命中值与敌方等级（或闪避值），按 PoB 原式复算"
        "「命中值 /(命中值 +(闪避 / 5) ^ 0.9)× 125」，夹在 5%..100%，再扣敌方格挡。"
        "也能**反解**：要达到某个命中率最少需要多少命中值（填 target_chance）。"
        "问「我 X 命中打 Y 级怪多少命中率 / 会不会 miss / 要多少命中才够」时用它。\n"
        "敌方闪避由怪物等级表查（用了哪一级会写在推导里）；**没给等级也没给闪避时"
        "不会假定一个**。不提供 DPS。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "accuracy": {"type": "number", "description": "我的命中值（角色面板上的数）"},
            "enemy_level": {
                "type": "integer",
                "description": "敌方等级（1-100），用于查怪物闪避表。地图常见 83",
            },
            "enemy_evasion": {"type": "number", "description": "直接给敌方闪避值"},
            "enemy_block_pct": {"type": "number", "description": "敌方格挡几率（%）"},
            "target_chance": {
                "type": "integer",
                "description": "反解用：想达到的命中率（%），如 90",
            },
            "cannot_be_evaded": {
                "type": "boolean",
                "description": "技能是否「无法被闪避」；本库不判断，由你按题面说明",
            },
        },
    },
)
def accuracy_calc(
    accuracy: float | None = None,
    enemy_level: int | None = None,
    enemy_evasion: float | None = None,
    enemy_block_pct: float | None = None,
    target_chance: int | None = None,
    cannot_be_evaded: bool = False,
) -> ToolResult:
    try:
        constants.table(constants.MONSTER_EVASION_TABLE)
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"怪物闪避表未入库：{exc}")

    if accuracy is None and target_chance is None:
        text = (
            "## 命中率怎么算（PoB 原式）\n\n"
            "命中率 = 取整(命中值 /(命中值 +(敌方闪避 / 5) ^ 0.9)× 125)，"
            f"夹在 {mitigation.HIT_CHANCE_FLOOR}%..100%。\n\n"
            "- **命中值堆到多少才够看敌方闪避**：83 级普通怪的闪避是 "
            f"{fmt(acc.monster_evasion(83))}，此时约 1,899 命中值达到 90%。\n"
            "- **下限 5%**：命中值再低也有 5% 打中（与闪避那一侧的上限 95% 对称）。\n"
            "- **敌方格挡是另一层**：最终命中率 = 精准命中率 ×(1 − 格挡%)，"
            "挡下来的那部分打不出伤害。\n\n"
            f"{_ACCURACY_BOUNDARY}"
        )
        return ToolResult(
            ok=True,
            data={"found": True, "kind": "rules", "boundary": _ACCURACY_BOUNDARY},
            citations=[_rules_citation(text, url=_accuracy_url(), title="命中率结算规则")],
            meta={"channel": "derived"},
        )

    if enemy_level is None and enemy_evasion is None:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "kind": "missing_inputs",
                "missing_inputs": ["敌方等级或敌方闪避值"],
                "reason": "命中率取决于打谁：83 级普通怪与 68 级的闪避差着几倍，"
                          "替你假定一个等级就是编造前提",
                "boundary": _ACCURACY_BOUNDARY,
            },
        )

    lines: list[str] = []
    data: dict[str, Any] = {"found": True, "boundary": _ACCURACY_BOUNDARY}
    try:
        if target_chance is not None:
            needed = acc.accuracy_for_chance(
                target_chance, enemy_level=enemy_level, enemy_evasion=enemy_evasion
            )
            lines.append(f"### 要达到 {needed.target_pct}% 命中率")
            lines.append("")
            lines.extend(render_steps(needed.steps))
            lines.append("")
            lines.append(
                f"**最少需要 {fmt(needed.accuracy)} 命中值**"
                f"（此时命中率 {needed.achieved_pct}%）。"
            )
            data.update({
                "kind": "accuracy_needed",
                "target_chance": needed.target_pct,
                "accuracy_needed": needed.accuracy,
                "achieved_chance": needed.achieved_pct,
                "enemy_evasion": needed.enemy_evasion,
                "enemy_level": needed.enemy_level,
            })
        if accuracy is not None:
            result = acc.hit_chance_vs_enemy(
                accuracy,
                enemy_level=enemy_level,
                enemy_evasion=enemy_evasion,
                enemy_block_pct=enemy_block_pct or 0.0,
                cannot_be_evaded=cannot_be_evaded,
            )
            if lines:
                lines.append("")
            lines.append(f"### {fmt(accuracy)} 命中值的命中率")
            lines.append("")
            lines.extend(render_steps(result.steps))
            lines.append("")
            lines.append(
                f"**命中率 {result.hit_chance:g}%**（约 {result.miss_chance:g}% 打空）。"
            )
            if result.at_floor:
                lines.append(
                    f"命中率已被下限 {mitigation.HIT_CHANCE_FLOOR}% 夹住："
                    "再低的命中值也是这个数。"
                )
            if result.always_hits:
                lines.append("这一击必中（精准命中率已达 100%）。")
            data.update({
                "kind": data.get("kind", "hit_chance"),
                "accuracy": result.accuracy,
                "enemy_evasion": result.enemy_evasion,
                "enemy_level": result.enemy_level,
                "accuracy_hit_chance": result.accuracy_hit_chance,
                "hit_chance": result.hit_chance,
                "miss_chance": result.miss_chance,
                "enemy_block_pct": result.enemy_block_pct,
            })
    except (mitigation.OutOfTableRange, ValueError) as exc:
        return ToolResult(ok=False, error=str(exc))

    lines.append("")
    lines.append(_ACCURACY_BOUNDARY)
    return ToolResult(
        ok=True,
        data=data,
        citations=[_rules_citation("\n".join(lines), url=_accuracy_url(),
                                  title="命中率逐步推导")],
        meta={"channel": "derived"},
    )


def _weapon_rate(weapon: str) -> tuple[float | None, str, list[str]]:
    """按底材名查基础攻速。返回 (攻速, 用了哪件, 候选名)。"""
    conn = dbmod.get_conn()
    rows = conn.execute(
        "SELECT name_zh, name_en, json_extract(stats,'$.AttackRateBase') AS aps "
        "FROM pob_base_item WHERE (name_zh = ? OR name_en = ? COLLATE NOCASE "
        "  OR name_zh LIKE ? OR name_en LIKE ? COLLATE NOCASE) "
        "ORDER BY (name_zh = ?) DESC, length(name_zh) LIMIT 5",
        (weapon, weapon, f"%{weapon}%", f"%{weapon}%", weapon),
    ).fetchall()
    with_aps = [r for r in rows if r["aps"] is not None]
    if not with_aps:
        return None, "", [r["name_zh"] for r in rows]
    best = with_aps[0]
    others = [r["name_zh"] for r in with_aps[1:]]
    return float(best["aps"]), f"{best['name_zh']}（{best['name_en']}）", others


def _skill_multiplier(skill: str, gem_level: int | None) -> tuple[float | None, str]:
    """按技能名查 `attackSpeedMultiplier`（PoB 原值，-30 表示 70%）。"""
    conn = dbmod.get_conn()
    gem = conn.execute(
        "SELECT gem_id, name_zh, name_en, natural_max_level FROM pob_skill_gem "
        "WHERE name_zh = ? OR name_en = ? COLLATE NOCASE OR name_zh LIKE ? "
        "ORDER BY (name_zh = ?) DESC, length(name_zh) LIMIT 1",
        (skill, skill, f"%{skill}%", skill),
    ).fetchone()
    if gem is None:
        return None, ""
    level = gem_level or int(gem["natural_max_level"] or 20)
    row = conn.execute(
        "SELECT gem_level, attack_speed_multiplier FROM pob_skill_level "
        "WHERE gem_id = ? AND attack_speed_multiplier IS NOT NULL "
        "ORDER BY abs(gem_level - ?) LIMIT 1",
        (gem["gem_id"], level),
    ).fetchone()
    if row is None:
        return 0.0, f"{gem['name_zh']}（该技能没有攻速倍率，按 100% 算）"
    return float(row["attack_speed_multiplier"]), (
        f"{gem['name_zh']} 按 {int(row['gem_level'])} 级"
    )


@REGISTRY.register(
    name="attack_speed_calc",
    description=(
        "算**每秒攻击次数与攻击间隔**：武器基础攻速 ×(1 + 技能攻速倍率)，"
        "再除以 round((1 + 攻速加成%)× 更多倍率, 2)。武器基础攻速与技能倍率**按名字查库**"
        "（`pob_base_item` / `pob_skill_level`）。问「这把武器多快 / 加了 X% 攻速每秒几下 / "
        "攻击间隔多少」时用它。\n"
        "不含双持、行动速度与技能覆盖攻击时间；不提供 DPS。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "base_rate": {"type": "number", "description": "武器基础攻速（次/秒）"},
            "weapon": {"type": "string", "description": "武器底材名，用来查基础攻速"},
            "skill": {"type": "string", "description": "技能名，用来查技能攻速倍率"},
            "gem_level": {"type": "integer", "description": "技能等级，默认自然最高级"},
            "increased_pct": {"type": "number", "description": "攻击速度加快的加总（%）"},
            "more_mult": {"type": "number", "description": "「更多攻击速度」乘算，1.0 为无"},
        },
    },
)
def attack_speed_calc(
    base_rate: float | None = None,
    weapon: str = "",
    skill: str = "",
    gem_level: int | None = None,
    increased_pct: float | None = None,
    more_mult: float | None = None,
) -> ToolResult:
    sources: list[str] = []
    rate = base_rate
    if rate is None and weapon:
        rate, used, others = _weapon_rate(weapon)
        if rate is None:
            return ToolResult(
                ok=True,
                data={
                    "found": False,
                    "reason": f"底材表里没有「{weapon}」的基础攻速"
                              f"{'（它不是武器）' if others else ''}",
                    "candidates": others,
                    "boundary": _SPEED_BOUNDARY,
                },
            )
        sources.append(f"武器基础攻速取自底材 {used}")
        if others:
            sources.append(f"同名候选还有：{'、'.join(others[:3])}")

    if rate is None:
        text = (
            "## 攻击速度怎么算（PoB 原式）\n\n"
            "每秒次数 = 1 ÷(基础间隔 ÷ round((1 + 攻速加成% / 100)× 更多倍率, 2))，"
            "其中基础间隔 = 1 ÷(武器基础攻速 ×(1 + 技能攻速倍率 / 100))。\n\n"
            "- **技能攻速倍率乘在武器攻速上**，而 PoB 存的是增量（存 -30 表示 70%）。\n"
            "- **`round(…, 2)` 在分母上**：先把攻速修正四舍五入到两位小数，再去除间隔。\n"
            f"- {_SPEED_BOUNDARY}\n"
        )
        return ToolResult(
            ok=True,
            data={
                "found": False, "kind": "rules",
                "missing_inputs": ["武器基础攻速（给 base_rate 或武器名）"],
                "boundary": _SPEED_BOUNDARY,
            },
            citations=[_rules_citation(text, url=_speed_url(), title="攻击速度结算规则")],
            meta={"channel": "derived"},
        )

    multiplier = 0.0
    if skill:
        found, note = _skill_multiplier(skill, gem_level)
        if found is None:
            sources.append(f"宝石表里没有「{skill}」，技能倍率按 100% 算")
        else:
            multiplier = found
            sources.append(f"技能攻速倍率取自 {note}")

    try:
        result = spd.attack_rate(
            rate,
            skill_multiplier=multiplier,
            increased_pct=increased_pct or 0.0,
            more_mult=more_mult if more_mult is not None else 1.0,
        )
    except ValueError as exc:
        return ToolResult(ok=False, error=str(exc))

    lines = ["### 攻击速度", ""]
    lines.extend(render_steps(result.steps))
    lines.append("")
    lines.append(
        f"**每秒 {result.rate:g} 次攻击，间隔 {result.interval:g} 秒。**"
    )
    for note in sources:
        lines.append(f"- {note}")
    lines.append("")
    lines.append(_SPEED_BOUNDARY)
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "kind": "result",
            "base_rate": result.base_rate,
            "skill_multiplier": result.skill_multiplier,
            "increased_pct": result.increased_pct,
            "more_mult": result.more_mult,
            "rate": result.rate,
            "interval": result.interval,
            "sources": sources,
            "boundary": _SPEED_BOUNDARY,
        },
        citations=[_rules_citation("\n".join(lines), url=_speed_url(),
                                  title="攻击速度逐步推导")],
        meta={"channel": "derived"},
    )
