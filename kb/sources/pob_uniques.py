"""从 PoB 的 `Uniques/*.lua` 抽取唯一装备。

文本块形如：

    The Anvil
    Amber Amulet
    Variant: Pre 1.3.0
    Variant: Pre 3.19.0
    Variant: Current
    Requires Level 45
    Implicits: 1
    {tags:attribute}+(20-30) to Strength
    {variant:1}10% Chance to Block Attack Damage
    {variant:2,3}(10-15)% Chance to Block Attack Damage

# 变体过滤是这个模块的核心，也是最容易出错的地方

**历史版本与当前版本混在同一个文本块里。** 1293 件里 752 件（58%）带 `Variant:`
标记。冈姆的壮志同时写着 `{variant:2}+500 to maximum Life`（3.25 之前）与
`{variant:1,3}+1000 to maximum Life`（当前）。不过滤就会同时输出两个值——
这正是「输出错误内容的代价高于输出查不到」要拦的情况。

**但「只取最后一个」或「只取名叫 Current 的」都不对**（实测 57 件反例）：
有些装备的 variant 不是历史版本而是**选择型**，全都是当前有效的：

    Impresence            ['Physical','Fire','Cold','Lightning','Chaos']   5 种元素
    Aul's Uprising        17 种光环
    Doryani's Invitation  ['Pre 3.11.0 (Fire)', ..., 'Current (Fire)', ...]  混合
    Hyperboreus           ['Pre 3.11.0 (Life Regen)', 'Fire and Chaos Resistances (Current)']
    Eyes of the Greatwolf 33 种，其中一个是 'Item Quantity (Pre 3.25.0)'

正确规则：**凡 variant 名里含 `Pre <版本号>` 即视为历史版本排除，其余全部保留。**
这条规则在上面六种形态上都成立（含 `X (Current)` 与 `Y (Pre 3.25.0)` 这种
把标记放在括号里的）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kb.sources.pob_lua import iter_long_strings, load_lua_file, parse_assignments
from kb.sources.poedb_mods import parse_ranges, templatize

logger = logging.getLogger(__name__)

UNIQUES_DIR = "Data/Uniques"

#: 历史版本标记。`Pre 3.11.0`、`Pre 2.6`、`(Pre 3.25.0)` 都算。
_PRE_VERSION_RE = re.compile(r"\bPre\s+\d+\.\d+(?:\.\d+)?", re.I)

#: 行内前缀 `{variant:1,3}`、`{tags:attribute}`、`{crafted}`、`{fractured}`、`{group:x}`
_PREFIX_RE = re.compile(r"\{([a-z_]+)(?::([^}]*))?\}")

#: 元数据行 `Key: value`。键首字母大写、允许空格。
_META_RE = re.compile(r"^([A-Z][A-Za-z ]{1,28}):\s*(.*)$")
#: `Requires Level 45` 这种不带冒号的写法
_REQ_LEVEL_RE = re.compile(r"^Requires Level\s+(\d+)\s*$", re.I)

#: 不进入词缀列表的元数据键。列全是为了让「没见过的键」能被发现——
#: 静默当成词缀会往知识库里写进一条假词条。
_META_KEYS = frozenset({
    "Variant", "Implicits", "Source", "League", "LevelReq", "Limited to",
    "Radius", "Upgrade", "Requires Level", "Has Alt Variant", "Selected Variant",
    "Selected Alt Variant", "Talisman Tier", "Sockets", "Has Alt Variant Two",
    "Crafted", "Has Alt Variant Three", "Selected Alt Variant Two",
    "Selected Alt Variant Three", "Energy Shield", "Suffix", "Item Level",
    "Left Ring slot", "Right Ring slot",
    # 职业名：升华专属唯一装备会写 `Duelist: ...`
    "Duelist", "Ranger", "Shadow", "Witch", "Templar", "Scion", "Marauder",
})


@dataclass
class PoBUniqueMod:
    """一条词缀。`text` 已去掉全部 `{...}` 前缀。"""

    text: str
    raw: str
    #: 适用的 variant 下标（1-based）。空 = 适用全部 variant。
    variants: tuple[int, ...] = ()
    tags: tuple[str, ...] = ()
    crafted: bool = False
    fractured: bool = False
    #: 是否隐式词缀（按 `Implicits: N` 取前 N 条非元数据行）
    is_implicit: bool = False

    @property
    def kind(self) -> str:
        if self.is_implicit:
            return "implicit"
        if self.crafted:
            return "crafted"
        if self.fractured:
            return "fractured"
        return "explicit"

    @property
    def ranges(self) -> list[tuple[float, float]]:
        return parse_ranges(self.text)

    @property
    def template(self) -> str:
        return templatize(self.text)

    def applies_to(self, keep: set[int]) -> bool:
        """无 variant 标记 = 适用全部；有标记则需与保留集合有交集。"""
        return not self.variants or bool(set(self.variants) & keep)


@dataclass
class BaseTypeLine:
    """一条底材行。`variants` 空 = 适用全部变体。"""

    name: str
    variants: tuple[int, ...] = ()


@dataclass
class PoBUnique:
    name_en: str
    source_file: str
    #: 底材行列表。多于一条时按变体选（不同版本可能换过底材）
    base_types: list[BaseTypeLine] = field(default_factory=list)
    variant_names: list[str] = field(default_factory=list)
    league: str = ""
    drop_source: str = ""
    level_req: int | None = None
    implicit_count: int = 0
    limited_to: int | None = None
    radius: str = ""
    all_mods: list[PoBUniqueMod] = field(default_factory=list)
    unknown_meta: list[str] = field(default_factory=list)

    # ── 变体划分 ──

    @property
    def current_variants(self) -> list[int]:
        """当前有效的 variant 下标（1-based）。无 variant 时返回空列表。"""
        return [
            i for i, name in enumerate(self.variant_names, 1)
            if not _PRE_VERSION_RE.search(name)
        ]

    @property
    def obsolete_variants(self) -> list[int]:
        return [
            i for i, name in enumerate(self.variant_names, 1)
            if _PRE_VERSION_RE.search(name)
        ]

    @property
    def has_variants(self) -> bool:
        return bool(self.variant_names)

    # ── 底材 ──

    @property
    def base_type_en(self) -> str:
        """当前变体的底材名。

        多底材行时按变体选：`Ashcaller` 的 variant 5 是 `Goat's Horn`，
        取第一行会拿到最旧版本的 `Quartz Wand`。
        """
        if not self.base_types:
            return ""
        keep = set(self.current_variants)
        if keep:
            for bt in self.base_types:
                if bt.variants and set(bt.variants) & keep:
                    return bt.name
        # 无变体标记的那行（或唯一一行）
        for bt in self.base_types:
            if not bt.variants:
                return bt.name
        return self.base_types[0].name

    @property
    def base_type_raw(self) -> str:
        return " | ".join(bt.name for bt in self.base_types)

    def implicit_count_for(self, scope: str = "current") -> int:
        """按变体过滤后的隐式词缀条数。

        `Implicits: N` 数的是**行数**，而那 N 行可能是同一条隐式的多个变体版本：
        `Ashcaller` 写 `Implicits: 3`，但三行分别属于 variant 1-3 / 4 / 5，
        当前变体下只有 1 条。直接拿 3 去跟 POEDB 比必然不符。
        """
        return sum(1 for m in self.mods_for(scope) if m.kind == "implicit")

    def mods_for(self, scope: str = "current") -> list[PoBUniqueMod]:
        """按变体范围取词缀。

        `scope="current"` 是唯一可用于回答的集合。`"obsolete"` 只用于版本 diff。
        """
        if not self.has_variants:
            return list(self.all_mods) if scope == "current" else []
        keep = set(self.current_variants if scope == "current" else self.obsolete_variants)
        if not keep:
            return []
        if scope == "obsolete":
            # 历史版本集合要排除同时适用于当前版本的行，否则「当前也有的词缀」
            # 会被同时算进历史，版本 diff 就全是噪声
            current = set(self.current_variants)
            return [
                m for m in self.all_mods
                if m.variants and set(m.variants) & keep and not (set(m.variants) & current)
            ]
        return [m for m in self.all_mods if m.applies_to(keep)]

    # ── 派生 ──

    @property
    def uid(self) -> str:
        """主键必须带底材（实测踩到）。

        同名唯一装备靠底材区分：`Grand Spectrum` 有 Cobalt / Crimson / Viridian
        三个珠宝版本，`Doryani's Delusion` 有三种靴子底材，`Combat Focus` 同理。
        只用名字做 uid 会让三条撞成一条，UPSERT 后只剩最后一件。
        """
        return f"unique:{self.name_en}|{self.base_type_en}"

    def ranges_fingerprint(self, scope: str = "current") -> list[list[list[float]]]:
        """区间指纹：逐条词缀的区间序列。纯数字，跨语言可比。"""
        return [
            [[a, b] for a, b in m.ranges]
            for m in self.mods_for(scope)
        ]

    def counts(self, scope: str = "current") -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.mods_for(scope):
            out[m.kind] = out.get(m.kind, 0) + 1
        return out


# ── 解析 ───────────────────────────────────────────────────

def parse_block(
    block: str, source_file: str, known_bases: set[str] | None = None
) -> PoBUnique | None:
    """解析一个文本块。结构不符预期返回 None 并告警（需求 2.7）。"""
    lines = [ln.rstrip() for ln in block.strip().splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    if len(lines) < 2:
        logger.warning("%s: 文本块行数不足，跳过：%r", source_file, block[:60])
        return None

    # 底材可能有**多行**，各带 variant 前缀（实测踩到）：
    #
    #   Ashcaller
    #   {variant:1,2,3}Quartz Wand
    #   {variant:4}Carved Wand
    #   {variant:5}Goat's Horn      ← 当前变体的底材
    #
    # 只取第一行会拿到最旧版本的底材（Quartz Wand），而 POEDB 显示的是
    # 当前的 Goat's Horn。更糟的是多出来的底材行会被当成词缀：
    # Twyzel 的 `Blasting Wand` 就曾变成一条隐式词缀。
    base_lines, body = _split_base_lines(lines[1:], known_bases)
    item = PoBUnique(
        name_en=lines[0].strip(),
        base_types=base_lines,
        source_file=source_file,
    )
    mod_lines: list[str] = []
    for line in body:
        s = line.strip()
        rl = _REQ_LEVEL_RE.match(s)
        if rl:
            item.level_req = int(rl.group(1))
            continue
        m = _META_RE.match(s)
        if m and m.group(1) in _META_KEYS:
            _apply_meta(item, m.group(1), m.group(2).strip())
            continue
        if m and not s.startswith("{"):
            # 形如 `Key: value` 但键没登记。可能是 PoB 新增的元数据，也可能
            # 是真的词缀文本里带冒号。记下来人工判断，先当词缀处理。
            item.unknown_meta.append(s[:80])
        mod_lines.append(line)

    for idx, line in enumerate(mod_lines):
        mod = _parse_mod_line(line)
        if mod is None:
            continue
        mod.is_implicit = idx < item.implicit_count
        item.all_mods.append(mod)

    if not item.all_mods:
        logger.debug("%s: %s 没有词缀行", source_file, item.name_en)
    return item


def load_base_names(pob_root: Path) -> set[str]:
    """从 `Data/Bases/*.lua` 读出全部合法底材名（约 1100 个）。

    用它当底材行的判据，而不是靠启发式。实测两种启发式都失败过：
      「无数字」  → `Elder Item`（影响力标记）与 `Onslaught`（词缀）被当成底材，
                   Thrillsteel 因此丢掉全部词缀
      「第一行」  → `Ashcaller` 拿到最旧版本的 `Quartz Wand` 而非当前 `Goat's Horn`
    底材名在册这条是精确的，顺带还校验了 PoB 自己两份数据的一致性。
    """
    names: set[str] = set()
    base_dir = pob_root / "src" / "Data" / "Bases"
    if not base_dir.is_dir():
        logger.warning("找不到 %s，底材行判据退化为「只取第一行」", base_dir)
        return names
    for path in sorted(base_dir.glob("*.lua")):
        try:
            names |= {str(k) for k in parse_assignments(load_lua_file(path))}
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取底材表 %s 失败：%s", path.name, exc)
    return names


def _split_base_lines(
    rest: list[str], known_bases: set[str] | None
) -> tuple[list[BaseTypeLine], list[str]]:
    """把开头的底材行与其余内容分开。

    判据：从第一行起，只要剥掉 `{...}` 前缀后的文本**在底材名册里**就算底材行，
    遇到第一个不在册的就停。底材名册为空时退化为只取第一行——宁可少取，
    多取会把词缀吞掉（那会让知识库丢词条，比丢底材名严重）。
    """
    bases: list[BaseTypeLine] = []
    for idx, line in enumerate(rest):
        s = line.strip()
        if _META_RE.match(s) or _REQ_LEVEL_RE.match(s):
            break
        stripped = _PREFIX_RE.sub("", s).strip()
        if not stripped:
            break
        if known_bases:
            if stripped not in known_bases:
                break
        elif bases:
            # 没有名册时只认第一行
            break
        variants: list[int] = []
        for m in _PREFIX_RE.finditer(s):
            if m.group(1) == "variant":
                for part in re.split(r"[,，]", m.group(2) or ""):
                    if part.strip().isdigit():
                        variants.append(int(part.strip()))
        bases.append(BaseTypeLine(name=stripped, variants=tuple(variants)))
    else:
        idx = len(rest)
    return bases or [BaseTypeLine(name="")], rest[len(bases):]


def _apply_meta(item: PoBUnique, key: str, value: str) -> None:
    if key == "Variant":
        item.variant_names.append(value)
    elif key == "Implicits":
        try:
            item.implicit_count = int(value)
        except ValueError:
            logger.warning("%s: Implicits 不是整数：%r", item.name_en, value)
    elif key == "League":
        item.league = value
    elif key == "Source":
        item.drop_source = value
    elif key == "LevelReq":
        try:
            item.level_req = int(value)
        except ValueError:
            pass
    elif key == "Limited to":
        try:
            item.limited_to = int(value)
        except ValueError:
            pass
    elif key == "Radius":
        item.radius = value
    # 其余已登记键（Upgrade / Sockets / 职业名等）当前不入库，
    # 但已从词缀列表里剔除，不会污染词缀数据


def _parse_mod_line(line: str) -> PoBUniqueMod | None:
    variants: list[int] = []
    tags: list[str] = []
    crafted = fractured = False

    for m in _PREFIX_RE.finditer(line):
        kind, arg = m.group(1), (m.group(2) or "")
        if kind == "variant":
            for part in re.split(r"[,，]", arg):
                part = part.strip()
                if part.isdigit():
                    variants.append(int(part))
        elif kind == "tags":
            tags += [t.strip() for t in arg.split(",") if t.strip()]
        elif kind == "crafted":
            crafted = True
        elif kind == "fractured":
            fractured = True
        # {group:x} 是 PoB 内部的词缀分组标记，对知识库无用

    text = _PREFIX_RE.sub("", line).strip()
    if not text:
        return None
    return PoBUniqueMod(
        text=text,
        raw=line.strip(),
        variants=tuple(variants),
        tags=tuple(tags),
        crafted=crafted,
        fractured=fractured,
    )


def extract_all(pob_root: Path) -> tuple[list[PoBUnique], list[str]]:
    """抽取全部唯一装备。返回 (列表, 问题列表)。"""
    base = pob_root / "src" / UNIQUES_DIR
    problems: list[str] = []
    if not base.is_dir():
        return [], [f"{UNIQUES_DIR}：目录不存在"]

    known_bases = load_base_names(pob_root)
    if not known_bases:
        problems.append(
            "底材名册为空（Data/Bases 读不到），底材行判据退化为只取第一行；"
            "换过底材的装备会拿到旧版底材名"
        )

    items: list[PoBUnique] = []
    seen: dict[str, str] = {}
    empty_by_design: list[str] = []
    for path in sorted(base.glob("*.lua")):
        rel = f"{UNIQUES_DIR}/{path.name}"
        try:
            text = load_lua_file(path)
            blocks = list(iter_long_strings(text))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{rel}：读取失败 {type(exc).__name__}: {exc}")
            continue
        if not blocks:
            # 区分「上游这张表还是空的」与「格式变了解析不出来」：
            # 前者是 `return {}`（如 graft.lua），报警会变成长期噪声。
            if "[[" not in text:
                empty_by_design.append(rel)
            else:
                problems.append(f"{rel}：有文本块标记但没解析出内容，格式可能已变")
        for b in blocks:
            item = parse_block(b, rel, known_bases)
            if item is None:
                continue
            prev = seen.get(item.uid)
            if prev:
                # uid 已含底材，仍然撞说明是真重复，会被 UPSERT 吞掉，必须暴露
                problems.append(
                    f"{item.name_en}（{item.base_type_en}）：uid 重复（{prev} 与 {rel}）"
                )
            seen[item.uid] = rel
            items.append(item)

    if empty_by_design:
        logger.info("以下文件在上游还是空表（正常）：%s", ", ".join(empty_by_design))

    unknown: dict[str, int] = {}
    for it in items:
        for u in it.unknown_meta:
            key = u.split(":", 1)[0]
            unknown[key] = unknown.get(key, 0) + 1
    for key, n in sorted(unknown.items(), key=lambda kv: -kv[1])[:10]:
        problems.append(f"未登记的元数据键 `{key}`（{n} 处），当前按词缀处理，需确认")

    return items, problems


def to_row_mods(item: PoBUnique, scope: str) -> list[dict[str, Any]]:
    return [
        {
            "kind": m.kind,
            "text": m.text,
            "ranges": [[a, b] for a, b in m.ranges],
            "template": m.template,
            "variants": list(m.variants),
            "tags": list(m.tags),
        }
        for m in item.mods_for(scope)
    ]
