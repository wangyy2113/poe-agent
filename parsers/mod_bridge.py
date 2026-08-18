"""英文词缀 → POEDB 中文词缀档位的桥接（4.2 的补充路径）。

## 为什么需要它

T 级只存在于 POEDB 侧（`mod_affix.tier`），而 POEDB 是中文站，词缀文本是中文。
PoB 侧（`pob_mod`）有英文文本但**没有 tier 列**。所以英文客户端的普通复制文本
本来一条 T 级都拿不到。

## 为什么不按词缀组名桥

实测两侧组名对不上：POEDB 用 `PhysicalDamage`，PoB 用 `LocalPhysicalDamage`；
`mod_affix` 的 1578 个组只有 856 个在 `pob_mod` 里有同名组，掉落词缀更低
（930 个里只有 107 个，11.5%）。局部武器词缀是**系统性**命名不同，不是个别遗漏。
按组名硬桥会在一部分词缀上悄悄给出错误 T 级。

## 改按「阶梯指纹」桥

同一条词缀在两个源里是同一份 GGG 数据，所以它的**整条档位阶梯**应当逐级相同：
每一级的 `ilvl` 与数值区间都一样。于是：

    英文文本 → pob_mod 严格模板匹配 → 该词缀所在组的完整阶梯（ilvl + 区间序列）
            → 在 mod_affix 里找同底材类别、阶梯指纹完全相同的组 → 取 T 级

这是**数值级的全等比对**，不依赖任何命名约定。指纹对不上就没有桥，不猜。

## 剩余风险与处理

两条不同词缀在同一底材类别上恰好拥有完全相同的阶梯（每级 ilvl 与区间都一样）
时会撞车——例如某些元素伤害词缀。这时**不选**，返回歧义并列出全部可能，
由上层如实呈现。宁可说「可能是 T2 也可能是 T3」，不能挑一个看着顺眼的。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from dataclasses import dataclass, field

from kb.sources.poedb_mods import loose_templatize, strict_template_regex


@dataclass
class BridgeCandidate:
    """桥接命中的一条 POEDB 词缀档位。"""

    tier: int
    tier_count: int
    ilvl: int
    ranges: list[tuple[float, float]]
    text_zh: str
    correct_group: str
    category: str
    category_label: str
    gen_label: str
    weight: int
    weight_pct: float
    url: str
    item_class_page: str
    loose_template: str = ""

    @property
    def tier_label(self) -> str:
        return f"T{self.tier}/{self.tier_count}" if self.tier_count else f"T{self.tier}"


@dataclass
class BridgeResult:
    text_en: str
    #: PoB 侧命中的词缀组（可能多个：同一句英文能对上多个组时本身就是歧义）
    pob_groups: list[str] = field(default_factory=list)
    matched: BridgeCandidate | None = None
    alternatives: list[BridgeCandidate] = field(default_factory=list)
    ambiguous: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.matched is not None


# ── 阶梯指纹原语。**公开**，因为不止一处需要它 ──────────────────────
#
# 两源配对只有两种可能的判据：组名，或数值阶梯。组名不行——本模块顶部那段实测写着
# 掉落词缀里同名组只占 11.5%，局部武器词缀是系统性命名不同。所以判据只能是阶梯。
#
# 提成公开 API 是因为 `scripts/verify_pob_mods.py`（跨来源对账）此前**按组名配对**，
# 与本模块的结论直接矛盾，代价是 2729 条 `mod_review` 里 89.2% 的行在 PoB 侧根本
# 没有对应阶梯——那不是数值冲突，是配对配错了。同一个概念不允许有两份判据。


def slot_key(ilvl: int, ranges: list[tuple[float, float]]) -> str:
    """一个档位的规范化键：物品等级 + 区间序列。"""
    return f"{ilvl}:" + ",".join(f"{lo:g}-{hi:g}" for lo, hi in ranges)


def ladder_slots(rows: list[tuple[int, list[tuple[float, float]]]]) -> set[str]:
    """一条词缀阶梯的指纹：全部 (ilvl, 区间) 档位的集合。"""
    return {slot_key(ilvl, ranges) for ilvl, ranges in rows}


def ladder_overlap(a: set[str], b: set[str]) -> float:
    """两条阶梯的 Jaccard 重合度。1.0 表示逐档全等，0 表示毫无共同档位。"""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def norm_ranges(raw: Any) -> tuple[tuple[float, float], ...]:
    """区间归一为可比的元组。**两处对账脚本共用这一份**。

    做两件归一，各有实测依据：

    **整数与浮点混写**（40 vs 40.0）不该算冲突——两源一个来自 HTML 文本、一个来自 Lua，
    数字字面写法必然不同。

    **端点排序**（(50, 40) → (40, 50)）：区间是一组取值，端点顺序是**呈现**而不是事实。
    PoB 对「减益」类词缀按降序存（`(50-40)% reduced ...`），POEDB 写成 (40-50)。
    不排序就会把 `Song of the Sirens` 的 [40,50] 与 [50,40]、`Plume of Pursuit` 的
    [20,30] 与 [30,20] 报成数值分歧——那是我们自己的归一化缺口，不是两源打架。

    **符号不动。** POEDB 把「减少 15%」写成 -15 而 PoB 存 +15 的量值，这是**约定差异**，
    但把符号也抹掉就等于放弃了「一侧把加号看成减号」这类真错误的检测能力。
    约定差异由调用方单独识别并如实归类（见 `scripts/verify_pob_uniques.py`）。
    """
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    out: list[tuple[float, float]] = []
    for pair in raw or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        a, b = pair
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        lo, hi = round(float(a), 4), round(float(b), 4)
        out.append((lo, hi) if lo <= hi else (hi, lo))
    return tuple(out)


def same_magnitude(
    a: tuple[tuple[float, float], ...], b: tuple[tuple[float, float], ...]
) -> bool:
    """两组区间的**量值**是否逐项相同（忽略符号）。

    用于识别符号约定差异：POEDB「移动速度减少 (15-10)%」记 -15..-10，
    PoB 存 15..10 并靠文本里的 `reduced` 表达方向。量值一致即约定差异，
    不是数值分歧——但必须**单独归类**而不是并入「一致」，否则真的符号错误也会被吞掉。
    """
    if len(a) != len(b) or not a:
        return False
    # 量值也要**排序后**比：`norm_ranges` 已把端点升序，于是 (-15, -10) 的绝对值序是
    # (15, 10) 而 (10, 15) 的是 (10, 15)——不排序就永远判不相等（第一版就是这个 bug）。
    return all(
        tuple(sorted((abs(x[0]), abs(x[1])))) == tuple(sorted((abs(y[0]), abs(y[1]))))
        for x, y in zip(a, b, strict=True)
    )


def parse_ranges_json(raw: str | None) -> list[tuple[float, float]]:
    return [(float(a), float(b)) for a, b in json.loads(raw or "[]")]


#: 旧的私有别名，保留给本模块内部既有调用点，避免这次改动牵连桥接逻辑。
_slot_key = slot_key
_ladder_slots = ladder_slots
_parse_ranges_json = parse_ranges_json


def bridge_english_mod(
    conn: sqlite3.Connection,
    text_en: str,
    item_class_pages: list[str],
) -> BridgeResult:
    """英文词缀文本 → POEDB 档位。查不到就 `matched=None` 且给出原因。"""
    result = BridgeResult(text_en=text_en)
    if not item_class_pages:
        result.reason = "底材类别未知，无法限定档位阶梯的比对范围"
        return result

    loose = loose_templatize(text_en)
    rows = conn.execute(
        "SELECT * FROM pob_mod WHERE loose_template = ? AND mod_kind = 'explicit'",
        [loose],
    ).fetchall()
    if not rows:
        result.reason = f"PoB 英文词缀库里没有模板 {loose!r} 的词缀"
        return result

    # ── PoB 侧：严格模板校验 + 区间覆盖，确定这行文本属于哪个词缀组 ──
    groups: dict[str, list[tuple[int, list[tuple[float, float]]]]] = {}
    matched_values: list[float] = []
    for row in rows:
        template = row["text_template"] or ""
        if not template:
            continue
        m = strict_template_regex(template).match(text_en)
        if m is None:
            continue
        values = [float(g) for g in m.groups()]
        ranges = _parse_ranges_json(row["ranges"])
        if len(values) != len(ranges):
            continue
        if not all(lo <= v <= hi for v, (lo, hi) in zip(values, ranges, strict=True)):
            continue
        group = row["mod_group"] or ""
        if not group:
            continue
        groups.setdefault(group, [])
        matched_values = values

    if not groups:
        result.reason = (
            f"PoB 侧召回 {len(rows)} 条，但没有一条的固定文本与数值区间同时吻合"
        )
        return result
    result.pob_groups = sorted(groups)

    # ── 取每个命中组的完整阶梯（PoB 侧是所有物品类型的并集）──
    pob_slots: set[str] = set()
    for group in groups:
        ladder = [
            (int(r["ilvl"] or 0), _parse_ranges_json(r["ranges"]))
            for r in conn.execute(
                """SELECT ilvl, ranges FROM pob_mod
                   WHERE mod_group = ? AND mod_kind = 'explicit'""",
                [group],
            )
        ]
        pob_slots |= _ladder_slots(ladder)

    # ── POEDB 侧：**按页面分别**算指纹，找全等的 ──
    # 必须按页面分开：一个中文类别对应多个底材页面（手套有 7 个），不同页面的
    # 阶梯可能不同，混在一起算会得到一条比 PoB 更长的阶梯，于是永远对不上。
    zh_ladders: dict[tuple[str, str, str], list[tuple[int, list[tuple[float, float]]]]] = {}
    placeholders = ",".join("?" * len(item_class_pages))
    for row in conn.execute(
        f"""SELECT item_class_page, correct_group, category, ilvl, ranges FROM mod_affix
            WHERE item_class_page IN ({placeholders}) AND correct_group != ''""",  # noqa: S608
        item_class_pages,
    ):
        key = (row["item_class_page"], row["category"], row["correct_group"])
        zh_ladders.setdefault(key, []).append(
            (int(row["ilvl"] or 0), _parse_ranges_json(row["ranges"]))
        )

    # PoB 的组覆盖所有物品类型，POEDB 的阶梯只覆盖一个底材页面，所以正确的关系是
    # **POEDB 阶梯 ⊆ PoB 阶梯**，不是全等。要求全等会让绝大多数词缀对不上。
    # 单档位的阶梯不作依据：一个 (ilvl, 区间) 的巧合概率太高，构不成指纹。
    hits: list[tuple[str, str]] = sorted(
        {
            (category, group)
            for (_, category, group), ladder in zh_ladders.items()
            if (slots := _ladder_slots(_dedupe(ladder))) and len(slots) >= 2
            and slots <= pob_slots
        }
    )
    if not hits:
        result.reason = (
            "PoB 与 POEDB 两侧的档位阶梯对不上（POEDB 该类别的每一档都要能在 PoB 侧"
            "找到 ilvl 与区间全同的档位），无法确定该英文词缀对应哪条中文词缀，"
            "因此不给 T 级"
        )
        return result

    # ── 在命中的组里按实际数值定档 ──
    candidates: list[BridgeCandidate] = []
    for category, group in hits:
        for row in conn.execute(
            f"""SELECT * FROM mod_affix
                WHERE correct_group = ? AND category = ?
                  AND item_class_page IN ({placeholders})
                ORDER BY tier""",  # noqa: S608
            [group, category, *item_class_pages],
        ):
            ranges = _parse_ranges_json(row["ranges"])
            if len(ranges) != len(matched_values):
                continue
            if not all(
                lo <= v <= hi for v, (lo, hi) in zip(matched_values, ranges, strict=True)
            ):
                continue
            candidates.append(
                BridgeCandidate(
                    tier=int(row["tier"] or 0),
                    tier_count=int(row["tier_count"] or 0),
                    ilvl=int(row["ilvl"] or 0),
                    ranges=ranges,
                    text_zh=row["text_zh"] or "",
                    correct_group=row["correct_group"] or "",
                    category=row["category"],
                    category_label=row["category_label"] or row["category"],
                    gen_label=row["gen_label"] or "",
                    weight=int(row["weight"] or 0),
                    weight_pct=float(row["weight_pct"] or 0.0),
                    url=row["url"] or "",
                    item_class_page=row["item_class_page"] or "",
                    loose_template=row["loose_template"] or "",
                )
            )

    if not candidates:
        result.reason = (
            "阶梯指纹对上了，但装备上的实际数值不落在任何一档区间内"
            "（可能被品质或工艺台改动过）"
        )
        return result

    # 同一 (category, group) 在多个底材页面上重复出现是正常的，按档位去重
    by_slot: dict[tuple[str, str, int], BridgeCandidate] = {}
    for c in candidates:
        by_slot.setdefault((c.category, c.correct_group, c.tier), c)
    distinct = list(by_slot.values())

    tiers = {c.tier for c in distinct}
    groups_hit = {(c.category, c.correct_group) for c in distinct}
    if len(tiers) == 1 and len(groups_hit) == 1:
        result.matched = distinct[0]
        result.reason = (
            f"经阶梯指纹比对到 POEDB 的「{distinct[0].text_zh}」，"
            f"{distinct[0].tier_label}"
        )
        return result

    # 多解：取最可能的一项并**同时列出其余可能**，与中文路径
    # （`entities.mod_lookup` 的歧义处理）保持一致——两条路径对同一件装备
    # 给出不同口径会让人无法判断该信哪个。
    distinct.sort(
        key=lambda c: ({"normal": 0, "master": 1, "essence": 2}.get(c.category, 3), c.tier)
    )
    result.matched = distinct[0]
    result.alternatives = distinct[1:]
    result.ambiguous = True
    result.reason = (
        f"阶梯指纹比对出 {len(groups_hit)} 个候选词缀组、{len(tiers)} 个可能档位"
        f"（{'、'.join(f'T{t}' for t in sorted(tiers))}）；已取最可能的"
        f"{distinct[0].tier_label}（{distinct[0].category_label}），其余可能一并列出"
    )
    return result


def _dedupe(
    ladder: list[tuple[int, list[tuple[float, float]]]],
) -> list[tuple[int, list[tuple[float, float]]]]:
    """同一档位会因多个底材页面重复出现，去重后才能与 PoB 的阶梯等长比对。"""
    seen: dict[str, tuple[int, list[tuple[float, float]]]] = {}
    for ilvl, ranges in ladder:
        key = f"{ilvl}:" + ",".join(f"{lo:g}-{hi:g}" for lo, hi in ranges)
        seen.setdefault(key, (ilvl, ranges))
    return list(seen.values())
