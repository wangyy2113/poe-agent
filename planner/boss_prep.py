"""把 Wiki 明写的 Boss 建议与 PoB 快照并排；不推断整场战斗结论。"""

from __future__ import annotations

import math
import re
from typing import Any

from parsers.pob_build import PobBuild


# 只接受完整的 Wiki 建议行。技能表、掉落表和内嵌 tooltip 的数字不能变成门槛。
_DPS = re.compile(r"^• At least (?P<value>\d+(?:\.\d+)?) million damage per second$")
_POOL = re.compile(
    r"^• At least (?P<value>[\d,]+) combined Life and Energy [Ss]hield"
    r"(?: and a reliable source of recovery)?$"
)
_RESISTS = re.compile(
    r"^• Capped elemental resistances \(fire, cold and lightning resistance at "
    r"(?P<value>\d+)%\)(?:;.*)?$"
)
_BOSS_CONFIGS = frozenset({"Boss", "Pinnacle", "Uber", "Shaper", "Sirus"})
_DPS_FIELDS = ("FullDPS", "CombinedDPS", "TotalDPS", "TotalDotDPS")


def _stat(build: PobBuild, fields: tuple[str, ...]) -> tuple[str, float] | None:
    for field in fields:
        value = build.player_stats.get(field)
        if value is not None and math.isfinite(value) and value >= 0:
            return field, value
    return None


def _wiki_recommendations(guide: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    found: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for section in guide.get("sections", []):
        if section.get("heading") != "Recommendations":
            continue
        for line in section.get("lines", []):
            if not isinstance(line, str) or len(line) > 600:
                # Wiki 展开的技能 tooltip 可以把一条列表项膨胀成数千字。
                # 这种行不能充当清晰、可复核的门槛原句。
                continue
            matches = (
                ("dps", _DPS.fullmatch(line), 1_000_000),
                ("combined_life_es", _POOL.fullmatch(line), 1),
                ("elemental_resistance", _RESISTS.fullmatch(line), 1),
            )
            for key, match, factor in matches:
                if match is None:
                    continue
                value = float(match.group("value").replace(",", "")) * factor
                if key in found:
                    ambiguous.add(key)
                else:
                    found[key] = {
                        "metric": key,
                        "wiki_minimum": value,
                        "source_line": line,
                        "source_heading": "Recommendations",
                        "source_url": str(guide["source_url"]) + "#Recommendations",
                        "source_hash": str(guide["content_hash"]),
                        "source_fetched_at": str(guide["fetched_at"]),
                    }
    for key in ambiguous:
        found.pop(key, None)
    return [found[key] for key in ("dps", "combined_life_es", "elemental_resistance")
            if key in found], sorted(ambiguous)


def compare_boss_guide(
    guide: dict[str, Any], build: PobBuild, *, same_boss_configuration: bool = False,
) -> dict[str, Any]:
    """数值关系是条件性对照；Wiki 建议不升级成当前版本保证。"""
    checks, ambiguous = _wiki_recommendations(guide)
    config = str(build.config.get("enemyIsBoss") or "")
    for check in checks:
        metric = check["metric"]
        check.update({"build_value": None, "build_fields": [], "difference": None,
                      "status": "unknown", "reason": ""})
        if metric == "dps":
            snapshot = _stat(build, _DPS_FIELDS)
            if snapshot is None:
                check["reason"] = "PoB PlayerStat 没有可用 DPS 快照"
                continue
            check["build_fields"] = [snapshot[0], "Config.enemyIsBoss"]
            check["build_value"] = snapshot[1]
            if not same_boss_configuration or config not in _BOSS_CONFIGS:
                check["reason"] = (
                    "须明确确认这份 PoB 的敌人、技能与增益配置适用于所问 Boss，"
                    "且 enemyIsBoss 已选 Boss 档；当前不作门槛比较"
                )
                continue
        elif metric == "combined_life_es":
            life = _stat(build, ("LifeUnreserved", "Life"))
            es = _stat(build, ("EnergyShield",))
            if life is None or es is None:
                check["reason"] = "PoB PlayerStat 缺少可用生命或能量护盾，不能把缺失项当 0"
                continue
            check["build_fields"] = [life[0], es[0]]
            check["build_value"] = life[1] + es[1]
            if "reliable source of recovery" in check["source_line"]:
                check["unassessed_clause"] = "可靠回复来源未由池子数值验证"
        else:
            fields = ("FireResist", "ColdResist", "LightningResist")
            values = [_stat(build, (field,)) for field in fields]
            if any(value is None for value in values):
                check["reason"] = "PoB PlayerStat 的火、冰、闪电抗性不全"
                continue
            check["build_fields"] = list(fields)
            check["build_value"] = {field: value[1] for field, value in zip(fields, values)}
            shortfalls = {field: max(0.0, check["wiki_minimum"] - value[1])
                          for field, value in zip(fields, values)}
            check["difference"] = shortfalls
            check["status"] = "at_or_above" if not any(shortfalls.values()) else "below"
            continue
        check["difference"] = check["build_value"] - check["wiki_minimum"]
        check["status"] = ("at_or_above" if check["difference"] >= 0 else "below")

    missing = []
    if not checks:
        missing.append("攻略 Recommendations 没有符合已审计格式的数值建议")
    if ambiguous:
        missing.append("同一指标有多条建议且无法确定适用条件：" + ", ".join(ambiguous))
    missing.extend((
        "药剂配置尚无逐 Boss 的已核验结构化依据",
        "该 Wiki 页面的建议是否仍适用当前游戏版本尚未核验",
    ))
    return {
        "boss_key": guide["boss_key"], "page_title": guide["page_title"],
        "source_url": guide["source_url"], "source_fetched_at": guide["fetched_at"],
        "build_tree_version": build.tree_version,
        "build_enemy_is_boss": config,
        "same_boss_configuration_assumed": same_boss_configuration,
        "checks": checks, "missing": missing,
        "status": "partial" if checks else "unknown",
        "boundary": (
            "只把抓取时 PoE Wiki Recommendations 中完整匹配的建议原句与用户 PoB "
            "PlayerStat 快照作数值对照。PoB 数值由用户提交，本库不重算；DPS 对照需要用户"
            "确认配置适用于该 Boss。达到页面数字不等于能存活或通关；Wiki 建议的当前版本"
            "适用性、打法、药剂与其它机制仍需逐项核验。"
        ),
    }
