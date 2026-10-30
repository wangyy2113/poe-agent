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
不得冒充本库重算结果。下游只有在显式请求有界计算时才能把白名单字段作为输入，
并必须逐项披露采用字段；其它场景里它仍是报告值与对账口径。

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
MAX_SHARE_CODE_CHARS = 7_000_000
MAX_DECOMPRESSED_XML_BYTES = 5_000_000


@dataclass(frozen=True)
class BuildGem:
    """技能组里的一颗宝石。`name_en` 是 PoB 的 `nameSpec`（英文）。"""

    name_en: str
    level: int | None = None
    quality: int | None = None
    enabled: bool = True
    #: PoB XML 里的内部技能 id；用于区分主动技能与辅助宝石，不拿名字猜。
    skill_id: str = ""
    is_support: bool = False
    #: 桥接到 `pob_skill_gem` 的结果；桥不上就是 None（**留原文，不猜**）
    gem_id: str | None = None
    name_zh: str = ""


@dataclass(frozen=True)
class SkillGroup:
    """一个技能组（一个插槽里连在一起的宝石）。"""

    slot: str = ""
    enabled: bool = True
    is_main: bool = False
    #: `mainActiveSkill` 是本组“主动技能列表”里的 1-based 下标，不是全部宝石下标。
    active_skill_index: int | None = None
    #: `mainActiveSkillCalcs` 是 Calcs 页在本组内选中的主动技能下标。
    active_skill_calcs_index: int | None = None
    gems: tuple[BuildGem, ...] = ()

    @property
    def active_gem(self) -> BuildGem | None:
        active = [gem for gem in self.gems if gem.enabled and not gem.is_support]
        index = self.active_skill_index or 1
        return active[index - 1] if 0 < index <= len(active) else None

    @property
    def calcs_active_gem(self) -> BuildGem | None:
        active = [gem for gem in self.gems if gem.enabled and not gem.is_support]
        index = self.active_skill_calcs_index or self.active_skill_index or 1
        return active[index - 1] if 0 < index <= len(active) else None


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
    #: `<Build mainSocketGroup>`：PoB 左侧栏选作 Build 主技能的技能组（1-based）。
    main_socket_group: int | None = None
    #: `<Calcs><Input name="skill_number">`：生成 PlayerStat 时选中的技能组（1-based）。
    calcs_skill_group: int | None = None
    #: 当前激活天赋方案的树版本（规范成 `3.29`）；这是 Build 游戏版本的可核对代理。
    tree_version: str = ""
    #: 版本值来自哪里；非 PoB 输入必须显式覆盖，避免报告谎称来自 `<Tree><Spec>`。
    tree_version_basis: str = ""
    #: 天赋节点来自哪里；非 PoB 输入必须显式覆盖。
    passive_nodes_basis: str = ""
    #: 所有 PlayerStat 的原始文本；覆盖范围字段中的 `All` 等非数字值。
    player_stat_text: dict[str, str] = field(default_factory=dict)
    #: 面板数值的来源口径。空值表示标准 PoB `<PlayerStat>`；截图等输入必须显式覆盖，
    #: 避免报告把视觉转录误称为 PoB 自己算出的快照。
    player_stats_basis: str = ""
    #: 装备、技能、万神殿等结构是否是完整快照。官方树链接与单张截图只是部分输入，
    #: 缺少对象时只能报未知，不能把“没拍到”说成“没有”。
    structure_complete: bool = True
    structure_basis: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def main_group(self) -> SkillGroup | None:
        return next((g for g in self.skill_groups if g.is_main), None)

    @property
    def calcs_group(self) -> SkillGroup | None:
        index = self.calcs_skill_group
        if index is not None and 0 < index <= len(self.skill_groups):
            return self.skill_groups[index - 1]
        return self.main_group


def decode_share_code(code: str) -> str:
    """分享码 → XML 文本。三种失败各给各的原因。"""
    text = (code or "").strip()
    if not text:
        raise PobParseError("分享码为空")
    # 允许用户直接粘 pobb.in 链接时给出明确指引，而不是丢一个 base64 报错
    if "://" in text or text.startswith("www."):
        raise PobParseError(
            "这看起来是链接。本函数只解码**分享码本身**；链接要先经 "
            "`parsers.pob_link.resolve_download_url` 过白名单再抓回原文"
            "（`/build` 端点已经这么做了）"
        )
    if len(text) > MAX_SHARE_CODE_CHARS:
        raise PobParseError("分享码超过安全长度上限")
    if not _CODE_RE.match(text):
        raise PobParseError("不是合法的分享码：含 base64 之外的字符，可能是粘贴时混入了别的内容")
    try:
        raw = base64.b64decode(text.replace("-", "+").replace("_", "/") + "==")
    except (binascii.Error, ValueError) as exc:
        raise PobParseError(f"base64 解码失败：{exc}") from exc
    try:
        decompressor = zlib.decompressobj()
        xml = decompressor.decompress(raw, MAX_DECOMPRESSED_XML_BYTES + 1)
    except zlib.error as exc:
        raise PobParseError(
            f"zlib 解压失败：{exc}。分享码是 base64 但不是 PoB 的压缩格式"
        ) from exc
    if len(xml) > MAX_DECOMPRESSED_XML_BYTES or decompressor.unconsumed_tail:
        raise PobParseError("PoB XML 解压后超过 5 MB 安全上限")
    if not decompressor.eof or decompressor.unused_data:
        raise PobParseError("zlib 数据不完整或含有额外内容")
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


