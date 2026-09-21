"""Harvest 材料转换与牺牲操作的保守状态转移。

覆盖 ``harvest_craft`` 中剩余 14 条材料/宝石随机转换与 3 条牺牲原文。
随机结果、堆叠费用缩放基准和未写明的数量保持未知，不从目录文字推导概率。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from craft.capacity import normalize_rarity
from craft.harvest_operations import HarvestCheck, HarvestCost

HarvestExchangeVerdict = Literal["compatible", "incompatible", "unknown"]


@dataclass(frozen=True)
class HarvestExchangeSpec:
    key: str
    effect_text: str
    mode: str
    input_label: str
    output_relation: str
    stack_scaled: bool = False


EXCHANGE_SPECS: tuple[HarvestExchangeSpec, ...] = (
    HarvestExchangeSpec(
        "support_gem_same_colour",
        "将一个辅助宝石转变为一个相同颜色的随机辅助宝石",
        "random_conversion", "辅助宝石", "相同颜色的随机辅助宝石",
    ),
    HarvestExchangeSpec(
        "special_fragment_pair",
        "将一叠恐怖碎片或空虚碎片转变为另一种碎片。或者将一叠塑形碎片或知识碎片转变为另一种碎片。消耗与堆叠数量成比例。",
        "stack_conversion", "指定成对碎片", "来源原文所列对应组内的另一种碎片", True,
    ),
    HarvestExchangeSpec(
        "catalyst", "将一堆催化剂变为另一种类型的催化剂。消耗与堆叠数量成比例。",
        "stack_conversion", "催化剂", "另一种类型的催化剂", True,
    ),
    HarvestExchangeSpec(
        "fossil", "将一堆化石变为另一种类型的化石。新的化石类型可能堆叠数量较小。消耗与堆叠数量成比例。",
        "stack_conversion", "化石", "另一种类型的化石；输出堆叠数量可能较小", True,
    ),
    HarvestExchangeSpec(
        "oil", "将一堆圣油变为另一种颜色的圣油。消耗与堆叠数量成比例。",
        "stack_conversion", "圣油", "另一种颜色的圣油", True,
    ),
    HarvestExchangeSpec(
        "shaper_fragment", "将一堆塑界者碎片变为其他随机的塑界者碎片。消耗与堆叠数量成比例。",
        "stack_conversion", "塑界者碎片", "其他随机的塑界者碎片", True,
    ),
    HarvestExchangeSpec(
        "conqueror_fragment", "将一堆征服者碎片变为其他随机的征服者碎片。消耗与堆叠数量成比例。",
        "stack_conversion", "征服者碎片", "其他随机的征服者碎片", True,
    ),
    HarvestExchangeSpec(
        "legion_fragment_or_emblem", "将一堆永恒碎片或永恒印记变为另一种类型的碎片或印记。消耗与堆叠数量成比例。",
        "stack_conversion", "永恒碎片或永恒印记", "另一种类型的碎片或印记", True,
    ),
    HarvestExchangeSpec(
        "offering_or_mortal_fragment", "将一堆祭品或凡人碎片变为同一类型的其他随机碎片。消耗与堆叠数量成比例。",
        "stack_conversion", "祭品或凡人碎片", "同一类型的其他随机碎片", True,
    ),
    HarvestExchangeSpec(
        "essence", "将一堆精华变为另一种类型但位阶相同的精华。消耗与堆叠数量成比例。",
        "stack_conversion", "精华", "另一种类型但位阶相同的精华", True,
    ),
    HarvestExchangeSpec(
        "elder_fragment", "将一堆裂界者碎片变为其他随机的裂界者碎片。消耗与堆叠数量成比例。",
        "stack_conversion", "裂界者碎片", "其他随机的裂界者碎片", True,
    ),
    HarvestExchangeSpec(
        "league_sextant", "将一堆赛季专属星盘变为另一种类型的赛季专属星盘。消耗与堆叠数量成比例。",
        "stack_conversion", "赛季专属星盘", "另一种类型的赛季专属星盘", True,
    ),
    HarvestExchangeSpec(
        "delirium_orb", "将一堆雾魇宝珠变为另一种类型的雾魇宝珠。消耗与堆叠数量成比例。",
        "stack_conversion", "雾魇宝珠", "另一种类型的雾魇宝珠", True,
    ),
    HarvestExchangeSpec(
        "divination_card_random", "将一张 {命运卡}变为另一张随机命运卡",
        "random_conversion", "一张命运卡", "另一张随机命运卡",
    ),
    HarvestExchangeSpec(
        "corrupted_rare_map", "献祭一张稀有腐化的地图，生成一张相同位阶和稀有度的新腐化的地图。",
        "map_sacrifice", "稀有腐化地图", "相同位阶、相同稀有度的新腐化地图",
    ),
    HarvestExchangeSpec(
        "corrupted_gem_experience", "献祭一颗腐化技能石，获得其总存量经验的50%，该经验以费斯特的透镜形式储存。",
        "gem_experience_sacrifice", "腐化技能石", "储存 50% 总存量经验的费斯特的透镜",
    ),
    HarvestExchangeSpec(
        "divination_card_gamble", "献祭最多半叠命运卡，获得相同命运卡的数量可能为0，也可能翻倍。",
        "card_gamble", "最多半叠命运卡", "0 到投入数量两倍的相同命运卡",
    ),
)
SUPPORTED_EXCHANGE_EFFECTS = frozenset(spec.effect_text for spec in EXCHANGE_SPECS)
_BY_EFFECT = {spec.effect_text: spec for spec in EXCHANGE_SPECS}
_BY_KEY = {spec.key: spec for spec in EXCHANGE_SPECS}

SELECTOR_ALIASES = {
    "辅助宝石同色转换": "support_gem_same_colour",
    "特殊碎片转换": "special_fragment_pair",
    "催化剂转换": "catalyst",
    "化石转换": "fossil",
    "圣油转换": "oil",
    "塑界者碎片转换": "shaper_fragment",
    "征服者碎片转换": "conqueror_fragment",
    "永恒碎片印记转换": "legion_fragment_or_emblem",
    "祭品凡人碎片转换": "offering_or_mortal_fragment",
    "精华转换": "essence",
    "裂界者碎片转换": "elder_fragment",
    "赛季星盘转换": "league_sextant",
    "雾魇宝珠转换": "delirium_orb",
    "命运卡随机转换": "divination_card_random",
    "腐化地图牺牲": "corrupted_rare_map",
    "腐化技能石经验": "corrupted_gem_experience",
    "命运卡赌博": "divination_card_gamble",
}


@dataclass(frozen=True)
class HarvestExchangeState:
    quantity: int | None = None
    max_stack_size: int | None = None
    rarity: str | None = None
    corrupted: bool | None = None
    map_tier: int | None = None
    gem_total_experience: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HarvestExchangeResult:
    verdict: HarvestExchangeVerdict
    craft_id: str
    effect_text: str
    operation_key: str
    mode: str
    input_label: str
    output_relation: str
    source: str
    source_priority: int
    source_url: str
    fetched_at: str
    costs: list[HarvestCost]
    before: HarvestExchangeState
    after: dict[str, Any]
    checks: list[HarvestCheck] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)
    boundary: str = (
        "compatible 只表示已提供状态通过 POEDB 效果原文明示的前提；原始费用不是市场成本，"
        "随机结果不换算概率，按堆叠数量成比例不擅自补费用缩放基准"
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "craft_id": self.craft_id,
            "effect_text": self.effect_text,
            "operation_key": self.operation_key,
            "mode": self.mode,
            "input_label": self.input_label,
            "output_relation": self.output_relation,
            "source": self.source,
            "source_priority": self.source_priority,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "costs": [cost.as_dict() for cost in self.costs],
            "before": self.before.as_dict(),
            "after": self.after,
            "checks": [check.as_dict() for check in self.checks],
            "unknown_fields": self.unknown_fields,
            "boundary": self.boundary,
        }


def _selector_effect(selector: str) -> str:
    value = (selector or "").strip()
    if value in SUPPORTED_EXCHANGE_EFFECTS:
        return value
    compact = value.replace(" ", "")
    key = SELECTOR_ALIASES.get(compact, compact if compact in _BY_KEY else "")
    return _BY_KEY[key].effect_text if key in _BY_KEY else value


def _resolve(conn: sqlite3.Connection, selector: str) -> tuple[sqlite3.Row, HarvestExchangeSpec] | None:
    effect = _selector_effect(selector)
    spec = _BY_EFFECT.get(effect)
    if spec is None:
        return None
    rows = conn.execute(
        "SELECT * FROM harvest_craft WHERE effect_text=? ORDER BY craft_id", (effect,)
    ).fetchall()
    return (rows[0], spec) if len(rows) == 1 else None


def _costs(conn: sqlite3.Connection, craft_id: str) -> list[HarvestCost]:
    rows = conn.execute(
        "SELECT amount,currency_name_zh,currency_metadata_id,currency_url "
        "FROM harvest_craft_cost WHERE craft_id=? ORDER BY position",
        (craft_id,),
    ).fetchall()
    return [HarvestCost(
        amount=int(row["amount"]),
        currency_name_zh=str(row["currency_name_zh"]),
        currency_metadata_id=str(row["currency_metadata_id"]),
        currency_url=str(row["currency_url"]),
    ) for row in rows]


def _quantity_check(state: HarvestExchangeState, url: str) -> HarvestCheck:
    if state.quantity is None:
        return HarvestCheck("quantity", "unknown", "需要提供投入堆叠数量", url)
    if state.quantity < 1:
        return HarvestCheck("quantity", "fail", "投入数量必须至少为 1", url)
    return HarvestCheck("quantity", "pass", f"投入数量为 {state.quantity}", url)


def evaluate_harvest_exchange(
    conn: sqlite3.Connection,
    selector: str,
    state: HarvestExchangeState,
) -> HarvestExchangeResult | None:
    resolved = _resolve(conn, selector)
    if resolved is None:
        return None
    row, spec = resolved
    url = str(row["url"])
    checks = [HarvestCheck(
        "source_contract", "pass", "POEDB 效果原文与已审计转换/牺牲语义逐字一致", url,
    )]
    unknown_fields: list[str] = []
    after: dict[str, Any] = {
        "output_relation": spec.output_relation,
        "output_quantity": None,
        "output_quantity_min": None,
        "output_quantity_max": None,
        "preserved_rarity": None,
        "preserved_map_tier": None,
        "stored_experience": None,
        "cost_scaling": "proportional_to_stack_size" if spec.stack_scaled else "fixed_catalog_cost",
    }

    if spec.mode == "stack_conversion":
        checks.append(_quantity_check(state, url))
        unknown_fields.extend((
            "随机得到的具体类型与概率",
            "完整费用的堆叠缩放基准",
            "输出堆叠数量" if spec.key != "fossil" else "可能缩小后的输出堆叠数量",
        ))
    elif spec.mode == "random_conversion":
        unknown_fields.append("随机得到的具体结果与概率")
    elif spec.mode == "map_sacrifice":
        rarity = normalize_rarity(state.rarity)
        if rarity is None:
            checks.append(HarvestCheck("rarity", "unknown", "需要确认地图稀有度为稀有", url))
        elif rarity != "稀有":
            checks.append(HarvestCheck("rarity", "fail", f"当前是{rarity}地图，原文要求稀有地图", url))
        else:
            checks.append(HarvestCheck("rarity", "pass", "当前地图稀有度为稀有", url))
        if state.corrupted is None:
            checks.append(HarvestCheck("corrupted", "unknown", "需要确认地图已腐化", url))
        elif not state.corrupted:
            checks.append(HarvestCheck("corrupted", "fail", "当前地图未腐化，原文要求腐化地图", url))
        else:
            checks.append(HarvestCheck("corrupted", "pass", "当前地图已腐化", url))
        if state.map_tier is None:
            checks.append(HarvestCheck("map_tier", "unknown", "需要提供地图位阶以冻结输出位阶", url))
        elif state.map_tier < 1:
            checks.append(HarvestCheck("map_tier", "fail", "地图位阶必须至少为 1", url))
        else:
            checks.append(HarvestCheck("map_tier", "pass", f"当前地图为 {state.map_tier} 阶", url))
            after["preserved_map_tier"] = state.map_tier
        after["preserved_rarity"] = "稀有"
        after["corrupted"] = True
        unknown_fields.append("生成的新地图具体名称、词缀与布局")
    elif spec.mode == "gem_experience_sacrifice":
        if state.corrupted is None:
            checks.append(HarvestCheck("corrupted", "unknown", "需要确认技能石已腐化", url))
        elif not state.corrupted:
            checks.append(HarvestCheck("corrupted", "fail", "当前技能石未腐化，原文要求腐化技能石", url))
        else:
            checks.append(HarvestCheck("corrupted", "pass", "当前技能石已腐化", url))
        experience = state.gem_total_experience
        if experience is None:
            checks.append(HarvestCheck("gem_total_experience", "unknown", "需要提供技能石总存量经验", url))
        elif experience < 0:
            checks.append(HarvestCheck("gem_total_experience", "fail", "总存量经验不能为负数", url))
        elif experience % 2:
            checks.append(HarvestCheck(
                "gem_total_experience", "unknown", "总存量经验为奇数；来源未说明 50% 的取整方式", url,
            ))
            unknown_fields.append("奇数经验按 50% 转换时的取整方式")
        else:
            checks.append(HarvestCheck(
                "gem_total_experience", "pass", f"总存量经验 {experience} 可精确取 50%", url,
            ))
            after["stored_experience"] = experience // 2
    elif spec.mode == "card_gamble":
        checks.append(_quantity_check(state, url))
        if state.max_stack_size is None:
            checks.append(HarvestCheck("max_stack_size", "unknown", "需要提供该命运卡的堆叠上限", url))
        elif state.max_stack_size < 1:
            checks.append(HarvestCheck("max_stack_size", "fail", "堆叠上限必须至少为 1", url))
        elif state.quantity is not None and state.quantity > state.max_stack_size // 2:
            checks.append(HarvestCheck(
                "half_stack_limit", "fail",
                f"投入 {state.quantity} 张超过堆叠上限 {state.max_stack_size} 的一半", url,
            ))
        elif state.quantity is not None and state.quantity >= 1:
            checks.append(HarvestCheck(
                "half_stack_limit", "pass",
                f"投入 {state.quantity} 张未超过堆叠上限 {state.max_stack_size} 的一半", url,
            ))
            after["output_quantity_min"] = 0
            after["output_quantity_max"] = state.quantity * 2
        unknown_fields.append("0 到翻倍之间各结果的概率")

    costs = _costs(conn, str(row["craft_id"]))
    if not costs:
        checks.append(HarvestCheck("cost_contract", "unknown", "该目录项缺少原始费用子项"))
    else:
        checks.append(HarvestCheck(
            "cost_contract", "pass",
            "页面原始费用：" + " + ".join(f"{cost.amount}x {cost.currency_name_zh}" for cost in costs),
            url,
        ))

    if any(check.status == "fail" for check in checks):
        verdict: HarvestExchangeVerdict = "incompatible"
    elif any(check.status == "unknown" for check in checks):
        verdict = "unknown"
    else:
        verdict = "compatible"

    return HarvestExchangeResult(
        verdict=verdict,
        craft_id=str(row["craft_id"]),
        effect_text=spec.effect_text,
        operation_key=spec.key,
        mode=spec.mode,
        input_label=spec.input_label,
        output_relation=spec.output_relation,
        source=str(row["source"]),
        source_priority=int(row["source_priority"]),
        source_url=url,
        fetched_at=str(row["fetched_at"]),
        costs=costs,
        before=state,
        after=after,
        checks=checks,
        unknown_fields=list(dict.fromkeys(unknown_fields)),
    )
