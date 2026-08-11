"""POEDB 工艺工作台目录抽取器。

本模块只结构化页面明确列出的四类事实：效果文本、原始通货费用、适用
ItemClasses 与解锁信息。它不解释这些选项如何改变装备状态，也不据此判断某件
装备当前能否执行该工艺。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser, Node

from kb.sources.poedb_mods import clean_mod_text

CRAFTING_BENCH_URL = "https://poedb.tw/cn/Crafting_Bench"
EXPECTED_HEADERS = ("词缀", "Require", "ItemClasses", "解锁")
_AMOUNT_RE = re.compile(r"^\s*(\d+)\s*x\s*$", re.I)


class CraftingBenchStructureError(ValueError):
    """页面结构或关键字段不完整；调用方必须拒绝整批写入。"""


@dataclass(frozen=True)
class BenchCost:
    position: int
    amount: int
    currency_slug: str
    currency_name_zh: str
    currency_url: str

    def canonical(self) -> dict[str, object]:
        return {
            "position": self.position,
            "amount": self.amount,
            "currency_slug": self.currency_slug,
            "currency_name_zh": self.currency_name_zh,
            "currency_url": self.currency_url,
        }


@dataclass(frozen=True)
class BenchRecipe:
    effect_text: str
    item_classes: tuple[str, ...]
    costs: tuple[BenchCost, ...]
    unlock_text: str
    unlock_url: str | None
    unlock_hint: str | None
    url: str
    recipe_id: str = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        payload = json.dumps(
            {
                "effect_text": self.effect_text,
                "item_classes": list(self.item_classes),
                "costs": [cost.canonical() for cost in self.costs],
                "unlock_text": self.unlock_text,
                "unlock_url": self.unlock_url,
                "unlock_hint": self.unlock_hint,
                "url": self.url,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        object.__setattr__(self, "recipe_id", f"bench:{digest}")
        object.__setattr__(self, "content_hash", digest)


@dataclass
class BenchExtractStats:
    total: int = 0
    succeeded: int = 0
    costs: int = 0
    multi_cost_recipes: int = 0
    currencies: set[str] = field(default_factory=set)


def _text(node: Node) -> str:
    return " ".join(node.text(deep=True, separator=" ").split())


def _find_recipe_table(tree: HTMLParser) -> Node:
    matches: list[Node] = []
    observed: list[tuple[str, ...]] = []
    for table in tree.css("table"):
        headers = tuple(_text(cell) for cell in table.css("thead th"))
        if headers:
            observed.append(headers)
        if headers == EXPECTED_HEADERS:
            matches.append(table)
    if len(matches) != 1:
        raise CraftingBenchStructureError(
            "预期恰好一个工艺台主表，实际匹配 "
            f"{len(matches)} 个；观测到的表头：{observed}"
        )
    return matches[0]


def _extract_costs(cell: Node, *, row_number: int, page_url: str) -> tuple[BenchCost, ...]:
    costs: list[BenchCost] = []
    pending_amount: int | None = None
    currency_anchors = 0

    for node in cell.iter(include_text=True):
        if node.tag == "-text":
            raw = node.text(deep=False)
            if not raw.strip():
                continue
            match = _AMOUNT_RE.fullmatch(raw)
            if match is None:
                raise CraftingBenchStructureError(
                    f"第 {row_number} 行费用数量格式无法识别：{raw!r}"
                )
            if pending_amount is not None:
                raise CraftingBenchStructureError(
                    f"第 {row_number} 行出现连续费用数量但没有对应通货"
                )
            pending_amount = int(match.group(1))
            if pending_amount <= 0:
                raise CraftingBenchStructureError(f"第 {row_number} 行费用必须大于 0")
            continue

        if node.tag != "a":
            continue
        classes = set((node.attributes.get("class") or "").split())
        if "item_currency" not in classes:
            raise CraftingBenchStructureError(
                f"第 {row_number} 行费用单元格含非通货链接"
            )
        currency_anchors += 1
        if pending_amount is None:
            raise CraftingBenchStructureError(
                f"第 {row_number} 行通货 {node.text(deep=True)!r} 缺少数量"
            )

        href = (node.attributes.get("href") or "").strip()
        name = _text(node)
        currency_url = urljoin(page_url, href)
        slug = urlparse(currency_url).path.rstrip("/").rsplit("/", 1)[-1]
        if not href or not slug or not name or not currency_url.startswith("https://"):
            raise CraftingBenchStructureError(
                f"第 {row_number} 行通货字段不完整：href={href!r}, name={name!r}"
            )
        costs.append(
            BenchCost(
                position=len(costs),
                amount=pending_amount,
                currency_slug=slug,
                currency_name_zh=name,
                currency_url=currency_url,
            )
        )
        pending_amount = None

    if pending_amount is not None:
        raise CraftingBenchStructureError(f"第 {row_number} 行末尾费用数量没有对应通货")
    if not costs or len(costs) != currency_anchors:
        raise CraftingBenchStructureError(f"第 {row_number} 行没有完整费用")
    return tuple(costs)


def _extract_unlock(cell: Node, *, row_number: int, page_url: str) -> tuple[str, str | None, str | None]:
    unlock_text = _text(cell)
    if not unlock_text:
        raise CraftingBenchStructureError(f"第 {row_number} 行解锁信息为空")

    anchors = cell.css("a")
    if len(anchors) > 1:
        raise CraftingBenchStructureError(f"第 {row_number} 行解锁信息含多个链接")
    unlock_url: str | None = None
    if anchors:
        href = (anchors[0].attributes.get("href") or "").strip()
        unlock_url = urljoin(page_url, href)
        if not href or not unlock_url.startswith("https://"):
            raise CraftingBenchStructureError(f"第 {row_number} 行解锁链接无效")

    hints: list[str] = []
    for node in cell.css("[title]"):
        hint = " ".join((node.attributes.get("title") or "").split())
        if hint and hint not in hints:
            hints.append(hint)
    return unlock_text, unlock_url, " / ".join(hints) or None


def extract_bench_recipes(
    html: str, page_url: str = CRAFTING_BENCH_URL
) -> tuple[list[BenchRecipe], BenchExtractStats]:
    """抽取主表；任一行缺关键字段即抛错，调用方不得部分写入。"""
    if not page_url.startswith("https://"):
        raise CraftingBenchStructureError("来源 URL 必须是 HTTPS")

    tree = HTMLParser(html)
    table = _find_recipe_table(tree)
    rows = table.css("tbody tr")
    if not rows:
        raise CraftingBenchStructureError("工艺台主表没有数据行")

    stats = BenchExtractStats(total=len(rows))
    recipes: list[BenchRecipe] = []
    seen_ids: set[str] = set()
    for row_number, row in enumerate(rows, start=1):
        cells = row.css("td")
        if len(cells) != 4:
            raise CraftingBenchStructureError(
                f"第 {row_number} 行预期 4 列，实际 {len(cells)} 列"
            )

        effect_text = clean_mod_text(cells[0].html)
        item_classes = tuple(
            part.strip() for part in _text(cells[2]).split("·") if part.strip()
        )
        if not effect_text:
            raise CraftingBenchStructureError(f"第 {row_number} 行效果文本为空")
        if not item_classes:
            raise CraftingBenchStructureError(f"第 {row_number} 行适用 ItemClasses 为空")

        costs = _extract_costs(cells[1], row_number=row_number, page_url=page_url)
        unlock_text, unlock_url, unlock_hint = _extract_unlock(
            cells[3], row_number=row_number, page_url=page_url
        )
        recipe = BenchRecipe(
            effect_text=effect_text,
            item_classes=item_classes,
            costs=costs,
            unlock_text=unlock_text,
            unlock_url=unlock_url,
            unlock_hint=unlock_hint,
            url=page_url,
        )
        if recipe.recipe_id in seen_ids:
            raise CraftingBenchStructureError(
                f"第 {row_number} 行与先前记录完整重复，拒绝静默去重"
            )
        seen_ids.add(recipe.recipe_id)
        recipes.append(recipe)
        stats.succeeded += 1
        stats.costs += len(costs)
        stats.multi_cost_recipes += int(len(costs) > 1)
        stats.currencies.update(cost.currency_slug for cost in costs)

    return recipes, stats
