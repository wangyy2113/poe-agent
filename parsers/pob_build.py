"""PoB 分享码解析（第四十八轮，需求 8.4 的入口）。

分享码 → XML → 结构化 build 模型。**本模块只解析，不做分析**：需求 8.4 那 16 项
（DPS / Effective HP / Max Hit / 抗性 / … / 升华）是下一轮的事。

## 编码方式取自 PoB 源码，不是猜的

`Classes/ImportTab.lua:504` 导出：`base64.encode(Deflate(xml))`，再把 `+`→`-`、`/`→`_`；
`:631` 导入反过来。也就是 **zlib deflate + URL-safe base64**。
拿 `spec/TestBuilds/3.13/OccVortex.xml` 实测往返完全一致。

## `PlayerStat` 是 PoB 自己算的快照，不是我们算的

XML 里带着 73 条 `<PlayerStat>`（`Life 6728`、四抗 `75/75/75/68`、`TotalDot 566925`…）。
**这既不是知识库证据，也不是本库的计算结果**——它反映的是用户自己那份 PoB 的
版本与配置。所以它在模型里是**用户提供的数据**，出口必须写清「取自你粘贴的 build」，
不得与本库计算器的结论混排。下一轮做分析时，它的角色是**对账口径**：
我们算的抗性/EHP 可以直接与它比。

## 只接受分享码，不接受链接

`pobb.in` / `pastebin` 要联网，而抓取入口有 `kb/fetcher.reject_poe2_host()` 那套护栏
要一并考虑（POE1/POE2 边界由代码执行）。链接单独一轮做。
"""

from __future__ import annotations

import base64
import binascii
import re
import zlib
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from parsers.item_text import ItemParseError, ParsedItem, parse_item


class PobParseError(ValueError):
    """分享码或 XML 认不出。**给出可读原因**，不吞异常——
    「解析失败」这四个字对用户没有任何帮助。"""


#: 分享码里允许出现的字符。先判形状再解码，能把「粘错了东西」与
#: 「粘对了但内容坏了」分开报。
_CODE_RE = re.compile(r"^[A-Za-z0-9\-_=+/]+$")


@dataclass(frozen=True)
class BuildGem:
    """技能组里的一颗宝石。`name_en` 是 PoB 的 `nameSpec`（英文）。"""

    name_en: str
    level: int | None = None
    quality: int | None = None
    enabled: bool = True
    #: 桥接到 `pob_skill_gem` 的结果；桥不上就是 None（**留原文，不猜**）
    gem_id: str | None = None
    name_zh: str = ""


@dataclass(frozen=True)
class SkillGroup:
    """一个技能组（一个插槽里连在一起的宝石）。"""

    slot: str = ""
    enabled: bool = True
    is_main: bool = False
    gems: tuple[BuildGem, ...] = ()


@dataclass(frozen=True)
class BuildItem:
    """一件装备：部位 + 解析结果。解析失败时 `parsed` 为 None 并带 `error`。"""

    slot: str
    raw: str
    parsed: ParsedItem | None = None
    error: str = ""


@dataclass
class PobBuild:
    """一份 build 的结构化形态。"""

    level: int | None = None
    class_name: str = ""
    ascend_class_name: str = ""
    bandit: str = ""
    pantheon_major: str = ""
    pantheon_minor: str = ""
    skill_groups: list[SkillGroup] = field(default_factory=list)
    items: list[BuildItem] = field(default_factory=list)
    #: `<Config><Input>`：这份数值是在什么假设下算出来的（需求 8.8 要求假设显式写出）
    config: dict[str, Any] = field(default_factory=dict)
    #: `<PlayerStat>`：**PoB 自己算的快照**，见模块 docstring
    player_stats: dict[str, float] = field(default_factory=dict)
    #: `<Tree><Spec nodes="...">` 里已分配的天赋节点 id
    passive_nodes: tuple[str, ...] = ()
    #: 树上**插着珠宝**的插槽数（珠宝不在 `Items` 的 Slot 里，在树的 `<Sockets>` 里）
    tree_jewels: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def main_group(self) -> SkillGroup | None:
        return next((g for g in self.skill_groups if g.is_main), None)


def decode_share_code(code: str) -> str:
    """分享码 → XML 文本。三种失败各给各的原因。"""
    text = (code or "").strip()
    if not text:
        raise PobParseError("分享码为空")
    # 允许用户直接粘 pobb.in 链接时给出明确指引，而不是丢一个 base64 报错
    if "://" in text or text.startswith("www."):
        raise PobParseError(
            "这看起来是链接。本版只接受**分享码本身**（PoB 里 Import/Export → "
            "Generate 出来的那串字符）；链接需要联网抓取，尚未支持"
        )
    if not _CODE_RE.match(text):
        raise PobParseError("不是合法的分享码：含 base64 之外的字符，可能是粘贴时混入了别的内容")
    try:
        raw = base64.b64decode(text.replace("-", "+").replace("_", "/") + "==")
    except (binascii.Error, ValueError) as exc:
        raise PobParseError(f"base64 解码失败：{exc}") from exc
    try:
        xml = zlib.decompress(raw)
    except zlib.error as exc:
        raise PobParseError(
            f"zlib 解压失败：{exc}。分享码是 base64 但不是 PoB 的压缩格式"
        ) from exc
    try:
        return xml.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PobParseError(f"解压出来的不是 UTF-8 文本：{exc}") from exc


