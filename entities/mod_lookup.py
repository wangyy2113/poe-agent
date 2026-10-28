"""词缀反查：装备文本 → 词缀定义（T 级、数值区间、词缀组）。

这是 `/item` 装备分析的核心查询路径。做法：
  1. 把装备上的一行词缀文本做数值占位化 → `text_template`
  2. 按 (text_template, item_class_page) 查 `mod_affix`
  3. 候选可能有多条（同一模板对应 T1..Tn），按**实际数值落在哪个区间**定 T 级

第 3 步是准确性的关键：`+47 敏捷` 的模板 `+# 敏捷` 会匹配到 8 个候选，
只有 47 落在 (43-50) 的那条才是 T2。若不做区间判定就直接取第一条，T 级必错。

诚实性约束：查不到就返回 None 并说明原因，**不猜**。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from kb.sources.poedb_mods import loose_templatize, strict_template_regex


@dataclass
class ModCandidate:
    """一条候选词缀定义。"""

    uid: str
    mod_code: str
    name_zh: str
    category: str
    category_label: str
    gen_type: int
    gen_label: str
    correct_group: str
    family_list: list[str]
    ilvl: int
    tier: int
    tier_count: int
    tier_source: str
    weight: int
    weight_pct: float
    text_zh: str
    text_template: str
    ranges: list[tuple[float, float]]
    tags: list[str]
    url: str
    item_class_zh: str
    #: POEDB 页面 slug。护甲类一个中文类别对应多个页面，跨页查询时要靠它去重
    item_class_page: str = ""
    #: 宽松模板。判断「是不是同一条词缀的另一个档位」用它——严格模板会因为
    #: 固定数字不同而在同一条词缀的不同档位间产生差异（`该装备附加 1 - #`）
    loose_template: str = ""

    def match_text(self, text: str) -> list[float] | None:
        """用严格模板正则匹配装备文本，返回按位置捕获的可变数值。

        同时完成两件校验：固定数字必须一致（否则整体不匹配），
        捕获的可变数值个数必然等于 `#` 个数、也就等于 len(ranges)。
        返回 None 表示该候选与这行文本不符。
        """
        m = strict_template_regex(self.text_template).match(text)
        if m is None:
            return None
        return [float(g) for g in m.groups()]

    def covers(self, values: list[float]) -> bool:
        """可变数值是否逐位落在区间内。固定值词缀（无可变位）恒为真。"""
        if len(values) != len(self.ranges):
            return False
        return all(lo <= v <= hi for v, (lo, hi) in zip(values, self.ranges, strict=True))

    @property
    def tier_label(self) -> str:
        return f"T{self.tier}/{self.tier_count}" if self.tier_count else f"T{self.tier}"

    @property
    def group_key(self) -> tuple[str, str]:
        """(来源类别, 词缀组)。T 级只在同一 group_key 内可比。"""
        return (self.category, self.correct_group)


@dataclass
class ModMatch:
    """反查结果。

    `matched` 为 None 表示无法确定，须如实声明而非猜测。
    `ambiguous` 为 True 表示多个来源/词缀组都能解释这行文本——
    此时 `matched` 只是其中一个可能，回答时**必须说明存在歧义**。
    """

    raw_text: str
    loose_template: str
    candidates: list[ModCandidate] = field(default_factory=list)
    #: 通过严格模板 + 区间校验的候选
    viable: list[ModCandidate] = field(default_factory=list)
    matched: ModCandidate | None = None
    ambiguous: bool = False
    #: 歧义时的分组（来源类别, 词缀组）→ 该组内最优候选
    alternatives: list[ModCandidate] = field(default_factory=list)
    #: 同一词缀组内出现多个 T 级 → 说明按多个底材页面查询时页面之间不一致。
    #: 单页面查询下不可能出现（同组各级区间互斥），所以这个标记只在跨页查询时有意义。
    #: 必须暴露出来：不同底材页面给不同 T 级时，取最优的那个就是在编造。
    tier_varies_by_page: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.matched is not None

    def describe(self) -> str:
        """给人看的结论。歧义时如实列出各种可能，不假装唯一。"""
        if not self.matched:
            return f"无法确定：{self.reason}"
        head = (
            f"{self.matched.tier_label} {self.matched.gen_label}"
            f"（{self.matched.category_label}，ilvl {self.matched.ilvl}，"
            f"权重 {self.matched.weight}）"
        )
        if not self.ambiguous:
            return head
        alts = "；".join(
            f"{c.category_label} {c.tier_label}(ilvl {c.ilvl})" for c in self.alternatives
        )
        return f"{head}　⚠ 存在歧义，同一数值也可能是：{alts}"


def _row_to_candidate(row: sqlite3.Row) -> ModCandidate:
    return ModCandidate(
        uid=row["uid"],
        mod_code=row["mod_code"],
        name_zh=row["name_zh"] or "",
        category=row["category"],
        category_label=row["category_label"] or row["category"],
        gen_type=row["gen_type"] or 0,
        gen_label=row["gen_label"] or "",
        correct_group=row["correct_group"] or "",
        family_list=json.loads(row["family_list"] or "[]"),
        ilvl=row["ilvl"] or 0,
        tier=row["tier"] or 0,
        tier_count=row["tier_count"] or 0,
        tier_source=row["tier_source"] or "",
        weight=row["weight"] or 0,
        weight_pct=row["weight_pct"] or 0.0,
        text_zh=row["text_zh"] or "",
        text_template=row["text_template"] or "",
        ranges=[(float(a), float(b)) for a, b in json.loads(row["ranges"] or "[]")],
        tags=json.loads(row["tags"] or "[]"),
        url=row["url"] or "",
        item_class_zh=row["item_class_zh"] or "",
        item_class_page=row["item_class_page"] or "",
        loose_template=row["loose_template"] or "",
    )


def lookup_mod(
    conn: sqlite3.Connection,
    text: str,
    item_class_page: str | None = None,
    *,
    item_class_pages: list[str] | None = None,
    categories: list[str] | None = None,
) -> ModMatch:
    """按词缀文本反查定义。

    `item_class_page` 指定底材类别（如 Bows）时结果更准——同一词缀在不同
    类别上的 T 级不同。不指定则跨类别查，T 级只能作参考。

    `item_class_pages` 传多个页面：护甲类的一个中文类别（手套）在 POEDB 上按
    属性需求拆成 6 个页面（`Gloves_str`、`Gloves_str_int`…），而装备文本只给
    「Gloves」这一个类别名，落到哪个页面取决于底材；调用方可提供已核实的具体页面。
    实测通用词缀（生命、抗性）在这 6 个页面上的 T 级阶梯完全一致，所以按整个
    类别查是可行的；不一致时置 `tier_varies_by_page` 如实报告，不取「最优的那个」。
    """
    loose = loose_templatize(text)
    match = ModMatch(raw_text=text, loose_template=loose)
    pages = [p for p in (item_class_pages or ([item_class_page] if item_class_page else [])) if p]

    # ── 阶段 1：宽松模板召回 ──
    sql = "SELECT * FROM mod_affix WHERE loose_template = ?"
    params: list = [loose]
    if pages:
        sql += " AND item_class_page IN (%s)" % ",".join("?" * len(pages))
        params.extend(pages)
    if categories:
        sql += " AND category IN (%s)" % ",".join("?" * len(categories))
        params.extend(categories)
    sql += " ORDER BY tier"

    rows = conn.execute(sql, params).fetchall()
    match.candidates = [_row_to_candidate(r) for r in rows]

    if not match.candidates:
        scope = ""
        if pages:
            scope = f"（底材类别 {pages[0] if len(pages) == 1 else '/'.join(pages[:3]) + '…'}）"
        match.reason = f"知识库中没有匹配模板 {loose!r} 的词缀{scope}"
        return match

    # ── 阶段 2：严格模板正则校验 + 区间覆盖 ──
    # 严格模板保证固定数字一致、可变位与 ranges 逐位对应
    for c in match.candidates:
        values = c.match_text(text)
        if values is not None and c.covers(values):
            match.viable.append(c)

    if not match.viable:
        match.reason = (
            f"召回 {len(match.candidates)} 个候选，但没有一个的固定文本与数值区间"
            f"同时吻合；可能是品质加成后的数值、被工艺台改动过，或该词缀不属此底材类别"
        )
        return match

    # ── 阶段 3：歧义判定 ──
    # T 级只在同一 (来源类别, 词缀组) 内可比。跨组的多解是**本质歧义**：
    # 例如「该装备的能量护盾提高 73%」既可能是掉落的 normal T3(68-79)，
    # 也可能是工艺台的 master T1(56-74)，光看数值无法区分。
    by_group: dict[tuple[str, str], list[ModCandidate]] = {}
    for c in match.viable:
        by_group.setdefault(c.group_key, []).append(c)
    for group in by_group.values():
        group.sort(key=lambda c: c.tier)

    # 组内优先取 T 级最优；组间按「掉落词缀优先」排序（normal 最常见）
    def group_rank(item: tuple[tuple[str, str], list[ModCandidate]]) -> tuple:
        (category, _), cands = item
        priority = {"normal": 0, "master": 1, "essence": 2}.get(category, 3)
        return (priority, cands[0].tier)

    ordered = sorted(by_group.items(), key=group_rank)
    match.matched = ordered[0][1][0]
    match.alternatives = [cands[0] for _, cands in ordered[1:]]
    match.ambiguous = len(ordered) > 1

    # 同一词缀组里出现多个 T 级：同组各级区间互斥，单页面查询下不可能发生，
    # 只会出自跨页查询时页面之间的阶梯不同。此时取哪个都是编造，故拒绝给 T 级。
    best_group = ordered[0][1]
    tiers_in_group = sorted({c.tier for c in best_group})
    if len(tiers_in_group) > 1:
        match.tier_varies_by_page = True
        match.matched = None
        match.reason = (
            "T 级取决于具体底材：该类别下不同底材页面给出不同档位（"
            + "、".join(f"T{t}" for t in tiers_in_group)
            + "），当前查询未能唯一确定具体底材页面，因此不给单一 T 级"
        )
        return match

    if match.ambiguous:
        match.reason = (
            f"数值可被 {len(ordered)} 个来源/词缀组解释，无法唯一确定；"
            f"已取最可能的一项并列出其余可能"
        )
    else:
        # 跨页查询时同一条词缀会按页面重复出现，那不是「多条同 T 并列」，
        # 只是同一条在多个底材页面上都成立。分开表述，否则读的人会误以为有歧义。
        pages = {c.item_class_page for c in best_group}
        if len(best_group) > 1 and len(pages) == len(best_group):
            note = f"（该类别 {len(pages)} 个底材页面上档位一致）"
        elif len(best_group) > 1:
            note = f"（组内 {len(best_group)} 条同 T 并列）"
        else:
            note = ""
        match.reason = f"唯一匹配 {match.matched.tier_label}{note}"
    return match


def exclusive_with(
    conn: sqlite3.Connection, candidate: ModCandidate, item_class_page: str, limit: int = 50
) -> list[ModCandidate]:
    """查与给定词缀互斥的其他词缀。

    互斥规则对齐 POEDB：**整个 ModFamilyList 有交集即互斥**
    （注意与 T 级分组只用 family_list[0] 不同）。
    """
    if not candidate.family_list:
        return []
    placeholders = ",".join("?" * len(candidate.family_list))
    rows = conn.execute(
        f"""SELECT * FROM mod_affix
            WHERE item_class_page = ? AND uid != ?
              AND EXISTS (
                  SELECT 1 FROM json_each(mod_affix.family_list)
                  WHERE json_each.value IN ({placeholders})
              )
            ORDER BY category, gen_type, tier LIMIT ?""",  # noqa: S608 占位符已参数化
        [item_class_page, candidate.uid, *candidate.family_list, limit],
    ).fetchall()
    return [_row_to_candidate(r) for r in rows]
