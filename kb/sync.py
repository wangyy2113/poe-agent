"""知识库自动同步计划与事实级版本 diff。

本模块不实现新的抓取器，而是编排 ``kb.pipeline`` 中已经带结构断言、数量护栏与
原子提交的分类采集器。默认只选择超过新鲜度阈值的分类；同步前后按稳定主键与非时间
字段计算新增、删除、修改，并把版本字段变化单独落库。

唯一装备主列表暂不进入自动集合：列表页会覆盖详情页补齐的「不可改变」等词条，除非
紧接着刷新 1,342 张详情页。没有把这条复合链做成原子操作前，自动化它会制造数据回退。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable


VOLATILE_COLUMNS = frozenset({
    "created_at",
    "fetched_at",
    "first_seen",
    "last_seen",
    "probed_at",
    "updated_at",
})
VERSION_COLUMNS = frozenset({
    "game_version",
    "league",
    "pob_version",
    "source_commit",
    "tree_version",
    "version",
    "version_scope",
})
DEFAULT_STALE_AFTER_HOURS = 24 * 7
MAX_DIFF_EXAMPLES = 20


@dataclass(frozen=True)
class TableSpec:
    table: str
    where: str = ""
    params: tuple[object, ...] = ()


@dataclass(frozen=True)
class CategorySpec:
    name: str
    ingest_category: str
    tables: tuple[TableSpec, ...]


@dataclass(frozen=True)
class SyncPlanItem:
    category: str
    due: bool
    reason: str
    last_success: str | None
    age_hours: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "due": self.due,
            "reason": self.reason,
            "last_success": self.last_success,
            "age_hours": self.age_hours,
        }


@dataclass(frozen=True)
class CategorySnapshot:
    records: dict[str, str]
    table_counts: dict[str, int]
    versions: dict[str, list[str]]
    fingerprint: str

    @property
    def total(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class CategoryDiff:
    category: str
    status: str
    before_fingerprint: str
    after_fingerprint: str
    before_total: int
    after_total: int
    added: int
    removed: int
    changed: int
    unchanged: int
    table_counts_before: dict[str, int]
    table_counts_after: dict[str, int]
    versions_before: dict[str, list[str]]
    versions_after: dict[str, list[str]]
    examples: dict[str, list[str]]
    elapsed_ms: int
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "status": self.status,
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "before_total": self.before_total,
            "after_total": self.after_total,
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "table_counts_before": self.table_counts_before,
            "table_counts_after": self.table_counts_after,
            "versions_before": self.versions_before,
            "versions_after": self.versions_after,
            "examples": self.examples,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error or None,
        }


CATEGORY_SPECS: tuple[CategorySpec, ...] = (
    CategorySpec("mods", "mod_affix", (TableSpec("mod_affix"),)),
    CategorySpec(
        "bench",
        "crafting_bench",
        (TableSpec("craft_bench_recipe"), TableSpec("craft_bench_recipe_cost")),
    ),
    CategorySpec(
        "metamods",
        "metamod_matrix",
        (TableSpec("craft_metamod_interaction"),),
    ),
    CategorySpec(
        "tree",
        "passive_tree",
        (
            TableSpec(
                "entity",
                "entity_type IN ('passive','ascendancy','atlas_passive')",
            ),
            TableSpec("passive_graph_node"),
            TableSpec("passive_graph_edge"),
            TableSpec("passive_tree_meta"),
            TableSpec("atlas_graph_node"),
            TableSpec("atlas_graph_edge"),
            TableSpec("atlas_tree_meta"),
        ),
    ),
    CategorySpec("currency", "currency", (TableSpec("poedb_currency"),)),
    CategorySpec("maps", "maps", (TableSpec("poedb_map"),)),
    CategorySpec("scarab", "scarab", (TableSpec("poedb_scarab"),)),
    CategorySpec(
        "essence",
        "essence",
        (TableSpec("poedb_essence"), TableSpec("poedb_essence_mod")),
    ),
    CategorySpec(
        "horticraft",
        "harvest",
        (
            TableSpec("harvest_craft"),
            TableSpec("harvest_craft_cost"),
            TableSpec("poedb_harvest_seed"),
            TableSpec("poedb_tag_weight"),
        ),
    ),
    CategorySpec(
        "monsters",
        "monsters",
        (TableSpec("poedb_monster_mod"), TableSpec("poedb_spectre")),
    ),
    CategorySpec("patch_notes", "ggg_patch_notes", (TableSpec("ggg_patch_note"),)),
    CategorySpec("boss_guides", "boss_guides", (TableSpec("boss_guide"),)),
)
SPEC_BY_NAME = {spec.name: spec for spec in CATEGORY_SPECS}


class SyncAlreadyRunning(RuntimeError):
    """同一 SQLite 中已有未结束的同步。"""


def _parse_sqlite_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_categories(categories: Iterable[str]) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(str(item).strip() for item in categories if str(item).strip()))
    unknown = [name for name in requested if name not in SPEC_BY_NAME]
    if unknown:
        allowed = ", ".join(spec.name for spec in CATEGORY_SPECS)
        raise ValueError(f"不支持自动同步的分类：{', '.join(unknown)}；可用：{allowed}")
    return requested


def build_plan(
    conn: sqlite3.Connection,
    *,
    categories: Iterable[str] | None = None,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
    now: datetime | None = None,
) -> list[SyncPlanItem]:
    """按最后一次有产出的采集时间生成同步计划，不把失败尝试冒充成功。"""
    if stale_after_hours <= 0:
        raise ValueError("stale_after_hours 必须大于 0")
    selected = validate_categories(SPEC_BY_NAME if categories is None else categories)
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    plan: list[SyncPlanItem] = []
    for name in selected:
        spec = SPEC_BY_NAME[name]
        row = conn.execute(
            "SELECT finished_at FROM ingest_run WHERE category=? "
            "AND finished_at IS NOT NULL AND succeeded > 0 "
            "ORDER BY run_id DESC LIMIT 1",
            (spec.ingest_category,),
        ).fetchone()
        if row is None:
            plan.append(SyncPlanItem(name, True, "never_succeeded", None, None))
            continue
        last = str(row[0])
        age = max(0.0, (clock - _parse_sqlite_time(last)).total_seconds() / 3600)
        due = age >= stale_after_hours
        plan.append(SyncPlanItem(
            name,
            due,
            "stale" if due else "fresh",
            last,
            round(age, 3),
        ))
    return plan


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _json_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    return value


def _row_key(table: str, row: sqlite3.Row, pk_columns: tuple[str, ...]) -> str:
    if pk_columns:
        values = [_json_value(row[column]) for column in pk_columns]
        return f"{table}:" + json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    payload = {
        key: _json_value(row[key])
        for key in row.keys()
        if key not in VOLATILE_COLUMNS
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{table}:sha256:{digest}"


def snapshot_category(conn: sqlite3.Connection, category: str) -> CategorySnapshot:
    spec = SPEC_BY_NAME.get(category)
    if spec is None:
        raise ValueError(f"未知同步分类：{category}")
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        records: dict[str, str] = {}
        table_counts: dict[str, int] = {}
        versions: dict[str, set[str]] = {}
        for table_spec in spec.tables:
            table = table_spec.table
            if not _table_exists(conn, table):
                table_counts[table] = 0
                continue
            info = list(conn.execute(f'PRAGMA table_info("{table}")'))
            pk_columns = tuple(
                str(row["name"])
                for row in sorted(info, key=lambda item: int(item["pk"]))
                if int(row["pk"]) > 0
            )
            sql = f'SELECT * FROM "{table}"'
            if table_spec.where:
                sql += f" WHERE {table_spec.where}"
            rows = list(conn.execute(sql, table_spec.params))
            table_counts[table] = len(rows)
            for row in rows:
                key = _row_key(table, row, pk_columns)
                substantive = {
                    column: _json_value(row[column])
                    for column in row.keys()
                    if column not in VOLATILE_COLUMNS
                }
                records[key] = hashlib.sha256(
                    json.dumps(
                        substantive,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                for column in VERSION_COLUMNS.intersection(row.keys()):
                    value = row[column]
                    if value not in (None, ""):
                        versions.setdefault(f"{table}.{column}", set()).add(str(value))
        fingerprint = hashlib.sha256(
            "\n".join(f"{key}\0{records[key]}" for key in sorted(records)).encode("utf-8")
        ).hexdigest()
        return CategorySnapshot(
            records=records,
            table_counts=table_counts,
            versions={key: sorted(values) for key, values in sorted(versions.items())},
            fingerprint=fingerprint,
        )
    finally:
        conn.row_factory = previous_factory


def diff_snapshots(
    category: str,
    before: CategorySnapshot,
    after: CategorySnapshot,
    *,
    elapsed_ms: int = 0,
    error: str = "",
) -> CategoryDiff:
    before_keys = set(before.records)
    after_keys = set(after.records)
    added_keys = sorted(after_keys - before_keys)
    removed_keys = sorted(before_keys - after_keys)
    changed_keys = sorted(
        key for key in before_keys & after_keys if before.records[key] != after.records[key]
    )
    unchanged = len(before_keys & after_keys) - len(changed_keys)
    if error:
        status = "failed"
    elif removed_keys:
        status = "attention"
    elif added_keys or changed_keys or before.versions != after.versions:
        status = "changed"
    else:
        status = "unchanged"
    return CategoryDiff(
        category=category,
        status=status,
        before_fingerprint=before.fingerprint,
        after_fingerprint=after.fingerprint,
        before_total=before.total,
        after_total=after.total,
        added=len(added_keys),
        removed=len(removed_keys),
        changed=len(changed_keys),
        unchanged=unchanged,
        table_counts_before=before.table_counts,
        table_counts_after=after.table_counts,
        versions_before=before.versions,
        versions_after=after.versions,
        examples={
            "added": added_keys[:MAX_DIFF_EXAMPLES],
            "removed": removed_keys[:MAX_DIFF_EXAMPLES],
            "changed": changed_keys[:MAX_DIFF_EXAMPLES],
        },
        elapsed_ms=elapsed_ms,
        error=error,
    )


def default_runners() -> dict[str, Callable[[bool], int]]:
    """延迟导入采集管线，避免仅查看计划时加载全部抽取器。"""
    from kb import pipeline  # noqa: PLC0415
    from kb import patch_notes  # noqa: PLC0415
    from kb import boss_guides  # noqa: PLC0415
    from core import db as dbmod  # noqa: PLC0415

    return {
        "mods": lambda force: pipeline.run_poedb_mods(force=force),
        "bench": lambda force: pipeline.run_poedb_crafting(force=force),
        "metamods": lambda force: pipeline.run_poedb_metamods(force=force),
        "tree": lambda force: pipeline.run_passive_tree(force=force),
        "currency": lambda force: pipeline.run_poedb_currency(force=force),
        "maps": lambda force: pipeline.run_poedb_maps(force=force),
        "scarab": lambda force: pipeline.run_poedb_scarab(force=force),
        "essence": lambda force: pipeline.run_poedb_essence(force=force),
        "horticraft": lambda force: pipeline.run_poedb_harvest(force=force),
        "monsters": lambda force: pipeline.run_poedb_monsters(force=force),
        "patch_notes": lambda force: patch_notes.sync_latest(dbmod.get_conn(), force=force),
        "boss_guides": lambda force: boss_guides.sync_latest(dbmod.get_conn(), force=force),
    }


def _persist_diff(conn: sqlite3.Connection, sync_id: str, diff: CategoryDiff) -> None:
    conn.execute(
        "INSERT INTO knowledge_sync_diff("
        "sync_id,category,status,before_fingerprint,after_fingerprint,before_total,"
        "after_total,added,removed,changed,unchanged,table_counts_before,table_counts_after,"
        "versions_before,versions_after,examples,elapsed_ms,error) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            sync_id,
            diff.category,
            diff.status,
            diff.before_fingerprint,
            diff.after_fingerprint,
            diff.before_total,
            diff.after_total,
            diff.added,
            diff.removed,
            diff.changed,
            diff.unchanged,
            json.dumps(diff.table_counts_before, ensure_ascii=False, sort_keys=True),
            json.dumps(diff.table_counts_after, ensure_ascii=False, sort_keys=True),
            json.dumps(diff.versions_before, ensure_ascii=False, sort_keys=True),
            json.dumps(diff.versions_after, ensure_ascii=False, sort_keys=True),
            json.dumps(diff.examples, ensure_ascii=False, sort_keys=True),
            diff.elapsed_ms,
            diff.error or None,
        ),
    )


def run_sync(
    conn: sqlite3.Connection,
    categories: Iterable[str],
    *,
    runners: dict[str, Callable[[bool], int]] | None = None,
    force: bool = False,
    mode: str = "explicit",
    refresh_coverage: Callable[[], int] | None = None,
) -> dict[str, object]:
    """串行执行同步；首个失败即停止，已完成分类的 diff 仍完整保留。"""
    selected = validate_categories(categories)
    if not selected:
        raise ValueError("同步分类不能为空")
    if mode not in {"auto", "explicit"}:
        raise ValueError("mode 只能是 auto 或 explicit")
    active_rows = list(conn.execute(
        "SELECT sync_id,started_at FROM knowledge_sync_run WHERE status='running' "
        "ORDER BY started_at DESC"
    ))
    if active_rows:
        clock = datetime.now(timezone.utc)
        recent = [
            row for row in active_rows
            if (clock - _parse_sqlite_time(str(row[1]))).total_seconds() < 12 * 3600
        ]
        if recent:
            raise SyncAlreadyRunning(f"已有同步正在运行：{recent[0][0]}")
        conn.executemany(
            "UPDATE knowledge_sync_run SET status='failed',finished_at=datetime('now'),"
            "error='进程中断后超过 12 小时未结束，已由下一次同步标记失败' WHERE sync_id=?",
            [(row[0],) for row in active_rows],
        )
        conn.commit()

    actual_runners = runners or default_runners()
    missing = [name for name in selected if name not in actual_runners]
    if missing:
        raise ValueError(f"缺少同步执行器：{', '.join(missing)}")

    sync_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO knowledge_sync_run(sync_id,mode,requested,planned,status,started_at) "
        "VALUES (?,?,?,?, 'running', datetime('now'))",
        (
            sync_id,
            mode,
            json.dumps(list(selected), ensure_ascii=False),
            json.dumps(list(selected), ensure_ascii=False),
        ),
    )
    conn.commit()
    diffs: list[CategoryDiff] = []
    failed_error = ""

    for category in selected:
        before = snapshot_category(conn, category)
        started = time.monotonic()
        error = ""
        try:
            code = actual_runners[category](force)
            if code != 0:
                error = f"采集器退出码 {code}"
        except Exception as exc:  # noqa: BLE001
            # 采集器若在事务中途抛错，不能让下面写 diff 的 commit 顺带提交半成品。
            conn.rollback()
            error = f"{type(exc).__name__}: {exc}"
        after = snapshot_category(conn, category)
        diff = diff_snapshots(
            category,
            before,
            after,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error=error,
        )
        _persist_diff(conn, sync_id, diff)
        conn.commit()
        diffs.append(diff)
        if error:
            failed_error = f"{category}: {error}"
            break

    if not failed_error and refresh_coverage is not None:
        try:
            coverage_code = refresh_coverage()
            if coverage_code != 0:
                failed_error = f"coverage: 刷新退出码 {coverage_code}"
        except Exception as exc:  # noqa: BLE001
            failed_error = f"coverage: {type(exc).__name__}: {exc}"

    summary = {
        "planned": len(selected),
        "completed": len(diffs),
        "unchanged": sum(diff.status == "unchanged" for diff in diffs),
        "changed": sum(diff.status == "changed" for diff in diffs),
        "attention": sum(diff.status == "attention" for diff in diffs),
        "failed": sum(diff.status == "failed" for diff in diffs),
        "added": sum(diff.added for diff in diffs),
        "removed": sum(diff.removed for diff in diffs),
        "modified": sum(diff.changed for diff in diffs),
    }
    final_status = (
        "failed"
        if failed_error
        else "attention"
        if any(diff.status == "attention" for diff in diffs)
        else "succeeded"
    )
    conn.execute(
        "UPDATE knowledge_sync_run SET status=?,finished_at=datetime('now'),summary=?,error=? "
        "WHERE sync_id=?",
        (
            final_status,
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
            failed_error or None,
            sync_id,
        ),
    )
    conn.commit()
    return {
        "sync_id": sync_id,
        "status": final_status,
        "summary": summary,
        "error": failed_error or None,
        "diffs": [diff.as_dict() for diff in diffs],
    }


def load_sync_run(conn: sqlite3.Connection, sync_id: str) -> dict[str, object] | None:
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM knowledge_sync_run WHERE sync_id=?", (sync_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in ("requested", "planned", "summary"):
            if result.get(field):
                result[field] = json.loads(str(result[field]))
        diffs = []
        for diff_row in conn.execute(
            "SELECT * FROM knowledge_sync_diff WHERE sync_id=? ORDER BY rowid", (sync_id,)
        ):
            item = dict(diff_row)
            for field in (
                "table_counts_before",
                "table_counts_after",
                "versions_before",
                "versions_after",
                "examples",
            ):
                item[field] = json.loads(str(item[field]))
            diffs.append(item)
        result["diffs"] = diffs
        return result
    finally:
        conn.row_factory = previous_factory
