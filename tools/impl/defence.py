"""防御计算：抗性与最大抗性、以及受击结算（闪避 / 格挡 / 压制 / 护甲）。

## 为什么抗性要有工具，而不是靠卡片

抗性上限 75、硬上限 90、下限 −200 这三个数早就在 `game_constant` 里（499 条），
但**它们摆在那儿答不了任何一个真实问题**：真实问题是「我火抗 120 是不是浪费了」
「地图 −60% 抗性之后我还剩多少」「超抗能不能防穿透」。这些都要按 PoB 的**运算顺序**
算一遍，而顺序在代码里而不在数据里。

所以这里的证据是**算出来的推导表**，逐步都写出来（`passive_path` 那一轮定下的规矩：
算出来的数字要连推导一起给，用户能照着核对到每一步）。

## 参数必须逐字来自问题

工具不猜「你大概有多少抗」。没给就是 0，并且**把每个输入原样回显在证据里**——
用户一眼能看出工具是拿什么算的。剧情抗性惩罚（−60%）**默认不加**：角色面板上的
抗性已经含它，替用户再减一次是把一个对的数字算错。

## 两个工具，一条链

`resistance_calc` 管抗性那一段，`mitigation_calc` 管它**之前**的几段：
能不能命中（闪避）、能不能挡下来（格挡 / 法术压制）、打中了减多少（护甲）。
分成两个工具而不是一个大工具，是因为参数完全不同——一个吃抗性词缀，
一个吃闪避/护甲/几率。但两者的证据都写明**顺序**，因为顺序错了结论会反向。

## 不做的事

图腾抗性与持续伤害抗性不算（PoB 有独立分支）；头目的护甲/闪避倍率不算
（PoB 那是按一份头目清单现算的均值，本库没有那份清单）；不读 build（PoB 导入还没做）；
不给装备推荐与价格。完整差异清单在 `calc/resistance.py` 与 `calc/mitigation.py`
的模块 docstring 里。
"""

from __future__ import annotations

from typing import Any

from calc import constants
from calc import mitigation as mit
from calc import resistance as res
from calc.derive import Step, pob_url, render_steps
from entities.taxonomy import MAX_FACTUAL_PRIORITY
from tools.registry import REGISTRY, ToolResult

#: 证据上限。实测最长的一版（受击七段全给）2354 字、抗性最长 2021 字，
#: 留出余量到 3200——推导表的**末尾是来源清单**，截掉它等于让引用消失。
#: 真截断了要看得见（见 `_citation`）：静默截断等同于漏答。
MAX_EVIDENCE_CHARS = 3200

_BOUNDARY = (
    "只按 PoB 的抗性公式复算：输入逐字取自问题，没给的项按 0 算；"
    "**不含图腾抗性与持续伤害抗性**（PoB 另有分支），也不读取角色档案、"
    "不给装备推荐与价格"
)

#: 输入项 → 证据里回显用的中文标签。回显是为了让「工具拿什么算的」一眼可见。
_INPUT_LABELS: tuple[tuple[str, str], ...] = (
    ("base_specific", "该抗性的「+X% 抗性」之和"),
    ("base_elemental", "「+X% 全部元素抗性」之和"),
    ("resist_penalty", "减抗 / 诅咒等负值"),
    ("campaign_penalty", "剧情抗性惩罚"),
    ("increased_pct", "「增加的抗性」百分比"),
    ("more_mult", "「更多/更少抗性」连乘"),
    ("extra_max_specific", "该抗性的最大抗性加成"),
    ("extra_max_elemental", "最大元素抗性加成"),
    ("max_override", "最大抗性被覆写为"),
    ("enemy_penetration", "敌方穿透"),
)


