"""Business signatures for this experiment (BUSINESS-SIG-V1).

* Curve: secp256k1 (ECDSA)
* Digest: SHA-256 over the canonical bytes of the signed object
* Encoding: fixed 64-byte ``r || s`` hex (128 characters), no DER, no recover id
* Keys: PEM files owned by each module (edge / VPP); no private key ever leaves
  its owner or appears in logs/API responses.

The signature is *external evidence*: it is not part of the signed payload.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from ecdsa import BadSignatureError, SECP256k1, SigningKey, VerifyingKey
from ecdsa.util import sigdecode_string, sigencode_string

ALGORITHM = "ECDSA-secp256k1-SHA256"
RULE_VERSION = "BUSINESS-SIG-V1"
SIG_HEX_LEN = 128
PUBKEY_HEX_LEN = 130  # uncompressed 0x04 || X || Y


class SignatureError(ValueError):
    """Raised for malformed keys or signatures."""


@dataclass(frozen=True)
class Signer:
    key_id: str
    _key: SigningKey

    @classmethod
    def from_file(cls, path: str, key_id: str) -> "Signer":
        if not os.path.exists(path):
            raise SignatureError(f"signing key not found: {path}")
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            raise SignatureError(f"signing key {path} must not be readable by group/others (mode {oct(mode)})")
        with open(path, "r", encoding="ascii") as handle:
            pem = handle.read()
        try:
            key = SigningKey.from_pem(pem)
        except Exception as exc:  # noqa: BLE001
            raise SignatureError(f"cannot load signing key {path}: {exc}") from exc
        if key.curve != SECP256k1:
            raise SignatureError(f"signing key {path} is not secp256k1")
        return cls(key_id=key_id, _key=key)

    @property
    def public_key_hex(self) -> str:
        return self._key.get_verifying_key().to_string("uncompressed").hex()

    def sign_digest(self, digest: bytes) -> str:
        if len(digest) != 32:
            raise SignatureError("digest must be 32 bytes")
        signature = self._key.sign_digest_deterministic(digest, hashfunc=hashlib.sha256, sigencode=sigencode_string)
        return signature.hex()

    def sign_bytes(self, payload: bytes) -> str:
        return self.sign_digest(hashlib.sha256(payload).digest())


def public_key_from_hex(text: str) -> VerifyingKey:
    if not isinstance(text, str) or len(text) != PUBKEY_HEX_LEN:
        raise SignatureError("public key must be a 130-char uncompressed hex string")
    try:
        raw = bytes.fromhex(text)
    except ValueError as exc:
        raise SignatureError("public key is not valid hex") from exc
    try:
        return VerifyingKey.from_string(raw[1:], curve=SECP256k1)
    except Exception as exc:  # noqa: BLE001
        raise SignatureError(f"invalid secp256k1 public key: {exc}") from exc


def verify_digest(digest: bytes, signature_hex: str, public_key_hex: str) -> tuple[bool, str]:
    """Verify ``r||s`` over a 32-byte digest; never raise on bad input."""
    try:
        if len(digest) != 32:
            return False, "SIGNATURE_INVALID:digest length"
        if not isinstance(signature_hex, str) or len(signature_hex) != SIG_HEX_LEN:
            return False, "SIGNATURE_INVALID:encoding"
        try:
            signature = bytes.fromhex(signature_hex)
        except ValueError:
            return False, "SIGNATURE_INVALID:hex"
        key = public_key_from_hex(public_key_hex)
        key.verify_digest(signature, digest, sigdecode=sigdecode_string)
        return True, "PASS"
    except BadSignatureError:
        return False, "SIGNATURE_INVALID:mismatch"
    except SignatureError as exc:
        return False, f"SIGNATURE_INVALID:{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"SIGNATURE_INVALID:{exc}"


def verify_bytes(payload: bytes, signature_hex: str, public_key_hex: str) -> tuple[bool, str]:
    return verify_digest(hashlib.sha256(payload).digest(), signature_hex, public_key_hex)
