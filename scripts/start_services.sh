#!/usr/bin/env bash
# Start the topology in config/system.json and require every service to be healthy.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m scripts.start_services
