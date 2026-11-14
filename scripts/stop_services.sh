#!/usr/bin/env bash
# Stop the configured business services; the chain keeps running.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$ROOT/logs/services/pids.txt"
if [ -f "$PIDFILE" ]; then
  while read -r pid name; do
    [ -n "${pid:-}" ] || continue
    if kill "$pid" 2>/dev/null; then echo "stopped $name ($pid)"; fi
  done < "$PIDFILE"
  rm -f "$PIDFILE"
fi
pkill -f "src\.(grid|vpp|edge|web|console)\.service" 2>/dev/null || true
sleep 1
