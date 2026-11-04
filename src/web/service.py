"""Read-only aggregation service behind the visualization dashboard.

The dashboard never becomes part of the trusted path: it only *reads* the
existing services and the chain, then renders what really happened:

    edge packet + signature  ->  VPP verification / Merkle / chain tx
    ->  on-chain root         ->  GRID independent verification

Two operator actions are exposed for the demo (feed a round, run a tamper
drill); both call the same code paths used by the CLI scripts.
"""
from __future__ import annotations

import copy
import os
import sys
from datetime import datetime, timezone
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.common.canonical import canonical_bytes, canonical_sha256, canonical_text  # noqa: E402
from src.common.config import contract_address, load_config  # noqa: E402
from src.common.crypto import verify_bytes  # noqa: E402
from src.common.merkle import build_tree, freeze_order, leaf_hash  # noqa: E402
from src.common.models import ProtocolError  # noqa: E402

SERVICE = "web"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _hex_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return None


class WebRuntime:
    def __init__(self, config: dict):
        self.config = config
        self.edges = {item["edgeId"]: item for item in config["edges"]}
        self.vpps = {item["vppId"]: item for item in config["vpps"]}
        self.grid = config["grid"]
        self.rpc_url = config["chain"]["rpcNodes"][config["grid"]["rpcNode"]]
        try:
            self.contract = contract_address(config)
        except Exception:  # noqa: BLE001 - dashboard must still render
            self.contract = (config["chain"].get("contract") or {}).get("address") or ""
        self.edge_urls = {edge["edgeId"]: f"http://127.0.0.1:{edge['port']}" for edge in config["edges"]}
        self.vpp_urls = {vpp["vppId"]: f"http://127.0.0.1:{vpp['port']}" for vpp in config["vpps"]}

    # -- low level ---------------------------------------------------------
    def _get(self, url: str, timeout: float = 4.0) -> Any:
        try:
            response = requests.get(url, timeout=timeout)
            if response.status_code >= 400:
                return None
            return response.json()
        except (requests.RequestException, ValueError):
            return None

    def _post(self, url: str, payload: dict, timeout: float = 90.0) -> Any:
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            return {"httpStatus": response.status_code, "body": response.json() if response.content else None}
        except (requests.RequestException, ValueError) as exc:
            return {"httpStatus": None, "error": str(exc)}

    def _rpc(self, method: str, params: list, timeout: float = 4.0) -> Any:
        try:
            response = requests.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                timeout=timeout,
            )
            return response.json().get("result")
        except (requests.RequestException, ValueError):
            return None

    def _edge_url(self, edge_id: str) -> str:
        return f"http://127.0.0.1:{self.edges[edge_id]['port']}"

    def _vpp_url(self, vpp_id: str) -> str:
        return f"http://127.0.0.1:{self.vpps[vpp_id]['port']}"

    @property
    def _grid_url(self) -> str:
        return f"http://127.0.0.1:{self.grid['port']}"

    # -- overview ----------------------------------------------------------
    def chain_snapshot(self) -> dict:
        block = self._rpc("getBlockNumber", [1])
        sealers = self._rpc("getSealerList", [1]) or []
        peers = self._rpc("getGroupPeers", [1]) or []
        pbft = self._rpc("getPbftView", [1])
        return {
            "rpcUrl": self.rpc_url,
            "blockNumber": _hex_int(block),
            "sealerCount": len(sealers),
            "peerCount": len(peers),
            "pbftView": _hex_int(pbft),
            "sealers": sealers,
            "online": block is not None,
        }

    def services_health(self) -> dict:
        result: dict[str, Any] = {}
        for edge_id in self.edges:
            result[edge_id] = self._get(f"{self._edge_url(edge_id)}/healthz", timeout=2) or {"ok": False}
        for vpp_id in self.vpps:
            result[vpp_id] = self._get(f"{self._vpp_url(vpp_id)}/healthz", timeout=2) or {"ok": False}
        result["grid"] = self._get(f"{self._grid_url}/healthz", timeout=2) or {"ok": False}
        result["web"] = {"ok": True, "service": "web"}
        return result

    def overview(self) -> dict:
        packets = self._all_packets()
        collection = self.collection()
        grid_rows = (self._get(f"{self._grid_url}/api/v1/batches") or {}).get("batches", [])
        statuses: dict[str, int] = {}
        records = 0
        for row in packets:
            statuses[row.get("status") or "UNKNOWN"] = statuses.get(row.get("status") or "UNKNOWN", 0) + 1
            records += int(row.get("recordCount") or 0)
        grid_statuses: dict[str, int] = {}
        for row in grid_rows:
            key = row.get("status") or "UNKNOWN"
            grid_statuses[key] = grid_statuses.get(key, 0) + 1
        run_id = ""
        if packets:
            newest = max(packets, key=lambda row: row.get("updatedAt") or "")
            detail = self._get(
                f"{self._vpp_url(newest['vppId'])}/api/v1/packets/{newest['packetId']}", timeout=3
            ) or {}
            for record in (detail.get("records") or [])[:1]:
                rid = str(record.get("recordId", ""))
                if rid.startswith("REC-"):
                    run_id = rid[4:].rsplit("-", 3)[0]
        return {
            "chain": self.chain_snapshot(),
            "services": self.services_health(),
            "counts": {
                "batches": len(packets),
                "records": records,
                "vppStatuses": statuses,
                "gridStatuses": grid_statuses,
                "edges": len(self.config["edges"]),
                "devices": sum(len(edge["devices"]) for edge in self.config["edges"]),
                "collectionRunning": collection["runningCount"],
            },
            "collection": collection,
            "runId": run_id,
            "generatedAt": _iso_now(),
        }

    # -- batch collection --------------------------------------------------
    def _all_packets(self) -> list[dict]:
        rows: list[dict] = []
        for vpp_id in self.vpps:
            payload = self._get(f"{self._vpp_url(vpp_id)}/api/v1/packets") or {}
            for item in payload.get("packets", []):
                rows.append({**item, "vppId": vpp_id})
        grid_rows = (self._get(f"{self._grid_url}/api/v1/batches") or {}).get("batches", [])
        grid_map = {row["batchId"]: row for row in grid_rows}
        edge_status: dict[str, str] = {}
        for edge_id in self.edges:
            payload = self._get(f"{self._edge_url(edge_id)}/api/v1/packets") or {}
            for item in payload.get("packets", []):
                edge_status[item["packetId"]] = item.get("status")
        for row in rows:
            grid = grid_map.get(row.get("batchId"), {})
            row["gridStatus"] = grid.get("status")
            row["edgeStatus"] = edge_status.get(row.get("packetId"))
            row["rootHash"] = grid.get("chainRoot") or row.get("rootHash")
        rows.sort(key=lambda item: item.get("updatedAt") or "", reverse=True)
        return rows

    def list_batches(self) -> dict:
        rows = self._all_packets()
        for row in rows:
            row.pop("_records", None)
        return {"batches": rows, "count": len(rows), "generatedAt": _iso_now()}

    def _find_packet(self, batch_id: str) -> tuple[str, dict] | tuple[None, None]:
        for vpp_id in self.vpps:
            payload = self._get(f"{self._vpp_url(vpp_id)}/api/v1/packets") or {}
            for item in payload.get("packets", []):
                if item.get("batchId") == batch_id:
                    return vpp_id, item
        return None, None

    def batch_detail(self, batch_id: str) -> dict:
        vpp_id, packet_row = self._find_packet(batch_id)
        if vpp_id is None:
            raise ProtocolError("NOT_FOUND", f"unknown batch {batch_id}", object_id=batch_id)
        packet_id = packet_row["packetId"]
        edge_id = packet_row["edgeId"]
        vpp_detail = self._get(f"{self._vpp_url(vpp_id)}/api/v1/packets/{packet_id}") or {}
        edge_detail = self._get(f"{self._edge_url(edge_id)}/api/v1/packets/{packet_id}") or {}
        grid_detail = self._get(f"{self._grid_url}/api/v1/batches/{batch_id}") or {}
        chain_read = self._get(f"{self._grid_url}/api/v1/chain/batches/{batch_id}")

        tx_hash = vpp_detail.get("txHash") or grid_detail.get("txHash")
        receipt = self._rpc("getTransactionReceipt", [1, tx_hash]) if tx_hash else None
        tx = self._rpc("getTransactionByHash", [1, tx_hash]) if tx_hash else None

        envelope = vpp_detail.get("envelope") or {}
        records = envelope.get("records") or vpp_detail.get("records") or []
        record_meta = {}
        if records:
            try:
                for index, record in enumerate(freeze_order(records)):
                    record_meta[record["recordId"]] = {
                        "index": index,
                        "leafHash": leaf_hash(record, canonical_bytes).hex(),
                        "canonical": canonical_text(record),
                    }
            except Exception:  # noqa: BLE001 - metadata is best-effort
                record_meta = {}

        merkle = None
        if records:
            try:
                tree = build_tree(records, canonical_bytes)
                merkle = {
                    "levels": [[digest.hex() for digest in level] for level in tree.levels],
                    "root": tree.root_hex,
                    "size": tree.size,
                    "rule": tree.rule_version,
                }
            except Exception as exc:  # noqa: BLE001 - never break the dashboard
                merkle = {"error": str(exc)}

        edge_packet = edge_detail.get("packet") or {}
        edge_signature = edge_detail.get("signature") or {}
        signature_check = None
        if edge_packet and edge_signature:
            trusted = self.vpps[vpp_id]["trustedEdges"].get(edge_id, {})
            ok, reason = verify_bytes(canonical_bytes(edge_packet), edge_signature.get("value", ""),
                                      trusted.get("publicKey", ""))
            signature_check = {
                "ok": ok,
                "reason": reason,
                "keyId": edge_signature.get("keyId"),
                "registeredKeyId": trusted.get("keyId"),
                "digest": canonical_sha256(edge_packet),
                "trustedPublicKey": trusted.get("publicKey"),
            }

        return {
            "batchId": batch_id,
            "packetId": packet_id,
            "vppId": vpp_id,
            "edgeId": edge_id,
            "generatedAt": _iso_now(),
            "edge": {
                "state": self._get(f"{self._edge_url(edge_id)}/api/v1/state"),
                "packet": edge_packet,
                "signature": edge_signature,
                "signatureCheck": signature_check,
                "delivery": edge_detail.get("delivery"),
                "status": edge_detail.get("status"),
            },
            "vpp": {
                "state": self._get(f"{self._vpp_url(vpp_id)}/api/v1/state"),
                "status": vpp_detail.get("status"),
                "gridStatus": vpp_detail.get("gridStatus"),
                "rootHash": vpp_detail.get("rootHash"),
                "recordCount": vpp_detail.get("recordCount"),
                "txHash": vpp_detail.get("txHash"),
                "blockNumber": vpp_detail.get("blockNumber"),
                "reason": vpp_detail.get("reason"),
                "envelope": envelope,
                "records": records,
                "proofs": vpp_detail.get("proofs") or envelope.get("proofs") or [],
            },
            "merkle": merkle,
            "recordMeta": record_meta,
            "chain": {
                "contractAddress": self.contract,
                "read": chain_read,
                "record": (chain_read or {}).get("chain"),
                "receipt": receipt,
                "tx": tx,
                "rpcUrl": self.rpc_url,
            },
            "grid": {
                "state": self._get(f"{self._grid_url}/api/v1/state"),
                "detail": grid_detail or None,
            },
        }


    # -- automation control ------------------------------------------------
    def collection(self) -> dict:
        edges = []
        for edge in self.config["edges"]:
            state = self._get(f"{self._edge_url(edge['edgeId'])}/api/v1/collection", timeout=3) or {}
            edges.append(
                {
                    "edgeId": edge["edgeId"],
                    "vppId": edge["vppId"],
                    "devices": len(edge["devices"]),
                    "running": bool(state.get("running")),
                    "intervalSeconds": state.get("intervalSeconds", edge.get("intervalSeconds", 10)),
                    "rounds": state.get("rounds", 0),
                    "lastRoundAt": state.get("lastRoundAt"),
                    "lastPacketId": state.get("lastPacketId"),
                    "online": bool(state),
                }
            )
        return {"edges": edges, "runningCount": sum(1 for e in edges if e["running"])}

    def collection_action(self, action: str) -> dict:
        results = []
        for edge in self.config["edges"]:
            url = f"{self._edge_url(edge['edgeId'])}/api/v1/collection/{action}"
            try:
                response = requests.post(url, timeout=10)
                body = response.json() if response.content else {}
                results.append({"edgeId": edge["edgeId"], "httpStatus": response.status_code,
                                "running": body.get("running")})
            except requests.RequestException as exc:
                results.append({"edgeId": edge["edgeId"], "httpStatus": None, "error": str(exc)})
        return {"action": action, "edges": results}

    # -- downlink commands -------------------------------------------------
    def commands(self, limit: int = 50) -> dict:
        payload = self._get(f"{self._grid_url}/api/v1/commands?limit={limit}", timeout=5) or {"commands": []}
        commands = payload.get("commands", [])[:limit]
        return {"commands": commands, "count": len(commands)}

    def create_command(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise ProtocolError("INVALID_SCHEMA", "body must be an object")
        payload = {
            "targetEdgeId": body.get("targetEdgeId"),
            "targetDeviceId": body.get("targetDeviceId"),
            "action": body.get("action"),
            "params": body.get("params") or {},
        }
        response = requests.post(f"{self._grid_url}/api/v1/commands", json=payload, timeout=120)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            error = data.get("error", {})
            raise ProtocolError(error.get("code", "INVALID_SCHEMA"), error.get("message", "grid rejected command"))
        return data

    def command_meta(self) -> dict:
        return {
            "edges": [
                {
                    "edgeId": edge["edgeId"],
                    "vppId": edge["vppId"],
                    "devices": [{"deviceId": d["deviceId"], "deviceType": d["deviceType"]} for d in edge["devices"]],
                }
                for edge in self.config["edges"]
            ],
            "actions": ["set_voltage", "power_on", "power_off"],
        }

    # -- demo actions ------------------------------------------------------
    def feed(self, rounds: int = 1) -> dict:
        from scripts.feed_records import feed_round  # local import keeps start-up light

        run_id = "RUN-UI-" + datetime.now(tz=timezone.utc).strftime("%H%M%S")
        results = []
        for round_no in range(1, max(1, min(rounds, 5)) + 1):
            for item in feed_round(self.config, run_id, round_no):
                packets = []
                for packet in item["result"].get("packets", []):
                    delivery = packet.get("delivery") or {}
                    response = delivery.get("response") or {}
                    packets.append(
                        {
                            "packetId": packet.get("packetId"),
                            "status": packet.get("status"),
                            "httpStatus": delivery.get("httpStatus"),
                            "batchId": response.get("batchId"),
                            "txHash": response.get("txHash"),
                            "blockNumber": response.get("blockNumber"),
                        }
                    )
                results.append({"edgeId": item["edgeId"], "sent": item["sent"], "packets": packets})
        return {"runId": run_id, "edges": results}

    def tamper(self) -> dict:
        rows = [row for row in self._all_packets() if row.get("gridStatus") == "VERIFIED"]
        if not rows:
            raise ProtocolError("NOT_FOUND", "no VERIFIED batch to tamper with; feed a round first")
        target = rows[0]
        batch_id = target["batchId"]
        vpp_id, packet_row = self._find_packet(batch_id)
        detail = self._get(f"{self._vpp_url(vpp_id)}/api/v1/packets/{packet_row['packetId']}") or {}
        envelope = detail.get("envelope") or {}
        if not envelope:
            raise ProtocolError("NOT_FOUND", "batch envelope not available")
        records = envelope.get("records") or []
        index = 1 if len(records) > 1 else 0
        original = self._post(f"{self._grid_url}/api/v1/verify", {"envelope": envelope})["body"]

        tampered_envelope = copy.deepcopy(envelope)
        before = tampered_envelope["records"][index]["value"]
        tampered_envelope["records"][index]["value"] = round(float(before) + 7.0, 3)
        tampered = self._post(f"{self._grid_url}/api/v1/verify", {"envelope": tampered_envelope})["body"]

        return {
            "batchId": batch_id,
            "vppId": vpp_id,
            "edgeId": packet_row["edgeId"],
            "recordId": records[index]["recordId"],
            "before": before,
            "after": tampered_envelope["records"][index]["value"],
            "original": {
                "status": original.get("status"),
                "checks": original.get("checks"),
                "recomputedRootHash": original.get("recomputedRootHash"),
                "chainRootHash": original.get("chainRootHash"),
            },
            "tampered": {
                "status": tampered.get("status"),
                "checks": tampered.get("checks"),
                "recomputedRootHash": tampered.get("recomputedRootHash"),
                "chainRootHash": tampered.get("chainRootHash"),
                "failures": tampered.get("failures"),
            },
            "generatedAt": _iso_now(),
        }


def create_app(config_path: str | None = None) -> Flask:
    config = load_config(config_path)
    runtime = WebRuntime(config)

    app = Flask(SERVICE, static_folder=STATIC_DIR, static_url_path="/static")
    app.config["RUNTIME"] = runtime

    @app.after_request
    def _no_cache_static(response):
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.errorhandler(ProtocolError)
    def _handle(error: ProtocolError):
        return jsonify(error.to_dict()), (404 if error.code == "NOT_FOUND" else 400)

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/healthz")
    def healthz():
        return jsonify({"service": SERVICE, "ok": True, "port": config["web"]["port"]})

    @app.get("/api/overview")
    def overview():
        return jsonify(runtime.overview())

    @app.get("/api/batches")
    def batches():
        return jsonify(runtime.list_batches())

    @app.get("/api/batches/<batch_id>")
    def batch(batch_id: str):
        return jsonify(runtime.batch_detail(batch_id))

    @app.post("/api/feed")
    def feed():
        body = request.get_json(silent=True) or {}
        return jsonify(runtime.feed(int(body.get("rounds", 1))))


    @app.get("/api/collection")
    def collection():
        return jsonify(runtime.collection())

    @app.post("/api/collection/start")
    def collection_start():
        return jsonify(runtime.collection_action("start"))

    @app.post("/api/collection/pause")
    def collection_pause():
        return jsonify(runtime.collection_action("pause"))

    @app.get("/api/commands")
    def commands():
        return jsonify(runtime.commands())

    @app.post("/api/commands")
    def create_command():
        return jsonify(runtime.create_command(request.get_json(silent=True) or {}))

    @app.get("/api/meta")
    def meta():
        return jsonify(runtime.command_meta())

    @app.post("/api/tamper")
    def tamper():
        return jsonify(runtime.tamper())

    return app


def main() -> None:
    app = create_app()
    runtime: WebRuntime = app.config["RUNTIME"]
    app.run(host="127.0.0.1", port=int(runtime.config["web"]["port"]), threaded=True)


if __name__ == "__main__":
    main()
