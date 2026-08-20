"""门槛型异常计算：感电 / 冰缓 / 焦灼 / 脆弱 / 干枯，以及冰冻的门槛判据。

## 与持续伤害不是一套公式

点燃/中毒/流血按伤害的百分比掉血（`ailment_dot_calc`）；这五种不掉血，
强度取决于「这一击的伤害 ÷ 目标的异常门槛」的 0.4 次方。所以同一个问题
「打 boss 能感电多少」，答它必须先知道 boss 的异常门槛——那是查表来的，
不是让用户报数。

## 一条能记住的标尺

`50% 感电 ⇔ 这一击的伤害等于目标的异常门槛`（比值 1 时 50 × 1^0.4 = 50）。
工具在证据里给出这条标尺，以及「要顶到上限需要多大的伤害」。

## 冰冻只给能钉住的那条

冰冻的强度是持续时间、走另一条分支，工具**不算时长曲线**，只给 PoB 自己写下的
门槛判据（要冻住最短 0.3 秒，门槛不能超过这一击伤害的 20 倍）与 0.3–3 秒上下限。
"""

from __future__ import annotations

from typing import Any

from calc import constants
from calc import threshold as th
from calc.derive import pob_url, render_steps
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 3200
#: 满级地图怪物的常见口径。**只在问题明确说了等级时才用**，
#: 默认不替用户假定等级——那是给结论装一个编造的前提。
COMMON_MAP_LEVEL = 83

_BOUNDARY = (
    "门槛按等级表 × 头目倍率 × 地图倍率算，这一击的伤害由问题给出；"
    "**冰冻只给门槛判据、不算时长曲线**（上游那段没有可引用的完整原式）；"
    "不含「异常效果提高」以外的各种特殊词缀，也不读取角色档案"
)


def _sources() -> list[str]:
    table = constants.provenance(constants.MONSTER_AILMENT_THRESHOLD_TABLE)
    url = table.url
    return [
        f"- 异常门槛的算法（含头目 {th.BOSS_TYPE_MULT} 倍率）："
        f"{pob_url(url, th.OFFENCE_FILE, lines=th.THRESHOLD_LINES)}",
        f"- 强度公式 `基数 ×(伤害/门槛)^0.4` 与它的反函数："
        f"{pob_url(url, th.OFFENCE_FILE, lines=th.EFFECT_LINES)}",
        f"- 冰冻的门槛判据（PoB 自己的说明原文）："
        f"{pob_url(url, th.OFFENCE_FILE, line=th.FREEZE_NOTE_LINE)}",
        f"- 门槛等级表：{table.key}（GGG 原始表 {table.dat_source or '未标注'}"
        f"{'，出处为推断' if table.inherited else ''}）{table.url}",
        "- 各异常的默认值与上下限：`pobMisc.nonDamagingAilment.*`"
        "（PoB `Modules/Data.lua`）",
    ]


def _rules_table() -> list[str]:
    rows = ["| 异常 | 基数 | 强度范围 | 持续 | 伤害类型 | 效果 |",
            "| --- | --- | --- | --- | --- | --- |"]
    for zh, spec in th.AILMENTS.items():
        low = constants.get(f"pobMisc.nonDamagingAilment.{spec['en']}.min")
        high = constants.get(f"pobMisc.nonDamagingAilment.{spec['en']}.max")
        duration = constants.get(f"pobMisc.nonDamagingAilment.{spec['en']}.duration")
        rows.append(
            f"| {zh}（{spec['en']}） | {float(spec['base']):g} | {low:g}–{high:g}% | "
            f"{duration:g} 秒 | {spec['type']} | {spec['effect']} |"
        )
    return rows


