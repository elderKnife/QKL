"""Experiment B: traceability of power-grid control commands."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone

from . import chain
from .common import deterministic_json_bytes, unique_batch_id
from .crypto_util import generate_or_load_keypair, public_key_hex, sign_bytes, verify_bytes
from .merkle import build_tree, compute_root, generate_proof, verify_proof
from .outputs import new_run_dir, write_json, write_text


KEY_DIR = "/home/blockchain/fisco-bcos-lab/runtime/business_keys"


def _sample_commands(nonce1: str, nonce2: str, nonce3: str) -> list[dict]:
    return [
        {
            "commandId": "GRID-CMD-001",
            "deviceId": "BESS-01",
            "commandType": "storage_charge",
            "targetValue": 50.0,
            "unit": "kW",
            "timestamp": "2026-10-24T13:10:00Z",
            "nonce": nonce1,
        },
        {
            "commandId": "GRID-CMD-002",
            "deviceId": "BESS-01",
            "commandType": "storage_discharge",
            "targetValue": 30.0,
            "unit": "kW",
            "timestamp": "2026-10-24T13:10:05Z",
            "nonce": nonce2,
        },
        {
            "commandId": "GRID-CMD-003",
            "deviceId": "PV-01",
            "commandType": "pv_curtailment",
            "targetValue": 20.0,
            "unit": "kW",
            "timestamp": "2026-10-24T13:10:10Z",
            "nonce": nonce3,
        },
    ]


def run() -> dict:
    run_dir = new_run_dir("experiment-B")
    print(f"[experiment B] run directory: {run_dir}")

    os.makedirs(KEY_DIR, exist_ok=True)
    private_key, public_key = generate_or_load_keypair(
        os.path.join(KEY_DIR, "grid_business_priv.pem"),
        os.path.join(KEY_DIR, "grid_business_pub.pem"),
    )
    pub_hex = public_key_hex(public_key)

    nonces = [os.urandom(8).hex() for _ in range(3)]
    commands = _sample_commands(*nonces)
    write_json(os.path.join(run_dir, "commands.json"), {"commands": commands})

    # GRID signs each deterministic byte sequence with the business key.
    signed = []
    for cmd in commands:
        raw = deterministic_json_bytes(cmd)
        signature = sign_bytes(private_key, raw)
        signed.append(
            {
                "command": cmd,
                "signedBytesHex": raw.hex(),
                "signature": signature,
            }
        )
    write_json(
        os.path.join(run_dir, "signed_commands.json"),
        {"publicKeyHex": pub_hex, "signedCommands": signed},
    )

    root = compute_root(commands, id_field="commandId")
    batch_id = unique_batch_id("CMD")
    ts = int(datetime.now(timezone.utc).timestamp())
    print(f"[experiment B] submitting instruction batch {batch_id}, root {root}")
    tx = chain.record_batch(
        batch_id=batch_id,
        evidence_type="INSTRUCTION_BATCH",
        data_type="GRID_COMMAND",
        merkle_root=root,
        record_count=len(commands),
        submitter="GRID",
        related_batch_id="",
        timestamp=ts,
    )
    receipt = chain.get_transaction_receipt(tx["tx_hash"])
    block_number = chain.get_block_number()
    block = chain.get_block_by_number(block_number)
    tx_by_hash = chain.get_transaction_by_hash(tx["tx_hash"])

    read_20200 = chain.get_batch(batch_id, port=20200)
    read_20202 = chain.get_batch(batch_id, port=20202)
    original_batch_matches = (
        read_20200["merkleRoot"] == root
        and read_20202["merkleRoot"] == root
        and read_20200["merkleRoot"] == read_20202["merkleRoot"]
    )

    # VPP_A verifies every GRID signature before forwarding.
    signatures_ok = all(
        verify_bytes(public_key, deterministic_json_bytes(s["command"]), s["signature"])
        for s in signed
    )

    # VPP_A writes an explicit forwarding record linked to the original batch.
    forward_batch_id = unique_batch_id("FORWARD")
    forward_tx = chain.record_batch(
        batch_id=forward_batch_id,
        evidence_type="FORWARD",
        data_type="COMMAND_FORWARD",
        merkle_root=root,
        record_count=len(commands),
        submitter="VPP_A",
        related_batch_id=batch_id,
        timestamp=ts,
    )
    forward_20200 = chain.get_batch(forward_batch_id, port=20200)
    forward_20202 = chain.get_batch(forward_batch_id, port=20202)
    forward_ok = (
        forward_20200["relatedBatchId"] == batch_id
        and forward_20202["relatedBatchId"] == batch_id
        and forward_20200["merkleRoot"] == root
        and forward_20202["merkleRoot"] == root
        and forward_20200["evidenceType"] == "FORWARD"
        and forward_20202["evidenceType"] == "FORWARD"
    )

    # EDGE verifies membership of one command and simulates execution.
    proof = generate_proof(commands, "GRID-CMD-002", id_field="commandId")
    target = next(c for c in commands if c["commandId"] == "GRID-CMD-002")
    proof_ok = verify_proof(target, proof, root)
    confirmations = [
        {
            "confirmationId": f"EDGE-CONF-{cmd['commandId']}",
            "commandId": cmd["commandId"],
            "deviceId": cmd["deviceId"],
            "status": "SIMULATED_SUCCESS",
            "note": "simulated execution confirmation only; this is not physical execution proof",
        }
        for cmd in commands
    ]
    write_json(
        os.path.join(run_dir, "edge_confirmations.json"),
        {"confirmations": confirmations},
    )

    # Tamper one command: the signature must fail and a new root must differ.
    tampered = copy.deepcopy(commands)
    tampered[1]["targetValue"] = 55.5
    tampered_signature_still_fails = not verify_bytes(
        public_key,
        deterministic_json_bytes(tampered[1]),
        signed[1]["signature"],
    )
    tampered_root = compute_root(tampered, id_field="commandId")
    tampered_root_differs = tampered_root != root

    evidence = {
        "experiment": "B",
        "businessPublicKeyHex": pub_hex,
        "batchId": batch_id,
        "contract": {
            "name": chain.CONTRACT_NAME,
            "address": chain.CONTRACT_ADDRESS,
        },
        "merkleRoot": root,
        "recordCount": len(commands),
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
            "originalBatchMatches": original_batch_matches,
        },
        "forward": {
            "batchId": forward_batch_id,
            "txHash": forward_tx["tx_hash"],
            "relatedBatchId": batch_id,
            "nodeQueries": {
                "20200": forward_20200,
                "20202": forward_20202,
            },
            "forwardOk": forward_ok,
        },
        "signatureVerification": {
            "allSignaturesValid": signatures_ok,
        },
        "membershipProof": {
            "record": target,
            "proof": proof,
            "verified": proof_ok,
        },
        "edgeSimulation": confirmations,
        "tamper": {
            "originalRoot": root,
            "tamperedCommand": tampered[1],
            "signatureFails": tampered_signature_still_fails,
            "tamperedRoot": tampered_root,
            "tamperedRootDiffers": tampered_root_differs,
        },
        "pass": bool(
            tx["status"] == 0
            and original_batch_matches
            and signatures_ok
            and forward_ok
            and proof_ok
            and tampered_signature_still_fails
            and tampered_root_differs
        ),
    }
    write_json(os.path.join(run_dir, "evidence.json"), evidence)
    write_text(
        os.path.join(run_dir, "chain_outputs.txt"),
        "\n\n".join(
            [
                f"recordBatch(instruction):\n{tx['output']}",
                f"receipt:\n{receipt}",
                f"block:\n{block}",
                f"txByHash:\n{tx_by_hash}",
                f"getBatch(20200):\n{read_20200['raw']}",
                f"getBatch(20202):\n{read_20202['raw']}",
                f"recordBatch(forward):\n{forward_tx['output']}",
                f"getBatch(forward, 20200):\n{forward_20200['raw']}",
                f"getBatch(forward, 20202):\n{forward_20202['raw']}",
            ]
        ),
    )
    print(
        f"[experiment B] done. pass={evidence['pass']} "
        f"tx={tx['tx_hash']} forward={forward_batch_id}"
    )
    return evidence
