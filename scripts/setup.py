#!/usr/bin/env python3
"""环境初始化——**跨平台唯一真源**。

`.sh` / `.bat` 只是转调本脚本的薄封装。新增初始化逻辑写在这里，
不要只改某一个平台的封装脚本。

全程只用 sys.executable + venv 模块 + pathlib，不调用任何 shell 工具，
因此 Windows / macOS / Linux 行为一致。幂等，可重复执行。

    python scripts/setup.py
    python scripts/setup.py --recreate   # 删掉 .venv 重建
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".venv"
MIN_PYTHON = (3, 10)


def _venv_python(venv_dir: Path) -> Path:
    """venv 里的解释器路径。Windows 在 Scripts/，其余在 bin/。"""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def step(msg: str) -> None:
    print(f"\n>>> {msg}")


def check_python() -> bool:
    step(f"检查 Python 版本（需要 >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}）")
    print(f"    当前：{sys.version.split()[0]}  {platform.platform()}")
    if sys.version_info < MIN_PYTHON:
        print(f"    !! 版本过低，请安装 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")
        return False

    # sqlite-vec 需要扩展加载支持；部分发行版编译时关掉了（如 macOS 自带 3.9）
    import sqlite3

    conn = sqlite3.connect(":memory:")
    has_ext = hasattr(conn, "enable_load_extension")
    opts = [r[0] for r in conn.execute("PRAGMA compile_options")]
    has_fts5 = any("FTS5" in o for o in opts)
    conn.close()
    print(f"    sqlite {sqlite3.sqlite_version}  FTS5={has_fts5}  扩展加载={has_ext}")
    if not has_fts5:
        print("    !! 该 Python 的 sqlite3 不支持 FTS5，检索无法工作")
        return False
    if not has_ext:
        print("    ·  不支持扩展加载 → 向量通道将自动禁用（BM25 不受影响）")
        print("       如需向量检索，请改用 python.org 或 Homebrew 发行版")
    return True


def create_venv(recreate: bool) -> Path:
    step("准备虚拟环境 .venv")
    if recreate and VENV_DIR.exists():
        print("    删除已有 .venv")
        shutil.rmtree(VENV_DIR)
    py = _venv_python(VENV_DIR)
    if py.is_file():
        print(f"    已存在：{py}")
        return py
    print(f"    创建中：{VENV_DIR}")
    venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(VENV_DIR)
    return _venv_python(VENV_DIR)


def install_deps(py: Path) -> bool:
    step("安装依赖")
    lock = REPO_ROOT / "requirements.lock"
    req = REPO_ROOT / "requirements.txt"
    target = lock if lock.is_file() else req
    print(f"    使用 {target.name}")

    cmds = [
        [str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        [str(py), "-m", "pip", "install", "--quiet", "-r", str(target)],
    ]
    for cmd in cmds:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, check=False)  # noqa: S603
        if proc.returncode != 0:
            if target is lock:
                print("    lock 安装失败，回退 requirements.txt")
                proc = subprocess.run(  # noqa: S603
                    [str(py), "-m", "pip", "install", "--quiet", "-r", str(req)],
                    cwd=REPO_ROOT, check=False,
                )
                if proc.returncode == 0:
                    break
            print(f"    !! 安装失败：{' '.join(cmd[-3:])}")
            return False
    print("    完成")
    return True


def make_dirs() -> None:
    step("创建运行时目录")
    for name in ("data", "logs", "data/http_cache"):
        p = REPO_ROOT / name
        p.mkdir(parents=True, exist_ok=True)
        print(f"    {p}")


def copy_templates() -> None:
    step("准备配置文件")
    env_file = REPO_ROOT / ".env"
    example = REPO_ROOT / ".env.example"
    if env_file.is_file():
        print("    .env 已存在，跳过（不覆盖已有密钥）")
    elif example.is_file():
        env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print("    已从 .env.example 创建 .env —— 请填入 LLM_API_KEY 与 ADMIN_TOKEN")
    # Windows 无 POSIX 权限位，chmod 语义不同，尝试即可，失败不影响
    try:
        env_file.chmod(0o600)
    except (OSError, NotImplementedError):
        print("    ·  当前平台不支持 chmod，请自行确认 .env 的访问权限")


def main() -> int:
    ap = argparse.ArgumentParser(description="POE1-Agent 环境初始化")
    ap.add_argument("--recreate", action="store_true", help="删除并重建 .venv")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 68)
    print("POE1-Agent 环境初始化")
    print("=" * 68)
    print(f"  仓库根：{REPO_ROOT}")

    if not check_python():
        return 1
    py = create_venv(args.recreate)
    if not install_deps(py):
        return 1
    make_dirs()
    copy_templates()

    rel = py.relative_to(REPO_ROOT) if py.is_relative_to(REPO_ROOT) else py
    print("\n" + "=" * 68)
    print("完成。后续步骤：")
    print("=" * 68)
    print("  1) 编辑 .env，填入 LLM_API_KEY 与 ADMIN_TOKEN")
    print(f"  2) 自检          {rel} scripts/selfcheck.py")
    print(f"  3) 建知识库      {rel} -m kb.pipeline")
    print(f"  4) 检索验证      {rel} scripts/test_search.py")
    print(f"  5) 启动服务      {rel} main.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
