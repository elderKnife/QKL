"""Compatibility backend for the console-prototype Vue dashboard.

The Vue UI (``console-prototype/frontend``) was written against a Go/MySQL
prototype.  This service keeps that UI and its API contract, but answers every
request from the **real** FISCO BCOS experiment:

* edges / VPP / GRID come from the running Flask services,
* the chain record, transaction, receipt and block come from FISCO JSON-RPC,
* the ledger drawer returns the signatures, hashes and receipt of one packet.

UI semantics (EDGE-001/EDGE-002, one VPP workbench) are mapped onto the real
topology: vpp-a's two edges are exposed as EDGE-001/EDGE-002; the GRID
workbench still sees batches from both VPPs.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, session, send_from_directory

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.chain.keys import address_from_pem  # noqa: E402
from src.common.canonical import canonical_bytes  # noqa: E402
from src.common.config import contract_address, load_config, resolve_path  # noqa: E402
from src.common.crypto import Signer  # noqa: E402
from src.common.merkle import build_tree, leaf_hash  # noqa: E402

SERVICE = "console"
CONFIG = load_config()
EDGE_SPECS = {edge["edgeId"]: edge for edge in CONFIG["edges"]}
VPP_SPECS = {vpp["vppId"]: vpp for vpp in CONFIG["vpps"]}
GRID_SPEC = CONFIG["grid"]
DIST_DIR = os.path.join(PROJECT_ROOT, "console-prototype", "frontend", "dist")

UI_EDGES = {"EDGE-001": "edge-01", "EDGE-002": "edge-02"}
UI_VPP = "vpp-a"
DEVICE_TYPE = {"pv": "photovoltaic", "storage": "battery_storage", "charger": "charging_pile"}
POWER_RANGE = {"pv": (0, 50), "storage": (-50, 50), "charger": (0, 30)}

_CACHE: dict[str, tuple[float, object]] = {}
_CACHE_LOCK = threading.Lock()


def iso_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_from_ms(ms) -> str:
    try:
        moment = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return iso_now()
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _cached(key: str, ttl: float, factory):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = factory()
    with _CACHE_LOCK:
        _CACHE[key] = (now, value)
    return value


# --- HTTP helpers ---------------------------------------------------------
def _get(url: str, timeout: float = 4.0):
    try:
        response = requests.get(url, timeout=timeout)
        if response.status_code >= 400:
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def _post(url: str, payload=None, timeout: float = 30.0):
    try:
        response = requests.post(url, json=payload or {}, timeout=timeout)
        body = response.json() if response.content else {}
        return response.status_code, body
    except (requests.RequestException, ValueError) as exc:
        return None, {"error": str(exc)}


def edge_url(edge_id: str) -> str:
    return f"http://127.0.0.1:{EDGE_SPECS[edge_id]['port']}"


def vpp_url(vpp_id: str) -> str:
    return f"http://127.0.0.1:{VPP_SPECS[vpp_id]['port']}"


GRID_URL = f"http://127.0.0.1:{GRID_SPEC['port']}"
RPC_URL = CONFIG["chain"]["rpcNodes"][GRID_SPEC.get("rpcNode", "node3")]


def rpc(method: str, params: list):
    try:
        response = requests.post(
            RPC_URL, json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1}, timeout=5
        )
        return response.json().get("result")
    except (requests.RequestException, ValueError):
        return None


def hex_int(value):
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return None


# --- raw data -------------------------------------------------------------
def edge_state(edge_id: str) -> dict:
    return _cached(f"state:{edge_id}", 2, lambda: _get(f"{edge_url(edge_id)}/api/v1/state") or {})


def edge_collection(edge_id: str) -> dict:
    return _cached(f"coll:{edge_id}", 2, lambda: _get(f"{edge_url(edge_id)}/api/v1/collection") or {})


def edge_packets(edge_id: str) -> list:
    payload = _cached(f"packets:{edge_id}", 2, lambda: _get(f"{edge_url(edge_id)}/api/v1/packets") or {})
    return payload.get("packets", []) if isinstance(payload, dict) else []


def edge_packet(edge_id: str, packet_id: str):
    return _cached(f"ep:{edge_id}:{packet_id}", 4, lambda: _get(f"{edge_url(edge_id)}/api/v1/packets/{packet_id}"))


def vpp_state(vpp_id: str) -> dict:
    return _cached(f"vppstate:{vpp_id}", 2, lambda: _get(f"{vpp_url(vpp_id)}/api/v1/state") or {})


def vpp_packets(vpp_id: str) -> list:
    payload = _cached(f"vpppackets:{vpp_id}", 2, lambda: _get(f"{vpp_url(vpp_id)}/api/v1/packets") or {})
    return payload.get("packets", []) if isinstance(payload, dict) else []


def vpp_packet(vpp_id: str, packet_id: str):
    return _cached(f"vp:{vpp_id}:{packet_id}", 4, lambda: _get(f"{vpp_url(vpp_id)}/api/v1/packets/{packet_id}"))


def grid_batches() -> list:
    payload = _cached("gridbatches", 2, lambda: _get(f"{GRID_URL}/api/v1/batches") or {})
    return payload.get("batches", []) if isinstance(payload, dict) else []


def grid_batch(batch_id: str):
    return _get(f"{GRID_URL}/api/v1/batches/{batch_id}")


def grid_chain(batch_id: str):
    return _get(f"{GRID_URL}/api/v1/chain/batches/{batch_id}")


def packet_id_from_batch(batch_id: str) -> str:
    return batch_id[6:] if batch_id.startswith("BATCH-") else batch_id


def batch_id_from_packet(packet_id: str) -> str:
    return f"BATCH-{packet_id}"


def edge_for_ui(ui_edge: str) -> str | None:
    return UI_EDGES.get(ui_edge)


def ui_edge_for_source(source: str) -> str | None:
    if source in UI_EDGES:
        return source
    if source == "edge":
        return "EDGE-001"
    if source == "edge2":
        return "EDGE-002"
    return None


def vpp_for_source(source: str) -> str:
    return "vpp-a" if source == "vpp" else source


def vpp_for_edge(edge_id: str) -> str:
    return EDGE_SPECS[edge_id]["vppId"]


def ui_edge_label(edge_id: str) -> str:
    for label, real in UI_EDGES.items():
        if real == edge_id:
            return label
    if edge_id.startswith("edge-"):
        return "EDGE-" + edge_id.split("-", 1)[1].zfill(3)
    return edge_id


def edge_public_key(edge_id: str) -> str:
    spec = EDGE_SPECS[edge_id]
    try:
        return Signer.from_file(resolve_path(spec["signingKey"]), spec["keyId"]).public_key_hex
    except Exception:  # noqa: BLE001
        return ""


def vpp_account(vpp_id: str) -> str:
    try:
        return address_from_pem(resolve_path(CONFIG["accounts"][VPP_SPECS[vpp_id]["chainAccount"]]["pem"]))
    except Exception:  # noqa: BLE001
        return ""


def latest_packet_detail(edge_id: str):
    packets = edge_packets(edge_id)
    if not packets:
        return None
    return edge_packet(edge_id, packets[-1]["packetId"])


def packet_records(edge_id: str, packet_id: str) -> list:
    detail = edge_packet(edge_id, packet_id) or {}
    return (detail.get("packet") or {}).get("records") or []


# --- view models ----------------------------------------------------------
def page_args():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        size = max(1, min(100, int(request.args.get("pageSize", 25))))
    except (TypeError, ValueError):
        size = 25
    return page, size


def paged(items: list, page: int | None = None, size: int | None = None) -> dict:
    if page is None or size is None:
        page, size = page_args()
    start = (page - 1) * size
    return {"items": items[start:start + size], "total": len(items), "page": page, "pageSize": size}


def edge_overview(ui_edge: str) -> dict:
    edge_id = UI_EDGES[ui_edge]
    state = edge_state(edge_id)
    collection = edge_collection(edge_id)
    packets = edge_packets(edge_id)
    latest = latest_packet_detail(edge_id)
    records = (latest or {}).get("packet", {}).get("records", [])
    delivered = sum(1 for item in packets if item.get("status") == "DELIVERED")
    pending = sum(1 for item in packets if item.get("status") in ("BUILT", "DELIVERY_FAILED"))
    rejected = sum(1 for item in packets if item.get("status") == "DUPLICATE_AT_VPP")
    devices = EDGE_SPECS[edge_id]["devices"]
    window = (latest or {}).get("packet", {}).get("collectedAt") or collection.get("lastRoundAt") or iso_now()
    return {
        "edgeId": ui_edge,
        "available": True,
        "database": "ok",
        "configuredDevices": len(devices),
        "currentWindowDevices": len({record["deviceId"] for record in records}),
        "currentWindowRecords": len(records),
        "pendingBatches": pending,
        "verifiedBatches": delivered,
        "rejectedBatches": rejected,
        "totalRecords": sum(int(item.get("recordCount") or 0) for item in packets),
        "recordsAwaitingWindow": collection.get("buffered", 0),
        "sampleMinSeconds": collection.get("intervalSeconds", 10),
        "sampleMaxSeconds": collection.get("intervalSeconds", 10),
        "windowSeconds": collection.get("intervalSeconds", 10),
        "windowStart": window,
        "windowEnd": window,
        "serverTime": iso_now(),
        "simulation": {
            "running": bool(collection.get("running")),
            "runId": collection.get("runId") or "",
            "updatedAt": collection.get("lastRoundAt") or iso_now(),
        },
    }


def vpp_overview() -> dict:
    rows = vpp_packets(UI_VPP)
    return {
        "available": True,
        "data": {
            "chainAdapter": "fisco-bcos",
            "database": "chain",
            "chainMode": "FISCO-BCOS",
            "batchCount": len(rows),
            "recordCount": sum(int(item.get("recordCount") or 0) for item in rows),
            "serverTime": iso_now(),
        },
    }


def grid_overview() -> dict:
    rows = grid_batches()
    counts = _get(f"{GRID_URL}/api/v1/batches/count") or {}
    passed = counts.get("passed", sum(1 for item in rows if item.get("status") == "VERIFIED"))
    failed = counts.get("failed", sum(1 for item in rows if item.get("status") == "REJECTED"))
    total = counts.get("total", len(rows))
    return {
        "available": True,
        "data": {
            "available": True,
            "database": "ok",
            "chainMode": "FISCO-BCOS",
            "batchCount": total,
            "passedBatches": passed,
            "failedBatches": failed,
            "checkedAt": iso_now(),
            "serverTime": iso_now(),
        },
    }


def edge_roster(ui_edge: str) -> dict:
    edge_id = UI_EDGES[ui_edge]
    collection = edge_collection(edge_id)
    packets = edge_packets(edge_id)
    last = packets[-1] if packets else {}
    return {
        "edgeId": ui_edge,
        "available": True,
        "database": "ok",
        "configuredDevices": len(EDGE_SPECS[edge_id]["devices"]),
        "deviceCount": len(EDGE_SPECS[edge_id]["devices"]),
        "sampleIntervalSeconds": collection.get("intervalSeconds", 10),
        "batchWindowSeconds": collection.get("intervalSeconds", 10),
        "simulation": {
            "running": bool(collection.get("running")),
            "runId": collection.get("runId") or "",
            "updatedAt": collection.get("lastRoundAt") or iso_now(),
        },
        "sampleMinSeconds": collection.get("intervalSeconds", 10),
        "sampleMaxSeconds": collection.get("intervalSeconds", 10),
        "windowSeconds": collection.get("intervalSeconds", 10),
        "lastReceivedAt": (last.get("delivery") or {}).get("at") or last.get("createdAt"),
        "status": "VERIFIED" if last.get("status") == "DELIVERED" else "PENDING",
    }


def device_items(ui_edge: str) -> list:
    edge_id = UI_EDGES[ui_edge]
    latest = latest_packet_detail(edge_id)
    records = (latest or {}).get("packet", {}).get("records", [])
    latest_by_device = {record["deviceId"]: record for record in records}
    items = []
    for device in EDGE_SPECS[edge_id]["devices"]:
        low, high = POWER_RANGE[device["deviceType"]]
        record = latest_by_device.get(device["deviceId"])
        items.append(
            {
                "deviceId": device["deviceId"],
                "deviceType": DEVICE_TYPE[device["deviceType"]],
                "minPowerKW": low,
                "maxPowerKW": high,
                "latestRecord": {"value": record["value"], "timestamp": record["timestamp"]} if record else None,
            }
        )
    return items


def edge_batch_status(status: str) -> str:
    if status == "DELIVERED":
        return "VERIFIED"
    if status == "DUPLICATE_AT_VPP":
        return "REJECTED"
    return "PENDING"


def vpp_batch_status(status: str, grid_status: str | None) -> str:
    if grid_status == "VERIFIED":
        return "VERIFIED"
    if status in ("FORWARDED", "CONFIRMED"):
        return "PENDING"
    return "REJECTED"


def edge_batch_rows(edge_id: str) -> list:
    rows = []
    for item in reversed(edge_packets(edge_id)):
        created = item.get("createdAt") or iso_now()
        rows.append(
            {
                "packetId": item["packetId"],
                "edgeId": ui_edge_label(edge_id),
                "recordCount": item.get("recordCount", 0),
                "status": edge_batch_status(item.get("status", "")),
                "windowStart": created,
                "windowEnd": created,
                "merkleRoot": None,
                "vppMerkleRoot": None,
                "attempts": 1,
                "createdAt": created,
            }
        )
    return rows


def vpp_batch_rows(vpp_id: str) -> list:
    grid_map = {item["batchId"]: item.get("status") for item in grid_batches()}
    rows = []
    for item in vpp_packets(vpp_id):
        grid_status = grid_map.get(item.get("batchId"))
        rows.append(
            {
                "packetId": item["packetId"],
                "edgeId": ui_edge_label(item.get("edgeId") or ""),
                "recordCount": item.get("recordCount", 0),
                "status": vpp_batch_status(item.get("status", ""), grid_status),
                "signatureStatus": "PASS",
                "chainStatus": "CONFIRMED" if item.get("txHash") else "未记录",
                "chainMode": "FISCO-BCOS",
                "windowStart": item.get("createdAt") or iso_now(),
                "windowEnd": item.get("createdAt") or iso_now(),
                "merkleRoot": item.get("rootHash"),
                "verificationStatus": "PASS" if grid_status == "VERIFIED" else ("FAIL" if grid_status == "REJECTED" else "PENDING"),
                "batchId": item.get("batchId"),
                "txHash": item.get("txHash"),
                "blockNumber": item.get("blockNumber"),
                "createdAt": item.get("createdAt"),
            }
        )
    return rows


def grid_batch_rows() -> list:
    rows = []
    for item in grid_batches():
        packet_id = packet_id_from_batch(item["batchId"])
        rows.append(
            {
                "packetId": packet_id,
                "batchId": item["batchId"],
                "vppId": item.get("vppId"),
                "edgeId": ui_edge_label(item.get("edgeId") or ""),
                "recordCount": item.get("recordCount", 0),
                "localMerkleRoot": item.get("chainRoot"),
                "chainMerkleRoot": item.get("chainRoot"),
                "txHash": item.get("txHash"),
                "blockNumber": item.get("blockNumber"),
                "verificationStatus": "PASS" if item.get("status") == "VERIFIED" else ("FAIL" if item.get("status") == "REJECTED" else "PENDING"),
                "createdAt": item.get("updatedAt"),
                "updatedAt": item.get("updatedAt"),
            }
        )
    return rows


def edge_detail(edge_id: str, packet_id: str):
    detail = edge_packet(edge_id, packet_id)
    if not detail:
        return None
    packet = detail.get("packet") or {}
    signature = detail.get("signature") or {}
    delivery = detail.get("delivery") or {}
    status = edge_batch_status(detail.get("status", ""))
    return {
        "packetId": packet_id,
        "edgeId": ui_edge_label(edge_id),
        "recordCount": len(packet.get("records") or []),
        "windowStart": packet.get("collectedAt"),
        "windowEnd": packet.get("collectedAt"),
        "createdAt": detail.get("createdAt"),
        "batchId": batch_id_from_packet(packet_id),
        "signature": f"{signature.get('keyId', '')}:{signature.get('value', '')}",
        "payloadHash": detail.get("digest"),
        "merkleRoot": None,
        "status": status,
        "attempts": 1,
        "nextAttemptAt": 0,
        "lastError": delivery.get("error") or ((delivery.get("response") or {}).get("error") if isinstance(delivery.get("response"), dict) else None),
        "events": edge_events_for(edge_id, packet_id),
    }


def vpp_detail(vpp_id: str, packet_id: str):
    detail = vpp_packet(vpp_id, packet_id)
    if not detail:
        return None
    envelope = detail.get("envelope") or {}
    edge_id = detail.get("edgeId")
    edge = edge_detail(edge_id, packet_id) if edge_id in EDGE_SPECS else None
    grid_map = {item["batchId"]: item.get("status") for item in grid_batches()}
    grid_status = grid_map.get(detail.get("batchId"))
    return {
        "packetId": packet_id,
        "edgeId": ui_edge_label(edge_id or ""),
        "recordCount": detail.get("recordCount", 0),
        "windowStart": envelope.get("builtAt") or detail.get("updatedAt"),
        "windowEnd": envelope.get("builtAt") or detail.get("updatedAt"),
        "createdAt": detail.get("updatedAt"),
        "batchId": detail.get("batchId"),
        "signature": (edge or {}).get("signature"),
        "payloadHash": (edge or {}).get("payloadHash") or detail.get("envelopeDigest"),
        "merkleRoot": detail.get("rootHash") or envelope.get("rootHash"),
        "status": vpp_batch_status(detail.get("status", ""), grid_status),
        "chainStatus": detail.get("status"),
        "chainMode": "FISCO-BCOS",
        "txHash": detail.get("txHash"),
        "blockNumber": detail.get("blockNumber"),
        "latestVerification": {"status": "PASS" if grid_status == "VERIFIED" else ("FAIL" if grid_status == "REJECTED" else "PENDING")},
        "simulatedSummary": {"merkleRoot": detail.get("rootHash") or envelope.get("rootHash")},
    }


def grid_detail(packet_id: str):
    batch_id = batch_id_from_packet(packet_id)
    item = grid_batch(batch_id)
    if not item:
        return None
    edge_id = item.get("edgeId")
    edge = edge_detail(edge_id, packet_id) if edge_id in EDGE_SPECS else None
    checks = item.get("checks") or {}
    check_items = [
        {
            "name": name,
            "status": "PASS" if value is True or value == "PASS" else ("FAIL" if value is False or value == "FAIL" else "NOT_CHECKED"),
            "message": str(value),
            "code": "",
        }
        for name, value in checks.items()
    ]
    status = "PASS" if item.get("status") == "VERIFIED" else ("FAIL" if item.get("status") == "REJECTED" else "PENDING")
    return {
        "packetId": packet_id,
        "batchId": batch_id,
        "vppId": item.get("vppId"),
        "edgeId": ui_edge_label(edge_id or ""),
        "recordCount": item.get("recordCount", 0),
        "localMerkleRoot": item.get("recomputedRoot") or item.get("chainRoot"),
        "chainMerkleRoot": item.get("chainRoot"),
        "txHash": item.get("txHash"),
        "blockNumber": item.get("blockNumber"),
        "createdAt": item.get("createdAt"),
        "updatedAt": item.get("updatedAt"),
        "verificationStatus": status,
        "signature": (edge or {}).get("signature"),
        "locator": {"adapter": "fisco-bcos", "txHash": item.get("txHash"), "blockNumber": item.get("blockNumber")},
        "verification": {
            "checks": check_items,
            "observer": "GRID (FISCO BCOS)",
            "ruleVersion": "MERKLE-V1/CANON-JSON-V1",
            "method": "独立读链 getBatch + 离线重算 Merkle 根 + 逐条证明验证",
            "computedRoot": item.get("recomputedRoot"),
            "realBlockchain": True,
            "locator": {"adapter": "fisco-bcos", "txHash": item.get("txHash"), "blockNumber": item.get("blockNumber")},
        },
    }


def edge_events_for(edge_id: str, packet_id: str) -> dict:
    detail = edge_packet(edge_id, packet_id) or {}
    packet = detail.get("packet") or {}
    delivery = detail.get("delivery") or {}
    events = [
        {
            "id": f"{packet_id}-sealed",
            "occurredAt": detail.get("createdAt") or packet.get("collectedAt"),
            "eventType": "BATCH_SEALED_SIGNED",
            "objectId": packet_id,
            "source": "edge",
            "details": {"packetId": packet_id, "digest": detail.get("digest"), "signature": (detail.get("signature") or {}).get("value")},
        }
    ]
    if delivery.get("at"):
        events.append(
            {
                "id": f"{packet_id}-delivery",
                "occurredAt": delivery.get("at"),
                "eventType": "DELIVERY_ATTEMPT",
                "objectId": packet_id,
                "source": "edge",
                "details": {"httpStatus": delivery.get("httpStatus"), "error": delivery.get("error")},
            }
        )
    return {"items": events, "total": len(events), "page": 1, "pageSize": 25}


def packet_records_for(source: str, packet_id: str) -> list:
    if source in UI_EDGES:
        edge_id = UI_EDGES[source]
        records = packet_records(edge_id, packet_id)
        return [
            {
                "recordId": record["recordId"],
                "packetId": packet_id,
                "edgeId": ui_edge_label(edge_id),
                "vppId": vpp_for_edge(edge_id),
                "deviceId": record["deviceId"],
                "timestamp": record["timestamp"],
                "value": record["value"],
                "unit": record["unit"],
                "record": record,
            }
            for record in records
        ]
    # vpp / grid: locate the VPP that owns this packet
    for vpp_id in VPP_SPECS:
        detail = vpp_packet(vpp_id, packet_id)
        if detail:
            records = detail.get("records") or []
            return [
                {
                    "recordId": record["recordId"],
                    "packetId": packet_id,
                    "edgeId": detail.get("edgeId"),
                    "vppId": vpp_id,
                    "deviceId": record["deviceId"],
                    "timestamp": record["timestamp"],
                    "value": record["value"],
                    "unit": record["unit"],
                    "record": record,
                }
                for record in records
            ]
    return []


# --- ledger ---------------------------------------------------------------
def ledger(packet_id: str) -> dict:
    vpp_id = None
    vpp_payload = None
    for candidate in VPP_SPECS:
        payload = vpp_packet(candidate, packet_id)
        if payload:
            vpp_id = candidate
            vpp_payload = payload
            break
    if not vpp_payload:
        return None
    edge_id = vpp_payload.get("edgeId")
    batch_id = vpp_payload.get("batchId")
    envelope = vpp_payload.get("envelope") or {}
    records = envelope.get("records") or []
    proofs = envelope.get("proofs") or []
    edge = edge_detail(edge_id, packet_id) if edge_id in EDGE_SPECS else {}

    tx_hash = vpp_payload.get("txHash")
    receipt = rpc("getTransactionReceipt", [1, tx_hash]) if tx_hash else None
    tx = rpc("getTransactionByHash", [1, tx_hash]) if tx_hash else None
    block_number = (receipt or {}).get("blockNumber")
    block = rpc("getBlockByNumber", [1, block_number, False]) if block_number else None
    chain_read = grid_chain(batch_id) if batch_id else None
    grid = grid_detail(packet_id) if batch_id else None

    leaves = []
    for record in records:
        leaves.append({"recordId": record["recordId"], "leafHash": leaf_hash(record, canonical_bytes).hex()})
    tree = build_tree(records, canonical_bytes) if records else None

    edge_spec = EDGE_SPECS.get(edge_id, {})
    vpp_spec = VPP_SPECS.get(vpp_id, {})
    return {
        "packetId": packet_id,
        "batchId": batch_id,
        "edgeId": ui_edge_label(edge_id or ""),
        "vppId": vpp_id,
        "generatedAt": iso_now(),
        "generatorMode": "FISCO-BCOS",
        "certificates": {
            "edge": {
                "keyId": edge_spec.get("keyId"),
                "algorithm": "ECDSA-secp256k1-SHA256",
                "publicKey": edge_public_key(edge_id) if edge_id in EDGE_SPECS else "",
                "signature": ((edge or {}).get("signature") or "").split(":", 1)[-1],
                "payloadHash": (edge or {}).get("payloadHash"),
            },
            "vpp": {
                "vppId": vpp_id,
                "keyId": vpp_spec.get("keyId"),
                "account": vpp_account(vpp_id) if vpp_id in VPP_SPECS else "",
            },
            "contract": {"address": contract_address(CONFIG), "chainId": CONFIG["chain"].get("chainId", 1), "groupId": CONFIG["chain"].get("groupId", 1)},
        },
        "chain": {
            "record": (chain_read or {}).get("chain") or {},
            "queriedVia": (chain_read or {}).get("queriedVia"),
            "transaction": {
                "hash": tx_hash,
                "from": (tx or {}).get("from"),
                "to": (tx or {}).get("to"),
                "input": (tx or {}).get("input"),
                "blockLimit": (tx or {}).get("blockLimit"),
                "chainId": (tx or {}).get("chainId"),
                "groupId": (tx or {}).get("groupId"),
                "nonce": (tx or {}).get("nonce"),
            },
            "receipt": receipt or {},
            "block": {
                "number": (block or {}).get("number"),
                "hash": (block or {}).get("hash"),
                "parentHash": (block or {}).get("parentHash"),
                "stateRoot": (block or {}).get("stateRoot"),
                "transactionsRoot": (block or {}).get("transactionsRoot"),
                "receiptsRoot": (block or {}).get("receiptsRoot"),
                "sealer": (block or {}).get("sealer"),
                "timestamp": (block or {}).get("timestamp"),
            },
        },
        "hashes": {
            "packetDigest": (edge or {}).get("payloadHash"),
            "merkleRoot": envelope.get("rootHash") or vpp_payload.get("rootHash"),
            "recomputedRoot": (grid or {}).get("localMerkleRoot") if grid else (tree.root_hex if tree else None),
            "txHash": tx_hash,
            "blockHash": (block or {}).get("hash"),
            "stateRoot": (block or {}).get("stateRoot"),
            "receiptsRoot": (block or {}).get("receiptsRoot"),
            "leaves": leaves,
        },
        "proofs": proofs,
        "checks": ((grid or {}).get("verification") or {}).get("checks", []),
        "verificationStatus": (grid or {}).get("verificationStatus"),
    }


# --- Flask app ------------------------------------------------------------
def create_app() -> Flask:
    app = Flask(SERVICE, static_folder=None)
    app.secret_key = os.urandom(32)
    app.config["JSON_AS_ASCII"] = False

    users = {
        os.environ.get("VPP_USERNAME", "vpp"): (os.environ.get("VPP_PASSWORD", "vpp-local-password"), "vpp"),
        os.environ.get("GRID_USERNAME", "grid"): (os.environ.get("GRID_PASSWORD", "grid-local-password"), "grid"),
    }

    def current_user():
        return session.get("user")

    def require_user():
        user = current_user()
        if not user:
            return None
        return user

    def unauthorized():
        return jsonify({"error": {"code": "UNAUTHENTICATED", "message": "请先登录", "retryable": False}}), 401

    def not_found(message: str):
        return jsonify({"error": {"code": "NOT_FOUND", "message": message, "retryable": False}}), 404

    @app.get("/healthz")
    def healthz():
        return jsonify({"service": "fisco-console", "status": "ok", "chain": "FISCO-BCOS"})

    @app.post("/api/auth/login")
    def login():
        body = request.get_json(silent=True) or {}
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        entry = users.get(username)
        if not entry or entry[0] != password:
            return jsonify({"error": {"code": "INVALID_CREDENTIALS", "message": "用户名或密码错误", "retryable": False}}), 401
        role = entry[1]
        session["user"] = {"username": username, "role": role}
        return jsonify({"username": username, "role": role, "expiresIn": 28800})

    @app.post("/api/auth/logout")
    def logout():
        session.pop("user", None)
        return jsonify({"ok": True})

    @app.get("/api/auth/me")
    def me():
        user = require_user()
        if not user:
            return unauthorized()
        return jsonify({"user": user, "role": user["role"], "username": user["username"]})

    @app.before_request
    def _guard():
        if not request.path.startswith("/api/"):
            return None
        if request.path in ("/api/auth/login", "/api/auth/me", "/api/auth/logout"):
            return None
        if not current_user():
            return unauthorized()
        return None

    @app.get("/api/overview")
    def overview():
        return jsonify(
            {
                "checkedAt": iso_now(),
                "edge": {"available": True, "data": edge_overview("EDGE-001")},
                "edge2": {"available": True, "data": edge_overview("EDGE-002")},
                "vpp": vpp_overview(),
                "grid": grid_overview(),
            }
        )

    @app.get("/api/edge/devices")
    def edge_devices():
        return jsonify({"items": device_items("EDGE-001")})

    @app.get("/api/edge2/devices")
    def edge2_devices():
        return jsonify({"items": device_items("EDGE-002")})

    @app.get("/api/edge/runs")
    def edge_runs():
        collection = edge_collection("edge-01")
        items = []
        if collection.get("runId"):
            items.append(
                {
                    "runId": collection["runId"],
                    "kind": "CONTINUOUS",
                    "generatedRecords": len(edge_packets("edge-01")) * 10 + collection.get("buffered", 0),
                    "createdAt": collection.get("lastRoundAt") or iso_now(),
                    "endedAt": None if collection.get("running") else iso_now(),
                }
            )
        return jsonify(paged(items))

    @app.get("/api/edge/events")
    def edge_events():
        items = []
        for packet in edge_packets("edge-01")[-50:]:
            detail = edge_packet("edge-01", packet["packetId"]) or {}
            items.extend((edge_events_for("edge-01", packet["packetId"]) or {}).get("items", []))
        items.sort(key=lambda item: item.get("occurredAt") or "", reverse=True)
        return jsonify(paged(items))

    @app.get("/api/<source>/batches")
    def batches(source: str):
        page, size = page_args()
        ui_edge = ui_edge_for_source(source)
        if ui_edge:
            items = edge_batch_rows(UI_EDGES[ui_edge])
        elif source in ("vpp",) or source in VPP_SPECS:
            items = vpp_batch_rows(vpp_for_source(source))
        elif source == "grid":
            items = grid_batch_rows()
        else:
            return not_found("unknown source")
        filters = request.args
        if filters.get("packetId"):
            items = [item for item in items if filters["packetId"] in item["packetId"]]
        if filters.get("status"):
            items = [item for item in items if item["status"] == filters["status"]]
        if filters.get("deviceId"):
            wanted = filters["deviceId"]
            kept = []
            for item in items:
                records = packet_records_for(source if source in UI_EDGES else "vpp", item["packetId"])
                if any(record["deviceId"] == wanted for record in records):
                    kept.append(item)
            items = kept
        result = paged(items, page, size)
        if ui_edge:
            vpp_id = vpp_for_edge(UI_EDGES[ui_edge])
            for item in result["items"]:
                if not item.get("vppMerkleRoot"):
                    detail = vpp_packet(vpp_id, item["packetId"])
                    if detail:
                        item["vppMerkleRoot"] = detail.get("rootHash")
                        item["merkleRootSource"] = "VPP"
        return jsonify(result)

    @app.get("/api/<source>/batches/<packet_id>")
    def batch_detail(source: str, packet_id: str):
        ui_edge = ui_edge_for_source(source)
        if ui_edge:
            detail = edge_detail(UI_EDGES[ui_edge], packet_id)
        elif source in ("vpp",) or source in VPP_SPECS:
            detail = vpp_detail(vpp_for_source(source), packet_id)
        elif source == "grid":
            detail = grid_detail(packet_id)
        else:
            return not_found("unknown source")
        if not detail:
            return not_found(f"batch {packet_id} not found")
        return jsonify(detail)

    @app.get("/api/<source>/batches/<packet_id>/records")
    def batch_records(source: str, packet_id: str):
        return jsonify(paged(packet_records_for(source, packet_id)))

    @app.get("/api/<source>/batches/<packet_id>/raw")
    def batch_raw(source: str, packet_id: str):
        ui_edge = ui_edge_for_source(source)
        if ui_edge:
            detail = edge_packet(UI_EDGES[ui_edge], packet_id)
            if not detail:
                return not_found("raw packet not found")
            return jsonify(detail)
        for vpp_id in VPP_SPECS:
            detail = vpp_packet(vpp_id, packet_id)
            if detail:
                return jsonify({"packet": (detail.get("envelope") or {}).get("records"), "envelope": detail.get("envelope"), "proofs": detail.get("proofs")})
        return not_found("raw packet not found")

    @app.get("/api/<source>/batches/<packet_id>/verifications")
    def batch_verifications(source: str, packet_id: str):
        if source == "grid":
            detail = grid_detail(packet_id)
        else:
            detail = vpp_detail(UI_PICK_VPP_FOR(packet_id), packet_id)
        if not detail:
            return jsonify(paged([]))
        verification = (detail.get("verification") if source == "grid" else None) or {}
        rows = []
        if source == "grid" and verification:
            rows.append(
                {
                    "id": f"{packet_id}-verification",
                    "status": detail.get("verificationStatus"),
                    "checkedAt": detail.get("updatedAt") or iso_now(),
                    "observer": verification.get("observer"),
                    "ruleVersion": verification.get("ruleVersion"),
                    "method": verification.get("method"),
                    "result": verification,
                }
            )
        elif source != "grid":
            grid = grid_detail(packet_id)
            if grid and grid.get("verification"):
                rows.append(
                    {
                        "id": f"{packet_id}-verification",
                        "status": grid.get("verificationStatus"),
                        "checkedAt": iso_now(),
                        "observer": grid["verification"].get("observer"),
                        "ruleVersion": grid["verification"].get("ruleVersion"),
                        "method": grid["verification"].get("method"),
                        "result": grid["verification"],
                    }
                )
        return jsonify(paged(rows))

    @app.get("/api/<source>/events")
    def events(source: str):
        object_id = request.args.get("objectId")
        edge_id = UI_EDGES.get(ui_edge_for_source(source) or "EDGE-001", "edge-01")
        packets = edge_packets(edge_id)
        if object_id:
            packets = [item for item in packets if item["packetId"] == object_id]
        items = []
        for packet in packets[-100:]:
            items.extend((edge_events_for(edge_id, packet["packetId"]) or {}).get("items", []))
        return jsonify(paged(items))

    @app.get("/api/vpp/workbench/overview")
    def vpp_workbench():
        rows = vpp_batch_rows(UI_VPP)
        return jsonify(
            {
                "chainMode": "FISCO-BCOS",
                "edgeRoster": [edge_roster("EDGE-001"), edge_roster("EDGE-002")],
                "batches": paged(rows, 1, 25),
            }
        )

    @app.get("/api/grid/overview")
    def grid_overview_api():
        data = grid_overview()["data"]
        return jsonify(data)

    @app.get("/api/grid/batches")
    def grid_batches_api():
        page, size = page_args()
        items = grid_batch_rows()
        for key, field in (("vppId", "vppId"), ("edgeId", "edgeId"), ("packetId", "packetId")):
            value = request.args.get(key)
            if value:
                items = [item for item in items if value in str(item.get(field) or "")]
        return jsonify(paged(items, page, size))

    @app.get("/api/grid/batches/<packet_id>")
    def grid_batch_api(packet_id: str):
        detail = grid_detail(packet_id)
        if not detail:
            return not_found("grid batch not found")
        return jsonify(detail)

    @app.get("/api/grid/batches/<packet_id>/records")
    def grid_records_of_batch(packet_id: str):
        return jsonify(paged(packet_records_for("grid", packet_id)))

    @app.get("/api/grid/batches/<packet_id>/verifications")
    def grid_verifications(packet_id: str):
        detail = grid_detail(packet_id)
        rows = []
        if detail and detail.get("verification"):
            rows.append(
                {
                    "id": f"{packet_id}-verification",
                    "status": detail.get("verificationStatus"),
                    "checkedAt": iso_now(),
                    "observer": detail["verification"].get("observer"),
                    "ruleVersion": detail["verification"].get("ruleVersion"),
                    "method": detail["verification"].get("method"),
                    "result": detail["verification"],
                }
            )
        return jsonify(paged(rows))

    @app.get("/api/grid/records")
    def grid_records():
        page, size = page_args()
        items = []
        for vpp_id in VPP_SPECS:
            for row in vpp_packets(vpp_id)[:80]:
                detail = vpp_packet(vpp_id, row["packetId"])
                if not detail:
                    continue
                for record in detail.get("records") or []:
                    items.append(
                        {
                            "recordId": record["recordId"],
                            "packetId": row["packetId"],
                            "edgeId": ui_edge_label(detail.get("edgeId") or ""),
                            "vppId": vpp_id,
                            "deviceId": record["deviceId"],
                            "timestamp": record["timestamp"],
                            "value": record["value"],
                            "unit": record["unit"],
                            "record": record,
                        }
                    )
        packet_filter = request.args.get("packetId")
        if packet_filter:
            items = [item for item in items if packet_filter in item["packetId"]]
        device_filter = request.args.get("deviceId")
        if device_filter:
            items = [item for item in items if item["deviceId"] == device_filter]
        return jsonify(paged(items, page, size))

    @app.post("/api/<ui_edge>/simulation/<operation>")
    def simulation(ui_edge: str, operation: str):
        edge_id = edge_for_ui(ui_edge) or UI_EDGES.get(ui_edge_for_source(ui_edge) or "")
        if not edge_id:
            return not_found("unknown edge")
        if operation == "sample-once":
            status, body = _post(f"{edge_url(edge_id)}/api/v1/collection/tick", timeout=30)
            if status is None:
                return jsonify({"error": {"code": "EDGE_UNAVAILABLE", "message": "edge unavailable", "retryable": True}}), 503
            return jsonify({"status": "SINGLE_ROUND_GENERATED", "generatedRecords": body.get("records", 0), "runId": body.get("runId")})
        if operation in ("start", "pause"):
            status, body = _post(f"{edge_url(edge_id)}/api/v1/collection/{operation}", timeout=15)
            if status is None:
                return jsonify({"error": {"code": "EDGE_UNAVAILABLE", "message": "edge unavailable", "retryable": True}}), 503
            return jsonify({"status": "SAMPLING_STARTED" if operation == "start" else "SAMPLING_PAUSED", "running": body.get("running"), "runId": body.get("runId")})
        return not_found("unknown operation")

    @app.post("/api/<source>/batches/<packet_id>/retry")
    def retry_batch(source: str, packet_id: str):
        edge_id = UI_EDGES.get(ui_edge_for_source(source) or "EDGE-001", "edge-01")
        status, body = _post(f"{edge_url(edge_id)}/api/v1/packets/{packet_id}/retry", timeout=30)
        return jsonify({"status": "DELIVERY_ATTEMPT", "result": body, "httpStatus": status})

    def _verify(packet_id: str):
        for vpp_id in VPP_SPECS:
            detail = vpp_packet(vpp_id, packet_id)
            if detail and detail.get("envelope"):
                status, body = _post(f"{GRID_URL}/api/v1/verify", {"envelope": detail["envelope"]}, timeout=60)
                if status is None:
                    return jsonify({"error": {"code": "GRID_UNAVAILABLE", "message": "grid unavailable", "retryable": True}}), 503
                return jsonify(
                    {
                        "status": "PASS" if body.get("status") == "VERIFIED" else "FAIL",
                        "checks": (body.get("checks") or {}),
                        "computedRoot": body.get("recomputedRootHash"),
                        "chainRoot": body.get("chainRootHash"),
                    }
                )
        return not_found("packet not found")

    @app.post("/api/vpp/batches/<packet_id>/verify")
    def vpp_verify(packet_id: str):
        return _verify(packet_id)

    @app.post("/api/grid/batches/<packet_id>/verify")
    def grid_verify(packet_id: str):
        return _verify(packet_id)

    @app.get("/api/ledger/<packet_id>")
    def ledger_api(packet_id: str):
        data = ledger(packet_id)
        if not data:
            return not_found("packet not found")
        return jsonify(data)

    # --- static SPA -------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(DIST_DIR, "index.html")

    @app.get("/assets/<path:filename>")
    def assets(filename: str):
        return send_from_directory(os.path.join(DIST_DIR, "assets"), filename)

    return app


def UI_PICK_VPP_FOR(packet_id: str) -> str:
    for vpp_id in VPP_SPECS:
        if vpp_packet(vpp_id, packet_id):
            return vpp_id
    return UI_VPP


app = create_app()


if __name__ == "__main__":
    if CONFIG.get('pipeline', {}).get('version') == 2:
        from src.pipeline.console import create_app as create_v2_console
        app = create_v2_console(CONFIG)
    app.run(
        host=os.environ.get("CONSOLE_HOST", "127.0.0.1"),
        port=int(os.environ.get("CONSOLE_PORT", "8400")),
        debug=os.environ.get("CONSOLE_DEBUG") == "1",
        threaded=True,
    )
