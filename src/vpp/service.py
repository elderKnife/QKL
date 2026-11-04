"""Virtual power plant (VPP) service.

Responsibilities for the minimal validation experiment:

1. receive an edge packet over plain HTTP (no blockchain on this hop),
2. verify the edge signature against the **pre-registered** key for that edge,
3. build the MERKLE-V1 tree and all per-record proofs,
4. write *only* the Merkle root (+ locating metadata) to FISCO BCOS,
5. forward the off-chain batch + proofs + chain receipt to the grid platform.

Nothing but the root hash and metadata is ever put on chain; the grid platform
re-reads the root through its own chain client before trusting the batch.
"""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Any

import requests
from flask import Flask, jsonify, request

from ..chain.factory import build_registry
from ..chain.registry import ChainBatch
from ..common.canonical import canonical_bytes, canonical_sha256
from ..common.config import load_config, resolve_path
from ..common.crypto import Signer, verify_bytes
from ..common.http import error_response, ok, read_json
from ..common.merkle import build_tree, object_root_hex
from ..common.models import (
    ACK_ENVELOPE_SCHEMA,
    BATCH_SCHEMA,
    ProtocolError,
    ack_batch_id,
    batch_id_for,
    canonical_packet_bytes,
    command_batch_id,
    utc_iso,
    utc_now_ms,
    validate_ack,
    validate_command,
    validate_packet,
)
from ..common.store import Database

SERVICE = "vpp"

SCHEMA = """
CREATE TABLE IF NOT EXISTS packets (
    packetId TEXT PRIMARY KEY,
    edgeId TEXT NOT NULL,
    digest TEXT NOT NULL,
    recordCount INTEGER NOT NULL,
    rootHash TEXT,
    batchId TEXT,
    txHash TEXT,
    blockNumber INTEGER,
    status TEXT NOT NULL,
    reason TEXT,
    envelope TEXT,
    gridStatus TEXT,
    createdAt TEXT NOT NULL,
    updatedAt TEXT NOT NULL
);
"""


