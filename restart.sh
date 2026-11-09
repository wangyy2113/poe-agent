#!/usr/bin/env bash
# macOS / Linux: restart the local Web service from any working directory.
set -euo pipefail
exec bash "$(dirname "$0")/scripts/restart.sh" "$@"
