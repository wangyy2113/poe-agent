"""POEDB `/cn/Monsters` 抽取器：怪物词缀 + 幽魂可用怪物倍率。

这一页有九张表，本模块只取两张，另外几张**刻意不取**，理由逐条写下来，
免得下一个人以为漏了：

===================================  ==========================================
「怪物」表（501 行）                  取。词缀来源 × 名字 × 效果文本
按等级的基础数值表（100 行）           不取。PoB 侧已在 `game_constant` 里
                                      （`monsterLifeTable` / `monsterDamageTable` /
                                      `monsterArmourTable` / `monsterEvasionTable` /
                                      `monsterAccuracyTable`），重复入库只会制造
                                      两份可能不一致的真源
魔法/稀有加成、首领加成（68 / 82 行）  不取。同上，PoB 的 `monsterConstants`
                                      与 `mapLevelLifeMult` 已覆盖
`Stat` 三张小表（14/15/13 行）        不取。`monster_slain_experience` 这类内部
                                      stat id，没有中文，不可面向用户
最后一张表（2026-08-19 为 1948 行；
2026-10-30 为 1796 行）               取。幽魂可用怪物的伤害% / 生命%
===================================  ==========================================

## 怪物词缀单元格里混着两种东西

一个 `span.explicitMod` 里可能同时有翻译好的中文与 `span.secondary` 包着的
**未翻译内部 stat id**（`monster dropped item rarity +% [0]`）。实测 501 行分四类：

  125 行只有中文        入库
   58 行中文 + 原始 id  入库，原始 id 单独存 `raw_stats`
   63 行只有原始 id     跳过（把调试字段当中文答案输出，比查不到更糟）
  248 行没有效果文本     跳过（页面只列了词缀名）

（2026-08-19 实测。入库 183 条 = 125 + 58，另有 12 组同名不同档的词缀，
如「死眼」的「伤害提高 30%」与「伤害提高 20%」，两条都留。）

## 幽魂表的键

`data-hover` 有两种形态：`?s=Data\\MonsterVarieties/<id>` 与 tooltip 缓存 URL
（`Poe_Data_MonsterVarieties_hover/<hash>`）。两种形态的占比会随页面重抓而变——
2026-08-13 的缓存是 1508 / 407，2026-08-19 重抓后是 **1921 / 27**——所以两条路都要留。

**不能用中文名或 slug 当键**：1948 行里只有 1610 个不同中文名，`Daemon` 一个 slug
挂了 27 行且名字各不相同，拿它们当键就是把不同怪物合并成一个。
2026-10-30 的重新抓取中，页面实际只列 1796 行；按名称、slug 与两个倍率比较，
它是旧快照的严格子集，少 152 行。同步将此标为 attention，不能把旧数当成现版事实。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from selectolax.parser import HTMLParser

from entities.taxonomy import Source

POEDB_MONSTERS_PAGE = "Monsters"
POEDB_MONSTERS_URL = f"https://poedb.tw/cn/{POEDB_MONSTERS_PAGE}"

#: 怪物词缀表的表头，用来定位而不是按下标取表——下标会随页面加表而错位
MOD_TABLE_HEADERS = ("类型", "等级", "名字", "词缀")
#: 幽魂表的表头
SPECTRE_TABLE_HEADERS = ("名字", "伤害%", "生命%", "幽魂")

SKIP_RAW_ONLY = "raw_stat_only"
SKIP_NO_TEXT = "no_mod_text"

_VARIETY_RE = re.compile(r"^\?s=Data\\MonsterVarieties/(\d+)$")
_HOVER_HASH_RE = re.compile(r"MonsterVarieties_hover/([0-9a-f]{8,})")
_SPACE_RE = re.compile(r"\s+")
_CJK_RE = re.compile(r"[一-鿿]")


class MonsterStructureError(ValueError):
    """页面结构与预期不符。定位失败即拒绝，不猜表。"""


@dataclass(frozen=True)
class MonsterMod:
    """一条怪物词缀。"""

    kind: str
    monster_level: int | None
    name_zh: str
    text_zh: str
    raw_stats: tuple[str, ...] = ()

    @property
    def mod_id(self) -> str:
        seed = json.dumps([self.kind, self.name_zh, self.text_zh],
                          ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [self.kind, self.monster_level, self.name_zh, self.text_zh, self.raw_stats],
            ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Spectre:
    """一个可作幽魂的怪物。倍率是页面原值（100 = 基础）。"""

    variety_key: str
    name_zh: str
    poedb_slug: str
    damage_pct: int
    life_pct: int

    @property
    def poedb_url(self) -> str:
        return f"https://poedb.tw/cn/{self.poedb_slug}"

    @property
    def content_hash(self) -> str:
        payload = json.dumps(
            [self.variety_key, self.name_zh, self.poedb_slug, self.damage_pct, self.life_pct],
            ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class MonsterExtract:
    """抽取结果与跳过项。跳过项由调用方登记，不静默丢弃。"""

    mods: list[MonsterMod] = field(default_factory=list)
    spectres: list[Spectre] = field(default_factory=list)
    #: (原因, 名字) —— 按原因聚合登记
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: 同名多条的幽魂名字（这些名字不进别名表，见模块说明）
    duplicate_names: tuple[str, ...] = ()


def _text(node) -> str:  # noqa: ANN001
    return _SPACE_RE.sub(" ", node.text(deep=True)).strip() if node is not None else ""


_BR_RE = re.compile(r"<br\s*/?>", re.I)


def _text_with_breaks(node) -> str:  # noqa: ANN001
    """把 `<br>` 规范成 ` / ` 再取文本。

    直接 `text(deep=True)` 会把换行吞掉，多条属性粘成一句：实测
    「全局物理伤害提高 60%命中值提高 100%敌人晕眩门槛降低 50%」——数字与下一条属性
    的名字连在一起，既没法读，也会让出口的数值回填校验对不上原文。
    ` / ` 是本仓既有约定（装备词缀那边 1b.4 就这么规范 POEDB 的 `<br>`）。
    """
    if node is None:
        return ""
    fragment = HTMLParser(_BR_RE.sub(" / ", node.html or ""))
    text = _SPACE_RE.sub(" ", fragment.text(deep=True) or "").strip()
    # 合并规范化产生的空段（`<br><br>` 或首尾 br）
    parts = [p.strip() for p in text.split("/")]
    return " / ".join(p for p in parts if p)


def _find_table(tree: HTMLParser, headers: tuple[str, ...]):  # noqa: ANN202
    """按表头定位表格。**不按下标**：页面加一张表就会让下标全错，
    而那种错会静默地把另一张表的数据当成本表入库。"""
    for table in tree.css("table"):
        got = tuple(_text(h) for h in table.css("th"))[: len(headers)]
        if got == headers:
            return table
    raise MonsterStructureError(f"页面上找不到表头为 {headers} 的表格")


def _rows(table) -> list:  # noqa: ANN001
    return table.css("tbody tr") or table.css("tr")


def _int_or_none(text: str) -> int | None:
    text = text.strip().replace("+", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_monster_mods(html: str) -> tuple[list[MonsterMod], list[tuple[str, str]]]:
    """解析怪物词缀表。返回 ``(入库条目, [(跳过原因, 名字)])``。"""
    table = _find_table(HTMLParser(html), MOD_TABLE_HEADERS)
    mods: list[MonsterMod] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in _rows(table):
        cells = row.css("td")
        if len(cells) < 4:
            continue
        kind = _text(cells[0])
        level = _int_or_none(_text(cells[1]))
        name_zh = _text(cells[2])
        if not kind or not name_zh:
            continue

        span = cells[3].css_first("span.explicitMod")
        if span is None:
            skipped.append((SKIP_NO_TEXT, name_zh))
            continue
        raw_stats = tuple(_text(s) for s in span.css("span.secondary") if _text(s))
        for node in span.css("span.secondary"):
            node.decompose()
        text_zh = _text_with_breaks(span)
        if not text_zh:
            # 只有未翻译的内部 stat id。它是调试信息，不能当中文答案输出
            skipped.append((SKIP_RAW_ONLY if raw_stats else SKIP_NO_TEXT, name_zh))
            continue

        mod = MonsterMod(kind=kind, monster_level=level, name_zh=name_zh,
                         text_zh=text_zh, raw_stats=raw_stats)
        if mod.mod_id in seen:
            # 同 (类型, 名字, 文本) 完全重复的行：页面里确实有，合并不丢信息
            continue
        seen.add(mod.mod_id)
        mods.append(mod)
    if not mods:
        raise MonsterStructureError("怪物词缀表里没有任何带中文文本的行，判为结构变更")
    return mods, skipped


def _variety_key(hover: str, damage: int | None, life: int | None) -> str:
    """幽魂行的身份键。两种形态，前缀区分，**不要合并**。

    ``v:<id>``            `data-hover` 给了 `Data\\MonsterVarieties/<id>`（2026-08-19
                          实测 1921 行）。这是 GGG 侧的种类 id，最可靠。
    ``h:<hash>:<伤害>:<生命>``  只给了 tooltip 缓存 URL。那个哈希是 **tooltip 内容**的
                          缓存键，不是种类 id：2026-08-13 的缓存页上 407 行走这条路，
                          其中 **7 例撞哈希**——每例都是同名同 slug 但两套倍率的不同
                          种类（「无名猛兽」276/360 与 374/585）。只用哈希会把它们合并
                          成一条，所以把倍率并进键。
                          （2026-08-19 重抓后这条路只剩 27 行、0 例撞哈希。**留着**：
                          占比是页面渲染方式决定的，会回来。）
    """
    matched = _VARIETY_RE.match(hover)
    if matched:
        return f"v:{matched.group(1)}"
    hashed = _HOVER_HASH_RE.search(hover)
    if not hashed or damage is None or life is None:
        return ""
    return f"h:{hashed.group(1)}:{damage}:{life}"


def parse_spectres(html: str) -> tuple[list[Spectre], tuple[str, ...]]:
    """解析幽魂表。返回 ``(条目, 同名多条的名字)``。

    第四列（「幽魂」）**不读**：实测 1948 行全是勾，这张表本身就是幽魂清单。
    存一个恒为真的布尔值会让人以为库里还有「不可作幽魂」的怪物。
    """
    table = _find_table(HTMLParser(html), SPECTRE_TABLE_HEADERS)
    out: list[Spectre] = []
    seen: dict[str, Spectre] = {}
    name_count: dict[str, int] = {}
    for row in _rows(table):
        cells = row.css("td")
        if len(cells) < 3:
            continue
        link = cells[0].css_first("a[href]")
        if link is None:
            continue
        name_zh = _text(link)
        href = str(link.attributes.get("href") or "")
        slug = href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
        damage = _int_or_none(_text(cells[1]))
        life = _int_or_none(_text(cells[2]))
        key = _variety_key(
            unquote(str(link.attributes.get("data-hover") or "")), damage, life
        )
        if not name_zh or not slug or not key or damage is None or life is None:
            continue

        entry = Spectre(variety_key=key, name_zh=name_zh, poedb_slug=slug,
                        damage_pct=damage, life_pct=life)
        if key in seen:
            # 同键但内容不同 = 键选错了，必须暴露而不是挑一条。
            # （`v:` 形态实测无此情况；`h:` 形态已把倍率并进键，见 `_variety_key`）
            if seen[key].content_hash != entry.content_hash:
                raise MonsterStructureError(
                    f"幽魂键 {key!r} 对应两套不同数值（{seen[key].name_zh} / {name_zh}），"
                    "键不再唯一，拒绝入库"
                )
            continue
        seen[key] = entry
        out.append(entry)
        name_count[name_zh] = name_count.get(name_zh, 0) + 1

    if not out:
        raise MonsterStructureError("幽魂表里没有可识别的行，判为结构变更")
    duplicates = tuple(sorted(n for n, c in name_count.items() if c > 1))
    return out, duplicates


def extract(html: str) -> MonsterExtract:
    """抽取整页需要的两张表。"""
    mods, skipped = parse_monster_mods(html)
    spectres, duplicates = parse_spectres(html)
    if not _CJK_RE.search(" ".join(m.text_zh for m in mods[:20])):
        raise MonsterStructureError("怪物词缀文本里没有中文，判为抓到了英文页")
    return MonsterExtract(mods=mods, spectres=spectres, skipped=skipped,
                          duplicate_names=duplicates)


def source_priority(priority_of) -> int:  # noqa: ANN001
    """POEDB 是 P1。单独一个函数是为了让调用方不必自己拼来源名。"""
    return int(priority_of(Source.POEDB.value))
