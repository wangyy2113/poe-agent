"""POEDB 唯一装备抽取器。

数据来源：https://poedb.tw/cn/Unique_item
该页 2.5MB，把全部唯一装备连同完整词缀内联在三个 tab-pane 里
（Weapon /284、Armour /434、Other /511，客户端切换所以内容全在 HTML）。
因此**一次抓取即可拿到全部数据**，无需逐个抓 1229 个详情页——对站点更友好。

每件装备的 DOM 单元（2026-08-04 实测）：
    <div class="col">
      <div class="d-flex border-top rounded">
        <div class="flex-shrink-0"><a class="UniqueItems UniqueItem" href="Slug"><img .../></a></div>
        <div class="flex-grow-1 ms-2">
          <div><a class="UniqueItem" href="/cn/Slug">
                 <span class="uniqueName">中文名</span>
                 <span class="uniqueTypeLine">底材</span></a></div>
          <div class="requirements">需求 等级 <span>1</span>, <span>14</span> 智慧</div>
          <div class="implicitMod">...</div>
          <div class="separator"></div>
          <div class="explicitMod">...</div>   ← 0..n
        </div>
      </div>
    </div>

结构断言：关键字段缺失即判为抽取失败并排除该条，不以部分字段写入知识库
（需求 2.7）。这样上游改版会立刻暴露成失败计数，而不是静默产生脏数据。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser, Node

from entities.models import (
    Entity,
    ModLine,
    Requirement,
    SourceRef,
    ValueRange,
)
from entities.taxonomy import EntityType, ModKind, Source, make_entity_id
from retrieval.tokenize import content_words

logger = logging.getLogger(__name__)

LIST_URL = "https://poedb.tw/cn/Unique_item"
BASE_URL = "https://poedb.tw"

#: 只取这三个 pane：它们是常规唯一装备（含 93 件秽生 Replica）。
#: 另有 秽生传奇物品 /372 与 传奇物品Recipe /23 两个 pane，实测**不含**
#: `div.col` + `span.uniqueName` 这套卡片结构，故不会被误收。
TARGET_PANES = ("Weapon传奇", "Armour传奇", "Other传奇")

#: 英文语言版索引页。用它取权威英文名与英文底材名——
#: **一次请求覆盖全部 1231 件**，无需逐个抓详情页（实测 373/1231 即 30% 的英文名
#: 无法由 slug 反推，如 `Abberaths_Horn` → `Abberath's Horn`、
#: `Titucius_Span` → `Titucius' Span`，所以这份数据不可省）。
EN_LIST_URL = "https://poedb.tw/us/Unique_item"

#: Royale 是独立游戏模式，其物品不存在于主线游戏，必须排除——
#: 否则会答出「这件装备存在」而主线里根本没有。
#: 秽生（Replica）**不排除**：它们是劫红装的真实物品，库里已有 93 件。
ROYALE_PREFIX = "Royale_"

#: 类别页（如 /cn/Body_Armours、/cn/Cobalt_Jewel）也列出该类别的全部传奇，
#: 且覆盖了索引页遗漏的物品（实测多出 111 件，含「无尽之衣 Tabula Rasa」——
#: 它在 /cn/Unique_item 与 /us/Unique_item 上都不存在，但详情页确实存在）。
#:
#: **不按 tab id 判定传奇区块**：id 形态不统一，珠宝页是「传奇」，胸甲页是
#: 「胸甲传奇」，鱼竿与酊剂页竟是「Acronym」（疑为 POEDB 模板缺陷，但里面
#: 装的是真实传奇）。改为收集页面上全部含 `span.uniqueName` 的 `div.col`。

#: POEDB 用 em dash 表示区间；也见过 en dash，两者都归一化
_DASHES = "—–‒―"
_RANGE_RE = re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*[" + _DASHES + r"-]\s*(-?\d+(?:\.\d+)?)\s*\)")
_SINGLE_RE = re.compile(r"(?<![\d.])(-?\d+(?:\.\d+)?)(?![\d.])")


#: 零词缀物品占比告警线。实测 1229 件中仅 13 件（约 1.1%）为碎片/契约/奇珍类
#: 无属性物品。若该比例大幅升高，说明词缀节点的 class 变了，此时应让采集失败
#: 而不是写入大量空壳实体。
ZERO_MOD_ALERT_RATIO = 0.10

#: 抽取成功率下限。低于此值判为页面结构变更。
MIN_SUCCESS_RATIO = 0.90


@dataclass
class ExtractStats:
    """抽取统计。失败项保留原因，便于定位上游改版。"""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    zero_mod: int = 0
    #: 按规则主动跳过的条目（当前只有 Royale 模式物品）。
    #: 与 failed 分开计数：跳过是设计意图，失败是需要排查的信号。
    skipped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    def fail(self, ident: str, reason: str) -> None:
        self.failed += 1
        self.failures.append((ident, reason))

    @property
    def success_ratio(self) -> float:
        return self.succeeded / self.total if self.total else 0.0

    @property
    def zero_mod_ratio(self) -> float:
        return self.zero_mod / self.succeeded if self.succeeded else 0.0


class StructureError(ValueError):
    """结构断言失败：页面结构与预期不符。"""


def _text(node: Node | None) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.text(deep=True)).strip()


def parse_value_ranges(text: str) -> list[ValueRange]:
    """从词缀原文解析数值区间。

    `+(40—55) 力量`      → [(40, 55)]
    `法术伤害提高 (8—12)%` → [(8, 12)]
    `此物品上装备的法术技能石等级 +1` → [(1, 1)]
    """
    ranges: list[ValueRange] = []
    consumed = text
    for m in _RANGE_RE.finditer(text):
        low, high = float(m.group(1)), float(m.group(2))
        ranges.append(ValueRange(low=low, high=high))
        consumed = consumed.replace(m.group(0), " ")
    if not ranges:
        for m in _SINGLE_RE.finditer(consumed):
            v = float(m.group(1))
            ranges.append(ValueRange(low=v, high=v))
    return ranges


def _normalize_mod_text(node: Node) -> str:
    """把词缀节点转成保留区间标记的单行文本。

    `<span class='mod-value'>+(40<span class="ndash">—</span>55)</span> 力量`
        → `+(40-55) 力量`
    区间分隔符统一成 ASCII 连字符，便于后续匹配与展示；
    数值本身原样保留，不做任何改写。
    """
    raw = node.html or ""
    # ndash span 只是视觉分隔符，替换为连字符
    raw = re.sub(r'<span class="ndash">[^<]*</span>', "-", raw)
    text = HTMLParser(raw).text(deep=True)
    for d in _DASHES:
        text = text.replace(d, "-")
    return re.sub(r"\s+", " ", text).strip()


def _parse_requirement_raw(raw: str) -> Requirement | None:
    """从需求原文构造 Requirement。索引页给节点、详情页给纯文本，共用这一份解析。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    req = Requirement(raw=raw)
    m = re.search(r"等级\s*(\d+)", raw)
    if m:
        req.level = int(m.group(1))
    for attr, field_name in (("力量", "strength"), ("敏捷", "dexterity"), ("智慧", "intelligence")):
        m = re.search(r"(\d+)\s*" + attr, raw)
        if m:
            setattr(req, field_name, int(m.group(1)))
    return req


