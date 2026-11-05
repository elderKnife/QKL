"""Business-layer ECDSA signing for experiment B.

This key pair is completely independent from the FISCO BCOS consensus-node
private keys.  It exists only to demonstrate who authored a business command.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)


def generate_or_load_keypair(
    private_path: str, public_path: str
) -> tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey]:
    if os.path.exists(private_path):
        private_key = serialization.load_pem_private_key(
            open(private_path, "rb").read(), password=None
        )
        assert isinstance(private_key, ec.EllipticCurvePrivateKey)
        return private_key, private_key.public_key()

    private_key = ec.generate_private_key(ec.SECP256K1())
    os.makedirs(os.path.dirname(private_path), exist_ok=True)
    with open(private_path, "wb") as f:
        f.write(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    with open(public_path, "wb") as f:
        f.write(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
    return private_key, private_key.public_key()


def sign_bytes(private_key: ec.EllipticCurvePrivateKey, data: bytes) -> str:
    der = private_key.sign(data, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    # Fixed-width 32-byte r||s keeps the off-chain proof byte-stable.
    return r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex()


def verify_bytes(public_key: ec.EllipticCurvePublicKey, data: bytes, signature_hex: str) -> bool:
    raw = bytes.fromhex(signature_hex)
    if len(raw) != 64:
        return False
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    der = encode_dss_signature(r, s)
    try:
        public_key.verify(der, data, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def public_key_hex(public_key: ec.EllipticCurvePublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return raw.hex()
