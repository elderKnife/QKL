#!/usr/bin/env python
"""Deploy RootHashRegistry and authorise the two VPP transaction accounts.

Writes ``config/chain.json`` with the deployment evidence (address, tx hash,
block number) that the services and the report use.
"""
from __future__ import annotations

import json
import os
import sys
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.common.config import load_config, resolve_path  # noqa: E402
from src.chain.client import ChainClient  # noqa: E402
from src.chain.keys import address_from_pem, load_artifacts  # noqa: E402
from src.chain.registry import RootHashRegistry  # noqa: E402


def main() -> int:
    config = load_config()
    chain = config["chain"]
    grid_account = config["accounts"]["grid"]
    rpc_url = chain["rpcNodes"][grid_account["rpcNode"]]
    pem = resolve_path(grid_account["pem"])

    print(f"[deploy] rpc={rpc_url} account={pem}")
    client = ChainClient(rpc_url, pem, group_id=chain["groupId"], chain_id=chain["chainId"])
    print(f"[deploy] deployer address = {client.address()}")
    print(f"[deploy] block number before = {client.block_number()}")

    abi, bytecode = load_artifacts(os.path.join(PROJECT_ROOT, "contracts", "build"))
    result = client.deploy(abi, bytecode, [])
    print(f"[deploy] status={result.status} tx={result.tx_hash} block={result.block_number} addr={result.contract_address}")
    if not result.ok or not result.contract_address:
        print(f"[deploy] FAILED output={result.output}")
        return 1

    registry = RootHashRegistry(client, result.contract_address, abi)
    authorizations = []
    for role in ("vppA", "vppB"):
        account = config["accounts"][role]
        vpp_address = address_from_pem(resolve_path(account["pem"]))
        auth = registry.set_authorized(vpp_address, True)
        print(f"[deploy] authorize {role} {vpp_address}: {auth.status} tx={auth.tx_hash} block={auth.block_number}")
        if not auth.ok:
            print(f"[deploy] authorize failed: {auth.output}")
            return 1
        authorizations.append(
            {
                "role": role,
                "address": vpp_address,
                "txHash": auth.tx_hash,
                "blockNumber": auth.block_number,
                "status": auth.status,
            }
        )

    checks = {
        "owner": registry.owner(),
        "vppAAuthorized": registry.is_authorized(authorizations[0]["address"]),
        "vppBAuthorized": registry.is_authorized(authorizations[1]["address"]),
    }
    print(f"[deploy] on-chain checks: {checks}")
    if not (checks["vppAAuthorized"] and checks["vppBAuthorized"]):
        print("[deploy] authorization check failed")
        return 1

    evidence = {
        "contractName": "RootHashRegistry",
        "address": result.contract_address,
        "deployer": client.address(),
        "deployTxHash": result.tx_hash,
        "deployBlockNumber": result.block_number,
        "rpcUrl": rpc_url,
        "groupId": chain["groupId"],
        "chainId": chain["chainId"],
        "deployedAt": int(time.time() * 1000),
        "authorizations": authorizations,
        "owner": checks["owner"],
    }
    out_path = os.path.join(PROJECT_ROOT, "config", "chain.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, indent=2, ensure_ascii=False)
    print(f"[deploy] evidence written to {out_path}")
    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
