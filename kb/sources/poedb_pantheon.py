"""POEDB 逐神详情页抽取器：万神殿的中文名、中文词缀、抓获头目与抓获地图。

## 第十七轮那条结论只对了一半

当时实测 `/cn/Pantheon` 页「0 张表、只有机制说明散文、**各神祇的神力文本不在这一页
上**」——这句是对的；但由此得出的「这个域缺的是**来源**」是错的。逐神页面一直存在，
就是 `/cn/Soul_of_<英文名>`，而且从缓存里 Quest 页的
`data-hover="?s=Data\\PantheonPanelLayout/<ID>"` 链接就能枚举出 10/12 个。
与第三十二轮宝石那轮同一个教训：**缺的是「没去看另一页」**。

## 一页两处，各给一半

**Attr 表**（`ID` / `Name` / `IsMajor` / `Main` / `Defeat1..3`）给桥接键与对账值：

    Main      暗影女皇 阿拉卡力之魂 <li>degen_effect_+% -10</li>
    Defeat1   切断者马雷格罗       <li>life_and_energy_shield_recovery_rate_+%_... 20</li>

**`div.PantheonPopup`** 给中文正文：章节与任务、每层的中文词缀、`抓获<头目>` 与
那个头目所在的**地图**。两处按 `div.separator` 分段，段序与 Attr 表行序一致
（实测 32/32 层的词缀条数、头目名两侧完全一致）。

## 桥接键是 `ID`，不是名字

Attr 表的 `ID` 逐字等于 `Pantheons.lua` 的表键（`Arakaali`），零多解——
与宝石那条 `grantedEffectId` 同一种键。两道判据缺一不可：`ID` 必须等于请求用的
PoB 键，`Name` 必须等于 PoB 侧的基础神力英文名。slug 是由英文名推出来的
（`Soul of the Brine King` → `Soul_of_the_Brine_King`），**推错时 POEDB 会给别的
条目而不是 404**，所以必须由页面自证。实测 12/12 两道判据全过，其中 10 个另有
Quest 页链接独立佐证。

## 中文词缀与 stat id **不逐项对齐**，不要 zip

同一层里，popup 的词缀顺序与 PoB / Attr 表的顺序**不一致**（实测艾贝拉斯第二层：
Attr 是「不受燃烧地面影响、移动速度」，popup 是「移动速度、不受燃烧地面影响」）。
按位置配对会把中文文本挂到错误的 stat id 上。

按数值配对同样不行，两个原因：**同一层里可能两条值相同**（月影女神第一层两条都是
布尔值 1、幽格尔第一层两条都是 50），而且 **PoB 的 `value` 是 stat 原生单位**
（图克哈玛第二层存 120 /分钟，页面显示 2%/秒）。

所以两侧各自成列、**并列存放而不配对**：中文用于展示与检索，PoB 侧用于英文检索与
溯源，对账在 `(stat_id, value)` 这一维上做（Attr 表与 PoB 两侧口径一致，实测 41/41）。
「凑得出来的对应关系」不等于「查得到的对应关系」。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from selectolax.parser import HTMLParser

POEDB_BASE_ZH = "https://poedb.tw/cn"

#: Attr 表里的层序键，顺序即层序。主神 4 层，次神只有前 2 个。
TIER_KEYS = ("Main", "Defeat1", "Defeat2", "Defeat3")

#: Attr 表里的标量行。
ATTR_ID = "ID"
ATTR_NAME = "Name"
ATTR_IS_MAJOR = "IsMajor"

#: `<li>` 里的 `stat_id value`。值可能带正负号与小数。
_STAT_LI_RE = re.compile(r"^(\S+)\s+([+-]?\d+(?:\.\d+)?)$")

#: 「抓获」前缀。页面把头目名放在这个词后面的链接里。
_CAPTURE_PREFIX = "抓获"


class PantheonPageError(ValueError):
    """逐神页结构与预期不符。修抽取器，不要放宽断言。"""


@dataclass(frozen=True)
class PageTier:
    """一层神力在页面上的样子。

    `mods_zh` 是**页面顺序**的中文词缀，`stats` 是 Attr 表顺序的
    `(stat_id, value)`。**两者不逐项对应**，理由见模块 docstring。
    """

    index: int
    name_zh: str
    stats: tuple[tuple[str, float], ...]
    mods_zh: tuple[str, ...]
    #: 第 1 层是任务奖励，没有抓获目标；2..4 层才有头目与地图。
    boss_zh: str = ""
    map_zh: str = ""
    act_zh: str = ""
    quest_zh: str = ""


@dataclass(frozen=True)
class PantheonPage:
    """一张逐神页。"""

    god_key: str
    name_en: str
    name_zh: str
    is_major: bool
    poedb_slug: str
    tiers: tuple[PageTier, ...]

    @property
    def poedb_url(self) -> str:
        return f"{POEDB_BASE_ZH}/{self.poedb_slug}"

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [
                self.god_key,
                self.name_en,
                self.name_zh,
                self.is_major,
                [
                    [t.index, t.name_zh, list(t.stats), list(t.mods_zh),
                     t.boss_zh, t.map_zh, t.act_zh, t.quest_zh]
                    for t in self.tiers
                ],
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _text(node) -> str:  # noqa: ANN001
    """与 `poedb_unique._text` 同一口径：`deep=True` 不插分隔符、只归一空白。

    词缀正文里数值是独立的 `<span class='mod-value'>`
    （`受到的持续性伤害降低 <span>10</span>%`）。插空格会写成「降低 10 %」，
    不插又会粘成「降低10%」——两者都不是页面上的样子，而卡片措辞要与
    唯一装备那边一致，否则出口的数值回填会在两种说法之间对不上。
    """
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.text(deep=True)).strip()


def _attr_rows(tree: HTMLParser) -> dict[str, object]:
    """Attr 表 → `{键: 单元格节点}`。按「两列」定位而不是按表下标。"""
    rows: dict[str, object] = {}
    for table in tree.css("table"):
        for tr in table.css("tr"):
            cells = tr.css("td")
            if len(cells) == 2:
                rows.setdefault(_text(cells[0]), cells[1])
    return rows


def _parse_stats(cell) -> tuple[str, tuple[tuple[str, float], ...]]:  # noqa: ANN001
    """Attr 单元格 → (层名, `(stat_id, value)` 序列)。

    层名是去掉全部 `<li>` 之后剩下的文本；`<li>` 是 `stat_id value`。
    """
    stats: list[tuple[str, float]] = []
    for li in cell.css("li"):
        raw = _text(li)
        matched = _STAT_LI_RE.match(raw)
        if not matched:
            raise PantheonPageError(f"Attr 表的词缀项不是 `stat_id 数值` 形态：{raw!r}")
        stats.append((matched.group(1), float(matched.group(2))))
    name = _text(cell)
    for li in cell.css("li"):
        name = name.replace(_text(li), "")
    return re.sub(r"\s+", " ", name).strip(), tuple(stats)


def _popup_segments(tree: HTMLParser) -> list[dict]:
    """`div.PantheonPopup` 的正文按 `div.separator` 分段。

    段内按 class 取：`explicitMod` 是词缀正文，`text-value` 是「第 N 章」或
    「抓获<头目>」，`questitem` 是任务，class 里含 `Map` 的链接是抓获地图。
    """
    stats_block = tree.css_first("div.PantheonPopup div.Stats div.implicitMod")
    if stats_block is None:
        raise PantheonPageError("页面上找不到 div.PantheonPopup 的正文块")

    segments: list[dict] = [{"mods": [], "labels": [], "map": "", "quest": ""}]
    for node in stats_block.iter():
        classes = (node.attributes.get("class") or "").split()
        if "separator" in classes:
            segments.append({"mods": [], "labels": [], "map": "", "quest": ""})
        elif "explicitMod" in classes:
            segments[-1]["mods"].append(_text(node))
        elif "text-value" in classes:
            segments[-1]["labels"].append(_text(node))
        elif "questitem" in classes:
            segments[-1]["quest"] = _text(node)
        elif "Map" in classes:
            segments[-1]["map"] = _text(node)
    return segments


def parse_pantheon_page(
    html: str, *, pob_key: str, name_en: str, soul_count: int
) -> PantheonPage:
    """抽取一张逐神页。两道桥接判据不过就抛错——**不猜**。

    `pob_key` / `name_en` / `soul_count` 都来自 PoB 侧，用来自证这一页确实是
    我们要的那位神祇（slug 是推出来的，推错时 POEDB 会给别的条目而不是 404）。
    """
    tree = HTMLParser(html)
    rows = _attr_rows(tree)

    page_id = _text(rows[ATTR_ID]) if ATTR_ID in rows else ""
    if page_id != pob_key:
        raise PantheonPageError(
            f"页面 ID 与 PoB 键不符：页面 {page_id!r} ≠ PoB {pob_key!r}"
        )
    page_name = _text(rows[ATTR_NAME]) if ATTR_NAME in rows else ""
    if page_name != name_en:
        raise PantheonPageError(
            f"页面 Name 与 PoB 英文名不符：页面 {page_name!r} ≠ PoB {name_en!r}"
        )

    is_major = _text(rows[ATTR_IS_MAJOR]) == "1" if ATTR_IS_MAJOR in rows else False
    segments = _popup_segments(tree)

    tiers: list[PageTier] = []
    for idx in range(soul_count):
        key = TIER_KEYS[idx]
        if key not in rows:
            raise PantheonPageError(f"{pob_key} 的 Attr 表缺第 {idx + 1} 层（{key}）")
        if idx >= len(segments):
            raise PantheonPageError(
                f"{pob_key} 的正文只有 {len(segments)} 段，不足 {soul_count} 层"
            )
        name_zh, stats = _parse_stats(rows[key])
        seg = segments[idx]
        mods_zh = tuple(seg["mods"])
        if len(mods_zh) != len(stats):
            # 两处都在同一页上，条数对不上说明分段判据错了，不是上游少给
            raise PantheonPageError(
                f"{pob_key} 第 {idx + 1} 层：正文 {len(mods_zh)} 条词缀，"
                f"Attr 表 {len(stats)} 条"
            )
        boss = ""
        act = ""
        for label in seg["labels"]:
            if label.startswith(_CAPTURE_PREFIX):
                boss = label[len(_CAPTURE_PREFIX):].strip()
            else:
                act = label
        if idx > 0 and boss != name_zh:
            # Attr 表的层名就是要打的那个头目，两处不一致说明段序错位了
            raise PantheonPageError(
                f"{pob_key} 第 {idx + 1} 层：正文抓获目标 {boss!r} ≠ Attr 层名 {name_zh!r}"
            )
        tiers.append(
            PageTier(
                index=idx + 1,
                name_zh=name_zh,
                stats=stats,
                mods_zh=mods_zh,
                boss_zh=boss,
                map_zh=str(seg["map"]),
                act_zh=act,
                quest_zh=str(seg["quest"]),
            )
        )

    god_name_zh = tiers[0].name_zh
    if not re.search(r"[一-鿿]", god_name_zh):
        raise PantheonPageError(f"{pob_key} 的中文名里没有中文：{god_name_zh!r}")

    return PantheonPage(
        god_key=pob_key,
        name_en=name_en,
        name_zh=god_name_zh,
        is_major=is_major,
        poedb_slug=name_en.replace(" ", "_"),
        tiers=tuple(tiers),
    )


@dataclass(frozen=True)
class Mismatch:
    """两侧对不上的一处。整位神祇因此被拒绝入库。"""

    god_key: str
    tier: int
    field: str
    poedb: str
    pob: str

    def __str__(self) -> str:
        return (
            f"{self.god_key} 第 {self.tier} 层 {self.field}："
            f"POEDB {self.poedb!r} ≠ PoB {self.pob!r}"
        )


def reconcile(page: PantheonPage, god) -> list[Mismatch]:  # noqa: ANN001
    """逐条对账 `(stat_id, value)`，并核对主神标记与层数。

    这一维**自带对账口径**：两侧都给 GGG stat id 与原生数值，所以是全量比对
    而不是抽样。对不上就整位拒绝——宁可少一位神祇，也不要把两个版本的数据
    拼在一起（与宝石那 8 颗同一条纪律）。
    """
    out: list[Mismatch] = []
    if page.is_major != god.is_major:
        out.append(
            Mismatch(god.key, 0, "IsMajor", str(page.is_major), str(god.is_major))
        )
    if len(page.tiers) != len(god.souls):
        out.append(
            Mismatch(god.key, 0, "层数", str(len(page.tiers)), str(len(god.souls)))
        )
        return out
    for tier, soul in zip(page.tiers, god.souls):
        want = [(m.stat_id, m.value) for m in soul.mods]
        got = list(tier.stats)
        if len(got) != len(want):
            out.append(
                Mismatch(god.key, tier.index, "词缀条数", str(len(got)), str(len(want)))
            )
            continue
        for (gid, gval), (wid, wval) in zip(got, want):
            if gid != wid:
                out.append(Mismatch(god.key, tier.index, "stat id", gid, wid))
            elif gval != wval:
                out.append(
                    Mismatch(god.key, tier.index, f"{gid} 的值", f"{gval:g}", f"{wval:g}")
                )
    return out
