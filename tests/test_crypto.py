"""Unit tests for BUSINESS-SIG-V1 (ECDSA secp256k1 / SHA-256 / r||s)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ecdsa import SECP256k1, SigningKey  # noqa: E402

from src.common.crypto import Signer, verify_bytes  # noqa: E402


class CryptoTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "key.pem")
        with open(self.path, "wb") as handle:
            handle.write(SigningKey.generate(curve=SECP256k1).to_pem())
        os.chmod(self.path, 0o600)
        self.signer = Signer.from_file(self.path, "test-key-v1")

    def tearDown(self):
        self.tmp.cleanup()


class CryptoSignatureTests(CryptoTestBase):
    def test_sign_and_verify(self):
        payload = b'{"a":1}'
        signature = self.signer.sign_bytes(payload)
        self.assertEqual(len(signature), 128)
        ok, reason = verify_bytes(payload, signature, self.signer.public_key_hex)
        self.assertTrue(ok, reason)

    def test_deterministic_signature(self):
        payload = b"deterministic"
        self.assertEqual(self.signer.sign_bytes(payload), self.signer.sign_bytes(payload))

    def test_tampered_payload_rejected(self):
        signature = self.signer.sign_bytes(b'{"a":1}')
        ok, reason = verify_bytes(b'{"a":2}', signature, self.signer.public_key_hex)
        self.assertFalse(ok)
        self.assertEqual(reason, "SIGNATURE_INVALID:mismatch")

    def test_wrong_key_rejected(self):
        other = SigningKey.generate(curve=SECP256k1)
        signature = self.signer.sign_bytes(b"x")
        ok, _ = verify_bytes(b"x", signature, other.get_verifying_key().to_string("uncompressed").hex())
        self.assertFalse(ok)

    def test_bad_encoding_rejected(self):
        for bad in ("", "zz", "00" * 64):
            ok, reason = verify_bytes(b"x", bad, self.signer.public_key_hex)
            self.assertFalse(ok, bad)
            self.assertTrue(reason.startswith("SIGNATURE_INVALID"))

    def test_world_readable_key_refused(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(Exception):
            Signer.from_file(self.path, "k")

    def test_public_key_shape(self):
        public_key = self.signer.public_key_hex
        self.assertEqual(len(public_key), 130)
        self.assertTrue(public_key.startswith("04"))


if __name__ == "__main__":
    unittest.main()
