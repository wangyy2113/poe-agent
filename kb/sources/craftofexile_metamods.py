"""Craft of Exile 元词缀交互矩阵抽取器（经 POEDB 镜像采集）。

本模块只保存页面矩阵逐格明确给出的结果与原始注释。空白单元格不是
``N/A``，也不是可推断的 ``unknown``：它们只进入抽取审计，不形成事实记录。
矩阵不提供给定装备可执行性、完整状态转移、步骤、概率、成本或路线。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser, Node

METAMOD_SOURCE_URL = "https://www.craftofexile.com/metamods"
METAMOD_MIRROR_URL = "https://poedb.tw/cn/metamods"
EXPECTED_GUARDS = (
    "前缀无法被变更",
    "后缀无法被变更",
    "无法骰出攻击词缀",
    "无法骰出法术词缀",
)
OUTCOME_BY_RAW_VALUE = {
    "YES": "yes",
    "NO": "no",
    "N/A": "not_applicable",
    "YES & NO": "conditional",
}
VERSION_SCOPE_UNKNOWN = "unknown"


class MetamodMatrixStructureError(ValueError):
    """页面结构或非空单元格无法无损识别；调用方必须拒绝整批写入。"""


def _text(node: Node) -> str:
    return " ".join(node.text(deep=True, separator=" ").split())


@dataclass(frozen=True)
class BlankMetamodCell:
    """源页面明确留空的格子；只用于审计，绝不能成为 Citation。"""

    section: str
    operation_text: str
    guard_text: str
    row_order: int
    column_order: int

    def canonical(self) -> dict[str, object]:
        return {
            "section": self.section,
            "operation_text": self.operation_text,
            "guard_text": self.guard_text,
            "row_order": self.row_order,
            "column_order": self.column_order,
        }


@dataclass(frozen=True)
class MetamodInteraction:
    section: str
    operation_text: str
    guard_text: str
    outcome: str
    raw_value: str
    note_text: str | None
    row_order: int
    column_order: int
    source_url: str = METAMOD_SOURCE_URL
    mirror_url: str = METAMOD_MIRROR_URL
    version_scope: str = VERSION_SCOPE_UNKNOWN
    interaction_id: str = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.source_url != METAMOD_SOURCE_URL:
            raise MetamodMatrixStructureError("原始来源 URL 必须固定为 Craft of Exile")
        if self.mirror_url != METAMOD_MIRROR_URL:
            raise MetamodMatrixStructureError("镜像 URL 必须固定为 POEDB metamods 页面")
        if self.version_scope != VERSION_SCOPE_UNKNOWN:
            raise MetamodMatrixStructureError("页面无版本字段，version_scope 必须为 unknown")
        if OUTCOME_BY_RAW_VALUE.get(self.raw_value) != self.outcome:
            raise MetamodMatrixStructureError("raw_value 与 outcome 映射不一致")
        if self.outcome == "conditional" and not self.note_text:
            raise MetamodMatrixStructureError("conditional 单元格必须保留原始条件注释")
        if self.row_order < 0 or not 0 <= self.column_order < len(EXPECTED_GUARDS):
            raise MetamodMatrixStructureError("矩阵位置字段越界")

        identity = json.dumps(
            {
                "operation_text": self.operation_text,
                "guard_text": self.guard_text,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload = json.dumps(
            {
                "operation_text": self.operation_text,
                "guard_text": self.guard_text,
                "outcome": self.outcome,
                "raw_value": self.raw_value,
                "note_text": self.note_text,
                "source_url": self.source_url,
                "mirror_url": self.mirror_url,
                "version_scope": self.version_scope,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        object.__setattr__(
            self,
            "interaction_id",
            "metamod:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        )
        object.__setattr__(
            self,
            "content_hash",
            hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )


@dataclass
class MetamodExtractStats:
    sections: int = 0
    operation_rows: int = 0
    total_cells: int = 0
    explicit_cells: int = 0
    blank_cells: list[BlankMetamodCell] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=dict)


def _extract_note(cell: Node) -> str | None:
    notes: list[str] = []
    for node in cell.css("small, span.small"):
        value = _text(node)
        if value and value not in notes:
            notes.append(value)
    return " | ".join(notes) or None


def extract_metamod_interactions(
    html: str,
    mirror_url: str = METAMOD_MIRROR_URL,
) -> tuple[list[MetamodInteraction], MetamodExtractStats]:
    """抽取所有有明确结果的格子；空白格只记审计，未知非空值整批拒绝。"""
    if mirror_url != METAMOD_MIRROR_URL:
        raise MetamodMatrixStructureError(
            "镜像 URL 必须固定为 https://poedb.tw/cn/metamods"
        )

    tree = HTMLParser(html)
    roots = tree.css("#markContent")
    if len(roots) != 1:
        raise MetamodMatrixStructureError(
            f"预期恰好一个 #markContent，实际 {len(roots)} 个"
        )
    root = roots[0]
    source_links = [
        (anchor.attributes.get("href") or "").rstrip("/")
        for anchor in root.css("a")
    ]
    if METAMOD_SOURCE_URL.rstrip("/") not in source_links:
        raise MetamodMatrixStructureError("页面缺少 Craft of Exile 原始来源链接")
    warnings = [_text(node) for node in root.css("blockquote")]
    if not any("可能并不总是与游戏的当前状态一致" in value for value in warnings):
        raise MetamodMatrixStructureError("页面缺少当前适用性警告，拒绝扩大证据范围")

    tables = root.css("table")
    if len(tables) != 1:
        raise MetamodMatrixStructureError(
            f"预期恰好一个交互矩阵，实际 {len(tables)} 个"
        )

    stats = MetamodExtractStats()
    interactions: list[MetamodInteraction] = []
    seen_sections: set[str] = set()
    seen_operations: set[str] = set()
    seen_ids: set[str] = set()
    outcome_counts = {value: 0 for value in OUTCOME_BY_RAW_VALUE.values()}
    section = ""
    guards: tuple[str, ...] = ()
    row_order = 0

    rows = tables[0].css("tr")
    if not rows:
        raise MetamodMatrixStructureError("交互矩阵没有行")
    for source_row, row in enumerate(rows, start=1):
        cells = row.css("td")
        if len(cells) != 5:
            raise MetamodMatrixStructureError(
                f"源表第 {source_row} 行预期 5 列，实际 {len(cells)} 列"
            )
        classes = set((row.attributes.get("class") or "").split())
        if "header" in classes:
            section = _text(cells[0])
            guards = tuple(_text(cell) for cell in cells[1:])
            if not section:
                raise MetamodMatrixStructureError(f"源表第 {source_row} 行分区名为空")
            if section in seen_sections:
                raise MetamodMatrixStructureError(f"分区 {section!r} 重复")
            if guards != EXPECTED_GUARDS:
                raise MetamodMatrixStructureError(
                    f"分区 {section!r} 的 guard 列发生变化：{guards!r}"
                )
            seen_sections.add(section)
            stats.sections += 1
            continue

        if not section or not guards:
            raise MetamodMatrixStructureError("数据行出现在首个分区表头之前")
        operation_text = _text(cells[0])
        if not operation_text:
            raise MetamodMatrixStructureError(f"源表第 {source_row} 行操作名为空")
        if operation_text in seen_operations:
            raise MetamodMatrixStructureError(
                f"操作名 {operation_text!r} 重复，operation + guard 已不再唯一"
            )
        seen_operations.add(operation_text)
        stats.operation_rows += 1

        for column_order, guard_text in enumerate(guards):
            stats.total_cells += 1
            cell = cells[column_order + 1]
            cell_text = _text(cell)
            if not cell_text:
                stats.blank_cells.append(
                    BlankMetamodCell(
                        section=section,
                        operation_text=operation_text,
                        guard_text=guard_text,
                        row_order=row_order,
                        column_order=column_order,
                    )
                )
                continue

            token_node = cell.css_first("span")
            raw_value = _text(token_node) if token_node is not None else ""
            outcome = OUTCOME_BY_RAW_VALUE.get(raw_value)
            if outcome is None:
                raise MetamodMatrixStructureError(
                    f"{operation_text!r} × {guard_text!r} 的非空结果无法识别："
                    f"{cell_text!r}"
                )
            note_text = _extract_note(cell)
            if outcome == "conditional" and not note_text:
                raise MetamodMatrixStructureError(
                    f"{operation_text!r} × {guard_text!r} 是 YES & NO 但缺少条件注释"
                )

            interaction = MetamodInteraction(
                section=section,
                operation_text=operation_text,
                guard_text=guard_text,
                outcome=outcome,
                raw_value=raw_value,
                note_text=note_text,
                row_order=row_order,
                column_order=column_order,
                mirror_url=mirror_url,
            )
            if interaction.interaction_id in seen_ids:
                raise MetamodMatrixStructureError(
                    f"交互键重复：{operation_text!r} × {guard_text!r}"
                )
            seen_ids.add(interaction.interaction_id)
            interactions.append(interaction)
            stats.explicit_cells += 1
            outcome_counts[outcome] += 1
        row_order += 1

    if not seen_sections or not interactions:
        raise MetamodMatrixStructureError("交互矩阵没有可引用的明确结果")
    stats.outcomes = {key: value for key, value in outcome_counts.items() if value}
    return interactions, stats
