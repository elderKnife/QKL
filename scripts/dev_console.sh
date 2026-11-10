#!/usr/bin/env bash
# Dev-only: console backend with auto-reload. Domain services must already run.
set -euo pipefail
cd "$(dirname "$0")/.."
export CONSOLE_DEBUG="${CONSOLE_DEBUG:-1}"
export CONSOLE_PORT="${CONSOLE_PORT:-8400}"
exec .venv/bin/python -m src.console.service
