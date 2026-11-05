"""Experiment A: trusted storage of distributed energy operation data."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone

from . import chain
from .common import deterministic_json_bytes, leaf_hash, unique_batch_id
from .merkle import build_tree, compute_root, generate_proof, verify_proof
from .outputs import new_run_dir, write_json, write_text


def _sample_records() -> list[dict]:
    return [
        {
            "recordId": "EDGE-PV-001",
            "deviceId": "PV-01",
            "deviceType": "photovoltaic",
            "metric": "active_power",
            "value": 45.6,
            "unit": "kW",
            "timestamp": "2026-10-24T13:00:00Z",
        },
        {
            "recordId": "EDGE-BESS-001",
            "deviceId": "BESS-01",
            "deviceType": "battery_storage",
            "metric": "soc",
            "value": 82.5,
            "unit": "%",
            "timestamp": "2026-10-24T13:00:05Z",
        },
        {
            "recordId": "EDGE-BESS-002",
            "deviceId": "BESS-01",
            "deviceType": "battery_storage",
            "metric": "charge_power",
            "value": 12.0,
            "unit": "kW",
            "timestamp": "2026-10-24T13:00:10Z",
        },
    ]


def run() -> dict:
    run_dir = new_run_dir("experiment-A")
    print(f"[experiment A] run directory: {run_dir}")

    records = _sample_records()
    write_json(os.path.join(run_dir, "edge_records.json"), {"records": records})

    # EDGE: deterministic bytes and leaf digests.
    leaf_details = []
    for record in sorted(records, key=lambda r: r["recordId"]):
        raw = deterministic_json_bytes(record)
        leaf_details.append(
            {
                "recordId": record["recordId"],
                "utf8BytesHex": raw.hex(),
                "leafSha256": leaf_hash(raw).hex(),
                "json": record,
            }
        )

    tree = build_tree(records)
    root = tree.root.hex()
    write_json(
        os.path.join(run_dir, "merkle_inputs.json"),
        {
            "leafDomain": "FISCO-BCOS-EVIDENCE-LEAF-V1:",
            "nodeDomain": "FISCO-BCOS-EVIDENCE-NODE-V1:",
            "oddLeafRule": "promote-last-node-without-duplication",
            "orderRule": "sort-by-recordId-ascending",
            "leaves": leaf_details,
            "merkleRoot": root,
        },
    )

    # VPP recomputes and submits the batch metadata on-chain.
    recomputed = compute_root(records)
    assert recomputed == root, "VPP recomputed root mismatch"
    batch_id = unique_batch_id("DATA")
    data_type = "ENERGY_DATA"
    ts = int(datetime.now(timezone.utc).timestamp())
    print(f"[experiment A] submitting batch {batch_id}, root {root}")
    tx = chain.record_batch(
        batch_id=batch_id,
        evidence_type="DATA_BATCH",
        data_type=data_type,
        merkle_root=root,
        record_count=len(records),
        submitter="VPP_A",
        related_batch_id="",
        timestamp=ts,
    )
    receipt = chain.get_transaction_receipt(tx["tx_hash"])
    block_number = chain.get_block_number()
    block = chain.get_block_by_number(block_number)
    tx_by_hash = chain.get_transaction_by_hash(tx["tx_hash"])

    # GRID reads the same batch through two different node RPC ports.
    read_20200 = chain.get_batch(batch_id, port=20200)
    read_20202 = chain.get_batch(batch_id, port=20202)
    both_match = (
        read_20200["merkleRoot"] == root
        and read_20202["merkleRoot"] == root
        and read_20200["merkleRoot"] == read_20202["merkleRoot"]
    )

    # Merkle membership proof for one record.
    proof = generate_proof(records, "EDGE-BESS-001")
    target = next(r for r in records if r["recordId"] == "EDGE-BESS-001")
    proof_ok = verify_proof(target, proof, root)

    # Tamper experiment.
    tampered = copy.deepcopy(records)
    tampered[1]["value"] = 91.7
    tampered_root = compute_root(tampered)
    proof_fails_on_tampered = not verify_proof(tampered[1], proof, root)
    tampered_root_differs = tampered_root != root

    evidence = {
        "experiment": "A",
        "batchId": batch_id,
        "contract": {
            "name": chain.CONTRACT_NAME,
            "address": chain.CONTRACT_ADDRESS,
        },
        "merkleRoot": root,
        "recordCount": len(records),
        "transaction": {
            "txHash": tx["tx_hash"],
            "status": tx["status"],
            "blockNumber": block_number,
            "block": block,
            "txByHash": tx_by_hash,
            "receipt": receipt,
        },
        "nodeQueries": {
            "20200": read_20200,
            "20202": read_20202,
            "bothMatch": both_match,
        },
        "membershipProof": {
            "record": target,
            "proof": proof,
            "verified": proof_ok,
        },
        "tamper": {
            "originalRoot": root,
            "tamperedRecord": tampered[1],
            "tamperedRoot": tampered_root,
            "proofFailsOnTampered": proof_fails_on_tampered,
            "tamperedRootDiffers": tampered_root_differs,
        },
        "pass": bool(
            tx["status"] == 0
            and both_match
            and proof_ok
            and proof_fails_on_tampered
            and tampered_root_differs
        ),
    }
    write_json(os.path.join(run_dir, "evidence.json"), evidence)
    write_text(
        os.path.join(run_dir, "chain_outputs.txt"),
        "\n\n".join(
            [
                f"recordBatch output:\n{tx['output']}",
                f"receipt:\n{receipt}",
                f"block:\n{block}",
                f"txByHash:\n{tx_by_hash}",
                f"getBatch(20200):\n{read_20200['raw']}",
                f"getBatch(20202):\n{read_20202['raw']}",
            ]
        ),
    )
    print(
        f"[experiment A] done. pass={evidence['pass']} "
        f"tx={tx['tx_hash']} block={block_number} root={root}"
    )
    return evidence
