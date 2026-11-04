"""Unit tests for CANON-JSON-V1."""
from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.common.canonical import (  # noqa: E402
    CanonicalizationError,
    canonical_bytes,
    canonical_sha256,
    canonical_text,
    parse_canonical_json,
)


class CanonicalJsonTests(unittest.TestCase):
    def test_key_order_is_stable(self):
        a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
        b = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
        self.assertEqual(canonical_text(a), canonical_text(b))
        self.assertEqual(canonical_text(a), '{"a":2,"b":1,"c":{"y":2,"z":1}}')

    def test_no_insignificant_whitespace(self):
        self.assertEqual(canonical_text({"a": [1, 2]}), '{"a":[1,2]}')

    def test_numbers_are_plain_decimal(self):
        self.assertEqual(canonical_text({"v": 12.345}), '{"v":12.345}')
        self.assertEqual(canonical_text({"v": 1e-7}), '{"v":0.0000001}')
        self.assertEqual(canonical_text({"v": 10.0}), '{"v":10}')
        self.assertEqual(canonical_text({"v": -0.0}), '{"v":0}')

    def test_non_finite_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(CanonicalizationError):
                canonical_bytes({"v": value})

    def test_unicode_is_utf8_and_nfc(self):
        composed = "\u00e9"          # é
        decomposed = "e\u0301"       # e + combining acute
        self.assertEqual(canonical_bytes({"k": composed}), canonical_bytes({"k": decomposed}))
        self.assertEqual(canonical_text({"k": "光伏"}), '{"k":"光伏"}')

    def test_duplicate_keys_rejected_on_parse(self):
        with self.assertRaises(CanonicalizationError):
            parse_canonical_json('{"a":1,"a":2}')

    def test_nan_token_rejected_on_parse(self):
        with self.assertRaises(CanonicalizationError):
            parse_canonical_json('{"a":NaN}')

    def test_sha256_is_stable(self):
        payload = {"recordId": "R1", "value": 1.5}
        self.assertEqual(canonical_sha256(payload), canonical_sha256(dict(reversed(list(payload.items())))))

    def test_booleans_are_not_numbers(self):
        self.assertEqual(canonical_text({"ok": True}), '{"ok":true}')


if __name__ == "__main__":
    unittest.main()
