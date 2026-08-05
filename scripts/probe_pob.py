"""清点 Path of Building 数据资产，并验证解析器能否全量跑通。

    python scripts/probe_pob.py                    # 清点 + 解析验证
    python scripts/probe_pob.py --align            # 额外与现有知识库做名称对齐
    POB_PATH=/path/to/PathOfBuilding python scripts/probe_pob.py

PoB 仓库位置按以下顺序解析，**不硬编码本机绝对路径**（跨平台规则）：
  1) 环境变量 `POB_PATH`
  2) `config.yaml` 的 `sources.pob.path`
  3) 仓库根的同级目录 `../PathOfBuilding`

这个脚本是「PoB 能提供什么、格式变没变」的唯一真源。PoB 每次更新后重跑它，
解析失败会明确报出文件与位置——比在导入时静默丢字段安全得多。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import get_config  # noqa: E402
from kb.sources.pob import PoBNotFound, load_repo  # noqa: E402
from kb.sources.pob_lua import (  # noqa: E402
    LuaOpaque,
    LuaSyntaxError,
    iter_long_strings,
    load_lua_file,
    parse_assignments,
    parse_return_table,
)


# ── 清点计划 ───────────────────────────────────────────────

@dataclass
class Spec:
    """一个数据资产：怎么解析、对应我们哪个知识域。"""

    label: str
    pattern: str
    form: str  # return | assign | longstring
    domain: str
    note: str = ""


#: 按「对 poe-agent 的价值」排序，不是按文件大小。
SPECS: list[Spec] = [
    Spec("唯一装备", "Data/Uniques/*.lua", "longstring", "装备/唯一装备",
         "与 POEDB 重叠，可做跨来源交叉校验"),
    Spec("底材", "Data/Bases/*.lua", "assign", "装备/底材",
         "含 implicit / req / tags / influenceTags，POEDB 侧目前无实体"),
    Spec("显式词缀", "Data/ModExplicit.lua", "return", "制作系统/显式词缀",
         "含 level + weightKey/weightVal，制作成功率的权重真源"),
    Spec("腐化隐式", "Data/ModCorrupted.lua", "return", "装备/腐化隐式"),
    Spec("裂界词缀", "Data/ModEldritch.lua", "return", "制作系统/裂界"),
    Spec("合成隐式", "Data/ModSynthesis.lua", "return", "装备/合成隐式"),
    Spec("专属词缀", "Data/ModItemExclusive.lua", "return", "制作系统/专属词缀"),
    Spec("工艺台词缀", "Data/ModMaster.lua", "return", "制作系统/工艺台"),
    Spec("面纱词缀", "Data/ModVeiled.lua", "return", "制作系统/辛迪加"),
    Spec("地渊词缀", "Data/ModDelve.lua", "return", "制作系统/化石"),
    Spec("珠宝词缀", "Data/ModJewel*.lua", "return", "装备/珠宝词缀"),
    Spec("药剂词缀", "Data/ModFlask.lua", "return", "装备/药剂词缀"),
    Spec("Foulborn 词缀", "Data/ModFoulborn.lua", "return", "联盟机制/本赛季"),
    Spec("附魔", "Data/Enchantment*.lua", "return", "装备/附魔"),
    Spec("技能石", "Data/Gems.lua", "return", "游戏基础/技能",
         "含 tags / 属性需求 / grantedEffectId，串联 Skills/"),
    Spec("技能定义", "Data/Skills/*.lua", "assign", "游戏基础/技能",
         "唯一含 mod() 调用的一组；只取字面量字段"),
    # 只清点最新版天赋树。41 个历史版本共 550MB，对知识库无用；
    # 版本 diff 分析（V3）需要时再单独取指定版本。
    Spec("天赋树(最新)", "TreeData/{latest}/tree.lua", "return", "游戏基础/天赋",
         "含节点 / 升华 / mastery / 星团槽，按版本分目录"),
    Spec("万神殿", "Data/Pantheons.lua", "return", "游戏基础/万神殿"),
    Spec("Boss 防御", "Data/Bosses.lua", "assign", "地图系统/Boss",
         "armourMult / evasionMult / isUber"),
    Spec("Boss 技能", "Data/BossSkills.lua", "return", "地图系统/Boss",
         "含伤害倍率与穿透，Boss Agent 的防御门槛依据"),
    Spec("精华", "Data/Essence.lua", "return", "制作系统/精华",
         "精华 → 底材类别 → 具体词缀 ID 的完整映射"),
    Spec("星团珠宝", "Data/ClusterJewels.lua", "return", "装备/珠宝词缀"),
    Spec("兽魂制作", "Data/BeastCraft.lua", "return", "制作系统/野兽"),
    Spec("熔炉", "Data/Crucible.lua", "return", "制作系统/熔炉"),
    Spec("纹身", "Data/TattooPassives.lua", "return", "游戏基础/天赋"),
    Spec("召唤物", "Data/Minions.lua", "assign", "游戏基础/召唤物", "含 mod() 调用"),
    Spec("幽灵", "Data/Spectres.lua", "assign", "游戏基础/召唤物", "含 mod() 调用"),
    Spec("稀有怪词缀", "Data/Rares.lua", "return", "地图系统/怪物"),
    Spec("通货名", "Data/CurrencyNames.lua", "return", "经济系统/通货"),
    Spec("技能消耗类型", "Data/Costs.lua", "return", "游戏基础/技能"),
    Spec("stat 显示文本", "Data/StatDescriptions/*.lua", "return", "游戏基础/术语",
         "内部 stat id → 人类可读文本，PoB 独有"),
    Spec("交易站 stat", "Data/TradeSiteStats.lua", "return", "经济系统/交易",
         "打通 Trade API / poe.watch 的 stat id"),
    Spec("游戏常量", "Data/Misc.lua", "assign", "游戏基础/常量",
         "gameConstants + 怪物属性表，calc/constants.py 的真源"),
    Spec("风味文本", "Data/FlavourText.lua", "return", "装备/唯一装备"),
]


@dataclass
class Result:
    spec: Spec
    files: int = 0
    entries: int = 0
    bytes_: int = 0
    ms: int = 0
    opaque: int = 0
    error: str = ""
    sample_keys: list[str] = field(default_factory=list)


def count_entries(value) -> int:
    return len(value) if isinstance(value, (dict, list)) else 0


def count_opaque(value, budget: int = 200_000) -> int:
    """统计未求值表达式的数量，用于判断「有多少信息被跳过」。"""
    stack = [value]
    n = 0
    seen = 0
    while stack and seen < budget:
        cur = stack.pop()
        seen += 1
        if isinstance(cur, LuaOpaque):
            n += 1
        elif isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return n


def probe(root: Path, spec: Spec, latest_tree: str = "") -> Result:
    res = Result(spec=spec)
    pattern = spec.pattern.replace("{latest}", latest_tree or "*")
    paths = sorted((root / "src").glob(pattern))
    if not paths:
        res.error = "未找到文件"
        return res
    started = time.monotonic()
    for path in paths:
        res.files += 1
        res.bytes_ += path.stat().st_size
        text = load_lua_file(path)
        try:
            if spec.form == "longstring":
                blocks = list(iter_long_strings(text))
                res.entries += len(blocks)
                if blocks and not res.sample_keys:
                    res.sample_keys = [b.strip().splitlines()[0] for b in blocks[:3]]
            elif spec.form == "return":
                value = parse_return_table(text)
                res.entries += count_entries(value)
                res.opaque += count_opaque(value)
                if not res.sample_keys:
                    res.sample_keys = [str(k) for k in list(value)[:3]]
            else:
                value = parse_assignments(text)
                res.entries += count_entries(value)
                res.opaque += count_opaque(value)
                if not res.sample_keys:
                    res.sample_keys = [str(k) for k in list(value)[:3]]
        except LuaSyntaxError as exc:
            res.error = f"{path.name}: {exc}"
            break
        except RecursionError:
            res.error = f"{path.name}: 嵌套过深"
            break
    res.ms = int((time.monotonic() - started) * 1000)
    return res


# ── 与现有知识库对齐 ───────────────────────────────────────

def align_with_kb(root: Path) -> None:
    import sqlite3

    cfg = get_config()
    db_path = cfg.db_path
    if not db_path.is_file():
        print("  跳过：本地还没有 data/poe.db")
        return
    conn = sqlite3.connect(str(db_path))

    pob_names: set[str] = set()
    for path in sorted((root / "src" / "Data" / "Uniques").glob("*.lua")):
        for block in iter_long_strings(load_lua_file(path)):
            lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
            if lines:
                pob_names.add(lines[0])

    db_en = {
        r[0] for r in conn.execute(
            "SELECT name_en FROM entity WHERE entity_type='unique_item' "
            "AND name_en IS NOT NULL AND name_en<>''"
        )
    }
    db_total = conn.execute(
        "SELECT count(*) FROM entity WHERE entity_type='unique_item'"
    ).fetchone()[0]
    conn.close()

    both = pob_names & db_en
    only_pob = sorted(pob_names - db_en)
    only_db = sorted(db_en - pob_names)
    print(f"  PoB 唯一装备 {len(pob_names)} 件 ｜ 库内 {db_total} 件（有英文名 {len(db_en)} 件）")
    print(f"  两边都有 : {len(both)}  → 可做跨来源数值交叉校验")
    print(f"  只在 PoB : {len(only_pob)}  例：{only_pob[:5]}")
    print(f"  只在 POEDB: {len(only_db)}  例：{only_db[:5]}")
    if len(db_en) < db_total:
        print(f"  注意：库内有 {db_total - len(db_en)} 件缺英文名，无法参与按名对齐")


# ── main ───────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pob-path", default=None)
    ap.add_argument("--align", action="store_true", help="与 data/poe.db 做名称对齐")
    ap.add_argument("--only", default="", help="只跑标签含该子串的资产")
    args = ap.parse_args()

    try:
        repo = load_repo(args.pob_path)
    except PoBNotFound as exc:
        raise SystemExit(str(exc)) from exc
    root = repo.root
    print(f"PoB 仓库：{root}（来自 {repo.origin}）")
    print(f"版本：{repo.version_label} ｜ 游戏版本 {repo.game_version or '未知'} ｜ "
          f"commit {repo.short_commit or '未知'}"
          f"{'' if repo.reproducible else '（引用不可复现！）'}")
    print(f"引用样例：{repo.blob_url('Data/ModExplicit.lua')}")
    print()

    specs = [s for s in SPECS if not args.only or args.only in s.label]
    print(f"{'资产':<14}{'条目':>7}{'文件':>5}{'大小':>9}{'耗时':>8}{'未求值':>8}  知识域")
    print("─" * 96)
    results: list[Result] = []
    latest_tree = repo.tree_version
    for spec in specs:
        res = probe(root, spec, latest_tree)
        results.append(res)
        if res.error:
            print(f"{spec.label:<14}{'—':>7}{res.files:>5}{'':>9}{'':>8}{'':>8}  "
                  f"[失败] {res.error[:70]}")
            continue
        print(f"{spec.label:<14}{res.entries:>7}{res.files:>5}"
              f"{res.bytes_ // 1024:>7}KB{res.ms:>6}ms{res.opaque:>8}  {spec.domain}")

    ok = [r for r in results if not r.error]
    bad = [r for r in results if r.error]
    print("─" * 96)
    print(f"可解析 {len(ok)}/{len(results)} 项，"
          f"合计 {sum(r.entries for r in ok):,} 条目，"
          f"{sum(r.bytes_ for r in ok) / 1024 / 1024:.1f} MB，"
          f"{sum(r.ms for r in ok) / 1000:.1f}s，"
          f"未求值表达式 {sum(r.opaque for r in ok):,} 处")

    print("\n【备注】")
    for spec in specs:
        if spec.note:
            print(f"  {spec.label}：{spec.note}")

    if bad:
        print("\n【解析失败，需处理】")
        for r in bad:
            print(f"  {r.spec.label}（{r.spec.pattern}）：{r.error}")

    if args.align:
        print("\n【与现有知识库对齐】")
        align_with_kb(root)

    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