def _parse_requirement(node: Node | None) -> Requirement | None:
    return _parse_requirement_raw(_text(node)) if node is not None else None


def _extract_mods(container: Node) -> list[ModLine]:
    """按 DOM 顺序抽取隐式与显式词缀。

    `explicitMod` 内若只含 `span.item_description`，那是括号说明文本而非
    真实词条，归为 DESCRIPTION，不计入事实词缀。
    """
    mods: list[ModLine] = []
    for node in container.css("div.implicitMod, div.explicitMod"):
        classes = (node.attributes.get("class") or "").split()
        text = _normalize_mod_text(node)
        if not text:
            continue

        if "implicitMod" in classes:
            kind = ModKind.IMPLICIT
        else:
            only_desc = node.css_first("span.item_description") is not None and not re.sub(
                r"[（(].*?[)）]", "", text
            ).strip()
            kind = ModKind.DESCRIPTION if only_desc else ModKind.EXPLICIT

        mods.append(ModLine(kind=kind, text=text, ranges=parse_value_ranges(text)))
    return mods


def _make_keywords(name_zh: str, base_type: str, mods: list[ModLine]) -> list[str]:
    """生成描述性关键词，补偿向量通道关闭时的语义检索能力（需求 3.12）。

    与 FTS 索引共用 retrieval.tokenize 的切分，保证写入与查询一致。
    """
    extra = [name_zh]
    if base_type:
        extra.append(base_type)
    return content_words([m.text for m in mods if m.is_factual], extra=extra)


