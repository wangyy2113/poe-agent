"""Max Hit：一发最多能挨多大的伤害（前六块计算器的合流）。

## 为什么这一块要单独有工具

「我这套装备能不能挨住那一发」是防御问题的终点，而它不是任何单一维度：
抗性、护甲、物理减伤、承受倍率、池子大小要按**顺序**合起来反解。
分开问四遍，用户得自己乘一遍——那正是最容易算错的地方。

## 边界写在最前面

只算**单一伤害类型、100% 不转化**的那一支（PoB 在这一支上是闭式解）。
伤害转化、守护技能、Ward、灵体、**Mind over Matter**（伤害分流到魔力）
都不算——PoB 对多类型转化用的是多轮平滑逼近，它自己的注释说完整解
「可能要解一个六次多项式」。拿单类型的结果去冒充那些情形，是一个带着推导表的错数字。
"""

from __future__ import annotations

from typing import Any

from calc import constants
from calc import maxhit as mh
from calc.derive import fmt, pob_url, render_steps
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

MAX_EVIDENCE_CHARS = 3200

_BOUNDARY = (
    "只算**单一伤害类型、100% 不转化**的一发：池子由问题给出（生命 + 能量护盾），"
    "抗性用实际生效值；**不含伤害转化、守护技能、Ward、灵体与 Mind over Matter**"
    "（PoB 对那些情形用的是多轮平滑逼近，不是闭式解）；护甲只对物理伤害生效"
)


def _sources() -> list[str]:
    cap = constants.provenance(constants.MAX_PHYS_DAMAGE_REDUCTION)
    url = cap.url
    return [
        f"- 无护甲时的闭式：{pob_url(url, mh.DEFENCE_FILE, lines=mh.SIMPLE_LINES)}",
        f"- 有护甲时的二次方程（含两个边界）："
        f"{pob_url(url, mh.DEFENCE_FILE, lines=mh.QUADRATIC_LINES)}",
        f"- PoB 自己的注释：完整解要解六次多项式，所以多类型转化用逼近："
        f"{pob_url(url, mh.DEFENCE_FILE, line=mh.COMMENT_LINE)}",
        f"- 物理减伤上限 {constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION):g}%："
        f"{cap.key}（GGG 原始表 {cap.dat_source or '未标注'}）{cap.url}",
    ]


def _render_rules() -> str:
    cap = constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION)
    return "\n".join([
        "### 一发最多能挨多少：怎么算", "",
        "一发打到身上依次经过 **抗性 → 护甲与物理减伤 → 池子（生命 + 能量护盾）**，"
        "Max Hit 就是把这条链反解。",
        "",
        "**没有护甲**时是一步除法：",
        "",
        "> 最大承受伤害 = 池 ÷（抗性倍率 × 承受倍率）",
        "",
        "所以 75% 抗性、5000 池 ⇒ 能挨 20,000：抗性把一发砍到四分之一。",
        "",
        f"**有护甲**时不是——护甲减伤本身依赖伤害大小（`护甲 /(护甲 + 5 × 伤害)`），"
        f"所以反解要解一个二次方程，再夹在「完全不算护甲」与「护甲吃满 {cap:g}% "
        f"减伤上限」两个边界之间。护甲对**大打击**的收益远低于小打击，"
        f"这就是「护甲挡不住大招」的算术来源。",
        "",
        "要算具体数字，请给出：**池子**（生命 + 能量护盾）、该类型的**实际生效抗性**，"
        "以及护甲（物理伤害才用得上）。",
        "",
        _BOUNDARY + "。",
        "",
        "来源：",
        *_sources(),
    ])


