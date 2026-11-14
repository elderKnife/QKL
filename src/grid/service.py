"""State grid platform (GRID) service — independent verifier.

The grid platform does **not** trust what a VPP claims.  For every batch it:

1. verifies the VPP business signature with the pre-registered public key,
2. re-reads the Merkle root from FISCO BCOS through its **own** chain client,
3. recomputes the root from the off-chain records,
4. verifies every per-record proof against the on-chain root,
5. optionally re-checks the transaction receipt (status / target contract).

Only then is the batch marked VERIFIED.  Tampered off-chain rows are reported
per record, never as a single batch-level "failure".
"""
from __future__ import annotations

import os
import threading
import uuid

import requests
from typing import Any

from flask import Flask, jsonify, request

from ..chain.factory import build_registry
from ..chain.registry import ChainBatch
from ..common.canonical import canonical_bytes, canonical_sha256
from ..common.config import load_config, resolve_path
from ..common.crypto import Signer, verify_bytes
from ..common.http import error_response, ok, read_json
from ..common.merkle import audit_batch, build_tree, object_root_hex
from ..common.models import (
    ACK_ENVELOPE_SCHEMA,
    COMMAND_SCHEMA,
    ProtocolError,
    ack_batch_id,
    command_batch_id,
    new_id,
    utc_iso,
    utc_now_ms,
    validate_ack_envelope,
    validate_batch_envelope,
    validate_command,
)
from ..common.store import Database

