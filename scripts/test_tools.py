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

    # ── 4. mod_sources：枚举类查询必须带覆盖声明 ──
    print("\n[4] mod_sources：药剂效果（场景 B）")
    r = call("mod_sources", stat_query="药剂效果")
    print(f"    ok={r.ok} 词缀渠道={r.data.get('affix_channels')} "
          f"唯一装备={r.data.get('unique_items')}")
    note = r.data.get("coverage_note") or ""
    print(f"    覆盖声明长度 {len(note)}，含「未采集」：{'未采集' in note}")
    print(f"      {note[:220]}…")
    if "未采集" not in note:
        problems.append("mod_sources 未携带未采集渠道声明——枚举会假装完整")
    for c in r.citations:
        print(f"    证据：{c['title']}")
    if not r.citations:
        problems.append("mod_sources(药剂效果) 没有返回证据")

    # ── 5. 未采集领域的关键词不应被 mod_sources 假装答上 ──
    print("\n[5] mod_sources：一个只存在于天赋树的说法")
    r = call("mod_sources", stat_query="这个词缀纯属虚构")
    print(f"    found={r.data.get('found')}  reason={r.data.get('reason')}")
    if r.data.get("found"):
        problems.append("虚构词缀被 mod_sources 命中")

    # ── 6. kb_coverage ──
    print("\n[6] kb_coverage")
    r = call("kb_coverage")
    print(f"    领域 {r.data.get('domains_total')}，可用 {r.data.get('domains_usable')}，"
          f"分类 {r.data.get('usable_categories')}")
    if not r.data.get("manifest"):
        problems.append("kb_coverage 没有返回覆盖清单")

    # ── 7. category 枚举说真话 ──
    print("\n[7] kb_search 的 category 枚举")
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

    # ── 8. 覆盖声明是否真的进了系统提示词 ──
    print("\n[8] 系统提示词是否注入覆盖声明")
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
