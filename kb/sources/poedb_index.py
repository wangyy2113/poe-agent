"""POEDB 索引页清单与全量本地抓取。

为什么只抓索引页不做全站镜像（2026-08-04 勘测数据）：
  - 索引页自带内联/内嵌的结构化数据。实测 `/cn/Unique_item` 一页含 1229 件装备
    + 7204 条词缀；`/cn/Bows` 一页含 ~1100 条词缀的完整 JSON。
  - 索引页总量约 30-50 个 + 101 个词缀类别页，合计 ~150 页、几十 MB，
    限速下几分钟抓完，且可反复重建。
  - 全站则不同：仅从 17 个页面就发现 3962 个不同 /cn/ 页面，全站量级 1-3 万，
    抓完需数小时、1-2GB，而详情页内容多为索引页的重复，收益极低。

本地化的意义：抓取一次后，抽取器开发与迭代**完全离线**，不再反复压站点。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from kb.fetcher import Fetcher

logger = logging.getLogger(__name__)

BASE = "https://poedb.tw/cn"

#: 英文语言版前缀。POEDB 同一 slug 在各语言版下内容对应，
#: 因此英文名可由英文版页面取得，不必逐个抓详情页。
EN_BASE = "https://poedb.tw/us"

#: 词缀数据的枢纽页。101 个类别页清单从它的
#: `href="/cn/XXX#ModifiersCalc"` 链接动态提取——不硬编码，
#: 这样 POEDB 新增底材类别时自动跟上。
MODIFIERS_HUB = "Modifiers"

#: 父级类别页：POEDB 把这几个部位按属性需求拆成多页（`Body_Armours_str` 等），
#: 词缀数据在带后缀的子页上，但**唯一装备列表只在不带后缀的父页上**
#: （实测 `/cn/Body_Armours` 列 109 件传奇，`/cn/Body_Armours_int` 一件都没有）。
#: 由 101 个词缀页剥离属性后缀得到，此处固化以免每次重复推导。
PARENT_CLASS_PAGES: tuple[str, ...] = (
    "Body_Armours", "Boots", "Gloves", "Helmets", "Shields",
)


@dataclass(frozen=True)
class IndexPage:
    """一个索引页。`extractor` 标明由哪个抽取器负责，未实现的先留空。"""

    slug: str
    category: str
    note: str = ""
    extractor: str = ""

    @property
    def url(self) -> str:
        return f"{BASE}/{self.slug}"


#: 分类索引页清单。来源：POEDB 导航栏（21 个真实项）+ 勘测发现的补充项。
#: `extractor` 为空表示尚未实现抽取器——抓取仍会做（便于离线开发），
#: 但不会进入知识库。
INDEX_PAGES: tuple[IndexPage, ...] = (
    # ── 装备 ──
    IndexPage("Unique_item", "装备", "唯一装备，1229 件内联", "poedb_unique"),
    IndexPage("Items", "装备", "物品总览"),
    IndexPage("Gem", "游戏基础", "宝石总览"),
    IndexPage("Skill_Gems", "游戏基础", "技能宝石"),
    IndexPage("Support_Gems", "游戏基础", "辅助宝石"),
    IndexPage("Transfigured_Gems", "游戏基础", "改造宝石"),
    # 由 scripts/verify_index_pages.py 对照导航栏发现的遗漏项：
    # 宝石标签索引页，列 GemTags/exceptional 下 55 项（赋予 / 增幅 / 启蒙 等）
    IndexPage("Exceptional", "游戏基础", "卓越辅助宝石（GemTags/exceptional，55 项）"),
    IndexPage("Vendor_recipe_system", "制作系统", "商人配方"),
    # ── 词缀 ──
    IndexPage("Modifiers", "装备", "词缀枢纽页，含 101 个类别页链接", "poedb_mods_hub"),
    IndexPage("Keywords", "游戏基础", "关键词"),
    IndexPage("Crafting_Bench", "制作系统", "工艺台"),
    IndexPage("Horticrafting", "制作系统", "收获/花园工艺"),
    IndexPage("metamods", "制作系统", "工艺互动"),
    IndexPage("Oil", "制作系统", "圣油"),
    # ── 制作系统 ──
    IndexPage("Essence", "制作系统", "精华，2692 条词缀内联"),
    IndexPage("Betrayal", "制作系统", "辛迪加"),
    IndexPage("Harvest", "制作系统", "古灵庄园"),
    IndexPage("Incursion", "制作系统", "神庙"),
    # ── 地图系统 ──
    IndexPage("Maps", "地图系统", "地图"),
    IndexPage("Atlas", "地图系统", "异界图鉴"),
    IndexPage("Atlas_of_Worlds", "地图系统", "世界地图"),
    IndexPage("atlas-skill-tree", "地图系统", "Atlas 天赋树"),
    IndexPage("Monsters", "地图系统", "怪物"),
    # ── 使命 / 天赋 ──
    IndexPage("Quest", "游戏基础", "使命"),
    IndexPage("Ascendancy", "游戏基础", "升华试炼"),
    IndexPage("Ascendancy_class", "游戏基础", "升华职业"),
    IndexPage("Bloodline_Ascendancy_class", "游戏基础", "血脉升华"),
    IndexPage("passive-skill-tree", "游戏基础", "天赋树"),
    IndexPage("Pantheon", "游戏基础", "万神殿"),
    # ── 经济系统 ──
    IndexPage("Currency", "经济系统", "通货"),
    IndexPage("Scarab", "经济系统", "圣甲虫"),
    IndexPage("Flasks", "装备", "药剂"),
)


@dataclass
class HarvestStats:
    total: int = 0
    fetched: int = 0
    cached: int = 0
    failed: int = 0
    bytes_total: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


#: 枢纽页 `<li>` 里指向站点功能而非游戏内容的链接。只排除这一个，
#: 其余「看着像索引页」的一律留在残差里由抽取器判定——见 `HubCandidate`。
_HUB_NON_CONTENT_SLUGS: frozenset[str] = frozenset({"patreon"})

_HUB_LI_LINK = re.compile(r"^/cn/([A-Za-z_0-9%\-]+)(#ModifiersCalc)?$")


@dataclass(frozen=True)
class HubCandidate:
    """枢纽页上**没有** `#ModifiersCalc` 片段、也不属于已知索引页的链接。

    为什么需要它（2026-09-02 实测）：枢纽页 147 个 `<li>` 链接里只有 **101 个**
    带该片段，而地图词缀五页与已入库的
    `Mavens_Invitation%3A_The_Feared#ModifiersCalc` 是**并排的兄弟节点**，
    只是被写成了不带片段的形式：

        <li><a href="/cn/Mavens_Invitation%3A_The_Feared#ModifiersCalc">…</a></li>
        <li><a href="/cn/Maps_low_tier">异界地图(Low)</a></li>

    于是 `discover_mod_class_pages` 的正则把它们静默丢掉，地图 / 保险箱 /
    六分仪 / 瓦尔容器的词缀**一条都没入库**（`mod_affix` 74 个 item class 里
    这几类全部为 0），而「地图词缀有哪些」在评测里被记成**检索缺口**——
    读起来像是上游没有这份数据，实际是我们从没去看过那几页。

    旧代码唯一的守卫是「一个都没发现就报错」：它拦得住整页改版，
    **拦不住少认了 46 个**。这正是本项目反复记的那条——
    `if not slugs` 这种全量守卫对**部分漏掉**是瞎的。

    残差 = `<li>` 链接 − 带片段的 − `INDEX_PAGES` − `PARENT_CLASS_PAGES`
    − 站点功能链接。**刻意不再列第二份「哪些页有词缀」的硬编码清单**：
    那份清单与枢纽页迟早分叉，而分叉的方向恰好就是这次这种「静默少认」。
    页面到底有没有词缀，由抽取器拿 `new ModsView(` 载荷判定——
    `declared=False` 的页取不到载荷是**正常结果**（它本来就可能只是索引页），
    不是 `declared=True` 那种「声明了有却找不到」的结构告警。
    """

    slug: str
    label_zh: str


def _fetch_hub(fetcher: Fetcher, *, force: bool) -> str:
    res = fetcher.fetch(f"{BASE}/{MODIFIERS_HUB}", force=force)
    if not res.ok:
        raise RuntimeError(f"取词缀枢纽页失败 HTTP {res.status_code}")
    return res.text


def discover_mod_class_pages(fetcher: Fetcher, *, force: bool = False) -> list[str]:
    """从词缀枢纽页提取**声明了**词缀区块的「底材类别」页 slug（实测 101 个）。

    不硬编码清单：POEDB 新增底材类别（新联盟常见）时能自动跟上。

    注意它只认带 `#ModifiersCalc` 片段的写法——枢纽页上还有一批**不带片段
    但确实有词缀**的页，那些由 `discover_candidate_mod_pages` 负责，
    原因见 `HubCandidate`。
    """
    return _declared_mod_pages(_fetch_hub(fetcher, force=force))


def _declared_mod_pages(html: str) -> list[str]:
    slugs = sorted(set(re.findall(r'href="/cn/([A-Za-z_0-9%\-]+)#ModifiersCalc"', html)))
    if not slugs:
        raise RuntimeError(
            "枢纽页未发现任何 #ModifiersCalc 链接，POEDB 结构可能已变更；"
            "拒绝继续以免静默漏掉全部词缀数据"
        )
    logger.info("发现 %d 个词缀类别页", len(slugs))
    return slugs


def discover_candidate_mod_pages(
    fetcher: Fetcher, *, force: bool = False
) -> list[HubCandidate]:
    """枢纽页上未被片段声明、也未被已知索引页认领的残差链接（实测 22 个）。

    这些是「可能有词缀但从没被抓过」的候选，判定权交给抽取器，理由见 `HubCandidate`。
    """
    return _candidate_mod_pages(_fetch_hub(fetcher, force=force))


def _candidate_mod_pages(html: str) -> list[HubCandidate]:
    declared = set(re.findall(r'href="/cn/([A-Za-z_0-9%\-]+)#ModifiersCalc"', html))
    known = {p.slug for p in INDEX_PAGES} | set(PARENT_CLASS_PAGES)
    seen: dict[str, str] = {}
    for node in HTMLParser(html).css("li a"):
        matched = _HUB_LI_LINK.match(str(node.attributes.get("href") or "").strip())
        if matched is None:
            continue
        slug = matched.group(1)
        if slug in declared or slug in known or slug in _HUB_NON_CONTENT_SLUGS:
            continue
        seen.setdefault(slug, node.text(strip=True))
    logger.info("枢纽页残差链接 %d 个（未声明词缀区块、也不是已知索引页）", len(seen))
    return [HubCandidate(slug=s, label_zh=seen[s]) for s in sorted(seen)]


def harvest(
    fetcher: Fetcher,
    *,
    include_mod_pages: bool = True,
    force: bool = False,
    only: list[str] | None = None,
) -> HarvestStats:
    """把索引页与词缀类别页抓到本地缓存。

    抓取本身即「本地化」——后续抽取全部从 data/http_cache 离线进行。
    """
    stats = HarvestStats()

    # (slug, 分类, 站点前缀)。前缀让英文语言版页面也能纳入本地化——
    # 权威英文名只在英文版上，漏抓它就等于 L2 补全跑不了离线。
    targets: list[tuple[str, str, str]] = [(p.slug, p.category, BASE) for p in INDEX_PAGES]
    if include_mod_pages:
        hub = _fetch_hub(fetcher, force=force)
        for slug in _declared_mod_pages(hub):
            targets.append((slug, "装备/词缀", BASE))
        # 父级类别页：唯一装备列表只在这些不带属性后缀的页面上
        for slug in PARENT_CLASS_PAGES:
            targets.append((slug, "装备/类别传奇", BASE))
        # 残差候选页：枢纽页列了但没声明词缀区块的（地图 / 保险箱 / 六分仪 …）。
        # 抓下来才能离线判定它到底有没有词缀，理由见 `HubCandidate`。
        for candidate in _candidate_mod_pages(hub):
            targets.append((candidate.slug, "装备/词缀候选", BASE))
    # 英文语言版索引页：权威英文名与英文底材名的来源
    targets.append(("Unique_item", "装备/英文名", EN_BASE))

    if only:
        wanted = {s.lower() for s in only}
        targets = [t for t in targets if t[0].lower() in wanted]

    stats.total = len(targets)
    logger.info("开始抓取 %d 个页面（限速由 Fetcher 控制）", stats.total)

    for i, (slug, category, base) in enumerate(targets, 1):
        url = f"{base}/{slug}"
        try:
            res = fetcher.fetch(url, force=force)
        except Exception as exc:
            stats.failed += 1
            stats.failures.append((slug, f"{type(exc).__name__}: {exc}"))
            logger.warning("[%d/%d] %s 抓取异常：%s", i, stats.total, slug, exc)
            continue

        if not res.ok:
            stats.failed += 1
            stats.failures.append((slug, f"HTTP {res.status_code}"))
            logger.warning("[%d/%d] %s HTTP %s", i, stats.total, slug, res.status_code)
            continue

        size = len(res.text)
        stats.bytes_total += size
        if res.from_cache:
            stats.cached += 1
        else:
            stats.fetched += 1
        logger.info(
            "[%d/%d] %-34s %8.0f KB  %s  (%s)",
            i, stats.total, slug, size / 1024,
            "缓存" if res.from_cache else "抓取", category,
        )

    return stats
