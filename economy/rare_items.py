"""稀有装备的官方 Trade API 可比挂单估值。

这不是成交价模型。服务只把装备文本中能被本地 PoB ``tradeHashes`` 与 GGG 当前
stat 字典双重确认的显式词缀，作为“存在性”筛选条件；再限定精确底材、物等、
腐化状态和在线联盟，返回可比挂单的稳健区间。无法映射的词缀必须逐条披露。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from economy.trade_api import SOURCE, TradeApiClient, TradeQuote
from entities.taxonomy import ModKind
from kb.sources.poedb_mods import loose_templatize, strict_template_regex
from parsers.item_mods import ItemModReport, ModAnalysis, analyze_mods
from parsers.item_text import ItemParseError, ParsedItem, Rarity, parse_item


class InvalidRareComparable(ValueError):
    """输入不是当前保守契约能比较的稀有装备。"""


@dataclass(frozen=True)
class MappedTradeStat:
    mod_text: str
    stat_ids: tuple[str, ...]
    official_texts: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RareComparableEstimate:
    status: str
    reason: str
    league: str
    source: str
    source_url: str
    fetched_at: str
    base_type_input: str
    base_type_en: str
    item_level: int | None
    corrupted: bool
    explicit_mod_count: int
    mapped_mod_count: int
    comparison_quality: str
    quality_filter: int | None = None
    socket_group_sizes: tuple[int, ...] | None = None
    mapped_stats: tuple[MappedTradeStat, ...] = field(default_factory=tuple)
    unmapped_mods: tuple[str, ...] = field(default_factory=tuple)
    quote: TradeQuote | None = None

    @property
    def available(self) -> bool:
        return self.quote is not None and self.quote.sufficient

    def as_dict(self) -> dict:
        out = asdict(self)
        out["available"] = self.available
        out["socket_group_sizes"] = (
            list(self.socket_group_sizes) if self.socket_group_sizes is not None else None
        )
        out["mapped_stats"] = [item.as_dict() for item in self.mapped_stats]
        out["unmapped_mods"] = list(self.unmapped_mods)
        out["quote"] = asdict(self.quote) if self.quote is not None else None
        return out


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _hashes(raw: str | None) -> tuple[str, ...]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(values, list):
        return ()
    out: list[str] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            return ()
        out.append(f"explicit.stat_{value}")
    return tuple(dict.fromkeys(out))


def _values_fit(row: sqlite3.Row, analysis: ModAnalysis) -> bool:
    template = str(row["text_template"] or "")
    match = strict_template_regex(template).match(analysis.mod.lookup_text)
    if match is None:
        return False
    values = [float(value) for value in match.groups()]
    try:
        ranges = json.loads(row["ranges"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return False
    if len(values) != len(ranges):
        return False
    return all(
        isinstance(pair, list)
        and len(pair) == 2
        and all(isinstance(bound, (int, float)) for bound in pair)
        and min(float(pair[0]), float(pair[1]))
        <= value
        <= max(float(pair[0]), float(pair[1]))
        for value, pair in zip(values, ranges, strict=True)
    )


def _candidate_hash_sets(
    conn: sqlite3.Connection,
    item: ParsedItem,
    analysis: ModAnalysis,
) -> set[tuple[str, ...]]:
    """返回一个词缀能被证实的 hash 集合；多个不同集合即保持歧义。"""
    if analysis.ambiguous:
        return set()
    if item.language == "en":
        rows = conn.execute(
            "SELECT text_template,ranges,trade_hashes FROM pob_mod "
            "WHERE loose_template=? AND trade_hashes NOT IN ('', '[]')",
            (loose_templatize(analysis.mod.lookup_text),),
        ).fetchall()
        return {
            hashes
            for row in rows
            if _values_fit(row, analysis) and (hashes := _hashes(row["trade_hashes"]))
        }

    if not analysis.mod_group:
        return set()
    kind_by_category = {
        "normal": "explicit",
        "master": "master",
        "veiled": "veiled",
        "delve": "delve",
        "synthesis": "synthesis",
    }
    mod_kind = kind_by_category.get(analysis.category)
    if not mod_kind:
        return set()
    rows = conn.execute(
        "SELECT trade_hashes FROM pob_mod WHERE mod_group=? AND mod_kind=? "
        "AND trade_hashes NOT IN ('', '[]')",
        (analysis.mod_group, mod_kind),
    ).fetchall()
    return {hashes for row in rows if (hashes := _hashes(row["trade_hashes"]))}


def map_trade_stats(
    conn: sqlite3.Connection,
    item: ParsedItem,
    mod_report: ItemModReport,
    official_stats: dict[str, str],
) -> tuple[tuple[MappedTradeStat, ...], tuple[str, ...]]:
    """映射可搜索显式词缀；任何歧义、缺 hash 或官方字典缺项都归入未映射。"""
    mapped: list[MappedTradeStat] = []
    unmapped: list[str] = []
    for analysis in mod_report.mods:
        # 普通复制中的多行词缀可能在 analyze_mods 中被合成一条新对象。
        # 这里按规范化后的词缀类型计数，不用原 ParsedMod 的对象身份。
        if analysis.mod.kind not in {
            ModKind.EXPLICIT,
            ModKind.CRAFTED,
            ModKind.FRACTURED,
        }:
            continue
        candidates = _candidate_hash_sets(conn, item, analysis)
        if len(candidates) != 1:
            unmapped.append(analysis.mod.text)
            continue
        stat_ids = next(iter(candidates))
        if not stat_ids or any(stat_id not in official_stats for stat_id in stat_ids):
            unmapped.append(analysis.mod.text)
            continue
        mapped.append(MappedTradeStat(
            mod_text=analysis.mod.text,
            stat_ids=stat_ids,
            official_texts=tuple(official_stats[stat_id] for stat_id in stat_ids),
        ))
    return tuple(mapped), tuple(unmapped)


class RareComparableService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: TradeApiClient,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.client = client
        self.now = now or (lambda: datetime.now(timezone.utc))

    def estimate(
        self, item_text: str, *, league: str | None = None,
        match_quality: bool = False, match_sockets: bool = False,
    ) -> RareComparableEstimate:
        try:
            item = parse_item(item_text)
        except ItemParseError as exc:
            raise InvalidRareComparable(exc.reason) from exc
        self._validate_item(item)
        quality_filter = None
        if match_quality:
            if item.quality is None:
                raise InvalidRareComparable("要求品质匹配时，装备文本必须明确给出品质")
            if not 0 <= item.quality <= 100:
                raise InvalidRareComparable("品质必须是 0–100 的整数百分比")
            quality_filter = item.quality
        socket_groups = None
        if match_sockets:
            if not item.sockets.raw or not item.sockets.groups:
                raise InvalidRareComparable("要求插槽连接匹配时，装备文本必须明确给出插槽")
            socket_groups = tuple(sorted(len(group) for group in item.sockets.groups))
            if item.sockets.total > 6:
                raise InvalidRareComparable("当前只支持总数不超过 6 的插槽连接组")
        base_type_en = self._resolve_base_type(item.base_type)
        official_stats = self.client.explicit_stats()
        mod_report = analyze_mods(self.conn, item)
        mapped, unmapped = map_trade_stats(self.conn, item, mod_report, official_stats)
        explicit_count = sum(
            analysis.mod.kind in {ModKind.EXPLICIT, ModKind.CRAFTED, ModKind.FRACTURED}
            for analysis in mod_report.mods
        )
        if not mapped:
            return RareComparableEstimate(
                status="unmapped",
                reason=(
                    "没有任何显式词缀能同时通过本地 PoB tradeHashes 与 GGG 当前 "
                    "stat 字典校验"
                ),
                league=(league or "").strip(),
                source=SOURCE,
                source_url="https://www.pathofexile.com/trade",
                fetched_at=_iso(self.now()),
                base_type_input=item.base_type,
                base_type_en=base_type_en,
                item_level=item.item_level,
                corrupted=item.flags.corrupted,
                explicit_mod_count=explicit_count,
                mapped_mod_count=0,
                comparison_quality="none",
                quality_filter=quality_filter,
                socket_group_sizes=socket_groups,
                unmapped_mods=unmapped,
            )

        selected_league = self._select_league(league, self.client.leagues())
        stat_ids = [stat_id for mapping in mapped for stat_id in mapping.stat_ids]
        quote = self.client.rare_quote(
            selected_league,
            base_type_en,
            int(item.item_level or 0),
            item.flags.corrupted,
            stat_ids,
            quality_percent=quality_filter,
            socket_group_sizes=socket_groups,
        )
        quality = "full" if not unmapped and len(mapped) == explicit_count else "partial"
        status = "ready" if quote.sufficient else "insufficient_samples"
        reason = (
            "已取得满足当前映射筛选条件的可比挂单；这是挂单样本，不是该装备的精确成交价"
            if quote.sufficient
            else (
                f"过滤后只有 {quote.sample_count} 条有效可比挂单，低于最低样本数 "
                f"{quote.minimum_sample_count}"
            )
        )
        if quality == "partial":
            reason += "；存在未映射显式词缀，结果只能视为部分可比"
        if quality_filter is not None or socket_groups is not None:
            reason += (
                "；已对抓取的挂单逐件核对请求的品质/连接组字段，"
                "字段缺失或不符的挂单已剔除；词缀数值与其他装备属性仍未归一化"
            )
        return RareComparableEstimate(
            status=status,
            reason=reason,
            league=selected_league,
            source=SOURCE,
            source_url=quote.source_url,
            fetched_at=_iso(self.now()),
            base_type_input=item.base_type,
            base_type_en=base_type_en,
            item_level=item.item_level,
            corrupted=item.flags.corrupted,
            explicit_mod_count=explicit_count,
            mapped_mod_count=len(mapped),
            comparison_quality=quality,
            quality_filter=quality_filter,
            socket_group_sizes=socket_groups,
            mapped_stats=mapped,
            unmapped_mods=unmapped,
            quote=quote,
        )

    @staticmethod
    def _validate_item(item: ParsedItem) -> None:
        if item.rarity is not Rarity.RARE:
            raise InvalidRareComparable("只接受稀有装备复制文本")
        if not item.base_type:
            raise InvalidRareComparable("装备文本缺少底材")
        if item.item_level is None:
            raise InvalidRareComparable("装备文本缺少物品等级")
        if item.flags.unidentified or item.flags.veiled:
            raise InvalidRareComparable("未鉴定或含未解锁隐匿词缀时，显式词缀不完整")
        special = []
        if item.flags.mirrored:
            special.append("镜像")
        if item.flags.split:
            special.append("已分裂")
        if item.flags.synthesised:
            special.append("合成")
        if item.flags.fractured or item.fractured:
            special.append("裂隙")
        if item.flags.influences:
            special.append("影响力")
        if special:
            raise InvalidRareComparable(
                f"首版可比查询尚未结构化这些会改变样本范围的状态：{'、'.join(special)}"
            )
        if item.unclassified:
            raise InvalidRareComparable("装备文本含未归类行，拒绝用可能不完整的状态查询")

    def _resolve_base_type(self, raw: str) -> str:
        rows = self.conn.execute(
            "SELECT DISTINCT name_en FROM pob_base_item "
            "WHERE name_en=? COLLATE NOCASE OR name_zh=?",
            (raw.strip(), raw.strip()),
        ).fetchall()
        names = [str(row["name_en"] or "").strip() for row in rows if row["name_en"]]
        names = list(dict.fromkeys(names))
        if len(names) != 1:
            raise InvalidRareComparable(
                f"本地权威底材表无法把 {raw!r} 唯一映射为英文底材，拒绝模糊查询"
            )
        return names[0]

    @staticmethod
    def _select_league(requested: str | None, leagues: list[str]) -> str:
        names = [name.strip() for name in leagues if name.strip()]
        if not names:
            raise InvalidRareComparable("GGG Trade API 没有返回可用联盟")
        if requested and requested.strip():
            wanted = requested.strip()
            exact = next((name for name in names if name.casefold() == wanted.casefold()), None)
            if exact is None:
                raise InvalidRareComparable(
                    f"GGG Trade API 当前没有联盟 {wanted!r}；可用：{', '.join(names)}"
                )
            return exact
        preferred = [
            name for name in names
            if name not in {"Standard", "Hardcore", "Solo Self-Found"}
            and not re.search(r"Hardcore|Ruthless|SSF", name, re.I)
        ]
        return preferred[0] if preferred else names[0]
