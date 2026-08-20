"""回复计算：能量护盾回充、偷取、魔力再生。

## 为什么这三件事值得一个工具

常量表里躺着 `energy_shield_recharge_rate_per_minute_%` = 2000、
`maximum_life_leech_rate_%_per_minute` = 1200 这些数，但**没人会这么问问题**。
真实问题是「我 5000 护盾多久回满」「偷取上限是多少」「魔力再生怎么算」——
答它们要先把「每分钟百分比」换成每秒、把延迟与速率分开说、把偷取的两层上限分开说。

三个容易记错的，都写进证据：

1. **护盾回充要先等 2 秒**。只说「每秒回 33%」会让人以为受伤后立刻开始回。
2. **偷取有两层上限**：每秒最多回复池的 20%（生命/魔力）或 10%（护盾），
   而**单次偷取实例**最多回复池的 10%。前者限速、后者限量。
3. **魔力再生基数是每分钟 105%**，换成每秒是 1.75%。拿 105 当每秒用会差 60 倍。

## 参数必须逐字来自问题

池大小、偷取百分比、这一击伤害一律由问题给出；没给就只讲规则。
护盾回充的「多久回满」不依赖池大小（每秒回 33% ⇒ 约 3 秒），所以规则卡也能答一半——
但**具体每秒回多少**必须有池大小才算。
"""

from __future__ import annotations

from typing import Any

from calc import constants
from calc import recovery as rec
from calc.derive import pob_url, render_steps
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 3200

_BOUNDARY = (
    "只按 PoB 的回复公式复算：输入逐字取自问题，没给的项不算；"
    "**不含击中回复 / 药剂 / 生命再生等另有上限的机制**，也不含 "
    "Mind over Matter 那种把伤害分流到魔力的池保护；不读取角色档案"
)


#: PoB 在 `Modules/Data.lua` 里把回充基数写了两次，取的是后写的字面量 `0.33`；
#: 而 GGG 常量 `energy_shield_recharge_rate_per_minute_%` = 2000 换算下来是 0.3333…
#: 两者差**正好 1%**。我们复算的是 PoB，所以用 0.33——但这条差异要说出来，
#: 否则用户会以为 33% 是游戏的精确值（判据见 `tests/unit/test_recovery.py`）。
_RECHARGE_ROUNDING_NOTE = (
    "口径提示：回充基数在 PoB 里是字面量 0.33，而 GGG 常量"
    "（每分钟 2000%）换算下来是 33.33%/秒，两者差 1%。这里按 PoB 的取值算，"
    "所以 5000 护盾得到每秒 1650 而不是 1666.7。"
)


def _sources() -> list[str]:
    base = constants.provenance(constants.ES_RECHARGE_BASE)
    delay = constants.provenance(constants.ES_RECHARGE_DELAY)
    leech = constants.provenance(constants.LEECH_RATE_BASE)
    regen = constants.provenance(constants.MANA_REGEN_RATE_PER_MINUTE)
    url = base.url

    def anchor(path: str, *, line: int | None = None, lines: tuple[int, int] | None = None) -> str:
        return pob_url(url, path, line=line, lines=lines)

    return [
        f"- 护盾回充基数 {constants.get(constants.ES_RECHARGE_BASE):g}"
        f"（每秒 {constants.get(constants.ES_RECHARGE_BASE) * 100:g}% 最大护盾）："
        f"{base.key} {base.url}",
        f"- 开始回充的延迟 {constants.get(constants.ES_RECHARGE_DELAY):g} 秒："
        f"{delay.key} {delay.url}",
        f"- 回充公式：{anchor(rec.DEFENCE_FILE, lines=rec.RECHARGE_LINES)}",
        f"- 偷取基数 {constants.get(constants.LEECH_RATE_BASE):g}"
        f"（一次实例每秒回复 {constants.get(constants.LEECH_RATE_BASE) * 100:g}% 池）："
        f"{leech.key} {leech.url}",
        f"- 偷取速率与持续时间：{anchor(rec.OFFENCE_FILE, lines=rec.LEECH_RATE_LINES)}",
        f"- 偷取的两层上限由常量表播种：{anchor(rec.SETUP_FILE, lines=rec.LEECH_CAP_LINES)}",
        f"- 魔力再生基数 {constants.get(constants.MANA_REGEN_RATE_PER_MINUTE):g}%/分钟："
        f"{regen.key}（GGG 原始表 {regen.dat_source or '未标注'}）{regen.url}",
    ]


