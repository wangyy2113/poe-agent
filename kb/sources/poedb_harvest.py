"""POEDB 收获（古灵庄园）定向工艺抽取器。

本模块只结构化页面明确列出的两类事实：**定向工艺的效果文本**与**原始命能费用**。
它不解释这些选项如何改变装备状态，也不据此判断某件装备当前能否执行该工艺——
与 `poedb_crafting.py`（工艺工作台）同一条边界。

## 这一页有三张表，三张都取（分三轮才取全）

`/cn/Horticrafting` 实测：

| 表 | 行数 | 内容 | 取它的那一轮 |
| --- | --- | --- | --- |
| 0 | 74 | `描述 ｜ 消耗` —— 定向工艺目录 | 第三十六轮 |
| 1 | 51 | `怪物 ｜ 阶级 ｜ Colour` —— 种子怪物按颜色掉命能 | 第五十八轮 |
| 2 | 41 | `Type ｜ Name ｜ Tags` —— 按标签改词缀权重，四类混排 | 第六十五轮 |

**后两张各挂了二十多轮，两次都是因为名字被写错了。** 表 1 在台账里叫
「作物与掉落来源」（真表头是 `怪物 ｜ 阶级 ｜ Colour`）；表 2 叫「催化剂等参考表」，
本模块注释还写过「催化剂与它们的标签，属于另一个域，这里仍然不取」——
而它化石那 18 行的 `Tags` **就是 `fossil` 域声明未采集的权重乘数**。
**注释与台账里的名字都会骗人，动手前先看真实表头。**

所以**按表头定位而不是按下标取表**：上游加一张表，下标就会指错，
而指错之后仍然「抽得出内容」——那是最难发现的一类错。

## 费用的写法与工艺台**相反**

工艺台是 `6x <通货链接>`（数量在前），这一页是 `<通货链接> x50`（数量在后），
多个组件之间用 `<br>` 分隔（实测 50 条单组件、24 条双组件，共 98 条）。
所以这里另写一个解析器而不是复用 `poedb_crafting._extract_costs`——
**两页的记法不同，不是同一段逻辑的两次实现**。要合并请先确认两边的顺序断言。

## `crafting*` span class 是**配色不是语义**，一律不存

描述里带 `<span class="craftingfire">火焰</span>` 这类 class，看着像现成的分类标签。
实测三处反证：

* 「重铸……包括一个**召唤生物**词缀」→ `craftingcaster`（召唤 ≠ 施法）
* 「重铸……包括一个**魔力**词缀」→ `craftingcaster`
* 「重铸……包括一个**元素**词缀」→ **没有任何 class**

拿它建分类列，「哪些收获工艺能改施法词缀」会答出召唤生物与魔力两条、还漏掉元素那条。
而**描述原文里本来就有那个中文词**（「火焰词缀」「元素词缀」），检索与 LIKE 都能匹配。
所以只保留纯文本。

## 描述**不**过 `clean_mod_text`

工艺台那边对效果单元格跑 `poedb_mods.clean_mod_text`，因为词缀正文里有 `<br>` 多行与
`(4—6)` 区间要归一。收获这一页实测**两样都没有**（74 条描述里 `<br>` 0 条、
破折号区间 0 条），所以直接取文本。照抄那一步不会更安全，只会让人以为这里也有多行词缀。

## 费用通货自带对账口径

费用链接的 `data-hover` 里是 GGG 元数据路径
（`Metadata/Items/Currency/HarvestSeedRed`），它正是 `poedb_currency.metadata_id`。
实测 **5/5** 命中且中文名逐字一致，所以调用方可以在入库前把这一维对完账
（与工艺台「18 个通货 100% 命中」是同一条检查）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import unquote, urljoin, urlparse

from selectolax.parser import HTMLParser, Node

HORTICRAFTING_URL = "https://poedb.tw/cn/Horticrafting"

#: 定向工艺主表的表头。**按它定位表**，不按下标。
EXPECTED_HEADERS = ("描述", "消耗")

#: 另外两张表的表头，用来报「本模块不取的有多少行」——静默省略与
#: 「页面上就没有」事后分不清。
#: 种子怪物表。**这张表的名字第三十六轮起被叫错了**：台账里写的是
#: 「作物与掉落来源」，而它三列其实是 `怪物 ｜ 阶级 ｜ Colour`——
#: 种子生成的怪物、它的阶级、以及**颜色**。颜色决定掉哪种命能，
#: 所以它是命能的获取途径（第五十八轮入库）。
SEED_HEADERS = ("怪物", "阶级", "Colour")
#: 标签权重乘数表。它被叫错过两次——台账里写「催化剂等参考表」、
#: 注释里写「催化剂与它们的标签，属于另一个域」。**实测三列是 `Type ｜ Name ｜ Tags`，
#: 四类混排**（化石 18 / 花园工艺站 11 / 催化 10 / 工艺工作台 2），
#: 而 `Tags` 列在化石那 18 行上**就是权重乘数**（`炽炎化石 → 冰霜x0,火焰x10`）——
#: 也就是 `fossil` 域 `partial_note` 里一直写着「未采集」的那一维。
#: 第六十五轮入库，理由与判据见 `parse_tag_weights`。
MISC_HEADERS = ("Type", "Name", "Tags")

#: `Tags` 单元格里跟在每个 `<span class="badge">` 后面的乘数：` x0` / ` x10` / ` x0.15`。
#: **必须逐个 span 取标签，不能对整格 `text()` 做切分**：实测「有害催化剂」那格是
#: 两个相邻 span（`physical_damage` 与 `chaos_damage`）且中间没有分隔符，
#: 整格取文本会粘成 `physical_damagechaos_damage` 这一个假标签。
_TAG_MULT_RE = re.compile(r"^[,\s]*x\s*([0-9]+(?:\.[0-9]+)?)")

#: 给了乘数的两类。催化剂与花园工艺站页面**只给标签、不给数字**，
#: 那两类的 `multiplier` 留 None——补 1 会把「页面没说」写成「乘数是 1」。
TAG_WEIGHT_KINDS_WITH_MULTIPLIER = ("化石", "工艺工作台")

#: `<a>通货</a> x50` 里跟在链接后面的数量。**数量在后**，与工艺台相反。
_AMOUNT_RE = re.compile(r"^\s*x\s*(\d+)\s*$", re.I)
#: `data-hover="?s=Data\BaseItemTypes\Metadata\Items\Currency\HarvestSeedRed"`
_META_RE = re.compile(r"(Metadata/Items/[\w/]+)")


class HarvestStructureError(ValueError):
    """页面结构或关键字段不完整；调用方必须拒绝整批写入。"""


@dataclass(frozen=True)
class HarvestCost:
    """一条工艺的一个费用组件。"""

    position: int
    amount: int
    currency_metadata_id: str
    currency_slug: str
    currency_name_zh: str
    currency_url: str

    def canonical(self) -> dict[str, object]:
        return {
            "position": self.position,
            "amount": self.amount,
            "currency_metadata_id": self.currency_metadata_id,
            "currency_slug": self.currency_slug,
            "currency_name_zh": self.currency_name_zh,
            "currency_url": self.currency_url,
        }


@dataclass(frozen=True)
class HarvestCraft:
    """一条定向工艺。`craft_id` 取整条记录 canonical JSON 的 SHA-256。"""

    effect_text: str
    costs: tuple[HarvestCost, ...]
    url: str
    craft_id: str = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        payload = json.dumps(
            {
                "effect_text": self.effect_text,
                "costs": [c.canonical() for c in self.costs],
                "url": self.url,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        object.__setattr__(self, "craft_id", f"harvest:{digest}")
        object.__setattr__(self, "content_hash", digest)


@dataclass
class HarvestExtractStats:
    """一次抽取的清点。`not_taken` 是本模块**有意跳过**的两张表的行数。"""

    total: int = 0
    costs: int = 0
    multi_cost_crafts: int = 0
    currencies: set[str] = field(default_factory=set)
    not_taken: dict[str, int] = field(default_factory=dict)


def _text(node: Node) -> str:
    return " ".join(node.text(deep=True, separator="").split())


def _headers(table: Node) -> tuple[str, ...]:
    cells = table.css("thead th") or table.css("tr th")
    return tuple(" ".join(c.text(deep=True).split()) for c in cells)


def _find_table(tree: HTMLParser, expected: tuple[str, ...], *, what: str) -> Node | None:
    matches = [t for t in tree.css("table") if _headers(t) == expected]
    if len(matches) > 1:
        raise HarvestStructureError(
            f"预期至多一个{what}表，实际匹配 {len(matches)} 个；"
            f"观测到的表头：{[_headers(t) for t in tree.css('table')]}"
        )
    return matches[0] if matches else None


def _extract_costs(
    cell: Node, *, row_number: int, page_url: str,
    known_currencies_by_slug: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[HarvestCost, ...]:
    """解析 `<a>通货</a> x50<br><a>通货</a> x2` 形态的费用单元格。

    **数量在链接之后**（工艺台正相反）。所以逐个节点走：遇到通货链接先挂起，
    遇到数量文本再配上去。挂起的链接没等到数量、或数量没有对应链接，都是结构变了。
    """
    costs: list[HarvestCost] = []
    pending: dict | None = None

    def flush(amount: int) -> None:
        nonlocal pending
        if pending is None:
            raise HarvestStructureError(
                f"第 {row_number} 行出现数量 x{amount} 但前面没有通货链接"
            )
        costs.append(
            HarvestCost(
                position=len(costs),
                amount=amount,
                currency_metadata_id=pending["metadata_id"],
                currency_slug=pending["slug"],
                currency_name_zh=pending["name"],
                currency_url=pending["url"],
            )
        )
        pending = None

    for node in cell.iter(include_text=True):
        if node.tag == "-text":
            raw = node.text(deep=False)
            if not raw.strip():
                continue
            match = _AMOUNT_RE.fullmatch(raw)
            if match is None:
                raise HarvestStructureError(
                    f"第 {row_number} 行费用数量格式无法识别：{raw!r}"
                )
            amount = int(match.group(1))
            if amount <= 0:
                raise HarvestStructureError(f"第 {row_number} 行费用必须大于 0")
            flush(amount)
            continue

        if node.tag != "a":
            continue
        classes = set((node.attributes.get("class") or "").split())
        if "item_currency" not in classes:
            raise HarvestStructureError(f"第 {row_number} 行费用单元格含非通货链接")
        if pending is not None:
            raise HarvestStructureError(
                f"第 {row_number} 行连续两个通货链接之间没有数量"
            )
        href = (node.attributes.get("href") or "").strip()
        name = _text(node)
        currency_url = urljoin(page_url, href)
        slug = urlparse(currency_url).path.rstrip("/").rsplit("/", 1)[-1]
        hover = unquote(node.attributes.get("data-hover") or "").replace("\\", "/")
        meta = _META_RE.search(hover)
        if not href or not slug or not name or not currency_url.startswith("https://"):
            raise HarvestStructureError(
                f"第 {row_number} 行通货字段不完整：href={href!r}, name={name!r}"
            )
        known = (known_currencies_by_slug or {}).get(slug)
        if known and (known[1] != name or (meta and meta.group(1) != known[0])):
            raise HarvestStructureError(
                f"第 {row_number} 行通货 {name!r} 与已入库 slug 身份不一致"
            )
        metadata_id = meta.group(1) if meta else (known[0] if known else None)
        if metadata_id is None:
            # Opaque tooltip hashes are not IDs. Resolve only through a
            # previously verified currency slug and the same Chinese name.
            raise HarvestStructureError(
                f"第 {row_number} 行通货 {name!r} 的 data-hover 里没有元数据路径，"
                "且没有可核对的通货目录记录"
            )
        pending = {
            "metadata_id": metadata_id,
            "slug": slug,
            "name": name,
            "url": currency_url,
        }

    if pending is not None:
        raise HarvestStructureError(
            f"第 {row_number} 行末尾通货 {pending['name']!r} 没有对应数量"
        )
    if not costs:
        raise HarvestStructureError(f"第 {row_number} 行没有费用")
    return tuple(costs)


def extract_harvest_crafts(
    html: str, *, page_url: str = HORTICRAFTING_URL,
    known_currencies_by_slug: Mapping[str, tuple[str, str]] | None = None,
) -> tuple[list[HarvestCraft], HarvestExtractStats]:
    """抽取定向工艺目录。返回 ``(工艺, 清点)``。"""
    tree = HTMLParser(html)
    table = _find_table(tree, EXPECTED_HEADERS, what="定向工艺主")
    if table is None:
        raise HarvestStructureError(
            f"找不到表头为 {EXPECTED_HEADERS} 的定向工艺主表；"
            f"观测到的表头：{[_headers(t) for t in tree.css('table')]}"
        )

    stats = HarvestExtractStats()
    crafts: list[HarvestCraft] = []
    seen: set[str] = set()
    for row_number, tr in enumerate(table.css("tr"), start=1):
        cells = tr.css("td")
        if len(cells) != 2:
            continue
        stats.total += 1
        effect = _text(cells[0])
        if not effect:
            raise HarvestStructureError(f"第 {row_number} 行描述为空")
        costs = _extract_costs(
            cells[1], row_number=row_number, page_url=page_url,
            known_currencies_by_slug=known_currencies_by_slug,
        )
        craft = HarvestCraft(effect_text=effect, costs=costs, url=page_url)
        if craft.craft_id in seen:
            # 描述实测 74/74 唯一；真撞上说明页面重复列了同一条
            raise HarvestStructureError(f"第 {row_number} 行与前面的工艺完全重复：{effect!r}")
        seen.add(craft.craft_id)
        crafts.append(craft)
        stats.costs += len(costs)
        stats.multi_cost_crafts += len(costs) > 1
        stats.currencies.update(c.currency_name_zh for c in costs)

    if not crafts:
        raise HarvestStructureError("定向工艺主表里没有解析到任何一行")

    # 这一页三张表现在都入库了：定向工艺（本函数）、种子怪物（`parse_seeds`，
    # 第五十八轮）、标签权重乘数（`parse_tag_weights`，第六十五轮）。
    # 所以 `not_taken` 空着——**它空着才是对的**，不是忘了填：
    # 这个字段的用途是「有意省略了多少行」，页面全取完之后就该没有条目。
    return crafts, stats


@dataclass(frozen=True)
class HarvestSeed:
    """一种种子怪物。`colour` 决定掉哪种命能。"""

    monster_zh: str
    tier: int
    #: `Wild` / `Vivid` / `Primal`。**与命能通货的英文名前缀逐字相同**
    #: （`Wild Crystallised Lifeforce`），所以桥接是查表不是推断。
    colour: str


def parse_seeds(html: str, *, page_url: str = HORTICRAFTING_URL) -> tuple[HarvestSeed, ...]:
    """解析种子怪物表。没有这张表就返回空元组。

    这一维回答的是**命能怎么来**：种子生成的怪物按颜色掉对应命能
    （实测 Wild / Vivid / Primal 各 17 个、共 51 个）。

    **颜色到命能是查表不是推断**：`Colour` 的三个取值与
    `poedb_currency.name_en` 的前缀逐字相同（`Wild Crystallised Lifeforce`）。
    第四种命能「神圣白晶」**不在这张表里**——它没有对应的种子怪物，
    这条边界要在出口说清，不能让用户以为四种都覆盖了。
    """
    tree = HTMLParser(html)
    table = _find_table(tree, SEED_HEADERS, what="种子怪物表")
    if table is None:
        return ()
    out: list[HarvestSeed] = []
    for row in table.css("tbody tr"):
        cells = row.css("td")
        if len(cells) != len(SEED_HEADERS):
            continue
        monster, tier_raw, colour = (_text(c) for c in cells)
        if not monster or not colour:
            continue
        try:
            tier = int(tier_raw)
        except ValueError:
            raise HarvestStructureError(
                f"种子怪物 {monster!r} 的阶级 {tier_raw!r} 不是数字"
            ) from None
        out.append(HarvestSeed(monster_zh=monster, tier=tier, colour=colour))
    return tuple(out)


@dataclass(frozen=True)
class TagWeight:
    """一条「这个东西把某个词缀标签的权重乘以多少」。

    `kind` 逐字取自页面的 `Type` 列，四类共用一张表是**因为页面就是一张表**——
    它们的共同语义正是「按标签改词缀权重」：

        化石       炽炎化石 → 冰霜 x0、火焰 x10      （屏蔽冰霜、火焰权重 ×10）
        工艺工作台   不能附加武器攻击相关属性修正 → 攻击 x0
        催化       有害催化剂 → physical_damage / chaos_damage   （只给标签）
        花园工艺站   荒野幼熊 → 施法                              （只给标签）
    """

    kind: str
    name_zh: str
    tag: str
    #: 乘数。**只有化石与工艺工作台给了数字**；另两类页面没给，留 None。
    #: `x0` 是「这类词缀不会出现」这个确定的事实，与 None（页面没说）不是一回事。
    multiplier: float | None

    @property
    def blocks(self) -> bool:
        """乘数为 0 = 这一类词缀被屏蔽。`None` 不算屏蔽（页面没给数字）。"""
        return self.multiplier == 0.0


def _parse_tag_cell(cell: Node) -> tuple[tuple[str, float | None], ...]:
    """`Tags` 单元格 → ((标签, 乘数), …)，按页面顺序。

    逐个 `span` 取标签、把紧跟其后的文本节点当乘数，理由见 `_TAG_MULT_RE`。
    """
    out: list[list] = []
    for node in cell.iter(include_text=True):
        if node.tag == "span":
            out.append([node.text(strip=True), None])
        elif node.tag == "-text" and out and out[-1][1] is None:
            matched = _TAG_MULT_RE.match(node.text() or "")
            if matched:
                out[-1][1] = float(matched.group(1))
    return tuple((tag, mult) for tag, mult in out if tag)


def parse_tag_weights(
    html: str, *, page_url: str = HORTICRAFTING_URL
) -> tuple[TagWeight, ...]:
    """解析标签权重乘数表。没有这张表就返回空元组。

    这一维补的是 `fossil` 域 `partial_note` 里那句「化石对权重的乘数未采集」。
    **它一直在这一页上**，而台账把这张表记成「催化剂等参考表」、
    本模块的注释又写着「属于另一个域，这里仍然不取」——
    于是它挂了近三十轮。**注释与台账里的名字都会骗人，动手前先看真实表头**
    （与第五十八轮种子怪物表被叫成「作物与掉落来源」是同一个错）。

    三条实测判据：

    * **标签逐个 `span` 取**，不对整格切分文本（见 `_TAG_MULT_RE`）；
    * **只有化石与工艺工作台有乘数**，另两类留 None 而不是补 1；
    * **标签名中英混排**（`冰霜` 与 `bleed` / `physical_damage`）——
      页面就是这样，英文那些在 `<i>` 里，本库存页面原值、不自己翻译。

    只有 PoB 侧没有第二来源：`Data/ModDelve.lua` 是化石的**词缀池**
    （早已入库成 `category='delve'`），不含权重乘数。
    """
    tree = HTMLParser(html)
    table = _find_table(tree, MISC_HEADERS, what="标签权重乘数表")
    if table is None:
        return ()
    out: list[TagWeight] = []
    for row in table.css("tbody tr"):
        cells = row.css("td")
        if len(cells) != len(MISC_HEADERS):
            continue
        kind, name = _text(cells[0]), _text(cells[1])
        if not kind or not name:
            continue
        pairs = _parse_tag_cell(cells[2])
        if not pairs:
            raise HarvestStructureError(
                f"{kind}「{name}」的 Tags 格里解析不出任何标签（{page_url}）"
            )
        for tag, mult in pairs:
            out.append(
                TagWeight(kind=kind, name_zh=name, tag=tag, multiplier=mult)
            )
    return tuple(out)
