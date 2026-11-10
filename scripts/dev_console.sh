#!/usr/bin/env bash
# Dev-only: console backend with auto-reload. Domain services must already run.
set -euo pipefail
cd "$(dirname "$0")/.."
pkill -f "src.console.service" 2>/dev/null || true
sleep 1
export CONSOLE_DEBUG="${CONSOLE_DEBUG:-1}"
export CONSOLE_PORT="${CONSOLE_PORT:-8400}"
exec .venv/bin/python -m src.console.service