def _render_rules() -> str:
    low, high, _ = th.freeze_threshold_note(0)
    return "\n".join([
        "### 门槛型异常：强度取决于「伤害 ÷ 异常门槛」", "",
        "公式是 **基数 ×(这一击的伤害 ÷ 目标的异常门槛)^0.4 × 效果加成**，"
        "再夹在各自的上下限之间：",
        "",
        *_rules_table(),
        "",
        f"一条能记住的标尺：**比值为 1（这一击正好等于门槛）时，感电 / 冰缓 / 焦灼 "
        f"正好是 50%**——所以要顶到感电上限 50%，这一击就得打出目标一整条异常门槛。",
        "",
        "**异常门槛怎么来**：等级表 ×（头目 "
        f"{th.BOSS_TYPE_MULT}）×（66 级起的地图倍率）。PoB 的说法是"
        "「门槛大致等于生命，头目除外——头目另有专门的倍率」。"
        "所以同一击打小怪能满感电、打头目可能只有几个百分点。",
        "",
        f"**冰冻**的强度是持续时间（{low:g}–{high:g} 秒），走另一条公式；"
        "能钉住的只有 PoB 自己写下的门槛判据：要冻住最短的 "
        f"{low:g} 秒，目标的异常门槛不能超过这一击伤害的 **20 倍**。",
        "",
        "**点燃 / 中毒 / 流血是伤害性异常**，按伤害百分比掉血，用 `ailment_dot_calc`。",
        "",
        _BOUNDARY + "。",
        "",
        "来源：",
        *_sources(),
    ])


def _render_result(result: th.AilmentEffectResult, level: int | None, is_boss: bool) -> str:
    out = [f"### {result.name}（{result.name_en}）强度", ""]
    out.extend(render_steps(result.steps))
    out.append("")
    who = ""
    if level is not None:
        who = f"{level} 级{'头目' if is_boss else '普通怪物'}"
    out.append(
        f"这一击 {result.hit_damage:,.0f} 打 {who}（异常门槛 {result.threshold:,.0f}）："
        f"**{result.name} {result.effect:g}%**，持续 {result.duration:g} 秒。"
    )
    if result.capped:
        out.append(
            f"已经顶到上限 {result.max_effect:g}%——比值到 1 就满了，"
            f"再高的伤害不会让{result.name}更强。"
        )
    else:
        out.append(
            f"要顶到上限 {result.max_effect:g}%，这一击需要打到约 "
            f"**{result.damage_for_max:,.0f}**（也就是目标的一整条异常门槛）。"
        )
    if result.below_min:
        out.append(
            f"低于下限 {result.min_effect:g}%：PoB 把它按下限记，"
            f"但这也意味着这一击对该目标几乎不构成{result.name}。"
        )
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
    provenance = constants.provenance(constants.MONSTER_AILMENT_THRESHOLD_TABLE)
    priority = int(provenance.source_priority)
    return {
        "chunk_id": f"ailment_threshold_calc:{title}",
        "entity_id": None,
        "title": title,
        "url": url,
        "source": provenance.source,
        "source_priority": priority,
        "can_support_facts": priority <= MAX_FACTUAL_PRIORITY,
        "text": text,
        "channel": "derived",
    }


def _freeze_result(
    hit_damage: float, monster_level: int, is_boss: bool, threshold: float, rules_url: str
) -> ToolResult:
    """冰冻：给能钉住的那半（门槛判据 + 上下限），并说清时长曲线为什么不算。"""
    low, high, max_threshold = th.freeze_threshold_note(hit_damage)
    lines = [
        "### 冰冻：能给的与不能给的", "",
        f"**冰冻的强度是持续时间**（{low:g}–{high:g} 秒），走的是与感电/冰缓不同的一条分支。"
        "**本库不算它的时长曲线**：PoB 那段只算了 `FreezeDurationMod`，"
        "完整曲线没有可引用的上游原式——「差不多能推出来」不是可引用的依据。",
        "",
        "能钉住的是 PoB 自己写下的**门槛判据**：",
        "",
        f"> 要冻住最短的 {low:g} 秒，目标的异常门槛不能超过**这一击伤害的 20 倍**。",
    ]
    data: dict[str, Any] = {
        "found": True, "kind": "freeze",
        "min_duration": low, "max_duration": high,
        "boundary": _BOUNDARY,
        "missing_inputs": ["冰冻时长曲线（上游无可引用原式）"],
    }
    if hit_damage:
        lines.append("")
        lines.append(
            f"这一击 {hit_damage:,.0f} ⇒ 门槛不超过 **{max_threshold:,.0f}** 的目标"
            f"能被冻住至少 {low:g} 秒。"
        )
        data["max_threshold_for_min_freeze"] = max_threshold
        if monster_level or threshold:
            try:
                computed = (
                    th.enemy_threshold(monster_level, is_boss=is_boss)
                    if not threshold else None
                )
            except th.OutOfTableRange as exc:
                return ToolResult(ok=False, error=str(exc))
            used = threshold or (computed.threshold if computed else 0.0)
            if computed is not None:
                lines.append("")
                lines.extend(render_steps(computed.steps))
            verdict = "能" if used <= max_threshold else "不能"
            lines.append("")
            lines.append(
                f"目标的异常门槛 **{used:,.0f}**，"
                f"所以这一击**{verdict}**把它冻住至少 {low:g} 秒"
                f"（{used:,.0f} {'≤' if used <= max_threshold else '>'} {max_threshold:,.0f}）。"
                f"**具体几秒仍然算不出来。**"
            )
            data["threshold"] = round(used, 2)
            data["can_freeze_minimum"] = used <= max_threshold
    lines.extend(["", _BOUNDARY + "。", "", "来源：", *_sources()])
    return ToolResult(
        ok=True,
        data=data,
        citations=[_citation("冰冻的门槛判据（时长曲线未建模）", "\n".join(lines), rules_url)],
        meta={"channel": "derived", "table": "game_constant"},
    )


