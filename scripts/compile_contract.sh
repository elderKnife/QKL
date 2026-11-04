#!/usr/bin/env bash
# Compile contracts/RootHashRegistry.sol with solc 0.6.10 (FISCO BCOS 2.x EVM).
# tools/solc is the upstream macOS x86_64 build (runs under Rosetta on Apple Silicon).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -x "$ROOT/tools/solc" ]; then
  echo "tools/solc missing. Download solc 0.6.10:" >&2
  echo "  curl -L -o tools/solc https://github.com/ethereum/solidity/releases/download/v0.6.10/solc-macos && chmod +x tools/solc" >&2
  exit 1
fi
mkdir -p "$ROOT/contracts/build"
"$ROOT/tools/solc" --bin --abi --overwrite -o "$ROOT/contracts/build" "$ROOT/contracts/RootHashRegistry.sol"
echo "artifacts: $(ls "$ROOT/contracts/build")"
