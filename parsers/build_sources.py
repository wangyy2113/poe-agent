"""Build 防御来源拆分（需求 8.5 的可审计 v1）。

PoB 分享码保存的是最终 `PlayerStat`，没有保存每个属性的完整 breakdown。本模块因此
只回答一件能确定的事：装备文本里哪些**原文行**明确属于哪类防御来源。它不把装备行
求和，也不声称解释了最终面板值；天赋、技能、配置与条件效果仍明确列为未拆分来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from entities.taxonomy import ModKind
from parsers.item_text import ParsedMod
from parsers.pob_build import PobBuild


@dataclass(frozen=True)
class DefenceSourceLine:
    category: str
    label: str
    slot: str
    item_name: str
    origin: str
    detail: str


@dataclass
class DefenceSources:
    lines: list[DefenceSourceLine] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    note: str = ""


_CATEGORY_LABELS = {
    "pool": "生命/护盾池",
    "resistance": "抗性",
    "max_resistance": "最大抗性",
    "armour_evasion": "护甲/闪避",
    "avoidance": "压制/格挡",
    "recovery": "吸纳/偷取/回复",
}

_MAX_RESIST = (
    "maximum fire resistance", "maximum cold resistance",
    "maximum lightning resistance", "maximum chaos resistance",
    "maximum elemental resistances", "最大火焰抗性", "最大冰霜抗性",
    "最大闪电抗性", "最大混沌抗性", "最大元素抗性", "抗性上限",
)
_POOL = (
    "maximum life", "maximum energy shield", "最大生命", "最大能量护盾",
)
_RESIST = (
    "fire resistance", "cold resistance", "lightning resistance", "chaos resistance",
    "elemental resistance", "all resistances", "火焰抗性", "冰霜抗性", "闪电抗性",
    "chaos resistances", " resistances", "混沌抗性", "元素抗性", "所有抗性",
)
_ARMOUR_EVASION = ("armour", "evasion rating", "护甲", "護甲", "闪避值", "閃避值")
_AVOIDANCE = (
    "suppress spell damage", "spell suppression", "chance to block",
    "block attack damage", "block spell damage", "压制法术伤害", "法术压制",
    "壓制法術傷害", "法術壓制", "法术伤害压制率", "法術傷害壓制率",
    "格挡攻击伤害", "格挡法术伤害",
    "格擋攻擊傷害", "格擋法術傷害",
)
_RECOVERY = (
    "recouped as", "leeched as", "regenerate", "regeneration rate", "recovery rate",
    "recharge rate", "吸纳", "吸納", "偷取", "再生", "回复率", "回復率", "充能率",
)
_OFFENSIVE_RESIST = (
    "enemy resistance", "enemies have", "nearby enemies", "敌人的抗性", "敵人的抗性",
    "附近的敌人", "附近的敵人",
)


def _categories(text: str) -> tuple[str, ...]:
    """保守关键词分类；只匹配词缀原文，不由数值反推来源。"""
    lowered = text.casefold()
    out: list[str] = []
    if any(token in lowered for token in _POOL):
        out.append("pool")
    has_max_resist = any(token in lowered for token in _MAX_RESIST)
    if has_max_resist:
        out.append("max_resistance")
    if (
        any(token in lowered for token in _RESIST)
        and not has_max_resist
        and not any(token in lowered for token in _OFFENSIVE_RESIST)
    ):
        out.append("resistance")
    if any(token in lowered for token in _ARMOUR_EVASION):
        out.append("armour_evasion")
    if any(token in lowered for token in _AVOIDANCE):
        out.append("avoidance")
    if any(token in lowered for token in _RECOVERY):
        out.append("recovery")
    return tuple(out)


def _origin(mod: ParsedMod) -> str:
    lowered = mod.text.casefold()
    # PoB 存档方言会把来源写成前缀 `{crafted}`，而游戏复制格式写成 `(crafted)`。
    # 解析器保留前者原文，因此这里也按原文明确标出，不把它降成普通显式。
    for marker, label in (
        ("{crafted}", "工艺词缀"),
        ("{fractured}", "裂隙词缀"),
        ("{implicit}", "隐式"),
    ):
        if marker in lowered:
            return label
    return {
        ModKind.IMPLICIT: "隐式",
        ModKind.CORRUPTED_IMPLICIT: "腐化隐式",
        ModKind.SYNTHESISED: "合成隐式",
        ModKind.ENCHANT: "附魔",
        ModKind.CRAFTED: "工艺词缀",
        ModKind.FRACTURED: "裂隙词缀",
    }.get(mod.kind, "显式")


def build_defence_sources(build: PobBuild) -> DefenceSources:
    """列出装备侧可见来源；不求和、不与最终 PlayerStat 做虚假对账。"""
    out = DefenceSources()
    seen: set[tuple[str, str, str, str]] = set()
    for item in build.items:
        parsed = item.parsed
        if parsed is None:
            continue
        item_name = parsed.display_name or "未命名装备"

        for prop, prop_label, category in (
            ("armour", "护甲", "armour_evasion"),
            ("evasion", "闪避值", "armour_evasion"),
            ("energy_shield", "能量护盾", "pool"),
            ("block_chance", "格挡率", "avoidance"),
        ):
            value = parsed.properties.get(prop)
            if not value:
                continue
            detail = f"{prop_label}: {value}"
            key = (category, item.slot, item_name, detail)
            if key not in seen:
                seen.add(key)
                out.lines.append(DefenceSourceLine(
                    category=category, label=_CATEGORY_LABELS[category],
                    slot=item.slot, item_name=item_name, origin="底材属性", detail=detail,
                ))

        for mod in parsed.all_mods:
            for category in _categories(mod.text):
                key = (category, item.slot, item_name, mod.text)
                if key in seen:
                    continue
                seen.add(key)
                out.lines.append(DefenceSourceLine(
                    category=category, label=_CATEGORY_LABELS[category],
                    slot=item.slot, item_name=item_name,
                    origin=_origin(mod), detail=mod.text,
                ))

    if build.passive_nodes:
        out.unresolved.append("天赋树：输入有节点 id，但本地节点图没有逐节点属性 breakdown")
    if build.skill_groups:
        out.unresolved.append("技能与辅助：输入有宝石配置，但没有逐效果对最终防御的贡献值")
    if build.config:
        out.unresolved.append("配置与条件效果：会影响 PlayerStat，但分享码不保存逐项贡献 breakdown")
    out.unresolved.append("升华、药剂及其它条件来源：当前输入没有逐项防御贡献，无法归因")
    out.note = (
        "这里只列装备文本中可逐字核对的防御来源，不求和，也不声称这些行等于最终面板；"
        + (
            "最终数值仍以 PlayerStat 为准。"
            if build.player_stats else
            "当前输入没有 PlayerStat，无法与最终面板对账。"
        )
        + "未列出的来源不是 0，而是输入没有提供 breakdown。"
    )
    return out