def _find_name_link(col: Node) -> Node | None:
    """找到承载物品名的那个 <a>。

    注意：一个 div.col 里有两个 class 含 `UniqueItem` 的链接——
      1. 图片链接 `a.UniqueItems.UniqueItem`，只含 <img>，href 无 /cn/ 前缀
      2. 名称链接 `a.UniqueItem`，含 span.uniqueName 与 span.uniqueTypeLine
    `css_first("a.UniqueItem")` 会命中第 1 个，所以必须按是否含 uniqueName 来挑。
    """
    for link in col.css("a[href]"):
        if link.css_first("span.uniqueName") is not None:
            return link
    return None


def _parse_item_card(col: Node, *, item_class: str, attributes: dict) -> Entity:
    """解析单个 div.col。任何关键字段缺失都抛 StructureError。

    `item_class` 与 `attributes` 由调用方给出，因为同一套卡片结构出现在
    索引页与类别页两处，两处能提供的类别信息粒度不同（索引页只有
    武器/护甲/其他这种 pane 分组，类别页给的是「胸甲」「钴蓝珠宝」这类真实
    物品类别）。把来源差异留在调用方，解析逻辑保持单一。
    """
    link = _find_name_link(col)
    if link is None:
        raise StructureError("未找到含 span.uniqueName 的 a[href]")

    name_node = link.css_first("span.uniqueName")
    if name_node is None:
        raise StructureError("未找到 span.uniqueName")
    name_zh = _text(name_node)
    if not name_zh:
        raise StructureError("uniqueName 为空")

    base_type = _text(link.css_first("span.uniqueTypeLine"))

    href = (link.attributes.get("href") or "").strip()
    if not href:
        raise StructureError("href 为空")
    slug = href.rsplit("/", 1)[-1]
    if not slug:
        raise StructureError(f"无法从 href 解析 slug: {href}")
    url = href if href.startswith("http") else f"{BASE_URL}/cn/{slug}"

    body = link.parent.parent if link.parent is not None else col
    # 零词缀是合法情况：碎片、契约、奇珍等物品只有需求等级没有属性词条
    # （实测 1229 件中 13 件如此）。因此不在单条上断言，改为在聚合层面断言，
    # 见 extract_uniques 里的 ZERO_MOD_ALERT_RATIO。
    mods = _extract_mods(body if body is not None else col)

    requirement = _parse_requirement((body or col).css_first("div.requirements"))

    return Entity(
        entity_id=make_entity_id(EntityType.UNIQUE_ITEM, slug),
        entity_type=EntityType.UNIQUE_ITEM,
        name_zh=name_zh,
        # 权威英文名只能来自详情页；此处**不**由 slug 反推
        # （Abberaths_Horn → Abberath's Horn，撇号会丢）
        name_en=None,
        slug=slug,
        base_type=base_type or None,
        item_class=item_class,
        mods=mods,
        requirement=requirement,
        search_keywords=_make_keywords(name_zh, base_type, mods),
        attributes=dict(attributes),
        ref=SourceRef(source=Source.POEDB.value, url=url, priority=1),
    )


def _pane_to_class(pane: str) -> str:
    return {
        "Weapon传奇": "武器",
        "Armour传奇": "护甲",
        "Other传奇": "其他",
    }.get(pane, pane)


def _tab_id_of(node: Node) -> str:
    """向上找最近的 `tab-pane` 祖先并返回其 id，找不到返回空串。"""
    cur: Node | None = node
    for _ in range(12):
        cur = cur.parent if cur is not None else None
        if cur is None:
            return ""
        if "tab-pane" in (cur.attributes.get("class") or ""):
            return cur.attributes.get("id") or ""
    return ""


def _iter_unique_cards(tree: HTMLParser):  # noqa: ANN201
    """遍历页面上全部唯一装备卡片，产出 (div.col 节点, 所在 tab id)。"""
    for col in tree.css("div.col"):
        if _find_name_link(col) is not None:
            yield col, _tab_id_of(col)


def _slug_of_card(col: Node) -> str:
    link = _find_name_link(col)
    if link is None:
        return ""
    return (link.attributes.get("href") or "").rsplit("/", 1)[-1]


def _class_from_tab(tab_id: str) -> str:
    """从传奇 tab 的 id 推出物品类别：`胸甲传奇` → `胸甲`。

    id 不以「传奇」结尾时（如鱼竿页的 `Acronym`）返回空串——
    宁可留空，也不猜一个类别名填进事实字段。
    """
    if tab_id.endswith("传奇") and len(tab_id) > 2:
        return tab_id[:-2]
    return ""


