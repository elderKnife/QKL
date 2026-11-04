"""Edge node service (upstream collection + downlink command execution).

No blockchain client is imported here on purpose: edge <-> VPP is a direct
signed HTTP link.  The edge:

* simulates its 10 terminal devices (stand-in for the future A1 adapter),
* collects one reading per device every ``intervalSeconds`` (default 10s),
  aggregates a packet, signs it and posts it to its VPP,
* accepts signed GRID commands forwarded by the VPP, verifies them, applies a
  simulated device action and returns a signed acknowledgement.

Start/pause of the collection loop is controlled through HTTP.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from typing import Any

import requests
from flask import Flask, jsonify, request

from ..common.canonical import canonical_bytes, canonical_sha256
from ..common.config import load_config, resolve_path
from ..common.crypto import Signer, verify_bytes
from ..common.http import error_response, ok, read_json
from ..common.models import (
    ACK_SCHEMA,
    PACKET_SCHEMA,
    ProtocolError,
    new_id,
    parse_iso_ms,
    utc_iso,
    utc_now_ms,
    validate_command,
    validate_record,
)

SERVICE = "edge"
MAX_STORED_COMMANDS = 200
MAX_STORED_PACKETS = 200


class EdgeRuntime:
    def __init__(self, config: dict, spec: dict):
        self.config = config
        self.spec = spec
        self.edge_id = spec["edgeId"]
        self.vpp_url = spec["vppUrl"].rstrip("/")
        self.records_per_packet = int(spec.get("recordsPerPacket", 10))
        self.interval = int(spec.get("intervalSeconds", config.get("edge", {}).get("defaultIntervalSeconds", 10)))
        self.max_buffer = int(config.get("edge", {}).get("maxBufferRecords", 400))
        self.devices = {item["deviceId"]: item["deviceType"] for item in spec["devices"]}
        self.signer = Signer.from_file(resolve_path(spec["signingKey"]), spec["keyId"])
        self.trusted_grid = spec["trustedGrid"]
        self.lock = threading.RLock()
        self.buffer: list[dict] = []
        self.seen_record_ids: set[str] = set()
        self.packets: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.device_state = {device_id: {"on": True, "voltageV": 220.0} for device_id in self.devices}
        self.collection_running = False
        self.collection_thread: threading.Thread | None = None
        self.collection_rounds = 0
        self.last_round_at: str | None = None
        self.last_packet_id: str | None = None
        self.run_id: str | None = None
        self.data_dir = resolve_path(os.path.join("data", self.edge_id))
        os.makedirs(self.data_dir, exist_ok=True)
        self._load_state()

    # -- persistence -------------------------------------------------------
    @property
    def _state_path(self) -> str:
        return os.path.join(self.data_dir, "edge-state.json")

    def _load_state(self) -> None:
        if not os.path.exists(self._state_path):
            return
        with open(self._state_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        self.packets = {item["packet"]["packetId"]: item for item in state.get("packets", [])}
        self.seen_record_ids = set(state.get("seenRecordIds", []))
        self.buffer = state.get("buffer", [])
        self.commands = state.get("commands", {})
        for device_id, value in (state.get("deviceState") or {}).items():
            if device_id in self.device_state and isinstance(value, dict):
                self.device_state[device_id].update(
                    {"on": bool(value.get("on", True)), "voltageV": float(value.get("voltageV", 220.0))}
                )

    def _save_state(self) -> None:
        state = {
            "edgeId": self.edge_id,
            "buffer": self.buffer,
            "seenRecordIds": sorted(self.seen_record_ids),
            "packets": list(self.packets.values())[-MAX_STORED_PACKETS:],
            "commands": dict(list(self.commands.items())[-MAX_STORED_COMMANDS:]),
            "deviceState": self.device_state,
            "updatedAt": utc_iso(),
        }
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self._state_path)

    # -- ingest ------------------------------------------------------------
    def ingest(self, records: list[Any]) -> dict:
        accepted: list[str] = []
        rejected: list[dict] = []
        with self.lock:
            if len(self.buffer) + len(records) > self.max_buffer:
                raise ProtocolError("INVALID_SCHEMA", f"edge buffer would exceed {self.max_buffer} records")
            for raw in records:
                try:
                    record = validate_record(raw)
                    if record["deviceId"] not in self.devices:
                        raise ProtocolError(
                            "TARGET_MISMATCH",
                            f"device {record['deviceId']} does not belong to {self.edge_id}",
                            object_id=record["deviceId"],
                        )
                    expected_type = self.devices[record["deviceId"]]
                    if record["deviceType"] != expected_type:
                        raise ProtocolError(
                            "INVALID_SCHEMA",
                            f"deviceType mismatch for {record['deviceId']}: expected {expected_type}",
                            object_id=record["deviceId"],
                        )
                    if record["recordId"] in self.seen_record_ids:
                        raise ProtocolError("DUPLICATE_ID", f"recordId {record['recordId']} already ingested",
                                            object_id=record["recordId"])
                    self.seen_record_ids.add(record["recordId"])
                    self.buffer.append(record)
                    accepted.append(record["recordId"])
                except ProtocolError as exc:
                    rejected.append({"recordId": raw.get("recordId") if isinstance(raw, dict) else None,
                                     "code": exc.code, "message": exc.message})
            packets = []
            while len(self.buffer) >= self.records_per_packet:
                packets.append(self._flush_locked())
            self._save_state()

        return {
            "edgeId": self.edge_id,
            "accepted": len(accepted),
            "acceptedRecordIds": accepted,
            "rejected": rejected,
            "buffered": len(self.buffer),
            "packets": packets,
        }

    # -- packet building / delivery ---------------------------------------
    def _next_packet(self) -> dict:
        batch = self.buffer[: self.records_per_packet]
        del self.buffer[: self.records_per_packet]
        ordered = sorted(batch, key=lambda r: (r["deviceId"], r["timestamp"], r["recordId"]))
        return {
            "schemaVersion": PACKET_SCHEMA,
            "packetId": new_id("PKT"),
            "edgeId": self.edge_id,
            "collectedAt": utc_iso(),
            "records": ordered,
        }

    def _flush_locked(self) -> dict:
        packet = self._next_packet()
        packet_bytes = canonical_bytes(packet)
        signature = {
            "algorithm": "ECDSA-secp256k1-SHA256",
            "ruleVersion": "BUSINESS-SIG-V1",
            "keyId": self.signer.key_id,
            "value": self.signer.sign_bytes(packet_bytes),
        }
        entry = {
            "packet": packet,
            "signature": signature,
            "digest": hashlib.sha256(packet_bytes).hexdigest(),
            "status": "BUILT",
            "delivery": None,
            "createdAt": utc_iso(),
        }
        self.packets[packet["packetId"]] = entry
        self.last_packet_id = packet["packetId"]
        self._deliver(packet["packetId"])
        return {
            "packetId": packet["packetId"],
            "status": self.packets[packet["packetId"]]["status"],
            "delivery": self.packets[packet["packetId"]]["delivery"],
            "recordCount": len(packet["records"]),
        }

    def _deliver(self, packet_id: str) -> None:
        entry = self.packets[packet_id]
        body = {"packet": entry["packet"], "signature": entry["signature"]}
        try:
            response = requests.post(f"{self.vpp_url}/api/v1/edge-packets", json=body, timeout=30)
            entry["delivery"] = {
                "httpStatus": response.status_code,
                "response": _safe_json(response),
                "at": utc_iso(),
            }
            if response.status_code < 300:
                entry["status"] = "DELIVERED"
            elif response.status_code == 409:
                entry["status"] = "DUPLICATE_AT_VPP"
            else:
                entry["status"] = "DELIVERY_FAILED"
        except requests.RequestException as exc:
            entry["delivery"] = {"httpStatus": None, "error": str(exc), "at": utc_iso()}
            entry["status"] = "DELIVERY_FAILED"

    # -- automatic collection ---------------------------------------------
    def collection_state(self) -> dict:
        with self.lock:
            return {
                "edgeId": self.edge_id,
                "running": self.collection_running,
                "intervalSeconds": self.interval,
                "deviceCount": len(self.devices),
                "rounds": self.collection_rounds,
                "lastRoundAt": self.last_round_at,
                "lastPacketId": self.last_packet_id,
                "runId": self.run_id,
                "buffered": len(self.buffer),
            }

    def start_collection(self) -> dict:
        with self.lock:
            if self.collection_running:
                return self.collection_state()
            self.collection_running = True
            self.run_id = "RUN-AUTO-" + time.strftime("%H%M%S")
            self.collection_thread = threading.Thread(
                target=self._collection_loop, name=f"collect-{self.edge_id}", daemon=True
            )
            self.collection_thread.start()
            return self.collection_state()

    def pause_collection(self) -> dict:
        with self.lock:
            self.collection_running = False
        return self.collection_state()

    def _collection_loop(self) -> None:
        while True:
            with self.lock:
                if not self.collection_running:
                    return
            try:
                self.collect_once()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                print(f"[{self.edge_id}] collection round failed: {exc}", flush=True)
            deadline = time.time() + self.interval
            while time.time() < deadline:
                with self.lock:
                    if not self.collection_running:
                        return
                time.sleep(0.2)

    def collect_once(self) -> dict:
        with self.lock:
            self.collection_rounds += 1
            seq = self.collection_rounds
            run_id = self.run_id or ("RUN-AUTO-" + time.strftime("%H%M%S"))
            self.run_id = run_id
            moment = utc_iso()
            records = []
            for device_id, device_type in self.devices.items():
                state = self.device_state[device_id]
                records.append(
                    {
                        "recordId": f"REC-{run_id}-{device_id}-{seq:05d}",
                        "deviceId": device_id,
                        "deviceType": device_type,
                        "metric": "active_power_kw",
                        "value": self._simulate(device_type, state, device_id, seq),
                        "unit": "kW",
                        "timestamp": moment,
                    }
                )
            result = self.ingest(records)
            self.last_round_at = moment
            packets = result.get("packets") or []
            if packets:
                self.last_packet_id = packets[-1]["packetId"]
            return {"round": seq, "records": len(records), "packets": packets}

    def _simulate(self, device_type: str, state: dict, device_id: str, seq: int) -> float:
        if not state["on"]:
            return 0.0
        rng = random.Random(f"{self.run_id}:{seq}:{device_id}")
        scale = max(0.0, min(1.2, state.get("voltageV", 220.0) / 220.0))
        if device_type == "pv":
            value = rng.uniform(5.0, 45.0) * scale
        elif device_type == "storage":
            value = rng.uniform(-12.0, 12.0)
        else:
            value = rng.uniform(0.0, 30.0) * scale
        return round(value, 3)

    # -- downlink commands -------------------------------------------------
    def handle_command(self, body: Any) -> dict:
        if not isinstance(body, dict) or "command" not in body or "signature" not in body:
            raise ProtocolError("INVALID_SCHEMA", "body must contain command and signature")
        command = validate_command(body["command"])
        signature = body["signature"]
        if not isinstance(signature, dict):
            raise ProtocolError("INVALID_SCHEMA", "signature must be an object")
        if signature.get("keyId") != self.trusted_grid["keyId"]:
            raise ProtocolError("IDENTITY_NOT_ALLOWED", "command keyId is not the registered grid key",
                                object_id=command["commandId"])
        sig_ok, reason = verify_bytes(canonical_bytes(command), signature.get("value", ""),
                                      self.trusted_grid["publicKey"])
        if not sig_ok:
            raise ProtocolError("SIGNATURE_INVALID", f"grid command signature rejected: {reason}",
                                object_id=command["commandId"])
        if command["targetEdgeId"] != self.edge_id:
            raise ProtocolError("TARGET_MISMATCH", f"command targets {command['targetEdgeId']}",
                                object_id=command["commandId"])
        if command["targetDeviceId"] not in self.devices:
            raise ProtocolError("TARGET_MISMATCH", f"device {command['targetDeviceId']} does not belong here",
                                object_id=command["commandId"])
        if parse_iso_ms(command["expiresAt"]) < utc_now_ms():
            raise ProtocolError("EXPIRED", "command has expired", object_id=command["commandId"])

        digest = canonical_sha256(command)
        with self.lock:
            existing = self.commands.get(command["commandId"])
            if existing:
                if existing["digest"] != digest:
                    raise ProtocolError("IDEMPOTENCY_CONFLICT",
                                        "same commandId with different content", object_id=command["commandId"])
                return {"ack": existing["ack"], "ackSignature": existing["ackSignature"],
                        "deviceState": self.device_state[command["targetDeviceId"]], "duplicate": True}

            state = self.device_state[command["targetDeviceId"]]
            if command["action"] == "power_on":
                state["on"] = True
            elif command["action"] == "power_off":
                state["on"] = False
            else:
                state["voltageV"] = command["params"]["voltageV"]
            ack = {
                "schemaVersion": ACK_SCHEMA,
                "ackId": new_id("ACK"),
                "commandId": command["commandId"],
                "commandDigest": digest,
                "edgeId": self.edge_id,
                "deviceId": command["targetDeviceId"],
                "action": command["action"],
                "result": "APPLIED",
                "appliedState": {"on": state["on"], "voltageV": state["voltageV"]},
                "executedAt": utc_iso(),
            }
            ack_signature = {
                "algorithm": "ECDSA-secp256k1-SHA256",
                "ruleVersion": "BUSINESS-SIG-V1",
                "keyId": self.signer.key_id,
                "value": self.signer.sign_bytes(canonical_bytes(ack)),
            }
            self.commands[command["commandId"]] = {
                "digest": digest,
                "ack": ack,
                "ackSignature": ack_signature,
            }
            self._save_state()

        delivery = self._send_ack(ack, ack_signature, command, body.get("chainAnchor") or {})
        return {
            "ack": ack,
            "ackSignature": ack_signature,
            "deviceState": self.device_state[command["targetDeviceId"]],
            "delivery": delivery,
        }

    def _send_ack(self, ack: dict, signature: dict, command: dict, chain_anchor: dict) -> dict:
        try:
            response = requests.post(
                f"{self.vpp_url}/api/v1/acks",
                json={"ack": ack, "signature": signature, "command": command, "chainAnchor": chain_anchor},
                timeout=60,
            )
            return {"httpStatus": response.status_code, "response": _safe_json(response)}
        except requests.RequestException as exc:
            return {"httpStatus": None, "error": str(exc)}

    # -- state -------------------------------------------------------------
    def flush(self) -> dict:
        with self.lock:
            if not self.buffer:
                return {"packetId": None, "status": "NOTHING_TO_FLUSH", "buffered": 0}
            result = self._flush_locked()
            self._save_state()
            return result

    def retry_delivery(self, packet_id: str) -> dict:
        with self.lock:
            if packet_id not in self.packets:
                raise ProtocolError("NOT_FOUND", f"unknown packetId {packet_id}", object_id=packet_id)
            self._deliver(packet_id)
            self._save_state()
            return {"packetId": packet_id, "status": self.packets[packet_id]["status"],
                    "delivery": self.packets[packet_id]["delivery"]}

    def state(self) -> dict:
        with self.lock:
            return {
                "edgeId": self.edge_id,
                "vppId": self.spec["vppId"],
                "keyId": self.spec["keyId"],
                "publicKey": self.signer.public_key_hex,
                "devices": sorted(self.devices),
                "deviceState": self.device_state,
                "buffered": len(self.buffer),
                "packetCount": len(self.packets),
                "recordsPerPacket": self.records_per_packet,
                "collection": self.collection_state(),
                "blockchain": "NOT USED BY EDGE",
            }


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:500]


def create_app(config_path: str | None = None, edge_id: str | None = None) -> Flask:
    config = load_config(config_path)
    specs = {item["edgeId"]: item for item in config["edges"]}
    edge_id = edge_id or os.environ.get("EDGE_ID")
    if edge_id not in specs:
        raise SystemExit(f"EDGE_ID must be one of {sorted(specs)}")
    runtime = EdgeRuntime(config, specs[edge_id])

    app = Flask(SERVICE)
    app.config["RUNTIME"] = runtime

    @app.errorhandler(ProtocolError)
    def _handle_protocol_error(error: ProtocolError):
        return error_response(error)

    @app.get("/healthz")
    def healthz():
        return jsonify({"service": SERVICE, "edgeId": runtime.edge_id, "ok": True,
                        "blockchain": "NOT USED BY EDGE"})

    @app.get("/api/v1/state")
    def state():
        return jsonify(runtime.state())

    @app.get("/api/v1/collection")
    def collection():
        return jsonify(runtime.collection_state())

    @app.post("/api/v1/collection/start")
    def collection_start():
        return ok(runtime.start_collection())

    @app.post("/api/v1/collection/pause")
    def collection_pause():
        return ok(runtime.pause_collection())

    @app.post("/api/v1/collection/tick")
    def collection_tick():
        return ok(runtime.collect_once())

    @app.post("/api/v1/records")
    def ingest_records():
        body = read_json(request)
        records = body.get("records") if isinstance(body, dict) and "records" in body else body
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list) or not records:
            raise ProtocolError("INVALID_SCHEMA", "body must be a record or {\"records\": [...]}")
        result = runtime.ingest(records)
        status = 409 if result["rejected"] else 202
        return ok(result, status=status)

    @app.post("/api/v1/flush")
    def flush():
        return ok(runtime.flush())

    @app.post("/api/v1/commands")
    def command():
        result = runtime.handle_command(read_json(request))
        return ok(result, status=200 if result.get("duplicate") else 201)

    @app.post("/api/v1/packets/<packet_id>/retry")
    def retry(packet_id: str):
        return ok(runtime.retry_delivery(packet_id))

    @app.get("/api/v1/packets")
    def packets():
        return jsonify({"packets": [
            {"packetId": pid, "status": item["status"], "delivery": item["delivery"],
             "recordCount": len(item["packet"]["records"]), "createdAt": item["createdAt"]}
            for pid, item in runtime.packets.items()
        ]})

    @app.get("/api/v1/packets/<packet_id>")
    def packet(packet_id: str):
        item = runtime.packets.get(packet_id)
        if item is None:
            raise ProtocolError("NOT_FOUND", f"unknown packetId {packet_id}", object_id=packet_id)
        return jsonify(item)

    return app


def main() -> None:
    app = create_app()
    runtime: EdgeRuntime = app.config["RUNTIME"]
    if runtime.spec.get("autoStart"):
        runtime.start_collection()
    app.run(host="127.0.0.1", port=runtime.spec["port"], threaded=True)


if __name__ == "__main__":
    main()
