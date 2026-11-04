#!/usr/bin/env python
"""Collect reproducible FISCO BCOS evidence for the experiment report.

Checks (per the five-node experiment book):
  * five independent node processes and their node ids,
  * five nodes in the sealer list / peer list (consensus participation),
  * non-decreasing PBFT view (consensus progress),
  * the same transaction read through different nodes,
  * the same batch root read through different nodes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.chain.client import ChainClient  # noqa: E402
from src.chain.keys import load_artifacts  # noqa: E402
from src.chain.registry import RootHashRegistry  # noqa: E402
from src.common.config import contract_address, load_chain_evidence, load_config, resolve_path  # noqa: E402

NODE_PORTS = [8545, 8546, 8547, 8548, 8549]


def rpc(port: int, method: str, params: list) -> dict:
    response = requests.post(
        f"http://127.0.0.1:{port}",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        timeout=10,
    )
    return response.json()


def main() -> int:
    config = load_config()
    evidence: dict = {
        "generatedAt": datetime.now(tz=timezone.utc).isoformat(),
        "chainVersion": None,
        "processes": [],
        "nodes": [],
        "transactionCrossNode": None,
        "batchCrossNode": None,
    }

    processes = subprocess.run(["pgrep", "-fl", "fisco-bcos"], capture_output=True, text=True).stdout.strip().splitlines()
    evidence["processes"] = processes

    sealers = set()
    for index, port in enumerate(NODE_PORTS):
        version = rpc(port, "getClientVersion", [])
        block = rpc(port, "getBlockNumber", [1])
        seal = rpc(port, "getSealerList", [1])
        peers = rpc(port, "getGroupPeers", [1])
        pbft_a = rpc(port, "getPbftView", [1])
        time.sleep(0.8)
        pbft_b = rpc(port, "getPbftView", [1])
        node_id_path = os.path.join(PROJECT_ROOT, "chain", "nodes", "127.0.0.1", f"node{index}", "conf", "node.nodeid")
        node_id = None
        if os.path.exists(node_id_path):
            with open(node_id_path, "r", encoding="ascii") as handle:
                node_id = handle.read().strip()
        evidence["chainVersion"] = evidence["chainVersion"] or (
            version.get("result", {}).get("FISCO-BCOS Version") if isinstance(version.get("result"), dict) else None
        )
        entry = {
            "index": index,
            "rpc": f"http://127.0.0.1:{port}",
            "nodeId": node_id,
            "version": version.get("result"),
            "blockNumber": int(block["result"], 16) if block.get("result") else None,
            "sealerCount": len(seal.get("result", [])),
            "peerCount": len(peers.get("result", [])),
            "sealers": seal.get("result", []),
            "pbftViewBefore": int(pbft_a["result"], 16) if pbft_a.get("result") else None,
            "pbftViewAfter": int(pbft_b["result"], 16) if pbft_b.get("result") else None,
        }
        sealers.update(seal.get("result", []))
        evidence["nodes"].append(entry)

    evidence["distinctSealers"] = len(sealers)
    evidence["allNodesSeeSameSealers"] = all(node["sealerCount"] == len(sealers) for node in evidence["nodes"])
    node_ids = {node["nodeId"] for node in evidence["nodes"] if node["nodeId"]}
    evidence["allNodeIdsAreSealers"] = node_ids == sealers
    evidence["nodeIdCount"] = len(node_ids)

    chain_evidence = load_chain_evidence(config)
    tx_hash = chain_evidence.get("deployTxHash")
    if tx_hash:
        receipts = {}
        for port in (NODE_PORTS[0], NODE_PORTS[3], NODE_PORTS[4]):
            body = rpc(port, "getTransactionReceipt", [1, tx_hash])
            result = body.get("result") or {}
            receipts[str(port)] = {
                "transactionHash": result.get("transactionHash"),
                "blockNumber": result.get("blockNumber"),
                "status": result.get("status"),
                "to": result.get("to"),
            }
        blocks = {value["blockNumber"] for value in receipts.values() if value.get("blockNumber")}
        evidence["transactionCrossNode"] = {
            "txHash": tx_hash,
            "description": "contract deployment transaction",
            "receipts": receipts,
            "sameBlockOnAllNodes": len(blocks) == 1,
        }

    # Batch root read through two different nodes with two different clients.
    batches = requests.get(f"http://127.0.0.1:{config['grid']['port']}/api/v1/batches", timeout=10).json().get("batches", [])
    verified = [b for b in batches if b.get("status") == "VERIFIED"]
    if verified:
        batch_id = verified[0]["batchId"]
        abi, _ = load_artifacts(os.path.join(PROJECT_ROOT, "contracts", "build"))
        address = contract_address(config)
        reads = {}
        for node_name, port in (("node0", NODE_PORTS[0]), ("node4", NODE_PORTS[4])):
            client = ChainClient(
                f"http://127.0.0.1:{port}",
                resolve_path(config["accounts"]["grid"]["pem"]),
                group_id=config["chain"]["groupId"],
                chain_id=config["chain"]["chainId"],
            )
            registry = RootHashRegistry(client, address, abi)
            chain_batch = registry.get_batch(batch_id)
            reads[node_name] = {
                "rpc": f"http://127.0.0.1:{port}",
                "batch": chain_batch.to_dict() if chain_batch else None,
            }
            client.close()
        roots = {value["batch"]["rootHash"] for value in reads.values() if value.get("batch")}
        evidence["batchCrossNode"] = {
            "batchId": batch_id,
            "reads": reads,
            "sameRootOnAllNodes": len(roots) == 1,
        }

    out_dir = os.path.join(PROJECT_ROOT, "data", "reports")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "chain-evidence.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, indent=2, ensure_ascii=False)

    print(f"fisco-bcos processes: {len(evidence['processes'])}")
    for node in evidence["nodes"]:
        print(
            f"  node{node['index']} rpc={node['rpc']} block={node['blockNumber']} "
            f"sealers={node['sealerCount']} peers={node['peerCount']} "
            f"pbft={node['pbftViewBefore']}->{node['pbftViewAfter']} nodeId={(node['nodeId'] or '')[:16]}..."
        )
    print(f"distinct sealers: {evidence['distinctSealers']} (all nodes agree: {evidence['allNodesSeeSameSealers']})")
    print(f"node files match sealer list: {evidence['allNodeIdsAreSealers']} ({evidence['nodeIdCount']} ids)")
    if evidence["transactionCrossNode"]:
        print(f"tx cross-node: {evidence['transactionCrossNode']['sameBlockOnAllNodes']} "
              f"({evidence['transactionCrossNode']['txHash'][:20]}...)")
    if evidence["batchCrossNode"]:
        print(f"batch cross-node: {evidence['batchCrossNode']['sameRootOnAllNodes']} "
              f"batch={evidence['batchCrossNode']['batchId']}")
    print(f"evidence written to {out_path}")

    ok = (
        len(evidence["processes"]) >= 5
        and evidence["distinctSealers"] == 5
        and evidence["allNodesSeeSameSealers"]
        and evidence["allNodeIdsAreSealers"]
        and (evidence.get("transactionCrossNode") or {}).get("sameBlockOnAllNodes", False)
        and (evidence.get("batchCrossNode") or {}).get("sameRootOnAllNodes", False)
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
