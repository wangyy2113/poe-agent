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
  - 同类别下不同底材页面给出不同 T 级，而输入底材未能逐字桥接到具体页面
  - 隐式词缀：尚未接入底材详情页的 T 级与区间反查
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field, replace

from entities.mod_lookup import ModCandidate, ModMatch, exclusive_with, lookup_mod
from entities.taxonomy import ModKind, Source
from kb.sources.poedb_mods import parse_ranges, strict_template_regex, templatize
from parsers.item_text import LINE_SEP, ParsedItem, ParsedMod
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
class BasePageEvidence:
    """逐字底材名的双来源桥：PoB 标签定属性页，POEDB 名称定中文对应。"""

    name_en: str
    name_zh: str
    page: str
    pob_url: str
    poedb_url: str
    implicit_text: str = ""


@dataclass
class ItemClassRef:
    """装备类别 → POEDB 词缀页面。

    `pages` 可能多于一个：护甲类的一个类别按属性需求拆成多个页面，
    若装备文本只给类别名，落到哪个页面取决于具体底材；已入库底材可缩窄。
    """

    raw: str
    pages: list[str] = field(default_factory=list)
    name_zh: str = ""
    code: str = ""
    matched_by: str = ""  # page / code / zh / base / base_candidates / base_conflict / base_unmapped
    reason: str = ""
    base_type: str = ""
    base_evidence: list[BasePageEvidence] = field(default_factory=list)
    base_note: str = ""

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
    evidence_source: str = Source.POEDB.value
    evidence_priority: int = 1
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


# ── 具体底材消歧 ────────────────────────────────────────────

_ARMOUR_PAGE_SUFFIX = {
    "str_armour": "str",
    "dex_armour": "dex",
    "int_armour": "int",
    "str_dex_armour": "str_dex",
    "str_int_armour": "str_int",
    "dex_int_armour": "dex_int",
    "str_dex_int_armour": "str_dex_int",
}
_ARMOUR_BASE_CATEGORIES = {"body", "gloves", "helmet", "boots", "shield"}


