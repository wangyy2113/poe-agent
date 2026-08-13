"""采集管线：抓取 → 抽取 → 实体入库 → 渲染卡片 → 切片 → FTS5 索引。

    python -m kb.pipeline --source poedb --category unique_item
    python -m kb.pipeline --source poedb --category unique_item --force   # 强制重抓
    python -m kb.pipeline --source poedb --category unique_item --rebuild # 清库重建

增量策略（需求 2.9）：内容哈希与上次一致则跳过重新切片与索引。
结构断言失败时整批拒绝写入，不产生脏数据（需求 2.7）。
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # 支持 `python kb/pipeline.py` 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod
from core.config import get_config
from core.logging_setup import setup_logging
from entities.models import Entity, ModLine, Requirement, SourceRef, ValueRange
from entities.taxonomy import EntityType, ModKind
from kb import coverage as coverage_mod
from kb.fetcher import Fetcher
from kb import render_tree
from kb.render import to_chunks
from kb.render_mods import build_mod_chunks
from kb.sources import (
    craftofexile_metamods,
    poedb_crafting,
    poedb_index,
    poedb_mods,
    poedb_tree,
    poedb_unique,
)
from retrieval import alias as alias_mod
from retrieval import fts as fts_mod
from retrieval import vocab as vocab_mod

logger = logging.getLogger(__name__)

#: 各类别切片的 entity_id 前缀。rebuild 时按前缀清理，互不干扰。
UNIQUE_PREFIX = "unique_item:"
MOD_LADDER_PREFIX = "mod_ladder:"

#: 快照完整性护栏。它们是采集安全阈值，不是游戏机制断言：首次导入至少应有
#: 100 条；已有快照更新时若骤降超过 5%，默认拒绝 destructive replace。
#: 当前真实页面为 783 条，阈值刻意不钉死当前行数，允许正常的小幅版本变化。
MIN_BENCH_SNAPSHOT_RECIPES = 100
MAX_BENCH_SNAPSHOT_DROP_FRACTION = 0.05

#: Metamod 页面当前有 134 个明确单元格；绝对下限防止登录页、错误页或截断页
#: 被当成首次可信快照。阈值不钉死当前值，允许上游正常增删。
MIN_METAMOD_SNAPSHOT_INTERACTIONS = 100
MAX_METAMOD_SNAPSHOT_DROP_FRACTION = 0.05


def _existing_hashes(conn, entity_type: str) -> dict[str, str]:
    return {
        r["entity_id"]: r["content_hash"]
        for r in conn.execute(
            "SELECT entity_id, content_hash FROM entity WHERE entity_type = ?", (entity_type,)
        )
    }


def _entity_hash(entity: Entity) -> str:
    """实体内容哈希：只覆盖会影响卡片与索引的字段。"""
    import hashlib

    payload = json.dumps(
        {
            "name_zh": entity.name_zh,
            "name_en": entity.name_en,
            "base_type": entity.base_type,
            "item_class": entity.item_class,
            "req": entity.requirement.raw if entity.requirement else None,
            "mods": [[m.kind.value, m.text] for m in entity.mods],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _upsert_entity(conn, entity: Entity) -> None:
    conn.execute(
        """
        INSERT INTO entity(entity_id, entity_type, name_zh, name_en, slug, base_type,
                           item_class, attributes, source, source_priority, url,
                           league, version, content_hash, fetched_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(entity_id) DO UPDATE SET
            name_zh=excluded.name_zh, name_en=excluded.name_en, slug=excluded.slug,
            base_type=excluded.base_type, item_class=excluded.item_class,
            attributes=excluded.attributes, source=excluded.source,
            source_priority=excluded.source_priority, url=excluded.url,
            league=excluded.league, version=excluded.version,
            content_hash=excluded.content_hash, fetched_at=excluded.fetched_at,
            updated_at=datetime('now')
        """,
        (
            entity.entity_id,
            entity.entity_type.value,
            entity.name_zh,
            entity.name_en,
            entity.slug,
            entity.base_type,
            entity.item_class,
            json.dumps(
                {
                    **entity.attributes,
                    "mods": [
                        {"kind": m.kind.value, "text": m.text,
                         "ranges": [[r.low, r.high] for r in m.ranges]}
                        for m in entity.mods
                    ],
                    "requirement": entity.requirement.model_dump() if entity.requirement else None,
                    "search_keywords": entity.search_keywords,
                },
                ensure_ascii=False,
            ),
            entity.ref.source,
            entity.ref.priority,
            entity.ref.url,
            entity.league,
            entity.version,
            entity.content_hash,
            entity.ref.fetched_at,
        ),
    )


def run_poedb_unique(*, force: bool = False, rebuild: bool = False) -> int:
    cfg = get_config()
    conn = dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))

    started = time.time()
    cur = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at) VALUES (?,?,datetime('now'))",
        ("POEDB", "unique_item"),
    )
    run_id = cur.lastrowid

    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )

    res = fetcher.fetch(poedb_unique.LIST_URL, force=force)
    if not res.ok:
        conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'), failed=1, note=? WHERE run_id=?",
            (f"抓取失败 HTTP {res.status_code}", run_id),
        )
        conn.commit()
        logger.error("抓取失败：HTTP %s", res.status_code)
        return 1

    # 结构断言失败会抛 StructureError，此处不捕获——宁可整批失败也不写脏数据
    entities, stats = poedb_unique.extract_uniques(res.text)
    logger.info(
        "抽取完成：%d/%d 成功（%.1f%%），零词缀 %d（%.1f%%）",
        stats.succeeded, stats.total, stats.success_ratio * 100,
        stats.zero_mod, stats.zero_mod_ratio * 100,
    )

    if rebuild:
        # 只清本类别：全表清空会连带删掉词缀梯度卡的 2.4 万条索引
        logger.info("rebuild：清空 unique_item 的 chunk / FTS / alias")
        fts_mod.clear_index(conn, entity_prefix=UNIQUE_PREFIX)
        alias_mod.clear_aliases(conn, entity_prefix=UNIQUE_PREFIX)

    known = {} if rebuild else _existing_hashes(conn, "unique_item")

    # 索引页**结构上拿不到**权威英文名（它只有中文名与底材），因此不能让它把
    # L2 已补全的 name_en / base_type_en 覆盖成空。
    # 原则：一个观测不到某字段的抽取器，无权清空该字段。
    # 这同时修掉一个增量退化——若不带过来，content_hash 每次都与库里不同，
    # 1229 件会在每次运行时被无谓重渲一遍。
    prior = {
        r["slug"]: (r["name_en"], json.loads(r["attributes"] or "{}").get("base_type_en"))
        for r in conn.execute(
            "SELECT slug, name_en, attributes FROM entity WHERE entity_type='unique_item'"
        )
    }

    changed: list[Entity] = []
    skipped = 0
    for e in entities:
        carried = prior.get(e.slug)
        if carried:
            e.name_en = e.name_en or carried[0]
            if carried[1]:
                e.attributes["base_type_en"] = carried[1]
        e.content_hash = _entity_hash(e)
        e.league = res.fetched_at[:10]  # 记录采集日期，赛季字段留给后续 GGG API 解析
        if known.get(e.entity_id) == e.content_hash:
            skipped += 1
            continue
        changed.append(e)

    logger.info("需要更新 %d 条，内容未变跳过 %d 条", len(changed), skipped)

    for e in changed:
        _upsert_entity(conn, e)

    # 词表须在渲染前刷新：装备名与底材名刚入库，卡片与查询要用同一份词表
    vocab_mod.build_vocab(conn)

    indexed = 0
    for e in changed:
        indexed += fts_mod.index_chunks(conn, to_chunks(e))
    if changed:
        alias_mod.index_aliases(conn, changed)

    # 自愈：rebuild 清掉了**全部** unique 切片，但本轮只重建索引页抽到的那批。
    # L2 补进来的实体（类别页补集 / 详情页兜底）不在本轮抽取结果里，
    # 会静默失去切片与别名——一条操作命令不该让 112 件装备从索引里消失。
    orphans = [
        _entity_from_row(r)
        for r in conn.execute(
            "SELECT * FROM entity WHERE entity_type='unique_item' AND entity_id NOT IN "
            "(SELECT DISTINCT entity_id FROM chunk WHERE entity_id IS NOT NULL)"
        )
    ]
    if orphans:
        indexed += _index_and_alias(conn, orphans)
        logger.info("补回 %d 件无切片的实体（多为 L2 补集，rebuild 时被连带清掉）",
                    len(orphans))

    elapsed_ms = int((time.time() - started) * 1000)
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, "
        "failed=?, skipped=?, elapsed_ms=?, note=? WHERE run_id=?",
        (
            stats.total, stats.succeeded, stats.failed, skipped, elapsed_ms,
            json.dumps(
                {
                    "content_hash": res.content_hash[:16],
                    "from_cache": res.from_cache,
                    "chunks_indexed": indexed,
                    "zero_mod": stats.zero_mod,
                },
                ensure_ascii=False,
            ),
            run_id,
        ),
    )
    conn.commit()

    counts = {
        "entity": conn.execute("SELECT count(*) FROM entity").fetchone()[0],
        "chunk": conn.execute("SELECT count(*) FROM chunk").fetchone()[0],
        "chunk_fts": conn.execute("SELECT count(*) FROM chunk_fts").fetchone()[0],
        "alias": conn.execute("SELECT count(*) FROM alias").fetchone()[0],
    }
    logger.info("入库完成：%s，本轮索引切片 %d，耗时 %dms", counts, indexed, elapsed_ms)
    return 0


def _index_and_alias(conn, entities: list[Entity]) -> int:  # noqa: ANN001
    """实体入库 → 卡片 → FTS 索引 → 别名。渲染前刷新领域词表。"""
    for e in entities:
        _upsert_entity(conn, e)
    vocab_mod.build_vocab(conn)
    indexed = 0
    for e in entities:
        indexed += fts_mod.index_chunks(conn, to_chunks(e))
    if entities:
        alias_mod.index_aliases(conn, entities)
    return indexed


def _entity_from_row(row) -> Entity:  # noqa: ANN001
    """把 entity 表的一行还原成 Entity，供只改个别字段后重新渲染卡片使用。"""
    attrs = json.loads(row["attributes"] or "{}")
    mods = [
        ModLine(
            kind=ModKind(m["kind"]),
            text=m["text"],
            ranges=[ValueRange(low=lo, high=hi) for lo, hi in m.get("ranges", [])],
        )
        for m in attrs.get("mods", [])
    ]
    req = attrs.get("requirement")
    return Entity(
        entity_id=row["entity_id"],
        entity_type=EntityType(row["entity_type"]),
        name_zh=row["name_zh"],
        name_en=row["name_en"],
        slug=row["slug"],
        base_type=row["base_type"],
        item_class=row["item_class"] or "",
        mods=mods,
        requirement=Requirement(**req) if req else None,
        search_keywords=attrs.get("search_keywords", []),
        attributes={
            k: v for k, v in attrs.items()
            if k not in ("mods", "requirement", "search_keywords")
        },
        league=row["league"],
        version=row["version"],
        ref=SourceRef(
            source=row["source"], url=row["url"], priority=row["source_priority"],
            fetched_at=row["fetched_at"],
        ),
    )


def run_unique_l2(*, force: bool = False) -> int:
    """唯一装备的 L2 补全：类别页补集 + 英文语言版权威英文名 + 详情页兜底。

    三个来源各补一块，都是**先测量再决定**的结果：

    1. **类别页补集**（零网络请求，页面已在缓存里）——索引页对唯一珠宝覆盖不全，
       实测类别页并集比索引页多 111 件，还包含索引页完全没列的「无尽之衣」。
    2. **英文语言版索引页**（1 次请求）——补权威 `name_en` 与英文底材名。
       实测 373/1231 即 30% 的英文名无法由 slug 反推，这一步不可省；
       而它一次请求就能覆盖全部，不必抓 1229 个详情页。
    3. **详情页兜底**（少量请求）——只用于前两步都拿不到中文名的物品
       （英文页独有的那 2 件）。
    """
    cfg = get_config()
    conn = dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    fetcher = _make_fetcher(cfg)

    started = time.time()
    cur = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at) VALUES (?,?,datetime('now'))",
        ("POEDB", "unique_l2"),
    )
    run_id = cur.lastrowid
    conn.commit()

    known = {
        r["slug"]: r["entity_id"]
        for r in conn.execute(
            "SELECT slug, entity_id FROM entity WHERE entity_type='unique_item'"
        )
    }
    league = None

    # ── 阶段 1：类别页补集 ──
    class_pages = poedb_index.discover_mod_class_pages(fetcher, force=force)
    class_pages = [*class_pages, *poedb_index.PARENT_CLASS_PAGES]
    agg = poedb_unique.ExtractStats()
    new_entities: dict[str, Entity] = {}
    for slug in class_pages:
        url = f"{poedb_index.BASE}/{slug}"
        res = fetcher.fetch(url, force=force)
        if not res.ok:
            logger.warning("类别页 %s 抓取失败 HTTP %s", slug, res.status_code)
            continue
        league = league or res.fetched_at[:10]
        ents, st = poedb_unique.extract_uniques_from_class_page(res.text, slug, url)
        agg.total += st.total
        agg.succeeded += st.succeeded
        agg.failed += st.failed
        agg.skipped += st.skipped
        agg.zero_mod += st.zero_mod
        agg.failures.extend(st.failures[:3])
        for e in ents:
            if e.slug not in known:
                new_entities.setdefault(e.slug, e)

    logger.info(
        "类别页扫描：%d 页，卡片 %d 张（成功 %d / 失败 %d / 跳过 Royale %d），"
        "其中索引页未收录 %d 件",
        len(class_pages), agg.total, agg.succeeded, agg.failed, agg.skipped,
        len(new_entities),
    )
    # 聚合层断言：单页样本小易误报，只在总量上判
    if agg.total and agg.success_ratio < poedb_unique.MIN_SUCCESS_RATIO:
        raise poedb_unique.StructureError(
            f"类别页抽取成功率 {agg.success_ratio:.1%} 低于下限 "
            f"{poedb_unique.MIN_SUCCESS_RATIO:.0%}（{agg.succeeded}/{agg.total}），"
            "判为页面结构变更，拒绝写入"
        )

    # ── 阶段 2：英文语言版索引页 → 权威英文名 ──
    res_en = fetcher.fetch(poedb_unique.EN_LIST_URL, force=force)
    if not res_en.ok:
        raise RuntimeError(f"英文索引页抓取失败 HTTP {res_en.status_code}")
    en_map = poedb_unique.extract_en_index(res_en.text)
    logger.info("英文索引页：%d 个 slug → 英文名", len(en_map))

    # 英文页独有、且类别页也没给出的 → 走详情页兜底拿中文名
    en_only = [s for s in en_map if s not in known and s not in new_entities]
    detail_added = 0
    detail_failed: list[tuple[str, str]] = []
    for slug in en_only:
        url = f"{poedb_index.BASE}/{slug}"
        try:
            r = fetcher.fetch(url, force=force)
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}")
            detail = poedb_unique.parse_detail_page(r.text)
            entity = poedb_unique.entity_from_detail(slug, url, detail)
        except Exception as exc:  # noqa: BLE001
            detail_failed.append((slug, f"{type(exc).__name__}: {exc}"))
            logger.warning("L2 详情页补全失败 %s：%s", slug, exc)
            continue
        new_entities[slug] = entity
        detail_added += 1
    logger.info("英文页独有 %d 件，详情页补全成功 %d 件，失败 %d 件",
                len(en_only), detail_added, len(detail_failed))

    # ── 写入：新增实体 ──
    # item_class 兜底：珠宝、雕像等页面的传奇 tab id 就是「传奇」，推不出类别名。
    # 用 POEDB 自己在词缀载荷里给的 item_class_zh 补（数据来源，不是猜）。
    page_class = {
        r["item_class_page"]: r["item_class_zh"]
        for r in conn.execute(
            "SELECT DISTINCT item_class_page, item_class_zh FROM mod_affix "
            "WHERE item_class_zh <> ''"
        )
    }
    added = list(new_entities.values())
    for e in added:
        en = en_map.get(e.slug)
        if en:
            e.name_en = e.name_en or en[0]
        if not e.item_class:
            e.item_class = page_class.get(e.attributes.get("source_page", ""), "")
        e.league = e.league or league
        e.content_hash = _entity_hash(e)
    indexed_new = _index_and_alias(conn, added)

    # ── 阶段 4：英文类别页，补类别页独有物品的英文名 ──
    # 英文索引页只覆盖它自己列的 1231 件；类别页独有的那批（多为唯一珠宝）
    # 不在其中。它们的英文名在**英文版类别页**上，按需只抓相关的几页，
    # 不是全部 106 页（实测 4 页就覆盖 107/110 件）。
    need_pages: dict[str, int] = {}
    for row in conn.execute(
        "SELECT attributes FROM entity WHERE entity_type='unique_item' "
        "AND (name_en IS NULL OR name_en = '')"
    ):
        page = json.loads(row["attributes"] or "{}").get("source_page", "")
        if page:
            need_pages[page] = need_pages.get(page, 0) + 1
    for page in sorted(need_pages, key=lambda p: -need_pages[p]):
        url_en = f"{poedb_index.EN_BASE}/{page}"
        try:
            r = fetcher.fetch(url_en, force=force)
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}")
            for slug, pair in poedb_unique.extract_en_index(r.text).items():
                en_map.setdefault(slug, pair)
        except Exception as exc:  # noqa: BLE001
            logger.warning("英文类别页 %s 取用失败：%s", page, exc)
    if need_pages:
        logger.info("英文类别页：%d 页（覆盖 %d 件缺英文名的物品）",
                    len(need_pages), sum(need_pages.values()))

    # ── 阶段 5：回填英文名与缺失的物品类别（卡片标题含英文名，须重渲重索引）──
    # 这一步对**全部**已有行生效，不只对本轮新增的——否则先前入库的行永远补不上。
    updated = en_filled = class_filled = 0
    to_refresh: list[Entity] = []
    for row in conn.execute("SELECT * FROM entity WHERE entity_type='unique_item'").fetchall():
        en = en_map.get(row["slug"])
        want_en = en[0] if en and en[0] else ""
        need_en = bool(want_en) and row["name_en"] != want_en

        page = json.loads(row["attributes"] or "{}").get("source_page", "")
        want_class = page_class.get(page, "")
        need_class = bool(want_class) and not (row["item_class"] or "")

        if not (need_en or need_class):
            continue
        entity = _entity_from_row(row)
        if need_en:
            entity.name_en = want_en
            if en[1]:
                entity.attributes["base_type_en"] = en[1]
            en_filled += 1
        if need_class:
            entity.item_class = want_class
            class_filled += 1
        entity.content_hash = _entity_hash(entity)
        to_refresh.append(entity)
        updated += 1
    indexed_upd = _index_and_alias(conn, to_refresh)
    conn.commit()
    logger.info("回填：英文名 %d 件，物品类别 %d 件", en_filled, class_filled)

    elapsed_ms = int((time.time() - started) * 1000)
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, "
        "failed=?, skipped=?, elapsed_ms=?, note=? WHERE run_id=?",
        (
            agg.total, len(added) + updated, agg.failed + len(detail_failed), agg.skipped,
            elapsed_ms,
            json.dumps(
                {
                    "class_page_new": len(added) - detail_added,
                    "detail_page_new": detail_added,
                    "name_en_filled": en_filled,
                    "item_class_filled": class_filled,
                    "chunks_indexed": indexed_new + indexed_upd,
                    "detail_failures": detail_failed[:10],
                    "extract_failures": agg.failures[:10],
                },
                ensure_ascii=False,
            ),
            run_id,
        ),
    )
    conn.commit()

    total_now = conn.execute(
        "SELECT count(*) FROM entity WHERE entity_type='unique_item'"
    ).fetchone()[0]
    with_en = conn.execute(
        "SELECT count(*) FROM entity WHERE entity_type='unique_item' "
        "AND name_en IS NOT NULL AND name_en <> ''"
    ).fetchone()[0]
    logger.info(
        "L2 补全完成：新增 %d 件（类别页 %d / 详情页 %d），回填英文名 %d / 类别 %d，"
        "重渲切片 %d；库内唯一装备 %d 件，其中有权威英文名 %d 件（%.1f%%），耗时 %dms",
        len(added), len(added) - detail_added, detail_added, en_filled, class_filled,
        indexed_new + indexed_upd,
        total_now, with_en, 100 * with_en / total_now if total_now else 0, elapsed_ms,
    )
    return 1 if detail_failed else 0


def _make_fetcher(cfg) -> Fetcher:  # noqa: ANN001
    return Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )


def _validate_bench_snapshot_size(
    conn, incoming_count: int, *, allow_shrink: bool = False  # noqa: ANN001
) -> dict[str, object]:
    """拒绝结构合法但明显截短的 destructive snapshot，并返回持久审计字段。"""
    existing_count = int(
        conn.execute("SELECT count(*) FROM craft_bench_recipe").fetchone()[0]
    )
    minimum_allowed = (
        math.ceil(existing_count * (1.0 - MAX_BENCH_SNAPSHOT_DROP_FRACTION))
        if existing_count else None
    )
    drop_ratio = (
        max(0.0, (existing_count - incoming_count) / existing_count)
        if existing_count else 0.0
    )
    guard_reason = ""
    if not existing_count and incoming_count < MIN_BENCH_SNAPSHOT_RECIPES:
        guard_reason = "below_absolute_minimum"
        message = (
            f"首次工艺台快照只有 {incoming_count} 条，低于安全下限 "
            f"{MIN_BENCH_SNAPSHOT_RECIPES}；拒绝建立目录"
        )
    elif minimum_allowed is not None and incoming_count < minimum_allowed:
        guard_reason = "relative_drop_exceeded"
        message = (
            f"工艺台快照从 {existing_count} 条骤降至 {incoming_count} 条，"
            f"超过允许的 {MAX_BENCH_SNAPSHOT_DROP_FRACTION:.0%}；"
            "可能是截短响应，拒绝覆盖旧目录"
        )
    else:
        message = ""

    applied = bool(allow_shrink and guard_reason)
    audit: dict[str, object] = {
        "allow_shrink_requested": allow_shrink,
        "allow_shrink_applied": applied,
        "existing_recipe_count": existing_count,
        "incoming_recipe_count": incoming_count,
        "drop_ratio": round(drop_ratio, 6),
        "minimum_allowed_count": minimum_allowed,
        "guard_reason": guard_reason or None,
    }
    if guard_reason and not allow_shrink:
        error = poedb_crafting.CraftingBenchStructureError(message)
        error.snapshot_audit = audit  # type: ignore[attr-defined]
        raise error
    if applied:
        logger.warning(
            "已显式允许工艺台目录缩减；跳过快照数量护栏（%s，%d → %d）",
            guard_reason,
            existing_count,
            incoming_count,
        )
    return audit


def _replace_bench_recipes(conn, recipes, fetched_at: str) -> tuple[int, int]:  # noqa: ANN001
    """用页面完整快照替换目录；调用方负责事务与数量护栏。"""
    conn.execute("DELETE FROM craft_bench_recipe_cost")
    conn.execute("DELETE FROM craft_bench_recipe")
    conn.executemany(
        """
        INSERT INTO craft_bench_recipe(
            recipe_id, effect_text, item_classes, unlock_text, unlock_url,
            unlock_hint, source, source_priority, url, content_hash,
            fetched_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        [
            (
                recipe.recipe_id,
                recipe.effect_text,
                json.dumps(list(recipe.item_classes), ensure_ascii=False),
                recipe.unlock_text,
                recipe.unlock_url,
                recipe.unlock_hint,
                "POEDB",
                1,
                recipe.url,
                recipe.content_hash,
                fetched_at,
            )
            for recipe in recipes
        ],
    )
    cost_rows = [
        (
            recipe.recipe_id,
            cost.position,
            cost.amount,
            cost.currency_slug,
            cost.currency_name_zh,
            cost.currency_url,
        )
        for recipe in recipes
        for cost in recipe.costs
    ]
    conn.executemany(
        """
        INSERT INTO craft_bench_recipe_cost(
            recipe_id, position, amount, currency_slug, currency_name_zh, currency_url
        ) VALUES (?,?,?,?,?,?)
        """,
        cost_rows,
    )
    return len(recipes), len(cost_rows)


def _commit_bench_snapshot(
    conn,  # noqa: ANN001
    recipes,  # noqa: ANN001
    *,
    fetched_at: str,
    run_id: int,
    total: int,
    elapsed_ms: int,
    note: str,
) -> tuple[int, int]:
    """原子提交目录快照与成功审计；任一步失败两者一起回滚。"""
    with conn:
        recipe_count, cost_count = _replace_bench_recipes(conn, recipes, fetched_at)
        cur = conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, "
            "failed=0, skipped=0, elapsed_ms=?, note=? WHERE run_id=?",
            (total, recipe_count, elapsed_ms, note, run_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"ingest_run {run_id} 不存在，拒绝提交无审计快照")
    return recipe_count, cost_count


def run_poedb_crafting(*, force: bool = False, allow_shrink: bool = False) -> int:
    """采集工艺工作台目录；不生成制作步骤或可执行性结论。"""
    cfg = get_config()
    conn = dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    fetcher = _make_fetcher(cfg)

    started = time.time()
    initial_note = json.dumps(
        {
            "allow_shrink_requested": allow_shrink,
            "allow_shrink_applied": False,
            "existing_recipe_count": None,
            "incoming_recipe_count": None,
            "drop_ratio": None,
            "minimum_allowed_count": None,
            "guard_reason": None,
            "status": "started",
        },
        ensure_ascii=False,
    )
    cur = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at, note) "
        "VALUES (?,?,datetime('now'),?)",
        ("POEDB", "crafting_bench", initial_note),
    )
    run_id = cur.lastrowid
    conn.commit()

    res = fetcher.fetch(poedb_crafting.CRAFTING_BENCH_URL, force=force)
    if not res.ok:
        conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'), failed=1, note=? WHERE run_id=?",
            (
                json.dumps(
                    {
                        "allow_shrink_requested": allow_shrink,
                        "allow_shrink_applied": False,
                        "existing_recipe_count": None,
                        "incoming_recipe_count": None,
                        "drop_ratio": None,
                        "minimum_allowed_count": None,
                        "guard_reason": None,
                        "status": "fetch_failed",
                        "error": f"HTTP {res.status_code}",
                    },
                    ensure_ascii=False,
                ),
                run_id,
            ),
        )
        conn.commit()
        logger.error("工艺台目录抓取失败：HTTP %s", res.status_code)
        return 1

    try:
        recipes, stats = poedb_crafting.extract_bench_recipes(
            res.text, poedb_crafting.CRAFTING_BENCH_URL
        )
        snapshot_audit = _validate_bench_snapshot_size(
            conn, len(recipes), allow_shrink=allow_shrink
        )
    except poedb_crafting.CraftingBenchStructureError as exc:
        rejected_audit = getattr(
            exc,
            "snapshot_audit",
            {
                "allow_shrink_requested": allow_shrink,
                "allow_shrink_applied": False,
                "existing_recipe_count": None,
                "incoming_recipe_count": None,
                "drop_ratio": None,
                "minimum_allowed_count": None,
                "guard_reason": None,
            },
        )
        conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'), total=?, failed=1, "
            "elapsed_ms=?, note=? WHERE run_id=?",
            (
                0,
                int((time.time() - started) * 1000),
                json.dumps(
                    {
                        **rejected_audit,
                        "status": "structure_rejected",
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                ),
                run_id,
            ),
        )
        conn.commit()
        raise

    elapsed_ms = int((time.time() - started) * 1000)
    cost_count = sum(len(recipe.costs) for recipe in recipes)
    note = json.dumps(
        {
            "page_content_hash": res.content_hash,
            "from_cache": res.from_cache,
            "costs": cost_count,
            "multi_cost_recipes": stats.multi_cost_recipes,
            "currencies": len(stats.currencies),
            "boundary": "catalog_only_no_execution_semantics",
            **snapshot_audit,
        },
        ensure_ascii=False,
    )
    # 快照替换与成功审计同属一个事务：中途崩溃不会留下「数据已换但 run 未完成」。
    recipe_count, committed_costs = _commit_bench_snapshot(
        conn,
        recipes,
        fetched_at=res.fetched_at,
        run_id=run_id,
        total=stats.total,
        elapsed_ms=elapsed_ms,
        note=note,
    )
    logger.info(
        "工艺台目录入库完成：配方 %d，费用子项 %d，双费用配方 %d，通货 %d 种，耗时 %dms",
        recipe_count,
        committed_costs,
        stats.multi_cost_recipes,
        len(stats.currencies),
        elapsed_ms,
    )
    return 0


