"""Unit tests for MERKLE-V1 (tree building, proofs, audit)."""
from __future__ import annotations

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.common.canonical import canonical_bytes  # noqa: E402
from src.common.merkle import (  # noqa: E402
    MerkleError,
    audit_batch,
    build_tree,
    leaf_hash,
    verify_proof,
)


def records(count: int) -> list[dict]:
    return [
        {
            "recordId": f"REC-{index:03d}",
            "deviceId": f"DEV-{index % 3}",
            "deviceType": "pv",
            "metric": "active_power_kw",
            "value": float(index),
            "unit": "kW",
            "timestamp": f"2026-11-04T10:00:{index:02d}.000Z",
        }
        for index in range(count)
    ]


class MerkleTests(unittest.TestCase):
    def test_sizes(self):
        for size in (1, 2, 3, 5, 6, 7, 8, 30, 31, 33):
            tree = build_tree(records(size), canonical_bytes)
            self.assertEqual(tree.size, size)
            for index in range(size):
                proof = tree.proof(index, "B1")
                ok, reason = verify_proof(tree.levels[0][index], proof, tree.root)
                self.assertTrue(ok, f"size={size} index={index} reason={reason}")

    def test_single_leaf_has_empty_path(self):
        tree = build_tree(records(1), canonical_bytes)
        proof = tree.proof(0, "B1")
        self.assertEqual(proof["siblings"], [])
        self.assertEqual(tree.root, tree.levels[0][0])
        self.assertTrue(verify_proof(tree.levels[0][0], proof, tree.root)[0])

    def test_odd_level_promotion(self):
        tree = build_tree(records(5), canonical_bytes)
        # 5 -> 3 -> 2 -> 1: the last leaf is promoted twice, then joined.
        self.assertEqual([len(level) for level in tree.levels], [5, 3, 2, 1])
        last = tree.proof(4, "B1")
        self.assertEqual(len(last["siblings"]), 1)
        self.assertEqual(last["siblings"][0]["position"], "left")

    def test_empty_batch_rejected(self):
        with self.assertRaises(MerkleError):
            build_tree([], canonical_bytes)

    def test_duplicate_ids_rejected(self):
        batch = records(3)
        batch[2]["recordId"] = batch[0]["recordId"]
        with self.assertRaises(MerkleError):
            build_tree(batch, canonical_bytes)

    def test_tamper_is_located(self):
        batch = records(7)
        tree = build_tree(batch, canonical_bytes)
        tampered = copy.deepcopy(batch)
        tampered[3]["value"] = 999.0
        report = audit_batch(tampered, tree.proofs("B1"), tree.root, canonical_bytes)
        self.assertFalse(report["ok"])
        self.assertEqual([item["recordId"] for item in report["failures"]], ["REC-003"])
        self.assertEqual(report["failures"][0]["reason"], "ROOT_MISMATCH")

    def test_audit_does_not_stop_at_first_failure(self):
        batch = records(6)
        tree = build_tree(batch, canonical_bytes)
        tampered = copy.deepcopy(batch)
        tampered[1]["value"] = 111.0
        tampered[4]["value"] = 222.0
        report = audit_batch(tampered, tree.proofs("B1"), tree.root, canonical_bytes)
        self.assertEqual({item["recordId"] for item in report["failures"]}, {"REC-001", "REC-004"})

    def test_forged_proof_rejected(self):
        tree = build_tree(records(4), canonical_bytes)
        proof = tree.proof(0, "B1")
        forged = copy.deepcopy(proof)
        # flip the direction of the first sibling: incompatible with the index
        forged["siblings"][0]["position"] = "left"
        ok, reason = verify_proof(tree.levels[0][0], forged, tree.root)
        self.assertFalse(ok)
        self.assertEqual(reason, "PROOF_MALFORMED")

    def test_wrong_root_rejected(self):
        tree = build_tree(records(3), canonical_bytes)
        proof = tree.proof(0, "B1")
        ok, reason = verify_proof(tree.levels[0][0], proof, bytes(32))
        self.assertFalse(ok)
        self.assertEqual(reason, "ROOT_MISMATCH")

    def test_proof_metadata_cannot_replace_root(self):
        tree = build_tree(records(4), canonical_bytes)
        changed = records(4)
        changed[0]["value"] = 777.0
        other = build_tree(changed, canonical_bytes)
        self.assertNotEqual(tree.root, other.root)
        proof = tree.proof(0, "B1")
        ok, reason = verify_proof(tree.levels[0][0], proof, other.root)
        self.assertFalse(ok)
        self.assertEqual(reason, "ROOT_MISMATCH")

    def test_missing_proof_reported(self):
        batch = records(3)
        tree = build_tree(batch, canonical_bytes)
        proofs = tree.proofs("B1")[:2]
        report = audit_batch(batch, proofs, tree.root, canonical_bytes)
        self.assertFalse(report["ok"])
        self.assertIn("PROOF_MISSING", {item["reason"] for item in report["failures"]})

    def test_leaf_prefix_is_used(self):
        record = records(1)[0]
        from src.common.canonical import canonical_bytes as cb

        import hashlib

        expected = hashlib.sha256(b"FISCO-BCOS-EVIDENCE-LEAF-V1:" + cb(record)).digest()
        self.assertEqual(leaf_hash(record, cb), expected)


if __name__ == "__main__":
    unittest.main()
