#!/usr/bin/env python3
"""跨系统发布验收：Linux 生成 SQLite 交接件，Windows 启动真实进程验证中文检索。

这是无真实密钥/网络的迁移契约。在线 LLM 中文问答仍需单独使用真实凭证验收。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db as dbmod  # noqa: E402
from entities.models import Chunk, DocumentMetadata  # noqa: E402
from entities.taxonomy import Category, Source  # noqa: E402
from retrieval.fts import index_chunks  # noqa: E402
from retrieval.tokenize import tokens_for_fts  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MARKER = "中文迁移验证"
API = "http://127.0.0.1:8787/poe_agent"


def prepare(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "poe.db"
    if path.exists():
        raise ValueError("迁移源数据库已存在")
    conn = dbmod.init_db(path)
    try:
        conn.execute(
            "INSERT INTO entity(entity_id,entity_type,name_zh,name_en,attributes,source,"
            "source_priority,url,content_hash,fetched_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            ("migration:zh", "probe", MARKER, "Migration Chinese Probe", "{}",
             Source.POEDB.value, 1, "https://poedb.tw/cn/", "migration-probe",
             "2026-01-01T00:00:00Z"),
        )
        card = Chunk(
            chunk_id="migration:zh:0", entity_id="migration:zh",
            text=f"{MARKER}：跨系统 SQLite 中文索引可读取。",
            tokens=tokens_for_fts(MARKER), keywords=MARKER,
            metadata=DocumentMetadata(
                title=MARKER, category=Category.BASICS, source=Source.POEDB.value,
                source_priority=1, url="https://poedb.tw/cn/",
            ),
        )
        index_chunks(conn, [card])
        conn.commit()
    finally:
        dbmod.close_db()
    (folder / "migration.env").write_text(
        "LLM_BASE_URL=https://example.invalid\n"
        "LLM_API_KEY=sk-ci-placeholder\n"
        "API_KEYS=ci-user-placeholder\n"
        "ADMIN_TOKEN=ci-admin-placeholder\n",
        encoding="utf-8",
    )
    print(f"已生成跨系统迁移件：{folder}")


def _request(path: str, *, payload: dict[str, object] | None = None,
             token: str = "") -> tuple[int, dict[str, object]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API + path, data=body, headers=headers,
                                 method="POST" if payload else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def verify(folder: Path) -> None:
    source = folder / "poe.db"
    source_env = folder / "migration.env"
    if not source.is_file() or not source_env.is_file():
        raise ValueError("缺少 Linux 生成的数据库或环境模板")
    destination = ROOT / "data" / "poe.db"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("验证目标已有 data/poe.db；拒绝覆盖")
    shutil.copy2(source, destination)
    shutil.copy2(source_env, ROOT / ".env")
    env = os.environ.copy()
    env.update({
        "PYTHONUTF8": "1", "LLM_BASE_URL": "https://example.invalid",
        "LLM_API_KEY": "sk-ci-placeholder", "API_KEYS": "ci-user-placeholder",
        "ADMIN_TOKEN": "ci-admin-placeholder",
    })
    log_path = folder / "server.log"
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "main.py")], cwd=ROOT, env=env,
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f"服务提前退出，返回码 {proc.returncode}")
                try:
                    status, health = _request("/healthz")
                    if status == 200 and health.get("db", {}).get("entities", 0) >= 1:
                        break
                except (urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(0.5)
            else:
                raise RuntimeError("服务 35 秒内未就绪")

            status, result = _request("/search", payload={"query": MARKER})
            if status != 200 or not any(
                MARKER in str(hit.get("text", "")) for hit in result.get("hits", [])
            ):
                raise RuntimeError("跨系统 SQLite 中文 FTS 检索失败")
            if _request("/admin/stats")[0] not in (401, 403):
                raise RuntimeError("管理接口未拒绝无凭证请求")
            if _request("/admin/stats", token="ci-admin-placeholder")[0] != 200:
                raise RuntimeError("管理接口有效凭证未通过")
            print("迁移验证通过：Linux SQLite + .env → 当前系统真实进程 → 中文检索与鉴权")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description="跨系统 SQLite 与配置迁移冒烟")
    parser.add_argument("mode", choices=("prepare", "verify"))
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.folder)
    else:
        verify(args.folder)


if __name__ == "__main__":
    main()
