#!/usr/bin/env bash
# Start the 5-node FISCO BCOS chain built under chain/nodes/127.0.0.1.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODES="$ROOT/chain/nodes/127.0.0.1"
if [ ! -d "$NODES" ]; then
  echo "chain not built; run scripts/build_chain.sh first" >&2
  exit 1
fi
bash "$NODES/start_all.sh"
sleep 3
for i in 0 1 2 3 4; do
  port=$((8545 + i))
  printf "node%d rpc %s: " "$i" "$port"
  curl -s -m 3 -X POST "http://127.0.0.1:$port" -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"getBlockNumber","params":[1],"id":1}' || echo "unreachable"
  echo
done