def _sources() -> tuple[str, list[str]]:
    """三个常量的出处 + 两处公式原文的同 commit 引用。

    URL 从常量行推出来（`res.pob_url`）：必须与常量入库那一刻的 commit 一致，
    否则引用点开是另一份代码而读者看不出来。
    """
    cap = constants.provenance(constants.MAX_RESIST_CAP)
    base_max = constants.provenance(constants.MAX_ALL_RESISTANCES)
    floor = constants.provenance(constants.RESIST_FLOOR)
    formula = pob_url(cap.url, res.FORMULA_FILE, lines=res.FORMULA_LINES)
    lines = [
        f"- 基础最大抗性 {constants.get(constants.MAX_ALL_RESISTANCES):g}%："
        f"{base_max.key}（GGG 原始表 {base_max.dat_source or '未标注'}）{base_max.url}",
        f"- 最大抗性硬上限 {constants.get(constants.MAX_RESIST_CAP):g}%：{cap.key}"
        f"（PoB 自己的 magic numbers，无 GGG 原始表出处）{cap.url}",
        f"- 抗性下限 {constants.get(constants.RESIST_FLOOR):g}%：{floor.key} {floor.url}",
        f"- 抗性结算公式：`calcs.resistances` {formula}",
        f"- 抗性 → 承受倍率：`resMult = 1 - (resist - enemyPen) / 100` "
        f"{pob_url(cap.url, res.FORMULA_FILE, line=res.TAKEN_LINE)}",
        f"- 剧情抗性惩罚三档（0 / −30 / −60，PoB 默认 −60）："
        f"{pob_url(cap.url, res.CONFIG_FILE, line=res.CONFIG_PENALTY_LINE)}",
    ]
    return formula, lines


def _render_rules() -> str:
    """没有给任何数值时：只讲规则，一个数都不替用户编。"""
    base_max = constants.get(constants.MAX_ALL_RESISTANCES)
    cap = constants.get(constants.MAX_RESIST_CAP)
    floor = constants.get(constants.RESIST_FLOOR)
    _, sources = _sources()
    return "\n".join([
        "### 抗性是怎么结算的（四种抗性同一套规则）",
        "",
        f"1. **加总**：所有「+X% 抗性」相加，含负值（地图的减抗词缀、诅咒、"
        f"元素冲击都是负的），剧情抗性惩罚也算在这一步。",
        "2. **乘上「增加的抗性」**：这类词缀很少见；整体不小于 0。",
        "3. **截断小数**：`75.9%` 按 `75%` 算，不四舍五入。",
        f"4. **夹在上下限之间**：下限 {floor:g}%，上限就是「最大抗性」。",
        "",
        f"**最大抗性**默认 {base_max:g}%，可被「+X% 最大元素抗性」这类词缀提高，"
        f"但不超过硬上限 {cap:g}%（少数唯一装备能覆写这个上限）。"
        f"「+X% 最大元素抗性」只作用于火焰/冰霜/闪电，**混沌抗性不吃**。",
        "",
        f"**承受倍率** = 1 −（实际抗性 − 敌方穿透）÷ 100。抗性 {base_max:g}% 时"
        f"承受 {100 - base_max:g}%，也就是同一发元素伤害只吃四分之一。",
        "",
        "**超出上限的抗性（超抗）不减伤，但不是白给**：减抗与诅咒作用在第 1 步、"
        "在夹上限**之前**，所以超抗能顶住它们；而**穿透是在夹上限之后才减的**"
        "（见下方两处引用的行号），所以超抗顶不住穿透。",
        "",
        _BOUNDARY + "。",
        "",
        "来源：",
        *sources,
    ])


