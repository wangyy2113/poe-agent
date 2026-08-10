"""结构化词缀查询工具，以及知识库自描述工具。

## 为什么要有这些工具

先前 Agent 只有 `kb_search` / `entity_lookup` 两个工具，**两个都只返回卡片文本**。
于是 93,875 条 `mod_affix` 记录里的 T 级、物品等级、生成权重、互斥关系，模型只能
靠 BM25 命中某张卡片再从表格里读——本来一条 SQL 就能精确回答的问题，变成了猜。
实测过这条路径的代价：查「弓上的火焰伤害前缀」曾经命中爪的词缀。

这里补的是**精确层的对外契约**：能算准的就别让模型猜。

## 一条硬约束：证据文本必须是渲染后的表格，不能是裸 JSON

出口闸门（`orchestrator/binding.py`）校验「事实句里的每个数值都要能在它绑定的
证据原文里找到」。若 `citations[].text` 塞的是 JSON，数值虽然在里面，但闸门做的是
子串与区间匹配，JSON 的转义与键名会干扰判断；更重要的是模型转述表格比转述 JSON
准确得多。所以这里统一渲染成 Markdown 表格再交出去。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from core import db as dbmod
from entities.taxonomy import MAX_FACTUAL_PRIORITY, Source
from tools.registry import REGISTRY, ToolResult

logger = logging.getLogger(__name__)

#: 单次返回的梯度组数上限。一个词缀在一个底材类别上最多横跨 10 个来源，
#: 全给会挤爆上下文，也会让模型倾向于把弱相关的也绑上去。
MAX_LADDERS = 6
#: 单条证据文本上限，与 kb_search 保持一致
MAX_EVIDENCE_CHARS = 1800


# ── 底材类别解析 ──────────────────────────────────────────────────

def _resolve_pages(conn: sqlite3.Connection, item_class: str) -> tuple[list[str], str]:
    """把用户说的类别名解析成 POEDB 页面 slug 列表。

    为什么要返回列表：POEDB 把护甲按属性需求拆成多页（「胸甲」对应 7 个
    `Body_Armours_*`），而玩家只会说「胸甲」。这些页面的词缀池**确有差异**，
    因此不能任选一个，必须全查并在输出里说明覆盖了哪几页。
    """
    q = (item_class or "").strip()
    if not q:
        return [], "未指定底材类别"

    # 1) 精确命中页面 slug
    rows = conn.execute(
        "SELECT DISTINCT item_class_page FROM mod_affix WHERE item_class_page = ?", (q,)
    ).fetchall()
    if rows:
        return [r[0] for r in rows], f"页面 {q}"

    # 2) 精确命中中文类别名
    rows = conn.execute(
        "SELECT DISTINCT item_class_page FROM mod_affix WHERE item_class_zh = ? "
        "ORDER BY item_class_page",
        (q,),
    ).fetchall()
    if rows:
        pages = [r[0] for r in rows]
        note = f"类别「{q}」对应 {len(pages)} 个 POEDB 页面" if len(pages) > 1 else f"类别「{q}」"
        return pages, note

    # 3) 模糊
    rows = conn.execute(
        "SELECT DISTINCT item_class_page, item_class_zh FROM mod_affix "
        "WHERE item_class_zh LIKE ? OR item_class_page LIKE ? ORDER BY item_class_page",
        (f"%{q}%", f"%{q}%"),
    ).fetchall()
    if rows:
        pages = [r[0] for r in rows]
        names = sorted({r[1] for r in rows if r[1]})
        return pages, f"模糊匹配「{q}」→ {'、'.join(names[:4])}（{len(pages)} 个页面）"

    return [], f"知识库里没有底材类别「{q}」"


def _known_classes(conn: sqlite3.Connection, limit: int = 24) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT item_class_zh FROM mod_affix WHERE item_class_zh <> '' "
            "GROUP BY item_class_zh ORDER BY count(*) DESC LIMIT ?",
            (limit,),
        )
    ]


# ── 渲染 ──────────────────────────────────────────────────────────

def _confidence_line(rows: list[sqlite3.Row]) -> str:
    """这一组梯度的置信度说明。**逐组现算**，不是全库比例。

    为什么必须有：36.9% 的词缀没有生成权重，这些词缀算不出成功率。若不在证据里
    写明，模型会把「权重列是空的」当成「权重是 0」，进而给出错误的概率。
    """
    bits: list[str] = []
    no_weight = sum(1 for r in rows if not (r["weight"] or 0))
    if no_weight == len(rows):
        bits.append("**本组无生成权重数据 → 不能给出成功率或概率**")
    elif no_weight:
        bits.append(f"其中 {no_weight}/{len(rows)} 条无生成权重，这部分算不出概率")
    if any((r["tier_source"] or "") == "derived" for r in rows):
        bits.append("T 级按 POEDB 前端算法推导（已通过不变量校验），非页面原文直接标注")
    if all(r["ranges"] == "[]" for r in rows):
        bits.append("固定值词缀，无数值区间")
    return "；".join(bits)


#: 单组最多列几行。超出时**明确写出还剩多少**——证据在出口按字数截断，
#: 若不声明，模型会把截断处当成列表结尾，进而说「一共 20 档」。
MAX_ROWS_PER_LADDER = 24


def _render_ladder(head: str, url: str, rows: list[sqlite3.Row]) -> str:
    """渲染一组 T 级梯度。数值全部原文照抄，不做任何换算。"""
    out = [f"### {head}", ""]
    # 同一词缀组里可能是几百条**平级**词缀而非阶梯（全为 T1/1），
    # 用「条」而不是「档」，避免暗示存在 T1..TN 的阶梯
    unit = "档" if max((r["tier_count"] or 0) for r in rows) > 1 else "条"
    out.append(f"共 {len(rows)} {unit}。")
    out.append("")
    out.append("| T 级 | 词缀名 | 词缀文本 | 物品等级 | 生成权重 |")
    out.append("| --- | --- | --- | --- | --- |")
    for r in rows[:MAX_ROWS_PER_LADDER]:
        tier = f"T{r['tier']}/{r['tier_count']}" if r["tier_count"] else f"T{r['tier']}"
        w = r["weight"] or 0
        weight = f"{w}（{r['weight_pct'] or 0:.3f}%）" if w else "无数据"
        out.append(
            f"| {tier} | {(r['name_zh'] or '—').strip() or '—'} | {r['text_zh']} "
            f"| {r['ilvl']} | {weight} |"
        )
    if len(rows) > MAX_ROWS_PER_LADDER:
        out.append("")
        out.append(
            f"（本组共 {len(rows)} 条，以上仅列前 {MAX_ROWS_PER_LADDER} 条；"
            "缩小 mod_query 范围或指定 source 可拿到更聚焦的结果）"
        )
    conf = _confidence_line(rows)
    if conf:
        out.append("")
        out.append(f"置信度：{conf}")
    out.append("")
    out.append(f"来源：POEDB {url}")
    return "\n".join(out)


def _citation(cid: str, title: str, url: str, text: str) -> dict[str, Any]:
    """结构化查询结果 → 引用。

    `url` 指向真正的 POEDB 数据页，用户可自行核对——这是「答案可溯源」的实质，
    不是给引用列表凑个链接。
    """
    return {
        "chunk_id": cid,
        "entity_id": None,
        "title": title,
        "url": url,
        "source": Source.POEDB.value,
        "source_priority": 1,
        "can_support_facts": 1 <= MAX_FACTUAL_PRIORITY,
        "text": text[:MAX_EVIDENCE_CHARS],
        "channel": "structured",
    }


# ── 工具 1：精确梯度查询 ──────────────────────────────────────────

@REGISTRY.register(
    name="mod_tiers",
    description=(
        "查某个底材类别上某条词缀的**完整 T 级梯度**（每档的物品等级与生成权重）。"
        "问「几档 / T1 需要多少物品等级 / 权重多少 / 各档数值区间」时用它，"
        "**不要用 kb_search**——这个工具直查结构化词缀库，结果精确；"
        "kb_search 是全文检索，只能命中卡片。\n"
        "返回里带置信度说明：无生成权重的词缀**不得给出成功率**。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "item_class": {
                "type": "string",
                "description": "底材类别，中文名或 POEDB 页面名，如「弓」「胸甲」「Bows」。"
                "护甲类中文名会对应多个页面，工具会全查并说明",
            },
            "mod_query": {
                "type": "string",
                "description": "词缀关键词：词缀文本片段（如「最大生命」「火焰伤害」）、"
                "词缀名（如「幻影之」）或词缀组英文名（如 IncreasedLife）",
            },
            "source": {
                "type": "string",
                "description": "可选，限定来源渠道，如「Base」（常规掉落池）、「精华」、"
                "「工艺工作台」、「隐匿」。不填则列出全部来源",
            },
        },
        "required": ["item_class", "mod_query"],
    },
)
def mod_tiers(item_class: str, mod_query: str, source: str | None = None) -> ToolResult:
    conn = dbmod.get_conn()
    pages, scope = _resolve_pages(conn, item_class)
    if not pages:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": scope,
                "known_classes": _known_classes(conn),
            },
        )

    q = (mod_query or "").strip()
    if not q:
        return ToolResult(ok=False, error="mod_query 不能为空")

    sql = (
        "SELECT * FROM mod_affix WHERE item_class_page IN (%s) "
        "AND (text_zh LIKE ? OR correct_group LIKE ? OR name_zh LIKE ?)"
        % ",".join("?" * len(pages))
    )
    params: list = [*pages, f"%{q}%", f"%{q}%", f"%{q}%"]
    if source:
        sql += " AND (category_label = ? OR category = ?)"
        params += [source, source]
    sql += " ORDER BY item_class_page, category, gen_type, correct_group, tier"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": f"{scope} 上没有匹配「{q}」的词缀"
                          + (f"（来源限定 {source}）" if source else ""),
                "hint": "换用词缀原文片段试试，或去掉 source 限定",
            },
        )

    # 按 (页面, 来源, 前后缀, 词缀组) 分组 —— T 级只在同一组内可比
    groups: dict[tuple, list[sqlite3.Row]] = {}
    for r in rows:
        key = (r["item_class_page"], r["category"], r["gen_type"], r["correct_group"])
        groups.setdefault(key, []).append(r)

    # 常规掉落池优先：玩家没点名来源时问的通常是掉落梯度
    def rank(item: tuple[tuple, list[sqlite3.Row]]) -> tuple:
        (_page, category, _gen, _grp), rs = item
        return ({"normal": 0, "master": 1, "essence": 2}.get(category, 3), -len(rs))

    ordered = sorted(groups.items(), key=rank)
    citations: list[dict[str, Any]] = []
    for (page, category, gen, grp), rs in ordered[:MAX_LADDERS]:
        first = rs[0]
        head = (
            f"{first['item_class_zh'] or page} · {first['gen_label'] or '其他'} · "
            f"{grp or '未分组'}（{first['category_label'] or category}）"
        )
        citations.append(
            _citation(
                cid=f"mod_tiers:{page}:{category}:{gen}:{grp}",
                title=head,
                url=first["url"] or "",
                text=_render_ladder(head, first["url"] or "", rs),
            )
        )

    return ToolResult(
        ok=True,
        data={
            "found": True,
            "scope": scope,
            "pages_searched": pages,
            "ladders_total": len(groups),
            "ladders_returned": len(citations),
            "truncated": len(groups) > len(citations),
        },
        citations=citations,
        meta={"channel": "structured", "table": "mod_affix"},
    )


# ── 工具 2：一条词缀能从哪些渠道获得 ──────────────────────────────

@REGISTRY.register(
    name="stat_sources",
    description=(
        "查某个属性**能从哪些渠道获得**——装备词缀、唯一装备、天赋树节点、升华节点、"
        "Atlas 天赋，逐个渠道分别列出。问「XX 能从哪来 / 有哪些途径 / 怎么获得 / "
        "哪些装备或天赋给 XX」时用它。\n"
        "**返回里必带 coverage_note**，写明哪些能提供属性的渠道尚未采集。回答这类"
        "「全面列举」问题时**必须**转述它：不完整的枚举被当成完整的呈现，本身就是"
        "错误答案。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "stat_query": {
                "type": "string",
                "description": "属性关键词，如「药剂效果」「移动速度」「法术压制」。"
                "用属性原文里的说法，不要用口语缩写",
            },
        },
        "required": ["stat_query"],
    },
)
def stat_sources(stat_query: str) -> ToolResult:
    """遍历渠道注册表。

    渠道清单不写在这里——见 `kb/stat_channels.py`。新增一个属性来源应当只是
    「注册一个渠道」，不该再回来改这个工具；同理，未采集渠道的声明由
    覆盖层派生，这里一个字都不手写。
    """
    from kb import stat_channels  # noqa: PLC0415  延迟导入避免循环

    conn = dbmod.get_conn()
    q = (stat_query or "").strip()
    if not q:
        return ToolResult(ok=False, error="stat_query 不能为空")

    res = stat_channels.scan_all(conn, q)

    if not res.hits:
        return ToolResult(
            ok=True,
            data={
                "found": False,
                "reason": f"已采集的 {len(stat_channels.channels())} 个渠道里，"
                          f"没有属性文本含「{q}」的条目",
                "channels_searched": [c.label_zh for c in stat_channels.channels()],
                "channels_no_match": res.no_match,
                # 用词提示：库里存在相近说法时给出原文，避免把「用词不对」
                # 当成「游戏里没这东西」
                "wording_hint": res.wording_hint,
                "coverage_note": res.coverage_note(),
            },
        )

    citations: list[dict[str, Any]] = []
    for hit in res.hits:
        text = hit.text
        if hit.caveat:
            text += f"\n\n> {hit.caveat}。"
        citations.append(
            _citation(
                cid=f"stat_sources:{hit.key}:{q}",
                title=hit.title,
                url=hit.url,
                text=text,
            )
        )

    return ToolResult(
        ok=True,
        data={
            "found": True,
            "channels_hit": [
                {"key": h.key, "label": h.label_zh, "count": h.total} for h in res.hits
            ],
            # 已采集但本次零命中的渠道。与「该渠道未采集」是两件事，分开报；
            # 且零命中**不等于**该渠道给不了这条属性（多为用词不一致）
            "channels_no_match": res.no_match,
            "channels_no_match_note": res.no_match_note(),
            "wording_hint": res.wording_hint,
            "total": res.total_hits,
            # 这条**必须**被转述：不列出未采集渠道，答案就是一份假装完整的清单
            "coverage_note": res.coverage_note(),
        },
        citations=citations,
        meta={"channel": "structured", "registry": "kb.stat_channels"},
    )


# ── 工具 3：知识库自描述 ──────────────────────────────────────────

@REGISTRY.register(
    name="kb_coverage",
    description=(
        "查本知识库**覆盖了什么、没覆盖什么**，以及每个领域的已知置信度限制。"
        "用户问「你能回答什么 / 你有哪些数据」，或你需要判断某个领域是否被采集时用它。"
        "在回答「全面列举」类问题前调用它，可以避免给出漏掉整个维度的清单。"
    ),
    parameters={"type": "object", "properties": {}},
)
def kb_coverage() -> ToolResult:
    from kb import coverage as cov  # noqa: PLC0415

    conn = dbmod.get_conn()
    rows = cov.load(conn)
    usable = [s for s in rows if s.usable]
    text = cov.render_manifest(conn, for_prompt=False)
    return ToolResult(
        ok=True,
        data={
            "domains_total": len(rows),
            "domains_usable": len(usable),
            "usable_categories": cov.usable_categories(conn),
            "manifest": text[:4000],
        },
        # 覆盖声明是**元信息**不是游戏事实，故不作为引用交出——
        # 出口闸门把「未采集」这类缺据声明判为 narrative，本就不需要绑定引用。
        citations=[],
        meta={"channel": "meta"},
    )
