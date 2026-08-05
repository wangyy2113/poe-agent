"""引用装配（任务 3.4）。

引用由**代码**统一渲染，不让模型自己写。模型只需在句末标 `[[E1]]`，
来源名、条目名与 URL 由这里按证据编号回填。这样引用不可能被编造，
格式也不会因为模型心情而变。

底部来源列表只列**实际被引用到**的证据（`used_labels`）——把检索到但没用上的
也列出来，会让答案看起来依据比实际更充分。
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.binding import Evidence


@dataclass(frozen=True)
class RenderedCitation:
    label: str
    source: str
    title: str
    url: str
    priority: int

    def render(self) -> str:
        """「来源：POEDB → 猎首（Headhunter）」+ URL（需求 15.6 指定格式）。"""
        return f"[{self.label}] 来源：{self.source} → {self.title}\n    {self.url}"


def collect(evidence: list[Evidence], used_labels: list[str]) -> list[RenderedCitation]:
    by_label = {e.label: e for e in evidence}
    out: list[RenderedCitation] = []
    for lb in used_labels:
        e = by_label.get(lb)
        if e is None:
            continue
        out.append(
            RenderedCitation(
                label=e.label,
                source=e.source,
                title=e.title,
                url=e.url,
                priority=e.source_priority,
            )
        )
    return out


def render_block(citations: list[RenderedCitation]) -> str:
    if not citations:
        return ""
    lines = "\n".join(c.render() for c in citations)
    return f"\n\n---\n**引用**\n{lines}"


def conflict_block(conflicts: list[dict]) -> str:
    """同实体跨来源数值冲突的并列对照（需求 1.9、15.5）。

    不自行判定谁对：并列展示双方并说明当前更推荐 POEDB。隐藏任何一方都是
    替用户做了他没授权的判断。
    """
    if not conflicts:
        return ""
    parts = ["\n\n---\n**来源冲突（未自行判定，请自行核对）**"]
    for c in conflicts:
        parts.append(
            f"\n- {c.get('subject', '')}\n"
            f"  - {c.get('source_a')}：{c.get('value_a')}\n"
            f"  - {c.get('source_b')}：{c.get('value_b')}\n"
            f"  - 当前更推荐采用 POEDB"
        )
    return "\n".join(parts)