def _validate_metamod_snapshot_size(
    conn, incoming_count: int, *, allow_shrink: bool = False  # noqa: ANN001
) -> dict[str, object]:
    """拒绝明显截短的矩阵快照；空白格已在 incoming_count 之外。"""
    existing_count = int(
        conn.execute("SELECT count(*) FROM craft_metamod_interaction").fetchone()[0]
    )
    minimum_allowed = (
        math.ceil(existing_count * (1.0 - MAX_METAMOD_SNAPSHOT_DROP_FRACTION))
        if existing_count else None
    )
    drop_ratio = (
        max(0.0, (existing_count - incoming_count) / existing_count)
        if existing_count else 0.0
    )
    guard_reason = ""
    if not existing_count and incoming_count < MIN_METAMOD_SNAPSHOT_INTERACTIONS:
        guard_reason = "below_absolute_minimum"
        message = (
            f"首次 Metamod 矩阵只有 {incoming_count} 个明确单元格，低于安全下限 "
            f"{MIN_METAMOD_SNAPSHOT_INTERACTIONS}；拒绝建立矩阵"
        )
    elif minimum_allowed is not None and incoming_count < minimum_allowed:
        guard_reason = "relative_drop_exceeded"
        message = (
            f"Metamod 矩阵从 {existing_count} 个明确单元格骤降至 {incoming_count} 个，"
            f"超过允许的 {MAX_METAMOD_SNAPSHOT_DROP_FRACTION:.0%}；拒绝覆盖旧快照"
        )
    else:
        message = ""

    applied = bool(allow_shrink and guard_reason)
    audit: dict[str, object] = {
        "allow_shrink_requested": allow_shrink,
        "allow_shrink_applied": applied,
        "existing_interaction_count": existing_count,
        "incoming_interaction_count": incoming_count,
        "drop_ratio": round(drop_ratio, 6),
        "minimum_allowed_count": minimum_allowed,
        "guard_reason": guard_reason or None,
    }
    if guard_reason and not allow_shrink:
        error = craftofexile_metamods.MetamodMatrixStructureError(message)
        error.snapshot_audit = audit  # type: ignore[attr-defined]
        raise error
    if applied:
        logger.warning(
            "已显式允许 Metamod 矩阵缩减；跳过数量护栏（%s，%d → %d）",
            guard_reason,
            existing_count,
            incoming_count,
        )
    return audit


