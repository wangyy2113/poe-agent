"""SQLite 一致性备份、校验与离线恢复。

运行中的数据库采用 WAL，直接复制 ``poe.db`` 可能遗漏尚未 checkpoint 的事务。本模块
创建快照时始终使用 SQLite Backup API；恢复则要求应用已经停止、WAL/SHM sidecar 不存在，
先为当前库创建安全快照，再用同目录临时文件 + ``os.replace`` 原子替换。

安全边界：所有可由 API/CLI 指定的备份名都必须位于配置的备份目录，不能包含目录分隔符，
也不能通过符号链接逃逸。恢复目标只来自配置，不接受任意路径参数。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from core.version import APP_VERSION

BACKUP_FORMAT_VERSION = 1
MANIFEST_SUFFIX = ".manifest.json"
BACKUP_NAME_RE = re.compile(r"^poe-\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{8}\.db$")
REQUIRED_TABLES = frozenset({"entity", "chunk", "alias", "conversation", "message", "trace"})
COUNTED_TABLES = (
    "entity",
    "chunk",
    "alias",
    "mod_affix",
    "pob_mod",
    "source_conflict",
    "conversation",
    "message",
    "trace",
)


class BackupError(RuntimeError):
    """备份操作因输入、完整性或运行状态不满足而失败。"""


@dataclass(frozen=True)
class BackupManifest:
    format_version: int
    backup_name: str
    created_at: str
    app_version: str
    sqlite_version: str
    database_sha256: str
    database_size: int
    schema_sha256: str
    user_version: int
    page_size: int
    page_count: int
    table_counts: dict[str, int]
    source_name: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> BackupManifest:
        expected = {field.name for field in cls.__dataclass_fields__.values()}
        missing = expected - value.keys()
        unknown = value.keys() - expected
        if missing or unknown:
            details = []
            if missing:
                details.append(f"缺字段 {sorted(missing)}")
            if unknown:
                details.append(f"未知字段 {sorted(unknown)}")
            raise BackupError("备份清单格式无效：" + "；".join(details))
        try:
            manifest = cls(
                format_version=int(value["format_version"]),
                backup_name=str(value["backup_name"]),
                created_at=str(value["created_at"]),
                app_version=str(value["app_version"]),
                sqlite_version=str(value["sqlite_version"]),
                database_sha256=str(value["database_sha256"]),
                database_size=int(value["database_size"]),
                schema_sha256=str(value["schema_sha256"]),
                user_version=int(value["user_version"]),
                page_size=int(value["page_size"]),
                page_count=int(value["page_count"]),
                table_counts={str(k): int(v) for k, v in dict(value["table_counts"]).items()},
                source_name=str(value["source_name"]),
            )
        except (TypeError, ValueError) as exc:
            raise BackupError("备份清单字段类型无效") from exc
        if manifest.format_version != BACKUP_FORMAT_VERSION:
            raise BackupError(
                f"不支持的备份清单版本 {manifest.format_version}，当前只支持 {BACKUP_FORMAT_VERSION}"
            )
        if not BACKUP_NAME_RE.fullmatch(manifest.backup_name):
            raise BackupError("备份清单中的文件名无效")
        if not re.fullmatch(r"[0-9a-f]{64}", manifest.database_sha256):
            raise BackupError("备份清单中的 SHA-256 无效")
        if not re.fullmatch(r"[0-9a-f]{64}", manifest.schema_sha256):
            raise BackupError("备份清单中的 schema SHA-256 无效")
        if min(manifest.database_size, manifest.page_size, manifest.page_count) < 0:
            raise BackupError("备份清单中的大小字段无效")
        return manifest


@dataclass(frozen=True)
class VerificationReport:
    backup_name: str
    valid: bool
    issues: tuple[str, ...]
    quick_check: str
    database_sha256: str
    database_size: int
    schema_sha256: str
    table_counts: dict[str, int]
    manifest: BackupManifest | None

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["issues"] = list(self.issues)
        return result


@dataclass(frozen=True)
class RestorePlan:
    backup_name: str
    target_name: str
    valid: bool
    issues: tuple[str, ...]
    target_exists: bool
    target_size: int
    safety_backup_required: bool
    sidecars: tuple[str, ...]
    expected_sha256: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["issues"] = list(self.issues)
        result["sidecars"] = list(self.sidecars)
        return result


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _backup_name(now: datetime, token: str) -> str:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"poe-{stamp}-{token[:8].lower()}.db"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema_sha256(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type,name,tbl_name,COALESCE(sql,'') FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name"
    ).fetchall()
    canonical = "\n".join("\x1f".join(str(value) for value in row) for row in rows)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _table_counts(conn: sqlite3.Connection, names: Iterable[str] = COUNTED_TABLES) -> dict[str, int]:
    existing = _table_names(conn)
    return {
        name: int(conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0])  # noqa: S608
        for name in names
        if name in existing
    }


def _quick_check(conn: sqlite3.Connection) -> str:
    rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
    return "ok" if rows == ["ok"] else "; ".join(rows)


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """用 URI mode=ro 打开，``as_uri`` 同时正确转义空格、问号和 Windows 盘符。"""
    return sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)


def _manifest_path(database_path: Path) -> Path:
    return database_path.with_name(database_path.name + MANIFEST_SUFFIX)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _resolved_root(backup_dir: Path, *, create: bool) -> Path:
    if create:
        backup_dir.mkdir(parents=True, exist_ok=True)
    if not backup_dir.is_dir():
        raise BackupError(f"备份目录不存在：{backup_dir}")
    return backup_dir.resolve(strict=True)


def resolve_backup(backup_dir: Path, backup_name: str) -> Path:
    """把不可信文件名限制在备份目录的直属层级。"""
    if not BACKUP_NAME_RE.fullmatch(backup_name) or Path(backup_name).name != backup_name:
        raise BackupError("备份名无效；必须使用 list 返回的完整 .db 文件名")
    root = _resolved_root(backup_dir, create=False)
    candidate = root / backup_name
    # 已存在符号链接时 resolve 会暴露逃逸；不存在时 parent 已是规范 root。
    resolved = candidate.resolve(strict=False)
    if resolved.parent != root:
        raise BackupError("备份路径越出配置目录")
    if candidate.is_symlink():
        raise BackupError("备份文件不能是符号链接")
    return candidate


def read_manifest(database_path: Path) -> BackupManifest:
    path = _manifest_path(database_path)
    if not path.is_file() or path.is_symlink():
        raise BackupError("备份清单不存在或不是普通文件")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("备份清单无法读取") from exc
    if not isinstance(raw, dict):
        raise BackupError("备份清单顶层必须是对象")
    manifest = BackupManifest.from_dict(raw)
    if manifest.backup_name != database_path.name:
        raise BackupError("备份清单与数据库文件名不匹配")
    return manifest


def _snapshot(
    source: sqlite3.Connection,
    destination: Path,
    *,
    progress: Callable[[int, int, int], None] | None = None,
) -> None:
    target = sqlite3.connect(str(destination))
    try:
        source.backup(target, pages=256, progress=progress, sleep=0.05)
        target.commit()
        if _quick_check(target) != "ok":
            raise BackupError("SQLite Backup API 生成的快照未通过 quick_check")
    finally:
        target.close()


def create_backup(
    db_path: Path,
    backup_dir: Path,
    *,
    connection: sqlite3.Connection | None = None,
    now: datetime | None = None,
    token: str | None = None,
    source_name: str | None = None,
) -> BackupManifest:
    """创建一致性快照及清单；支持传入应用正在使用的连接。"""
    if not db_path.is_file():
        raise BackupError(f"数据库不存在：{db_path}")
    root = _resolved_root(backup_dir, create=True)
    name = _backup_name(now or _utc_now(), token or uuid.uuid4().hex)
    destination = resolve_backup(root, name)
    if destination.exists() or _manifest_path(destination).exists():
        raise BackupError("备份名发生碰撞，请重新创建")
    temp = root / f".{name}.{uuid.uuid4().hex}.tmp"
    own_connection = connection is None
    source = connection or sqlite3.connect(str(db_path))
    complete = False
    try:
        _snapshot(source, temp)
        check = _connect_readonly(temp)
        try:
            quick = _quick_check(check)
            if quick != "ok":
                raise BackupError(f"备份 quick_check 失败：{quick}")
            tables = _table_names(check)
            missing = REQUIRED_TABLES - tables
            if missing:
                raise BackupError(f"备份缺少必要表：{sorted(missing)}")
            schema_hash = _schema_sha256(check)
            counts = _table_counts(check)
            user_version = int(check.execute("PRAGMA user_version").fetchone()[0])
            page_size = int(check.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(check.execute("PRAGMA page_count").fetchone()[0])
        finally:
            check.close()
        os.replace(temp, destination)
        manifest = BackupManifest(
            format_version=BACKUP_FORMAT_VERSION,
            backup_name=name,
            created_at=(now or _utc_now()).astimezone(timezone.utc).isoformat(),
            app_version=APP_VERSION,
            sqlite_version=sqlite3.sqlite_version,
            database_sha256=_sha256(destination),
            database_size=destination.stat().st_size,
            schema_sha256=schema_hash,
            user_version=user_version,
            page_size=page_size,
            page_count=page_count,
            table_counts=counts,
            source_name=source_name or db_path.name,
        )
        _write_json_atomic(_manifest_path(destination), asdict(manifest))
        complete = True
        return manifest
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"创建备份失败：{type(exc).__name__}: {exc}") from exc
    finally:
        if own_connection:
            source.close()
        temp.unlink(missing_ok=True)
        if not complete:
            destination.unlink(missing_ok=True)
            _manifest_path(destination).unlink(missing_ok=True)


def list_backups(backup_dir: Path) -> list[dict[str, Any]]:
    """列出完整备份对；不读取数据库正文，因此适合管理页快速轮询。"""
    try:
        root = _resolved_root(backup_dir, create=False)
    except BackupError:
        return []
    items: list[dict[str, Any]] = []
    for manifest_path in root.glob(f"poe-*.db{MANIFEST_SUFFIX}"):
        database = manifest_path.with_name(manifest_path.name.removesuffix(MANIFEST_SUFFIX))
        try:
            manifest = read_manifest(database)
            items.append({
                "backup_name": manifest.backup_name,
                "created_at": manifest.created_at,
                "app_version": manifest.app_version,
                "database_size": manifest.database_size,
                "database_sha256": manifest.database_sha256,
                "table_counts": manifest.table_counts,
                "complete": database.is_file() and not database.is_symlink(),
            })
        except BackupError as exc:
            items.append({
                "backup_name": database.name,
                "complete": False,
                "error": str(exc),
            })
    return sorted(items, key=lambda item: str(item.get("created_at", "")), reverse=True)


def verify_backup(backup_dir: Path, backup_name: str) -> VerificationReport:
    database = resolve_backup(backup_dir, backup_name)
    issues: list[str] = []
    manifest: BackupManifest | None = None
    digest = ""
    size = 0
    schema_hash = ""
    counts: dict[str, int] = {}
    quick = "not_run"
    try:
        manifest = read_manifest(database)
    except BackupError as exc:
        issues.append(str(exc))
    if not database.is_file():
        issues.append("备份数据库文件不存在")
    elif database.is_symlink():
        issues.append("备份数据库不能是符号链接")
    else:
        try:
            size = database.stat().st_size
            digest = _sha256(database)
            conn = _connect_readonly(database)
            try:
                quick = _quick_check(conn)
                if quick != "ok":
                    issues.append(f"SQLite quick_check 失败：{quick}")
                schema_hash = _schema_sha256(conn)
                tables = _table_names(conn)
                missing = REQUIRED_TABLES - tables
                if missing:
                    issues.append(f"缺少必要表：{sorted(missing)}")
                counts = _table_counts(conn)
            finally:
                conn.close()
        except (OSError, sqlite3.Error) as exc:
            issues.append(f"数据库无法读取：{type(exc).__name__}: {exc}")
    if manifest is not None:
        if size != manifest.database_size:
            issues.append(f"文件大小不匹配：清单 {manifest.database_size}，实际 {size}")
        if digest != manifest.database_sha256:
            issues.append("数据库 SHA-256 与清单不匹配")
        if schema_hash and schema_hash != manifest.schema_sha256:
            issues.append("schema SHA-256 与清单不匹配")
        if counts and counts != manifest.table_counts:
            issues.append("关键表行数与清单不匹配")
    return VerificationReport(
        backup_name=backup_name,
        valid=not issues,
        issues=tuple(issues),
        quick_check=quick,
        database_sha256=digest,
        database_size=size,
        schema_sha256=schema_hash,
        table_counts=counts,
        manifest=manifest,
    )


def _sidecars(db_path: Path) -> tuple[Path, ...]:
    return tuple(
        path
        for suffix in ("-wal", "-shm", "-journal")
        if (path := db_path.with_name(db_path.name + suffix)).exists()
    )


def plan_restore(db_path: Path, backup_dir: Path, backup_name: str) -> RestorePlan:
    report = verify_backup(backup_dir, backup_name)
    sidecars = _sidecars(db_path)
    issues = list(report.issues)
    if sidecars:
        issues.append("目标数据库存在 WAL/SHM/journal；请先停止应用并完成优雅关停")
    if db_path.is_symlink():
        issues.append("目标数据库不能是符号链接")
    return RestorePlan(
        backup_name=backup_name,
        target_name=db_path.name,
        valid=not issues,
        issues=tuple(issues),
        target_exists=db_path.is_file(),
        target_size=db_path.stat().st_size if db_path.is_file() else 0,
        safety_backup_required=db_path.is_file(),
        sidecars=tuple(path.name for path in sidecars),
        expected_sha256=report.database_sha256,
    )


def restore_backup(db_path: Path, backup_dir: Path, backup_name: str) -> dict[str, Any]:
    """离线恢复。调用方必须先展示 plan 并要求显式 ``--apply``。"""
    plan = plan_restore(db_path, backup_dir, backup_name)
    if not plan.valid:
        raise BackupError("恢复预检失败：" + "；".join(plan.issues))
    source = resolve_backup(backup_dir, backup_name)
    safety: BackupManifest | None = None
    if db_path.is_file():
        safety = create_backup(db_path, backup_dir, source_name=f"pre-restore:{db_path.name}")
    # 安全快照期间若另一个进程启动并重新建立 sidecar，不能继续替换它正在使用的主文件。
    appeared = _sidecars(db_path)
    if appeared:
        raise BackupError(
            "恢复中止：创建安全快照后出现 SQLite sidecar，应用可能仍在运行："
            + ", ".join(path.name for path in appeared)
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp = db_path.with_name(f".{db_path.name}.{uuid.uuid4().hex}.restore.tmp")
    try:
        shutil.copyfile(source, temp)
        copied_hash = _sha256(temp)
        if copied_hash != plan.expected_sha256:
            raise BackupError("恢复临时文件与预检哈希不一致，源快照可能在操作期间被修改")
        os.replace(temp, db_path)
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(f"替换目标数据库失败：{type(exc).__name__}: {exc}") from exc
    finally:
        temp.unlink(missing_ok=True)
    restored = _connect_readonly(db_path)
    try:
        quick = _quick_check(restored)
    finally:
        restored.close()
    if quick != "ok":
        raise BackupError(f"恢复后的数据库 quick_check 失败：{quick}")
    return {
        "restored": True,
        "backup_name": backup_name,
        "target_name": db_path.name,
        "database_sha256": _sha256(db_path),
        "database_size": db_path.stat().st_size,
        "safety_backup": safety.backup_name if safety else None,
        "quick_check": quick,
    }
