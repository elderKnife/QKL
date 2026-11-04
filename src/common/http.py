"""Small HTTP helpers shared by the three services."""
from __future__ import annotations

import json
from typing import Any

from flask import Request, Response, jsonify

from .models import ProtocolError

MAX_BODY_BYTES = 2 * 1024 * 1024

_STATUS_BY_CODE = {
    "INVALID_SCHEMA": 400,
    "DUPLICATE_ID": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "IDENTITY_NOT_ALLOWED": 403,
    "SIGNATURE_INVALID": 400,
    "PROOF_INVALID": 400,
    "ROOT_MISMATCH": 409,
    "EXPIRED": 400,
    "TARGET_MISMATCH": 409,
    "CHAIN_UNAVAILABLE": 503,
    "TX_OUTCOME_UNKNOWN": 502,
    "NOT_FOUND": 404,
    "CONFIG_ERROR": 500,
}


def status_for(code: str) -> int:
    return _STATUS_BY_CODE.get(code, 500)


def read_json(request: Request) -> Any:
    raw = request.get_data(cache=False)
    if len(raw) > MAX_BODY_BYTES:
        raise ProtocolError("INVALID_SCHEMA", f"request body exceeds {MAX_BODY_BYTES} bytes")
    if not raw:
        raise ProtocolError("INVALID_SCHEMA", "request body is empty")
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ProtocolError("INVALID_SCHEMA", f"request body is not UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProtocolError("INVALID_SCHEMA", f"request body is not valid JSON: {exc}") from exc


def error_response(error: ProtocolError) -> tuple[Response, int]:
    return jsonify(error.to_dict()), status_for(error.code)


def ok(payload: dict, status: int = 200) -> tuple[Response, int]:
    return jsonify(payload), status