def _replace_metamod_interactions(conn, interactions, fetched_at: str) -> int:  # noqa: ANN001
    """用全部明确单元格替换快照；空白源格永不写入事实表。"""
    conn.execute("DELETE FROM craft_metamod_interaction")
    conn.executemany(
        """
        INSERT INTO craft_metamod_interaction(
            interaction_id, section, operation_text, guard_text, outcome,
            raw_value, note_text, row_order, column_order, source,
            source_priority, source_url, mirror_url, content_hash, fetched_at,
            version_scope, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        [
            (
                item.interaction_id,
                item.section,
                item.operation_text,
                item.guard_text,
                item.outcome,
                item.raw_value,
                item.note_text,
                item.row_order,
                item.column_order,
                "Craft of Exile",
                4,
                item.source_url,
                item.mirror_url,
                item.content_hash,
                fetched_at,
                item.version_scope,
            )
            for item in interactions
        ],
    )
    return len(interactions)


def _commit_metamod_snapshot(
    conn,  # noqa: ANN001
    interactions,  # noqa: ANN001
    *,
    fetched_at: str,
    run_id: int,
    total: int,
    skipped: int,
    elapsed_ms: int,
    note: str,
) -> int:
    """原子提交矩阵事实、空白格审计和成功状态。"""
    with conn:
        interaction_count = _replace_metamod_interactions(
            conn, interactions, fetched_at
        )
        cur = conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, "
            "failed=0, skipped=?, elapsed_ms=?, note=? WHERE run_id=?",
            (total, interaction_count, skipped, elapsed_ms, note, run_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"ingest_run {run_id} 不存在，拒绝提交无审计快照")
    return interaction_count


def _finish_metamod_failure(
    conn,  # noqa: ANN001
    *,
    run_id: int,
    started: float,
    status: str,
    error: BaseException,
    audit: dict[str, object] | None = None,
) -> None:
    """尽力把未预期异常收口为终态审计；只记录异常类型，不泄露路径或 SQL。"""
    try:
        conn.rollback()
        note = {
            **(audit or {}),
            "status": status,
            "error_type": type(error).__name__,
        }
        with conn:
            cur = conn.execute(
                "UPDATE ingest_run SET finished_at=datetime('now'), failed=1, "
                "elapsed_ms=?, note=? WHERE run_id=?",
                (
                    int((time.time() - started) * 1000),
                    json.dumps(note, ensure_ascii=False),
                    run_id,
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"ingest_run {run_id} 不存在")
    except Exception as audit_exc:  # noqa: BLE001
        logger.error(
            "Metamod 失败终态审计写入失败（原异常 %s，审计异常 %s）",
            type(error).__name__,
            type(audit_exc).__name__,
        )


def run_poedb_metamods(*, force: bool = False, allow_shrink: bool = False) -> int:
    """采集 Craft of Exile 元词缀矩阵镜像；只保存页面逐格事实。"""
    cfg = get_config()
    conn = dbmod.init_db(
        cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0)
    )
    fetcher = _make_fetcher(cfg)
    started = time.time()
    initial_note = json.dumps(
        {
            "status": "started",
            "allow_shrink_requested": allow_shrink,
            "allow_shrink_applied": False,
            "existing_interaction_count": None,
            "incoming_interaction_count": None,
            "drop_ratio": None,
            "minimum_allowed_count": None,
            "guard_reason": None,
        },
        ensure_ascii=False,
    )
    cur = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at, note) "
        "VALUES (?,?,datetime('now'),?)",
        ("Craft of Exile", "metamod_matrix", initial_note),
    )
    run_id = cur.lastrowid
    conn.commit()

    try:
        res = fetcher.fetch(craftofexile_metamods.METAMOD_MIRROR_URL, force=force)
    except Exception as exc:  # noqa: BLE001
        _finish_metamod_failure(
            conn,
            run_id=run_id,
            started=started,
            status="fetch_exception",
            error=exc,
            audit={"allow_shrink_requested": allow_shrink},
        )
        raise
    if not res.ok:
        conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'), failed=1, note=? "
            "WHERE run_id=?",
            (
                json.dumps(
                    {
                        "status": "fetch_failed",
                        "error": f"HTTP {res.status_code}",
                        "allow_shrink_requested": allow_shrink,
                        "allow_shrink_applied": False,
                    },
                    ensure_ascii=False,
                ),
                run_id,
            ),
        )
        conn.commit()
        logger.error("Metamod 矩阵镜像抓取失败：HTTP %s", res.status_code)
        return 1

    try:
        interactions, stats = craftofexile_metamods.extract_metamod_interactions(
            res.text, craftofexile_metamods.METAMOD_MIRROR_URL
        )
        snapshot_audit = _validate_metamod_snapshot_size(
            conn, len(interactions), allow_shrink=allow_shrink
        )
    except craftofexile_metamods.MetamodMatrixStructureError as exc:
        rejected_audit = getattr(
            exc,
            "snapshot_audit",
            {
                "allow_shrink_requested": allow_shrink,
                "allow_shrink_applied": False,
                "existing_interaction_count": None,
                "incoming_interaction_count": None,
                "drop_ratio": None,
                "minimum_allowed_count": None,
                "guard_reason": None,
            },
        )
        conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'), failed=1, elapsed_ms=?, "
            "note=? WHERE run_id=?",
            (
                int((time.time() - started) * 1000),
                json.dumps(
                    {**rejected_audit, "status": "structure_rejected", "error": str(exc)},
                    ensure_ascii=False,
                ),
                run_id,
            ),
        )
        conn.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        _finish_metamod_failure(
            conn,
            run_id=run_id,
            started=started,
            status="extract_exception",
            error=exc,
            audit={"allow_shrink_requested": allow_shrink},
        )
        raise

    elapsed_ms = int((time.time() - started) * 1000)
    try:
        blank_cells = [cell.canonical() for cell in stats.blank_cells]
        note = json.dumps(
            {
                "page_content_hash": res.content_hash,
                "from_cache": res.from_cache,
                "sections": stats.sections,
                "operation_rows": stats.operation_rows,
                "explicit_cells": stats.explicit_cells,
                "blank_cells": blank_cells,
                "outcomes": stats.outcomes,
                "version_scope": craftofexile_metamods.VERSION_SCOPE_UNKNOWN,
                "source_url": craftofexile_metamods.METAMOD_SOURCE_URL,
                "mirror_url": craftofexile_metamods.METAMOD_MIRROR_URL,
                "boundary": "matrix_cells_only_no_execution_or_route_semantics",
                **snapshot_audit,
            },
            ensure_ascii=False,
        )
        committed = _commit_metamod_snapshot(
            conn,
            interactions,
            fetched_at=res.fetched_at,
            run_id=run_id,
            total=stats.total_cells,
            skipped=len(stats.blank_cells),
            elapsed_ms=elapsed_ms,
            note=note,
        )
    except Exception as exc:  # noqa: BLE001
        _finish_metamod_failure(
            conn,
            run_id=run_id,
            started=started,
            status="commit_exception",
            error=exc,
            audit=snapshot_audit,
        )
        raise
    logger.info(
        "Metamod 矩阵入库完成：分区 %d，操作 %d，明确单元格 %d，空白缺口 %d，"
        "版本范围 unknown，耗时 %dms",
        stats.sections,
        stats.operation_rows,
        committed,
        len(stats.blank_cells),
        elapsed_ms,
    )
    return 0


def run_harvest(*, force: bool = False) -> int:
    """把索引页与全部词缀类别页抓到本地缓存。

    抓取即本地化：之后所有抽取器开发与迭代都从 data/http_cache 离线进行，
    不再反复请求站点。

    结果记入 `ingest_run` 台账：缓存新鲜度是能否回答当前赛季问题的前提，
    「上次抓取是什么时候、抓了多少页、有没有失败」必须可查，不能只留在日志里。
    """
    cfg = get_config()
    conn = dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    fetcher = _make_fetcher(cfg)

    started = time.time()
    cur = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at) VALUES (?,?,datetime('now'))",
        ("POEDB", "harvest"),
    )
    run_id = cur.lastrowid
    conn.commit()

    stats = poedb_index.harvest(fetcher, force=force)
    elapsed_ms = int((time.time() - started) * 1000)

    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, "
        "failed=?, skipped=?, elapsed_ms=?, note=? WHERE run_id=?",
        (
            stats.total, stats.fetched + stats.cached, stats.failed, stats.cached,
            elapsed_ms,
            json.dumps(
                {
                    "fetched": stats.fetched,
                    "from_cache": stats.cached,
                    "bytes_total": stats.bytes_total,
                    "failures": stats.failures[:10],
                },
                ensure_ascii=False,
            ),
            run_id,
        ),
    )
    conn.commit()

    logger.info(
        "抓取完成：%d 页（新抓 %d，缓存 %d，失败 %d），共 %.1f MB，耗时 %dms",
        stats.total, stats.fetched, stats.cached, stats.failed,
        stats.bytes_total / 1048576, elapsed_ms,
    )
    for slug, reason in stats.failures[:20]:
        logger.warning("  失败 %s: %s", slug, reason)
    return 1 if stats.failed else 0


def _upsert_mods(conn, records: list) -> int:  # noqa: ANN001
    rows = [
        (
            m.uid, m.mod_code, m.item_class_page, m.item_class_code, m.item_class_zh,
            m.category, m.category_label, m.name_zh, m.gen_type, m.gen_label,
            json.dumps(m.family_list, ensure_ascii=False), m.correct_group,
            m.ilvl, m.tier, m.tier_count, m.tier_source, m.weight, m.weight_pct,
            m.text_zh, m.text_template, m.loose_template,
            json.dumps([[a, b] for a, b in m.ranges]),
            json.dumps(m.tags, ensure_ascii=False),
            json.dumps(m.spawn_tags, ensure_ascii=False),
            json.dumps(m.fossil_tags, ensure_ascii=False),
            m.variant_id, "POEDB", 1, m.url,
        )
        for m in records
    ]
    conn.executemany(
        """
        INSERT INTO mod_affix(
            uid, mod_code, item_class_page, item_class_code, item_class_zh,
            category, category_label, name_zh, gen_type, gen_label,
            family_list, correct_group, ilvl, tier, tier_count, tier_source,
            weight, weight_pct, text_zh, text_template, loose_template, ranges,
            tags, spawn_tags, fossil_tags, variant_id,
            source, source_priority, url, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(uid) DO UPDATE SET
            tier=excluded.tier, tier_count=excluded.tier_count,
            weight=excluded.weight, weight_pct=excluded.weight_pct,
            text_zh=excluded.text_zh, text_template=excluded.text_template,
            loose_template=excluded.loose_template,
            ranges=excluded.ranges, updated_at=datetime('now')
        """,
        rows,
    )
    return len(rows)


def run_poedb_mods(*, force: bool = False, rebuild: bool = False) -> int:
    """抽取全部 101 个底材类别页的词缀并入库。"""
    cfg = get_config()
    conn = dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    fetcher = _make_fetcher(cfg)

    started = time.time()
    cur = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at) VALUES (?,?,datetime('now'))",
        ("POEDB", "mod_affix"),
    )
    run_id = cur.lastrowid

    if rebuild:
        logger.info("rebuild：清空 mod_affix")
        conn.execute("DELETE FROM mod_affix")

    slugs = poedb_index.discover_mod_class_pages(fetcher, force=force)
    total_records = 0
    pages_ok = pages_nodata = pages_failed = 0
    conflicts = 0
    failures: list[tuple[str, str]] = []

    for i, slug in enumerate(slugs, 1):
        url = f"{poedb_index.BASE}/{slug}"
        try:
            res = fetcher.fetch(url, force=force)
            if not res.ok:
                raise RuntimeError(f"HTTP {res.status_code}")
            mods, stats = poedb_mods.extract_mods(res.text, slug, url)
        except poedb_mods.NoModsDataError:
            pages_nodata += 1
            logger.info("[%d/%d] %-32s 无常规词缀池（合法）", i, len(slugs), slug)
            continue
        except Exception as exc:
            pages_failed += 1
            failures.append((slug, f"{type(exc).__name__}: {exc}"))
            logger.error("[%d/%d] %-32s 失败：%s", i, len(slugs), slug, exc)
            continue

        # 去重冲突必须为 0，否则说明主键设计有误、正在丢数据
        if stats.dedup_conflicts:
            conflicts += stats.dedup_conflicts
            logger.error(
                "[%d/%d] %s 出现 %d 处去重冲突（实质字段不同却同键），拒绝写入该页",
                i, len(slugs), slug, stats.dedup_conflicts,
            )
            pages_failed += 1
            failures.append((slug, f"去重冲突 {stats.dedup_conflicts} 处"))
            continue

        written = _upsert_mods(conn, mods)
        total_records += written
        pages_ok += 1
        logger.info("[%d/%d] %-32s %5d 条（%d 组）", i, len(slugs), slug,
                    written, len(stats.categories))

    if conflicts:
        conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'), failed=?, note=? WHERE run_id=?",
            (pages_failed, f"去重冲突共 {conflicts} 处，已拒绝相关页面", run_id),
        )
        conn.commit()
        logger.error("存在去重冲突，词缀库可能不完整。请检查主键设计。")
        return 3

    elapsed_ms = int((time.time() - started) * 1000)
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, "
        "failed=?, skipped=?, elapsed_ms=?, note=? WHERE run_id=?",
        (
            len(slugs), pages_ok, pages_failed, pages_nodata, elapsed_ms,
            json.dumps({"records": total_records, "no_mods_pages": pages_nodata,
                        "failures": failures[:10]}, ensure_ascii=False),
            run_id,
        ),
    )
    conn.commit()

    # 词表须在渲染前刷新：底材类别名（含单字「弓」「爪」）刚入库，
    # 查询侧靠它把「弓上的…」正确扫成「弓」，否则物品类别这一维会在检索中丢失
    vocab_mod.build_vocab(conn)

    # 汇总成 T 级梯度卡片并建 FTS 索引，使词缀可被自然语言检索
    chunks = build_mod_chunks(conn)
    # **总是**清空后重建，不只在 --rebuild 时清。
    # `build_mod_chunks` 每次产出的是全量卡片（它不做增量），而 chunk_id 由
    # entity_id 派生。粒度或命名一变，旧 id 的卡片就再也不会被覆盖，只会以
    # 孤儿身份留在索引里继续被检索命中——实测粒度从
    # (类别,来源,前后缀,词缀组) 并成 (类别,词缀组) 时正是这种情形。
    fts_mod.clear_index(conn, entity_prefix=MOD_LADDER_PREFIX)
    indexed = fts_mod.index_chunks(conn, chunks)
    logger.info("词缀梯度卡片：%d 张，已索引 %d 张", len(chunks), indexed)
    conn.commit()

    counts = conn.execute(
        "SELECT count(*) n, count(DISTINCT mod_code) codes, "
        "count(DISTINCT item_class_page) classes, count(DISTINCT category) cats "
        "FROM mod_affix"
    ).fetchone()
    logger.info(
        "词缀入库完成：%d 条记录，%d 个不同词缀，%d 个底材类别，%d 个来源组；"
        "页面 成功%d/无池%d/失败%d，耗时 %dms",
        counts["n"], counts["codes"], counts["classes"], counts["cats"],
        pages_ok, pages_nodata, pages_failed, elapsed_ms,
    )
    return 1 if pages_failed else 0


def run_passive_tree(*, force: bool = False, targets: list[str] | None = None) -> int:
    """采集天赋树与 Atlas 天赋树。

    **每次全量重建**，不做增量。两个原因：
      1. 实体只有两千级，全量重建比增量对账便宜得多；
      2. `entity_id` 里带内容哈希（同名节点按内容分成多个实体，见
         `poedb_tree` 的模块说明），内容一变就是新 id，旧 id 不会被 UPSERT
         覆盖，只会以孤儿身份留在索引里继续被检索命中。按前缀清干净是唯一
         可靠的做法——这与词缀梯度卡踩过的坑是同一个。
    """
    cfg = get_config()
    conn = dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    started = time.time()
    cur = conn.execute(
        "INSERT INTO ingest_run(source, category, started_at) VALUES (?,?,datetime('now'))",
        ("POEDB", "passive_tree"),
    )
    run_id = cur.lastrowid

    fetcher = Fetcher(
        cache_dir=cfg.cache_dir,
        user_agent=str(cfg.get("crawl.user_agent")),
        requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
        timeout=float(cfg.get("crawl.timeout_seconds", 60)),
        cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
    )

    wanted = targets or [poedb_tree.TARGET_PASSIVE, poedb_tree.TARGET_ATLAS]
    note: dict = {"trees": {}}
    total_entities = total_indexed = total_alias = 0

    for target in wanted:
        version = poedb_tree.discover_version(fetcher, target, force=force)
        ep_zh = poedb_tree.TreeEndpoint(target, version, poedb_tree.LANG_ZH)
        ep_en = poedb_tree.TreeEndpoint(target, version, poedb_tree.LANG_EN)

        payload_zh = poedb_tree.fetch_payload(fetcher, ep_zh, force=force)
        # 英文名取不到不阻塞采集：中文名才是必需的，英文名缺了只是英文检索差一点。
        # 但缺多少必须可见，不能静默。
        try:
            payload_en = poedb_tree.fetch_payload(fetcher, ep_en, force=force)
        except poedb_tree.TreeStructureError as exc:
            logger.warning("%s 英文版取不到（%s），本轮 name_en 将为空", target, exc)
            payload_en = None

        nodes, st = poedb_tree.extract(payload_zh, payload_en, ep_zh)
        entities = [poedb_tree.node_to_entity(n, ep_zh) for n in nodes]
        logger.info(
            "%s v%s：节点 %d（带内容 %d / %.0f%%），合并为 %d 个实体 %s；缺英文名 %d",
            target, version, st.total_nodes, st.with_content, st.content_ratio * 100,
            st.merged, st.by_kind, st.missing_en,
        )

        # 一棵树可能产出多种实体类型（天赋树里的升华节点独立成域），
        # 必须把每一种的前缀都清掉——漏一种就留下孤儿卡片继续被检索命中
        for etype in ep_zh.entity_types:
            fts_mod.clear_index(conn, entity_prefix=f"{etype.value}:")
            alias_mod.clear_aliases(conn, entity_prefix=f"{etype.value}:")
            conn.execute("DELETE FROM entity WHERE entity_type=?", (etype.value,))

        for e in entities:
            e.content_hash = _entity_hash(e)
            _upsert_entity(conn, e)
        vocab_mod.build_vocab(conn)

        indexed = 0
        for e in entities:
            indexed += fts_mod.index_chunks(conn, render_tree.to_chunks(e))

        # 别名只收有专名的节点。普通天赋的名字就是它给的属性（「火焰伤害」「生命」），
        # 入表会让装备词缀类问题被强行注入天赋卡——见 poedb_tree.NAMED_KINDS。
        aliasable = poedb_tree.aliasable_entities(entities)
        n_alias = alias_mod.index_aliases(conn, aliasable)

        total_entities += len(entities)
        total_indexed += indexed
        total_alias += n_alias
        note["trees"][target] = {
            "version": version,
            "nodes": st.total_nodes,
            "with_content": st.with_content,
            "entities": len(entities),
            "by_kind": st.by_kind,
            "chunks": indexed,
            "aliased": len(aliasable),
            "missing_en": st.missing_en,
            "skipped_empty": st.skipped_empty,
        }
        logger.info(
            "%s 入库：实体 %d，卡片 %d，别名实体 %d/%d（只收有专名的种类）",
            target, len(entities), indexed, len(aliasable), len(entities),
        )

    elapsed_ms = int((time.time() - started) * 1000)
    conn.execute(
        "UPDATE ingest_run SET finished_at=datetime('now'), total=?, succeeded=?, "
        "failed=0, skipped=0, elapsed_ms=?, note=? WHERE run_id=?",
        (total_entities, total_entities, elapsed_ms,
         json.dumps(note, ensure_ascii=False), run_id),
    )
    conn.commit()
    logger.info("天赋树采集完成：实体 %d，卡片 %d，别名 %d 条，耗时 %dms",
                total_entities, total_indexed, total_alias, elapsed_ms)
    return 0


def run_coverage() -> int:
    """探测并刷新覆盖声明。

    每次采集之后都会自动跑（见 `main`）——覆盖声明一旦滞后于实际数据，
    它就从「诚实的边界说明」变成「看起来可信的假话」，比没有更糟。
    """
    cfg = get_config()
    conn = dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))
    n = coverage_mod.refresh(conn, cache_dir=cfg.cache_dir)
    conn.commit()

    rows = coverage_mod.load(conn)
    usable = [s for s in rows if s.usable]
    logger.info(
        "覆盖声明：%d 个领域，可用 %d，未采集 %d",
        n, len(usable), len(rows) - len(usable),
    )
    for s in rows:
        logger.info("  [%-13s] %-28s %8s 条  %s",
                    s.status, s.spec.display_zh, f"{s.item_count:,}" if s.item_count else "-",
                    s.spec.authority_url if not s.usable else "")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="POE1-Agent 采集管线")
    ap.add_argument("--source", default="poedb", choices=["poedb"])
    ap.add_argument(
        "--category", default="unique_item",
        choices=[
            "unique_item", "unique_l2", "mods", "bench", "metamods", "tree",
            "coverage", "all",
        ],
        help="unique_item=唯一装备索引页, unique_l2=类别页补集+权威英文名+详情页兜底, "
             "mods=词缀, bench=工艺工作台目录与原始费用, "
             "metamods=Craft of Exile 元词缀交互矩阵（POEDB 镜像）, "
             "tree=天赋树与 Atlas 天赋树, coverage=只刷新覆盖声明, all=全部",
    )
    ap.add_argument("--harvest", action="store_true",
                    help="只抓取索引页与词缀类别页到本地缓存，不抽取")
    ap.add_argument("--force", action="store_true", help="强制重新抓取（忽略缓存）")
    ap.add_argument("--rebuild", action="store_true", help="清空后重建")
    ap.add_argument(
        "--allow-bench-shrink",
        action="store_true",
        help="显式允许工艺台目录大幅缩减；仅在人工核对上游确实删项后使用",
    )
    ap.add_argument(
        "--allow-metamod-shrink",
        action="store_true",
        help="显式允许 Metamod 矩阵大幅缩减；仅在人工核对源页面后使用",
    )
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = get_config()
    setup_logging(cfg.logs_dir, str(cfg.get("logging.level", "INFO")))

    try:
        if args.harvest:
            return run_harvest(force=args.force)

        rc = 0
        if args.category in ("unique_item", "all"):
            rc |= run_poedb_unique(force=args.force, rebuild=args.rebuild)
        # L2 必须在索引页之后跑：它以索引页已入库的 slug 集合为「已知」基准
        if args.category in ("unique_l2", "all"):
            rc |= run_unique_l2(force=args.force)
        if args.category in ("mods", "all"):
            rc |= run_poedb_mods(force=args.force, rebuild=args.rebuild)
        if args.category in ("bench", "all"):
            rc |= run_poedb_crafting(
                force=args.force, allow_shrink=args.allow_bench_shrink
            )
        if args.category in ("metamods", "all"):
            rc |= run_poedb_metamods(
                force=args.force, allow_shrink=args.allow_metamod_shrink
            )
        if args.category in ("tree", "all"):
            rc |= run_passive_tree(force=args.force)
        # 覆盖声明总是最后刷新：它的每个数字都来自库里的实际行数，
        # 任何一次采集之后不刷新，它就开始说假话。
        rc |= run_coverage()
        return rc
    except poedb_unique.StructureError as exc:
        logger.error("结构断言失败，已拒绝写入：%s", exc)
        return 2
    except poedb_mods.ModStructureError as exc:
        logger.error("词缀结构断言失败，已拒绝写入：%s", exc)
        return 2
    except poedb_crafting.CraftingBenchStructureError as exc:
        logger.error("工艺台目录结构断言失败，已拒绝写入：%s", exc)
        return 2
    except craftofexile_metamods.MetamodMatrixStructureError as exc:
        logger.error("Metamod 矩阵结构断言失败，已拒绝写入：%s", exc)
        return 2
    except poedb_tree.TreeStructureError as exc:
        logger.error("天赋树结构断言失败，已拒绝写入：%s", exc)
        return 2
    finally:
        dbmod.close_db()


if __name__ == "__main__":
    raise SystemExit(main())
