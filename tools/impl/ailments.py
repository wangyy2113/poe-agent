"""持续伤害计算：点燃 / 中毒 / 流血。

## 为什么值得一个工具

这三件事在社区里被记错的方式几乎一样：**把「每秒」记成「总共」**。
点燃是每秒 90% 火焰伤害、持续 4 秒——总共 360%。把它当成「总共 90%」，
结论会差 4 倍；反过来当成「每秒 90% 持续 4 秒 = 每秒 360%」也是错的。

工具因此逐段写出来：每秒多少 → 持续几秒 → 总共多少，再加上叠加规则与减伤口径。

## 一击有多大必须由问题给出

`pob_skill_level` 刻意没有伤害列（PoB 存的是乘数），所以「这一击打多少」不是查得到的
事实。没给伤害就只讲规则——不替用户编一个 10000。

## 三条容易记错的都写进证据

1. 点燃 90% / 中毒 30% / 流血 70% 都是**每秒**，持续 4 / 2 / 5 秒。
2. 叠加：中毒**无上限**（真实 DPS 取决于攻速），点燃与流血默认只有 1 层。
3. **流血不吃护甲**——物理持续伤害只被物理伤害减免削（上限 75%）。
"""

from __future__ import annotations

from typing import Any

from calc import ailment as ail
from calc import constants
from calc.derive import pob_url, render_steps
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 3200

_BOUNDARY = (
    "只按 PoB 的持续伤害公式复算：**这一击有多大由问题给出**（库里没有伤害数值，"
    "PoB 存的是乘数）；不含暴击对异常的加成、不含感电与冰冻"
    "（那是门槛型异常、公式不同），也不读取角色档案"
)


def _sources() -> list[str]:
    percent = constants.provenance(constants.IGNITE_PERCENT)
    url = percent.url

    def anchor(lines: tuple[int, int]) -> str:
        return pob_url(url, ail.OFFENCE_FILE, lines=lines)

    return [
        f"- 点燃：{anchor(ail.IGNITE_LINES)}",
        f"- 中毒：{anchor(ail.POISON_LINES)}",
        f"- 流血（含「护甲不参与」的减伤口径）：{anchor(ail.BLEED_LINES)}",
        f"- 「每秒」这个口径的出处是 PoB 自己的说明字符串"
        f"（`ignite deals 90% per second`）："
        f"{pob_url(url, ail.OFFENCE_FILE, line=ail.IGNITE_PER_SECOND_LINE)}",
        f"- 流血默认 1 层：{pob_url(url, 'src/Modules/CalcSetup.lua', line=ail.SETUP_STACK_LINE)}",
        f"- 各项基数：`pobMisc.*PercentBase` / `*DurationBase` / `DotDpsCap` {url}",
    ]


def _rules_table() -> list[str]:
    rows = ["| 异常 | 每秒占比 | 持续 | 总共 | 伤害类型 | 默认叠加 |",
            "| --- | --- | --- | --- | --- | --- |"]
    for zh, spec in ail.AILMENTS.items():
        raw = constants.get(str(spec["percent_key"]))
        percent = raw * 100 if spec["percent_is_fraction"] else raw
        duration = constants.get(str(spec["duration_key"]))
        stacks = "无上限" if spec["default_stacks"] is None else f"{spec['default_stacks']} 层"
        rows.append(
            f"| {zh}（{spec['en']}） | {percent:g}% | {duration:g} 秒 | "
            f"{percent * duration:g}% | {spec['damage_type']} | {stacks} |"
        )
    return rows


def _render_rules() -> str:
    return "\n".join([
        "### 持续伤害：点燃 / 中毒 / 流血", "",
        "三种都是「**每秒**造成这一击某类伤害的百分之几」，所以「总共」= 每秒 × 持续：",
        "",
        *_rules_table(),
        "",
        "三条容易记错的：",
        "",
        "1. **百分比是每秒的，不是总共的。** 点燃 4 秒总共打出 360% 火焰伤害。",
        "2. **中毒没有层数上限**，每次命中叠一层，所以真实 DPS 取决于攻速；"
        "点燃与流血默认只有 1 层（血之舞蹈把流血改成 8 层）。",
        "3. **流血不吃护甲**：物理持续伤害只被目标的物理伤害减免削"
        f"（上限 {constants.get(constants.MONSTER_MAX_PHYS_DR):g}%），护甲只作用于击中。"
        "点燃吃火抗、中毒吃混沌抗。",
        "",
        f"每秒伤害另有一个硬上限 {constants.get(constants.DOT_DPS_CAP):,.0f}"
        "（PoB 的注释写着它是 (2^31 − 1) ÷ 60）。",
        "",
        "算具体数字要给出**造成异常的那一击有多大**——库里没有伤害数值"
        "（PoB 存的是乘数，判据见 `pob_skill_level` 的表注释）。",
        "",
        _BOUNDARY + "。",
        "",
        "来源：",
        *_sources(),
    ])


def _render_result(result: ail.AilmentResult) -> str:
    spec = ail.AILMENTS[result.name]
    out = [f"### {result.name}（{result.name_en}）持续伤害", ""]
    out.extend(render_steps(result.steps))
    out.append("")
    out.append(
        f"**每秒 {result.dps_capped:.0f}**，持续 {result.duration:g} 秒，"
        f"**总共 {result.total:.0f}**。"
    )
    out.append(
        f"{result.percent_per_second:g}% 是**每秒**的比例（{spec['source']}），"
        f"所以 {result.duration:g} 秒总共打出这一击的 "
        f"{result.percent_per_second * result.duration:g}%。"
    )
    out.append(str(spec["stack_note"]) + "。")
    if result.name == "流血":
        out.append(
            "物理持续伤害**不吃护甲**：护甲只作用于击中，流血只被目标的物理伤害减免削。"
        )
    if result.capped:
        out.append(f"已经顶到持续伤害上限 {result.dps_cap:,.0f}/秒。")
    out.append("")
    out.append(_BOUNDARY + "。")
    out.append("")
    out.append("来源：")
    out.extend(_sources())
    text = "\n".join(out)
    if len(text) <= MAX_EVIDENCE_CHARS:
        return text
    return (
        f"{text[:MAX_EVIDENCE_CHARS]}\n\n（证据超过 {MAX_EVIDENCE_CHARS} 字已截断，"
        f"剩余 {len(text) - MAX_EVIDENCE_CHARS} 字未包含在内）"
    )


