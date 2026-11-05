#!/usr/bin/env bash
# Start grid, two VPPs and two edges; waits until every /healthz answers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs/services data
PY="$ROOT/.venv/bin/python"
PIDFILE="$ROOT/logs/services/pids.txt"
: > "$PIDFILE"

start() {
  local name="$1"; shift
  nohup "$@" > "logs/services/${name}.log" 2>&1 &
  echo "$! $name" >> "$PIDFILE"
  echo "started $name pid $!"
}

start grid "$PY" -m src.grid.service
start vpp-a env VPP_ID=vpp-a "$PY" -m src.vpp.service
start vpp-b env VPP_ID=vpp-b "$PY" -m src.vpp.service
start edge-01 env EDGE_ID=edge-01 "$PY" -m src.edge.service
start edge-02 env EDGE_ID=edge-02 "$PY" -m src.edge.service
start edge-03 env EDGE_ID=edge-03 "$PY" -m src.edge.service
start edge-04 env EDGE_ID=edge-04 "$PY" -m src.edge.service
start console "$PY" -m src.console.service

for port in 8101 8102 8103 8104 8201 8202 8301 8400; do
  for _ in $(seq 1 40); do
    if curl -sf -m 2 "http://127.0.0.1:${port}/healthz" > /dev/null 2>&1; then
      echo "healthz ok: ${port}"
      break
    fi
    sleep 0.5
  done
done
