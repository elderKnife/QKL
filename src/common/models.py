"""Wire models and validation (frozen for this validation experiment).

Only the upstream half of the platform is modelled here: device records ->
edge packet -> VPP batch -> chain anchor -> grid verification.  The A1 device
simulator and the downlink command path are intentionally out of scope.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .canonical import CanonicalizationError, canonical_bytes, canonical_sha256

PACKET_SCHEMA = "EDGE-PACKET-V1"
BATCH_SCHEMA = "VPP-BATCH-V1"
RECORD_SCHEMA = "DEVICE-RECORD-V1"
COMMAND_SCHEMA = "GRID-COMMAND-V1"
ACK_SCHEMA = "EDGE-ACK-V1"
ACK_ENVELOPE_SCHEMA = "ACK-ENVELOPE-V1"

DEVICE_TYPES = ("pv", "storage", "charger")
METRIC = "active_power_kw"
UNIT = "kW"
REQUIRED_RECORD_FIELDS = ("recordId", "deviceId", "deviceType", "metric", "value", "unit", "timestamp")
MAX_PACKET_RECORDS = 256
MAX_TIMESTAMP_SKEW_SECONDS = 300
COMMAND_ACTIONS = ("set_voltage", "power_on", "power_off")
MAX_COMMAND_TTL_SECONDS = 600

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

ERROR_CODES = (
    "INVALID_SCHEMA",
    "DUPLICATE_ID",
    "IDENTITY_NOT_ALLOWED",
    "SIGNATURE_INVALID",
    "PROOF_INVALID",
    "ROOT_MISMATCH",
    "EXPIRED",
    "TARGET_MISMATCH",
    "CHAIN_UNAVAILABLE",
    "TX_OUTCOME_UNKNOWN",
    "IDEMPOTENCY_CONFLICT",
    "NOT_FOUND",
    "CONFIG_ERROR",
)


class ProtocolError(ValueError):
    """A structured, stable protocol failure."""

    def __init__(self, code: str, message: str, *, object_id: str | None = None, retryable: bool = False):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.object_id = object_id
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "objectId": self.object_id,
                "retryable": self.retryable,
            }
        }


def utc_now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def utc_iso(ms: int | None = None) -> str:
    moment = datetime.fromtimestamp((ms if ms is not None else utc_now_ms()) / 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_iso_ms(text: Any) -> int:
    if not isinstance(text, str) or not _TS_RE.match(text):
        raise ProtocolError("INVALID_SCHEMA", f"timestamp must be ISO-8601 UTC with milliseconds, got {text!r}")
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ProtocolError("INVALID_SCHEMA", f"invalid timestamp {text!r}: {exc}") from exc
    return int(moment.timestamp() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ProtocolError("INVALID_SCHEMA", f"{field} must match {_ID_RE.pattern}")
    return value


def _require_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("INVALID_SCHEMA", f"{field} must be a number")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ProtocolError("INVALID_SCHEMA", f"{field} must be finite")
    # power precision is frozen to 3 decimals for this experiment
    if abs(number * 1000 - round(number * 1000)) > 1e-6:
        raise ProtocolError("INVALID_SCHEMA", f"{field} must have at most 3 decimal places")
    return number


def validate_record(record: Any) -> dict:
    if not isinstance(record, dict):
        raise ProtocolError("INVALID_SCHEMA", "record must be an object")
    missing = [field for field in REQUIRED_RECORD_FIELDS if field not in record]
    if missing:
        raise ProtocolError("INVALID_SCHEMA", f"record missing fields: {','.join(missing)}")
    extra = set(record) - set(REQUIRED_RECORD_FIELDS)
    if extra:
        raise ProtocolError("INVALID_SCHEMA", f"record has unexpected fields: {','.join(sorted(extra))}")

    record_id = _require_id(record["recordId"], "recordId")
    device_id = _require_id(record["deviceId"], "deviceId")
    if record["deviceType"] not in DEVICE_TYPES:
        raise ProtocolError("INVALID_SCHEMA", f"deviceType must be one of {DEVICE_TYPES}", object_id=device_id)
    if record["metric"] != METRIC:
        raise ProtocolError("INVALID_SCHEMA", f"metric must be {METRIC}", object_id=device_id)
    if record["unit"] != UNIT:
        raise ProtocolError("INVALID_SCHEMA", f"unit must be {UNIT}", object_id=device_id)
    value = _require_number(record["value"], "value")
    if record["deviceType"] in ("pv", "charger") and value < 0:
        raise ProtocolError("INVALID_SCHEMA", f"{record['deviceType']} power must be non-negative", object_id=device_id)
    parse_iso_ms(record["timestamp"])
    return {
        "recordId": record_id,
        "deviceId": device_id,
        "deviceType": record["deviceType"],
        "metric": METRIC,
        "value": value,
        "unit": UNIT,
        "timestamp": record["timestamp"],
    }


def validate_packet(packet: Any) -> dict:
    if not isinstance(packet, dict):
        raise ProtocolError("INVALID_SCHEMA", "packet must be an object")
    required = ("schemaVersion", "packetId", "edgeId", "collectedAt", "records")
    missing = [f for f in required if f not in packet]
    if missing:
        raise ProtocolError("INVALID_SCHEMA", f"packet missing fields: {','.join(missing)}")
    if packet["schemaVersion"] != PACKET_SCHEMA:
        raise ProtocolError("INVALID_SCHEMA", f"unsupported packet schemaVersion {packet['schemaVersion']!r}")
    packet_id = _require_id(packet["packetId"], "packetId")
    edge_id = _require_id(packet["edgeId"], "edgeId")
    parse_iso_ms(packet["collectedAt"])
    records = packet["records"]
    if not isinstance(records, list) or not records:
        raise ProtocolError("INVALID_SCHEMA", "packet.records must be a non-empty array", object_id=packet_id)
    if len(records) > MAX_PACKET_RECORDS:
        raise ProtocolError("INVALID_SCHEMA", f"packet exceeds {MAX_PACKET_RECORDS} records", object_id=packet_id)
    seen: set[str] = set()
    validated: list[dict] = []
    for record in records:
        item = validate_record(record)
        if item["recordId"] in seen:
            raise ProtocolError("DUPLICATE_ID", f"duplicate recordId {item['recordId']}", object_id=packet_id)
        seen.add(item["recordId"])
        validated.append(item)
    return {
        "schemaVersion": PACKET_SCHEMA,
        "packetId": packet_id,
        "edgeId": edge_id,
        "collectedAt": packet["collectedAt"],
        "records": validated,
    }


def packet_digest(packet: dict) -> str:
    """SHA-256 over the canonical packet text (what the edge signs)."""
    return canonical_sha256(packet)


def batch_id_for(packet_id: str) -> str:
    """One upstream packet -> one anchor batch (frozen for this experiment)."""
    return f"BATCH-{packet_id}"


def validate_batch_envelope(envelope: Any) -> dict:
    required = (
        "schemaVersion",
        "batchId",
        "vppId",
        "edgeId",
        "packetId",
        "recordCount",
        "rootHash",
        "builtAt",
        "records",
        "proofs",
    )
    if not isinstance(envelope, dict):
        raise ProtocolError("INVALID_SCHEMA", "batch envelope must be an object")
    missing = [f for f in required if f not in envelope]
    if missing:
        raise ProtocolError("INVALID_SCHEMA", f"batch envelope missing fields: {','.join(missing)}")
    if envelope["schemaVersion"] != BATCH_SCHEMA:
        raise ProtocolError("INVALID_SCHEMA", f"unsupported batch schemaVersion {envelope['schemaVersion']!r}")
    batch_id = _require_id(envelope["batchId"], "batchId")
    _require_id(envelope["vppId"], "vppId")
    _require_id(envelope["edgeId"], "edgeId")
    _require_id(envelope["packetId"], "packetId")
    parse_iso_ms(envelope["builtAt"])
    records = envelope["records"]
    if not isinstance(records, list) or not records:
        raise ProtocolError("INVALID_SCHEMA", "batch.records must be a non-empty array", object_id=batch_id)
    if not isinstance(envelope["recordCount"], int) or envelope["recordCount"] <= 0:
        raise ProtocolError("INVALID_SCHEMA", "recordCount must be a positive integer", object_id=batch_id)
    if envelope["recordCount"] != len(records):
        raise ProtocolError("INVALID_SCHEMA", "recordCount does not match records length", object_id=batch_id)
    root = envelope["rootHash"]
    if not isinstance(root, str) or len(root) != 64:
        raise ProtocolError("INVALID_SCHEMA", "rootHash must be 64 hex characters", object_id=batch_id)
    try:
        bytes.fromhex(root)
    except ValueError as exc:
        raise ProtocolError("INVALID_SCHEMA", "rootHash is not valid hex", object_id=batch_id) from exc
    if not isinstance(envelope["proofs"], list) or len(envelope["proofs"]) != len(records):
        raise ProtocolError("INVALID_SCHEMA", "one proof per record is required", object_id=batch_id)
    validated_records = [validate_record(record) for record in records]
    return envelope


def canonical_packet_bytes(packet: dict) -> bytes:
    try:
        return canonical_bytes(packet)
    except CanonicalizationError as exc:  # pragma: no cover - defensive
        raise ProtocolError("INVALID_SCHEMA", f"packet cannot be canonicalised: {exc}") from exc


def command_batch_id(command_id: str) -> str:
    return f"BATCH-CMD-{command_id}"


def ack_batch_id(ack_id: str) -> str:
    return f"BATCH-ACK-{ack_id}"


def validate_command(command: Any) -> dict:
    required = (
        "schemaVersion", "commandId", "gridId", "targetEdgeId", "targetDeviceId",
        "action", "params", "issuedAt", "expiresAt", "nonce",
    )
    if not isinstance(command, dict):
        raise ProtocolError("INVALID_SCHEMA", "command must be an object")
    missing = [field for field in required if field not in command]
    if missing:
        raise ProtocolError("INVALID_SCHEMA", f"command missing fields: {','.join(missing)}")
    extra = set(command) - set(required)
    if extra:
        raise ProtocolError("INVALID_SCHEMA", f"command has unexpected fields: {','.join(sorted(extra))}")
    if command["schemaVersion"] != COMMAND_SCHEMA:
        raise ProtocolError("INVALID_SCHEMA", f"unsupported command schemaVersion {command['schemaVersion']!r}")
    command_id = _require_id(command["commandId"], "commandId")
    _require_id(command["gridId"], "gridId")
    target_edge = _require_id(command["targetEdgeId"], "targetEdgeId")
    target_device = _require_id(command["targetDeviceId"], "targetDeviceId")
    action = command["action"]
    if action not in COMMAND_ACTIONS:
        raise ProtocolError("INVALID_SCHEMA", f"action must be one of {COMMAND_ACTIONS}", object_id=command_id)
    params = command["params"]
    if not isinstance(params, dict):
        raise ProtocolError("INVALID_SCHEMA", "params must be an object", object_id=command_id)
    if action == "set_voltage":
        if set(params) != {"voltageV"}:
            raise ProtocolError("INVALID_SCHEMA", "set_voltage requires exactly params.voltageV", object_id=command_id)
        voltage = _require_number(params["voltageV"], "params.voltageV")
        if not 180.0 <= voltage <= 260.0:
            raise ProtocolError("INVALID_SCHEMA", "voltageV must be within 180..260", object_id=command_id)
        if abs(voltage * 10 - round(voltage * 10)) > 1e-6:
            raise ProtocolError("INVALID_SCHEMA", "voltageV must have at most 1 decimal place", object_id=command_id)
        params = {"voltageV": voltage}
    else:
        if params:
            raise ProtocolError("INVALID_SCHEMA", f"{action} does not take params", object_id=command_id)
        params = {}
    issued = parse_iso_ms(command["issuedAt"])
    expires = parse_iso_ms(command["expiresAt"])
    if expires <= issued:
        raise ProtocolError("INVALID_SCHEMA", "expiresAt must be after issuedAt", object_id=command_id)
    if expires - issued > MAX_COMMAND_TTL_SECONDS * 1000:
        raise ProtocolError("INVALID_SCHEMA", f"command TTL exceeds {MAX_COMMAND_TTL_SECONDS}s", object_id=command_id)
    nonce = command["nonce"]
    if not isinstance(nonce, str) or not nonce or len(nonce) > 64:
        raise ProtocolError("INVALID_SCHEMA", "nonce must be a non-empty string", object_id=command_id)
    return {
        "schemaVersion": COMMAND_SCHEMA,
        "commandId": command_id,
        "gridId": command["gridId"],
        "targetEdgeId": target_edge,
        "targetDeviceId": target_device,
        "action": action,
        "params": params,
        "issuedAt": command["issuedAt"],
        "expiresAt": command["expiresAt"],
        "nonce": nonce,
    }


def validate_ack(ack: Any) -> dict:
    required = (
        "schemaVersion", "ackId", "commandId", "commandDigest", "edgeId",
        "deviceId", "action", "result", "appliedState", "executedAt",
    )
    if not isinstance(ack, dict):
        raise ProtocolError("INVALID_SCHEMA", "ack must be an object")
    missing = [field for field in required if field not in ack]
    if missing:
        raise ProtocolError("INVALID_SCHEMA", f"ack missing fields: {','.join(missing)}")
    if ack["schemaVersion"] != ACK_SCHEMA:
        raise ProtocolError("INVALID_SCHEMA", f"unsupported ack schemaVersion {ack['schemaVersion']!r}")
    ack_id = _require_id(ack["ackId"], "ackId")
    _require_id(ack["commandId"], "commandId")
    _require_id(ack["edgeId"], "edgeId")
    _require_id(ack["deviceId"], "deviceId")
    digest = ack["commandDigest"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise ProtocolError("INVALID_SCHEMA", "commandDigest must be 64 hex chars", object_id=ack_id)
    if ack["action"] not in COMMAND_ACTIONS:
        raise ProtocolError("INVALID_SCHEMA", "unknown action", object_id=ack_id)
    if ack["result"] not in ("APPLIED", "REJECTED"):
        raise ProtocolError("INVALID_SCHEMA", "result must be APPLIED or REJECTED", object_id=ack_id)
    state = ack["appliedState"]
    if not isinstance(state, dict) or "on" not in state or not isinstance(state["on"], bool):
        raise ProtocolError("INVALID_SCHEMA", "appliedState.on must be a boolean", object_id=ack_id)
    if "voltageV" in state:
        _require_number(state["voltageV"], "appliedState.voltageV")
    parse_iso_ms(ack["executedAt"])
    return ack


def validate_ack_envelope(envelope: Any) -> dict:
    required = ("schemaVersion", "ack", "command", "vppId", "builtAt")
    if not isinstance(envelope, dict):
        raise ProtocolError("INVALID_SCHEMA", "ack envelope must be an object")
    missing = [field for field in required if field not in envelope]
    if missing:
        raise ProtocolError("INVALID_SCHEMA", f"ack envelope missing fields: {','.join(missing)}")
    if envelope["schemaVersion"] != ACK_ENVELOPE_SCHEMA:
        raise ProtocolError("INVALID_SCHEMA", f"unsupported ack envelope schemaVersion {envelope['schemaVersion']!r}")
    validate_command(envelope["command"])
    validate_ack(envelope["ack"])
    _require_id(envelope["vppId"], "vppId")
    parse_iso_ms(envelope["builtAt"])
    return envelope