def _base_modifier_page(row: sqlite3.Row) -> str | None:
    """把已入库的 PoB 底材标签映射到 POEDB 词缀页；未知标签不猜。"""
    poedb_page = str(row["poedb_page"] or "")
    if not poedb_page:
        return None
    if row["category_key"] not in _ARMOUR_BASE_CATEGORIES:
        return poedb_page
    try:
        tags = json.loads(row["tags"] or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(tags, list):
        return None
    armour_tags = [tag for tag in _ARMOUR_PAGE_SUFFIX if tag in tags]
    if len(armour_tags) != 1:
        return None
    tag = armour_tags[0]
    # 胸甲导入时已明确记录标签与 modifier_page；再次对照，不能容忍漂移。
    if row["armour_tag"] and row["armour_tag"] != tag:
        return None
    page = f"{poedb_page}_{_ARMOUR_PAGE_SUFFIX[tag]}"
    if row["modifier_page"] and row["modifier_page"] != page:
        return None
    return page


def refine_item_class_with_base(
    conn: sqlite3.Connection, ref: ItemClassRef, base_type: str
) -> ItemClassRef:
    """用逐字底材桥把类别候选页缩到可证明的页；冲突时不给 T 级。

    中文名并不唯一（当前库中四个重名），但若同名记录全都指向同一词缀页，
    页面依然唯一。两源的武器类别英文拼法不完全相同，故以实际词缀页交集
    判断是否兼容。类别页与底材页冲突、底材没有可验证的页时，
    不退回跨页查询：那会掩盖矛盾并给出看似可信的档位。
    """
    base_type = base_type.strip()
    if not base_type or not ref.ok:
        return ref
    rows = conn.execute(
        """SELECT name_en,name_zh,item_class,category_key,poedb_page,
                  armour_tag,modifier_page,tags,pob_url,poedb_url,implicit_text
           FROM pob_base_item
           WHERE name_en = ? COLLATE NOCASE OR name_zh = ?
           ORDER BY base_id""",
        (base_type, base_type),
    ).fetchall()
    if not rows:
        return ref
    # 药剂在 PoB 中统一叫 Flask，而 POEDB 词缀按 Life/Mana/Utility
    # 乃至 Iron/Silver 拆页；当前没有逐底材的可靠桥，保留既有类别查询。
    if all(row["category_key"] == "flask" for row in rows):
        return ref
    ref.base_type = base_type
    resolved = [(row, _base_modifier_page(row)) for row in rows]
    compatible = [(row, page) for row, page in resolved if page in ref.pages]
    if not compatible:
        if any(page is None for _, page in resolved):
            ref.matched_by = "base_unmapped"
            ref.reason = (
                f"已识别底材「{base_type}」，但其属性标签无法唯一映射到类别"
                "词缀页面；不给词缀 T 级"
            )
        else:
            ref.matched_by = "base_conflict"
            ref.reason = (
                f"底材「{base_type}」的词缀页不属于装备文本类别「{ref.raw}」；"
                "类别与底材矛盾，不给词缀 T 级"
            )
        ref.pages = []
        return ref

    # 同名且同类别的另一条记录若无可核实页面，不能假定用户一定指向已映射那条。
    compatible_class = {str(row["item_class"]) for row, _ in compatible}
    if any(page is None and str(row["item_class"]) in compatible_class
           for row, page in resolved):
        ref.pages = []
        ref.reason = f"同名底材「{base_type}」中有记录无法映射到词缀页，不能选另一条代替"
        ref.matched_by = "base_unmapped"
        return ref

    evidence: list[BasePageEvidence] = []
    for row, page in compatible:
        assert page is not None
        evidence.append(BasePageEvidence(
            name_en=str(row["name_en"]), name_zh=str(row["name_zh"]),
            page=page, pob_url=str(row["pob_url"]),
            poedb_url=str(row["poedb_url"]),
            implicit_text=str(row["implicit_text"] or ""),
        ))

    old_page_count = len(ref.pages)
    ref.pages = sorted({item.page for item in evidence})
    ref.base_evidence = evidence
    ref.matched_by = "base" if len(ref.pages) == 1 else "base_candidates"
    if old_page_count > len(ref.pages):
        ref.base_note = (
            f"按逐字底材「{base_type}」的 PoB 属性标签与 POEDB 名称桥，"
            f"将词缀查询从 {old_page_count} 个类别页面缩到 {len(ref.pages)} 个页面"
        )
    if len(evidence) > 1:
        shared = (
            f"底材名「{base_type}」对应 {len(evidence)} 条注册记录；"
            f"它们的词缀页{'一致' if len(ref.pages) == 1 else '不同'}，"
            "没有擅自选择某一件具体底材"
        )
        ref.base_note = f"{ref.base_note}；{shared}" if ref.base_note else shared
    return ref


# ── 词缀匹配 ────────────────────────────────────────────────


def analyze_mods(conn: sqlite3.Connection, item: ParsedItem) -> ItemModReport:
    """给装备上的每条词缀定 T 级与数值范围。"""
    ref = refine_item_class_with_base(
        conn, resolve_item_class(conn, item.item_class), item.base_type
    )
    report = ItemModReport(item_class=ref)
    if ref.reason:
        report.warnings.append(ref.reason)
    if ref.base_note:
        report.warnings.append(ref.base_note)
    if ref.matched_by in ("code", "zh") and len(ref.pages) > 1:
        report.warnings.append(
            f"类别「{ref.raw}」对应 {len(ref.pages)} 个底材页面，已跨页查询；"
            f"页面之间 T 级不一致的词缀会标为未匹配而不是取其一"
        )

    mods, reconciliation_warnings = _reconcile_plain_copy_lines(conn, item, ref)
    report.warnings.extend(reconciliation_warnings)
    for mod, grouping_uncertain in mods:
        if grouping_uncertain:
            report.mods.append(ModAnalysis(
                mod=mod,
                affix_type=_affix_type_zh(mod.affix_type),
                ambiguous=True,
                reason=(
                    "普通复制文本存在可行但无法唯一确认的词缀分组；无法确定这行是否属于相邻的"
                    "混合词缀，因此不给 T 级。请用 Ctrl+Alt+C 进阶复制核对"
                ),
            ))
        else:
            report.mods.append(_analyze_one(conn, mod, ref, item))
    return report


def _reconcile_plain_copy_lines(
    conn: sqlite3.Connection, item: ParsedItem, ref: ItemClassRef
) -> tuple[list[tuple[ParsedMod, bool]], list[str]]:
    """仅在中文普通复制与词缀库共同支持时，合并相邻的两至三行。

    解析器不查库，仍原样保留各行；这里产生分析用的副本，不改调用方的
    ``ParsedItem``。拼接必须通过完整模板及区间校验，且来源、词缀组、
    档位不能有歧义。各行若都能单独解释，普通复制没有证据证明它们同属
    一条；候选拼接彼此重叠时也不选择一种分组。
    """
    mods = item.all_mods
    if item.language != "zh" or item.advanced_copy or not ref.ok or len(mods) < 2:
        return [(mod, False) for mod in mods], []

    plausible: dict[tuple[int, int], ModMatch] = {}
    eligible: set[tuple[int, int]] = set()
    single_viable: dict[int, bool] = {}

    def can_stand_alone(index: int) -> bool:
        if index not in single_viable:
            mod = mods[index]
            hit = lookup_mod(
                conn, mod.lookup_text, item_class_pages=ref.pages,
                categories=_KIND_TO_CATEGORIES.get(mod.kind),
            )
            single_viable[index] = bool(hit.viable)
        return single_viable[index]

    for start in range(len(mods) - 1):
        for width in (2, 3):
            end = start + width
            if end > len(mods):
                continue
            group = mods[start:end]
            if any(not _can_join_plain_lines(a, b) for a, b in zip(group, group[1:])):
                continue
            joined = LINE_SEP.join(mod.text for mod in group)
            hit = lookup_mod(
                conn, joined, item_class_pages=ref.pages,
                categories=_KIND_TO_CATEGORIES.get(group[0].kind),
            )
            if not hit.viable:
                continue
            plausible[start, end] = hit
            # 各行都可单独成立、或完整词缀跨来源/底材仍不唯一时，
            # 不仅不能合并，也不能再把某一行的单独匹配当成确定的 T 级。
            interpretations = {
                (candidate.category, candidate.correct_group, candidate.tier,
                 candidate.tier_count, candidate.gen_label, candidate.ilvl,
                 tuple(candidate.ranges), candidate.weight, candidate.weight_pct)
                for candidate in hit.viable
            }
            if (hit.matched is not None and not hit.ambiguous
                    and not hit.tier_varies_by_page
                    and len(interpretations) == 1
                    and hit.matched.gen_label in ("前缀", "后缀")
                    and not all(can_stand_alone(i) for i in range(start, end))):
                eligible.add((start, end))

    # 任意两个候选共用一行，就无法从普通复制判断正确分组；一并弃用。
    selected = {
        start: (end, hit)
        for (start, end), hit in plausible.items()
        if (start, end) in eligible
        if not any(
            start < other_end and other_start < end
            for other_start, other_end in plausible
            if (other_start, other_end) != (start, end)
        )
    }
    selected_spans = {(start, end) for start, (end, _) in selected.items()}
    uncertain_indices = {
        index
        for (start, end) in plausible if (start, end) not in selected_spans
        for index in range(start, end)
    }

    result: list[tuple[ParsedMod, bool]] = []
    warnings: list[str] = []
    if uncertain_indices:
        warnings.append(
            "普通复制中有相邻行可组成词缀，但分组或来源无法唯一确认；"
            "相关行的档位保持未匹配。"
            "请用 Ctrl+Alt+C 进阶复制核对"
        )
    index = 0
    while index < len(mods):
        if index in selected:
            end, hit = selected[index]
            group = mods[index:end]
            left = group[0]
            joined = LINE_SEP.join(mod.text for mod in group)
            result.append((replace(
                left,
                text=joined,
                raw="\n".join(mod.raw for mod in group),
                values=[value for mod in group for value in mod.values],
                ranges=[span for mod in group for span in mod.ranges],
                lines=[mod.text for mod in group],
            ), False))
            candidate = hit.matched
            assert candidate is not None
            warnings.append(
                f"普通复制中将相邻 {len(group)} 行「{joined}」按 POEDB "
                f"{candidate.category_label or candidate.category} 词缀合并分析；"
                "原文仍保留。若游戏显示为独立词缀，请用 Ctrl+Alt+C 进阶复制核对"
            )
            index = end
        else:
            result.append((mods[index], index in uncertain_indices))
            index += 1
    return result, warnings


def _can_join_plain_lines(left: ParsedMod, right: ParsedMod) -> bool:
    """分段、类型与客户端旗标必须一致；已有词缀头或多行信息不重组。"""
    return (
        left.kind in (ModKind.EXPLICIT, ModKind.CRAFTED, ModKind.FRACTURED)
        and left.kind == right.kind
        and left.section == right.section
        and left.flags == right.flags
        and not left.is_multiline and not right.is_multiline
        and left.tier is None and right.tier is None
        and left.tier_rank is None and right.tier_rank is None
        and not left.affix_type and not right.affix_type
        and not left.affix_name and not right.affix_name
    )


def _analyze_one(
    conn: sqlite3.Connection, mod: ParsedMod, ref: ItemClassRef, item: ParsedItem
) -> ModAnalysis:
    # 进阶复制的 Prefix/Suffix 头是游戏直接给的；即使档位查不到，容量计数
    # 仍应使用这个方向，不依赖 POEDB 是否收录整条混合词缀。
    out = ModAnalysis(mod=mod, affix_type=_affix_type_zh(mod.affix_type))

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
    if mod.kind is ModKind.IMPLICIT:
        _fill_base_implicit(out, mod, ref, item)
        return out
    if mod.kind is ModKind.CORRUPTED_IMPLICIT:
        out.reason = "腐化隐式不等于底材固有隐式；当前没有可核实的 T 级与区间，不能借底材词条推断"
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


def _fill_base_implicit(
    out: ModAnalysis, mod: ParsedMod, ref: ItemClassRef, item: ParsedItem
) -> None:
    """只核实英文底材固有隐式的原文和可变区间，不从 PoB 文本猜 T 级。"""
    if not ref.ok and ref.reason:
        out.reason = f"{ref.reason}；隐式区间也不能据此核实"
        return
    out.reason = (
        "隐式词缀的 T 级尚无底材详情页证据；当前未能从已核实底材的英文固有"
        "隐式原文确定区间"
    )
    if item.language != "en" or not ref.base_evidence:
        return
    definitions = {base.implicit_text for base in ref.base_evidence}
    if len(definitions) != 1:
        return
    definition = next(iter(definitions))
    if not definition:
        return
    full_text = LINE_SEP.join(part.strip() for part in definition.splitlines() if part.strip())
    # 底材定义是多行时，绝不能让装备复制中的单独首行继承整条的区间。
    if not full_text or (LINE_SEP in full_text and not mod.is_multiline):
        return
    ranges = parse_ranges(full_text)
    match = strict_template_regex(templatize(full_text)).match(mod.lookup_text)
    if match is None:
        return
    rolled_values = [float(value) for value in match.groups()]
    if len(rolled_values) != len(ranges) or any(
        not lo <= value <= hi
        for value, (lo, hi) in zip(rolled_values, ranges, strict=True)
    ):
        return
    out.ranges = ranges
    out.roll_pct = _roll_pct(rolled_values, ranges)
    out.source_label = "PoB 底材固有隐式"
    out.url = ref.base_evidence[0].pob_url
    out.evidence_source = Source.POB.value
    out.evidence_priority = 2
    out.reason = (
        "英文隐式原文与已核实底材的 PoB 定义逐字吻合，全部可变数值落在其区间内；"
        "PoB 未给隐式 T 级，故仍标未匹配 T 级"
    )


def _lookup(conn: sqlite3.Connection, mod: ParsedMod, ref: ItemClassRef) -> ModMatch:
    """按整条词缀查库；多行整条失配时不可用首行冒充整条的 T 级。"""
    categories = _KIND_TO_CATEGORIES.get(mod.kind)
    return lookup_mod(
        conn, mod.lookup_text, item_class_pages=ref.pages, categories=categories
    )


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