def extract_uniques_from_class_page(
    html: str, page_slug: str, url: str
) -> tuple[list[Entity], ExtractStats]:
    """从底材类别页抽取该类别的全部唯一装备。

    这是索引页的**补集来源**：实测类别页并集比 `/cn/Unique_item` 多 111 件
    （多为唯一珠宝，以及「无尽之衣」这类索引页完全没列的物品）。
    页面已在本地缓存里，因此这一步零网络请求。
    """
    tree = HTMLParser(html)
    stats = ExtractStats()
    entities: list[Entity] = []
    seen: set[str] = set()

    for col, tab in _iter_unique_cards(tree):
        stats.total += 1
        if _slug_of_card(col).startswith(ROYALE_PREFIX):
            stats.skipped += 1
            continue
        try:
            entity = _parse_item_card(
                col,
                item_class=_class_from_tab(tab),
                attributes={"source_page": page_slug, "tab": tab},
            )
        except StructureError as exc:
            stats.fail(_text(col.css_first("span.uniqueName")) or "<未知>", str(exc))
            continue
        except Exception as exc:  # 解析期意外错误也不得静默
            stats.fail("<异常>", f"{type(exc).__name__}: {exc}")
            continue

        if entity.entity_id in seen:
            continue
        seen.add(entity.entity_id)
        entities.append(entity)
        stats.succeeded += 1
        if not entity.factual_mods:
            stats.zero_mod += 1

    return entities, stats


def extract_en_index(html: str) -> dict[str, tuple[str, str]]:
    """从英文语言版索引页取 slug → (权威英文名, 英文底材名)。

    slug 是两个语言版共用的 join key。**这是唯一不需要逐个抓详情页就能拿到
    权威英文名的途径**——列表页只有中文名，而 slug 反推会丢撇号
    （`Abberaths_Horn` 的真名是 `Abberath's Horn`）。
    """
    tree = HTMLParser(html)
    out: dict[str, tuple[str, str]] = {}
    for col, _tab in _iter_unique_cards(tree):
        slug = _slug_of_card(col)
        if not slug or slug.startswith(ROYALE_PREFIX):
            continue
        link = _find_name_link(col)
        if link is None:
            continue
        name_en = _text(link.css_first("span.uniqueName"))
        base_en = _text(link.css_first("span.uniqueTypeLine"))
        if name_en:
            out.setdefault(slug, (name_en, base_en))
    if not out:
        raise StructureError(
            f"英文索引页未抽到任何条目（{EN_LIST_URL}），结构可能已变更；"
            "拒绝继续以免把 name_en 全部清空"
        )
    return out


@dataclass
class DetailPage:
    """单品详情页解析结果，用于交叉核对与取权威英文名。"""

    name_zh: str = ""
    #: 权威英文名（详情页 FlavourText 之后的裸 div），列表页拿不到
    name_en: str | None = None
    base_type: str = ""
    item_class: str = ""
    requirement_raw: str = ""
    mods: list[ModLine] = field(default_factory=list)
    flavour_text: str | None = None

    @property
    def factual_mod_texts(self) -> list[str]:
        return [m.text for m in self.mods if m.is_factual]


def parse_detail_page(html: str) -> DetailPage:
    """解析单品详情页。

    页面上可能有多个 `div.newItemPopup`（底材卡、相关物品等），
    唯一装备本体带 `UniquePopup` class，优先取它。
    """
    tree = HTMLParser(html)
    popup = tree.css_first("div.newItemPopup.UniquePopup") or tree.css_first("div.newItemPopup")
    if popup is None:
        raise StructureError("详情页未找到 div.newItemPopup")

    out = DetailPage()

    header = popup.css_first("div.itemHeader")
    if header is not None:
        names = header.css("div.itemName")
        if names:
            out.name_zh = _text(names[0].css_first("span.lc") or names[0])
        type_line = header.css_first("div.itemName.typeLine")
        if type_line is not None:
            out.base_type = _text(type_line.css_first("span.lc") or type_line)

    content = popup.css_first("div.content")
    if content is None:
        raise StructureError("详情页 popup 内未找到 div.content")

    props = [_text(p) for p in content.css("div.property")]
    if props:
        out.item_class = props[0]

    req = content.css_first("div.requirements")
    if req is not None:
        out.requirement_raw = _text(req)

    out.mods = _extract_mods(content)

    flavour = content.css_first("div.FlavourText")
    if flavour is not None:
        out.flavour_text = _text(flavour)

    # 权威英文名：content 内最后一个无 class 且含 ASCII 字母的裸 div
    for div in reversed(content.css("div")):
        if div.attributes.get("class"):
            continue
        txt = _text(div)
        if txt and re.fullmatch(r"[A-Za-z0-9'’\-,.:!? ]+", txt):
            out.name_en = txt
            break

    return out