class VppRuntime:
    def __init__(self, config: dict, spec: dict):
        self.config = config
        self.spec = spec
        self.vpp_id = spec["vppId"]
        self.grid_url = spec["gridUrl"].rstrip("/")
        self.signer = Signer.from_file(resolve_path(spec["signingKey"]), spec["keyId"])
        self.trusted_edges = spec["trustedEdges"]
        self.trusted_grid = spec["trustedGrid"]
        self.edge_urls = {
            edge["edgeId"]: f"http://127.0.0.1:{edge['port']}"
            for edge in config["edges"]
        }
        self.lock = threading.Lock()
        self.client, self.registry = build_registry(config, _role_for(config, self.vpp_id))
        self.db = Database(
            resolve_path(os.path.join("data", self.vpp_id, "vpp.db")),
            SCHEMA,
        )

    # -- verification ------------------------------------------------------
    def verify_edge_packet(self, packet: dict, signature: dict) -> str:
        edge_id = packet.get("edgeId")
        trusted = self.trusted_edges.get(edge_id)
        if trusted is None:
            raise ProtocolError("IDENTITY_NOT_ALLOWED", f"edge {edge_id} is not registered with {self.vpp_id}",
                                object_id=str(edge_id))
        key_id = signature.get("keyId")
        if key_id != trusted["keyId"]:
            raise ProtocolError("IDENTITY_NOT_ALLOWED",
                                f"keyId {key_id!r} is not the registered key for {edge_id}",
                                object_id=str(edge_id))
        payload = canonical_packet_bytes(packet)
        sig_ok, reason = verify_bytes(payload, signature.get("value", ""), trusted["publicKey"])
        if not sig_ok:
            raise ProtocolError("SIGNATURE_INVALID", f"edge packet signature rejected: {reason}",
                                object_id=packet.get("packetId"))
        return hashlib.sha256(payload).hexdigest()

    # -- main flow ---------------------------------------------------------
    def handle_packet(self, body: Any) -> dict:
        if not isinstance(body, dict) or "packet" not in body or "signature" not in body:
            raise ProtocolError("INVALID_SCHEMA", "body must contain packet and signature")
        raw_packet = body["packet"]
        signature = body["signature"]
        if not isinstance(signature, dict):
            raise ProtocolError("INVALID_SCHEMA", "signature must be an object")
        packet = validate_packet(raw_packet)
        digest = self.verify_edge_packet(packet, signature)
        packet_id = packet["packetId"]

        existing = self.db.query_one("SELECT * FROM packets WHERE packetId = ?", (packet_id,))
        if existing:
            if existing["digest"] != digest:
                raise ProtocolError("IDEMPOTENCY_CONFLICT",
                                    f"packetId {packet_id} already seen with different content",
                                    object_id=packet_id)
            return self.snapshot(packet_id) | {"duplicate": True}

        now = utc_iso()
        self.db.execute(
            "INSERT INTO packets (packetId, edgeId, digest, recordCount, status, createdAt, updatedAt)"
            " VALUES (?,?,?,?,?,?,?)",
            (packet_id, packet["edgeId"], digest, len(packet["records"]), "RECEIVED", now, now),
        )

        with self.lock:
            tree = build_tree(packet["records"], canonical_bytes)
            root_hex = tree.root_hex
            batch_id = batch_id_for(packet_id)
            envelope = {
                "schemaVersion": BATCH_SCHEMA,
                "batchId": batch_id,
                "vppId": self.vpp_id,
                "edgeId": packet["edgeId"],
                "packetId": packet_id,
                "recordCount": len(packet["records"]),
                "rootHash": root_hex,
                "builtAt": utc_iso(),
                "merkleRuleVersion": tree.rule_version,
                "canonicalRuleVersion": "CANON-JSON-V1",
                "records": packet["records"],
                "proofs": tree.proofs(batch_id),
            }
            self.db.execute(
                "UPDATE packets SET rootHash=?, batchId=?, status=?, envelope=?, updatedAt=? WHERE packetId=?",
                (root_hex, batch_id, "MERKLE_BUILT", Database.dumps({"packet": packet, "envelope": envelope}),
                 utc_iso(), packet_id),
            )

            chain_state = self._submit_root(batch_id, root_hex, envelope)
            self.db.execute(
                "UPDATE packets SET status=?, txHash=?, blockNumber=?, reason=?, updatedAt=? WHERE packetId=?",
                (chain_state["status"], chain_state.get("txHash"), chain_state.get("blockNumber"),
                 chain_state.get("reason"), utc_iso(), packet_id),
            )

            if chain_state.get("txHash"):
                envelope["chainReceipt"] = {
                    "txHash": chain_state["txHash"],
                    "blockNumber": chain_state.get("blockNumber"),
                    "registryAddress": self.registry.address,
                    "status": chain_state["status"],
                }
                self.db.execute(
                    "UPDATE packets SET envelope=?, updatedAt=? WHERE packetId=?",
                    (Database.dumps({"packet": packet, "envelope": envelope}), utc_iso(), packet_id),
                )

            grid_state = self._forward_to_grid(envelope)
            self.db.execute(
                "UPDATE packets SET gridStatus=?, status=?, updatedAt=? WHERE packetId=?",
                (grid_state["status"], "FORWARDED" if grid_state["status"] == "ACCEPTED" else "FORWARD_FAILED",
                 utc_iso(), packet_id),
            )

        return self.snapshot(packet_id)

    def _submit_root(self, batch_id: str, root_hex: str, envelope: dict) -> dict:
        try:
            result = self.registry.register_batch(
                batch_id=batch_id,
                root_hex=root_hex,
                record_count=envelope["recordCount"],
                submitted_at=utc_now_ms(),
                vpp_id=self.vpp_id,
                schema_version="MERKLE-V1/CANON-JSON-V1",
            )
        except ProtocolError as exc:
            return {"status": "CHAIN_UNAVAILABLE", "reason": f"{exc.code}: {exc.message}"}

        if result.ok:
            return {"status": "CONFIRMED", "txHash": result.tx_hash, "blockNumber": result.block_number}

        # A duplicate revert can be the honest outcome of a retry whose first
        # transaction did land.  Only accept it when the on-chain root matches.
        recovered = self._recover_from_chain(batch_id, root_hex)
        if recovered:
            return recovered
        return {
            "status": "FAILED",
            "reason": f"registerBatch reverted: {result.output}",
            "txHash": result.tx_hash,
            "blockNumber": result.block_number,
        }

    def _recover_from_chain(self, batch_id: str, root_hex: str) -> dict | None:
        try:
            chain_batch: ChainBatch | None = self.registry.get_batch(batch_id)
        except ProtocolError:
            return {"status": "UNKNOWN", "reason": "transaction outcome unknown; chain query failed"}
        if chain_batch is None:
            return None
        if chain_batch.root == root_hex and chain_batch.vpp_id == self.vpp_id:
            return {"status": "CONFIRMED", "txHash": None, "blockNumber": None,
                    "reason": "recovered: batch already anchored with identical root"}
        return None

    def _forward_to_grid(self, envelope: dict) -> dict:
        signature = {
            "algorithm": "ECDSA-secp256k1-SHA256",
            "ruleVersion": "BUSINESS-SIG-V1",
            "keyId": self.spec["keyId"],
            "value": self.signer.sign_bytes(canonical_bytes(envelope)),
        }
        body = {"vppId": self.vpp_id, "envelope": envelope, "signature": signature}
        try:
            response = requests.post(f"{self.grid_url}/api/v1/batches", json=body, timeout=60)
            payload = response.json() if response.content else {}
            return {
                "status": "ACCEPTED" if response.status_code < 300 else "REJECTED",
                "httpStatus": response.status_code,
                "response": payload,
            }
        except requests.RequestException as exc:
            return {"status": "UNREACHABLE", "httpStatus": None, "error": str(exc)}


    # -- downlink: GRID command forwarding ---------------------------------
    def handle_command(self, body: Any) -> dict:
        if not isinstance(body, dict) or "command" not in body or "signature" not in body:
            raise ProtocolError("INVALID_SCHEMA", "body must contain command, signature and chainAnchor")
        command = validate_command(body["command"])
        signature = body["signature"]
        if signature.get("keyId") != self.trusted_grid["keyId"]:
            raise ProtocolError("IDENTITY_NOT_ALLOWED", "command is not signed by the registered grid key",
                                object_id=command["commandId"])
        ok, reason = verify_bytes(canonical_bytes(command), signature.get("value", ""),
                                  self.trusted_grid["publicKey"])
        if not ok:
            raise ProtocolError("SIGNATURE_INVALID", f"grid command signature rejected: {reason}",
                                object_id=command["commandId"])
        target_edge = command["targetEdgeId"]
        if target_edge not in self.trusted_edges:
            raise ProtocolError("TARGET_MISMATCH", f"edge {target_edge} is not served by {self.vpp_id}",
                                object_id=command["commandId"])

        # The command must be anchored by GRID before it may reach a device.
        expected_root = object_root_hex(command, canonical_bytes)
        anchor = body.get("chainAnchor") or {}
        anchor_id = anchor.get("batchId") or command_batch_id(command["commandId"])
        chain_batch: ChainBatch | None = self.registry.get_batch(anchor_id)
        if chain_batch is None:
            raise ProtocolError("ROOT_MISMATCH", f"command {command['commandId']} is not anchored on chain",
                                object_id=command["commandId"])
        if chain_batch.root != expected_root:
            raise ProtocolError("ROOT_MISMATCH", "on-chain command root does not match the command",
                                object_id=command["commandId"])

        try:
            response = requests.post(
                f"{self.edge_urls[target_edge]}/api/v1/commands",
                json={"command": command, "signature": signature,
                      "chainAnchor": {"batchId": anchor_id, "rootHash": expected_root,
                                      "txHash": anchor.get("txHash"), "blockNumber": anchor.get("blockNumber")}},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise ProtocolError("CHAIN_UNAVAILABLE", f"edge {target_edge} unreachable: {exc}", retryable=True) from exc
        payload = _safe_json(response)
        if response.status_code >= 400:
            error = (payload or {}).get("error") or {}
            raise ProtocolError(error.get("code", "TARGET_MISMATCH"),
                                error.get("message", "edge rejected command"),
                                object_id=command["commandId"])
        return {
            "commandId": command["commandId"],
            "edgeId": target_edge,
            "vppId": self.vpp_id,
            "status": "ACKNOWLEDGED",
            "edge": payload,
        }

    # -- downlink: anchor edge acknowledgement and forward to GRID --------
    def handle_ack(self, body: Any) -> dict:
        if not isinstance(body, dict) or "ack" not in body or "signature" not in body:
            raise ProtocolError("INVALID_SCHEMA", "body must contain ack and signature")
        ack = validate_ack(body["ack"])
        signature = body["signature"]
        trusted = self.trusted_edges.get(ack["edgeId"])
        if trusted is None:
            raise ProtocolError("IDENTITY_NOT_ALLOWED", f"edge {ack['edgeId']} is not registered",
                                object_id=ack["ackId"])
        if signature.get("keyId") != trusted["keyId"]:
            raise ProtocolError("IDENTITY_NOT_ALLOWED", "ack keyId does not match the edge",
                                object_id=ack["ackId"])
        ok, reason = verify_bytes(canonical_bytes(ack), signature.get("value", ""), trusted["publicKey"])
        if not ok:
            raise ProtocolError("SIGNATURE_INVALID", f"edge ack signature rejected: {reason}",
                                object_id=ack["ackId"])

        command = body.get("command")
        if isinstance(command, dict):
            validate_command(command)
            if canonical_sha256(command) != ack["commandDigest"]:
                raise ProtocolError("ROOT_MISMATCH", "ack.commandDigest does not match the command",
                                    object_id=ack["ackId"])

        ack_root = object_root_hex(ack, canonical_bytes)
        batch_id = ack_batch_id(ack["ackId"])
        try:
            result = self.registry.register_batch(
                batch_id=batch_id,
                root_hex=ack_root,
                record_count=1,
                submitted_at=utc_now_ms(),
                vpp_id=self.vpp_id,
                schema_version="EDGE-ACK-V1",
            )
        except ProtocolError as exc:
            raise ProtocolError("CHAIN_UNAVAILABLE", f"ack anchor failed: {exc.message}", retryable=True) from exc

        if not result.ok:
            recovered = self.registry.get_batch(batch_id)
            if recovered is None or recovered.root != ack_root:
                raise ProtocolError("CHAIN_UNAVAILABLE", "ack anchor reverted", retryable=True)
            tx_hash, block_number = None, None
        else:
            tx_hash, block_number = result.tx_hash, result.block_number

        envelope = {
            "schemaVersion": ACK_ENVELOPE_SCHEMA,
            "vppId": self.vpp_id,
            "ack": ack,
            "command": command,
            "edgeSignature": signature,
            "chainReceipt": {
                "txHash": tx_hash,
                "blockNumber": block_number,
                "registryAddress": self.registry.address,
                "status": "CONFIRMED",
            },
            "builtAt": utc_iso(),
        }
        envelope_signature = {
            "algorithm": "ECDSA-secp256k1-SHA256",
            "ruleVersion": "BUSINESS-SIG-V1",
            "keyId": self.spec["keyId"],
            "value": self.signer.sign_bytes(canonical_bytes(envelope)),
        }
        try:
            response = requests.post(
                f"{self.grid_url}/api/v1/acks",
                json={"vppId": self.vpp_id, "envelope": envelope, "signature": envelope_signature},
                timeout=60,
            )
            grid_payload = _safe_json(response)
            grid_status = "ACCEPTED" if response.status_code < 300 else "REJECTED"
        except requests.RequestException as exc:
            grid_payload = {"error": str(exc)}
            grid_status = "UNREACHABLE"

        return {
            "ackId": ack["ackId"],
            "commandId": ack["commandId"],
            "status": "ANCHORED",
            "txHash": tx_hash,
            "blockNumber": block_number,
            "gridStatus": grid_status,
            "grid": grid_payload,
        }

    # -- read models -------------------------------------------------------
    def snapshot(self, packet_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM packets WHERE packetId = ?", (packet_id,))
        if row is None:
            raise ProtocolError("NOT_FOUND", f"unknown packetId {packet_id}", object_id=packet_id)
        stored = Database.loads(row.get("envelope"), {})
        envelope = stored.get("envelope", {})
        return {
            "packetId": row["packetId"],
            "edgeId": row["edgeId"],
            "batchId": row["batchId"],
            "recordCount": row["recordCount"],
            "rootHash": row["rootHash"],
            "status": row["status"],
            "txHash": row["txHash"],
            "blockNumber": row["blockNumber"],
            "reason": row["reason"],
            "gridStatus": row["gridStatus"],
            "chain": {"txHash": row["txHash"], "blockNumber": row["blockNumber"]},
            "envelopeDigest": row["digest"],
            "updatedAt": row["updatedAt"],
            "envelope": envelope,
            "records": envelope.get("records", []),
            "proofs": envelope.get("proofs", []),
        }

    def state(self) -> dict:
        rows = self.db.query_all("SELECT status, COUNT(*) AS n FROM packets GROUP BY status")
        return {
            "vppId": self.vpp_id,
            "address": self.client.address(),
            "rpcUrl": self.client.rpc_url,
            "chainBlockNumber": self.client.block_number(),
            "trustedEdges": sorted(self.trusted_edges),
            "packetsByStatus": {row["status"]: row["n"] for row in rows},
        }


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:500]}


