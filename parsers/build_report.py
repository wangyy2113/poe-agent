"""Build 体检报告（第四十九轮，需求 8.4）。

需求 8.4 的原文是「检查并报告下列全部项目，**不得遗漏**」，共 16 项；
8.4.7 补的那句才是关键：**「IF 某项数据缺失 THEN SHALL 标注为『未知』而非填入估计值」**。

## 核心设计：只判不算

| | 来自哪里 |
| --- | --- |
| **值** | build 自带的 `<PlayerStat>` / `<Build>` / `<Items>`——**用户提供的数据** |
| **判据** | `game_constant` 里的常量（带 GGG 原始表出处）与 `calc/` 的既有规则 |
| **缺失** | 标注「未知」+ 说清缺哪一项，`value` 留空 |

**为什么不重算**：从 build 的词缀反推「你有多少火抗」要把每条词缀文本聚合成属性，
那正是卡住 DPS 的**同一个加成链问题**。做一个会与 PoB 悄悄不一致的部分重算，
就是「带着引用的错数字」——比说「未知」糟得多。重算留作后续一轮，
那时 `PlayerStat` 正好是现成的对账口径。

**DPS 只复述、不计算**：写「你的 PoB 算出来是 X」并注明本库不算 DPS。
复述用户自己工具的数**不违反**那条边界——边界是「本库不算 DPS」，
不是「不许引用你 build 里的数」。

## 16 项由 `_CHECKS` 表驱动

「不得遗漏」是需求原文，所以靠**代码**保证而不是靠写的时候记得：
一张常量表列出全部 16 项，缺哪一项在单测里当场失败。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from calc import constants
from parsers.pob_build import PobBuild

#: 取自 build 而不是本库算出来的——报告里每一项都要说清这一点。
FROM_BUILD = "取自你粘贴的 build（PoB 自己算的）"


@dataclass(frozen=True)
class CheckItem:
    """一项检查。`known=False` 时 `value` **必须为空**（8.4.7：不许填估计值）。"""

    key: str
    label: str
    value: str = ""
    known: bool = True
    #: `known=False` 时说清缺的是哪一项，以及为什么
    missing_reason: str = ""
    #: 与判据比对后的结论（「已满」「差 5% 到上限」）；没有判据就留空
    verdict: str = ""
    #: 判据的出处。数值判据一律带常量键名，便于复核
    basis: str = ""
    #: 值的来源说明
    source: str = FROM_BUILD


@dataclass
class BuildReport:
    items: list[CheckItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_key(self, key: str) -> CheckItem | None:
        return next((i for i in self.items if i.key == key), None)

    @property
    def unknown_count(self) -> int:
        return sum(1 for i in self.items if not i.known)


def _missing(key: str, label: str, stat: str) -> CheckItem:
    """统一的「未知」构造。**`value` 一定是空的**——这是 8.4.7 的硬要求。"""
    return CheckItem(
        key=key, label=label, value="", known=False,
        missing_reason=(
            f"这份 build 的 PlayerStat 里没有 `{stat}`。"
            "常见原因是导出它的 PoB 版本还没有这一项（例如法术压制是 3.16 才加的），"
            "本库**不据此估计**。"
        ),
    )


def _fmt(value: float) -> str:
    return f"{value:g}"


# ── 各项检查 ───────────────────────────────────────────────


def _resist(key: str, label: str, stat: str, *, elemental: bool) -> Callable:
    def check(_conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
        stats = build.player_stats
        if stat not in stats:
            return _missing(key, label, stat)
        value = stats[stat]
        over = stats.get(f"{stat}OverCap")
        cap = constants.get(constants.MAX_RESIST_CAP)
        shown = f"{_fmt(value)}%"
        if over:
            # 超抗顶得住减抗与诅咒，顶不住穿透（`calc/resistance` 的既有结论）
            shown += f"（超抗 +{_fmt(over)}%）"
        if value >= 75:
            verdict = "已达常规上限 75%"
        else:
            verdict = f"距常规上限 75% 还差 {_fmt(75 - value)}%"
        basis = (
            f"常规上限 75%、硬上限 {_fmt(cap)}% "
            f"（`game_constant.{constants.MAX_RESIST_CAP}`）"
        )
        if not elemental:
            basis += "；混沌抗性**不吃**「+X% 全部元素抗性」与「+X% 最大元素抗性」"
        return CheckItem(key=key, label=label, value=shown, verdict=verdict, basis=basis)

    return check


def _plain(key: str, label: str, stat: str, *, unit: str = "", note: str = "") -> Callable:
    def check(_conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
        if stat not in build.player_stats:
            return _missing(key, label, stat)
        return CheckItem(
            key=key, label=label,
            value=f"{_fmt(build.player_stats[stat])}{unit}", basis=note,
        )

    return check


def _capped(key: str, label: str, stat: str, const_key: str) -> Callable:
    def check(_conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
        if stat not in build.player_stats:
            return _missing(key, label, stat)
        value = build.player_stats[stat]
        cap = constants.get(const_key)
        return CheckItem(
            key=key, label=label, value=f"{_fmt(value)}%",
            verdict="已达上限" if value >= cap else f"距上限 {_fmt(cap)}% 还差 {_fmt(cap - value)}%",
            basis=f"上限 {_fmt(cap)}% 取自 `game_constant.{const_key}`",
        )

    return check


def _dps(_conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
    """**只复述，不计算。** 本库没有整条加成链，算不了 DPS。"""
    stats = build.player_stats
    for stat in ("FullDPS", "CombinedDPS", "TotalDPS"):
        if stats.get(stat):
            return CheckItem(
                key="dps", label="DPS", value=f"{_fmt(stats[stat])}（{stat}）",
                basis="**本库不计算 DPS**（缺整条加成链：增伤/更多/转化/穿透）。"
                      "这个数是你的 PoB 算的，本库只是复述。",
            )
    return _missing("dps", "DPS", "FullDPS / CombinedDPS / TotalDPS")


def _armour(_conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
    if "Armour" not in build.player_stats:
        return _missing("armour", "护甲", "Armour")
    return CheckItem(
        key="armour", label="护甲", value=_fmt(build.player_stats["Armour"]),
        # 与 `num_armour_reduction` / `gap_armour_without_damage` 那对用例同一条规矩
        basis="护甲**不是固定百分比减伤**（`护甲/(护甲+5×这一击的伤害)`），"
              "不给出这一击多大就没有减伤百分比可言，本库不替你假定一个伤害值。",
    )


def _pantheon(conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
    """`None` 是「没开」，不是「未知」——两者要分开。"""
    picks: list[str] = []
    unopened: list[str] = []
    for label, key in (("主神", build.pantheon_major), ("副神", build.pantheon_minor)):
        if not key or key == "None":
            unopened.append(label)
            continue
        row = conn.execute(
            "SELECT god_name_zh FROM pantheon_soul WHERE god_key = ? LIMIT 1", (key,)
        ).fetchone()
        picks.append(f"{label}：{row[0] if row else key}")
    if not picks:
        return CheckItem(
            key="pantheon", label="万神殿", value="未启用任何神力",
            verdict="两个位置都空着", basis="取自 build 的 pantheonMajorGod / MinorGod",
        )
    value = "；".join(picks)
    if unopened:
        value += f"（{'、'.join(unopened)}未启用）"
    return CheckItem(key="pantheon", label="万神殿", value=value,
                     basis="神祇键桥接到 `pantheon_soul.god_key`（与 PoB 的键逐字一致）")


def _ascendancy(_conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
    if not build.ascend_class_name or build.ascend_class_name == "None":
        return CheckItem(key="ascendancy", label="升华", value="未选择升华",
                         basis="取自 build 的 ascendClassName")
    return CheckItem(
        key="ascendancy", label="升华",
        value=f"{build.class_name} / {build.ascend_class_name}",
        basis="取自 build 的 className / ascendClassName",
    )


def _flasks(_conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
    hits = [i for i in build.items if "flask" in i.slot.lower()]
    if not hits:
        return CheckItem(key="flasks", label="药剂", value="没有药剂",
                         basis="按 build 的 Slot 名统计")
    names = [i.parsed.name or i.parsed.base_type for i in hits if i.parsed]
    return CheckItem(
        key="flasks", label="药剂",
        value=f"{len(hits)} 件：{'、'.join(names[:6])}" + (" 等" if len(names) > 6 else ""),
        basis="按 build 的 Slot 名统计",
    )


def _jewels(_conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
    """珠宝**不在 `Items` 的 Slot 里**，插在天赋树的 `<Sockets>` 上。

    实测 OccVortex：Slot 里一件珠宝都没有，而树上插着 3 颗——
    按 Slot 名找会得出「没有珠宝」这个错结论。
    """
    return CheckItem(
        key="jewels", label="珠宝",
        value=f"树上插着 {build.tree_jewels} 颗" if build.tree_jewels else "树上没有珠宝",
        basis="按天赋树 `<Spec><Sockets>` 里已占用的插槽统计"
              "（珠宝不在装备 Slot 里，按 Slot 找会得出「没有珠宝」的错结论）",
    )


def _curses(conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
    """按已入库的 `tags` 判定诅咒，**不靠名字猜**。"""
    ids = [g.gem_id for grp in build.skill_groups for g in grp.gems if g.gem_id]
    if not ids:
        return CheckItem(key="curses", label="诅咒", value="没有可识别的诅咒宝石",
                         basis="按 `pob_skill_gem.tags` 里的 `curse` 判定")
    rows = conn.execute(
        f"SELECT name_zh, tags FROM pob_skill_gem WHERE gem_id IN "  # noqa: S608
        f"({','.join('?' * len(ids))})", ids
    ).fetchall()
    names = [r[0] for r in rows if "curse" in json.loads(r[1] or "[]")]
    return CheckItem(
        key="curses", label="诅咒",
        value="、".join(names) if names else "没有诅咒宝石",
        basis="按 `pob_skill_gem.tags` 里的 `curse` 判定，不靠名字猜",
    )


def _passive_tree(_conn, build: PobBuild) -> CheckItem:  # noqa: ANN001
    if not build.passive_nodes:
        return CheckItem(
            key="passive_tree", label="天赋树", value="", known=False,
            missing_reason="build 的 `<Tree><Spec nodes>` 里没有已分配节点",
        )
    return CheckItem(
        key="passive_tree", label="天赋树",
        value=f"已分配 {len(build.passive_nodes)} 个节点"
              + (f"（角色 {build.level} 级）" if build.level else ""),
        basis="节点数取自 build 的 `<Spec nodes>`。**本轮不校验点数是否合法、"
              "也不解析每个节点给什么**——那要接天赋树图，另开一轮",
    )


#: 需求 8.4 原文点名的项目，**逐字抄下来**。
#: 这份清单由**需求**定，不由实现定——写成 `len(_CHECKS)` 就变成了
#: 「要求的项数 = 我实现了几项」，那种自证式常量恒等于自己、永远不会失败。
REQUIREMENT_8_4_ITEMS: tuple[str, ...] = (
    "DPS", "Effective HP", "Max Hit", "抗性", "混沌抗性", "法术压制", "护甲",
    "闪避", "格挡", "药剂", "保留", "诅咒", "万神殿", "珠宝", "天赋树", "升华",
)

#: 每一项检查覆盖需求里的哪一项。元素抗性拆成三条**更细**而不是更少，
#: 三条一起覆盖需求的「抗性」。单测据此断言 16 项一个不缺。
_COVERS: dict[str, str] = {
    "dps": "DPS", "effective_hp": "Effective HP", "max_hit": "Max Hit",
    "fire_resist": "抗性", "cold_resist": "抗性", "lightning_resist": "抗性",
    "chaos_resist": "混沌抗性", "spell_suppression": "法术压制", "armour": "护甲",
    "evasion": "闪避", "block": "格挡", "flasks": "药剂", "reservation": "保留",
    "curses": "诅咒", "pantheon": "万神殿", "jewels": "珠宝",
    "passive_tree": "天赋树", "ascendancy": "升华",
}

_CHECKS: tuple[tuple[str, Callable], ...] = (
    ("dps", _dps),
    ("effective_hp", _plain("effective_hp", "Effective HP", "TotalEHP")),
    ("max_hit", _plain("max_hit", "Max Hit", "PhysicalMaximumHitTaken")),
    ("fire_resist", _resist("fire_resist", "火焰抗性", "FireResist", elemental=True)),
    ("cold_resist", _resist("cold_resist", "冰霜抗性", "ColdResist", elemental=True)),
    ("lightning_resist",
     _resist("lightning_resist", "闪电抗性", "LightningResist", elemental=True)),
    ("chaos_resist", _resist("chaos_resist", "混沌抗性", "ChaosResist", elemental=False)),
    ("spell_suppression",
     _capped("spell_suppression", "法术压制", "SpellSuppressionChance",
             constants.SUPPRESSION_CHANCE_CAP)),
    ("armour", _armour),
    ("evasion", _plain("evasion", "闪避值", "Evasion",
                       note="闪避率上限 95%（`game_constant.pobMisc.EvadeChanceCap`）；"
                            "闪避**值**本身没有上限")),
    ("block", _capped("block", "格挡", "BlockChance", constants.BLOCK_CHANCE_CAP)),
    ("flasks", _flasks),
    ("reservation", _plain("reservation", "保留", "ManaReserved", unit="")),
    ("curses", _curses),
    ("pantheon", _pantheon),
    ("jewels", _jewels),
    ("passive_tree", _passive_tree),
    ("ascendancy", _ascendancy),
)


def uncovered_requirements() -> tuple[str, ...]:
    """需求 8.4 里**还没有任何检查覆盖**的项目。空元组才算「不得遗漏」达标。

    单测调它。判据落在需求那一侧：新增检查忘了登记 `_COVERS`、
    或者删掉某项检查，这里都会立刻报出来。
    """
    covered = {_COVERS.get(key, "") for key, _fn in _CHECKS}
    return tuple(item for item in REQUIREMENT_8_4_ITEMS if item not in covered)


def build_health_report(conn, build: PobBuild) -> BuildReport:  # noqa: ANN001
    """按需求 8.4 逐项检查。缺的标「未知」，**不填估计值**。"""
    report = BuildReport(items=[fn(conn, build) for _key, fn in _CHECKS])
    report.notes.append(
        "报告里的数值**取自你粘贴的 build**（PoB 自己算的），本库只做核对与判读，"
        "**没有重算任何一项**；判据（上限、规则）来自本库的游戏常量表，逐条注明出处。"
    )
    if report.unknown_count:
        report.notes.append(
            f"其中 {report.unknown_count} 项标为「未知」：这份 build 里没有对应数值，"
            "本库**不据此估计**（需求 8.4.7）。"
        )
    return report