def _leech_caps() -> list[str]:
    rows = ["| 池 | 每秒速率上限 | 单次实例上限 |", "| --- | --- | --- |"]
    for pool, (rate_key, instance_key) in (
        ("生命", (constants.MAX_LIFE_LEECH_RATE, constants.MAX_LIFE_LEECH_INSTANCE)),
        ("魔力", (constants.MAX_MANA_LEECH_RATE, constants.MAX_MANA_LEECH_INSTANCE)),
        ("能量护盾", (constants.MAX_ES_LEECH_RATE, constants.MAX_ES_LEECH_INSTANCE)),
    ):
        rows.append(
            f"| {pool} | {constants.get(rate_key) / 60:g}% | "
            f"{constants.get(instance_key):g}% |"
        )
    return rows


def _render_rules() -> str:
    base = constants.get(constants.ES_RECHARGE_BASE)
    delay = constants.get(constants.ES_RECHARGE_DELAY)
    leech_base = constants.get(constants.LEECH_RATE_BASE)
    regen = constants.get(constants.MANA_REGEN_RATE_PER_MINUTE)
    return "\n".join([
        "### 回复是怎么算的",
        "",
        f"**能量护盾回充**：每秒回复最大护盾的 **{base * 100:g}%**（所以回满约 "
        f"{1 / base:.1f} 秒），但**要先等 {delay:g} 秒没受到伤害才开始**。"
        f"「多久回满」= 延迟 + 池 ÷ 每秒回充量，两段都要算。",
        "",
        f"**偷取**有两层上限，前者限速、后者限量：",
        "",
        *_leech_caps(),
        "",
        f"一次偷取实例每秒回复池的 **{leech_base * 100:g}%**，"
        f"所以它持续 = 这次偷到的量 ÷（池 × {leech_base:g}）秒；"
        f"多个实例叠加时总速率仍受上表的速率上限约束。",
        "",
        f"**魔力再生**基数是每分钟 **{regen:g}%**，也就是每秒 "
        f"**{regen / 60 / 100 * 100:.2f}%** 魔力。",
        "",
        _RECHARGE_ROUNDING_NOTE,
        "",
        _BOUNDARY + "。",
        "",
        "来源：",
        *_sources(),
    ])


def _recharge_section(result: rec.RechargeResult) -> list[str]:
    body = render_steps(result.steps)
    body.append("")
    body.append(
        f"{result.energy_shield:g} 点护盾：**每秒回充 {result.per_second}**、"
        f"延迟 {result.delay_seconds:g} 秒，"
        f"**受伤后约 {result.total_seconds:.1f} 秒回满**"
        f"（其中回充本身 {result.seconds_to_full:.1f} 秒）。"
    )
    body.append(
        f"每秒回复的是最大护盾的 {result.base_pct:g}%，所以**回满耗时与护盾大小无关**——"
        f"护盾越大，每秒回得越多。"
    )
    body.append(_RECHARGE_ROUNDING_NOTE)
    return body


