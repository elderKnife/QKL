#!/usr/bin/env python
"""End-to-end demonstration for the minimal validation experiment.

Flow:
    edge (sign, no chain) -> VPP (verify, Merkle, root to chain) -> grid (verify)

The script feeds records, waits for the chain-confirmed batches, asks the grid
platform for its independent verification result and writes an evidence file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from scripts.feed_records import feed_round  # noqa: E402
from src.common.config import load_config  # noqa: E402


def wait_json(url: str, predicate, timeout: float = 60.0, interval: float = 0.5) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = requests.get(url, timeout=10).json()
        except requests.RequestException as exc:
            last = {"error": str(exc)}
            time.sleep(interval)
            continue
        if predicate(last):
            return last
        time.sleep(interval)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()

    config = load_config()
    run_id = args.run_id or (config["experiment"]["runId"] + "-" + datetime.now(tz=timezone.utc).strftime("%H%M%S"))
    print(f"runId = {run_id}")

    health = {}
    for edge in config["edges"]:
        health[edge["edgeId"]] = requests.get(f"http://127.0.0.1:{edge['port']}/healthz", timeout=5).json()
    for vpp in config["vpps"]:
        health[vpp["vppId"]] = requests.get(f"http://127.0.0.1:{vpp['port']}/healthz", timeout=5).json()
    health["grid"] = requests.get(f"http://127.0.0.1:{config['grid']['port']}/healthz", timeout=5).json()
    print("health:", json.dumps(health, ensure_ascii=False))

    packets = []
    for round_no in range(1, args.rounds + 1):
        results = feed_round(config, run_id, round_no)
        for item in results:
            for packet in item["result"]["packets"]:
                packets.append({"edgeId": item["edgeId"], "packetId": packet["packetId"], "round": round_no})
        print(f"round {round_no}: " + ", ".join(f"{i['edgeId']}->{len(i['result']['packets'])} packet(s)" for i in results))

    vpp_ports = {vpp["vppId"]: vpp["port"] for vpp in config["vpps"]}
    edge_vpp = {edge["edgeId"]: edge["vppId"] for edge in config["edges"]}
    rows = []
    for item in packets:
        port = vpp_ports[edge_vpp[item["edgeId"]]]
        detail = wait_json(
            f"http://127.0.0.1:{port}/api/v1/packets/{item['packetId']}",
            lambda d: d.get("status") in ("FORWARDED", "FORWARD_FAILED", "FAILED", "UNKNOWN", "CHAIN_UNAVAILABLE"),
        )
        rows.append(
            {
                "edgeId": item["edgeId"],
                "packetId": item["packetId"],
                "batchId": detail.get("batchId"),
                "vppStatus": detail.get("status"),
                "txHash": detail.get("txHash"),
                "blockNumber": detail.get("blockNumber"),
                "rootHash": detail.get("rootHash"),
                "recordCount": detail.get("recordCount"),
                "vppId": detail.get("envelope", {}).get("vppId"),
            }
        )

    time.sleep(1.0)
    batches = requests.get(f"http://127.0.0.1:{config['grid']['port']}/api/v1/batches", timeout=10).json().get("batches", [])
    by_id = {b["batchId"]: b for b in batches}
    for row in rows:
        grid = by_id.get(row["batchId"], {})
        row["gridStatus"] = grid.get("status")
        row["gridChainRoot"] = grid.get("chainRoot")

    print("\n=== data path evidence ===")
    for row in rows:
        print(
            f"{row['edgeId']} {row['packetId']} -> {row['vppId']} batch={row['batchId']} "
            f"tx={row['txHash']} block={row['blockNumber']} root={row['rootHash']} grid={row['gridStatus']}"
        )

    all_ok = all(row["gridStatus"] == "VERIFIED" for row in rows) and all(row["vppStatus"] == "FORWARDED" for row in rows)
    report = {
        "runId": run_id,
        "generatedAt": datetime.now(tz=timezone.utc).isoformat(),
        "rounds": args.rounds,
        "packets": rows,
        "allVerified": all_ok,
        "gridState": requests.get(f"http://127.0.0.1:{config['grid']['port']}/api/v1/state", timeout=10).json(),
    }
    out_dir = os.path.join(PROJECT_ROOT, "data", "reports")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"demo-{run_id}.json")
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    print(f"\nallVerified={all_ok}; evidence written to {out_path}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
