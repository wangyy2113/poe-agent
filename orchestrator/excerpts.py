"""Select source passages; never ask a model to rewrite unstructured facts."""
from __future__ import annotations

import json
from dataclasses import dataclass

from orchestrator.binding import Evidence


@dataclass(frozen=True)
class Passage:
    id: str
    evidence: Evidence
    heading: str
    text: str


def passages(evidence: list[Evidence]) -> list[Passage]:
    result = []
    for item in evidence:
        if not item.can_support_facts:
            continue
        heading = item.title
        table: list[str] = []

        def add(text: str) -> None:
            result.append(Passage(f"P{len(result) + 1}", item, heading, text))

        for raw in [*item.text.splitlines(), ""]:
            line = raw.strip()
            if line.startswith("来源："):
                if table:
                    add("\n".join(table))
                    table = []
                break
            if line.startswith("|"):
                table.append(raw)
                continue
            if table:
                add("\n".join(table))
                table = []
            if not line or line.startswith("http"):
                continue
            if line.startswith("#") or (line.startswith("**") and line.endswith("**")):
                heading = line.strip("#* ")
            else:
                add(raw)
    return result


def render_selection(raw: str, items: list[Passage]) -> tuple[str, list[str]]:
    """The model can only select existing IDs; other output is never displayed."""
    try:
        selected = json.loads(raw)
    except (TypeError, ValueError):
        return "", []
    by_id = {p.id: p for p in items}
    if (not isinstance(selected, list) or not selected or len(selected) > 8
            or any(not isinstance(key, str) or key not in by_id for key in selected)):
        return "", []
    parts, labels = [], []
    heading = None
    for key in dict.fromkeys(selected):
        p = by_id[key]
        group = (p.evidence.title, p.heading)
        if group != heading:
            title = p.evidence.title if p.heading == p.evidence.title else f"{p.evidence.title} · {p.heading}"
            parts.append(f"**{title}**")
            heading = group
        parts.append(f"{p.text}\n\n[[{p.evidence.label}]]")
        if p.evidence.label not in labels:
            labels.append(p.evidence.label)
    return "\n\n".join(parts), labels


def source_quotes(evidence: list[Evidence]) -> dict[str, Passage]:
    """Stable source block IDs; appending evidence never changes earlier IDs."""
    return {
        f"Q{item.label[1:]}.{index}": passage
        for item in evidence if item.claim_scope == "excerpt"
        for index, passage in enumerate(passages([item]), 1)
    }


def expand_quotes(raw: str, evidence: list[Evidence]) -> str:
    """Restore selected whole source blocks before the existing binding gate."""
    import re

    quotes = source_quotes(evidence)

    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in quotes:
            return "（所选资料段落不存在，无法核实。）"
        passage = quotes[key]
        return f"\n\n{passage.text}[[{passage.evidence.label}]]\n\n"

    return re.sub(r"\[\[(Q\d+\.\d+)\]\]", replace, raw)
