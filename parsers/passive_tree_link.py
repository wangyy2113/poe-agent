"""官方 PoE1 天赋树链接解析。

二进制格式逐字移植自 PoB `Classes/PassiveSpec.lua:DecodeURL/EncodeURL`：4 字节格式版本、
职业、升华位域、普通节点、星团节点、可选的大师效果。只接受 pathofexile.com 官方域名，
全程本地解码，不抓取用户提供的 URL。
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from parsers.pob_build import PobBuild


class PassiveTreeLinkError(ValueError):
    pass


@dataclass(frozen=True)
class PassiveTreeLink:
    serialization_version: int
    tree_version: str
    class_id: int
    ascendancy_id: int
    secondary_ascendancy_id: int
    nodes: tuple[str, ...]
    cluster_nodes: tuple[str, ...]
    mastery_effects: tuple[tuple[str, str], ...]


_CLASS_NAMES = {
    0: "Scion",
    1: "Marauder",
    2: "Ranger",
    3: "Witch",
    4: "Duelist",
    5: "Templar",
    6: "Shadow",
}
_PATH_MARKERS = {"passive-skill-tree", "fullscreen-passive-skill-tree"}
_TREE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.\d+)?$")
_PAYLOAD_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


def looks_like_passive_tree_link(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return (
        (parsed.hostname or "").lower().removeprefix("www.") == "pathofexile.com"
        and any(part in _PATH_MARKERS for part in parsed.path.split("/"))
    )


def _parts(link: str) -> tuple[str, str]:
    parsed = urlparse(link.strip() if "://" in link else f"https://{link.strip()}")
    if parsed.scheme not in {"http", "https"}:
        raise PassiveTreeLinkError("天赋树链接只支持 http/https")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host != "pathofexile.com":
        raise PassiveTreeLinkError("只支持 pathofexile.com 的官方 PoE1 天赋树链接")
    parts = [part for part in parsed.path.split("/") if part]
    marker_index = next((i for i, part in enumerate(parts) if part in _PATH_MARKERS), None)
    if marker_index is None or marker_index + 1 >= len(parts):
        raise PassiveTreeLinkError("链接里缺少天赋树编码")
    tail = parts[marker_index + 1:]
    tree_version = ""
    if len(tail) >= 2 and (matched := _TREE_VERSION_RE.match(tail[0])):
        tree_version = f"{matched.group(1)}.{matched.group(2)}"
        tail = tail[1:]
    elif len(tail) >= 2 and tail[0] in {"ruthless", "ruthless-alternate", "alternate"}:
        # 这些官方路径没有赛季号，不能从模式名反推版本。
        tail = tail[1:]
    payload = tail[0] if tail else ""
    if not payload or not _PAYLOAD_RE.fullmatch(payload):
        raise PassiveTreeLinkError("天赋树编码不是合法的 URL-safe Base64")
    return payload, tree_version


def _take_u16(data: bytes, offset: int, count: int) -> tuple[tuple[int, ...], int]:
    end = offset + count * 2
    if end > len(data):
        raise PassiveTreeLinkError("天赋树编码被截断：节点数量超过剩余数据")
    return tuple(int.from_bytes(data[i:i + 2], "big") for i in range(offset, end, 2)), end


def parse_passive_tree_link(link: str) -> PassiveTreeLink:
    payload, tree_version = _parts(link)
    try:
        data = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PassiveTreeLinkError(f"天赋树 Base64 解码失败：{exc}") from exc
    if len(data) < 6:
        raise PassiveTreeLinkError("天赋树编码太短，至少需要 6 字节头部")

    version = int.from_bytes(data[:4], "big")
    if version > 6:
        raise PassiveTreeLinkError(f"不支持的天赋树编码版本 {version}（当前最高为 6）")
    class_id = data[4]
    if class_id not in _CLASS_NAMES:
        raise PassiveTreeLinkError(f"未知职业编号 {class_id}")
    ascendancy_bits = data[5] if version >= 4 else 0
    ascendancy_id = ascendancy_bits & 3
    secondary_ascendancy_id = (ascendancy_bits & 12) >> 2

    cluster_nodes: tuple[int, ...] = ()
    mastery_effects: tuple[tuple[str, str], ...] = ()
    if version >= 5:
        if len(data) < 7:
            raise PassiveTreeLinkError("天赋树编码缺少节点数量")
        nodes, offset = _take_u16(data, 7, data[6])
        if offset >= len(data):
            raise PassiveTreeLinkError("天赋树编码缺少星团节点数量")
        cluster_nodes_raw, offset = _take_u16(data, offset + 1, data[offset])
        cluster_nodes = tuple(node + 65536 for node in cluster_nodes_raw)
        if version >= 6:
            if offset >= len(data):
                raise PassiveTreeLinkError("天赋树编码缺少大师效果数量")
            mastery_count = data[offset]
            offset += 1
            end = offset + mastery_count * 4
            if end > len(data):
                raise PassiveTreeLinkError("天赋树编码被截断：大师效果数量超过剩余数据")
            mastery_effects = tuple(
                (
                    str(int.from_bytes(data[i + 2:i + 4], "big")),
                    str(int.from_bytes(data[i:i + 2], "big")),
                )
                for i in range(offset, end, 4)
            )
            offset = end
        if offset != len(data):
            raise PassiveTreeLinkError(f"天赋树编码末尾有 {len(data) - offset} 个无法解释的字节")
    else:
        offset = 7 if version >= 4 else 6
        if (len(data) - offset) % 2:
            raise PassiveTreeLinkError("旧版天赋树节点数据不是完整的 2 字节序列")
        nodes, offset = _take_u16(data, offset, (len(data) - offset) // 2)

    return PassiveTreeLink(
        serialization_version=version,
        tree_version=tree_version,
        class_id=class_id,
        ascendancy_id=ascendancy_id,
        secondary_ascendancy_id=secondary_ascendancy_id,
        nodes=tuple(str(node) for node in nodes),
        cluster_nodes=tuple(str(node) for node in cluster_nodes),
        mastery_effects=mastery_effects,
    )


def passive_tree_build(link: str) -> tuple[PobBuild, PassiveTreeLink]:
    """把树链接转换成可复用现有报告链路的部分 Build。"""
    tree = parse_passive_tree_link(link)
    build = PobBuild(
        class_name=_CLASS_NAMES[tree.class_id],
        tree_version=tree.tree_version,
        passive_nodes=(*tree.nodes, *tree.cluster_nodes),
    )
    build.warnings.append(
        "输入仅包含天赋树：装备、技能、配置与 PlayerStat 均未提供，相关体检项会显示未知。"
    )
    if tree.ascendancy_id:
        build.warnings.append(
            f"链接只提供升华编号 {tree.ascendancy_id}；未在跨版本情况下猜测升华名称。"
        )
    return build, tree
