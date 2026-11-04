#!/usr/bin/env python
"""Tamper drill: prove that a modified off-chain record is detected against the
on-chain root, and that the failing record is identified individually.

The drill uses ``POST /api/v1/verify`` which re-verifies a supplied batch
without persisting it, so the original evidence stays untouched.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone

import requests

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.common.config import load_config  # noqa: E402


def pick_packet(config: dict) -> tuple[str, dict]:
    for vpp in config["vpps"]:
        packets = requests.get(f"http://127.0.0.1:{vpp['port']}/api/v1/packets", timeout=10).json().get("packets", [])
        for packet in packets:
            if packet.get("status") == "FORWARDED":
                detail = requests.get(
                    f"http://127.0.0.1:{vpp['port']}/api/v1/packets/{packet['packetId']}", timeout=10
                ).json()
                return vpp["vppId"], detail
    raise SystemExit("no FORWARDED packet found; run scripts/demo_flow.py first")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vpp-id", default=None)
    parser.add_argument("--record-index", type=int, default=1)
    args = parser.parse_args()

    config = load_config()
    vpp_id, detail = pick_packet(config)
    envelope = detail["envelope"]
    batch_id = envelope["batchId"]
    grid_url = f"http://127.0.0.1:{config['grid']['port']}"

    chain = requests.get(f"{grid_url}/api/v1/chain/batches/{batch_id}", timeout=10).json()
    print(f"batch {batch_id} on chain: root={chain['chain']['rootHash']} count={chain['chain']['recordCount']} "
          f"queriedVia={chain['queriedVia']}")

    original = requests.post(f"{grid_url}/api/v1/verify", json={"envelope": envelope}, timeout=30).json()
    print(f"original batch -> {original['status']} (checks={original['checks']})")
    if original["status"] != "VERIFIED":
        raise SystemExit("original batch did not verify; refusing to run the tamper drill")

    record = envelope["records"][args.record_index]
    tampered = copy.deepcopy(envelope)
    before = tampered["records"][args.record_index]["value"]
    tampered["records"][args.record_index]["value"] = round(float(before) + 7.0, 3)
    tampered["records"][args.record_index]["recordId"] = record["recordId"]
    print(f"tampering record {record['recordId']}: {before} -> {tampered['records'][args.record_index]['value']}")

    result = requests.post(f"{grid_url}/api/v1/verify", json={"envelope": tampered}, timeout=30).json()
    print(f"tampered batch -> {result['status']}")
    print(f"  declaredRoot={result['declaredRootHash']}")
    print(f"  recomputedRoot={result['recomputedRootHash']}")
    print(f"  chainRoot  ={result['chainRootHash']}")
    print(f"  failures   ={json.dumps(result['failures'], ensure_ascii=False)}")

    failing_ids = {item["recordId"] for item in result["failures"] if item.get("recordId")}
    detected = result["status"] == "REJECTED" and record["recordId"] in failing_ids
    target_only = failing_ids == {record["recordId"]}
    print(f"detected={detected} only_target_reported={target_only}")

    # A forged proof must also be rejected (metadata cannot be trusted).
    forged = copy.deepcopy(envelope)
    if forged["proofs"]:
        forged["proofs"][0]["siblings"] = list(reversed(forged["proofs"][0]["siblings"]))
    forged_result = requests.post(f"{grid_url}/api/v1/verify", json={"envelope": forged}, timeout=30).json()
    print(f"forged proof batch -> {forged_result['status']}")

    # Edge -> VPP: a tampered packet must be rejected by signature verification.
    edge_tamper = drill_edge_tamper(config)

    report = {
        "generatedAt": datetime.now(tz=timezone.utc).isoformat(),
        "edgeTamper": edge_tamper,
        "batchId": batch_id,
        "vppId": vpp_id,
        "chain": chain,
        "originalStatus": original["status"],
        "tamperedRecordId": record["recordId"],
        "before": before,
        "after": tampered["records"][args.record_index]["value"],
        "tamperedStatus": result["status"],
        "tamperedFailures": result["failures"],
        "forgedProofStatus": forged_result["status"],
        "detected": detected,
        "onlyTargetReported": target_only,
    }
    out_dir = os.path.join(PROJECT_ROOT, "data", "reports")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"tamper-{batch_id}.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    print(f"report written to {out_path}")
    edge_ok = edge_tamper.get("tamperedStatus") == "SIGNATURE_INVALID" and edge_tamper.get("unknownKeyStatus") == "IDENTITY_NOT_ALLOWED"
    return 0 if (detected and target_only and forged_result["status"] == "REJECTED" and edge_ok) else 1




def drill_edge_tamper(config: dict) -> dict:
    """Re-post a modified edge packet / an unknown keyId and record the rejection."""
    edge = config["edges"][0]
    vpp = next(item for item in config["vpps"] if item["vppId"] == edge["vppId"])
    packets = requests.get(f"http://127.0.0.1:{edge['port']}/api/v1/packets", timeout=10).json().get("packets", [])
    delivered = [item for item in packets if item.get("status") == "DELIVERED"]
    result = {"packetId": None, "tamperedStatus": None, "unknownKeyStatus": None}
    if not delivered:
        return result
    packet_id = delivered[0]["packetId"]
    detail = requests.get(f"http://127.0.0.1:{edge['port']}/api/v1/packets/{packet_id}", timeout=10).json()
    packet = detail["packet"]
    signature = detail["signature"]
    result["packetId"] = packet_id

    tampered = copy.deepcopy(packet)
    tampered["records"][0]["value"] = round(float(tampered["records"][0]["value"]) + 1.0, 3)
    response = requests.post(
        f"http://127.0.0.1:{vpp['port']}/api/v1/edge-packets",
        json={"packet": tampered, "signature": signature},
        timeout=20,
    )
    body = response.json()
    result["tamperedStatus"] = body.get("error", {}).get("code")
    result["tamperedHttp"] = response.status_code

    forged_signature = dict(signature)
    forged_signature["keyId"] = "unknown-key-v9"
    response = requests.post(
        f"http://127.0.0.1:{vpp['port']}/api/v1/edge-packets",
        json={"packet": packet, "signature": forged_signature},
        timeout=20,
    )
    body = response.json()
    result["unknownKeyStatus"] = body.get("error", {}).get("code")
    result["unknownKeyHttp"] = response.status_code
    print(f"edge packet tamper -> {result['tamperedStatus']} (HTTP {result['tamperedHttp']})")
    print(f"unknown keyId      -> {result['unknownKeyStatus']} (HTTP {result['unknownKeyHttp']})")
    return result


if __name__ == "__main__":
    sys.exit(main())