@REGISTRY.register(
    name="ailment_threshold_calc",
    description=(
        "算门槛型异常的强度：感电 / 冰缓 / 焦灼 / 脆弱 / 干枯。"
        "强度 = 基数 ×(这一击的伤害 ÷ 目标的异常门槛)^0.4，夹在各自上下限之间；"
        "门槛按怪物等级表 × 头目 7.68 × 地图倍率算出来（不用用户报数）。\n"
        "也能反过来算「要顶到上限需要多大的伤害」。\n"
        "**冰冻**只给门槛判据（要冻住 0.3 秒，门槛不超过伤害的 20 倍），不算时长曲线。\n"
        "点燃 / 中毒 / 流血是伤害性异常，用 `ailment_dot_calc`。"
        "问题没给伤害或没给等级时只讲规则——不要自己假定一个 83 级。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "ailment": {
                "type": "string",
                "description": "感电 / 冰缓 / 焦灼 / 脆弱 / 干枯（或 Shock / Chill / …）。"
                "问通用规则时留空",
            },
            "hit_damage": {"type": "number", "description": "这一击对应类型的伤害"},
            "monster_level": {
                "type": "integer",
                "description": "目标等级（1–100）。满级地图怪物常按 83 算，"
                "**但只有问题说了才填**",
            },
            "is_boss": {
                "type": "boolean",
                "description": "目标是不是头目：头目的异常门槛要另乘 7.68 与头目地图倍率",
            },
            "threshold": {
                "type": "number",
                "description": "直接给目标的异常门槛（知道的话），给了就不查表",
            },
            "effect_mod_pct": {
                "type": "number", "description": "「异常效果提高」百分比",
            },
            "target_effect": {
                "type": "number",
                "description": "反过来问：要造成这么强的异常（如感电 30%）需要多大的伤害",
            },
        },
        "required": [],
    },
)
def ailment_threshold_calc(
    ailment: str = "",
    hit_damage: float = 0.0,
    monster_level: int = 0,
    is_boss: bool = False,
    threshold: float = 0.0,
    effect_mod_pct: float = 0.0,
    target_effect: float = 0.0,
) -> ToolResult:
    try:
        _sources()
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"异常门槛常量未入库：{exc}")

    rules_url = pob_url(
        constants.provenance(constants.MONSTER_AILMENT_THRESHOLD_TABLE).url,
        th.OFFENCE_FILE, lines=th.EFFECT_LINES,
    )
    notes: list[str] = []
    if "冰冻" in ailment or ailment.strip().lower() == "freeze":
        # 冰冻**不是**「不能答」，而是「能答一半」：时长曲线没有可引用的原式，
        # 但门槛判据与上下限是钉住的。第二十六轮实测：这里原先直接报错，
        # 模型于是连着换参数调了三次工具、把预算烧光，最后一个字都没答出来——
        # 契约（该拒答）过了，机制是坏的。能答的那一半必须以证据形式给出去。
        return _freeze_result(hit_damage, monster_level, is_boss, threshold, rules_url)
    if ailment.strip():
        try:
            th._resolve(ailment)  # noqa: SLF001  只为尽早报错
        except th.UnknownAilment as exc:
            return ToolResult(ok=False, error=str(exc))
    if not ailment.strip() or (
        not (hit_damage or target_effect) or not (monster_level or threshold)
    ):
        if ailment.strip() and hit_damage and not (monster_level or threshold):
            notes.append(
                "给了伤害但没说打**谁**：异常强度取决于「伤害 ÷ 目标的异常门槛」，"
                "而门槛随等级与是否头目差几十倍。给个等级（或直接给门槛）就能算。\n"
            )
        elif ailment.strip() and not hit_damage:
            notes.append(
                "没给**这一击的伤害**，强度算不出来（它是伤害与门槛的比值决定的）。\n"
            )
        text = "".join(notes) + _render_rules()
        return ToolResult(
            ok=True,
            data={
                "found": True, "kind": "rules",
                "shock_max": constants.get("pobMisc.nonDamagingAilment.Shock.max"),
                "chill_max": constants.get("pobMisc.nonDamagingAilment.Chill.max"),
                "boss_type_mult": th.BOSS_TYPE_MULT,
                "missing_inputs": [n.strip() for n in notes],
                "boundary": _BOUNDARY,
            },
            citations=[_citation("门槛型异常的强度规则", text, rules_url)],
            meta={"channel": "derived", "table": "game_constant"},
        )

    if target_effect and (monster_level or threshold):
        # 反问：要造成 X% 需要多大伤害。门槛照样查表，不让用户报数
        computed = (
            th.enemy_threshold(monster_level, is_boss=is_boss)
            if not threshold else None
        )
        used = threshold or (computed.threshold if computed else 0.0)
        needed = th.damage_for_effect(
            ailment, target_effect, used, effect_mod_pct=effect_mod_pct
        )
        steps = list(computed.steps) if computed else []
        text = "\n".join([
            f"### 要造成 {target_effect:g}% {ailment.strip()} 需要多大的伤害", "",
            *(render_steps(steps) + [""] if steps else []),
            f"目标的异常门槛 **{used:,.0f}**，要造成 {target_effect:g}% 的"
            f"{ailment.strip()}，这一击需要打出约 **{needed:,.0f}**。",
            "",
            "反解自同一个公式：由 `强度 = 基数 ×(伤害/门槛)^0.4` 得 "
            "`伤害 = 门槛 ×(强度/基数)^2.5`。",
            "",
            _BOUNDARY + "。",
            "",
            "来源：",
            *_sources(),
        ])
        return ToolResult(
            ok=True,
            data={
                "found": True, "kind": "inverse",
                "threshold": round(used, 2),
                "target_effect": target_effect,
                "damage_needed": round(needed, 2),
                "boundary": _BOUNDARY,
            },
            citations=[_citation(
                f"造成 {target_effect:g}% 需要伤害 {needed:,.0f}", text, rules_url,
            )],
            meta={"channel": "derived", "table": "game_constant"},
        )

    try:
        result = th.ailment_effect(
            ailment, hit_damage,
            level=monster_level or COMMON_MAP_LEVEL, is_boss=is_boss,
            threshold=threshold or None, effect_mod_pct=effect_mod_pct,
        )
    except (th.UnknownAilment, th.OutOfTableRange) as exc:
        return ToolResult(ok=False, error=str(exc))

    return ToolResult(
        ok=True,
        data={
            "found": True, "kind": "result",
            "ailment": result.name,
            "threshold": round(result.threshold, 2),
            "effect": round(result.effect, 2),
            "max_effect": result.max_effect,
            "capped": result.capped,
            "damage_for_max": round(result.damage_for_max, 2),
            "duration": result.duration,
            "boundary": _BOUNDARY,
        },
        citations=[_citation(
            f"{result.name} {result.effect:g}%（门槛 {result.threshold:,.0f}）",
            _render_result(result, monster_level or None, is_boss),
            rules_url,
        )],
        meta={"channel": "derived", "table": "game_constant"},
    )
