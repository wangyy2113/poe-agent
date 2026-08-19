"""POEDB 天赋树数据源（天赋树 + Atlas 天赋树）。

## 为什么是这个来源

`/cn/passive-skill-tree` 那张页面只有 7 KB，是个纯 JS 渲染的 SVG 空壳，一个中文字
都没有——按抓页面的老路子走会得出「POEDB 没有中文天赋树」的错误结论，`tasks.md`
1b.7 里记的「天赋树须先补 POEDB 中文页」这条阻塞就是这么来的。

实际数据在 JS 里请求的一个 JSON 端点上：`/data/{target}/{version}/data_{lang}.json`。
中文版 6.3 MB、3,390 个节点，`name` 与 `stats` 全是中文，是**一等的 P1 中文来源**，
英文版（`data_us.json`）同样可得，所以中英文名都来自 POEDB，PoB 只做 P2 交叉校验。

## 版本号必须发现，不能写死

端点里的 `{version}` 来自 JS bundle 里的 `poe1version`，而 bundle 的文件名带内容
哈希（`passive-skill-tree.<hash>.js`），两者都会随 POEDB 更新而变。所以走三步发现链：

    页面 → bundle URL（正则取 script src）→ poe1version → 拼 JSON URL

发现失败就**显式报错**。写死 3.29 的后果是新赛季上线后我们会安静地继续回答上个
版本的天赋树，而这类错误在输出里看不出来。

## 三个数据形态上的坑

1. **大师天赋（Mastery）的效果不在 `stats` 里。** 353 个 mastery 节点的 `stats`
   是空数组，效果在 `masteryEffects[].stats`。只读 `stats` 会安静地丢掉全部大师
   天赋——而大师天赋是很大一块属性来源。
2. **`stats` 里的 `\n` 语义不唯一。** 71 条含换行，PoB 的 `tree.lua` 在同一位置
   也有同样的 `\n`，说明来自 GGG 源数据。但它有时是两条独立属性
   （`回复能量护盾不能超过护甲\n每秒再生能量护盾…`），有时只是一句话的折行
   （`每因此失去 10 点怒火，\n获得 1 秒狂野兽性`）。**无法可靠区分，因此不猜**：
   原文照存，渲染时用续行缩进，不套用 ` / ` 分隔符（那会把折行处读成两条属性）。
3. **同名节点的内容可能不同。** 实测 117 组同 (名字, 种类) 但内容有差异，
   「元素伤害」这一个名字下有 20 个节点、4 种数值（12% / 10% / 6% / 另一条完全
   不同的）。所以实体粒度取 **(名字, 种类, 内容)**，不能按名字合并——合并后
   6% 和 12% 只剩一个，那是凭空造数值。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field

from entities.models import Entity, ModLine, SourceRef
from entities.taxonomy import EntityType, ModKind, Source, make_entity_id
from kb.fetcher import Fetcher

logger = logging.getLogger(__name__)

BASE = "https://poedb.tw"

#: 两棵树。key 即端点里的 `{target}`。
TARGET_PASSIVE = "passive-skill-tree"
TARGET_ATLAS = "atlas-skill-tree"

#: `{lang}`。POEDB 用 `document.documentElement.lang`，中文站是 `cn`、英文站是 `us`。
LANG_ZH = "cn"
LANG_EN = "us"

#: 每棵树对应的**默认**实体类型。Atlas 树单独一类——它作用于地图产出而非角色属性，
#: 混进 `passive` 会让「哪些天赋给某属性」把地图掉落词条也算进来。
#:
#: 升华节点是例外：它们在天赋树的 JSON 里，但类型归 `ascendancy`（见
#: `entity_type_of`）。理由是覆盖层与渠道枚举都按实体类型分域——若把升华塞进
#: `passive`，覆盖层会因为 `entity_type='ascendancy'` 查不到行而报「升华未采集，
#: 去 POEDB 查」，而我们手里明明有 406 个升华节点。对着已有数据说未采集，
#: 是假拒答，比漏答更糟。
TARGET_ENTITY_TYPE = {
    TARGET_PASSIVE: EntityType.PASSIVE,
    TARGET_ATLAS: EntityType.ATLAS_PASSIVE,
}

#: 升华类节点的种类。这些节点从天赋树 JSON 来，但独立成域。
ASCENDANCY_KINDS = frozenset({"ascendancy", "ascendancy_notable"})

#: 结构断言下限。低于此值判为页面/端点结构变更，拒绝写入而不是写进一小半。
MIN_NODES = {TARGET_PASSIVE: 2500, TARGET_ATLAS: 600}
#: 带内容节点占比下限。JSON 结构变了（比如 stats 改名）时这个比例会塌。
MIN_CONTENT_RATIO = 0.5

#: 节点种类。顺序即判定优先级。
KIND_MASTERY = "mastery"
KIND_KEYSTONE = "keystone"
KIND_ASC_NOTABLE = "ascendancy_notable"
KIND_ASCENDANCY = "ascendancy"
KIND_NOTABLE = "notable"
KIND_NORMAL = "normal"

KIND_ZH = {
    KIND_MASTERY: "大师天赋",
    KIND_KEYSTONE: "基石",
    KIND_ASC_NOTABLE: "升华核心",
    KIND_ASCENDANCY: "升华节点",
    KIND_NOTABLE: "核心天赋",
    KIND_NORMAL: "普通天赋",
}

#: 有**专名**的种类。只有这些进别名表。
#:
#: 为什么要区分：普通天赋的名字就是它给的属性本身（「火焰伤害」「生命」
#: 「攻击速度」），不是专名。把它们灌进别名表会让「弓上的火焰伤害前缀有哪些等级」
#: 这类装备词缀问题被强行注入天赋卡——实测现有 12 条检索用例里有 3 条会被这样污染。
#: 属性描述类查询本就该走 FTS 与 `mod_sources`，别名表是给专名用的。
NAMED_KINDS = frozenset({KIND_MASTERY, KIND_KEYSTONE, KIND_ASC_NOTABLE,
                         KIND_ASCENDANCY, KIND_NOTABLE})

#: 别名最短长度（归一化后）。2 字名（「出血」「凝聚」「力量」）太容易在无关问题里
#: 被子串命中——别名表的子串匹配只要求长度 ≥2，这里再收一档。
MIN_ALIAS_LEN = 3


class TreeStructureError(RuntimeError):
    """端点发现或结构断言失败。抛出即整批拒绝写入。"""


# ── 端点发现 ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class TreeEndpoint:
    """一棵树的一种语言版本的数据端点。"""

    target: str
    version: str
    lang: str

    @property
    def json_url(self) -> str:
        return f"{BASE}/data/{self.target}/{self.version}/data_{self.lang}.json"

    @property
    def page_url(self) -> str:
        """给用户看的页面 URL，作为引用地址。

        逐节点 URL 不存在（整棵树是一个 SVG 应用），因此引用到页面级——与
        `mod_affix` 引用底材类别页而非逐词缀页是同一口径。节点名在标题里，
        用户可用页面自带的搜索框定位。
        """
        return f"{BASE}/{self.lang}/{self.target}/"

    @property
    def entity_type(self) -> EntityType:
        """这棵树的默认实体类型（升华节点另算，见 `entity_type_of`）。"""
        return TARGET_ENTITY_TYPE[self.target]

    @property
    def entity_types(self) -> tuple[EntityType, ...]:
        """这棵树会产出的**全部**实体类型。

        重建时要按这些类型逐个清前缀——漏掉一个就会留下孤儿卡片继续被检索命中。
        """
        if self.target == TARGET_PASSIVE:
            return (EntityType.PASSIVE, EntityType.ASCENDANCY)
        return (self.entity_type,)


def entity_type_of(node_kind: str, ep: TreeEndpoint) -> EntityType:
    """节点 → 实体类型。天赋树里的升华节点归 `ascendancy` 域。"""
    if ep.target == TARGET_PASSIVE and node_kind in ASCENDANCY_KINDS:
        return EntityType.ASCENDANCY
    return ep.entity_type


_JS_SRC_RE = re.compile(
    r'<script[^>]+src="(https://cdn\.poedb\.tw/js/(?:passive|atlas)[^"]*\.js)"'
)
_VERSION_RE = re.compile(r'poe1version\s*:\s*"(\d+\.\d+)"')


def discover_version(fetcher: Fetcher, target: str, *, force: bool = False) -> str:
    """三步发现链取树版本号。

    任何一步取不到都抛 `TreeStructureError`——不回退到写死的版本号。
    """
    page_url = f"{BASE}/{LANG_ZH}/{target}/"
    res = fetcher.fetch(page_url, force=force)
    if not res.ok:
        raise TreeStructureError(f"取树页面失败 HTTP {res.status_code}：{page_url}")

    m = _JS_SRC_RE.search(res.text)
    if not m:
        raise TreeStructureError(
            f"{page_url} 里没找到树的 JS bundle（script src），POEDB 前端结构可能已变更；"
            "拒绝继续以免用过期版本号请求数据"
        )
    js_url = m.group(1)

    js = fetcher.fetch(js_url, force=force)
    if not js.ok:
        raise TreeStructureError(f"取 JS bundle 失败 HTTP {js.status_code}：{js_url}")
    mv = _VERSION_RE.search(js.text)
    if not mv:
        raise TreeStructureError(
            f"{js_url} 里没找到 poe1version，无法确定树版本；拒绝猜测版本号"
        )
    version = mv.group(1)
    logger.info("发现 %s 版本 %s（bundle %s）", target, version, js_url.rsplit("/", 1)[-1])
    return version


def fetch_payload(fetcher: Fetcher, ep: TreeEndpoint, *, force: bool = False) -> dict:
    res = fetcher.fetch(ep.json_url, force=force)
    if not res.ok:
        raise TreeStructureError(f"取树数据失败 HTTP {res.status_code}：{ep.json_url}")
    try:
        payload = json.loads(res.text)
    except json.JSONDecodeError as exc:
        raise TreeStructureError(f"{ep.json_url} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload.get("nodes"), dict):
        raise TreeStructureError(f"{ep.json_url} 缺少 nodes 字典")
    return payload


# ── 抽取 ──────────────────────────────────────────────────────────

@dataclass
class MasteryEffect:
    effect_id: int
    stats: list[str]


@dataclass
class TreeNode:
    """一个天赋节点（已按内容合并同名同容的多个树上位置）。"""

    kind: str
    name_zh: str
    name_en: str | None
    stats: list[str]
    mastery_effects: list[MasteryEffect] = field(default_factory=list)
    reminder: list[str] = field(default_factory=list)
    #: 树上有多少个位置是这一个（名字, 种类, 内容）组合
    positions: int = 1
    #: 组内的 GGG skill id，升序。可用于与 PoB 交叉校验。
    skill_ids: list[int] = field(default_factory=list)
    #: 升华归属。`ascendancy_id` 是 JSON 里节点自带的英文 id，
    #: `ascendancy_zh` / `class_zh` 由顶层 classes 映射而来。
    ascendancy_id: str = ""
    ascendancy_zh: str = ""
    class_zh: str = ""
    #: JSON 上的 `is*` 旗标，原样保留（isBloodline / isMultipleChoice / …）
    flags: list[str] = field(default_factory=list)

    @property
    def content_key(self) -> tuple:
        return (
            self.kind,
            self.name_zh,
            tuple(self.stats),
            tuple((e.effect_id, tuple(e.stats)) for e in self.mastery_effects),
        )

    @property
    def digest(self) -> str:
        """内容指纹。

        entity_id 用内容哈希而不是 skill id：同名节点会按内容分成多个实体，
        而 skill id 在上游增删时会整体错位（1b.3 在 `ModMaster.lua` 上踩过同样的坑）。
        """
        payload = json.dumps(self.content_key, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]

    @property
    def slug(self) -> str:
        """英文 slug。别名表用它做英文匹配，取不到英文名时留空。

        别名是**匹配辅助**不是事实断言，所以允许由英文名派生。
        """
        if not self.name_en:
            return ""
        return re.sub(r"[^A-Za-z0-9]+", "_", self.name_en).strip("_")

    @property
    def has_content(self) -> bool:
        return bool(self.stats or self.mastery_effects)

    @property
    def is_named(self) -> bool:
        return self.kind in NAMED_KINDS


@dataclass
class TreeExtractStats:
    total_nodes: int = 0
    with_content: int = 0
    merged: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    #: 中文有、英文侧缺名字的节点数（英文名允许缺，但要可见）
    missing_en: int = 0
    #: 没有 stats 也没有 masteryEffects 的节点（珠宝插槽、纯连接点等），合法跳过
    skipped_empty: int = 0

    @property
    def content_ratio(self) -> float:
        return self.with_content / self.total_nodes if self.total_nodes else 0.0


def _kind_of(node: dict) -> str:
    if node.get("isMastery"):
        return KIND_MASTERY
    if node.get("isKeystone"):
        return KIND_KEYSTONE
    if node.get("ascendancyName"):
        return KIND_ASC_NOTABLE if node.get("isNotable") else KIND_ASCENDANCY
    if node.get("isNotable"):
        return KIND_NOTABLE
    return KIND_NORMAL


def _as_list(v) -> list:  # noqa: ANN001
    """POEDB 的数组有时是 list、有时是以 "1"/"2" 为键的 dict（Lua 表转 JSON 的后遗症）。"""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v[k] for k in sorted(v, key=lambda x: int(x) if str(x).isdigit() else 0)]
    return [v]


def _ascendancy_maps(payload: dict) -> tuple[dict[str, str], dict[str, str]]:
    """英文升华 id → (中文升华名, 中文职业名)。"""
    asc_zh: dict[str, str] = {}
    cls_zh: dict[str, str] = {}
    for cls in _as_list(payload.get("classes")):
        if not isinstance(cls, dict):
            continue
        cname = str(cls.get("name") or "")
        for asc in _as_list(cls.get("ascendancies")):
            if not isinstance(asc, dict):
                continue
            aid = str(asc.get("id") or "")
            if not aid:
                continue
            asc_zh[aid] = str(asc.get("name") or aid)
            cls_zh[aid] = cname
    return asc_zh, cls_zh


def extract(
    payload_zh: dict, payload_en: dict | None, ep: TreeEndpoint
) -> tuple[list[TreeNode], TreeExtractStats]:
    """把两种语言的 payload 合成节点列表。

    合并粒度 = (种类, 名字, 内容)。同一组合在树上出现多次记 `positions`，
    不是丢掉多余的——「树上有 20 处」本身是有用的事实。
    """
    nodes_zh = payload_zh["nodes"]
    nodes_en = (payload_en or {}).get("nodes") or {}
    asc_zh, cls_zh = _ascendancy_maps(payload_zh)

    st = TreeExtractStats()
    groups: dict[tuple, TreeNode] = {}

    for key, raw in nodes_zh.items():
        if not isinstance(raw, dict):
            continue
        st.total_nodes += 1

        stats = [str(s) for s in _as_list(raw.get("stats")) if str(s).strip()]
        effects = [
            MasteryEffect(
                effect_id=int(e.get("effect") or 0),
                stats=[str(s) for s in _as_list(e.get("stats")) if str(s).strip()],
            )
            for e in _as_list(raw.get("masteryEffects"))
            if isinstance(e, dict)
        ]
        effects = [e for e in effects if e.stats]
        if not stats and not effects:
            st.skipped_empty += 1
            continue
        st.with_content += 1

        name_zh = str(raw.get("name") or "").strip()
        if not name_zh:
            # 无名字的节点没法引用也没法检索，计入跳过而不是塞个占位名
            st.skipped_empty += 1
            st.with_content -= 1
            continue

        en_raw = nodes_en.get(key) if isinstance(nodes_en, dict) else None
        name_en = str((en_raw or {}).get("name") or "").strip() or None
        if name_en is None:
            st.missing_en += 1

        aid = str(raw.get("ascendancyName") or "")
        node = TreeNode(
            kind=_kind_of(raw),
            name_zh=name_zh,
            name_en=name_en,
            stats=stats,
            mastery_effects=effects,
            reminder=[str(s) for s in _as_list(raw.get("reminderText")) if str(s).strip()],
            skill_ids=[int(raw["skill"])] if str(raw.get("skill", "")).lstrip("-").isdigit() else [],
            ascendancy_id=aid,
            ascendancy_zh=asc_zh.get(aid, ""),
            class_zh=cls_zh.get(aid, ""),
            flags=sorted(k for k, v in raw.items() if k.startswith("is") and v is True),
        )

        prev = groups.get(node.content_key)
        if prev is None:
            groups[node.content_key] = node
        else:
            prev.positions += 1
            prev.skill_ids.extend(node.skill_ids)
            # 英文名以第一个非空为准；旗标取并集（同一天赋在不同位置旗标可能不同）
            prev.name_en = prev.name_en or node.name_en
            prev.flags = sorted(set(prev.flags) | set(node.flags))

    out = list(groups.values())
    for n in out:
        n.skill_ids.sort()
    st.merged = len(out)
    for n in out:
        st.by_kind[n.kind] = st.by_kind.get(n.kind, 0) + 1

    _assert_structure(st, ep)
    return out, st


def _assert_structure(st: TreeExtractStats, ep: TreeEndpoint) -> None:
    floor = MIN_NODES.get(ep.target, 100)
    if st.total_nodes < floor:
        raise TreeStructureError(
            f"{ep.target} 只解析到 {st.total_nodes} 个节点（下限 {floor}），"
            "判为端点或结构变更，拒绝写入"
        )
    if st.content_ratio < MIN_CONTENT_RATIO:
        raise TreeStructureError(
            f"{ep.target} 带内容节点仅 {st.content_ratio:.1%}（下限 "
            f"{MIN_CONTENT_RATIO:.0%}），stats 字段可能已改名，拒绝写入"
        )
    if not st.by_kind.get(KIND_KEYSTONE) and ep.target == TARGET_PASSIVE:
        raise TreeStructureError(
            "天赋树里没解析到任何基石（keystone），isKeystone 标志可能已变更，拒绝写入"
        )


# ── 实体 ──────────────────────────────────────────────────────────

def node_to_entity(node: TreeNode, ep: TreeEndpoint) -> Entity:
    """节点 → 实体。

    `stats` 进 `mods`（kind=passive），大师天赋的可选效果进 `mods`（kind=mastery）。
    两者用不同 ModKind 是刻意的：大师天赋是**选一**，任何遍历 mods 的代码都不该
    把六个互斥效果当成同时生效的六条属性。
    """
    mods: list[ModLine] = [ModLine(kind=ModKind.PASSIVE, text=s) for s in node.stats]
    for eff in node.mastery_effects:
        mods.extend(ModLine(kind=ModKind.MASTERY, text=s) for s in eff.stats)
    mods.extend(ModLine(kind=ModKind.DESCRIPTION, text=s) for s in node.reminder)

    slug = node.slug or f"node_{node.digest}"
    etype = entity_type_of(node.kind, ep)
    entity_id = make_entity_id(etype, f"{slug}_{node.digest}")

    keywords: list[str] = [node.name_zh, KIND_ZH.get(node.kind, node.kind)]
    if node.ascendancy_zh:
        keywords += [node.ascendancy_zh, node.class_zh]

    return Entity(
        entity_id=entity_id,
        entity_type=etype,
        name_zh=node.name_zh,
        name_en=node.name_en,
        slug=slug,
        item_class=KIND_ZH.get(node.kind, node.kind),
        mods=mods,
        search_keywords=[k for k in keywords if k],
        attributes={
            "tree": ep.target,
            "tree_version": ep.version,
            "node_kind": node.kind,
            "node_kind_zh": KIND_ZH.get(node.kind, node.kind),
            "positions": node.positions,
            "skill_ids": node.skill_ids,
            "ascendancy_id": node.ascendancy_id,
            "ascendancy_zh": node.ascendancy_zh,
            "class_zh": node.class_zh,
            "flags": node.flags,
            "mastery_effects": [
                {"effect": e.effect_id, "stats": e.stats} for e in node.mastery_effects
            ],
            # 机器可复现的数据出处。页面 URL 给人看，这个给复现用。
            "data_url": ep.json_url,
            "aliasable": node.is_named,
        },
        version=ep.version,
        ref=SourceRef(source=Source.POEDB.value, url=ep.page_url, priority=1),
    )


def aliasable_entities(entities: list[Entity]) -> list[Entity]:
    """筛出可以进别名表的实体。

    三条同时成立才收：
      1. 种类有专名（`NAMED_KINDS`）——普通天赋的名字是属性描述而非专名
      2. 名字在本批里唯一——「生命专精」这类同名多实体入表会一次注入多张卡
      3. 归一化后长度 ≥ `MIN_ALIAS_LEN`——2 字名太容易在无关问题里被子串命中
    """
    from retrieval.alias import normalize  # noqa: PLC0415  避免导入环

    named = [
        e for e in entities
        if e.attributes.get("aliasable")
        and len(normalize(e.name_zh)) >= MIN_ALIAS_LEN
    ]
    seen: dict[str, int] = {}
    for e in named:
        seen[normalize(e.name_zh)] = seen.get(normalize(e.name_zh), 0) + 1
    return [e for e in named if seen[normalize(e.name_zh)] == 1]

# ── 图结构：连接关系、职业起点、总点数 ──────────────────────────────

#: 珠宝插槽在图里**可经过**（游戏里它就是一个能点的节点），所以不单列 kind。
KIND_JEWEL = "jewel_socket"

#: 中心节点。7 个职业起点都连在它上面，但游戏里**不能从一个职业穿到另一个职业**。
#: 不排除它，「野蛮人 → 女巫」会算成 2 点——实测就是这么错的（真值 22）。
ROOT_ID = "root"


@dataclass(frozen=True)
class GraphNode:
    """图里的一个节点。`passable` 决定它能不能出现在路径中间。"""

    node_id: str
    name_zh: str
    name_en: str
    kind: str
    ascendancy_id: str
    class_start: str
    passable: bool


@dataclass
class TreeGraph:
    """一棵树的图。`edges` 已规范化为 node_a < node_b 的无向边。"""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: set[tuple[str, str]] = field(default_factory=set)
    #: totalPoints / ascendancyPoints
    meta: dict[str, float] = field(default_factory=dict)

    @property
    def passable_ids(self) -> set[str]:
        return {n.node_id for n in self.nodes if n.passable}

    @property
    def class_starts(self) -> dict[str, str]:
        """中文职业名 → 起点节点 id。"""
        return {n.class_start: n.node_id for n in self.nodes if n.class_start}


def _graph_kind(raw: dict) -> str:
    if raw.get("isJewelSocket"):
        return KIND_JEWEL
    return _kind_of(raw)


def _class_start_names(payload: dict) -> dict[int, str]:
    """classStartIndex → 中文职业名。索引是 `classes` 数组的下标。"""
    out: dict[int, str] = {}
    for i, cls in enumerate(_as_list(payload.get("classes"))):
        if isinstance(cls, dict):
            out[i] = str(cls.get("name") or "")
    return out


def extract_graph(
    payload_zh: dict, payload_en: dict | None, ep: TreeEndpoint
) -> TreeGraph:
    """抽取图结构。节点属性走 `extract()`，这里只管「怎么连的」。

    `passable` 的三条排除都是**游戏规则**，不是数据问题（判据见 `core/db.py` 的
    `passive_graph_node` 表注释）：root 不能穿、升华节点只能从升华起点进、
    大师天赋不作为通路。

    节点 id 直接用 JSON 的键——实测它与节点的 `skill` 字段 3389/3389 相等，
    也就是 GGG 的 skill id，所以能与已入库天赋卡的 `skill_ids` 对上。
    """
    nodes_zh = payload_zh.get("nodes")
    if not isinstance(nodes_zh, dict) or not nodes_zh:
        raise TreeStructureError(f"{ep.json_url} 缺少 nodes 字典")
    nodes_en = (payload_en or {}).get("nodes") or {}
    class_names = _class_start_names(payload_zh)

    graph = TreeGraph()
    for node_id, raw in nodes_zh.items():
        if not isinstance(raw, dict):
            continue
        start_index = raw.get("classStartIndex")
        class_start = (
            class_names.get(int(start_index), "") if isinstance(start_index, int) else ""
        )
        kind = _graph_kind(raw)
        ascendancy_id = str(raw.get("ascendancyName") or "")
        passable = (
            node_id != ROOT_ID
            and not ascendancy_id
            and kind != KIND_MASTERY
        )
        graph.nodes.append(
            GraphNode(
                node_id=str(node_id),
                name_zh=str(raw.get("name") or ""),
                name_en=str((nodes_en.get(node_id) or {}).get("name") or ""),
                kind=kind,
                ascendancy_id=ascendancy_id,
                class_start=class_start,
                passable=passable,
            )
        )

    known = {n.node_id for n in graph.nodes}
    for node_id, raw in nodes_zh.items():
        if not isinstance(raw, dict):
            continue
        for other in _as_list(raw.get("out")) + _as_list(raw.get("in")):
            other_id = str(other)
            if other_id not in known or other_id == str(node_id):
                continue
            a, b = sorted((str(node_id), other_id))
            graph.edges.add((a, b))

    points = payload_zh.get("points")
    if isinstance(points, dict):
        for key in ("totalPoints", "ascendancyPoints"):
            value = points.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                graph.meta[key] = float(value)

    if len(graph.class_starts) < 7:
        raise TreeStructureError(
            f"{ep.json_url} 只解析到 {len(graph.class_starts)} 个职业起点（应为 7），"
            "classStartIndex 可能已改名，拒绝写入"
        )
    if not graph.edges:
        raise TreeStructureError(f"{ep.json_url} 没有解析到任何连接关系（in/out 可能已改名）")
    return graph


def build_adjacency(
    graph_edges: set[tuple[str, str]], passable: set[str]
) -> dict[str, set[str]]:
    """邻接表。只保留两端都可经过的边。"""
    adj: dict[str, set[str]] = {}
    for a, b in graph_edges:
        if a in passable and b in passable:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
    return adj


def nearest_path(
    adj: dict[str, set[str]], src: str, targets: set[str]
) -> list[str] | None:
    """一次 BFS 找出离 `src` **最近的那个目标**，返回节点 id 序列（含起点与终点）。

    点数 = ``len(path) - 1``（起点是职业出生点，不消耗天赋点）。

    为什么是「一次 BFS 找最近的一个」而不是「对每个目标各跑一次」：同名小点在树上
    有几十处（「力量」73 处），逐个跑就是 7 个职业 × 73 次 = 511 次 BFS，实测那样
    单次查询要 327ms；BFS 本身就是按距离扩展的，第一个命中的目标必然最近。
    """
    if src not in adj and src not in targets:
        return None
    if src in targets:
        return [src]
    prev: dict[str, str] = {src: src}
    queue = deque([src])
    while queue:
        cur = queue.popleft()
        for nxt in adj.get(cur, ()):
            if nxt in prev:
                continue
            prev[nxt] = cur
            if nxt in targets:
                path = [nxt]
                while path[-1] != src:
                    path.append(prev[path[-1]])
                return list(reversed(path))
            queue.append(nxt)
    return None


def shortest_path(
    graph_edges: set[tuple[str, str]], passable: set[str], src: str, dst: str
) -> list[str] | None:
    """单目标最短路径。`nearest_path` 的一元特例，判据只写在那一处。

    **只走 `passable`**：终点自己可以是不可经过的类型吗？不能——升华节点与大师天赋
    本来就不是「从起点走过去」这个模型能描述的东西，调用方要先判掉并说明原因，
    而不是在这里返回一条穿过它们的假路径。
    """
    if src not in passable or dst not in passable:
        return None
    return nearest_path(build_adjacency(graph_edges, passable), src, {dst})