def _role_for(config: dict, vpp_id: str) -> str:
    mapping = {item["vppId"]: item["chainAccount"] for item in config["vpps"]}
    return mapping[vpp_id]


def create_app(config_path: str | None = None, vpp_id: str | None = None) -> Flask:
    config = load_config(config_path)
    specs = {item["vppId"]: item for item in config["vpps"]}
    vpp_id = vpp_id or os.environ.get("VPP_ID")
    if vpp_id not in specs:
        raise SystemExit(f"VPP_ID must be one of {sorted(specs)}")
    runtime = VppRuntime(config, specs[vpp_id])

    app = Flask(SERVICE)
    app.config["RUNTIME"] = runtime

    @app.errorhandler(ProtocolError)
    def _handle_protocol_error(error: ProtocolError):
        return error_response(error)

    @app.get("/healthz")
    def healthz():
        return jsonify({"service": SERVICE, "vppId": runtime.vpp_id, "ok": True})

    @app.get("/api/v1/state")
    def state():
        return jsonify(runtime.state())

    @app.post("/api/v1/edge-packets")
    def receive_packet():
        result = runtime.handle_packet(read_json(request))
        status = 200 if result.get("duplicate") else 201
        return ok(result, status=status)

    @app.get("/api/v1/packets/<packet_id>")
    def packet(packet_id: str):
        return jsonify(runtime.snapshot(packet_id))

    @app.post("/api/v1/commands")
    def receive_command():
        return ok(runtime.handle_command(read_json(request)))

    @app.post("/api/v1/acks")
    def receive_ack():
        return ok(runtime.handle_ack(read_json(request)))

    @app.get("/api/v1/packets")
    def packets():
        rows = runtime.db.query_all(
            "SELECT packetId, edgeId, batchId, status, gridStatus, txHash, blockNumber, recordCount, updatedAt"
            " FROM packets ORDER BY createdAt DESC LIMIT 100"
        )
        return jsonify({"packets": rows})

    return app


def main() -> None:
    app = create_app()
    runtime: VppRuntime = app.config["RUNTIME"]
    app.run(host="127.0.0.1", port=runtime.spec["port"], threaded=True)


if __name__ == "__main__":
    main()