def entity_from_detail(slug: str, url: str, detail: DetailPage) -> Entity:
    """详情页 → 实体（L2 补全通道）。

    什么时候用它：某件装备在任何索引页上都找不到，只能从它自己的详情页取。
    索引页能覆盖时**不要**走这条路——逐个抓详情页对站点不友好，且详情页的
    字段完整度反而更差（实测 `The_Wellhook` 的底材与类别在详情页解析为空，
    `The_Draugurs_Lantern` 连中文名都没有，POEDB 未本地化）。

    关键字段缺失即抛 StructureError，不以部分字段入库（需求 2.7）。
    """
    if not slug:
        raise StructureError("slug 为空")
    name_zh = detail.name_zh.strip()
    if not name_zh:
        raise StructureError(f"详情页未取到名称：{slug}")

    return Entity(
        entity_id=make_entity_id(EntityType.UNIQUE_ITEM, slug),
        entity_type=EntityType.UNIQUE_ITEM,
        name_zh=name_zh,
        # 详情页的裸 div 是权威英文名来源；取不到就留空，不由 slug 反推
        name_en=detail.name_en,
        slug=slug,
        base_type=detail.base_type or None,
        item_class=detail.item_class or "",
        mods=detail.mods,
        requirement=_parse_requirement_raw(detail.requirement_raw),
        flavour_text=detail.flavour_text,
        search_keywords=_make_keywords(name_zh, detail.base_type, detail.mods),
        attributes={"source_page": slug, "via": "detail"},
        ref=SourceRef(source=Source.POEDB.value, url=url, priority=1),
    )


def extract_uniques(html: str) -> tuple[list[Entity], ExtractStats]:
    """从 Unique_item 页抽取全部唯一装备。

    返回 (实体列表, 统计)。单条失败不影响整体，但会计入 failures 供排查。
    """
    tree = HTMLParser(html)
    stats = ExtractStats()
    entities: list[Entity] = []
    seen: set[str] = set()

    found_panes = 0
    for pane_id in TARGET_PANES:
        pane = tree.css_first(f'div[id="{pane_id}"]')
        if pane is None:
            logger.warning("未找到 tab-pane: %s（POEDB 可能改版）", pane_id)
            continue
        found_panes += 1

        cols = pane.css("div.col")
        logger.info("pane %s: %d 个候选卡片", pane_id, len(cols))
        for col in cols:
            stats.total += 1
            try:
                entity = _parse_item_card(
                    col,
                    item_class=_pane_to_class(pane_id),
                    attributes={"pane": pane_id},
                )
            except StructureError as exc:
                ident = _text(col.css_first("span.uniqueName")) or "<未知>"
                stats.fail(ident, str(exc))
                continue
            except Exception as exc:  # 解析期意外错误也不得静默
                stats.fail("<异常>", f"{type(exc).__name__}: {exc}")
                continue

            if entity.entity_id in seen:
                continue
            seen.add(entity.entity_id)
            entities.append(entity)
            stats.succeeded += 1
            if not entity.factual_mods:
                stats.zero_mod += 1

    # ── 聚合层结构断言：宁可整批失败，也不写入大量空壳或残缺实体 ──
    if found_panes == 0:
        raise StructureError(
            f"三个目标 tab-pane 全部未找到（{TARGET_PANES}），"
            "POEDB 页面结构可能已变更，拒绝写入以免产生脏数据"
        )
    if stats.total and stats.success_ratio < MIN_SUCCESS_RATIO:
        raise StructureError(
            f"抽取成功率 {stats.success_ratio:.1%} 低于下限 {MIN_SUCCESS_RATIO:.0%}"
            f"（{stats.succeeded}/{stats.total}），判为页面结构变更"
        )
    if stats.zero_mod_ratio > ZERO_MOD_ALERT_RATIO:
        raise StructureError(
            f"零词缀物品占比 {stats.zero_mod_ratio:.1%} 超过告警线 "
            f"{ZERO_MOD_ALERT_RATIO:.0%}（{stats.zero_mod}/{stats.succeeded}），"
            "词缀节点的 class 可能已变更"
        )

    return entities, stats