def _render_result(result: res.ResistanceResult, inputs: dict[str, float]) -> str:
    _, sources = _sources()
    out = [f"### {result.element_zh}抗性结算（按 PoB 公式复算）", ""]
    # 只回显**真正给了**的项。把一串 0 也列出来，读起来像是在断言
    # 「你的最大抗性加成是 0」，而事实是「问题没提」——这两件事不能混。
    out.append("**输入（逐字取自问题，其余项问题未给、按 0 算）**：")
    shown = 0
    for key, label in _INPUT_LABELS:
        if key not in inputs:
            continue
        value = inputs[key]
        shown += 1
        if key == "more_mult":
            out.append(f"- {label}：×{value:g}")
        else:
            out.append(f"- {label}：{value:g}%")
    if not shown:
        out.append("- （问题未给任何数值，下表按全 0 演示公式）")
    out.append("")
    out.extend(render_steps(result.steps))
    out.append("")

    conclusion = (
        f"**{result.element_zh}抗性实际生效 {result.final}%**"
        f"（最大抗性 {result.max_resist}%，下限 {result.floor}%）。"
    )
    if result.over_cap:
        conclusion += (
            f"总抗 {result.total}% 超出上限 {result.over_cap}%：这部分**不减伤**，"
            f"但能顶住 {result.over_cap}% 以内的减抗词缀或诅咒。"
        )
    if result.missing:
        conclusion += f"距离上限还差 {result.missing}%。"
    out.append(conclusion)
    out.append("")

    pen_note = (
        f"（已减去敌方穿透 {result.enemy_penetration:g}%）"
        if result.enemy_penetration
        else ""
    )
    out.append(
        f"**承受倍率 {result.taken_mult:.2f}**{pen_note}：每 100 点"
        f"{result.element_zh}伤害实际吃 {result.taken_pct:.0f} 点。"
    )
    if 0 < result.taken_mult < 1:
        # 有效生命倍率是承受倍率的倒数——这是同一件事的另一种说法，
        # 也是玩家真正关心的口径（「多抗一发」）。
        out.append(
            f"换个口径：面对{result.element_zh}伤害时，同一血量顶得住的伤害是裸抗时的 "
            f"{1 / result.taken_mult:.2f} 倍。"
        )
    if result.enemy_penetration:
        out.append("")
        out.append(
            "**穿透是在夹上限之后才减的**，所以超抗顶不住穿透（两处代码的先后顺序，"
            "引用里给了行号）。"
        )
    out.append("")
    out.append(_BOUNDARY + "。")
    out.append("")
    out.append("来源：")
    out.extend(sources)
    return "\n".join(out)


def _clip(text: str) -> str:
    """超长时截断，并**写明截断了**——推导表的末尾是来源清单，悄悄截掉它等于引用消失。"""
    if len(text) <= MAX_EVIDENCE_CHARS:
        return text
    kept = text[:MAX_EVIDENCE_CHARS]
    return (
        f"{kept}\n\n（证据超过 {MAX_EVIDENCE_CHARS} 字已截断，"
        f"剩余 {len(text) - MAX_EVIDENCE_CHARS} 字未包含在内）"
    )