def _parse_skills(root: ET.Element, main_socket_group: int | None) -> list[SkillGroup]:
    groups: list[SkillGroup] = []
    node = root.find("Skills")
    if node is None:
        return groups
    skill_nodes = node.findall("Skill")
    if not skill_nodes:
        # 当前 PoB 把技能组包在多个 `<SkillSet>` 中；PlayerStat 与主技能下标都绑定
        # 当前激活的那一套。旧版则把 `<Skill>` 直接放在 `<Skills>` 下。
        active_set = node.get("activeSkillSet") or "1"
        skill_set = next(
            (item for item in node.findall("SkillSet") if item.get("id") == active_set),
            None,
        )
        if skill_set is None:
            skill_set = node.find("SkillSet")
        skill_nodes = skill_set.findall("Skill") if skill_set is not None else []
    for group_index, skill in enumerate(skill_nodes, start=1):
        gems = tuple(
            BuildGem(
                name_en=(g.get("nameSpec") or "").strip(),
                level=_int_or_none(g.get("level")),
                quality=_int_or_none(g.get("quality")),
                enabled=(g.get("enabled") or "true").lower() != "false",
                skill_id=g.get("skillId") or "",
                is_support=(g.get("skillId") or "").startswith("Support")
                or "SupportGem" in (g.get("gemId") or ""),
            )
            for g in skill.findall("Gem")
            if (g.get("nameSpec") or "").strip()
        )
        groups.append(SkillGroup(
            slot=skill.get("slot") or "",
            enabled=(skill.get("enabled") or "true").lower() != "false",
            is_main=group_index == main_socket_group,
            active_skill_index=_int_or_none(skill.get("mainActiveSkill")),
            active_skill_calcs_index=_int_or_none(skill.get("mainActiveSkillCalcs")),
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
        main_socket_group=_int_or_none(build_node.get("mainSocketGroup")),
    )
    for stat in build_node.findall("PlayerStat"):
        name, value = stat.get("stat"), stat.get("value")
        if not name:
            continue
        if value is not None:
            out.player_stat_text[name] = value
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
    calcs_skill = root.find("Calcs/Input[@name='skill_number']")
    if calcs_skill is not None:
        out.calcs_skill_group = _int_or_none(calcs_skill.get("number"))
    out.skill_groups = _parse_skills(root, out.main_socket_group)
    out.items = _parse_items(root, out.warnings)

    # 天赋树与珠宝都在 `<Tree><Spec>` 里：`nodes` 是已分配节点，
    # **珠宝不在 `Items` 的 Slot 里**——它们插在树的 `<Sockets>` 上
    # （实测 OccVortex：Slot 里一件珠宝都没有，树上有 3 颗）。
    tree = root.find("Tree")
    spec = None
    if tree is not None:
        specs = tree.findall("Spec")
        active_spec = _int_or_none(tree.get("activeSpec")) or 1
        if 0 < active_spec <= len(specs):
            spec = specs[active_spec - 1]
        elif specs:
            spec = specs[0]
    if spec is not None:
        out.tree_version = (spec.get("treeVersion") or "").replace("_", ".")
        out.passive_nodes = tuple(
            n.strip() for n in (spec.get("nodes") or "").split(",") if n.strip()
        )
        sockets = spec.find("Sockets")
        if sockets is not None:
            out.tree_jewels = sum(
                1 for s in sockets.findall("Socket")
                if (s.get("itemId") or "0") != "0"
            )
    missing_sections = [
        name for name, present in (
            ("Items", root.find("Items") is not None),
            ("Skills", root.find("Skills") is not None),
            ("Tree/Spec", spec is not None),
        ) if not present
    ]
    if missing_sections:
        out.structure_complete = False
        out.structure_basis = "PoB XML 缺少 " + "、".join(missing_sections)
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
                enabled=gem.enabled, skill_id=gem.skill_id, is_support=gem.is_support,
                gem_id=hit[0], name_zh=hit[1],
            ))
        groups.append(SkillGroup(
            slot=group.slot, enabled=group.enabled,
            is_main=group.is_main, active_skill_index=group.active_skill_index,
            active_skill_calcs_index=group.active_skill_calcs_index,
            gems=tuple(gems),
        ))
    build.skill_groups = groups
    if missing:
        uniq = sorted(set(missing))
        build.warnings.append(
            f"{len(uniq)} 颗宝石未能与本库桥接，已保留英文原文未做进一步分析："
            f"{'、'.join(uniq[:5])}{' 等' if len(uniq) > 5 else ''}"
        )
    return build
