#!/usr/bin/env bash
# macOS Finder double-click entry.
bash "$(dirname "$0")/restart.sh"
result=$?
if [[ $result -ne 0 ]]; then
    read -r -p "重启未完成，请查看上方提示。按回车关闭窗口。" _
fi
exit "$result"