def _citation(title: str, text: str, url: str) -> dict[str, Any]:
    provenance = constants.provenance(constants.MAX_RESIST_CAP)
    priority = int(provenance.source_priority)
    return {
        "chunk_id": f"resistance_calc:{title}",
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
    name="resistance_calc",
    description=(
        "按 Path of Building 的原式复算抗性：把「+X% 抗性」加总（含地图减抗、诅咒等"
        "负值与剧情抗性惩罚）→ 截断小数 → 夹在下限与最大抗性之间，给出**逐步推导**、"
        "实际生效抗性、超出上限的部分、距上限差多少与承受倍率。\n"
        "也能只讲规则（最大抗性上限、硬上限、下限、超抗防什么不防什么）——"
        "问题没给数值时不要自己编，留空即可。\n"
        "**所有数值参数必须逐字取自用户问题**；问题没提到的项不要填，"
        "**也不要替它做加减**——地图减抗、诅咒写进 `resist_penalty`，"
        "分开传推导里才会同时出现问题里的每个数字。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "element": {
                "type": "string",
                "description": "抗性类型：火焰 / 冰霜 / 闪电 / 混沌。"
                "问的是通用规则（如「抗性上限是多少」）时留空",
            },
            "base_specific": {
                "type": "number",
                "description": "该抗性的「+X% 抗性」之和；地图减抗、诅咒、元素冲击写成负数",
            },
            "base_elemental": {
                "type": "number",
                "description": "「+X% 全部元素抗性」之和（混沌抗性不吃，工具会写明）",
            },
            "resist_penalty": {
                "type": "number",
                "description": "地图减抗词缀、诅咒、元素冲击等**负值**之和，写成负数。"
                "**不要自己先与抗性相减**：分开传，推导里才会同时出现问题里的两个数字",
            },
            "campaign_penalty": {
                "type": "number",
                "description": "剧情抗性惩罚：0 / -30（第五幕）/ -60（第十幕）。"
                "**只有在把装备词缀逐项相加时才传**——角色面板上的抗性已含这一项",
            },
            "increased_pct": {
                "type": "number",
                "description": "「增加的抗性」百分比之和（罕见）",
            },
            "more_mult": {"type": "number", "description": "「更多/更少抗性」连乘，默认 1"},
            "extra_max_specific": {
                "type": "number",
                "description": "该抗性的「+X% 最大抗性」之和",
            },
            "extra_max_elemental": {
                "type": "number",
                "description": "「+X% 最大元素抗性」之和（对混沌不生效）",
            },
            "max_override": {
                "type": "number",
                "description": "最大抗性被唯一装备覆写成的值（如熔铸之魂），会绕过硬上限",
            },
            "enemy_penetration": {
                "type": "number",
                "description": "敌方对该抗性的穿透值",
            },
        },
        "required": [],
    },
)
def resistance_calc(
    element: str = "",
    base_specific: float = 0.0,
    base_elemental: float = 0.0,
    resist_penalty: float = 0.0,
    campaign_penalty: float = 0.0,
    increased_pct: float = 0.0,
    more_mult: float = 1.0,
    extra_max_specific: float = 0.0,
    extra_max_elemental: float = 0.0,
    max_override: float | None = None,
    enemy_penetration: float = 0.0,
) -> ToolResult:
    inputs: dict[str, float] = {
        "base_specific": float(base_specific),
        "base_elemental": float(base_elemental),
        "resist_penalty": float(resist_penalty),
        "campaign_penalty": float(campaign_penalty),
        "increased_pct": float(increased_pct),
        "more_mult": float(more_mult),
        "extra_max_specific": float(extra_max_specific),
        "extra_max_elemental": float(extra_max_elemental),
        "enemy_penetration": float(enemy_penetration),
    }
    if max_override is not None:
        inputs["max_override"] = float(max_override)
    # 全零 = 问题里没有任何数值。这时讲规则，而不是输出一份「0% 抗性」的推导——
    # 那是把「你没告诉我」呈现成「你的抗性是 0」。
    given = {
        key: value
        for key, value in inputs.items()
        if value != (1.0 if key == "more_mult" else 0.0)
    }

    try:
        formula_url, _ = _sources()
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"抗性常量未入库：{exc}")

    if not given and not element.strip():
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "kind": "rules",
                "base_max_resist": constants.get(constants.MAX_ALL_RESISTANCES),
                "max_resist_cap": constants.get(constants.MAX_RESIST_CAP),
                "resist_floor": constants.get(constants.RESIST_FLOOR),
                "boundary": _BOUNDARY,
            },
            citations=[_citation("抗性与最大抗性的结算规则", _render_rules(), formula_url)],
            meta={"channel": "derived", "table": "game_constant"},
        )

    try:
        result = res.resistance(
            element or "火焰",
            base_specific=float(base_specific),
            base_elemental=float(base_elemental),
            resist_penalty=float(resist_penalty),
            increased_pct=float(increased_pct),
            more_mult=float(more_mult),
            campaign_penalty=float(campaign_penalty),
            extra_max_specific=float(extra_max_specific),
            extra_max_elemental=float(extra_max_elemental),
            max_override=None if max_override is None else float(max_override),
            enemy_penetration=float(enemy_penetration),
        )
    except res.UnknownElement as exc:
        return ToolResult(ok=False, error=str(exc))
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"抗性常量未入库：{exc}")

    title = f"{result.element_zh}抗性结算（{result.final}%）"
    return ToolResult(
        ok=True,
        data={
            "found": True,
            "kind": "result",
            "element": result.element_zh,
            "inputs": given,
            "total": result.total,
            "max_resist": result.max_resist,
            "floor": result.floor,
            "final": result.final,
            "over_cap": result.over_cap,
            "missing": result.missing,
            "taken_mult": round(result.taken_mult, 4),
            "boundary": _BOUNDARY,
        },
        citations=[_citation(title, _render_result(result, given), formula_url)],
        meta={"channel": "derived", "table": "game_constant"},
    )


