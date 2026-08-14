"""POEDB 词缀抽取器。

数据形态（2026-08-04 实测）：词缀数据**不是** HTML 表格，而是传给
`new ModsView({...})` 的 JSON 载荷（Bows 页面该 script 达 848KB）。
页面上的 `#ModifiersCalc` 区域只有 Mustache 模板。
解析 JSON 比解析 HTML 稳得多——上游改版式样不会影响我们。

T 级算法：**严格复刻 POEDB 自身的实现**，反压缩自
`https://cdn.poedb.tw/js/ModsView.f39fca410dd746d3.js`：

    // 派生字段
    CorrectGroup = ModFamilyList[0]
    GenGroup     = "" + ModGenerationTypeID + CorrectGroup
    Code         = basename(hover)
    // T 级：在每个来源类别（normal/essence/...）内独立计算
    key          = "" + ModGenerationTypeID + CorrectGroup
    levels[key]  = 该组全部 Level → 去重 → 降序
    Tier         = levels[key].indexOf(parseInt(Level)) + 1

三个容易搞错、会直接导致 T 级错误的点：
  1. T 级分组只用 `ModFamilyList[0]`，而**互斥判断要用整个 ModFamilyList 求交集**
  2. Level 先去重再排名 → 同 Level 的词缀共享同一个 T
  3. T 级按来源类别分别算，不能跨类别混算（精华的 T 与掉落的 T 是两套）

为什么要对齐 POEDB 而不是自己定义：POEDB 是本项目的 P1 来源，
用户会拿我们的输出与 POEDB 页面对照。不一致就是错。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from selectolax.parser import HTMLParser

logger = logging.getLogger(__name__)

MARKER = "new ModsView("

#: 生成类型：POEDB 载荷里的 `gen` 字段就是 {"1":"前缀","2":"后缀"}
GEN_PREFIX = 1
GEN_SUFFIX = 2

GEN_LABELS = {1: "前缀", 2: "后缀", 0: "其他"}

#: 载荷里的非词缀键，遍历词缀组时要跳过
META_KEYS = frozenset({"baseitem", "config", "gen", "opt", "crafter"})

#: 完整词缀池要求逐行零失败；不能用成功率阈值掩盖分母缺行。


class ModStructureError(ValueError):
    """词缀数据结构与预期不符——**真问题**，应让采集失败而非写脏数据。"""


class NoModsDataError(ValueError):
    """该页面本来就没有常规词缀池——**合法情况**，不是错误。

    实测例：`/cn/Assembled_Eye_Jewel`（聚合之凝珠宝）只有虚空忆境固定词缀
    与传奇，页面内无 `#ModifiersCalc` 区块。这类页面要与「结构变更」区分开，
    否则要么误报失败，要么把真的改版当成正常跳过。
    """


@dataclass
class ModRecord:
    """一条词缀在**某个底材类别**上的完整信息。

    同一条词缀（mod_code）在不同底材类别上的 Level / 权重 / T 级可能不同，
    因此以 (mod_code, item_class_page, category) 为主键，不做跨类别去重。
    这样才能正确回答「这条词缀在弓上是 T5，在法杖上是 T4」。
    """

    mod_code: str
    item_class_page: str      # 页面 slug，如 Bows
    item_class_code: str      # POEDB 的 ItemClassesCode，如 Bow
    item_class_zh: str        # 中文类别名，如 弓
    category: str             # normal / essence / master / delve / ...
    category_label: str       # 该来源组的中文标题

    name_zh: str              # 词缀名（如「猫鼬之」）；部分组是 HTML，已去标签
    gen_type: int             # 1=前缀 2=后缀 0=其他
    gen_label: str
    family_list: list[str]    # 词缀组；**互斥判断用整个列表求交集**
    correct_group: str        # family_list[0]；**T 级分组只用它**

    ilvl: int                 # Level：该词缀要求的物品等级
    tier: int                 # 按 POEDB 算法推导
    tier_count: int           # 该组共有多少级，用于展示 "T3/8"
    tier_source: str          # "poedb_payload"=数据自带 / "derived"=按算法推导

    weight: int               # DropChance 生成权重
    weight_pct: float         # 组内占比（%）

    text_zh: str              # 词缀文本，区间归一为 ASCII 连字符，数值原样保留
    text_template: str        # 严格模板：只占位区间，固定数字保留 → 用于校验
    loose_template: str       # 宽松模板：全部数字占位 → 用于召回
    ranges: list[tuple[float, float]]

    tags: list[str]           # mod_no 徽章文本（如「属性」）
    spawn_tags: list[str]     # spawn_no：可生成的底材标签
    fossil_tags: list[str]    # fossil_no：化石标签

    url: str

    #: 来源实例标识。同一条词缀可由多个来源实例赋予（如同一词缀由 T6 与 T7
    #: 两种精华给出，两者 tier 不同但 mod_code 相同），需保留区分。
    variant_id: str = ""

    @property
    def key(self) -> tuple:
        """去重键 = **实质字段全集**。

        这样去重在构造上即无损：只有每个承载事实的字段都相同的记录才会被合并。
        实测必要性：essence 组中「忌惮之破空精华」同 mod_code 却有 tier 6 与 7
        两条（由不同等级精华赋予），若只按 (mod_code, page, category) 去重会丢一条。
        """
        return (
            self.mod_code,
            self.item_class_page,
            self.category,
            self.variant_id,
            self.gen_type,
            self.correct_group,
            self.ilvl,
            self.tier,
            self.weight,
            self.text_zh,
        )

    @property
    def uid(self) -> str:
        """稳定主键，用于入库。由去重键哈希而来，可复现。"""
        import hashlib

        payload = "|".join(str(x) for x in self.key)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]  # noqa: S324 非安全用途


@dataclass
class ModExtractStats:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    #: 主键重复而被合并的条数。与 failed 分开统计——去重是正常现象，
    #: 但占比过高说明主键设计有误、正在丢数据，必须能看见。
    deduped: int = 0
    #: 去重时实质字段冲突的条数。**这个必须为 0**，否则就是在丢数据。
    dedup_conflicts: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    failures: list[tuple[str, str]] = field(default_factory=list)
    dup_samples: list[tuple[str, str, str]] = field(default_factory=list)
    conflict_samples: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def success_ratio(self) -> float:
        """成功率只看「构建失败」，去重不算失败。"""
        denom = self.total - self.deduped
        return self.succeeded / denom if denom else 0.0

    @property
    def dedup_ratio(self) -> float:
        return self.deduped / self.total if self.total else 0.0

    def fail(self, ident: str, reason: str) -> None:
        self.failed += 1
        if len(self.failures) < 50:
            self.failures.append((ident, reason))


# ── 载荷提取 ────────────────────────────────────────────────

_VERIFIED_EMPTY_MOD_PAGES = {
    # 2026-08-13 真实页面人工核对：该底材只有固定/传奇内容，无 ModifiersCalc。
    "Assembled_Eye_Jewel": "Assembled Eye Jewel",
}
_BODY_ARMOUR_PAGE_TAGS = {
    "Body_Armours_str": "Str",
    "Body_Armours_dex": "Dex",
    "Body_Armours_int": "Int",
    "Body_Armours_str_dex": "StrDex",
    "Body_Armours_str_int": "StrInt",
    "Body_Armours_dex_int": "DexInt",
    "Body_Armours_str_dex_int": "StrDexInt",
}


def _verified_page_shell(html: str, page_slug: str, page_url: str) -> bool:
    """核对目标页身份和完整文档尾部，拒绝重定向内容、挑战页与截断页。"""
    if page_url.rstrip("/") != f"https://poedb.tw/cn/{page_slug}":
        return False
    if not html.rstrip().lower().endswith("</html>") or "poedb_footer." not in html:
        return False
    tree = HTMLParser(html)
    root = tree.css_first("html")
    title = tree.css_first("title")
    footer = tree.css_first("footer")
    if (
        root is None
        or str(root.attributes.get("lang") or "").lower() != "cn"
        or title is None
        or not title.text(deep=True).strip()
        or footer is None
    ):
        return False
    hrefs = {
        str(node.attributes.get("href") or "").strip()
        for node in tree.css("a[href]")
    }
    return (
        f"/cn/{page_slug}" in hrefs
        and f"/cn/marked?id={page_slug}" in hrefs
    )


def _verified_no_mods_page(html: str, page_slug: str, page_url: str) -> bool:
    """只接受审计 allowlist 中、身份和完整尾部均可核对的真实空池页。"""
    expected_base_type = _VERIFIED_EMPTY_MOD_PAGES.get(page_slug)
    if expected_base_type is None or not _verified_page_shell(html, page_slug, page_url):
        return False
    tree = HTMLParser(html)
    if tree.css_first("#ModifiersCalc") is not None or MARKER in html:
        return False
    for row in tree.css("tr"):
        cells = row.css("td")
        if len(cells) >= 2 and cells[0].text(deep=True).strip() == "BaseType":
            if cells[1].text(deep=True).strip() == expected_base_type:
                return True
    return False


def _validate_payload_page_identity(
    html: str, payload: dict[str, Any], page_slug: str, page_url: str
) -> None:
    """把结构化载荷绑定到请求页面；胸甲页额外核对属性标签。"""
    if not _verified_page_shell(html, page_slug, page_url):
        raise ModStructureError(f"{page_slug} 页面身份或完整文档尾部无法核对")
    expected_tag = _BODY_ARMOUR_PAGE_TAGS.get(page_slug)
    if expected_tag is None:
        return
    baseitem = payload.get("baseitem")
    if not isinstance(baseitem, dict) or (
        str(baseitem.get("href") or "") != "Body_Armours"
        or str(baseitem.get("cn") or "") != "Body Armour"
        or str(baseitem.get("tags") or "") != expected_tag
    ):
        raise ModStructureError(
            f"{page_slug} 的载荷身份不匹配：预期 Body Armour/{expected_tag}"
        )


def extract_payload(html: str, page_slug: str, page_url: str) -> dict[str, Any]:
    """从 `new ModsView({...})` 取出 JSON 载荷。

    不能用正则：载荷含大量转义引号与花括号。用括号配对扫描，
    并正确跳过字符串字面量与反斜杠转义。
    """
    start = html.find(MARKER)
    if start < 0:
        if "ModifiersCalc" in html:
            raise ModStructureError(
                "页面有 ModifiersCalc 区块却找不到 `new ModsView(` 载荷，POEDB 可能已改版"
            )
        if _verified_no_mods_page(html, page_slug, page_url):
            raise NoModsDataError(
                "页面身份、完整尾部和已审计空页 allowlist 均已核对，该底材类别没有常规词缀池"
            )
        raise ModStructureError(
            "响应缺少词缀载荷且无法验证为所请求的合法空池页面；拒绝用它清空旧页"
        )

    i = start + len(MARKER)
    while i < len(html) and html[i] not in "{[":
        i += 1
    if i >= len(html):
        raise ModStructureError("`new ModsView(` 之后未找到 JSON 起始括号")

    begin = i
    depth = 0
    in_str = False
    quote = ""
    escaped = False
    while i < len(html):
        ch = html[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_str = False
        elif ch in "\"'":
            in_str = True
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                raw = html[begin : i + 1]
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ModStructureError(f"载荷 JSON 解析失败：{exc}") from exc
                if not isinstance(payload, dict):
                    raise ModStructureError("词缀载荷根节点不是对象")
                _validate_payload_page_identity(html, payload, page_slug, page_url)
                return payload
        i += 1
    raise ModStructureError("载荷括号未配对，HTML 可能被截断")


# ── 文本处理 ────────────────────────────────────────────────

_DASHES = "—–‒―"
_NDASH_SPAN = re.compile(r'<span class="ndash">[^<]*</span>')
_TAG = re.compile(r"<[^>]+>")
#: 换行与块级结束标记。POEDB 用 `<br>` 分隔混合词缀的多行效果，
#: 剥标签前必须先换成分隔符，否则两行会粘成一句读不通的话。
#: `</span>` 是行内标记，**不能**列进来。
_LINE_BREAK = re.compile(r"<\s*br\s*/?\s*>|</\s*(?:div|p|li|tr)\s*>", re.I)
#: 多行词缀的行间分隔符。与 PoB 侧 `PoBMod.text` 的拼接方式保持一致，
#: 两个源用同一种表示才能比对。
LINE_SEP = " / "
_RANGE_RE = re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*[" + _DASHES + r"-]\s*(-?\d+(?:\.\d+)?)\s*\)")
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def clean_mod_text(raw: str) -> str:
    """词缀 HTML → 文本。区间分隔符统一为 ASCII 连字符，数值原样保留。

    **多行词缀必须保留行边界**（实测踩到）：POEDB 的载荷里混合词缀是
    `+(4—6)% 所有元素抗性<br>召唤生物获得 +(5—8)% 元素抗性`。
    早先直接剥掉 `<br>`，两行粘成 `+(4-6)% 所有元素抗性召唤生物获得 +(5-8)% 元素抗性`
    ——既会原样输出给用户，模板也永远匹配不上真实装备上的任何一行。
    影响面不小：约 10% 的词缀行是多值的，工艺台词缀里也有。

    分隔符用 `LINE_SEP`（与 PoB 侧一致），这样两个源的多行词缀表示相同，可比对。
    """
    if not raw:
        return ""
    s = _NDASH_SPAN.sub("-", raw)
    s = _LINE_BREAK.sub(LINE_SEP, s)
    s = _TAG.sub("", s)
    for d in _DASHES:
        s = s.replace(d, "-")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = re.sub(r"\s+", " ", s).strip()
    # 块级标记常成对出现，去掉由此产生的空段与首尾分隔符
    sep = LINE_SEP.strip()
    s = re.sub(rf"(?:\s*{re.escape(sep)}\s*)+", LINE_SEP, s)
    return s.strip().strip(sep).strip()


def parse_ranges(text: str) -> list[tuple[float, float]]:
    """解析**区间标记**，逐位对应严格模板里的 `#`。

    `+(8-12) 敏捷`  → [(8, 12)]
    `+1 技能石等级` → []          ← 固定值词缀没有可变位

    注意不做「无区间则把所有数字当区间」的兜底：那会让固定值词缀
    产生虚假的可变位，与严格模板的 `#` 个数不符，导致反查永久失配。
    固定值词缀的正确语义是「零个可变位，文本须完全一致」。
    """
    return [
        (float(m.group(1)), float(m.group(2)))
        for m in _RANGE_RE.finditer(text)
    ]


def templatize(text: str) -> str:
    """**严格模板**：只把区间标记占位，固定数字原样保留。

    `+(40-55) 力量`                              → `+# 力量`
    `击败敌人时有 (5-8)% 的几率获得 4 秒猛攻状态`  → `击败敌人时有 #% 的几率获得 4 秒猛攻状态`

    为什么固定数字不能占位（实测踩过的坑）：上例里「4 秒」是常量，
    若一并占位成 `#`，反查时装备文本 `...6% ...4 秒...` 会解析出 2 个数值，
    而该词缀只有 1 个区间，长度不等 → 永远匹配不上。
    严格模板用于**校验**：`#` 的个数与位置恰好对应可变数值。
    """
    s = _RANGE_RE.sub("#", text)
    return re.sub(r"\s+", " ", s).strip()


def loose_templatize(text: str) -> str:
    """**宽松模板**：所有数字都占位，用于**召回**。

    装备上的文本是具体数值（`+47 力量`），看不出哪个位置原本是区间、
    哪个是常量，因此召回阶段只能全部占位。召回后再用严格模板逐条校验。
    """
    s = _RANGE_RE.sub("#", text)
    s = _NUM_RE.sub("#", s)
    return re.sub(r"\s+", " ", s).strip()


def strict_template_regex(strict_template: str) -> re.Pattern[str]:
    """把严格模板编译成正则，用于从装备文本中按位置抽取可变数值。

    这一步同时完成两件事：
      - 校验固定数字是否一致（不一致则整体匹配失败）
      - 按 `#` 的位置捕获可变数值，保证与 ranges 逐位对应
    """
    parts = strict_template.split("#")
    pattern = r"\s*".join(re.escape(p).replace(r"\ ", r"\s*") for p in parts[:1])
    for p in parts[1:]:
        pattern += r"(-?\d+(?:\.\d+)?)" + re.escape(p).replace(r"\ ", r"\s*")
    return re.compile(r"^\s*" + pattern + r"\s*$")


def _strip_tags_list(items: Any) -> list[str]:
    """mod_no 之类字段是徽章 HTML 数组，取其纯文本。"""
    if not isinstance(items, list):
        return []
    out = []
    for x in items:
        t = clean_mod_text(str(x))
        if t:
            out.append(t)
    return out


def _as_str_list(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(x) for x in items if isinstance(x, (str, int, float))]


def _basename(url: str) -> str:
    """Code = basename(hover)，与 POEDB 的 `this.basename(a.hover)` 一致。"""
    if not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1]


# ── T 级计算（严格复刻 POEDB）───────────────────────────────

def compute_tiers(records: list[dict[str, Any]]) -> tuple[dict[int, int], dict[str, int]]:
    """按 POEDB 算法计算 T 级。

    返回 (记录下标 → tier, 分组键 → 该组级数)。

    复刻要点：分组键 = `str(ModGenerationTypeID) + CorrectGroup`；
    Level 去重后**降序**；tier = 下标 + 1（最高 Level = T1）。
    """
    buckets: dict[str, list[int]] = {}
    keys: list[str] = []
    for rec in records:
        gen = str(rec.get("ModGenerationTypeID", ""))
        family = rec.get("ModFamilyList") or []
        correct_group = str(family[0]) if family else ""
        key = f"{gen}{correct_group}"
        keys.append(key)
        try:
            lvl = int(rec.get("Level", 0))
        except (TypeError, ValueError):
            lvl = 0
        buckets.setdefault(key, []).append(lvl)

    ordered: dict[str, list[int]] = {}
    for key, levels in buckets.items():
        # 去重后降序 —— 与 `[...new Set(r)]` + `sort((a,b)=>b-a)` 一致
        ordered[key] = sorted(set(levels), reverse=True)

    tiers: dict[int, int] = {}
    for idx, (rec, key) in enumerate(zip(records, keys, strict=True)):
        try:
            lvl = int(rec.get("Level", 0))
        except (TypeError, ValueError):
            lvl = 0
        tiers[idx] = ordered[key].index(lvl) + 1

    counts = {k: len(v) for k, v in ordered.items()}
    return tiers, counts


def _group_records(node: Any) -> list[dict[str, Any]]:
    """词缀组可能是 list，也可能是 dict（veiled/elder/shaper 等）。统一成 list。"""
    if isinstance(node, list):
        return [r for r in node if isinstance(r, dict)]
    if isinstance(node, dict):
        out: list[dict[str, Any]] = []
        for v in node.values():
            if isinstance(v, dict):
                out.append(v)
            elif isinstance(v, list):
                out.extend(r for r in v if isinstance(r, dict))
        return out
    return []


def extract_mods(
    html: str, page_slug: str, page_url: str
) -> tuple[list[ModRecord], ModExtractStats]:
    """从一个底材类别页抽取全部词缀。"""
    payload = extract_payload(html, page_slug, page_url)
    stats = ModExtractStats()

    baseitem = payload.get("baseitem") or {}
    opt = payload.get("opt") or {}
    config = payload.get("config") or {}
    gen_map = payload.get("gen") or {}

    item_class_code = str(opt.get("ItemClassesCode") or baseitem.get("cn") or page_slug)
    item_class_zh = clean_mod_text(str(baseitem.get("link_name") or "")) or item_class_code

    gen_labels = {int(k): clean_mod_text(str(v)) for k, v in gen_map.items() if str(k).isdigit()}
    gen_labels.setdefault(0, "其他")

    out: list[ModRecord] = []
    seen: dict[tuple[str, str, str], ModRecord] = {}

    for category, node in payload.items():
        if category in META_KEYS:
            continue
        records = _group_records(node)
        if not records:
            continue

        cat_cfg = config.get(category) or {}
        category_label = (
            clean_mod_text(str(cat_cfg.get("title", ""))) if isinstance(cat_cfg, dict) else ""
        ) or category

        # T 级在每个来源类别内独立计算
        tiers, tier_counts = compute_tiers(records)

        # 权重占比按生成类型汇总（对齐 POEDB 的 pct 计算口径）
        weight_by_gen: dict[int, int] = {}
        for rec in records:
            try:
                w = int(rec.get("DropChance") or 0)
            except (TypeError, ValueError):
                w = 0
            g = _gen_of(rec)
            weight_by_gen[g] = weight_by_gen.get(g, 0) + w

        for idx, rec in enumerate(records):
            stats.total += 1
            try:
                mod = _build_record(
                    rec=rec,
                    idx=idx,
                    tiers=tiers,
                    tier_counts=tier_counts,
                    weight_by_gen=weight_by_gen,
                    gen_labels=gen_labels,
                    category=category,
                    category_label=category_label,
                    page_slug=page_slug,
                    page_url=page_url,
                    item_class_code=item_class_code,
                    item_class_zh=item_class_zh,
                )
            except Exception as exc:
                stats.fail(f"{category}[{idx}]", f"{type(exc).__name__}: {exc}")
                continue

            if mod.key in seen:
                stats.deduped += 1
                # 去重必须无损：若重复项的实质字段不同，说明主键太粗、正在丢数据。
                # 这种情况要报出来，而不是静默合并。
                kept = seen[mod.key]
                conflicts = _material_diff(kept, mod)
                if conflicts:
                    stats.dedup_conflicts += 1
                    if len(stats.conflict_samples) < 10:
                        stats.conflict_samples.append(
                            (category, mod.name_zh, ", ".join(conflicts))
                        )
                elif len(stats.dup_samples) < 10:
                    stats.dup_samples.append((category, mod.name_zh, mod.text_zh[:60]))
                continue
            seen[mod.key] = mod
            out.append(mod)
            stats.succeeded += 1
            stats.categories[category] = stats.categories.get(category, 0) + 1

    if not stats.total:
        raise ModStructureError(f"{page_slug} 词缀载荷没有任何可识别行")
    if stats.failed:
        detail = "; ".join(f"{i}: {r}" for i, r in stats.failures[:5])
        raise ModStructureError(
            f"{page_slug} 有 {stats.failed}/{stats.total} 行抽取失败；完整池要求零丢失。"
            f"失败样本：{detail}"
        )

    return out, stats


def _gen_of(rec: dict[str, Any]) -> int:
    """生成类型。POEDB 对非 1/2 的值统一归为 0。"""
    raw = str(rec.get("ModGenerationTypeID", ""))
    return int(raw) if raw in ("1", "2") else 0


def _build_record(
    *,
    rec: dict[str, Any],
    idx: int,
    tiers: dict[int, int],
    tier_counts: dict[str, int],
    weight_by_gen: dict[int, int],
    gen_labels: dict[int, str],
    category: str,
    category_label: str,
    page_slug: str,
    page_url: str,
    item_class_code: str,
    item_class_zh: str,
) -> ModRecord:
    family = _as_str_list(rec.get("ModFamilyList"))
    correct_group = family[0] if family else ""
    gen = _gen_of(rec)
    gen_key = f"{rec.get('ModGenerationTypeID', '')}{correct_group}"

    try:
        ilvl = int(rec.get("Level") or 0)
    except (TypeError, ValueError):
        ilvl = 0
    raw_weight = rec.get("DropChance")
    if category == "normal" and raw_weight in (None, ""):
        raise ValueError("normal 词缀缺少 DropChance，不能把缺失权重当成 0")
    try:
        weight = int(raw_weight or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"DropChance 不是整数：{raw_weight!r}") from exc
    if weight < 0:
        raise ValueError(f"DropChance 不能为负数：{weight}")

    text_zh = clean_mod_text(str(rec.get("str") or ""))

    hover = str(rec.get("hover") or "")
    mod_code = str(rec.get("Code") or "") or _basename(hover)
    if not mod_code:
        # 没有稳定标识符时用「类别+组+等级+文本」兜底，保证主键唯一且可复现
        mod_code = f"{category}:{correct_group}:{ilvl}:{abs(hash(text_zh)) % 10**10}"

    # 载荷自带 Tier 的组（essence 等）优先采信数据，其余按 POEDB 算法推导
    payload_tier = rec.get("Tier")
    if payload_tier not in (None, ""):
        try:
            tier = int(payload_tier)
            tier_source = "poedb_payload"
        except (TypeError, ValueError):
            tier = tiers.get(idx, 0)
            tier_source = "derived"
    else:
        tier = tiers.get(idx, 0)
        tier_source = "derived"

    total_w = weight_by_gen.get(gen, 0)
    weight_pct = round(100.0 * weight / total_w, 4) if total_w else 0.0

    return ModRecord(
        mod_code=mod_code,
        item_class_page=page_slug,
        item_class_code=item_class_code,
        item_class_zh=item_class_zh,
        category=category,
        category_label=category_label,
        name_zh=clean_mod_text(str(rec.get("Name") or "")),
        gen_type=gen,
        gen_label=gen_labels.get(gen, "其他"),
        family_list=family,
        correct_group=correct_group,
        ilvl=ilvl,
        tier=tier,
        tier_count=tier_counts.get(gen_key, 0),
        tier_source=tier_source,
        weight=weight,
        weight_pct=weight_pct,
        text_zh=text_zh,
        text_template=templatize(text_zh),
        loose_template=loose_templatize(text_zh),
        ranges=parse_ranges(text_zh),
        tags=_strip_tags_list(rec.get("mod_no")),
        spawn_tags=_as_str_list(rec.get("spawn_no")),
        fossil_tags=_as_str_list(rec.get("fossil_no")),
        url=f"{page_url}#ModifiersCalc",
        variant_id=str(rec.get("BaseItemTypesID") or ""),
    )


#: 去重时必须一致的实质字段。任一不同即说明两条记录承载不同事实，
#: 合并它们会丢信息。
_MATERIAL_FIELDS = ("text_zh", "ilvl", "tier", "weight", "gen_type", "correct_group")


def _material_diff(a: ModRecord, b: ModRecord) -> list[str]:
    """比较两条同主键记录的实质字段，返回不一致的字段名。"""
    diffs: list[str] = []
    for f in _MATERIAL_FIELDS:
        va, vb = getattr(a, f), getattr(b, f)
        if va != vb:
            diffs.append(f"{f}: {va!r} vs {vb!r}")
    return diffs


def are_exclusive(a: ModRecord, b: ModRecord) -> bool:
    """两条词缀是否互斥。

    **用整个 ModFamilyList 求交集**（与 T 级分组只用 [0] 不同），
    对齐 POEDB 的 `getIntersection(a.ModFamilyList, b.ModFamilyList).length > 0`。
    """
    return bool(set(a.family_list) & set(b.family_list))
