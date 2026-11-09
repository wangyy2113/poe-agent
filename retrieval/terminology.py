"""Search-only terminology aliases; never infer an entity or game rule."""
from __future__ import annotations

import re


# Match the complete action/object phrase. Bare 偷取 is also life/mana leech
# and must not be rewritten. Triggers, rarity, duration and item class still
# come from the question and retrieved source, not this search vocabulary.
_MOD_STEAL = re.compile(
    r"(?<!生命)(?<!魔力)(?<!护盾)(?:(?:偷取|窃取)(?:怪物的?|的)?词缀|词缀(?:偷取|窃取))"
)


def search_query(question: str) -> str:
    """Keep surrounding constraints, including negation, in the search text."""
    return _MOD_STEAL.sub("获得怪物属性", question)