# ── 受击结算：闪避 / 格挡 / 法术压制 / 护甲 ────────────────────────

#: 受击顺序。它本身就是答案的一部分：顺序错了结论会反向
#: （最典型的是「护甲能不能减元素伤害」——护甲作用在减伤那一段，
#: 默认只对物理，除非有「护甲也作用于某类伤害」的词缀）。
_CHAIN = (
    "一次攻击落到身上的结算顺序：**能不能命中（闪避）→ 能不能挡下（格挡）"
    "／法术能不能被压制 → 打中了减多少（护甲与物理减伤）→ 抗性**。"
    "抗性那一段用 `resistance_calc`。"
)

_MITIGATION_BOUNDARY = (
    "只按 PoB 的受击公式复算：输入逐字取自问题，没给的项不算；"
    "**护甲减伤依赖这一击有多大**，不给伤害数值就没有百分比可言；"
    "**不含头目的护甲/闪避倍率**（PoB 那是按一份头目清单现算的均值，本库没有那份清单）、"
    "不含「不能被闪避」这类旗标，也不读取角色档案、不给装备推荐与价格"
)


def _mitigation_sources() -> list[str]:
    """四段公式各自的同 commit 行号引用 + 用到的常量出处。"""
    cap = constants.provenance(constants.MAX_RESIST_CAP)
    url = cap.url

    def anchor(lines: tuple[int, int]) -> str:
        return pob_url(url, mit.DEFENCE_FILE, lines=lines)

    accuracy = constants.provenance(constants.MONSTER_ACCURACY_TABLE)
    return [
        f"- 命中率公式 `calcs.hitChance`：{anchor(mit.HIT_CHANCE_LINES)}",
        f"- 闪避率与 {constants.get(constants.EVADE_CHANCE_CAP):g}% 上限："
        f"{anchor(mit.EVADE_LINES)}",
        f"- 护甲减伤 `calcs.armourReduction`：{anchor(mit.ARMOUR_LINES)}",
        f"- 物理减伤上限 {constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION):g}%："
        f"{constants.provenance(constants.MAX_PHYS_DAMAGE_REDUCTION).key}"
        f"（GGG 原始表 {constants.provenance(constants.MAX_PHYS_DAMAGE_REDUCTION).dat_source or '未标注'}）",
        f"- 格挡上限：基础最大 {constants.get(constants.MAX_BLOCK):g}%、"
        f"硬上限 {constants.get(constants.BLOCK_CHANCE_CAP):g}%（`pobMisc.BlockChanceCap`）"
        f" {anchor(mit.BLOCK_LINES)}",
        f"- 法术压制：几率上限 {constants.get(constants.SUPPRESSION_CHANCE_CAP):g}%、"
        f"效果 {constants.get(constants.SUPPRESSION_EFFECT):g}%（`pobMisc.SuppressionEffect`）",
        f"- 怪物命中值等级表：{accuracy.key}"
        f"（GGG 原始表 {accuracy.dat_source or '未标注'}"
        f"{'，出处为推断' if accuracy.inherited else ''}）{accuracy.url}",
    ]


