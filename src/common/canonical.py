"""Deterministic canonical JSON for this experiment (CANON-JSON-V1).

This is *this project's* deterministic JSON rule.  It is intentionally simple
and explicit; it is **not** a claim of RFC 8785 compliance.

Rules (frozen for this validation experiment, see docs/03-数据协议.md):

1. The value must be a JSON object/array/scalar made of UTF-8 strings, booleans,
   null, integers, and finite decimal numbers.
2. Object keys are sorted by Unicode code point; no insignificant whitespace:
   separators are exactly ``","`` and ``":"``.
3. Strings are emitted with ``ensure_ascii=False`` and are NFC-normalised.
4. Numbers are emitted through :class:`decimal.Decimal` using the shortest
   plain (non-exponent) representation and must not be NaN/Infinity.
5. Encoding is UTF-8; there is no trailing newline.

``canonical_bytes`` is the single source of truth used by edges, VPPs and the
grid platform, so a hash computed anywhere in the system matches.
"""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

CANONICAL_RULE_VERSION = "CANON-JSON-V1"
MAX_DEPTH = 32


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented under CANON-JSON-V1."""


def _normalise_string(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _format_number(value: Any, path: str) -> str:
    if isinstance(value, bool):  # bool is an int subclass, keep explicit
        raise CanonicalizationError(f"{path}: boolean is not a number")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"{path}: non-finite number rejected")
        # str(float) is the shortest round-tripping representation; going
        # through Decimal keeps it plain (never exponent notation).
        return _decimal_text(Decimal(str(value)), path)
    if isinstance(value, Decimal):
        return _decimal_text(value, path)
    raise CanonicalizationError(f"{path}: unsupported number type {type(value)!r}")


def _decimal_text(value: Decimal, path: str) -> str:
    if not value.is_finite():
        raise CanonicalizationError(f"{path}: non-finite number rejected")
    sign, digits, exponent = value.as_tuple()
    text = format(value, "f")  # plain decimal, never 1E+2
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0", "-"):
        text = "0"
    return text


def _canonicalise(value: Any, depth: int, path: str) -> Any:
    if depth > MAX_DEPTH:
        raise CanonicalizationError(f"{path}: nesting deeper than {MAX_DEPTH}")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _normalise_string(value)
    if isinstance(value, (int, float, Decimal)):
        return ("__number__", _format_number(value, path))
    if isinstance(value, (list, tuple)):
        return [_canonicalise(item, depth + 1, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        out = {}
        for key in sorted(value.keys()):
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: object key must be a string, got {key!r}")
            if key == "":
                raise CanonicalizationError(f"{path}: empty object key rejected")
            normalised = _normalise_string(key)
            if normalised in out:
                raise CanonicalizationError(f"{path}: duplicate key after NFC normalisation: {key!r}")
            out[normalised] = _canonicalise(value[key], depth + 1, f"{path}.{key}")
        return out
    raise CanonicalizationError(f"{path}: unsupported type {type(value)!r}")


def _render(value: Any) -> str:
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "__number__":
        return value[1]
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_render(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _render(v) for k, v in value.items()) + "}"
    raise CanonicalizationError(f"unsupported rendered type {type(value)!r}")


def canonical_text(value: Any) -> str:
    """Return the CANON-JSON-V1 text for ``value``."""
    return _render(_canonicalise(value, 0, "$"))


def canonical_bytes(value: Any) -> bytes:
    """Return the CANON-JSON-V1 UTF-8 bytes for ``value``."""
    return canonical_text(value).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """SHA-256 (hex) of the canonical bytes of ``value``."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict:
    """``json.loads`` hook: refuse duplicate keys instead of silently keeping one."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalizationError(f"duplicate JSON key rejected: {key!r}")
        result[key] = value
    return result


def parse_canonical_json(raw: bytes | str) -> Any:
    """Parse JSON bytes strictly, rejecting duplicate keys and NaN/Infinity."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    except UnicodeDecodeError as exc:  # pragma: no cover - defensive
        raise CanonicalizationError(f"payload is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                CanonicalizationError(f"non-finite JSON constant rejected: {token}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise CanonicalizationError(f"invalid JSON: {exc}") from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
