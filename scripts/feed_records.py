#!/usr/bin/env python
"""Temporary record feeder used *instead of* the A1 device simulator.

A1 (300-device simulator) is explicitly out of scope for this validation
experiment.  This script only produces a handful of plausible power records per
edge so the edge -> VPP -> chain -> grid path can be exercised.  It is clearly
labelled as a stand-in and is not part of the trusted data path.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone

import requests

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.common.config import load_config  # noqa: E402

METRIC = "active_power_kw"
UNIT = "kW"


def utc_iso_ms(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def power_for(device_type: str, rng: random.Random) -> float:
    if device_type == "pv":
        value = rng.uniform(5.0, 45.0)
    elif device_type == "storage":
        value = rng.uniform(-12.0, 12.0)
    elif device_type == "charger":
        value = rng.uniform(0.0, 30.0)
    else:  # pragma: no cover - config validated elsewhere
        raise ValueError(device_type)
    return round(value, 3)


def build_records(edge: dict, run_id: str, round_no: int, moment: datetime) -> list[dict]:
    records = []
    for device in edge["devices"]:
        rng = random.Random(f"{run_id}:{round_no}:{device['deviceId']}")
        records.append(
            {
                "recordId": f"REC-{run_id}-{device['deviceId']}-{round_no:03d}",
                "deviceId": device["deviceId"],
                "deviceType": device["deviceType"],
                "metric": METRIC,
                "value": power_for(device["deviceType"], rng),
                "unit": UNIT,
                "timestamp": utc_iso_ms(moment),
            }
        )
    return records


def post_records(edge: dict, records: list[dict]) -> dict:
    url = f"http://127.0.0.1:{edge['port']}/api/v1/records"
    response = requests.post(url, json={"records": records}, timeout=30)
    payload = response.json()
    if response.status_code >= 400 and not payload.get("accepted"):
        raise RuntimeError(f"edge {edge['edgeId']} rejected records: {payload}")
    return payload


def feed_round(config: dict, run_id: str, round_no: int, moment: datetime | None = None) -> list[dict]:
    moment = moment or datetime.now(tz=timezone.utc)
    results = []
    for edge in config["edges"]:
        records = build_records(edge, run_id, round_no, moment)
        result = post_records(edge, records)
        results.append({"edgeId": edge["edgeId"], "sent": len(records), "result": result})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    config = load_config(args.config)
    run_id = args.run_id or config["experiment"]["runId"]
    summary = []
    for round_no in range(1, args.rounds + 1):
        results = feed_round(config, run_id, round_no)
        summary.append({"round": round_no, "edges": results})
        print(f"round {round_no}: " + ", ".join(
            f"{item['edgeId']} sent={item['sent']} packets={[p['packetId'] for p in item['result']['packets']]}"
            for item in results
        ))
    print(json.dumps(summary, indent=2, ensure_ascii=False)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