def _render_mitigation_rules() -> str:
    """没给数值时：讲规则与顺序，一个数都不替用户编。"""
    return "\n".join([
        "### 受击结算的四段与它们的上限",
        "",
        _CHAIN,
        "",
        "| 环节 | 怎么算 | 上限 |",
        "| --- | --- | --- |",
        f"| 闪避 | 命中率 = 取整(命中值 /(命中值 +(闪避 / 5) ^ 0.9) × 125)，"
        f"命中率**最低 {mit.HIT_CHANCE_FLOOR}%** | 闪避率最高 "
        f"{constants.get(constants.EVADE_CHANCE_CAP):g}% |",
        f"| 格挡 | 几率取到最大格挡为止，超出部分不生效 | 基础最大 "
        f"{constants.get(constants.MAX_BLOCK):g}%，加成后不超过 "
        f"{constants.get(constants.BLOCK_CHANCE_CAP):g}% |",
        f"| 法术压制 | 触发时减 **{constants.get(constants.SUPPRESSION_EFFECT):g}%** 法术伤害 |"
        f" 几率最高 {constants.get(constants.SUPPRESSION_CHANCE_CAP):g}% |",
        f"| 护甲 | 减伤 = 护甲 /(护甲 + 5 × 这一击的伤害) | 与物理减伤合计不超过 "
        f"{constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION):g}% |",
        "",
        "两条最容易记错的：",
        "",
        f"1. **护甲不是固定百分比减伤**——同样的护甲，面对小击减得多、面对大击减得少。"
        f"所以「多少护甲够用」必须先说清这一击有多大。",
        f"2. **法术压制减的是 "
        f"{constants.get(constants.SUPPRESSION_EFFECT):g}%**，不是 50%。",
        "",
        _MITIGATION_BOUNDARY + "。",
        "",
        "来源：",
        *_mitigation_sources(),
    ])


def _render_mitigation(sections: list[tuple[str, list[str]]]) -> str:
    out: list[str] = []
    for title, body in sections:
        out.append(f"### {title}")
        out.append("")
        out.extend(body)
        out.append("")
    out.append(_CHAIN)
    out.append("")
    out.append(_MITIGATION_BOUNDARY + "。")
    out.append("")
    out.append("来源：")
    out.extend(_mitigation_sources())
    return "\n".join(out)


def _evade_section(result: mit.HitChanceResult) -> list[str]:
    body = render_steps(result.steps)
    body.append("")
    level = f"（{result.monster_level} 级怪物）" if result.monster_level else ""
    body.append(
        f"闪避 {result.evasion:g} 对命中值 {result.accuracy:g}{level}："
        f"**命中率 {result.hit_chance}%、闪避率 {result.evade_chance:g}%**。"
    )
    if result.hit_chance <= mit.HIT_CHANCE_FLOOR:
        body.append(
            f"命中率已经到下限 {mit.HIT_CHANCE_FLOOR}%：再堆闪避也降不下去"
            f"（闪避率上限 {result.evade_cap:g}%）。"
        )
    return body


def _armour_section(result: mit.ArmourResult) -> list[str]:
    body = render_steps(result.steps)
    body.append("")
    body.append(
        f"{result.armour:g} 护甲面对一次 {result.hit_damage:g} 的物理打击："
        f"**减伤 {result.reduction}%**，这一击剩 {result.damage_after:g}。"
    )
    body.append(
        "换一个大小的打击，这个百分比就变了——护甲不是固定百分比减伤。"
    )
    if result.capped:
        body.append(f"已经顶到物理减伤上限 {result.reduction_cap:g}%。")
    return body


