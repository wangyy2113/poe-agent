"""Resolve presentation requests and short follow-ups before retrieval."""
from __future__ import annotations

import re
import sqlite3

from retrieval.alias import lookup

_STYLE = re.compile(
    r"[，,。？！?!；;\s]*(?:请|麻烦)?(?:简短说明|简要说明|简短回答|简单说说|"
    r"用中文回答并附来源|用中文回答|附上来源|附来源)[。.!！\s]*$"
)
_PRONOUN = re.compile(r"^(它们|这些|这两件|两者|它|这个|这件|该装备|该技能)")
_MULTIPLE = ("和", "与", "以及", "跟", "对比", "比较", "区别", "相比", "还是")
ENTITY_FIELDS = ("是什么", "是啥", "有哪些词缀", "有什么词缀", "隐式", "显式", "属性", "基础信息", "怎么获得", "获取途径")


def without_style(question: str) -> str:
    previous = None
    while previous != question:
        previous, question = question, _STYLE.sub("", question).strip()
    return question


def entity_question(question: str) -> bool:
    question = without_style(question)
    return any(term in question for term in ENTITY_FIELDS) and not any(
        term in question for term in (*_MULTIPLE, "能否", "能不能", "价格", "制作", "历史", "旧版", "档", "阶", "权重", "能造成", "组合")
    )


def resolve_followup(
    conn: sqlite3.Connection, question: str, conversation_id: str | None,
    owner_id: str | None,
) -> tuple[str, str]:
    """Only inherit a unique subject from the most recent owned user message.

    Never use assistant output as evidence or search across another owner's chats.
    Ambiguity becomes a clarification, not a guessed subject.
    """
    marker = _PRONOUN.match(question)
    if not marker or not conversation_id or not owner_id:
        return question, ""
    rows = conn.execute(
        "SELECT m.content FROM message m JOIN conversation c USING(conversation_id) "
        "WHERE c.conversation_id=? AND c.owner_id=? AND m.role='user' "
        "ORDER BY m.created_at DESC, m.rowid DESC LIMIT 5",
        (conversation_id, owner_id),
    ).fetchall()
    for row in rows:
        previous = str(row["content"])
        # Streaming saves the current question before calling the orchestrator.
        if previous == question or _PRONOUN.match(previous):
            continue
        ids = lookup(conn, previous)
        if len(ids) != 1 or marker.group() in ("它们", "这些", "这两件", "两者"):
            break
        entity = conn.execute(
            "SELECT name_zh, name_en FROM entity WHERE entity_id=?", (ids[0],)
        ).fetchone()
        if entity:
            return (str(entity["name_zh"] or entity["name_en"])
                    + question[marker.end():]), ""
        break
    return question, "请补充你指的是哪件装备或哪个技能，我再按对应资料回答。"


def build_clarification(question: str) -> str:
    if re.search(r"(?:推荐|给我|一套|规划).*(?:开荒|BD|bd|构筑)|(?:开荒|BD|bd|构筑).*推荐", question):
        return (
            "要给出可核对的构筑方案，请先提供赛季、职业/升华、当前等级和目标内容；"
            "已有角色可直接导入 PoB。\n\n"
            "我可以据此检查技能搭配、防御缺口与升级顺序。预算需要结合对应赛季的"
            "实时价格核算，不能只凭技能和预算金额给出一套已验证的装备清单。"
        )
    return ""


def unknown_named_item(question: str) -> bool:
    if "装备" not in question or not entity_question(question):
        return False
    names = re.findall(r"[「『“\"]([^」』”\"]+)[」』”\"]", question)
    if not names:
        return False
    from core.db import get_conn
    return not any(lookup(get_conn(), name) for name in names)
