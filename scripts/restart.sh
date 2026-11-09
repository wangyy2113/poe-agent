#!/usr/bin/env bash
# Local macOS / Linux process management; systemd deployments use systemctl.
set -euo pipefail
cd "$(dirname "$0")/.."
repo_dir=$(pwd -P)
python_bin="$repo_dir/.venv/bin/python"

fail() { echo "$*" >&2; exit 1; }
[[ $# -eq 0 ]] || fail "用法：./restart.sh（不需要参数）"
[[ -x "$python_bin" ]] || fail "未找到 .venv，请先运行：python scripts/setup.py"
command -v lsof >/dev/null || fail "需要 lsof 来确认端口所属进程；请先安装 lsof。"
port=$("$python_bin" -c 'from core.config import get_config; print(int(get_config().get("server.port", 8787)))')
pids=$(lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t || true)

# Verify every listener before stopping any process. Never kill all Python tasks.
for pid in $pids; do
    process_dir=$(lsof -a -p "${pid}" -d cwd -Fn | sed -n 's/^n//p')
    process_command=$(ps -p "${pid}" -o command= | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    [[ "$process_dir" == "$repo_dir" ]] || fail "端口 ${port} 被其他目录的进程 ${pid} 占用，未停止任何进程。"
    case "$process_command" in
        poe-agent|"$python_bin main.py"|"$python_bin $repo_dir/main.py") ;;
        *) fail "端口 ${port} 的进程 ${pid} 不是已识别的 poe-agent 启动入口，请手动核对。" ;;
    esac
done

for pid in $pids; do
    echo "正在停止 poe-agent（PID ${pid}）……"
    kill -TERM "${pid}"
    for ((attempt=0; attempt<60; attempt++)); do
        kill -0 "${pid}" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "${pid}" 2>/dev/null; then
        fail "进程 ${pid} 在 30 秒内尚未退出；未强制终止，也未启动第二个实例。"
    fi
done

exec "$python_bin" - <<'PY'
import os
import subprocess
import sys
import time

import httpx

from api import normalize_base_path
from core.config import REPO_ROOT, get_config

cfg = get_config()
port = int(cfg.get("server.port", 8787))
host = str(cfg.get("server.host", "127.0.0.1"))
host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
url_host = f"[{host}]" if ":" in host else host
base = f"http://{url_host}:{port}" + normalize_base_path(cfg.get("server.base_path", "/poe_agent"))
cfg.logs_dir.mkdir(parents=True, exist_ok=True)
log_path = cfg.logs_dir / "console.log"
with log_path.open("ab") as log:
    process = subprocess.Popen(
        [sys.executable, str(REPO_ROOT / "main.py")], cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"},
    )

print(f"正在启动 poe-agent（PID {process.pid}）……", flush=True)
with httpx.Client(timeout=2, trust_env=False) as client:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"启动失败。请查看日志：{log_path}")
        # An existing service's health response must not count as our startup.
        listeners = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        ).stdout.split()
        if str(process.pid) in listeners:
            try:
                response = client.get(base + "/healthz")
                if response.status_code == 200 and response.json().get("status") == "ok":
                    print(f"服务已就绪：{base}/\n日志：{log_path}")
                    break
            except (httpx.HTTPError, ValueError):
                pass
        time.sleep(0.5)
    else:
        raise SystemExit(f"进程已启动，但健康检查未在 30 秒内通过。请查看日志：{log_path}")
PY