def _block_section(result: mit.BlockResult, *, spell: bool) -> list[str]:
    kind = "法术格挡" if spell else "攻击格挡"
    body = render_steps(result.steps)
    body.append("")
    body.append(
        f"**{kind}几率 {result.chance:g}%**（最大 {result.max_block}%）。"
    )
    if result.over_cap:
        body.append(
            f"超出上限的 {result.over_cap:g}% 不生效：要让它生效得先提高**最大**格挡"
            f"（基础 {result.base_max:g}%，加成后不超过硬上限 {result.hard_cap:g}%）。"
        )
    return body


def _suppression_section(result: mit.SuppressionResult) -> list[str]:
    body = render_steps(result.steps)
    body.append("")
    body.append(
        f"压制几率 {result.chance:g}%、每次触发减 {result.effect:g}% 法术伤害，"
        f"**平均减伤 {result.average_reduction:g}%**。"
    )
    return body


@REGISTRY.register(
    name="mitigation_calc",
    description=(
        "按 Path of Building 的原式复算**受击**的前几段：闪避（含怪物等级的命中值）、"
        "格挡几率与上限、法术压制、护甲对某一击的减伤。给出逐步推导与各自的上限。\n"
        "**护甲减伤依赖这一击有多大**：问「多少护甲够用」时必须同时给出伤害数值，"
        "没有就只讲规则。\n"
        "抗性那一段用 `resistance_calc`。所有数值参数必须逐字取自用户问题，"
        "问题没提到的项不要填，也不要替它做加减。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "evasion": {"type": "number", "description": "闪避值"},
            "enemy_accuracy": {
                "type": "number",
                "description": "敌方命中值。不知道就填 `monster_level`，由怪物等级表查",
            },
            "monster_level": {
                "type": "integer",
                "description": "怪物等级（1–100）。用来查它的命中值；地图满级怪物常按 83–85 算",
            },
            "flat_evade_chance": {
                "type": "number",
                "description": "「+X% 闪避几率」这类固定几率（罕见）",
            },
            "increased_evade_pct": {
                "type": "number", "description": "「增加的闪避几率」百分比（罕见）",
            },
            "armour": {"type": "number", "description": "护甲值"},
            "hit_damage": {
                "type": "number",
                "description": "这一击的物理伤害。**算护甲减伤必须有它**",
            },
            "flat_physical_reduction": {
                "type": "number",
                "description": "另有的「物理伤害减免」百分比（耐力球、坚定等）",
            },
            "block_chance": {"type": "number", "description": "攻击格挡几率合计"},
            "spell_block_chance": {"type": "number", "description": "法术格挡几率合计"},
            "extra_max_block": {
                "type": "number", "description": "「+X% 最大格挡」之和",
            },
            "suppression_chance": {
                "type": "number", "description": "法术压制几率",
            },
        },
        "required": [],
    },
)
def mitigation_calc(
    evasion: float = 0.0,
    enemy_accuracy: float | None = None,
    monster_level: int | None = None,
    flat_evade_chance: float = 0.0,
    increased_evade_pct: float = 0.0,
    armour: float = 0.0,
    hit_damage: float = 0.0,
    flat_physical_reduction: float = 0.0,
    block_chance: float | None = None,
    spell_block_chance: float | None = None,
    extra_max_block: float = 0.0,
    suppression_chance: float | None = None,
) -> ToolResult:
    try:
        _mitigation_sources()
    except constants.MissingConstant as exc:
        return ToolResult(ok=False, error=f"受击结算常量未入库：{exc}")

    sections: list[tuple[str, list[str]]] = []
    data: dict[str, Any] = {"found": True, "kind": "result"}
    notes: list[str] = []

    if evasion:
        if enemy_accuracy is None and monster_level is None:
            # 不替用户假定一个怪物等级：85 级看着「合理」，但那是编造的前提
            notes.append(
                "给了闪避值但没给敌方命中值或怪物等级，**闪避率算不出来**："
                "闪避率取决于打你的那个怪物的命中值。"
            )
        else:
            try:
                result = mit.evade(
                    evasion,
                    accuracy=enemy_accuracy,
                    monster_level=monster_level,
                    flat_evade_chance=flat_evade_chance,
                    increased_evade_pct=increased_evade_pct,
                )
            except mit.OutOfTableRange as exc:
                return ToolResult(ok=False, error=str(exc))
            sections.append(("闪避与命中率", _evade_section(result)))
            data["evade"] = {
                "hit_chance": result.hit_chance,
                "evade_chance": result.evade_chance,
                "accuracy": result.accuracy,
                "monster_level": result.monster_level,
            }

    if armour or hit_damage:
        if not hit_damage:
            notes.append(
                "给了护甲值但没给这一击的伤害，**减伤百分比算不出来**："
                "护甲减伤 = 护甲 /(护甲 + 5 × 这一击的伤害)，同样的护甲对小击减得多、"
                "对大击减得少。"
            )
        else:
            result = mit.armour(armour, hit_damage, flat_reduction=flat_physical_reduction)
            sections.append(("护甲减伤", _armour_section(result)))
            data["armour"] = {
                "reduction": result.reduction,
                "damage_after": round(result.damage_after, 2),
                "capped": result.capped,
            }

    for chance, spell in ((block_chance, False), (spell_block_chance, True)):
        if chance is None:
            continue
        result = mit.block(chance, extra_max_block=extra_max_block, spell=spell)
        title = "法术格挡" if spell else "攻击格挡"
        sections.append((title, _block_section(result, spell=spell)))
        data["spell_block" if spell else "block"] = {
            "chance": result.chance,
            "max_block": result.max_block,
            "over_cap": result.over_cap,
        }

    if suppression_chance is not None:
        result = mit.suppression(suppression_chance)
        sections.append(("法术压制", _suppression_section(result)))
        data["suppression"] = {
            "chance": result.chance,
            "effect": result.effect,
            "average_reduction": round(result.average_reduction, 2),
        }

    if extra_max_block and block_chance is None and spell_block_chance is None:
        # 只给了「+X% 最大格挡」——按 0 几率算没有意义，讲上限本身
        result = mit.block(0.0, extra_max_block=extra_max_block)
        sections.append(("最大格挡", _block_section(result, spell=False)))
        data["block"] = {"max_block": result.max_block, "chance": 0.0, "over_cap": 0.0}

    data["boundary"] = _MITIGATION_BOUNDARY
    if not sections:
        rules = _render_mitigation_rules()
        if notes:
            rules = "\n".join([*notes, "", rules])
        return ToolResult(
            ok=True,
            data={
                "found": True,
                "kind": "rules",
                "evade_chance_cap": constants.get(constants.EVADE_CHANCE_CAP),
                "block_chance_cap": constants.get(constants.BLOCK_CHANCE_CAP),
                "suppression_effect": constants.get(constants.SUPPRESSION_EFFECT),
                "damage_reduction_cap": constants.get(constants.MAX_PHYS_DAMAGE_REDUCTION),
                "boundary": _MITIGATION_BOUNDARY,
                "missing_inputs": notes,
            },
            citations=[_citation(
                "受击结算的四段与上限", rules,
                pob_url(constants.provenance(constants.MAX_RESIST_CAP).url,
                        mit.DEFENCE_FILE, lines=mit.HIT_CHANCE_LINES),
            )],
            meta={"channel": "derived", "table": "game_constant"},
        )

    text = _render_mitigation(sections)
    if notes:
        text = "\n".join([*notes, "", text])
        data["missing_inputs"] = notes
    title = "受击结算（" + "、".join(name for name, _ in sections) + "）"
    return ToolResult(
        ok=True,
        data=data,
        citations=[_citation(
            title, text,
            pob_url(constants.provenance(constants.MAX_RESIST_CAP).url,
                    mit.DEFENCE_FILE, lines=mit.ARMOUR_LINES),
        )],
        meta={"channel": "derived", "table": "game_constant"},
    )
