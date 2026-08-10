#!/usr/bin/env python3
"""结构化工具验证：精确层的对外契约是否真的精确、是否诚实。

    python scripts/test_tools.py

检验重点不是「有没有返回」，而是：
  - 证据文本是**渲染后的表格**（否则出口闸门的数值回填校验无法工作）
  - 无生成权重的词缀必须在证据里写明「不能给出成功率」
  - 枚举类查询必须携带未采集渠道声明
  - kb_search 的 category 枚举必须与实际有数据的分类一致
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from tools.registry import REGISTRY  # noqa: E402
import tools.impl  # noqa: F401,E402  触发注册


def call(name: str, **kwargs):  # noqa: ANN201
    return REGISTRY.call(name, json.dumps(kwargs, ensure_ascii=False))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    dbmod.init_db(cfg.db_path)

    problems: list[str] = []
    print("=" * 84)
    print(f"结构化工具验证（已注册：{'、'.join(REGISTRY.names())}）")
    print("=" * 84)

    # ── 1. mod_tiers：精确梯度 ──
    print("\n[1] mod_tiers：弓 × 火焰伤害")
    r = call("mod_tiers", item_class="弓", mod_query="火焰伤害")
    print(f"    ok={r.ok} data={r.data}")
    if not r.ok or not r.data.get("found"):
        problems.append("mod_tiers(弓, 火焰伤害) 没有命中")
    else:
        first = r.citations[0]
        print(f"    首条证据标题：{first['title']}")
        print(f"    证据是表格：{'| T 级 |' in first['text']}")
        print(f"    URL：{first['url']}")
        if "| T 级 |" not in first["text"]:
            problems.append("mod_tiers 的证据文本不是渲染表格，出口数值校验会失效")
        if not first["url"]:
            problems.append("mod_tiers 的证据缺 URL，答案无法溯源")
        print("\n    ---- 证据原文（前 900 字）----")
        print("\n".join("    " + ln for ln in first["text"][:900].splitlines()))

    # ── 2. 护甲类中文名 → 多页面 ──
    print("\n[2] mod_tiers：胸甲（一个中文名对应多个 POEDB 页面）")
    r = call("mod_tiers", item_class="胸甲", mod_query="最大生命")
    n_pages = len(r.data.get("pages_searched") or []) if r.ok else 0
    print(f"    覆盖页面 {n_pages} 个：{(r.data.get('pages_searched') or [])[:8]}")
    print(f"    说明：{r.data.get('scope')}")
    if n_pages < 2:
        problems.append("胸甲应解析出多个 Body_Armours_* 页面，实际只有 %d 个" % n_pages)

    # ── 3. 无权重词缀必须写明算不出概率 ──
    print("\n[3] mod_tiers：胸甲 × 魔力保留效能（含无权重来源）")
    r = call("mod_tiers", item_class="胸甲", mod_query="保留效能")
    warned = any("不能给出成功率" in c["text"] or "无生成权重" in c["text"]
                 for c in r.citations)
    print(f"    梯度组 {r.data.get('ladders_total')}，含置信度告警：{warned}")
    for c in r.citations[:3]:
        line = next((ln for ln in c["text"].splitlines() if ln.startswith("置信度")), "")
        print(f"      {c['title'][:48]:50s} {line[:60]}")
    if not warned:
        problems.append("存在无权重来源却没有任何置信度告警——会被误当成概率可算")

    # ── 4. stat_sources：多渠道枚举必须逐渠道列出 + 带覆盖声明 ──
    print("\n[4] stat_sources：药剂效果（场景 B）")
    r = call("stat_sources", stat_query="药剂效果")
    hit = r.data.get("channels_hit") or []
    print(f"    ok={r.ok} 命中 {len(hit)} 个渠道，共 {r.data.get('total')} 条")
    for h in hit:
        print(f"      - {h['label']:10s} {h['count']:>4} 条")
    # 场景 B 的要点是**跨渠道**：只答装备而漏掉天赋，等于漏了玩家点名要的一个维度
    labels = {h["key"] for h in hit}
    for need in ("equipment_mod", "unique_item", "passive"):
        if need not in labels:
            problems.append(f"stat_sources(药剂效果) 未覆盖渠道 {need}——跨渠道枚举不完整")
    note = r.data.get("coverage_note") or ""
    print(f"    覆盖声明长度 {len(note)}，含「未采集」：{'未采集' in note}")
    print(f"      {note[:200]}…")
    if "未采集" not in note:
        problems.append("stat_sources 未携带未采集渠道声明——枚举会假装完整")
    # 覆盖声明只该列**属性来源**类的未采集领域，不该把市场价格、联盟机制也塞进来
    for irrelevant in ("市场价格", "联盟机制", "游戏常量"):
        if irrelevant in note:
            problems.append(f"覆盖声明混入了非属性来源领域「{irrelevant}」，会稀释真正的缺口")
    for c in r.citations:
        print(f"    证据：{c['title']}")
    if len(r.citations) != len(hit):
        problems.append("命中渠道数与证据条数不一致——有渠道命中了却没给证据")

    # ── 5. 虚构属性不得被命中 ──
    print("\n[5] stat_sources：虚构属性")
    r = call("stat_sources", stat_query="这个词缀纯属虚构")
    print(f"    found={r.data.get('found')}  reason={r.data.get('reason')}")
    print(f"    用词提示：{r.data.get('wording_hint') or '（无，正确——不该给不相干的建议）'}")
    if r.data.get("found"):
        problems.append("虚构词缀被 stat_sources 命中")
    if r.data.get("wording_hint"):
        problems.append("虚构查询不应给出用词提示——那会把编造的问题导向不相干的词缀")

    # ── 5b. 社区简称：零命中不得被读成「该渠道给不了」 ──
    print("\n[5b] stat_sources：社区简称「法术压制」（游戏原文为「法术伤害压制率」）")
    r = call("stat_sources", stat_query="法术压制")
    nm = r.data.get("channels_no_match") or []
    hint = r.data.get("wording_hint") or ""
    print(f"    零命中渠道：{nm}")
    print(f"    用词提示：{hint[:150]}")
    if "装备词缀" in nm and not hint:
        problems.append(
            "装备词缀渠道零命中却没有用词提示——模型会据此断言装备给不了该属性"
        )
    if hint and "压制" not in hint:
        problems.append("用词提示没指向有区分度的子词（应为「压制」而非「法术」）")
    if not (r.data.get("channels_no_match_note") or ""):
        problems.append("零命中未附说明——「没命中」会被读成「给不了」")

    # ── 6. check_mod_combination：三态可行性，禁止把局部兼容当可制作 ──
    print("\n[6] check_mod_combination：项链 × 最大生命 + 火焰抗性")
    r = call(
        "check_mod_combination",
        item_class="项链",
        targets=[{"query": "最大生命"}, {"query": "火焰抗性"}],
        rarity="稀有",
    )
    print(f"    ok={r.ok} verdict={r.data.get('verdict') if r.ok else 'error'}")
    print(f"    gaps={[g.get('code') for g in (r.data.get('gaps') or [])] if r.ok else []}")
    print(f"    citations={len(r.citations)}")
    if not r.ok:
        problems.append(f"check_mod_combination 调用失败：{r.error}")
    else:
        if r.data.get("verdict") != "unknown":
            problems.append("无 family 冲突但容量规则缺失时必须为 unknown")
        if not any(g.get("code") == "affix_capacity" for g in r.data.get("gaps") or []):
            problems.append("check_mod_combination 未报告前后缀容量缺据")
        if not r.citations or any(not c.get("url") for c in r.citations):
            problems.append("check_mod_combination 的候选事实缺少 POEDB 引用")
        forbidden = {"steps", "probability", "cost"}

        def collect_keys(value):  # noqa: ANN202
            if isinstance(value, dict):
                yield from value
                for child in value.values():
                    yield from collect_keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from collect_keys(child)

        leaked = forbidden & set(collect_keys(r.data))
        if leaked:
            problems.append(f"check_mod_combination 越界返回字段：{sorted(leaked)}")

    # ── 7. kb_coverage ──
    print("\n[7] kb_coverage")
    r = call("kb_coverage")
    print(f"    领域 {r.data.get('domains_total')}，可用 {r.data.get('domains_usable')}，"
          f"分类 {r.data.get('usable_categories')}")
    if not r.data.get("manifest"):
        problems.append("kb_coverage 没有返回覆盖清单")

    # ── 8. category 枚举说真话 ──
    print("\n[8] kb_search 的 category 枚举")
    from tools.impl.kb_search import sync_category_enum  # noqa: PLC0415

    cats = sync_category_enum()
    spec = REGISTRY.spec("kb_search")
    enum = spec.parameters["properties"]["category"]["enum"]
    print(f"    实际有数据：{cats}")
    print(f"    schema enum：{enum}")
    if enum != cats:
        problems.append("category 枚举与实际有数据的分类不一致")
    if len(enum) == 6:
        problems.append("枚举仍是六类，未按实际数据收窄")

    # ── 9. 覆盖声明是否真的进了系统提示词 ──
    print("\n[9] 系统提示词是否注入覆盖声明")
    from agents.knowledge import ALLOWED_TOOLS, build_system_prompt  # noqa: PLC0415

    sp = build_system_prompt()
    has = "知识库覆盖范围" in sp
    print(f"    注入：{has}，提示词长度 {len(sp)}")
    print(f"    允许工具：{ALLOWED_TOOLS}")
    if not has:
        problems.append("系统提示词没有注入覆盖声明")
    missing = [t for t in ALLOWED_TOOLS if t not in REGISTRY.names()]
    if missing:
        problems.append(f"ALLOWED_TOOLS 里有未注册的工具：{missing}")

    print("\n" + "=" * 84)
    if problems:
        print(f"结论：**未通过**，{len(problems)} 个问题")
        for p in problems:
            print(f"  - {p}")
    else:
        print("结论：全部通过")
    dbmod.close_db()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
