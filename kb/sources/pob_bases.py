"""PoB 底材注册表抽取器（全装备类别）。

PoB ``Data/Bases/*.lua`` 提供底材的英文名、类型与生成标签；POEDB 的中英文类别页
提供同一 slug 下的中英文名称桥接。两侧必须逐条精确对应，任何缺失或多解都整条拒绝，
**不能由 slug 猜英文名，也不能把英文名冒充中文名**。

## 一个 lua 文件不等于一个 POEDB 页面

PoB 按武器族分文件（`sword.lua`），POEDB 按 ItemClass 分页面（单手剑 / 双手剑 /
锐剑各一页）。所以类别配置里一个 `BaseCategory` 允许挂多个页面，用 PoB 的 `type`
与页面上的 HTML class 双向对齐。实测漏掉锐剑页时 sword.lua 有 32 条桥不上，
补上 `Thrusting_One_Hand_Swords` 后只剩 7 条——**桥不上先怀疑少抓了页面**。

## PoB 侧有三类东西不该入库

按 2026-08-18 实测（1063 条底材，可桥接 1022 条 = 96.1%）分三族处理：

``pob_pseudo``    PoB 自用伪条目：`hidden = true`（`Energy Blade One Handed`）或
                  标签只剩 `default`（`Random One Hand Sword`、已从游戏移除的旧箭袋）。
                  **按规则排除，不算采集缺口**——POEDB 没有它们是正确的。
``variant``       PoB 把一个 POEDB 底材展开成多个变体：`Two-Toned Boots (Armour/Evasion)`、
                  `Two-Stone Ring (Fire/Lightning)`。去掉括号能对上，但那样多条 PoB 记录
                  会共用一个中文名与页面，数值却各不相同。**本轮跳过并登记**，
                  要做需要先决定变体的中文名从哪来。
``no_poedb_page`` 带 `experimental_base` 的实验底材（Ruthless 等），POEDB 列表页没有。
                  **跳过并登记**：没有中文名就不能入库。

跳过的条目由调用方写进缺口台账，不静默丢弃——那正是「知识库不知道自己缺什么」。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from selectolax.parser import HTMLParser

from kb.sources.pob_lua import LuaOpaque, load_lua_file, parse_assignments

BODY_BASE_FILE = "Data/Bases/body.lua"
POEDB_BODY_CN_URL = "https://poedb.tw/cn/Body_Armours"
POEDB_BODY_EN_URL = "https://poedb.tw/us/Body_Armours"

ARMOUR_TAG_TO_PAGE: dict[str, str] = {
    "str_armour": "Body_Armours_str",
    "dex_armour": "Body_Armours_dex",
    "int_armour": "Body_Armours_int",
    "str_dex_armour": "Body_Armours_str_dex",
    "str_int_armour": "Body_Armours_str_int",
    "dex_int_armour": "Body_Armours_dex_int",
    "str_dex_int_armour": "Body_Armours_str_dex_int",
}


class BaseStructureError(ValueError):
    """底材源结构不完整或跨来源无法唯一对应。"""


@dataclass(frozen=True)
class PoedbPage:
    """一个 POEDB 类别页面。`classes` 是页面上底材链接的 HTML class 后缀。

    一页可能含多个 class（`Flasks` 页上有 LifeFlask / ManaFlask / HybridFlask /
    UtilityFlask 四种），所以是元组而不是单值。
    """

    slug: str
    classes: tuple[str, ...]

    @property
    def cn_url(self) -> str:
        return f"https://poedb.tw/cn/{self.slug}"

    @property
    def en_url(self) -> str:
        return f"https://poedb.tw/us/{self.slug}"


@dataclass(frozen=True)
class BaseCategory:
    """一个底材类别：PoB 的一个 lua 文件 × POEDB 的若干页面。

    `item_classes` 是该文件里要收的 PoB `type` 值。写死而不是「照收全部」：
    mace.lua 里同时有单手锤、双手锤和短杖，而它们在 POEDB 是三个页面、
    在词缀库里也是三个 `item_class_page`。
    """

    key: str
    lua_file: str
    item_classes: tuple[str, ...]
    pages: tuple[PoedbPage, ...]
    #: 护甲类才有「按属性分的词缀页」（胸甲的 Body_Armours_str 等）。其余类别为 None。
    armour_tag_pages: dict[str, str] | None = None
    #: 该类别的底材是否必须有插槽。护甲与武器必须有（胸甲 socketLimit=0 是脏数据），
    #: 首饰、腰带、箭袋、药剂本来就没有。写成按类别声明而不是统一放开：
    #: 统一放开等于把「胸甲没有插槽」这种明显错误也放过去。
    requires_sockets: bool = True

    @property
    def source_file(self) -> str:
        return f"Data/Bases/{self.lua_file}"


#: 全部底材类别。条数与 slug 均按 2026-08-18 实测填写，改动前先跑 `--dry-run` 清点。
CATEGORIES: tuple[BaseCategory, ...] = (
    BaseCategory("body", "body.lua", ("Body Armour",),
                 (PoedbPage("Body_Armours", ("BodyArmour",)),), ARMOUR_TAG_TO_PAGE),
    BaseCategory("helmet", "helmet.lua", ("Helmet",),
                 (PoedbPage("Helmets", ("Helmet",)),)),
    BaseCategory("gloves", "gloves.lua", ("Gloves",),
                 (PoedbPage("Gloves", ("Gloves",)),)),
    BaseCategory("boots", "boots.lua", ("Boots",),
                 (PoedbPage("Boots", ("Boots",)),)),
    BaseCategory("shield", "shield.lua", ("Shield",),
                 (PoedbPage("Shields", ("Shield",)),)),
    BaseCategory("quiver", "quiver.lua", ("Quiver",),
                 (PoedbPage("Quivers", ("Quiver",)),), requires_sockets=False),
    BaseCategory("belt", "belt.lua", ("Belt",),
                 (PoedbPage("Belts", ("Belt",)),), requires_sockets=False),
    BaseCategory("amulet", "amulet.lua", ("Amulet",),
                 (PoedbPage("Amulets", ("Amulet",)),), requires_sockets=False),
    BaseCategory("ring", "ring.lua", ("Ring",),
                 (PoedbPage("Rings", ("Ring",)),), requires_sockets=False),
    BaseCategory("bow", "bow.lua", ("Bow",),
                 (PoedbPage("Bows", ("Bow",)),)),
    BaseCategory("claw", "claw.lua", ("Claw",),
                 (PoedbPage("Claws", ("Claw",)),)),
    BaseCategory("dagger", "dagger.lua", ("Dagger",),
                 (PoedbPage("Daggers", ("Dagger",)),
                  PoedbPage("Rune_Daggers", ("RuneDagger",)))),
    BaseCategory("axe", "axe.lua", ("One Handed Axe", "Two Handed Axe"),
                 (PoedbPage("One_Hand_Axes", ("OneHandAxe",)),
                  PoedbPage("Two_Hand_Axes", ("TwoHandAxe",)))),
    BaseCategory("mace", "mace.lua",
                 ("One Handed Mace", "Two Handed Mace", "Sceptre"),
                 (PoedbPage("One_Hand_Maces", ("OneHandMace",)),
                  PoedbPage("Two_Hand_Maces", ("TwoHandMace",)),
                  PoedbPage("Sceptres", ("Sceptre",)))),
    BaseCategory("sword", "sword.lua", ("One Handed Sword", "Two Handed Sword"),
                 (PoedbPage("One_Hand_Swords", ("OneHandSword",)),
                  PoedbPage("Two_Hand_Swords", ("TwoHandSword",)),
                  # 锐剑：漏掉这一页时 sword.lua 有 32 条桥不上
                  PoedbPage("Thrusting_One_Hand_Swords", ("ThrustingOneHandSword",)))),
    BaseCategory("staff", "staff.lua", ("Staff",),
                 (PoedbPage("Staves", ("Staff",)),
                  PoedbPage("Warstaves", ("Warstaff",)))),
    BaseCategory("wand", "wand.lua", ("Wand",),
                 (PoedbPage("Wands", ("Wand",)),)),
    BaseCategory("flask", "flask.lua", ("Flask",),
                 (PoedbPage("Flasks", ("LifeFlask", "ManaFlask",
                                       "HybridFlask", "UtilityFlask")),),
                 requires_sockets=False),
)

CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}

#: 变体后缀：`Two-Toned Boots (Armour/Evasion)`。
_VARIANT_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")

#: 跳过原因。`pob_pseudo` 不算采集缺口，另两类要进台账。
SKIP_PSEUDO = "pob_pseudo"
SKIP_VARIANT = "variant_expansion"
SKIP_NO_PAGE = "no_poedb_page"


@dataclass(frozen=True)
class SkippedBase:
    """未入库的 PoB 底材及原因。调用方负责登记，不静默丢弃。"""

    name_en: str
    category: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class BaseItem:
    """一条底材。字段来源分两侧：`name_zh` 来自 POEDB（P1），其余来自 PoB（P2）。"""

    base_id: str
    name_en: str
    name_zh: str
    poedb_slug: str
    item_class: str
    #: PoB 的 subType。腰带、戒指、箭袋等没有，为空串
    subtype: str
    #: 插槽上限。首饰、腰带、药剂没有插槽，为 0
    socket_limit: int
    tags: tuple[str, ...]
    influence_tags: dict[str, str]
    #: 只有护甲类有「按属性分的词缀页」；其余类别为空串
    armour_tag: str
    modifier_page: str
    implicit_text: str | None
    implicit_mod_types: list[Any]
    implicit_ids: tuple[str, ...]
    #: 数值表。护甲是 armour、武器是 weapon、药剂是 flask，首饰三者皆无
    stats: dict[str, Any]
    #: `stats` 取自 PoB 的哪个键（armour / weapon / flask / none）。
    #: 单列出来是为了不让「护甲值」与「武器伤害」挤在一个没有名字的 JSON 里
    stats_kind: str
    requirements: dict[str, Any]
    category_key: str = "body"
    poedb_page: str = "Body_Armours"
    source_file: str = BODY_BASE_FILE

    @property
    def poedb_url(self) -> str:
        return f"https://poedb.tw/cn/{self.poedb_slug}"

    @property
    def content_hash(self) -> str:
        payload = {
            "name_en": self.name_en,
            "name_zh": self.name_zh,
            "poedb_slug": self.poedb_slug,
            "item_class": self.item_class,
            "subtype": self.subtype,
            "socket_limit": self.socket_limit,
            "tags": self.tags,
            "influence_tags": self.influence_tags,
            "armour_tag": self.armour_tag,
            "modifier_page": self.modifier_page,
            "implicit_text": self.implicit_text,
            "implicit_mod_types": self.implicit_mod_types,
            "implicit_ids": self.implicit_ids,
            "stats": self.stats,
            "stats_kind": self.stats_kind,
            "requirements": self.requirements,
            "category_key": self.category_key,
            "poedb_page": self.poedb_page,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


#: 旧名。`craft/pool.py` 与既有测试按 `BodyArmourBase` 引用同一个结构——
#: 胸甲那条严格链路的调用点保持原样，不为一次泛化去改它。
BodyArmourBase = BaseItem


_SPACE_RE = re.compile(r"\s+")


def _text(node) -> str:  # noqa: ANN001
    return _SPACE_RE.sub(" ", node.text(deep=True)).strip() if node is not None else ""


#: 被排除的 slug 前缀。`Royale_*` 是已下线的 Royale 模式底材，**英文名与常规底材
#: 完全相同**（`Leather Belt` 同时对应 `Leather_Belt` 与 `Royale_Leather_Belt`），
#: 不剔掉就会让每个撞名的常规底材都变成「命中 2 个 slug」而整批拒绝。
#: 它们是另一个游戏模式的命名空间，不是常规底材的变体。
_EXCLUDED_SLUG_PREFIXES = ("Royale_",)

#: 至少含一个中日韩字符 —— 用来在「同 slug 中英双标签」时挑出真正的中文名。
_CJK_RE = re.compile(r"[一-鿿]")


def parse_poedb_base_names(
    html: str, classes: tuple[str, ...], *, page: str = ""
) -> tuple[dict[str, str], set[str]]:
    """返回 ``(slug -> 显示名, 无法唯一命名的 slug 集合)``。空页面直接拒绝。

    页面上同一个底材会出现两次（一个空文本的图标链接 + 一个带名字的链接），空名跳过。

    同一个 slug 出现多个不同显示名有两种真实情况，处理方式不同：

    **中英双标签**：中文页对个别条目同时给出英文与中文（实测 `Unset_Ring` →
    `Unset Ring` 与 `潜能之戒`）。此时取含中日韩字符的那个——这个字段的定义就是中文名，
    英文那个是渲染兜底。

    **一个 slug 多个变体名**：`Two-Toned_Boots` 在中文页有「异色鞋 (火冰)」
    「异色鞋 (火闪)」「异色鞋(冰闪)」三个。无法唯一命名，**记为 ambiguous 并从桥接池
    剔除**，对应的 PoB 底材会按 variant 跳过并登记。挑一个就是伪造对应关系。
    """
    tree = HTMLParser(html)
    candidates: dict[str, list[str]] = {}
    for cls in classes:
        for link in tree.css(f"a.whiteitem.{cls}"):
            name = _text(link)
            href = str(link.attributes.get("href") or "").strip()
            if not name or not href:
                continue
            slug = href.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            if not slug or slug.startswith(_EXCLUDED_SLUG_PREFIXES):
                continue
            if name not in candidates.setdefault(slug, []):
                candidates[slug].append(name)

    out: dict[str, str] = {}
    ambiguous: set[str] = set()
    for slug, names in candidates.items():
        if len(names) == 1:
            out[slug] = names[0]
            continue
        cjk = [n for n in names if _CJK_RE.search(n)]
        if len(cjk) == 1:
            out[slug] = cjk[0]
        else:
            ambiguous.add(slug)
    if not out:
        if ambiguous:
            # 区分两种空：真的没抓到卡片（抽取器或页面结构问题），
            # 与全部条目都无法唯一命名（数据问题）。混成一句会指错排查方向。
            raise BaseStructureError(
                f"POEDB {page or classes} 页面上 {len(ambiguous)} 个 slug 全部无法唯一命名"
            )
        raise BaseStructureError(
            f"POEDB {page or classes} 页面没有可识别的普通底材卡片"
        )
    return out, ambiguous


def parse_poedb_body_names(html: str) -> dict[str, str]:
    """胸甲页的兼容入口（既有测试仍在用）。只返回名称映射。"""
    names, _ = parse_poedb_base_names(html, ("BodyArmour",), page="Body_Armours")
    return names


def _jsonable(value: Any, *, path: str) -> Any:
    if isinstance(value, LuaOpaque):
        raise BaseStructureError(f"{path} 含未求值 Lua 表达式")
    if isinstance(value, dict):
        int_keys = sorted(k for k in value if isinstance(k, int))
        other_keys = [k for k in value if not isinstance(k, int)]
        if int_keys and not other_keys and int_keys == list(range(1, len(int_keys) + 1)):
            return [_jsonable(value[k], path=f"{path}[{k}]") for k in int_keys]
        return {
            str(k): _jsonable(v, path=f"{path}.{k}")
            for k, v in value.items()
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise BaseStructureError(f"{path} 含不支持的值类型 {type(value).__name__}")


def _string_set(value: Any, *, path: str) -> tuple[str, ...]:
    if not isinstance(value, dict):
        raise BaseStructureError(f"{path} 必须是集合表")
    names = sorted(str(k) for k, enabled in value.items() if isinstance(k, str) and enabled is True)
    if not names:
        raise BaseStructureError(f"{path} 不能为空")
    return tuple(names)


def _string_map(value: Any, *, path: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise BaseStructureError(f"{path} 必须是映射表")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item:
            raise BaseStructureError(f"{path} 含非字符串键值")
        out[key] = item
    return dict(sorted(out.items()))


def _string_array(value: Any, *, path: str) -> tuple[str, ...]:
    converted = _jsonable(value or {}, path=path)
    if converted == {}:
        return ()
    if not isinstance(converted, list) or any(not isinstance(item, str) for item in converted):
        raise BaseStructureError(f"{path} 必须是字符串数组")
    return tuple(converted)


#: PoB 存放数值的键，按类别只会命中其中一个。首饰、腰带、箭袋一个都没有。
_STATS_KEYS = ("armour", "weapon", "flask")


def _classify_skip(
    name_en: str, raw: dict[str, Any], en_names: set[str], ambiguous_names: set[str]
) -> tuple[str, str] | None:
    """判断这条 PoB 底材是否该跳过。返回 (reason, detail) 或 None。

    三族的判据见模块 docstring。顺序有讲究：先判伪条目——`Random One Hand Sword`
    这类既没有真实标签也不该进台账，若先判「POEDB 没这一页」就会把它记成采集缺口。
    """
    tags = set(raw.get("tags") or {})
    if raw.get("hidden") is True:
        return SKIP_PSEUDO, "PoB 标了 hidden，是它自用的伪条目"
    if tags == {"default"}:
        return SKIP_PSEUDO, "只有 default 标签，非真实可掉落底材（多为已从游戏移除）"
    stripped = _VARIANT_SUFFIX_RE.sub("", name_en)
    if stripped != name_en and (stripped in en_names or stripped in ambiguous_names):
        # `ambiguous_names` 也要看：异色鞋在 POEDB 中文页有三个变体名，那个 slug
        # 因无法唯一命名已被剔出桥接池，只查 en_names 会把它误判成「POEDB 没这一页」。
        return SKIP_VARIANT, f"PoB 变体展开，POEDB 只有合并条目 {stripped!r}"
    if "experimental_base" in tags:
        return SKIP_NO_PAGE, "实验底材（experimental_base），POEDB 列表页未收录"
    return SKIP_NO_PAGE, "POEDB 对应类别页面上没有同名条目"


def extract_category_bases(
    category: BaseCategory,
    pob_root: Path,
    pages_html: dict[str, tuple[str, str]],
) -> tuple[list[BaseItem], list[SkippedBase]]:
    """抽取一个类别的底材并与 POEDB 中英文名称逐条桥接。

    `pages_html` 是 ``页面 slug -> (中文 HTML, 英文 HTML)``，必须覆盖
    `category.pages` 的全部页面——少一页会让该页对应的底材全部桥不上，
    那种失败应该在配置层暴露，而不是变成一堆「缺口」。

    返回 `(入库条目, 跳过条目)`。桥接不上的**不抛异常**：PoB 里确实存在
    POEDB 没有页面的底材（实验底材），整批拒绝会让 96% 可用数据一起进不来。
    但跳过的必须逐条带原因返回，由调用方登记。
    """
    path = pob_root / "src" / Path(category.source_file)
    if not path.is_file():
        raise BaseStructureError(f"找不到 {category.source_file}")

    raw_bases = parse_assignments(load_lua_file(path))
    if not raw_bases:
        raise BaseStructureError(f"{category.source_file} 没有可识别底材")

    cn_by_slug: dict[str, str] = {}
    en_by_slug: dict[str, str] = {}
    slug_page: dict[str, str] = {}
    ambiguous_slugs: set[str] = set()
    ambiguous_names: set[str] = set()
    for page in category.pages:
        htmls = pages_html.get(page.slug)
        if htmls is None:
            raise BaseStructureError(
                f"类别 {category.key} 缺页面 {page.slug} 的 HTML —— 配置与调用方不一致"
            )
        cn_html, en_html = htmls
        cn, cn_ambiguous = parse_poedb_base_names(cn_html, page.classes, page=page.slug)
        en, en_ambiguous = parse_poedb_base_names(en_html, page.classes, page=page.slug)
        # 任一侧无法唯一命名的 slug 一律不进桥接池：中文名不确定就不能入库
        for slug in cn_ambiguous | en_ambiguous:
            cn.pop(slug, None)
            # 记下被剔除 slug 的英文名，变体判定要用（异色鞋就在这一类里）
            dropped = en.pop(slug, "")
            if dropped:
                ambiguous_names.add(dropped)
            ambiguous_slugs.add(slug)
        if set(cn) != set(en):
            missing_cn = sorted(set(en) - set(cn))
            missing_en = sorted(set(cn) - set(en))
            raise BaseStructureError(
                f"POEDB {page.slug} 中英文清单 slug 不一致："
                f"缺中文={missing_cn[:5]}，缺英文={missing_en[:5]}"
            )
        for slug in cn:
            # 同一个 slug 出现在多页只保留首个页面归属，不作为错误：实测
            # `Energy_Blade` 同时挂在单手剑与双手剑页上，而它是 PoB 的伪条目、
            # 永远不会被桥接到。为此整批失败不值得。
            slug_page.setdefault(slug, page.slug)
        cn_by_slug.update(cn)
        en_by_slug.update(en)

    slugs_by_en: dict[str, list[str]] = {}
    for slug, name in en_by_slug.items():
        slugs_by_en.setdefault(name, []).append(slug)
    en_names = set(slugs_by_en)

    out: list[BaseItem] = []
    skipped: list[SkippedBase] = []
    seen_ids: set[str] = set()
    for name_en, raw in raw_bases.items():
        if not isinstance(raw, dict):
            raise BaseStructureError(f"底材 {name_en!r} 不是表")
        item_class = raw.get("type")
        if item_class not in category.item_classes:
            # 同一个 lua 文件里的其它 ItemClass 由别的类别配置负责，不是错误
            continue

        matches = slugs_by_en.get(str(name_en), [])
        if len(matches) > 1:
            # 多解必须整批拒绝：猜一个就等于伪造中文名对应关系
            raise BaseStructureError(
                f"PoB 底材 {name_en!r} 在 POEDB 英文页命中 {len(matches)} 个 slug"
            )
        if not matches:
            reason, detail = _classify_skip(
                str(name_en), raw, en_names, ambiguous_names
            )
            skipped.append(SkippedBase(str(name_en), category.key, reason, detail))
            continue
        slug = matches[0]
        name_zh = cn_by_slug.get(slug, "")
        if not name_zh:
            raise BaseStructureError(f"POEDB 底材 {slug!r} 缺中文名")

        subtype = raw.get("subType")
        if subtype is None:
            subtype = ""  # 腰带、戒指、箭袋等 PoB 不给 subType
        if not isinstance(subtype, str):
            raise BaseStructureError(f"{name_en!r}.subType 不是字符串")

        socket_limit = raw.get("socketLimit")
        if socket_limit is None:
            socket_limit = 0  # 首饰、腰带、箭袋、药剂没有插槽
        if not isinstance(socket_limit, int) or socket_limit < 0:
            raise BaseStructureError(f"{name_en!r}.socketLimit 非法：{socket_limit!r}")
        if category.requires_sockets and socket_limit <= 0:
            raise BaseStructureError(
                f"{name_en!r}.socketLimit 非正整数（{socket_limit!r}），"
                f"而 {category.key} 类别的底材必须有插槽"
            )

        tags = _string_set(raw.get("tags"), path=f"{name_en}.tags")

        armour_tag = ""
        modifier_page = ""
        if category.armour_tag_pages:
            armour_tags = [t for t in category.armour_tag_pages if t in tags]
            if len(armour_tags) != 1:
                raise BaseStructureError(
                    f"{name_en!r} 必须且只能有一个属性护甲标签，实际 {armour_tags}"
                )
            armour_tag = armour_tags[0]
            modifier_page = category.armour_tag_pages[armour_tag]

        influence_tags = _string_map(
            raw.get("influenceTags") or {}, path=f"{name_en}.influenceTags"
        )

        stats: dict[str, Any] = {}
        stats_kind = "none"
        for key in _STATS_KEYS:
            if raw.get(key) is None:
                continue
            value = _jsonable(raw.get(key), path=f"{name_en}.{key}")
            # 空表是真实形态：`Golden Wreath` 这类头盔有 armour 键但没有护甲值。
            # 只要求「是表」，不要求非空——要求非空会把它们整批拒绝。
            if not isinstance(value, dict):
                raise BaseStructureError(f"{name_en!r}.{key} 不是表")
            stats, stats_kind = value, key
            break

        requirements = _jsonable(raw.get("req") or {}, path=f"{name_en}.req")
        if not isinstance(requirements, dict):
            raise BaseStructureError(f"{name_en!r}.req 不是表")

        seed = json.dumps(
            [category.source_file, str(name_en)], ensure_ascii=False, separators=(",", ":")
        )
        base_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        if base_id in seen_ids:
            raise BaseStructureError(f"底材稳定键重复：{name_en!r}")
        seen_ids.add(base_id)

        implicit_raw = raw.get("implicit")
        if implicit_raw is not None and not isinstance(implicit_raw, str):
            raise BaseStructureError(f"{name_en!r}.implicit 不是字符串")
        implicit_mod_types = _jsonable(
            raw.get("implicitModTypes") or {}, path=f"{name_en}.implicitModTypes"
        )
        if implicit_mod_types == {}:
            implicit_mod_types = []
        if not isinstance(implicit_mod_types, list):
            raise BaseStructureError(f"{name_en!r}.implicitModTypes 不是数组")

        out.append(
            BaseItem(
                base_id=base_id,
                name_en=str(name_en),
                name_zh=name_zh,
                poedb_slug=slug,
                item_class=str(item_class),
                subtype=subtype,
                socket_limit=socket_limit,
                tags=tags,
                influence_tags=influence_tags,
                armour_tag=armour_tag,
                modifier_page=modifier_page,
                implicit_text=implicit_raw,
                implicit_mod_types=implicit_mod_types,
                implicit_ids=_string_array(
                    raw.get("implicitIds"), path=f"{name_en}.implicitIds"
                ),
                stats=stats,
                stats_kind=stats_kind,
                requirements=requirements,
                category_key=category.key,
                poedb_page=slug_page[slug],
                source_file=category.source_file,
            )
        )

    out.sort(key=lambda base: (base.item_class, base.armour_tag, base.name_en))
    return out, skipped


def extract_body_armour_bases(
    pob_root: Path,
    poedb_cn_html: str,
    poedb_en_html: str,
) -> list[BaseItem]:
    """胸甲的兼容入口：只抽胸甲，且**桥接不上即整批拒绝**。

    保留严格语义是有意的：`craft/pool.py` 的条件权重概率完全建立在胸甲注册表上，
    那条链路宁可不给答案也不能给一份缺条目的池。全类别导入走
    `extract_category_bases`，它按族跳过并登记。
    """
    category = CATEGORY_BY_KEY["body"]
    bases, skipped = extract_category_bases(
        category, pob_root, {"Body_Armours": (poedb_cn_html, poedb_en_html)}
    )
    if skipped:
        names = ", ".join(s.name_en for s in skipped[:5])
        raise BaseStructureError(
            f"{len(skipped)} 条胸甲未能与 POEDB 桥接（{names}）——胸甲注册表要求全覆盖"
        )
    return bases
