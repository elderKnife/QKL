#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODES="$ROOT/chain/nodes/127.0.0.1"
if [ -f "$NODES/stop_all.sh" ]; then
  bash "$NODES/stop_all.sh"
else
  echo "no stop_all.sh under $NODES" >&2
fi
