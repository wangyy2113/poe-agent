#!/usr/bin/env bash
# 薄封装：真正的初始化逻辑在 scripts/setup.py（跨平台唯一真源）。
# 新增初始化步骤请改 setup.py，不要改这里。
set -euo pipefail

cd "$(dirname "$0")/.."

for candidate in python3.12 python3.13 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        exec "$candidate" scripts/setup.py "$@"
    fi
done

echo "未找到 python3，请先安装 Python 3.10+" >&2
exit 1
