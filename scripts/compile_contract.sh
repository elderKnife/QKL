#!/usr/bin/env bash
# Compile both registries with solc 0.6.10; solc-js also works on ARM.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/contracts/build"
if [ -x "$ROOT/tools/solc" ]; then
  "$ROOT/tools/solc" --version | grep -Eq 'Version: 0\.6\.10\+' || {
    echo "tools/solc must be version 0.6.10" >&2
    exit 1
  }
  "$ROOT/tools/solc" --bin --abi --overwrite -o "$ROOT/contracts/build" "$ROOT/contracts/RootHashRegistry.sol"
  "$ROOT/tools/solc" --bin --abi --overwrite -o "$ROOT/contracts/build" "$ROOT/contracts/RootHashRegistryV2.sol"
else
  node "$ROOT/scripts/compile_contracts.cjs"
fi
echo "artifacts: $(ls "$ROOT/contracts/build")"
