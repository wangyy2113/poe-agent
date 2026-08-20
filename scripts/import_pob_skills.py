#!/usr/bin/env python3
"""离线导入技能石与辅助宝石（PoB 出结构，POEDB 出中文名）。

读本地 PoB ``Data/Gems.lua`` + ``Data/Skills/*.lua`` 与已缓存的 POEDB 宝石清单页。
**默认不发起网络请求**；PoB commit 未钉住时拒绝导入（引用必须可复现）。

桥不上的条目按成因写进缺口台账，不静默丢弃——判据见 `kb/sources/pob_skills.py`
的模块 docstring。

    python scripts/import_pob_skills.py --dry-run    # 只清点
    python scripts/import_pob_skills.py              # 导入并重建卡片、别名
    python scripts/import_pob_skills.py --fetch      # 缺页时联网补抓（遵守限速）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calc import constants  # noqa: E402
from core import db as dbmod  # noqa: E402
from core.config import get_config  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402
from entities.models import Entity, SourceRef  # noqa: E402
from entities.taxonomy import EntityType, Source, make_entity_id  # noqa: E402
from kb import coverage as cov  # noqa: E402
from kb import render_skills  # noqa: E402
from kb.fetcher import Fetcher, cache_path_for  # noqa: E402
from kb.sources.pob import PoBNotFound, PoBRepo, load_repo  # noqa: E402
from kb.sources.pob_skills import (  # noqa: E402
    POEDB_GEM_URL,
    GemStructureError,
    SkillGem,
    SkippedGem,
    extract_gems,
)
from retrieval import alias as alias_mod  # noqa: E402
from retrieval import fts as fts_mod  # noqa: E402
from retrieval import vocab as vocab_mod  # noqa: E402

#: 数量护栏。2026-08-18 实测可桥接 810 条，留出上游删项余量。
MIN_TOTAL_GEMS = 700
MAX_RELATIVE_DROP = 0.20

_ENTITY_TYPE = {"active": EntityType.SKILL, "support": EntityType.SUPPORT_GEM}


def gem_rows(gem: SkillGem, repo: PoBRepo, priority: int) -> tuple[tuple, list[tuple]]:
    """一颗宝石 → (宝石行, 等级行列表)。"""
    head = (
        gem.gem_id,
        gem.name_en,
        gem.name_zh,
        json.dumps(gem.name_zh_alt, ensure_ascii=False),
        gem.poedb_slug,
        gem.gem_kind,
        gem.attr_color,
        json.dumps(gem.tags, ensure_ascii=False),
        gem.tag_string,
        json.dumps(gem.tags_zh, ensure_ascii=False),
        gem.natural_max_level,
        gem.cast_time,
        json.dumps(gem.skill_types, ensure_ascii=False),
        int(gem.is_vaal),
        int(gem.is_transfigured),
        gem.description_en,
        gem.source_file,
        Source.POB.value,
        priority,
        repo.blob_url(gem.source_file),
        gem.poedb_url,
        repo.commit,
        repo.version_label,
        repo.game_version,
        gem.content_hash,
    )
    levels = [
        (
            gem.gem_id,
            lv.gem_level,
            lv.level_requirement,
            json.dumps(lv.cost, ensure_ascii=False, sort_keys=True),
            lv.cooldown,
            lv.stored_uses,
            lv.crit_chance,
            lv.mana_multiplier,
            lv.attack_speed_multiplier,
            lv.reservation_percent,
            lv.reservation_flat,
            lv.base_damage_min,
            lv.base_damage_max,
            lv.damage_type,
            lv.damage_effectiveness,
        )
        for lv in gem.levels
    ]
    return head, levels


def to_entity(gem: SkillGem, repo: PoBRepo, priority: int) -> Entity:
    """宝石 → 实体。

    `ref.url` 用 PoB 的钉 commit blob 地址：卡片上的数值全部来自那个文件，
    引用必须指向数值的实际出处。中文名的出处（POEDB 页面）另存 `poedb_url`，
    卡片末尾单列一行。
    """
    entity_type = _ENTITY_TYPE[gem.gem_kind]
    return Entity(
        # id 用 **POEDB slug**，与 `make_entity_id` 的约定和全仓库其它实体一致
        # （`unique_item:Kaoms_Heart`）。PoB 侧的 `gem_id` 放进 attributes：
        # 两者都唯一（表上各有 UNIQUE 约束），但按 slug 建 id 才能让
        # `tests/golden/loader.matches_slug` 与引用装配用同一套写法。
        entity_id=make_entity_id(entity_type, gem.poedb_slug),
        entity_type=entity_type,
        name_zh=gem.name_zh,
        name_en=gem.name_en,
        slug=gem.poedb_slug,
        attributes={
            "gem_id": gem.gem_id,  # 回指 pob_skill_gem / pob_skill_level 的主键
            "gem_kind": gem.gem_kind,
            "attr_color": gem.attr_color,
            "name_zh_alt": list(gem.name_zh_alt),
            "tags": list(gem.tags),
            "tags_zh": list(gem.tags_zh),
            "tag_string": gem.tag_string,
            "natural_max_level": gem.natural_max_level,
            "cast_time": gem.cast_time,
            "skill_types": list(gem.skill_types),
            "is_vaal": gem.is_vaal,
            "is_transfigured": gem.is_transfigured,
            "description_en": gem.description_en,
            "source_file": gem.source_file,
            "poedb_url": gem.poedb_url,
            "levels": [
                {
                    "gem_level": lv.gem_level,
                    "level_requirement": lv.level_requirement,
                    "cost": lv.cost,
                    "cooldown": lv.cooldown,
                    "stored_uses": lv.stored_uses,
                    "crit_chance": lv.crit_chance,
                    "mana_multiplier": lv.mana_multiplier,
                    "attack_speed_multiplier": lv.attack_speed_multiplier,
                    "reservation_percent": lv.reservation_percent,
                    "reservation_flat": lv.reservation_flat,
                    "base_damage_min": lv.base_damage_min,
                    "base_damage_max": lv.base_damage_max,
                    "damage_type": lv.damage_type,
                    # 卡片刻意不渲染它（见 render_skills 模块说明），
                    # 但结构化字段留着——工具要按等级给完整数据
                    "damage_effectiveness": lv.damage_effectiveness,
                }
                for lv in gem.levels
            ],
        },
        search_keywords=list(gem.tags_zh),
        ref=SourceRef(
            source=Source.POB.value,
            url=repo.blob_url(gem.source_file),
            priority=priority,
        ),
        version=repo.version_label,
        content_hash=gem.content_hash,
    )


def validate_snapshot_size(conn, incoming: int, *, allow_shrink: bool = False) -> None:  # noqa: ANN001
    existing = int(conn.execute("SELECT count(*) FROM pob_skill_gem").fetchone()[0])
    reason = ""
    if incoming < MIN_TOTAL_GEMS:
        reason = f"低于绝对下限 {MIN_TOTAL_GEMS}（incoming={incoming}）"
    elif existing and incoming < existing * (1 - MAX_RELATIVE_DROP):
        reason = f"相对缩减超过 {MAX_RELATIVE_DROP:.0%}（{existing} -> {incoming}）"
    if reason and not allow_shrink:
        raise GemStructureError(f"拒绝替换宝石快照：{reason}")


def replace_snapshot(
    conn, gems: list[SkillGem], repo: PoBRepo, priority: int  # noqa: ANN001
) -> tuple[int, int]:
    """整体替换宝石与等级两张表。

    全量替换而不是增量：`gem_id` 稳定，但等级行数会随版本变化，增量对账的成本
    远高于重建 810 行 + 3 万行。等级表靠外键 ON DELETE CASCADE 跟着清。
    """
    heads: list[tuple] = []
    levels: list[tuple] = []
    for gem in gems:
        head, level_rows = gem_rows(gem, repo, priority)
        heads.append(head)
        levels.extend(level_rows)
    conn.execute("DELETE FROM pob_skill_gem")
    conn.execute("DELETE FROM pob_skill_level")
    conn.executemany(
        """
        INSERT INTO pob_skill_gem(
            gem_id,name_en,name_zh,name_zh_alt,poedb_slug,gem_kind,attr_color,
            tags,tag_string,tags_zh,natural_max_level,cast_time,skill_types,
            is_vaal,is_transfigured,description_en,source_file,source,source_priority,
            pob_url,poedb_url,source_commit,pob_version,game_version,content_hash,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        """,
        heads,
    )
    conn.executemany(
        """
        INSERT INTO pob_skill_level(
            gem_id,gem_level,level_requirement,cost,cooldown,stored_uses,crit_chance,
            mana_multiplier,attack_speed_multiplier,reservation_percent,
            reservation_flat,base_damage_min,base_damage_max,damage_type,
            damage_effectiveness
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        levels,
    )
    return len(heads), len(levels)


def rebuild_index(conn, entities: list[Entity]) -> tuple[int, int]:  # noqa: ANN001
    """重建实体、卡片与别名。

    按 entity_type 前缀先清干净再写：`gem_id` 稳定所以 UPSERT 也能覆盖，但上游删掉
    一颗宝石时旧行会以孤儿身份留在索引里继续被检索命中——天赋树那边踩过同一个坑。
    """
    from kb.pipeline import _upsert_entity  # noqa: PLC0415  复用同一套写法，避免两份 SQL

    for entity_type in (EntityType.SKILL, EntityType.SUPPORT_GEM):
        prefix = f"{entity_type.value}:"
        fts_mod.clear_index(conn, entity_prefix=prefix)
        alias_mod.clear_aliases(conn, entity_prefix=prefix)
        conn.execute("DELETE FROM entity WHERE entity_type = ?", (entity_type.value,))

    for entity in entities:
        _upsert_entity(conn, entity)
    vocab_mod.build_vocab(conn)

    chunks = 0
    for entity in entities:
        chunks += fts_mod.index_chunks(conn, render_skills.to_chunks(entity))
    aliases = alias_mod.index_aliases(conn, entities)
    return chunks, aliases


def record_skipped(conn, skipped: list[SkippedGem]) -> int:  # noqa: ANN001
    """按成因聚合登记进缺口台账。

    逐条写会用 62 行淹掉台账里真正的采集待办；只写一个总数则把「PoB 没发」与
    「我们取不到中文名」混成一件事——那正是上一轮在底材上修掉的错误。
    """
    groups: dict[str, list[SkippedGem]] = {}
    for item in skipped:
        groups.setdefault(item.reason, []).append(item)
    for reason, items in sorted(groups.items()):
        sample = "、".join(i.name or i.gem_key for i in items[:3])
        point = (
            f"[{reason}] {len(items)} 颗宝石未能在 PoB 与 POEDB 之间桥接："
            f"{sample}{' 等' if len(items) > 3 else ''}（{items[0].detail}）"
        )
        conn.execute(
            """
            INSERT INTO knowledge_gap(question, missing_point, guessed_category,
                                      kind, hit_count, first_seen, last_seen)
            VALUES (?,?,?,?,1,datetime('now'),datetime('now'))
            ON CONFLICT(question, missing_point) DO UPDATE SET
                hit_count = hit_count + 1, last_seen = datetime('now')
            """,
            ("技能石与辅助宝石的完整清单", point[:500], "partial/skill_gem", "ingest_gap"),
        )
    return len(groups)


def _poedb_html(*, fetcher: Fetcher | None) -> str:
    cfg = get_config()
    path = cache_path_for(cfg.cache_dir, POEDB_GEM_URL)
    if path.is_file():
        return path.read_text(encoding="utf-8", errors="ignore")
    if fetcher is None:
        raise GemStructureError(
            f"缺少本地缓存 {POEDB_GEM_URL}（{path.name}）；加 --fetch 允许联网补抓"
        )
    return fetcher.fetch(POEDB_GEM_URL).text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pob-path", default=None)
    parser.add_argument("--dry-run", action="store_true", help="只解析和体检，不写库")
    parser.add_argument("--fetch", action="store_true",
                        help="缺少 POEDB 页面缓存时联网补抓（遵守 robots 与限速）")
    parser.add_argument("--allow-shrink", action="store_true",
                        help="显式允许低于下限或大幅缩减（仅人工确认上游变化后使用）")
    args = parser.parse_args()
    cfg = get_config()
    setup_logging(cfg.logs_dir, "WARNING")
    # 先开库：基础伤害要从常量表取两个 gameConstants（见下）
    dbmod.init_db(cfg.db_path, vector_dim=int(cfg.get("retrieval.vector.dim", 0) or 0))

    fetcher = None
    if args.fetch:
        fetcher = Fetcher(
            cache_dir=cfg.cache_dir,
            user_agent=str(cfg.get("crawl.user_agent")),
            requests_per_second=float(cfg.get("crawl.requests_per_second", 0.5)),
            timeout=float(cfg.get("crawl.timeout_seconds", 60)),
            cache_ttl_hours=int(cfg.get("crawl.cache_ttl_hours", 24)),
        )

    try:
        repo = load_repo(args.pob_path, cfg)
        if not repo.reproducible or len(repo.commit) != 40:
            raise GemStructureError("PoB commit 未钉住，拒绝导入不可复现引用")
        started = time.monotonic()
        # 基础伤害要两个 gameConstants；常量表还没导入时就不算那三列（留 NULL），
        # 而不是拿默认值凑一个数字出来
        try:
            game_constants = {
                "base": constants.get("gameConstants.SkillDamageBaseEffectiveness"),
                "incremental": constants.get(
                    "gameConstants.SkillDamageIncrementalEffectiveness"
                ),
            }
        except constants.MissingConstant:
            game_constants = None
            print("  [提示] 常量表缺 SkillDamage*Effectiveness，基础伤害三列将留空"
                  "（先跑 scripts/import_pob_constants.py）")
        gems, skipped = extract_gems(
            repo.root, _poedb_html(fetcher=fetcher), game_constants=game_constants
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
    except (PoBNotFound, GemStructureError, OSError) as exc:
        print(f"[中止] {exc}")
        return 2

    by_kind = Counter(gem.gem_kind for gem in gems)
    by_color = Counter(gem.attr_color for gem in gems)
    by_reason = Counter(item.reason for item in skipped)
    level_rows = sum(len(gem.levels) for gem in gems)
    no_tags = sum(1 for gem in gems if not gem.tags_zh)
    print(f"PoB：{repo.version_label} commit={repo.commit}")
    print(f"宝石：{len(gems)} 颗（{dict(sorted(by_kind.items()))}），解析 {elapsed_ms} ms")
    print(f"颜色分布：{dict(sorted(by_color.items()))}")
    print(f"等级行：{level_rows}；蜕变 {sum(g.is_transfigured for g in gems)}、"
          f"瓦尔 {sum(g.is_vaal for g in gems)}；另译 {sum(1 for g in gems if g.name_zh_alt)} 颗")
    print(f"缺中文标签：{no_tags} 颗")
    print(f"跳过 {len(skipped)} 条：{dict(sorted(by_reason.items()))}")
    if args.dry_run:
        print("（--dry-run，未写库）")
        return 0

    conn = dbmod.get_conn()
    priority = repo.priority(cfg)
    entities = [to_entity(gem, repo, priority) for gem in gems]
    try:
        validate_snapshot_size(conn, len(gems), allow_shrink=args.allow_shrink)
        run = conn.execute(
            "INSERT INTO ingest_run(source,category,started_at,total) "
            "VALUES (?,?,datetime('now'),?)",
            (Source.POB.value, "pob_skill_gem", len(gems)),
        )
        run_id = int(run.lastrowid)
        conn.execute("SAVEPOINT pob_skill_snapshot")
        try:
            written, levels_written = replace_snapshot(conn, gems, repo, priority)
            chunks, aliases = rebuild_index(conn, entities)
            gap_rows = record_skipped(conn, skipped)
            conn.execute("RELEASE pob_skill_snapshot")
        except Exception:
            conn.execute("ROLLBACK TO pob_skill_snapshot")
            conn.execute("RELEASE pob_skill_snapshot")
            raise
        note = (
            f"{written} 颗宝石 / {levels_written} 行等级；卡片 {chunks}、别名 {aliases}；"
            f"跳过 {len(skipped)} 条 {dict(sorted(by_reason.items()))}；台账 {gap_rows} 组"
        )
        conn.execute(
            "UPDATE ingest_run SET finished_at=datetime('now'),succeeded=?,failed=0,"
            "elapsed_ms=?,note=? WHERE run_id=?",
            (written, elapsed_ms, note, run_id),
        )
        conn.commit()
    except (GemStructureError, ValueError) as exc:
        conn.rollback()
        print(f"[中止] {exc}")
        return 2

    # 覆盖声明必须当场刷新：它注入 system prompt、供缺据声明归因，
    # 不刷新就会继续对外声称技能石「已抓取未入库」——那是一句现成的假话。
    domains = cov.refresh(conn, cache_dir=cfg.cache_dir)
    conn.commit()
    print(f"已写入 {written} 颗宝石、{levels_written} 行等级；卡片 {chunks}、别名 {aliases}"
          f"（run_id={run_id}）")
    print(f"跳过项按成因登记进缺口台账：{gap_rows} 组；覆盖声明已刷新 {domains} 个领域")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