def _citation(title: str, text: str, url: str) -> dict[str, Any]:
    provenance = constants.provenance(constants.IGNITE_PERCENT)
    priority = int(provenance.source_priority)
    return {
        "chunk_id": f"ailment_dot_calc:{title}",
        "entity_id": None,
        "title": title,
        "url": url,
        "source": provenance.source,
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text,
        "channel": "derived",
    }


@REGISTRY.register(
    name="ailment_dot_calc",
    description=(
        "按 Path of Building 的原式复算持续伤害：点燃 / 中毒 / 流血的**每秒**伤害、"
        "持续时间与总伤害，含叠加规则（中毒无上限、点燃与流血默认 1 层）与减伤口径"
        "（点燃吃火抗、中毒吃混沌抗、**流血不吃护甲**）。\n"
        "问题没给「这一击的伤害」时只讲规则——库里没有伤害数值，不要自己编一个。\n"
        "感电与冰冻是**门槛型**异常、公式完全不同，本工具不算。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "ailment": {
                "type": "string",
                "description": "异常类型：点燃 / 中毒 / 流血（或 Ignite / Poison / Bleed）。"
                "问通用规则时留空",
            },
            "hit_damage": {
                "type": "number",
                "description": "造成异常的那一击的对应类型伤害"
                "（点燃取火焰、中毒取物理+混沌、流血取物理）",
            },
            "stacks": {"type": "integer", "description": "同时生效的层数，默认 1"},
            "mitigation_pct": {
                "type": "number",
                "description": "目标的抗性（点燃/中毒）或物理伤害减免（流血）百分比",
            },
            "dot_multiplier_pct": {
                "type": "number", "description": "「持续伤害加成」百分比",
            },
            "increased_effect_pct": {
                "type": "number", "description": "「异常效果提高」百分比",
            },
            "faster_rate_pct": {
                "type": "number",
                "description": "「燃烧/流血更快」百分比：每秒更高、持续更短，总伤害不变",
            },
        },
        "required": [],
    },
)
def ailment_dot_calc(
    ailment: str = "",
    hit_damage: float = 0.0,
    stacks: int = 1,
    mitigation_pct: float = 0.0,
    dot_multiplier_pct: float = 0.0,
    increased_effect_pct: float = 0.0,
    faster_rate_pct: float = 0.0,
) -> ToolResult:
    try:
        _sources()
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"持续伤害常量未入库：{exc}")

    rules_url = pob_url(
        constants.provenance(constants.IGNITE_PERCENT).url,
        ail.OFFENCE_FILE, lines=ail.IGNITE_LINES,
    )
    if not ailment.strip() or not hit_damage:
        note = ""
        if ailment.strip() and not hit_damage:
            try:
                zh = ail._resolve(ailment)  # noqa: SLF001  只为把名字规范化后回显
            except ail.UnknownAilment as exc:
                return ToolResult(ok=False, error=str(exc))
            note = (
                f"问的是{zh}，但没给**造成{zh}的那一击有多大**，"
                f"所以具体数字算不出来（每秒伤害是这一击的百分比）。下面是规则。\n"
            )
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "kind": "rules",
                "ignite_pct_per_second": constants.get(constants.IGNITE_PERCENT) * 100,
                "poison_pct_per_second": constants.get(constants.POISON_PERCENT) * 100,
                "bleed_pct_per_second": constants.get(constants.BLEED_PERCENT),
                "dot_dps_cap": constants.get(constants.DOT_DPS_CAP),
                "missing_inputs": ["这一击的伤害"] if note else [],
                "boundary": _BOUNDARY,
            },
            citations=[_citation(
                "持续伤害的三种规则", (note + _render_rules()) if note else _render_rules(),
                rules_url,
            )],
            meta={"channel": "derived", "table": "game_constant"},
        )

    try:
        result = ail.ailment_dot(
            ailment, hit_damage, stacks=stacks, mitigation_pct=mitigation_pct,
            dot_multiplier_pct=dot_multiplier_pct,
            increased_effect_pct=increased_effect_pct,
            faster_rate_pct=faster_rate_pct,
        )
    except ail.UnknownAilment as exc:
        return ToolResult(ok=False, error=str(exc))

    return ToolResult(
        ok=True,
        data={
            "found": True,
            "kind": "result",
            "ailment": result.name,
            "percent_per_second": result.percent_per_second,
            "dps": round(result.dps_capped, 2),
            "duration": result.duration,
            "total": round(result.total, 2),
            "stacks": result.stacks,
            "capped": result.capped,
            "boundary": _BOUNDARY,
        },
        citations=[_citation(
            f"{result.name}每秒 {result.dps_capped:.0f}、总共 {result.total:.0f}",
            _render_result(result),
            pob_url(constants.provenance(constants.IGNITE_PERCENT).url,
                    ail.OFFENCE_FILE, lines=ail.AILMENTS[result.name]["lines"]),
        )],
        meta={"channel": "derived", "table": "game_constant"},
    )
