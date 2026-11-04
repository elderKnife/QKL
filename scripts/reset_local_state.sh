#!/usr/bin/env bash
# Archive local service state. Does NOT touch the chain or its evidence.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$ROOT/data/archive-$TS"
mkdir -p "$ARCHIVE"
for dir in data/edge-01 data/edge-02 data/vpp-a data/vpp-b data/grid data/reports; do
  if [ -d "$ROOT/$dir" ]; then
    mkdir -p "$ARCHIVE/$(dirname "$dir")"
    mv "$ROOT/$dir" "$ARCHIVE/$dir"
  fi
done
mkdir -p "$ROOT/data/reports"
echo "archived local state to $ARCHIVE (chain data untouched)"