def _leech_section(result: rec.LeechResult) -> list[str]:
    body = render_steps(result.steps)
    body.append("")
    body.append(
        f"{result.pool_name} {result.pool:g}：一个偷取实例每秒回 "
        f"{result.instance_rate:g}，**实际每秒回复 {result.total_rate:g}**"
        f"（速率上限 {result.rate_cap:g}，即每秒 {result.rate_cap_pct:g}% {result.pool_name}）。"
    )
    if result.amount:
        body.append(
            f"这一击偷到 {result.amount:g}，单次实例上限是 {result.instance_cap:g}"
            f"（{result.pool_name}的 {result.instance_cap_pct:g}%），"
            f"实际按 {result.effective_amount:g} 计、持续 {result.duration:.2f} 秒。"
        )
    if result.rate_capped:
        body.append("**速率已封顶**：再叠偷取实例不会更快，只会让它持续更久。")
    if result.amount_capped:
        body.append("**单次量已封顶**：这一击偷到的量超过了单次实例上限，超出部分不生效。")
    return body


def _regen_section(result: rec.ManaRegenResult) -> list[str]:
    body = render_steps(result.steps)
    body.append("")
    body.append(
        f"魔力 {result.mana:g}：**每秒再生 {result.per_second:.2f}**"
        f"（基数每分钟 {result.base_pct_per_minute:g}%，即每秒 "
        f"{result.base_pct_per_minute / 60 / 100 * 100:.2f}%）。"
    )
    return body


def _clip(text: str) -> str:
    if len(text) <= MAX_EVIDENCE_CHARS:
        return text
    return (
        f"{text[:MAX_EVIDENCE_CHARS]}\n\n（证据超过 {MAX_EVIDENCE_CHARS} 字已截断，"
        f"剩余 {len(text) - MAX_EVIDENCE_CHARS} 字未包含在内）"
    )


def _citation(title: str, text: str, url: str) -> dict[str, Any]:
    provenance = constants.provenance(constants.ES_RECHARGE_BASE)
    priority = int(provenance.source_priority)
    return {
        "chunk_id": f"recovery_calc:{title}",
        "entity_id": None,
        "title": title,
        "url": url,
        "source": provenance.source,
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": _clip(text),
        "channel": "derived",
    }


