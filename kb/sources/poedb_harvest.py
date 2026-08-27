"""POEDB 收获（古灵庄园）定向工艺抽取器。

本模块只结构化页面明确列出的两类事实：**定向工艺的效果文本**与**原始命能费用**。
它不解释这些选项如何改变装备状态，也不据此判断某件装备当前能否执行该工艺——
与 `poedb_crafting.py`（工艺工作台）同一条边界。

## 这一页有三张表，只取第一张

`/cn/Horticrafting` 实测（2026-08-27）：

| 表 | 行数 | 内容 | 本模块 |
| --- | --- | --- | --- |
| 0 | 74 | `描述 ｜ 消耗` —— 定向工艺目录 | **取** |
| 1 | 51 | `怪物 ｜ 阶级 ｜ Colour` —— 作物与掉落来源 | 不取，调用方登记条数 |
| 2 | 41 | `Type ｜ Name ｜ Tags` —— 催化 10 / 化石 18 / 工艺台 2 / 花园工艺站 11 | 不取，粒度混杂 |

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
from urllib.parse import unquote, urljoin, urlparse

from selectolax.parser import HTMLParser, Node

HORTICRAFTING_URL = "https://poedb.tw/cn/Horticrafting"

#: 定向工艺主表的表头。**按它定位表**，不按下标。
EXPECTED_HEADERS = ("描述", "消耗")

#: 另外两张表的表头，用来报「本模块不取的有多少行」——静默省略与
#: 「页面上就没有」事后分不清。
CROP_HEADERS = ("怪物", "阶级", "Colour")
MISC_HEADERS = ("Type", "Name", "Tags")

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


def _extract_costs(cell: Node, *, row_number: int, page_url: str) -> tuple[HarvestCost, ...]:
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
        if meta is None:
            # 元数据路径是与 poedb_currency 对账的键，缺了就没法核对
            raise HarvestStructureError(
                f"第 {row_number} 行通货 {name!r} 的 data-hover 里没有元数据路径"
            )
        pending = {
            "metadata_id": meta.group(1),
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
    html: str, *, page_url: str = HORTICRAFTING_URL
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
        costs = _extract_costs(cells[1], row_number=row_number, page_url=page_url)
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

    for headers, label in ((CROP_HEADERS, "作物与掉落来源"), (MISC_HEADERS, "相关物品")):
        other = _find_table(tree, headers, what=label)
        if other is not None:
            stats.not_taken[label] = sum(1 for tr in other.css("tr") if tr.css("td"))
    return crafts, stats
