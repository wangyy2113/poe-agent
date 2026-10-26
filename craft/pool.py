"""胸甲普通词缀有效池与条件权重概率。

这里只计算一个严格条件模型：具体胸甲底材、物品等级、前缀或后缀侧、普通来源，
且当前装备没有已占用 family。结果表示“下一次同侧普通加权选择”的分布，绝不表示
任何通货、精华、化石、Harvest 或其它工艺的一次使用成功率。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Any, Literal

PoolStatus = Literal["exact", "unknown"]
GenerationSide = Literal["prefix", "suffix"]

MAX_BASE_CHARS = 80
MAX_TARGET_CHARS = 100
MIN_ITEM_LEVEL = 1
MAX_ITEM_LEVEL = 100

_SIDE_TO_GEN = {"prefix": 1, "suffix": 2}
_SIDE_ZH = {"prefix": "前缀", "suffix": "后缀"}


@dataclass(frozen=True)
class PoolGap:
    code: str
    message: str
    authority_url: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "authority_url": self.authority_url,
        }


@dataclass(frozen=True)
class BasePoolRef:
    base_id: str
    name_en: str
    name_zh: str
    poedb_slug: str
    armour_tag: str
    modifier_page: str
    tags: tuple[str, ...]
    pob_url: str
    poedb_url: str
    source_commit: str
    pob_version: str
    game_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_id": self.base_id,
            "name_en": self.name_en,
            "name_zh": self.name_zh,
            "poedb_slug": self.poedb_slug,
            "armour_tag": self.armour_tag,
            "modifier_page": self.modifier_page,
            "tags": list(self.tags),
            "pob_version": self.pob_version,
            "game_version": self.game_version,
        }


@dataclass(frozen=True)
class EligibleAffix:
    uid: str
    correct_group: str
    family_list: tuple[str, ...]
    tier: int | None
    required_item_level: int
    weight: int
    name_zh: str
    text_zh: str
    url: str

    def digest_tuple(self) -> tuple[Any, ...]:
        return (
            self.uid,
            self.correct_group,
            self.family_list,
            self.tier,
            self.required_item_level,
            self.weight,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "correct_group": self.correct_group,
            "family_list": list(self.family_list),
            "tier": self.tier,
            "required_item_level": self.required_item_level,
            "weight": self.weight,
            "name_zh": self.name_zh,
            "text_zh": self.text_zh,
        }


@dataclass(frozen=True)
class PageSnapshotProof:
    scope: str
    modifier_page: str
    row_count: int
    row_digest: str
    source_url: str
    source_final_url: str
    source_content_hash: str
    source_fetched_at: str
    source_total_rows: int
    source_deduped_rows: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "modifier_page": self.modifier_page,
            "row_count": self.row_count,
            "row_digest": self.row_digest,
            "source_url": self.source_url,
            "source_final_url": self.source_final_url,
            "source_content_hash": self.source_content_hash,
            "source_fetched_at": self.source_fetched_at,
            "source_total_rows": self.source_total_rows,
            "source_deduped_rows": self.source_deduped_rows,
        }


@dataclass
class PoolOddsReport:
    status: PoolStatus
    base_type: str
    item_level: int
    generation_side: GenerationSide
    target_query: str
    base: BasePoolRef | None = None
    pool_size: int | None = None
    total_weight: int | None = None
    target_group: str | None = None
    target_weight: int | None = None
    probability_numerator: int | None = None
    probability_denominator: int | None = None
    probability_decimal: str | None = None
    expected_independent_selections: str | None = None
    target_rows: list[EligibleAffix] = field(default_factory=list)
    pool_digest: str | None = None
    page_snapshot: PageSnapshotProof | None = None
    assumptions: list[str] = field(default_factory=list)
    exclusions: list[str] = field(default_factory=list)
    candidate_groups: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[PoolGap] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "base_type": self.base_type,
            "item_level": self.item_level,
            "generation_side": self.generation_side,
            "target_query": self.target_query,
            "base": self.base.to_dict() if self.base else None,
            "pool_size": self.pool_size,
            "total_weight": self.total_weight,
            "target_group": self.target_group,
            "target_weight": self.target_weight,
            "probability_numerator": self.probability_numerator,
            "probability_denominator": self.probability_denominator,
            "probability_decimal": self.probability_decimal,
            "expected_independent_selections": self.expected_independent_selections,
            "target_rows": [row.to_dict() for row in self.target_rows],
            "pool_digest": self.pool_digest,
            "page_snapshot": self.page_snapshot.to_dict() if self.page_snapshot else None,
            "assumptions": list(self.assumptions),
            "exclusions": list(self.exclusions),
            "candidate_groups": list(self.candidate_groups),
            "gaps": [gap.to_dict() for gap in self.gaps],
        }


POOL_SNAPSHOT_MODE = "replace_by_page_v2_source_provenance"
NORMAL_POOL_SNAPSHOT_MODE = "replace_normal_by_page_v1_source_provenance"
POOL_SNAPSHOT_MAX_AGE = timedelta(days=7)
_POOL_PAGE_COLUMNS = (
    "uid", "mod_code", "item_class_page", "item_class_code", "item_class_zh",
    "category", "category_label", "name_zh", "gen_type", "gen_label",
    "family_list", "correct_group", "ilvl", "tier", "tier_count", "tier_source",
    "weight", "weight_pct", "text_zh", "text_template", "loose_template", "ranges",
    "tags", "spawn_tags", "fossil_tags", "variant_id", "source", "source_priority", "url",
)


def affix_page_digest(
    conn: sqlite3.Connection, modifier_page: str, *, normal_only: bool = False
) -> tuple[int, str]:
    """返回词缀页当前完整行集的数量与稳定摘要，供导入审计和查询端双向核对。"""
    predicate = " AND category='normal'" if normal_only else ""
    rows = conn.execute(
        f"SELECT {','.join(_POOL_PAGE_COLUMNS)} FROM mod_affix "
        f"WHERE item_class_page=?{predicate} ORDER BY uid",
        (modifier_page,),
    ).fetchall()
    payload = [
        [row[column] for column in _POOL_PAGE_COLUMNS]
        for row in rows
    ]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return len(payload), digest


def _sha256_text(value: Any) -> str | None:
    text = str(value or "").lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        return None
    return text


def _snapshot_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def verify_affix_page_snapshot(
    conn: sqlite3.Connection, modifier_page: str
) -> tuple[PageSnapshotProof | None, str]:
    """验证最近导入的库摘要、原始载荷证明、零丢失统计与新鲜度。"""
    row = conn.execute(
        "SELECT source,note FROM ingest_run WHERE category='mod_affix' "
        "AND finished_at IS NOT NULL ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if not row or not row[1]:
        return None, "没有可审计的 mod_affix 页级替换记录"
    if str(row[0] or "") != "POEDB":
        return None, "最近的 mod_affix 导入不是 POEDB 来源"
    try:
        note = json.loads(row[1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, "最近的 mod_affix 导入审计损坏"
    mode = note.get("snapshot_mode") if isinstance(note, dict) else None
    if mode not in (POOL_SNAPSHOT_MODE, NORMAL_POOL_SNAPSHOT_MODE):
        return None, "最近的 mod_affix 导入没有来源级页范围替换证明"
    normal_only = mode == NORMAL_POOL_SNAPSHOT_MODE
    pages = note.get("pages")
    page = pages.get(modifier_page) if isinstance(pages, dict) else None
    if not isinstance(page, dict) or page.get("status") != "complete":
        return None, f"最近的 mod_affix 导入没有完整替换 {modifier_page} 的受审计范围"
    if normal_only and page.get("scope") != "normal":
        return None, f"{modifier_page} 的 normal 范围声明无效"
    if page.get("parse_complete") is not True or page.get("explicit_no_mods") is not False:
        return None, f"{modifier_page} 未声明非空载荷零丢失解析"

    expected_count = page.get("rows")
    expected_digest = _sha256_text(page.get("digest"))
    source_hash = _sha256_text(page.get("source_content_hash"))
    source_url = str(page.get("source_url") or "")
    source_final_url = str(page.get("source_final_url") or "")
    expected_url = f"https://poedb.tw/cn/{modifier_page}"
    fetched_at = _snapshot_time(page.get("source_fetched_at"))
    counters = {
        key: page.get(key)
        for key in (
            "source_total_rows", "source_succeeded_rows", "source_failed_rows",
            "source_deduped_rows", "source_dedup_conflicts",
        )
    }
    textless = page.get("source_textless_rows", 0)
    normal_source_rows = page.get("source_normal_rows") if normal_only else None
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count <= 0
        or expected_digest is None
        or source_hash is None
        or source_url != expected_url
        or source_final_url != expected_url
        or fetched_at is None
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in counters.values())
        or isinstance(textless, bool) or not isinstance(textless, int) or textless < 0
        or (normal_only and (
            isinstance(normal_source_rows, bool)
            or not isinstance(normal_source_rows, int)
            or normal_source_rows != expected_count
        ))
    ):
        return None, f"{modifier_page} 的来源级页审计字段无效"
    if (
        counters["source_failed_rows"] != 0
        or counters["source_dedup_conflicts"] != 0
        or (not normal_only and counters["source_succeeded_rows"] != expected_count)
        or (normal_only and counters["source_succeeded_rows"] < expected_count)
        or counters["source_total_rows"]
        != counters["source_succeeded_rows"] + counters["source_deduped_rows"]
        + textless
        or textless != 0
    ):
        return None, f"{modifier_page} 的来源行统计不能证明零丢失"

    now = datetime.now(timezone.utc)
    age = now - fetched_at
    if age < -timedelta(minutes=5) or age > POOL_SNAPSHOT_MAX_AGE:
        return None, f"{modifier_page} 的来源快照已过期或时间异常"

    actual_count, actual_digest = affix_page_digest(
        conn, modifier_page, normal_only=normal_only
    )
    if actual_count != expected_count or actual_digest != expected_digest:
        return None, f"{modifier_page} 当前数据与最近页范围替换审计不一致"
    return PageSnapshotProof(
        scope="normal" if normal_only else "full_page",
        modifier_page=modifier_page,
        row_count=actual_count,
        row_digest=actual_digest,
        source_url=source_url,
        source_final_url=source_final_url,
        source_content_hash=source_hash,
        source_fetched_at=fetched_at.isoformat(timespec="seconds"),
        source_total_rows=int(counters["source_total_rows"]),
        source_deduped_rows=int(counters["source_deduped_rows"]),
    ), ""


BASE_AUTHORITY = "https://github.com/PathOfBuildingCommunity/PathOfBuilding"
POOL_AUTHORITY = "https://poedb.tw/cn/Modifiers"


def _unknown(
    *,
    base_type: str,
    item_level: int,
    generation_side: GenerationSide,
    target_query: str,
    code: str,
    message: str,
    authority_url: str,
    base: BasePoolRef | None = None,
    candidate_groups: list[dict[str, Any]] | None = None,
) -> PoolOddsReport:
    return PoolOddsReport(
        status="unknown",
        base_type=base_type,
        item_level=item_level,
        generation_side=generation_side,
        target_query=target_query,
        base=base,
        candidate_groups=candidate_groups or [],
        gaps=[PoolGap(code=code, message=message, authority_url=authority_url)],
        exclusions=_scope_exclusions(),
    )


def _scope_exclusions() -> list[str]:
    return [
        "不给出任何通货或具体工艺的一次使用成功率",
        "不处理已有装备词缀的 family 排除、前后缀容量或当前装备状态",
        "不处理影响力、精华、化石、Harvest、隐匿、Eldritch、Synthesis 或 Metamod",
        "不提供制作步骤、实际期望通货、市场成本或最快/最经济路线",
    ]


def _json_string_tuple(value: Any) -> tuple[str, ...] | None:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, list) or any(not isinstance(item, str) or not item for item in parsed):
        return None
    return tuple(parsed)


def _resolve_base(conn: sqlite3.Connection, base_type: str) -> tuple[BasePoolRef | None, str]:
    """按名字解析胸甲底材。非胸甲要给**准确**的理由。

    底材注册表已扩到全部 18 个装备类别（1022 条），所以这里现在会命中头盔、戒指、
    法杖……而本模块的条件权重模型只对胸甲成立（同侧普通池 + 按属性分的词缀页）。
    命中非胸甲时必须说清「本工具只建模胸甲」，而不是让下面的必填字段检查报一句
    「注册记录不完整」——那会把「工具范围之外」说成「数据有问题」，
    是两件完全不同的事，排查方向也不同。
    """
    rows = conn.execute(
        "SELECT * FROM pob_base_item WHERE name_en=? OR name_zh=? ORDER BY base_id",
        (base_type, base_type),
    ).fetchall()
    if not rows:
        return None, f"底材 {base_type!r} 没有逐字匹配的底材注册记录"
    unique = {str(row["base_id"]): row for row in rows}
    if len(unique) != 1:
        # POEDB 会把两个底材译成同一个中文名（「丝绸手套」「召集法杖」各两条），
        # 此时拒绝而不是挑一条 —— 挑错了整份池就是错的
        return None, f"底材 {base_type!r} 命中 {len(unique)} 条记录，无法唯一确定"
    row = next(iter(unique.values()))
    if str(row["category_key"] or "") != "body":
        return None, (
            f"底材 {base_type!r} 是{row['item_class']}，不是胸甲；"
            "本工具只建模胸甲的同侧普通词缀池，其它部位的条件权重模型尚未建立"
        )
    tags = _json_string_tuple(row["tags"])
    required = (
        row["name_en"], row["name_zh"], row["poedb_slug"], row["armour_tag"],
        row["modifier_page"], row["pob_url"], row["poedb_url"], row["source_commit"],
    )
    if tags is None or not tags or any(not value for value in required):
        return None, f"底材 {base_type!r} 的注册记录不完整，不能用于池计算"
    return BasePoolRef(
        base_id=str(row["base_id"]),
        name_en=str(row["name_en"]),
        name_zh=str(row["name_zh"]),
        poedb_slug=str(row["poedb_slug"]),
        armour_tag=str(row["armour_tag"]),
        modifier_page=str(row["modifier_page"]),
        tags=tags,
        pob_url=str(row["pob_url"]),
        poedb_url=str(row["poedb_url"]),
        source_commit=str(row["source_commit"]),
        pob_version=str(row["pob_version"] or ""),
        game_version=str(row["game_version"] or ""),
    ), ""


def _eligible_rows(
    conn: sqlite3.Connection,
    base: BasePoolRef,
    item_level: int,
    generation_side: GenerationSide,
) -> tuple[list[EligibleAffix], str]:
    rows = conn.execute(
        "SELECT uid,correct_group,family_list,tier,ilvl,weight,name_zh,text_zh,url "
        "FROM mod_affix WHERE item_class_page=? AND category='normal' "
        "AND gen_type=? AND ilvl<=? ORDER BY uid",
        (base.modifier_page, _SIDE_TO_GEN[generation_side], item_level),
    ).fetchall()
    if not rows:
        return [], "指定条件下没有普通词缀行，无法证明池是完整的"

    eligible: list[EligibleAffix] = []
    for row in rows:
        weight = row["weight"]
        if weight is None or not isinstance(weight, int) or weight < 0:
            return [], f"词缀 {row['uid']} 缺少有效整数权重"
        if weight == 0:
            continue
        families = _json_string_tuple(row["family_list"])
        if families is None or not families:
            return [], f"词缀 {row['uid']} 缺少完整 family_list"
        if not row["correct_group"] or not row["url"]:
            return [], f"词缀 {row['uid']} 缺少 correct_group 或来源 URL"
        ilvl = row["ilvl"]
        if not isinstance(ilvl, int) or ilvl < 0:
            return [], f"词缀 {row['uid']} 缺少有效物品等级"
        eligible.append(
            EligibleAffix(
                uid=str(row["uid"]),
                correct_group=str(row["correct_group"]),
                family_list=families,
                tier=int(row["tier"]) if row["tier"] is not None else None,
                required_item_level=ilvl,
                weight=weight,
                name_zh=str(row["name_zh"] or ""),
                text_zh=str(row["text_zh"] or ""),
                url=str(row["url"]),
            )
        )
    if not eligible:
        return [], "指定条件下所有普通词缀权重均为 0，无法形成加权池"
    return eligible, ""


def _literal_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _target_groups(
    conn: sqlite3.Connection,
    base: BasePoolRef,
    item_level: int,
    generation_side: GenerationSide,
    target_query: str,
) -> list[dict[str, Any]]:
    pattern = f"%{_literal_like(target_query)}%"
    rows = conn.execute(
        "SELECT correct_group,count(*) AS rows_count,min(tier) AS min_tier,max(tier) AS max_tier,"
        "group_concat(DISTINCT text_zh) AS samples "
        "FROM mod_affix WHERE item_class_page=? AND category='normal' AND gen_type=? "
        "AND ilvl<=? AND weight>0 AND (correct_group=? OR name_zh=? "
        "OR text_zh LIKE ? ESCAPE '\\') GROUP BY correct_group ORDER BY correct_group",
        (
            base.modifier_page,
            _SIDE_TO_GEN[generation_side],
            item_level,
            target_query,
            target_query,
            pattern,
        ),
    ).fetchall()
    return [
        {
            "correct_group": str(row["correct_group"] or ""),
            "eligible_tiers": int(row["rows_count"] or 0),
            "min_tier": int(row["min_tier"]) if row["min_tier"] is not None else None,
            "max_tier": int(row["max_tier"]) if row["max_tier"] is not None else None,
            "sample_texts": [
                text for text in str(row["samples"] or "").split(",") if text
            ][:3],
        }
        for row in rows
        if row["correct_group"]
    ]


def _decimal_ratio(numerator: int, denominator: int) -> str:
    with localcontext() as ctx:
        ctx.prec = 18
        value = Decimal(numerator) / Decimal(denominator)
    return format(value, "f").rstrip("0").rstrip(".") or "0"


def analyze_affix_pool(
    conn: sqlite3.Connection,
    *,
    base_type: str,
    item_level: int,
    generation_side: GenerationSide,
    target_query: str,
) -> PoolOddsReport:
    """计算精确底材上普通同侧词缀组的条件权重概率。"""
    base_name = (base_type or "").strip()
    target = (target_query or "").strip()
    if not base_name or len(base_name) > MAX_BASE_CHARS:
        raise ValueError(f"base_type 必须为 1..{MAX_BASE_CHARS} 个字符")
    if not target or len(target) > MAX_TARGET_CHARS:
        raise ValueError(f"target_query 必须为 1..{MAX_TARGET_CHARS} 个字符")
    if generation_side not in _SIDE_TO_GEN:
        raise ValueError("generation_side 只能是 prefix 或 suffix")
    if isinstance(item_level, bool) or not isinstance(item_level, int):
        raise ValueError("item_level 必须是整数")
    if not MIN_ITEM_LEVEL <= item_level <= MAX_ITEM_LEVEL:
        raise ValueError(
            f"item_level 必须在 {MIN_ITEM_LEVEL}..{MAX_ITEM_LEVEL} 范围内"
        )

    base, reason = _resolve_base(conn, base_name)
    if base is None:
        return _unknown(
            base_type=base_name,
            item_level=item_level,
            generation_side=generation_side,
            target_query=target,
            code="base_not_resolved",
            message=reason,
            authority_url=BASE_AUTHORITY,
        )

    page_snapshot, reason = verify_affix_page_snapshot(conn, base.modifier_page)
    if page_snapshot is None:
        return _unknown(
            base_type=base_name,
            item_level=item_level,
            generation_side=generation_side,
            target_query=target,
            code="pool_snapshot_unverified",
            message=reason,
            authority_url=f"https://poedb.tw/cn/{base.modifier_page}",
            base=base,
        )

    pool, reason = _eligible_rows(conn, base, item_level, generation_side)
    if reason:
        return _unknown(
            base_type=base_name,
            item_level=item_level,
            generation_side=generation_side,
            target_query=target,
            code="pool_incomplete",
            message=reason,
            authority_url=f"https://poedb.tw/cn/{base.modifier_page}",
            base=base,
        )

    groups = _target_groups(conn, base, item_level, generation_side, target)
    if not groups:
        return _unknown(
            base_type=base_name,
            item_level=item_level,
            generation_side=generation_side,
            target_query=target,
            code="target_not_resolved",
            message=(
                f"目标 {target!r} 在该底材、物品等级与{_SIDE_ZH[generation_side]}侧的"
                "普通正权重池中没有逐字候选；不能把未命中解释为 0%"
            ),
            authority_url=f"https://poedb.tw/cn/{base.modifier_page}",
            base=base,
        )
    if len(groups) != 1:
        return _unknown(
            base_type=base_name,
            item_level=item_level,
            generation_side=generation_side,
            target_query=target,
            code="target_ambiguous",
            message=f"目标 {target!r} 命中 {len(groups)} 个词缀组，必须先消歧",
            authority_url=f"https://poedb.tw/cn/{base.modifier_page}",
            base=base,
            candidate_groups=groups,
        )

    target_group = str(groups[0]["correct_group"])
    target_rows = [row for row in pool if row.correct_group == target_group]
    total_weight = sum(row.weight for row in pool)
    target_weight = sum(row.weight for row in target_rows)
    if total_weight <= 0 or target_weight <= 0:
        return _unknown(
            base_type=base_name,
            item_level=item_level,
            generation_side=generation_side,
            target_query=target,
            code="weight_incomplete",
            message="完整池或目标组没有正整数权重，不能输出概率",
            authority_url=f"https://poedb.tw/cn/{base.modifier_page}",
            base=base,
        )

    probability = Fraction(target_weight, total_weight)
    expected = Fraction(total_weight, target_weight)
    digest_payload = [row.digest_tuple() for row in sorted(pool, key=lambda item: item.uid)]
    pool_digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return PoolOddsReport(
        status="exact",
        base_type=base_name,
        item_level=item_level,
        generation_side=generation_side,
        target_query=target,
        base=base,
        pool_size=len(pool),
        total_weight=total_weight,
        target_group=target_group,
        target_weight=target_weight,
        probability_numerator=probability.numerator,
        probability_denominator=probability.denominator,
        probability_decimal=_decimal_ratio(target_weight, total_weight),
        expected_independent_selections=(
            str(expected.numerator)
            if expected.denominator == 1
            else f"{expected.numerator}/{expected.denominator}"
        ),
        target_rows=target_rows,
        pool_digest=pool_digest,
        page_snapshot=page_snapshot,
        assumptions=[
            "底材由 PoB 标签唯一映射到一个 POEDB 胸甲词缀页",
            "仅使用 POEDB category=normal、指定物品等级可生成且权重大于 0 的同侧词缀档位",
            "目标表示唯一 correct_group 的任一可用档位",
            "当前装备尚未应用任何已有词缀 family 排除",
            f"数值只表示下一次{_SIDE_ZH[generation_side]}普通加权选择的条件分布",
        ],
        exclusions=_scope_exclusions(),
    )
