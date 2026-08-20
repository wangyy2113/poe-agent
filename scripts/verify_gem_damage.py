#!/usr/bin/env python3
"""拿 POEDB 的技能页对账 PoB 的 effectiveness 公式（不入库，只报告）。

## 为什么需要这一步

`pob_skill_level` 刻意没有伤害列：PoB 存的每级伤害是**乘数**（冰霜新星 1 级存
`0.8 / 1.2`），要配 `baseEffectiveness` / `incrementalEffectiveness` 与
`statInterpolation` 按公式算出来才是游戏里的显示值。算错就是「带着引用的错数字」。

所以上计算器之前先要一套**对账口径**：POEDB 的技能页把区间写成
「造成 (25 — 1122) - (38 — 1683) 冰霜伤害」——括号里是 **1 级 → 自然最高级**。
拿它对我们算出来的两端，对得上才谈得上入库。

    python scripts/verify_gem_damage.py --sample 20     # 抽样对账（会联网抓页面）
    python scripts/verify_gem_damage.py --only 冰霜新星  # 只对一个
    python scripts/verify_gem_damage.py --cached-only    # 只用已缓存的页，不联网

## 公式（`CalcTools.lua:195-206`）

    availableEffectiveness =(SkillDamageBaseEffectiveness
                             + SkillDamageIncrementalEffectiveness ×(角色等级 − 1))
                            × 宝石 baseEffectiveness
                            ×(1 + 宝石 incrementalEffectiveness)^(角色等级 − 1)
    某级的伤害 = round(availableEffectiveness × 该级那个乘数)

其中「角色等级」取该宝石等级的 `levelRequirement`（PoB 的 `actorLevel` 默认值）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calc import constants  # noqa: E402
from calc import skill_damage as sd  # noqa: E402
from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from kb.fetcher import Fetcher  # noqa: E402
from kb.sources.pob import PoBNotFound, load_repo  # noqa: E402
from kb.sources.pob_skills import SKILL_FILES  # noqa: E402
from kb.sources.pob_lua import load_lua_file, parse_assignments  # noqa: E402

#: POEDB 把区间写成「造成 (低1 — 低max) - (高1 — 高max) X伤害」。
#: 破折号是全角 —，与减号不同；两端分别是**宝石 1 级与自然最高级**。
_RANGE_RE = re.compile(
    r"造成\s*\((\d[\d,]*)\s*—\s*(\d[\d,]*)\)\s*-\s*\((\d[\d,]*)\s*—\s*(\d[\d,]*)\)\s*"
    r"([一-鿿]{2,4})伤害"
)
#: 只对账这几种伤害类型的技能：它们的 stat 名字在 PoB 侧是 `spell_*_base_*_damage`
#: 这种可判形态，不需要猜。
_TYPE_STATS = {
    "冰霜": "cold", "火焰": "fire", "闪电": "lightning",
    "混沌": "chaos", "物理": "physical",
}


def _num(text: str) -> int:
    return int(text.replace(",", ""))


def _pob_skills(pob_root: Path) -> dict[str, dict]:
    """`gem_id`（小写）→ granted effect 原始表。只取有 levels 的。"""
    out: dict[str, dict] = {}
    for rel in SKILL_FILES:
        path = pob_root / "src" / rel
        if not path.is_file():
            continue
        # `parse_assignments` 把 `skills["IceNova"] = {...}` 解析成**顶层键**，
        # 没有中间那层 `skills`（与 `kb/sources/pob_skills.py:482` 的用法一致）
        for key, value in parse_assignments(load_lua_file(path)).items():
            if isinstance(value, dict) and isinstance(value.get("levels"), dict):
                out[str(key).lower()] = value
    return out


def _expected(effect: dict, gem_level: int) -> tuple[int, int] | None:
    """按公式算某个宝石等级的（最小, 最大）伤害。算不出来返回 None。

    公式与两个坑（同名 stat 累加、先 round 再相加）都在 `calc/skill_damage.py` 里，
    **这里只调用不重写**——两处各写一遍的话，对账脚本就不再是在验证入库用的那份代码。
    """
    levels = effect["levels"]
    level = levels.get(gem_level)
    stats = effect.get("stats")
    if not isinstance(level, dict) or not isinstance(stats, dict):
        return None
    interp = level.get("statInterpolation")
    if not isinstance(interp, dict):
        return None
    result = sd.base_damage(
        {i: str(n) for i, n in stats.items() if isinstance(i, int)},
        {i: v for i, v in level.items() if isinstance(i, int) and isinstance(v, (int, float))},
        {i: v for i, v in interp.items() if isinstance(i, int)},
        actor_level=int(level.get("levelRequirement") or 1),
        base_effectiveness=float(effect.get("baseEffectiveness") or 1),
        incremental_effectiveness=float(effect.get("incrementalEffectiveness") or 0),
        game_base=constants.get("gameConstants.SkillDamageBaseEffectiveness"),
        game_incremental=constants.get("gameConstants.SkillDamageIncrementalEffectiveness"),
    )
    return (result.minimum, result.maximum) if result else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=12, help="抽样个数")
    parser.add_argument("--only", default="", help="只对账这个中文名")
    parser.add_argument("--cached-only", action="store_true", help="只用已缓存页，不联网")
    parser.add_argument("--pob-path", default=None)
    args = parser.parse_args()

    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    conn = dbmod.get_conn()

    try:
        repo = load_repo(args.pob_path, cfg)
    except PoBNotFound as exc:
        print(f"[中止] {exc}")
        return 2
    effects = _pob_skills(repo.root)
    print(f"PoB：{repo.version_label} commit={repo.short_commit}，"
          f"解析到 {len(effects)} 个 granted effect")

    where = "WHERE name_zh = ?" if args.only else ""
    rows = conn.execute(
        "SELECT gem_id, name_zh, name_en, poedb_url, natural_max_level "
        f"FROM pob_skill_gem {where} ORDER BY name_zh",
        (args.only,) if args.only else (),
    ).fetchall()
    if not rows:
        print(f"[中止] 库里没有名为「{args.only}」的技能")
        return 2

    fetcher = Fetcher(
        Path(str(cfg.get("fetch.cache_dir", "data/http_cache"))),
        user_agent=str(cfg.get("fetch.user_agent", "poe-agent/0.1")),
        requests_per_second=float(cfg.get("fetch.requests_per_second", 0.5)),
    )

    checked = matched = 0
    skipped: dict[str, int] = {}
    problems: list[str] = []
    for row in rows:
        if checked >= args.sample and not args.only:
            break
        effect = effects.get(str(row["gem_id"]).lower())
        if effect is None:
            skipped["pob 侧没有这个 granted effect"] = skipped.get("pob 侧没有这个 granted effect", 0) + 1
            continue
        low = _expected(effect, 1)
        high = _expected(effect, int(row["natural_max_level"]))
        if not low or not high:
            skipped["不是 effectiveness 插值"] = skipped.get("不是 effectiveness 插值", 0) + 1
            continue
        path = fetcher._cache_path(row["poedb_url"])  # noqa: SLF001  只为判断是否要联网
        if args.cached_only and not path.exists():
            skipped["页面未缓存（--cached-only）"] = skipped.get("页面未缓存（--cached-only）", 0) + 1
            continue
        try:
            page = fetcher.fetch(row["poedb_url"]).text or ""
        except Exception as exc:  # noqa: BLE001  单页失败不该中断整轮
            problems.append(f"{row['name_zh']}：抓取失败 {exc}")
            continue
        found = _RANGE_RE.search(re.sub(r"<[^>]+>", " ", page))
        if not found:
            skipped["页面上没有可对账的伤害区间"] = skipped.get("页面上没有可对账的伤害区间", 0) + 1
            continue
        checked += 1
        page_low = (_num(found.group(1)), _num(found.group(3)))
        page_high = (_num(found.group(2)), _num(found.group(4)))
        ok = page_low == low and page_high == high
        matched += ok
        mark = "  " if ok else "✗ "
        print(f"{mark}{row['name_zh']}（{row['name_en']}）{found.group(5)}伤害")
        print(f"     1 级 页面 {page_low}  算出 {low}"
              f"   {row['natural_max_level']} 级 页面 {page_high}  算出 {high}")
        if not ok:
            problems.append(
                f"{row['name_zh']}：页面 {page_low}/{page_high} vs 算出 {low}/{high}"
            )

    print(f"\n对账 {checked} 个，命中 {matched}"
          f"（{matched / checked * 100:.0f}%）" if checked else "\n没有可对账的样本")
    if skipped:
        print("跳过：" + "、".join(f"{k} {v}" for k, v in sorted(skipped.items())))
    if problems:
        print(f"\n【不一致 {len(problems)} 项】")
        for p in problems[:10]:
            print(f"  {p}")
    dbmod.close_db()
    return 0 if checked and matched == checked else 1


if __name__ == "__main__":
    raise SystemExit(main())
