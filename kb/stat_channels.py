"""属性来源渠道注册表：「某属性能从哪里获得」的**唯一**枚举入口。

## 为什么要有注册表

场景「药剂效果提高这条属性我能从哪些渠道拿到（哪件装备、哪个珠宝、哪个天赋点、
哪个技能）」是个**枚举**问题，而枚举类问题有个特殊性质：**不完整的枚举被当成完整
的呈现出来，本身就是错误答案**。少说一个渠道，用户就会按残缺的清单去做决策。

初版的 `mod_sources` 工具硬编码扫两处（`mod_affix` 与 `entity` 里的唯一装备）。
接天赋树时才发现这个形状撑不住：

  - 每加一个渠道都要改查询工具，于是「加数据」变成了「改查询层」；
  - 工具描述里那句「尚未采集的渠道（天赋树、星团珠宝天赋、技能石、升华等）」是
    **手写**的，天赋树入库当天它就成了假话。这正是覆盖层要消灭的漂移，
    不该在工具层重新长出来。

所以把「有哪些渠道」提成一份注册表：新增渠道 = 注册一个 `ChannelSpec`，
查询工具与覆盖声明都不用动。

## 为什么不做成一张扁平的 stat_provider 表

考虑过把所有渠道的属性行拍平进一张表（channel, provider, stat_text, …），
放弃了：各渠道**有用的回答形态本就不同**。装备词缀要按 (底材类别, 来源, 前后缀,
词缀组) 聚合并报最低物品等级与有无权重；天赋节点要报节点种类、所属升华、树上几处；
唯一装备要报底材。硬拍平的结果是要么丢掉这些维度，要么让 `extra` 变成一个每个
渲染器都得特判的杂物袋——那是**假的统一**。

真正需要统一的只有两件事：**渠道清单可枚举**、**未采集渠道声明可派生**。
注册表两件都给了，代价是每个渠道保留自己的 SQL 与渲染。

## 未采集声明由 (inventory × status) 派生

`kb.coverage.DomainSpec.provides_stats` 声明「这个领域是属性的独立来源」，
运行时探测给出它有没有数据。两者的差集就是「枚举必然不完整的那部分」。
一行手写都没有，所以它不会变成假话。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: 每个渠道最多列几行。超出时**必须写出还剩多少**——枚举类答案里，
#: 静默截断和漏掉渠道是同一种错误。
MAX_ROWS = 12


@dataclass
class ChannelHit:
    """一个渠道的命中结果。`text` 是渲染好的 Markdown 表格。

    为什么是渲染后的表格而不是结构化数据：出口闸门（`orchestrator/binding.py`）
    校验「事实句里的数值能否在其绑定的证据原文里找到」，做的是子串与区间匹配。
    JSON 的键名与转义会干扰匹配，而且模型转述表格比转述 JSON 准确。
    """

    key: str
    label_zh: str
    #: 命中总数（**截断前**）。用于声明「还有多少没列出」。
    total: int
    title: str
    url: str
    text: str
    #: 该渠道特有的读法提醒（如 Atlas 天赋作用于地图产出而非角色属性）
    caveat: str = ""


@dataclass(frozen=True)
class ChannelSpec:
    key: str
    label_zh: str
    #: 对应 `kb.coverage.DOMAINS` 里的 key。覆盖状态由它派生，不在这里另写一份。
    domain_key: str
    scan: Callable[[sqlite3.Connection, str], ChannelHit | None]
    caveat: str = ""


_CHANNELS: list[ChannelSpec] = []


def register(spec: ChannelSpec) -> ChannelSpec:
    _CHANNELS.append(spec)
    return spec


def channels() -> list[ChannelSpec]:
    return list(_CHANNELS)


# ── 渲染小工具 ────────────────────────────────────────────────────

def _table(header: list[str], rows: list[list[str]], total: int, more_hint: str) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for r in rows[:MAX_ROWS]:
        out.append("| " + " | ".join(r) + " |")
    if total > MAX_ROWS:
        out.append("")
        out.append(f"（本渠道共 {total} 条，以上列出前 {MAX_ROWS} 条。{more_hint}）")
    return out


# ── 渠道 1：装备词缀 ──────────────────────────────────────────────

def _scan_equipment_mods(conn: sqlite3.Connection, q: str) -> ChannelHit | None:
    rows = conn.execute(
        "SELECT item_class_zh, category_label, gen_label, correct_group, "
        "       count(*) n, min(ilvl) min_ilvl, max(weight) max_weight, "
        "       min(text_zh) sample_text, min(url) url "
        "FROM mod_affix WHERE text_zh LIKE ? "
        "GROUP BY item_class_zh, category_label, gen_label, correct_group "
        "ORDER BY count(*) DESC",
        (f"%{q}%",),
    ).fetchall()
    if not rows:
        return None

    lines = [f"### 装备词缀：含「{q}」的词缀出现在这些 (底材类别 × 来源) 组合上", ""]
    lines += _table(
        ["底材类别", "来源", "前后缀", "词缀组", "条数", "最低物品等级", "有生成权重"],
        [
            [
                r["item_class_zh"] or "?", r["category_label"] or "?",
                r["gen_label"] or "其他", r["correct_group"] or "未分组",
                str(r["n"]), str(r["min_ilvl"]),
                "是" if (r["max_weight"] or 0) else "**否**",
            ]
            for r in rows
        ],
        len(rows),
        "用 mod_tiers 查某个具体类别的完整 T 级梯度",
    )
    lines += ["", f"文本样例：{rows[0]['sample_text']}"]
    no_weight = sum(1 for r in rows if not (r["max_weight"] or 0))
    if no_weight:
        lines.append(
            f"> 其中 {no_weight}/{len(rows)} 个组合**没有生成权重数据**，"
            "这些组合算不出出现概率或制作成功率。"
        )
    return ChannelHit(
        key="equipment_mod",
        label_zh="装备词缀",
        total=len(rows),
        title=f"「{q}」的装备词缀渠道（{len(rows)} 个组合）",
        url=rows[0]["url"] or "https://poedb.tw/cn/Modifiers",
        text="\n".join(lines),
    )


# ── 渠道 2..N：实体上的属性（唯一装备 / 天赋 / 升华 / Atlas）────────

def _entity_mod_rows(
    conn: sqlite3.Connection, entity_type: str, q: str
) -> list[tuple[sqlite3.Row, dict, list[dict]]]:
    """扫某类实体、取出真正含 `q` 的属性行。

    先用 SQL 的 LIKE 粗筛整个 attributes JSON（把大部分行挡在 Python 之外），
    再在 Python 里精确判断命中的是**属性文本**而不是别的字段——
    只靠 LIKE 会把名字、标签、机制说明里的偶然出现也算成命中。
    """
    rows = conn.execute(
        "SELECT entity_id, name_zh, name_en, base_type, url, attributes "
        "FROM entity WHERE entity_type=? AND attributes LIKE ?",
        (entity_type, f"%{q}%"),
    ).fetchall()
    out: list[tuple[sqlite3.Row, dict, list[dict]]] = []
    for r in rows:
        try:
            attrs = json.loads(r["attributes"] or "{}")
        except json.JSONDecodeError:
            logger.warning("attributes 不是合法 JSON：%s", r["entity_id"])
            continue
        hits = [
            m for m in attrs.get("mods", [])
            # 说明类词条（reminderText / 括号注解）不是属性，命中它不算这个实体提供该属性
            if q in (m.get("text") or "") and m.get("kind") != "description"
        ]
        if hits:
            out.append((r, attrs, hits))
    return out


def _scan_unique_items(conn: sqlite3.Connection, q: str) -> ChannelHit | None:
    found = _entity_mod_rows(conn, "unique_item", q)
    if not found:
        return None
    lines = [f"### 唯一装备：{len(found)} 件的词缀里含「{q}」", ""]
    lines += _table(
        ["装备", "底材", "词缀原文"],
        [
            [
                r["name_zh"] + (f"（{r['name_en']}）" if r["name_en"] else ""),
                r["base_type"] or "—",
                " / ".join(h["text"] for h in hits),
            ]
            for r, _a, hits in found
        ],
        len(found),
        "用 entity_lookup 查某件装备的完整词缀",
    )
    return ChannelHit(
        key="unique_item",
        label_zh="唯一装备",
        total=len(found),
        title=f"「{q}」的唯一装备渠道（{len(found)} 件）",
        url=found[0][0]["url"] or "https://poedb.tw/cn/Unique_item",
        text="\n".join(lines),
    )


def _tree_scanner(
    entity_type: str, key: str, label_zh: str, fallback_url: str, caveat: str = ""
) -> Callable[[sqlite3.Connection, str], ChannelHit | None]:
    """天赋类渠道的扫描器工厂（天赋树 / 升华 / Atlas 三者形状相同）。"""

    def scan(conn: sqlite3.Connection, q: str) -> ChannelHit | None:
        found = _entity_mod_rows(conn, entity_type, q)
        if not found:
            return None

        rows: list[list[str]] = []
        mastery_n = 0
        for r, attrs, hits in found:
            is_mastery = any(h.get("kind") == "mastery" for h in hits)
            mastery_n += is_mastery
            where = attrs.get("ascendancy_zh") or ""
            if where and attrs.get("class_zh"):
                where = f"{attrs['class_zh']} · {where}"
            positions = int(attrs.get("positions") or 1)
            rows.append([
                r["name_zh"] + (f"（{r['name_en']}）" if r["name_en"] else ""),
                str(attrs.get("node_kind_zh") or "天赋")
                + ("（效果**选一**）" if is_mastery else ""),
                where or "—",
                str(positions),
                " / ".join(h["text"] for h in hits),
            ])

        lines = [f"### {label_zh}：{len(found)} 个节点的属性里含「{q}」", ""]
        lines += _table(
            ["节点", "种类", "所属升华", "树上处数", "属性原文"],
            rows, len(found),
            "用 kb_search 查某个具体节点的完整属性",
        )
        if mastery_n:
            lines += [
                "",
                f"> 其中 {mastery_n} 个是**大师天赋**：一个大师天赋节点列出多个效果，"
                "点它时只能选定其中一个，不是同时获得全部。",
            ]
        lines += [
            "",
            "> 天赋节点的**天赋点消耗与连接关系未入库**，因此无法据此回答"
            "「点到它要花几点」「怎么走过去」。",
        ]
        return ChannelHit(
            key=key, label_zh=label_zh, total=len(found),
            title=f"「{q}」的{label_zh}渠道（{len(found)} 个节点）",
            url=found[0][0]["url"] or fallback_url,
            text="\n".join(lines),
            caveat=caveat,
        )

    return scan


def _scan_pantheon(conn: sqlite3.Connection, q: str) -> ChannelHit | None:
    """万神殿神力渠道。

    不套 `_tree_scanner`：那张表的列是「节点 / 种类 / 所属升华 / 树上处数」，
    对神力全是空的，而神力真正要说清的是**哪一层、抓获谁、在哪张图**——
    那正是「我要这条属性，得去做什么」的答案。
    """
    found = _entity_mod_rows(conn, "pantheon", q)
    if not found:
        return None

    rows: list[list[str]] = []
    for r, attrs, hits in found:
        texts = {h["text"] for h in hits}
        for tier in attrs.get("tiers") or []:
            matched = [t for t in (tier.get("mods_zh") or []) if t in texts]
            if not matched:
                continue
            if tier.get("tier") == 1:
                where = "基础神力"
                if tier.get("act_zh"):
                    where += f"（{tier['act_zh']}）"
            else:
                where = f"抓获 {tier.get('name_zh') or '—'}"
                if tier.get("map_zh"):
                    where += f"（{tier['map_zh']}）"
            rows.append([
                r["name_zh"] + (f"（{r['name_en']}）" if r["name_en"] else ""),
                "主要" if attrs.get("is_major") else "次要",
                f"第 {tier.get('tier')} 层",
                where,
                " / ".join(matched),
            ])

    lines = [f"### 万神殿：{len(found)} 位神祇的神力里含「{q}」", ""]
    lines += _table(
        ["神祇", "类别", "层", "怎么拿到", "词缀原文"],
        rows, len(rows),
        "用 kb_search 查某位神祇的完整各层神力",
    )
    lines += [
        "",
        "> 万神殿神力分**主要**与**次要**两类。这里列出的是全部命中的神力，"
        "不代表它们可以同时生效——启用规则本身未采集（见覆盖声明）。",
    ]
    return ChannelHit(
        key="pantheon", label_zh="万神殿神力", total=len(found),
        title=f"「{q}」的万神殿渠道（{len(found)} 位神祇）",
        url=found[0][0]["url"] or "https://poedb.tw/cn/Pantheon",
        text="\n".join(lines),
    )


# ── 注册。顺序即呈现顺序：玩家最常想到的渠道在前 ──────────────────

register(ChannelSpec(
    key="equipment_mod", label_zh="装备词缀", domain_key="mod_affix",
    scan=_scan_equipment_mods,
))
register(ChannelSpec(
    key="unique_item", label_zh="唯一装备", domain_key="unique_item",
    scan=_scan_unique_items,
))
register(ChannelSpec(
    key="passive", label_zh="天赋树节点", domain_key="passive_tree",
    scan=_tree_scanner("passive", "passive", "天赋树节点",
                       "https://poedb.tw/cn/passive-skill-tree/"),
))
register(ChannelSpec(
    key="ascendancy", label_zh="升华节点", domain_key="ascendancy",
    scan=_tree_scanner("ascendancy", "ascendancy", "升华节点",
                       "https://poedb.tw/cn/passive-skill-tree/"),
))
register(ChannelSpec(
    key="atlas_passive", label_zh="Atlas 天赋", domain_key="atlas_passive",
    scan=_tree_scanner(
        "atlas_passive", "atlas_passive", "Atlas 天赋",
        "https://poedb.tw/cn/atlas-skill-tree/",
        caveat="Atlas 天赋作用于**地图产出**（掉落、怪物、机制），"
               "不是角色自身属性；不要与角色天赋树混为一谈",
    ),
))
register(ChannelSpec(
    key="pantheon", label_zh="万神殿神力", domain_key="pantheon",
    scan=_scan_pantheon,
))


# ── 对外：扫描 + 未采集声明 ───────────────────────────────────────

@dataclass(frozen=True)
class UncoveredChannel:
    """枚举覆盖不到的属性来源，**带成因**。

    两种成因的措辞不能混：说底材「未采集」是句假话（库里 1,022 条），
    而说它「已采集」又会让人以为枚举里有它。区别是实打实的：
    `base_item` 有数据但没有注册枚举渠道，所以它的隐式词缀既不在清单里，
    以前也不再被声明为缺口——那正是这个字段要修掉的静默漏洞。
    """

    display_zh: str
    authority_url: str
    #: not_collected 该领域一条都没入库 / not_enumerable 已入库但没有枚举渠道
    cause: str


@dataclass
class ScanResult:
    hits: list[ChannelHit] = field(default_factory=list)
    #: 已采集、但这次没有条目的属性文本完全包含查询串的渠道。
    #: **这不等于该渠道不能提供该属性**，见 `no_match_note()`。
    no_match: list[str] = field(default_factory=list)
    #: 枚举覆盖不到的属性来源渠道 → 枚举必然在这些维度上不完整
    uncovered: list[UncoveredChannel] = field(default_factory=list)
    #: 用词提示：库里实际存在的相近说法（见 `_wording_hint`）
    wording_hint: str = ""

    @property
    def total_hits(self) -> int:
        return sum(h.total for h in self.hits)

    def coverage_note(self) -> str:
        """未采集渠道声明。

        这不是可选的补充说明，而是答案的必要组成部分：枚举类问题里，
        把不完整的枚举当成完整的呈现出来就是给了一个错答案。
        """
        if not self.uncovered:
            return "已声明的全部属性来源渠道均已接入本枚举，本次枚举覆盖了所有已知渠道。"
        parts: list[str] = []
        missing = [c for c in self.uncovered if c.cause == "not_collected"]
        unwired = [c for c in self.uncovered if c.cause == "not_enumerable"]
        if missing:
            items = "；".join(f"{c.display_zh}（{c.authority_url}）" for c in missing)
            parts.append(f"**本知识库尚未采集**：{items}")
        if unwired:
            # 措辞必须与上一类分开：这些领域**有数据**，缺的是把它们的属性接进枚举。
            # 说成「未采集」就是在一个有 1,022 条底材的库上讲假话。
            items = "；".join(f"{c.display_zh}（{c.authority_url}）" for c in unwired)
            parts.append(
                f"**已采集但其属性尚未接入本枚举**（查这些渠道要单独用对应工具）：{items}"
            )
        return (
            "以下渠道也能提供属性，因此上面的清单在这些维度上必然不完整——"
            "回答时必须如实说明：" + "；".join(parts)
        )

    def no_match_note(self) -> str:
        """「这个渠道没命中」与「这个渠道给不了」是两件事。

        实测这条区别很要紧：查「法术压制」时装备词缀渠道零命中，但库里有 414 条
        含「压制」的词缀——游戏原文写的是「法术伤害压制率」，「法术压制」是社区简称。
        若把零命中读成「装备上没有这条属性」，就会给出一个方向完全相反的答案。
        """
        if not self.no_match:
            return ""
        return (
            f"以下渠道**已采集但本次零命中**：{'、'.join(self.no_match)}。"
            "零命中只说明没有条目的属性文本**完全包含**该查询串，"
            "**不等于**这些渠道不能提供该属性——常见原因是用词与游戏原文不一致。"
        )


#: 用词提示里，子词至少要占查询串这么大比重才算「同一个说法的一部分」。
#:
#: 没有这道闸的后果实测过：查「这个属性纯属虚构」时会切出「属性」（库里 849 条），
#: 于是提示「实际写法例如：所有属性提高 5%」——把一个纯属编造的查询导向了一堆
#: 无关词缀。虚构查询的正确回答是「没有」，不是给个不相干的建议。
MIN_HINT_RATIO = 0.4


def _wording_hint(conn: sqlite3.Connection, q: str) -> str:
    """查询串在词缀库里**完全没有字面命中**时，给出库里实际存在的相近说法。

    做法是把查询串按词切开，找一个确实存在的子词，并**原样**举出几条真实文本。
    不猜同义词、不改写——只是把库里的原话拿出来，让下一次查询能用对词。

    两道闸，都是实测出来的：

    1. **只在全查询串零命中时才给提示。** 否则会产出假陈述：查「药剂效果」时
       装备词缀渠道明明命中了 17 个组合（因为别的渠道零命中而触发了提示），
       提示却说「词缀库里没有『药剂效果』的字面说法」。
    2. **子词取命中数最少的那个，不是最多的。** 命中最多等于最泛：
       「法术压制」会切出「法术」(5012 条) 与「压制」(414 条)，
       挑最多的就挑走了「法术」，而有区分度的是「压制」——
       游戏原文写的是「法术伤害压制率」。
    """
    from retrieval.tokenize import segment  # noqa: PLC0415

    def count(term: str) -> int:
        return conn.execute(
            "SELECT count(*) FROM mod_affix WHERE text_zh LIKE ?", (f"%{term}%",)
        ).fetchone()[0]

    # 闸 1：全串已有字面命中，就没有「用词不对」这回事
    if count(q):
        return ""

    subs = [
        w for w in dict.fromkeys(segment(q))
        if len(w) >= 2 and w != q and len(w) / len(q) >= MIN_HINT_RATIO
    ]
    # 闸 2：取最具区分度（命中最少）的那个非空子词
    scored = [(count(w), w) for w in subs]
    scored = [(n, w) for n, w in scored if n]
    if not scored:
        return ""
    n_hit, word = min(scored)

    samples = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT text_zh FROM mod_affix WHERE text_zh LIKE ? "
            "ORDER BY length(text_zh) LIMIT 3",
            (f"%{word}%",),
        )
    ]
    return (
        f"用词提示：词缀库里没有「{q}」的字面说法，但有 {n_hit} 条含「{word}」，"
        f"实际写法例如：{'；'.join(samples)}。"
        "「查询串对不上」与「游戏里没有」是两回事——请改用游戏原文的说法重查，"
        "不要据此断言该属性不存在。"
    )


def scan_all(conn: sqlite3.Connection, query: str) -> ScanResult:
    """遍历全部注册渠道。"""
    from kb import coverage as cov  # noqa: PLC0415  避免导入环

    q = (query or "").strip()
    res = ScanResult()
    if not q:
        return res

    for spec in channels():
        try:
            hit = spec.scan(conn, q)
        except sqlite3.Error as exc:
            # 单个渠道查询失败不该让整轮枚举失败，但**必须**报出来，
            # 否则这次枚举会静默地少一个维度
            logger.warning("渠道 %s 扫描失败：%s", spec.key, exc)
            res.no_match.append(f"{spec.label_zh}（查询失败）")
            continue
        if hit is None:
            res.no_match.append(spec.label_zh)
        else:
            hit.caveat = hit.caveat or spec.caveat
            res.hits.append(hit)

    if res.no_match:
        res.wording_hint = _wording_hint(conn, q)

    by_key = {s.key: s for s in cov.load(conn)}
    # 判据是「**这个领域的属性能不能被本枚举列出来**」，而不是「它有没有数据」。
    # 两者不等价，实测就有反例：`base_item` 已入库 1,022 条、`provides_stats=True`、
    # 却没有注册枚举渠道，于是底材隐式词缀既不出现在清单里、也不再被声明为缺口——
    # 一份看起来完整的枚举。所以「有数据」还要再加一个条件：有枚举 SQL。
    #
    # 这个条件是**派生**的（渠道注册表里有没有它），不是手写的开关，
    # 所以接入新渠道的当天声明就自动变化，不会变成假话。
    enumerable = {spec.domain_key for spec in _CHANNELS}
    for spec_dom in cov.DOMAINS:
        if not spec_dom.provides_stats:
            continue
        st = by_key.get(spec_dom.key)
        usable = st is not None and st.usable
        if usable and spec_dom.key in enumerable:
            continue
        res.uncovered.append(
            UncoveredChannel(
                display_zh=spec_dom.display_zh,
                authority_url=spec_dom.authority_url,
                cause="not_enumerable" if usable else "not_collected",
            )
        )
    return res