@REGISTRY.register(
    name="recovery_calc",
    description=(
        "按 Path of Building 的原式复算回复：能量护盾回充（每秒量、2 秒延迟、多久回满）、"
        "生命/魔力/护盾偷取（单次量、持续时间、每秒速率与**两层上限**）、魔力再生。"
        "给出逐步推导。\n"
        "问题没给池大小时只讲规则（回充百分比与延迟、偷取的两层上限、再生基数）——"
        "不要自己编一个池大小。所有数值参数必须逐字取自用户问题。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "energy_shield": {"type": "number", "description": "最大能量护盾，算回充用"},
            "increased_recharge_pct": {
                "type": "number", "description": "「增加的能量护盾回充速度」百分比",
            },
            "faster_start_pct": {
                "type": "number", "description": "「更快开始回充」百分比（缩短那 2 秒延迟）",
            },
            "pool": {"type": "number", "description": "算偷取时的池大小（生命/魔力/护盾的最大值）"},
            "pool_name": {
                "type": "string",
                "description": "偷取的池：生命 / 魔力 / 能量护盾。三种池的上限不同，别混",
            },
            "leech_pct": {"type": "number", "description": "偷取百分比，如「2% 物理伤害偷取生命」写 2"},
            "hit_damage": {"type": "number", "description": "这一击的伤害，用来算单次偷到多少"},
            "increased_leech_rate_pct": {
                "type": "number", "description": "「增加的偷取速度」百分比",
            },
            "instances": {
                "type": "integer", "description": "同时生效的偷取实例数（默认 1）",
            },
            "mana": {"type": "number", "description": "最大魔力，算魔力再生用"},
            "increased_regen_pct": {
                "type": "number", "description": "「增加的魔力再生速度」百分比",
            },
            "flat_regen": {"type": "number", "description": "定值魔力再生（每秒）"},
        },
        "required": [],
    },
)
def recovery_calc(
    energy_shield: float = 0.0,
    increased_recharge_pct: float = 0.0,
    faster_start_pct: float = 0.0,
    pool: float = 0.0,
    pool_name: str = "生命",
    leech_pct: float = 0.0,
    hit_damage: float = 0.0,
    increased_leech_rate_pct: float = 0.0,
    instances: int = 1,
    mana: float = 0.0,
    increased_regen_pct: float = 0.0,
    flat_regen: float = 0.0,
) -> ToolResult:
    try:
        sources = _sources()
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"回复常量未入库：{exc}")

    sections: list[tuple[str, list[str]]] = []
    data: dict[str, Any] = {"found": True, "kind": "result"}
    notes: list[str] = []

    if energy_shield:
        result = rec.energy_shield_recharge(
            energy_shield,
            increased_recharge_pct=increased_recharge_pct,
            faster_start_pct=faster_start_pct,
        )
        sections.append(("能量护盾回充", _recharge_section(result)))
        data["recharge"] = {
            "per_second": result.per_second,
            "delay_seconds": result.delay_seconds,
            "total_seconds": round(result.total_seconds, 2),
        }

    if pool:
        try:
            result = rec.leech(
                pool,
                pool_name=pool_name,
                leech_pct=leech_pct,
                hit_damage=hit_damage,
                increased_leech_rate_pct=increased_leech_rate_pct,
                instances=instances,
            )
        except rec.UnknownPool as exc:
            return ToolResult(ok=False, error=str(exc))
        sections.append((f"{result.pool_name}偷取", _leech_section(result)))
        data["leech"] = {
            "pool": result.pool_name,
            "instance_rate": result.instance_rate,
            "total_rate": result.total_rate,
            "rate_cap": result.rate_cap,
            "instance_cap": result.instance_cap,
            "duration": round(result.duration, 2),
        }
    elif leech_pct or hit_damage:
        notes.append(
            "问了偷取但没给池大小（最大生命 / 魔力 / 护盾），**每秒回多少算不出来**："
            "偷取的速率与上限都是按池的百分比算的。"
        )

    if mana:
        result = rec.mana_regen(
            mana, increased_regen_pct=increased_regen_pct, flat_regen=flat_regen
        )
        sections.append(("魔力再生", _regen_section(result)))
        data["mana_regen"] = {"per_second": round(result.per_second, 2)}

    data["boundary"] = _BOUNDARY
    if not sections:
        text = _render_rules()
        if notes:
            text = "\n".join([*notes, "", text])
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "kind": "rules",
                "es_recharge_pct_per_second": constants.get(constants.ES_RECHARGE_BASE) * 100,
                "es_recharge_delay": constants.get(constants.ES_RECHARGE_DELAY),
                "leech_rate_pct_per_second": constants.get(constants.LEECH_RATE_BASE) * 100,
                "boundary": _BOUNDARY,
                "missing_inputs": notes,
            },
            citations=[_citation(
                "回复的三段与它们的上限", text,
                pob_url(constants.provenance(constants.ES_RECHARGE_BASE).url,
                        rec.DEFENCE_FILE, lines=rec.RECHARGE_LINES),
            )],
            meta={"channel": "derived", "table": "game_constant"},
        )

    out: list[str] = []
    for title, body in sections:
        out.append(f"### {title}")
        out.append("")
        out.extend(body)
        out.append("")
    out.append(_BOUNDARY + "。")
    out.append("")
    out.append("来源：")
    out.extend(sources)
    if notes:
        out = [*notes, "", *out]
        data["missing_inputs"] = notes
    title = "回复（" + "、".join(name for name, _ in sections) + "）"
    return ToolResult(
        ok=True,
        data=data,
        citations=[_citation(
            title, "\n".join(out),
            pob_url(constants.provenance(constants.ES_RECHARGE_BASE).url,
                    rec.DEFENCE_FILE, lines=rec.RECHARGE_LINES),
        )],
        meta={"channel": "derived", "table": "game_constant"},
    )