def encode_share_code(xml: str) -> str:
    """XML → 分享码。与 `decode_share_code` 成对，单测用它做往返。"""
    packed = zlib.compress(xml.encode("utf-8"))
    return base64.b64encode(packed).decode().replace("+", "-").replace("/", "_")


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _parse_skills(root: ET.Element) -> list[SkillGroup]:
    groups: list[SkillGroup] = []
    node = root.find("Skills")
    if node is None:
        return groups
    for skill in node.findall("Skill"):
        gems = tuple(
            BuildGem(
                name_en=(g.get("nameSpec") or "").strip(),
                level=_int_or_none(g.get("level")),
                quality=_int_or_none(g.get("quality")),
                enabled=(g.get("enabled") or "true").lower() != "false",
            )
            for g in skill.findall("Gem")
            if (g.get("nameSpec") or "").strip()
        )
        groups.append(SkillGroup(
            slot=skill.get("slot") or "",
            enabled=(skill.get("enabled") or "true").lower() != "false",
            is_main=skill.get("mainActiveSkill") not in (None, "", "nil"),
            gems=gems,
        ))
    return groups


def _parse_items(root: ET.Element, warnings: list[str]) -> list[BuildItem]:
    node = root.find("Items")
    if node is None:
        return []
    # `<Slot name="Gloves" itemId="7"/>` → 部位映射。没有 Slot 的走 `Item` 自己的序号。
    by_id = {
        (s.get("itemId") or ""): (s.get("name") or "")
        for s in node.findall("Slot")
    }
    out: list[BuildItem] = []
    for item in node.findall("Item"):
        raw = (item.text or "").strip()
        if not raw:
            continue
        slot = by_id.get(item.get("id") or "", "")
        try:
            parsed = parse_item(raw)
        except ItemParseError as exc:
            warnings.append(f"装备解析失败（部位 {slot or '未知'}）：{exc}")
            out.append(BuildItem(slot=slot, raw=raw, error=str(exc)))
            continue
        out.append(BuildItem(slot=slot, raw=raw, parsed=parsed))
    return out


def parse_build(xml: str) -> PobBuild:
    """XML → `PobBuild`。不是 PoB 的 XML 就抛 `PobParseError`。"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise PobParseError(f"XML 解析失败：{exc}") from exc
    if root.tag != "PathOfBuilding":
        raise PobParseError(
            f"根节点是 <{root.tag}> 而不是 <PathOfBuilding>，这不是一份 PoB build"
        )
    build_node = root.find("Build")
    if build_node is None:
        raise PobParseError("缺少 <Build> 节点")

    out = PobBuild(
        level=_int_or_none(build_node.get("level")),
        class_name=build_node.get("className") or "",
        ascend_class_name=build_node.get("ascendClassName") or "",
        bandit=build_node.get("bandit") or "",
        pantheon_major=build_node.get("pantheonMajorGod") or "",
        pantheon_minor=build_node.get("pantheonMinorGod") or "",
    )
    for stat in build_node.findall("PlayerStat"):
        name, value = stat.get("stat"), stat.get("value")
        if not name:
            continue
        try:
            out.player_stats[name] = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    config = root.find("Config")
    if config is not None:
        for inp in config.findall("Input"):
            key = inp.get("name")
            if not key:
                continue
            out.config[key] = (
                inp.get("string") or inp.get("number") or inp.get("boolean") or ""
            )
    out.skill_groups = _parse_skills(root)
    out.items = _parse_items(root, out.warnings)

    # 天赋树与珠宝都在 `<Tree><Spec>` 里：`nodes` 是已分配节点，
    # **珠宝不在 `Items` 的 Slot 里**——它们插在树的 `<Sockets>` 上
    # （实测 OccVortex：Slot 里一件珠宝都没有，树上有 3 颗）。
    spec = root.find("Tree/Spec")
    if spec is not None:
        out.passive_nodes = tuple(
            n.strip() for n in (spec.get("nodes") or "").split(",") if n.strip()
        )
        sockets = spec.find("Sockets")
        if sockets is not None:
            out.tree_jewels = sum(
                1 for s in sockets.findall("Socket")
                if (s.get("itemId") or "0") != "0"
            )
    return out


def bridge_gems(conn, build: PobBuild) -> PobBuild:  # noqa: ANN001
    """把技能组里的宝石按**英文名**桥接到 `pob_skill_gem`。

    桥不上就**留原文并记 warning**，不猜：PoB 的 `nameSpec` 里有本库没有的条目
    （未桥接的历史/未上线宝石），硬配会把一颗宝石的数值挂到另一颗身上。
    """
    lookup: dict[str, tuple[str, str]] = {
        str(r["name_en"]).lower(): (str(r["gem_id"]), str(r["name_zh"] or ""))
        for r in conn.execute("SELECT gem_id, name_en, name_zh FROM pob_skill_gem")
    }
    missing: list[str] = []
    groups: list[SkillGroup] = []
    for group in build.skill_groups:
        gems: list[BuildGem] = []
        for gem in group.gems:
            hit = lookup.get(gem.name_en.lower())
            if hit is None:
                missing.append(gem.name_en)
                gems.append(gem)
                continue
            gems.append(BuildGem(
                name_en=gem.name_en, level=gem.level, quality=gem.quality,
                enabled=gem.enabled, gem_id=hit[0], name_zh=hit[1],
            ))
        groups.append(SkillGroup(
            slot=group.slot, enabled=group.enabled,
            is_main=group.is_main, gems=tuple(gems),
        ))
    build.skill_groups = groups
    if missing:
        uniq = sorted(set(missing))
        build.warnings.append(
            f"{len(uniq)} 颗宝石未能与本库桥接，已保留英文原文未做进一步分析："
            f"{'、'.join(uniq[:5])}{' 等' if len(uniq) > 5 else ''}"
        )
    return build