SERVICE = "grid"

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batchId TEXT PRIMARY KEY,
    vppId TEXT NOT NULL,
    edgeId TEXT,
    packetId TEXT,
    declaredRoot TEXT,
    recomputedRoot TEXT,
    chainRoot TEXT,
    recordCount INTEGER,
    status TEXT NOT NULL,
    checks TEXT,
    failures TEXT,
    txHash TEXT,
    blockNumber INTEGER,
    createdAt TEXT NOT NULL,
    updatedAt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    commandId TEXT PRIMARY KEY,
    gridId TEXT NOT NULL,
    targetEdgeId TEXT NOT NULL,
    targetDeviceId TEXT NOT NULL,
    action TEXT NOT NULL,
    params TEXT,
    status TEXT NOT NULL,
    command TEXT,
    txHash TEXT,
    blockNumber INTEGER,
    vppId TEXT,
    vppStatus TEXT,
    vppResponse TEXT,
    ack TEXT,
    ackStatus TEXT,
    ackTxHash TEXT,
    ackBlockNumber INTEGER,
    createdAt TEXT NOT NULL,
    updatedAt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS acks (
    ackId TEXT PRIMARY KEY,
    commandId TEXT NOT NULL,
    edgeId TEXT,
    deviceId TEXT,
    status TEXT NOT NULL,
    root TEXT,
    chainRoot TEXT,
    txHash TEXT,
    blockNumber INTEGER,
    envelope TEXT,
    createdAt TEXT NOT NULL
);
"""


class GridRuntime:
    def __init__(self, config: dict, spec: dict):
        self.config = config
        self.spec = spec
        self.trusted_vpps = spec["trustedVpps"]
        self.signer = Signer.from_file(resolve_path(spec["signingKey"]), spec["keyId"])
        self.lock = threading.Lock()
        self.client, self.registry = build_registry(config, "grid", rpc_node=spec.get("rpcNode"))
        self.edge_to_vpp = {edge["edgeId"]: edge["vppId"] for edge in config["edges"]}
        self.vpp_urls = {vpp["vppId"]: f"http://127.0.0.1:{vpp['port']}" for vpp in config["vpps"]}
        self.command_ttl = int(spec.get("commandTtlSeconds", 120))
        self.edge_devices = {
            edge["edgeId"]: {device["deviceId"] for device in edge["devices"]}
            for edge in config["edges"]
        }
        self.db = Database(resolve_path(os.path.join("data", "grid", "grid.db")), SCHEMA)

    # -- verification core --------------------------------------------------
    def verify_signature(self, vpp_id: str, envelope: dict, signature: dict) -> None:
        trusted = self.trusted_vpps.get(vpp_id)
        if trusted is None:
            raise ProtocolError("IDENTITY_NOT_ALLOWED", f"vpp {vpp_id} is not a trusted submitter",
                                object_id=vpp_id)
        if signature.get("keyId") != trusted["keyId"]:
            raise ProtocolError("IDENTITY_NOT_ALLOWED",
                                f"keyId {signature.get('keyId')!r} does not match {vpp_id}",
                                object_id=vpp_id)
        sig_ok, reason = verify_bytes(canonical_bytes(envelope), signature.get("value", ""), trusted["publicKey"])
        if not sig_ok:
            raise ProtocolError("SIGNATURE_INVALID", f"vpp batch signature rejected: {reason}",
                                object_id=envelope.get("batchId"))

    def verify_batch(self, vpp_id: str, envelope: dict) -> dict:
        validate_batch_envelope(envelope)
        batch_id = envelope["batchId"]
        records = envelope["records"]
        proofs = envelope["proofs"]
        declared_root = envelope["rootHash"]

        tree = build_tree(records, canonical_bytes)
        recomputed_root = tree.root_hex

        chain_batch: ChainBatch | None = self.registry.get_batch(batch_id)
        chain_root = chain_batch.root if chain_batch else None

        checks: dict[str, Any] = {
            "schema": "PASS",
            "recordCountMatches": envelope["recordCount"] == len(records),
            "declaredRootMatchesRecords": declared_root == recomputed_root,
            "chainRecordExists": chain_batch is not None,
            "chainRootMatchesDeclared": chain_root is not None and chain_root == declared_root,
            "chainVppMatches": chain_batch is not None and chain_batch.vpp_id == envelope["vppId"],
            "chainRecordCountMatches": chain_batch is not None and chain_batch.record_count == envelope["recordCount"],
        }

        receipt_check = self._check_receipt(envelope)
        checks["chainReceipt"] = receipt_check["status"]

        proof_report: dict[str, Any] | None = None
        if chain_root is not None:
            proof_report = audit_batch(records, proofs, bytes.fromhex(chain_root), canonical_bytes)
            checks["proofsVerified"] = proof_report["ok"]

        failures: list[dict] = []
        for name, value in checks.items():
            if value is False:
                failures.append({"recordId": None, "reason": name})
        if proof_report:
            failures.extend(proof_report["failures"])

        verified = (
            checks["recordCountMatches"]
            and checks["declaredRootMatchesRecords"]
            and checks["chainRecordExists"]
            and checks["chainRootMatchesDeclared"]
            and checks["chainVppMatches"]
            and checks["chainRecordCountMatches"]
            and checks.get("proofsVerified") is True
            and receipt_check["status"] in ("PASS", "NOT_CHECKED")
        )

        return {
            "batchId": batch_id,
            "vppId": envelope["vppId"],
            "edgeId": envelope.get("edgeId"),
            "packetId": envelope.get("packetId"),
            "recordCount": len(records),
            "declaredRootHash": declared_root,
            "recomputedRootHash": recomputed_root,
            "chainRootHash": chain_root,
            "status": "VERIFIED" if verified else "REJECTED",
            "checks": checks,
            "failures": failures,
            "chainReceipt": receipt_check,
            "chainBatch": chain_batch.to_dict() if chain_batch else None,
            "verifiedAt": utc_iso(),
        }

    def _check_receipt(self, envelope: dict) -> dict:
        receipt = envelope.get("chainReceipt")
        if not isinstance(receipt, dict) or not receipt.get("txHash"):
            return {"status": "NOT_CHECKED", "reason": "no chain receipt supplied"}
        try:
            raw = self.client.get_receipt(receipt["txHash"])
        except ProtocolError as exc:
            return {"status": "ERROR", "reason": f"receipt query failed: {exc.message}"}
        if not raw or not raw.get("transactionHash"):
            return {"status": "FAIL", "reason": "receipt not found on chain"}
        status = int(raw.get("status", "0x0"), 16) if isinstance(raw.get("status"), str) else int(raw.get("status") or 0)
        to_address = str(raw.get("to", "")).lower()
        expected = self.registry.address.lower()
        block_number = int(raw.get("blockNumber", "0x0"), 16) if isinstance(raw.get("blockNumber"), str) else None
        ok_flag = status == 0 and to_address == expected
        return {
            "status": "PASS" if ok_flag else "FAIL",
            "txHash": raw.get("transactionHash"),
            "blockNumber": block_number,
            "contractAddress": raw.get("to"),
            "receiptStatus": status,
            "reason": None if ok_flag else "receipt status or target contract mismatch",
        }


    # -- downlink commands -------------------------------------------------
    def create_command(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise ProtocolError("INVALID_SCHEMA", "body must be an object")
        target_edge = body.get("targetEdgeId")
        target_device = body.get("targetDeviceId")
        if target_edge not in self.edge_to_vpp:
            raise ProtocolError("TARGET_MISMATCH", f"unknown edge {target_edge!r}")
        if target_device not in self.edge_devices.get(target_edge, set()):
            raise ProtocolError("TARGET_MISMATCH", f"device {target_device!r} does not belong to {target_edge}")
        vpp_id = self.edge_to_vpp[target_edge]

        now_ms = utc_now_ms()
        command = validate_command(
            {
                "schemaVersion": COMMAND_SCHEMA,
                "commandId": new_id("CMD"),
                "gridId": self.spec.get("gridId", "grid"),
                "targetEdgeId": target_edge,
                "targetDeviceId": target_device,
                "action": body.get("action"),
                "params": body.get("params") or {},
                "issuedAt": utc_iso(now_ms),
                "expiresAt": utc_iso(now_ms + self.command_ttl * 1000),
                "nonce": uuid.uuid4().hex,
            }
        )
        signature = {
            "algorithm": "ECDSA-secp256k1-SHA256",
            "ruleVersion": "BUSINESS-SIG-V1",
            "keyId": self.spec["keyId"],
            "value": self.signer.sign_bytes(canonical_bytes(command)),
        }
        root = object_root_hex(command, canonical_bytes)
        batch_id = command_batch_id(command["commandId"])
        chain_result = self.registry.register_batch(
            batch_id=batch_id,
            root_hex=root,
            record_count=1,
            submitted_at=now_ms,
            vpp_id="grid",
            schema_version="GRID-COMMAND-V1",
        )
        if not chain_result.ok:
            existing = self.registry.get_batch(batch_id)
            if existing is None or existing.root != root:
                raise ProtocolError("CHAIN_UNAVAILABLE", "command anchoring failed", retryable=True,
                                    object_id=command["commandId"])
            tx_hash, block_number = chain_result.tx_hash, chain_result.block_number
        else:
            tx_hash, block_number = chain_result.tx_hash, chain_result.block_number

        now = utc_iso()
        self.db.execute(
            "INSERT INTO commands (commandId, gridId, targetEdgeId, targetDeviceId, action, params, status,"
            " command, txHash, blockNumber, vppId, createdAt, updatedAt)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                command["commandId"], command["gridId"], target_edge, target_device, command["action"],
                Database.dumps(command["params"]), "ANCHORED", Database.dumps(command),
                tx_hash, block_number, vpp_id, now, now,
            ),
        )

        anchor = {"batchId": batch_id, "rootHash": root, "txHash": tx_hash, "blockNumber": block_number}
        vpp_response: Any = None
        vpp_status = "UNREACHABLE"
        try:
            response = requests.post(
                f"{self.vpp_urls[vpp_id]}/api/v1/commands",
                json={"command": command, "signature": signature, "chainAnchor": anchor},
                timeout=120,
            )
            vpp_response = response.json() if response.content else {}
            vpp_status = "ACKNOWLEDGED" if response.status_code < 300 else "REJECTED"
        except requests.RequestException as exc:
            vpp_response = {"error": str(exc)}

        self.db.execute(
            "UPDATE commands SET vppId=?, vppStatus=?, vppResponse=?, updatedAt=?,"
            " status = CASE WHEN status = 'COMPLETED' THEN status ELSE ? END WHERE commandId=?",
            (vpp_id, vpp_status, Database.dumps(vpp_response), utc_iso(), vpp_status, command["commandId"]),
        )
        return self.get_command(command["commandId"]) | {"chainAction": chain_result.to_dict()}

    def handle_ack(self, body: Any) -> dict:
        if not isinstance(body, dict) or "envelope" not in body or "signature" not in body:
            raise ProtocolError("INVALID_SCHEMA", "body must contain vppId, envelope and signature")
        envelope = body["envelope"]
        vpp_id = body.get("vppId") or envelope.get("vppId")
        trusted = self.trusted_vpps.get(vpp_id)
        if trusted is None:
            raise ProtocolError("IDENTITY_NOT_ALLOWED", f"vpp {vpp_id!r} is not a trusted submitter")
        signature = body["signature"]
        if signature.get("keyId") != trusted["keyId"]:
            raise ProtocolError("IDENTITY_NOT_ALLOWED", "ack envelope keyId does not match the VPP")
        sig_ok, reason = verify_bytes(canonical_bytes(envelope), signature.get("value", ""),
                                      trusted["publicKey"])
        if not sig_ok:
            raise ProtocolError("SIGNATURE_INVALID", f"ack envelope signature rejected: {reason}")
        validate_ack_envelope(envelope)

        ack = envelope["ack"]
        command = envelope["command"]
        if canonical_sha256(command) != ack["commandDigest"]:
            raise ProtocolError("ROOT_MISMATCH", "ack.commandDigest does not match the command",
                                object_id=ack["ackId"])

        ack_root = object_root_hex(ack, canonical_bytes)
        ack_batch = self.registry.get_batch(ack_batch_id(ack["ackId"]))
        command_root = object_root_hex(command, canonical_bytes)
        command_batch = self.registry.get_batch(command_batch_id(command["commandId"]))

        checks = {
            "ackAnchored": ack_batch is not None,
            "ackRootMatches": ack_batch is not None and ack_batch.root == ack_root,
            "commandAnchored": command_batch is not None,
            "commandRootMatches": command_batch is not None and command_batch.root == command_root,
            "vppMatches": ack_batch is not None and ack_batch.vpp_id == vpp_id,
        }
        verified = all(checks.values())
        status = "VERIFIED" if verified else "REJECTED"

        self.db.execute(
            "INSERT INTO acks (ackId, commandId, edgeId, deviceId, status, root, chainRoot, txHash,"
            " blockNumber, envelope, createdAt) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(ackId) DO UPDATE SET status=excluded.status, chainRoot=excluded.chainRoot,"
            " txHash=excluded.txHash, blockNumber=excluded.blockNumber",
            (
                ack["ackId"], ack["commandId"], ack["edgeId"], ack["deviceId"], status, ack_root,
                ack_batch.root if ack_batch else None,
                (envelope.get("chainReceipt") or {}).get("txHash"),
                (envelope.get("chainReceipt") or {}).get("blockNumber"),
                Database.dumps(envelope), utc_iso(),
            ),
        )
        self.db.execute(
            "UPDATE commands SET status=?, ack=?, ackStatus=?, ackTxHash=?, ackBlockNumber=?, updatedAt=?"
            " WHERE commandId=?",
            (
                "COMPLETED" if verified else "REJECTED", Database.dumps(ack), status,
                (envelope.get("chainReceipt") or {}).get("txHash"),
                (envelope.get("chainReceipt") or {}).get("blockNumber"),
                utc_iso(), ack["commandId"],
            ),
        )
        return {
            "ackId": ack["ackId"],
            "commandId": ack["commandId"],
            "status": status,
            "checks": checks,
            "ackRootHash": ack_root,
            "chainRootHash": ack_batch.root if ack_batch else None,
            "commandRootHash": command_root,
            "chainReceipt": envelope.get("chainReceipt"),
            "ack": ack,
        }

    def get_command(self, command_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM commands WHERE commandId = ?", (command_id,))
        if row is None:
            raise ProtocolError("NOT_FOUND", f"unknown command {command_id}", object_id=command_id)
        for field in ("params", "command", "ack", "vppResponse"):
            row[field] = Database.loads(row.get(field), {} if field != "ack" else None)
        return row

    def list_commands(self, limit: int = 50) -> dict:
        rows = self.db.query_all(
            "SELECT commandId, targetEdgeId, targetDeviceId, action, params, status, txHash, blockNumber,"
            " vppId, vppStatus, ackStatus, ackTxHash, ackBlockNumber, createdAt, updatedAt"
            " FROM commands ORDER BY createdAt DESC LIMIT ?",
            (limit,),
        )
        for row in rows:
            row["params"] = Database.loads(row.get("params"), {})
        return {"commands": rows}

    def list_acks(self, limit: int = 50) -> dict:
        return {"acks": self.db.query_all(
            "SELECT ackId, commandId, edgeId, deviceId, status, root, chainRoot, txHash, blockNumber, createdAt"
            " FROM acks ORDER BY createdAt DESC LIMIT ?", (limit,))}

    # -- persistence -------------------------------------------------------
    def store(self, report: dict) -> None:
        now = utc_iso()
        self.db.execute(
            "INSERT INTO batches (batchId, vppId, edgeId, packetId, declaredRoot, recomputedRoot, chainRoot,"
            " recordCount, status, checks, failures, txHash, blockNumber, createdAt, updatedAt)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(batchId) DO UPDATE SET status=excluded.status, checks=excluded.checks,"
            " failures=excluded.failures, recomputedRoot=excluded.recomputedRoot, chainRoot=excluded.chainRoot,"
            " updatedAt=excluded.updatedAt",
            (
                report["batchId"],
                report["vppId"],
                report.get("edgeId"),
                report.get("packetId"),
                report["declaredRootHash"],
                report["recomputedRootHash"],
                report["chainRootHash"],
                report["recordCount"],
                report["status"],
                Database.dumps(report["checks"]),
                Database.dumps(report["failures"]),
                report["chainReceipt"].get("txHash"),
                report["chainReceipt"].get("blockNumber"),
                now,
                now,
            ),
        )

    def handle(self, body: Any) -> dict:
        if not isinstance(body, dict) or "envelope" not in body or "signature" not in body:
            raise ProtocolError("INVALID_SCHEMA", "body must contain envelope and signature")
        vpp_id = body.get("vppId") or body["envelope"].get("vppId")
        envelope = body["envelope"]
        self.verify_signature(vpp_id, envelope, body["signature"])
        with self.lock:
            report = self.verify_batch(vpp_id, envelope)
            self.store(report)
        return report

    def verify_stateless(self, body: Any) -> dict:
        """Verify a supplied batch without persisting it (tamper drill)."""
        if not isinstance(body, dict) or "envelope" not in body:
            raise ProtocolError("INVALID_SCHEMA", "body must contain envelope")
        envelope = body["envelope"]
        with self.lock:
            report = self.verify_batch(envelope.get("vppId", ""), envelope)
        return report

    def get_batch(self, batch_id: str) -> dict:
        row = self.db.query_one("SELECT * FROM batches WHERE batchId = ?", (batch_id,))
        if row is None:
            raise ProtocolError("NOT_FOUND", f"unknown batchId {batch_id}", object_id=batch_id)
        row["checks"] = Database.loads(row["checks"], {})
        row["failures"] = Database.loads(row["failures"], [])
        return row

    def state(self) -> dict:
        rows = self.db.query_all("SELECT status, COUNT(*) AS n FROM batches GROUP BY status")
        return {
            "grid": "state-grid-platform",
            "address": self.client.address(),
            "rpcUrl": self.client.rpc_url,
            "chainBlockNumber": self.client.block_number(),
            "registryAddress": self.registry.address,
            "trustedVpps": sorted(self.trusted_vpps),
            "batchesByStatus": {row["status"]: row["n"] for row in rows},
        }


def create_app(config_path: str | None = None) -> Flask:
    config = load_config(config_path)
    runtime = GridRuntime(config, config["grid"])

    app = Flask(SERVICE)
    app.config["RUNTIME"] = runtime

    @app.errorhandler(ProtocolError)
    def _handle_protocol_error(error: ProtocolError):
        return error_response(error)

    @app.get("/healthz")
    def healthz():
        return jsonify({"service": SERVICE, "ok": True})

    @app.get("/api/v1/state")
    def state():
        return jsonify(runtime.state())

    @app.post("/api/v1/batches")
    def receive_batch():
        report = runtime.handle(read_json(request))
        status = 201 if report["status"] == "VERIFIED" else 409
        return ok(report, status=status)

    @app.post("/api/v1/verify")
    def verify():
        report = runtime.verify_stateless(read_json(request))
        return ok(report)

    @app.get("/api/v1/batches")
    def batches():
        rows = runtime.db.query_all(
            "SELECT batchId, vppId, edgeId, status, recordCount, chainRoot, txHash, blockNumber, updatedAt"
            " FROM batches ORDER BY createdAt DESC LIMIT 100"
        )
        return jsonify({"batches": rows})

    @app.get("/api/v1/batches/count")
    def batches_count():
        total = runtime.db.query_one("SELECT COUNT(*) AS n FROM batches")["n"]
        passed = runtime.db.query_one("SELECT COUNT(*) AS n FROM batches WHERE status = 'VERIFIED'")["n"]
        failed = runtime.db.query_one("SELECT COUNT(*) AS n FROM batches WHERE status = 'REJECTED'")["n"]
        return jsonify({"total": total, "passed": passed, "failed": failed})

    @app.get("/api/v1/batches/<batch_id>")
    def batch(batch_id: str):
        return jsonify(runtime.get_batch(batch_id))


    @app.post("/api/v1/commands")
    def create_command():
        report = runtime.create_command(read_json(request))
        return ok(report, status=201)

    @app.get("/api/v1/commands")
    def commands():
        return jsonify(runtime.list_commands())

    @app.get("/api/v1/commands/<command_id>")
    def command(command_id: str):
        return jsonify(runtime.get_command(command_id))

    @app.post("/api/v1/acks")
    def ack():
        return ok(runtime.handle_ack(read_json(request)))

    @app.get("/api/v1/acks")
    def acks():
        return jsonify(runtime.list_acks())

    @app.get("/api/v1/chain/batches/<batch_id>")
    def chain_batch(batch_id: str):
        chain_batch_obj = runtime.registry.get_batch(batch_id)
        if chain_batch_obj is None:
            raise ProtocolError("NOT_FOUND", f"batch {batch_id} is not anchored on chain", object_id=batch_id)
        return jsonify({
            "batchId": batch_id,
            "chain": chain_batch_obj.to_dict(),
            "queriedVia": runtime.client.rpc_url,
            "queriedAt": utc_iso(),
            "blockNumberNow": runtime.client.block_number(),
        })

    return app


def main() -> None:
    if load_config().get('pipeline', {}).get('version') == 2:
        from ..pipeline.apps import grid_app
        app = grid_app(load_config())
        app.run(host='127.0.0.1', port=app.config['runtime'].spec['port'], threaded=True)
        return
    app = create_app()
    runtime: GridRuntime = app.config["RUNTIME"]
    app.run(host="127.0.0.1", port=runtime.spec["port"], threaded=True)


if __name__ == "__main__":
    main()
