"""Render requested unique-item fields from the stored source record."""
from __future__ import annotations

import json
import sqlite3

from orchestrator.binding import Evidence
from orchestrator.question import entity_question, without_style


def render_entity_answer(
    conn: sqlite3.Connection, question: str, evidence: list[Evidence],
) -> str:
    if len(evidence) != 1 or not entity_question(question):
        return ""
    e = evidence[0]
    if not e.can_support_facts or not (e.entity_id or "").startswith("unique_item:"):
        return ""
    row = conn.execute(
        "SELECT name_zh, name_en, base_type, attributes FROM entity WHERE entity_id=?",
        (e.entity_id,),
    ).fetchone()
    if not row:
        return ""
    attrs = json.loads(row["attributes"])
    question = without_style(question)
    marker = f"[[{e.label}]]"
    name = row["name_zh"] or row["name_en"]
    definition = any(w in question for w in ("是什么", "是啥", "基础信息"))
    acquisition = any(w in question for w in ("怎么获得", "获取途径"))
    mods = attrs.get("mods") or []
    lines = []
    if definition:
        lines.append(f"**{name}**：唯一装备，底材为{row['base_type']}{marker}。")
    if definition or any(w in question for w in ("词缀", "属性")):
        selected = [m for m in mods if m.get("kind") in ("implicit", "explicit")]
        kind = "implicit" if "隐式" in question else "explicit" if "显式" in question else ""
        if kind:
            selected = [m for m in selected if m["kind"] == kind]
        # A named stat narrows the result only when it actually matches a field.
        stats = [s for s in ("生命", "力量", "敏捷", "智慧") if s in question]
        if stats:
            selected = [m for m in selected if all(s in m["text"] for s in stats)]
        if not selected:
            return ""
        for mod in selected:
            label = "隐式" if mod["kind"] == "implicit" else "显式"
            lines.append(f"- {label}：{mod['text']}{marker}。")
        # Reading a single field's range endpoint does not need an LLM or formula.
        if len(selected) == 1 and any(w in question for w in ("最多", "最高", "最大值")):
            ranges = selected[0].get("ranges") or []
            if len(ranges) == 1 and len(ranges[0]) == 2:
                maximum = max(float(v) for v in ranges[0])
                lines.insert(0, f"该词缀的上限为 **{maximum:g}**{marker}。")
    if acquisition:
        routes = attrs.get("acquisition") or []
        if not routes:
            return ""
        lines.extend(
            f"- {route['source_zh']}"
            + (f"：{route['ingredient']}" if route.get("ingredient") else "（命运卡）")
            + marker + "。"
            for route in routes if route.get("source_zh")
        )
        lines.append("以上是已收录的命运卡或升级配方；具体掉落地点与概率尚无依据。")
    return "\n".join(lines)
