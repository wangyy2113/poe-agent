"""装备词缀 → T 级与数值范围（任务 4.2，需求 7.3/7.9）。

`item_text.py` 只把文本变成结构，这里才连库。分开是为了让解析能离线单测。

## T 级从哪来（按可信度排序，不满足就往下退，退到底就如实说未匹配）

1. **装备文本自带**（进阶复制 Ctrl+Alt+C）。游戏自己给的 `(Tier: 5)` 与
   `17(16-20)` 区间，比查我们的库更硬——库是抓来的二手数据，游戏是一手。
   这条路径**语言无关**，英文客户端也能用。
2. **中文文本查 POEDB 词缀库**（`entities.mod_lookup`）。宽松模板召回 →
   严格模板正则校验 → 数值区间覆盖 → 词缀组歧义判定。
3. **英文普通复制**：拿不到 T 级。POEDB 词缀库只有中文文本（`mod_affix.text_zh`），
   而 PoB 的英文库没有 tier 列。曾试过按词缀组名桥接两侧，实测组名对不上——
   POEDB 用 `PhysicalDamage`，PoB 用 `LocalPhysicalDamage`，1578 个组只有 856 个
   同名，局部武器词缀是系统性不同。硬桥会在一部分词缀上给出错误 T 级，
   而错的 T 级比没有 T 级更糟，所以不桥，如实告知改用 Ctrl+Alt+C。

## 需求 7.9 的落点

匹配不上就 `matched=None` + `reason`，**绝不猜 T 级**。以下情形都算未匹配，
且各自给出不同原因，让用户知道是「库里没有」还是「无法定到底材」：

  - 类别名解析不出对应的 POEDB 页面
  - 模板召回为空（库里没这条词缀）
  - 召回了但没有一条的数值区间吻合（可能被品质/工艺台改过）
  - 同类别下不同底材页面给出不同 T 级（底材库未入库，定不到页面）
  - 隐式词缀：它来自底材，而底材库尚未入库
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from entities.mod_lookup import ModCandidate, ModMatch, exclusive_with, lookup_mod
from entities.taxonomy import ModKind
from parsers.item_text import ParsedItem, ParsedMod
from parsers.mod_bridge import BridgeResult, bridge_english_mod

#: 词缀类型 → 可在 `mod_affix.category` 里出现的来源类别。
#: 收窄召回范围能减少歧义：附魔只可能来自 enchant 组，工艺台只可能来自 master。
_KIND_TO_CATEGORIES: dict[ModKind, list[str] | None] = {
    ModKind.ENCHANT: ["enchant"],
    ModKind.CRAFTED: ["master", "veiled"],
    # 显式与裂隙词缀的来源很多（掉落、精华、影响词、天灾…），不收窄，
    # 让歧义判定去处理——收窄成 normal 会把精华词缀误判为未匹配
    ModKind.EXPLICIT: None,
    ModKind.FRACTURED: None,
    ModKind.SYNTHESISED: ["synthesis", "synthesis_corrupted"],
}

#: T 级来源，用于在输出里标注这条 T 级有多硬
TIER_FROM_ITEM_TEXT = "item_text"
TIER_FROM_POEDB = "poedb"
#: 英文文本经「PoB 阶梯指纹 ↔ POEDB 阶梯」比对得出，见 `mod_bridge.py`
TIER_FROM_BRIDGE = "bridge"
TIER_NONE = ""

TIER_SOURCE_LABEL = {
    TIER_FROM_ITEM_TEXT: "装备文本自带（进阶复制）",
    TIER_FROM_POEDB: "POEDB 词缀库",
    TIER_FROM_BRIDGE: "PoB↔POEDB 阶梯指纹比对",
    TIER_NONE: "无",
}


@dataclass
class ItemClassRef:
    """装备类别 → POEDB 词缀页面。

    `pages` 可能多于一个：护甲类的一个类别按属性需求拆成多个页面，
    而装备文本只给类别名，落到哪个页面取决于底材（底材库尚未入库）。
    """

    raw: str
    pages: list[str] = field(default_factory=list)
    name_zh: str = ""
    code: str = ""
    matched_by: str = ""  # page / code / zh
    reason: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.pages)


@dataclass
class ModAnalysis:
    """一条词缀的分析结果。

    `tier_source` 必须跟着 `tier` 一起看：`item_text` 是游戏给的，
    `poedb` 是我们查的，空串表示**没有 T 级**——此时 `reason` 说明为什么没有。
    """

    mod: ParsedMod
    tier: int | None = None
    tier_count: int | None = None
    tier_source: str = TIER_NONE
    ranges: list[tuple[float, float]] = field(default_factory=list)
    roll_pct: float | None = None
    ilvl: int | None = None
    weight: int | None = None
    weight_pct: float | None = None
    affix_type: str = ""      # 前缀 / 后缀
    mod_group: str = ""
    #: 来源类别的原始值（normal/essence/master…）。查同组更高档位时必须带上它——
    #: 只按词缀组查会把工艺台的 T1 当成掉落词缀的 T1，两者数值差一个量级
    category: str = ""
    source_label: str = ""    # Base / 精华 / 工艺工作台 …
    #: 宽松模板，用于识别「同一条词缀的其他档位」
    loose_template: str = ""
    url: str = ""
    ambiguous: bool = False
    alternatives: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.tier is not None or bool(self.mod_group)

    @property
    def tier_label(self) -> str:
        """T 级展示。没有就明说未匹配，不留空让人误以为是 T1。"""
        if self.tier is None:
            return "未匹配"
        return f"T{self.tier}/{self.tier_count}" if self.tier_count else f"T{self.tier}"


@dataclass
class ItemModReport:
    item_class: ItemClassRef
    mods: list[ModAnalysis] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return sum(1 for m in self.mods if m.tier is not None)

    @property
    def unmatched(self) -> list[ModAnalysis]:
        return [m for m in self.mods if m.tier is None]

    @property
    def prefix_count(self) -> int:
        return sum(1 for m in self.mods if m.affix_type == "前缀")

    @property
    def suffix_count(self) -> int:
        return sum(1 for m in self.mods if m.affix_type == "后缀")

    def citations(self) -> list[tuple[str, str]]:
        """(标题, URL) 去重列表，供上层装配引用。"""
        seen: dict[str, str] = {}
        for m in self.mods:
            if m.url and m.url not in seen:
                seen[m.url] = m.mod.text
        return [(title, url) for url, title in seen.items()]


# ── 类别解析 ────────────────────────────────────────────────


def resolve_item_class(conn: sqlite3.Connection, raw_class: str) -> ItemClassRef:
    """装备文本里的类别名 → POEDB 词缀页面集合。

    三条路依次试，全部走数据库实际取值，不硬编码映射表：

      1. 页面 slug 直接命中（`Bows`、`Amulets`；空格换下划线后 `One Hand Swords`
         → `One_Hand_Swords`）
      2. 英文类别码命中（`Gloves` → `item_class_code='Gloves'`，对应 6 个页面）
      3. 中文类别名命中（`手套`）

    顺序有讲究：先试页面是因为武器类的页面 slug 恰好等于复数类别名，那是最精确的
    单页命中；退到类别码才会拿到多页。反过来的话弓也会走多页逻辑，白丢精度。
    """
    ref = ItemClassRef(raw=raw_class.strip())
    if not ref.raw:
        ref.reason = "装备文本里没有类别行，无法确定词缀所属底材类别"
        return ref

    slug = ref.raw.replace(" ", "_")
    row = conn.execute(
        """SELECT item_class_page, item_class_zh, item_class_code FROM mod_affix
           WHERE item_class_page = ? LIMIT 1""",
        [slug],
    ).fetchone()
    if row:
        ref.pages = [row["item_class_page"]]
        ref.name_zh = row["item_class_zh"] or ""
        ref.code = row["item_class_code"] or ""
        ref.matched_by = "page"
        return ref

    # 类别码是单数（`Body Armour`），而装备文本给复数（`Body Armours`），
    # 所以去尾 s 也要试一次。中文没有单复数，只试原文。
    candidates: list[tuple[str, str, str]] = [("item_class_code", ref.raw, "code")]
    if ref.raw.endswith("s"):
        candidates.append(("item_class_code", ref.raw[:-1], "code"))
    candidates.append(("item_class_zh", ref.raw, "zh"))

    for column, value, matched_by in candidates:
        rows = conn.execute(
            f"""SELECT DISTINCT item_class_page, item_class_zh, item_class_code
                FROM mod_affix WHERE {column} = ? ORDER BY item_class_page""",  # noqa: S608
            [value],
        ).fetchall()
        if rows:
            ref.pages = [r["item_class_page"] for r in rows]
            ref.name_zh = rows[0]["item_class_zh"] or ""
            ref.code = rows[0]["item_class_code"] or ""
            ref.matched_by = matched_by
            return ref

    ref.reason = (
        f"类别 {ref.raw!r} 在词缀库里没有对应页面。词缀库按 POEDB 的底材类别页组织，"
        f"该类别可能尚未采集（如药剂、地图、雕像的部分页面），无法给出 T 级"
    )
    return ref


# ── 词缀匹配 ────────────────────────────────────────────────


def analyze_mods(conn: sqlite3.Connection, item: ParsedItem) -> ItemModReport:
    """给装备上的每条词缀定 T 级与数值范围。"""
    ref = resolve_item_class(conn, item.item_class)
    report = ItemModReport(item_class=ref)
    if ref.reason:
        report.warnings.append(ref.reason)
    if ref.matched_by in ("code", "zh") and len(ref.pages) > 1:
        report.warnings.append(
            f"类别「{ref.raw}」对应 {len(ref.pages)} 个底材页面，已跨页查询；"
            f"页面之间 T 级不一致的词缀会标为未匹配而不是取其一"
        )

    for mod in item.all_mods:
        report.mods.append(_analyze_one(conn, mod, ref, item))
    return report


def _analyze_one(
    conn: sqlite3.Connection, mod: ParsedMod, ref: ItemClassRef, item: ParsedItem
) -> ModAnalysis:
    out = ModAnalysis(mod=mod)

    # ── 路径 1：装备文本自带 T 级（进阶复制）──
    if mod.tier is not None:
        out.tier = mod.tier
        out.tier_source = TIER_FROM_ITEM_TEXT
        out.ranges = list(mod.ranges)
        out.roll_pct = mod.roll_pct()
        out.affix_type = _affix_type_zh(mod.affix_type)
        out.reason = "T 级与数值区间由装备文本（进阶复制）直接给出，未经库查询"
        # 库里还能补 ilvl 与权重，补不到不影响 T 级的可信度
        if ref.ok and item.language == "zh":
            hit = _lookup(conn, mod, ref)
            if hit.matched:
                out.ilvl = hit.matched.ilvl
                out.weight = hit.matched.weight
                out.weight_pct = hit.matched.weight_pct
                out.mod_group = hit.matched.correct_group
                out.url = hit.matched.url
        return out

    if mod.tier_rank is not None:
        # 工艺台词缀用 Rank，不是掉落 T 级，不能混为一谈
        out.affix_type = _affix_type_zh(mod.affix_type)
        out.reason = (
            f"工艺台词缀（Rank {mod.tier_rank}），不属掉落词缀的 T 级体系，"
            f"不给 T 级"
        )
        return out

    # ── 隐式词缀：来自底材，不在词缀库里 ──
    if mod.kind in (ModKind.IMPLICIT, ModKind.CORRUPTED_IMPLICIT):
        out.reason = "隐式词缀由底材决定，底材库尚未入库，无法给出 T 级与数值范围"
        return out

    if not ref.ok:
        out.reason = ref.reason or "底材类别未知，无法给出 T 级"
        return out

    # ── 路径 3：英文文本走阶梯指纹桥 ──
    if item.language != "zh":
        _fill_from_bridge(out, bridge_english_mod(conn, mod.lookup_text, ref.pages), mod)
        return out

    # ── 路径 2：中文文本查库 ──
    hit = _lookup(conn, mod, ref)
    _fill_from_match(out, hit, mod)
    return out


def _lookup(conn: sqlite3.Connection, mod: ParsedMod, ref: ItemClassRef) -> ModMatch:
    """查库。多行词缀先按拼接文本查，查不到再退回单行。

    退回是必要的：混合词缀在库里存成一条（`A / B`），但普通复制看不出两行属于
    同一条词缀，此时 `lookup_text` 就是单行，拼接查询会失配。
    """
    categories = _KIND_TO_CATEGORIES.get(mod.kind)
    hit = lookup_mod(
        conn, mod.lookup_text, item_class_pages=ref.pages, categories=categories
    )
    if not hit.ok and mod.is_multiline:
        first = lookup_mod(
            conn, mod.lines[0], item_class_pages=ref.pages, categories=categories
        )
        if first.ok:
            return first
    return hit


def _fill_from_match(out: ModAnalysis, hit: ModMatch, mod: ParsedMod) -> None:
    out.reason = hit.reason
    out.ambiguous = hit.ambiguous
    if hit.matched is None:
        return
    c = hit.matched
    out.tier = c.tier
    out.tier_count = c.tier_count
    out.tier_source = TIER_FROM_POEDB
    out.ranges = list(c.ranges)
    out.ilvl = c.ilvl
    out.weight = c.weight
    out.weight_pct = c.weight_pct
    out.affix_type = c.gen_label or _affix_type_zh(mod.affix_type)
    out.mod_group = c.correct_group
    out.category = c.category
    out.loose_template = c.loose_template
    out.source_label = c.category_label or c.category
    out.url = c.url
    out.roll_pct = _roll_pct(mod.values, c.ranges)
    out.alternatives = [
        f"{a.category_label or a.category} {a.tier_label}（ilvl {a.ilvl}）"
        for a in hit.alternatives
    ]


def _fill_from_bridge(out: ModAnalysis, hit: BridgeResult, mod: ParsedMod) -> None:
    out.reason = hit.reason
    out.ambiguous = hit.ambiguous
    out.alternatives = [
        f"{a.category_label} {a.tier_label}（{a.text_zh}，ilvl {a.ilvl}）"
        for a in hit.alternatives
    ]
    if hit.matched is None:
        if not hit.reason:
            out.reason = "英文词缀未能对应到 POEDB 档位"
        # 补一句可操作的建议：进阶复制能让游戏直接给出 T 级
        out.reason += "。用 Ctrl+Alt+C 进阶复制可由游戏直接给出 T 级"
        return
    c = hit.matched
    out.tier = c.tier
    out.tier_count = c.tier_count
    out.tier_source = TIER_FROM_BRIDGE
    out.ranges = list(c.ranges)
    out.ilvl = c.ilvl
    out.weight = c.weight
    out.weight_pct = c.weight_pct
    out.affix_type = c.gen_label or _affix_type_zh(mod.affix_type)
    out.mod_group = c.correct_group
    out.category = c.category
    out.loose_template = c.loose_template
    out.source_label = c.category_label
    out.url = c.url
    out.roll_pct = _roll_pct(mod.values, c.ranges)


def _roll_pct(values: list[float], ranges: list[tuple[float, float]]) -> float | None:
    """实际数值在库里区间中的位置。定值位不计入（没有 roll 好坏之说）。"""
    if not ranges or len(values) != len(ranges):
        return None
    pcts = [
        (v - lo) / (hi - lo)
        for v, (lo, hi) in zip(values, ranges, strict=True)
        if hi > lo
    ]
    return sum(pcts) / len(pcts) if pcts else None


def _affix_type_zh(affix_type: str) -> str:
    return {"Prefix": "前缀", "Suffix": "后缀"}.get(affix_type, "")


# ── 可制作方向（4.3 用）────────────────────────────────────


def blocking_mods(
    conn: sqlite3.Connection, analysis: ModAnalysis, ref: ItemClassRef, limit: int = 8
) -> list[ModCandidate]:
    """与该词缀互斥的其他词缀——也就是「因为有它，做不出哪些」。

    只在单页面命中时查：互斥关系按底材页面定义，跨页取交集会把不同底材的
    互斥关系混在一起。
    """
    if not analysis.mod_group or len(ref.pages) != 1:
        return []
    hit = lookup_mod(
        conn, analysis.mod.lookup_text, item_class_pages=ref.pages,
        categories=_KIND_TO_CATEGORIES.get(analysis.mod.kind),
    )
    if hit.matched is None:
        return []
    return exclusive_with(conn, hit.matched, ref.pages[0], limit=limit)