def _render_result(result: mh.MaxHitResult, damage_type: str) -> str:
    out = [f"### 一发{damage_type}最多能挨 {fmt(result.max_hit)}", ""]
    out.extend(render_steps(result.steps))
    out.append("")
    out.append(
        f"池子 {fmt(result.pool)}、{damage_type}抗性 {result.resist_pct:g}%"
        + (f"、护甲 {fmt(result.armour)}" if result.armour else "")
        + f"：**一发最多能挨 {fmt(result.max_hit)} 原始伤害**"
        f"（相当于裸池的 {result.effective_multiplier:.2f} 倍）。"
    )
    if result.armour:
        out.append(
            f"这一发会被护甲与减伤挡掉约 {result.armour_reduction_pct:.1f}%——"
            f"护甲减伤依赖伤害大小，所以**同一堆护甲对更大的打击挡得更少**。"
        )
        if result.armour_capped:
            out.append(
                f"这一发已经把减伤顶到上限（{constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION):g}%），"
                "再加护甲不会更高。"
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
    provenance = constants.provenance(constants.MAX_PHYS_DAMAGE_REDUCTION)
    priority = int(provenance.source_priority)
    return {
        "chunk_id": f"max_hit_calc:{title}",
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
    name="max_hit_calc",
    description=(
        "算「一发最多能挨多大的伤害」：把抗性 → 护甲/物理减伤 → 生命+护盾池这条链反解。"
        "无护甲时是一步除法，有护甲时按 PoB 的二次方程闭式解并夹在两个边界之间；"
        "同时给出「相当于裸池的多少倍」。\n"
        "**只算单一伤害类型、不转化**；不含守护技能、Ward、Mind over Matter。\n"
        "问题没给池子或抗性时只讲规则，不要自己编一个 5000 血。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "life": {"type": "number", "description": "当前可用生命"},
            "energy_shield": {"type": "number", "description": "能量护盾"},
            "pool": {
                "type": "number",
                "description": "直接给池子（生命 + 护盾）。给了它就不再看 life / energy_shield",
            },
            "resist_pct": {
                "type": "number",
                "description": "该类型的**实际生效抗性**（不是超抗前的总抗）。物理伤害填 0",
            },
            "armour": {"type": "number", "description": "护甲值（只对物理伤害生效）"},
            "flat_reduction_pct": {
                "type": "number", "description": "另有的物理伤害减免百分比（耐力球等）",
            },
            "taken_mult": {
                "type": "number",
                "description": "「受到的伤害减少/增加」乘算修正，0.8 表示少受 20%",
            },
            "overwhelm_pct": {
                "type": "number", "description": "敌方的压制（无视物理减伤）百分比",
            },
            "damage_type": {
                "type": "string",
                "description": "伤害类型（物理 / 火焰 / 冰霜 / 闪电 / 混沌），只用于措辞",
            },
        },
        "required": [],
    },
)
def max_hit_calc(
    life: float = 0.0,
    energy_shield: float = 0.0,
    pool: float = 0.0,
    resist_pct: float = 0.0,
    armour: float = 0.0,
    flat_reduction_pct: float = 0.0,
    taken_mult: float = 1.0,
    overwhelm_pct: float = 0.0,
    damage_type: str = "",
) -> ToolResult:
    try:
        _sources()
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"减伤上限常量未入库：{exc}")

    rules_url = pob_url(
        constants.provenance(constants.MAX_PHYS_DAMAGE_REDUCTION).url,
        mh.DEFENCE_FILE, lines=mh.QUADRATIC_LINES,
    )
    total = pool or (life + energy_shield)
    if total <= 0:
        note = ""
        if resist_pct or armour:
            note = (
                "给了抗性 / 护甲但**没给池子**（生命 + 能量护盾），"
                "能挨多少就算不出来——Max Hit 是「池子 ÷ 各层倍率」。\n\n"
            )
        return ToolResult(
            ok=True,
            data={
                "found": True, "kind": "rules",
                "damage_reduction_cap": constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION),
                "missing_inputs": ["生命 + 能量护盾"] if note else [],
                "boundary": _BOUNDARY,
            },
            citations=[_citation("Max Hit 的算法与边界", note + _render_rules(), rules_url)],
            meta={"channel": "derived", "table": "game_constant"},
        )

    result = mh.max_hit(
        total, resist_pct=resist_pct, armour=armour,
        flat_reduction_pct=flat_reduction_pct, taken_mult=taken_mult,
        overwhelm_pct=overwhelm_pct,
    )
    label = damage_type.strip() or ("物理" if armour else "该类型")
    return ToolResult(
        ok=True,
        data={
            "found": True, "kind": "result",
            "pool": total,
            "max_hit": result.max_hit,
            "without_armour": round(result.without_armour, 2),
            "effective_multiplier": round(result.effective_multiplier, 2),
            "armour_reduction_pct": round(result.armour_reduction_pct, 2),
            "armour_capped": result.armour_capped,
            "damage_type": label,
            "boundary": _BOUNDARY,
        },
        citations=[_citation(
            f"一发{label}最多能挨 {fmt(result.max_hit)}",
            _render_result(result, label), rules_url,
        )],
        meta={"channel": "derived", "table": "game_constant"},
    )
