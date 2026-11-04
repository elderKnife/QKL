"""Merkle evidence rules (MERKLE-V1).

The rules intentionally reuse the historical FISCO BCOS evidence prefixes from
the project's previous experiment, so old evidence keeps its original meaning:

* leaf  = SHA256(b"FISCO-BCOS-EVIDENCE-LEAF-V1:" + canonical_bytes(record))
* node  = SHA256(b"FISCO-BCOS-EVIDENCE-NODE-V1:" + left_digest + right_digest)

Notes
-----
* Child digests are concatenated as **raw 32-byte values**, never as hex text.
* Records are ordered by ``(deviceId, timestamp, recordId)`` before leaves are
  built; the order is part of the rule version and is verified by the proof.
* Odd levels promote the last node unchanged (no self-duplication).
* Empty batches, duplicate record ids and malformed records are rejected
  before a tree is built.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

LEAF_PREFIX = b"FISCO-BCOS-EVIDENCE-LEAF-V1:"
NODE_PREFIX = b"FISCO-BCOS-EVIDENCE-NODE-V1:"
RULE_VERSION = "MERKLE-V1"

MAX_RECORDS = 4096
HEX_LEN = 64


class MerkleError(ValueError):
    """Raised when a batch or a proof violates MERKLE-V1."""


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def leaf_hash(record: dict, canonical_bytes_func) -> bytes:
    """Return the 32-byte leaf digest for one record."""
    return sha256(LEAF_PREFIX + canonical_bytes_func(record))


def node_hash(left: bytes, right: bytes) -> bytes:
    if len(left) != 32 or len(right) != 32:
        raise MerkleError("node_hash requires two 32-byte digests")
    return sha256(NODE_PREFIX + left + right)


@dataclass
class MerkleTree:
    """A fully materialised Merkle tree (all levels kept for proofs)."""

    rule_version: str
    record_ids: list[str]
    levels: list[list[bytes]] = field(default_factory=list)

    @property
    def root(self) -> bytes:
        if not self.levels:
            raise MerkleError("empty tree")
        return self.levels[-1][0]

    @property
    def root_hex(self) -> str:
        return self.root.hex()

    @property
    def size(self) -> int:
        return len(self.levels[0]) if self.levels else 0

    def proof(self, index: int, batch_id: str) -> dict:
        if index < 0 or index >= self.size:
            raise MerkleError(f"leaf index {index} out of range for tree size {self.size}")
        siblings = []
        position = index
        for level in self.levels[:-1]:
            sibling_index = position ^ 1
            if sibling_index < len(level):
                side = "left" if sibling_index < position else "right"
                siblings.append({"hash": level[sibling_index].hex(), "position": side})
            position //= 2
        return {
            "ruleVersion": self.rule_version,
            "batchId": batch_id,
            "recordId": self.record_ids[index],
            "index": index,
            "treeSize": self.size,
            "siblings": siblings,
        }

    def proofs(self, batch_id: str) -> list[dict]:
        return [self.proof(index, batch_id) for index in range(self.size)]


def freeze_order(records: Sequence[dict]) -> list[dict]:
    """Frozen MERKLE-V1 leaf order: (deviceId, timestamp, recordId)."""
    return sorted(
        records,
        key=lambda r: (str(r.get("deviceId", "")), str(r.get("timestamp", "")), str(r.get("recordId", ""))),
    )


def build_tree(records: Iterable[dict], canonical_bytes_func) -> MerkleTree:
    ordered = freeze_order(list(records))
    if not ordered:
        raise MerkleError("empty batch rejected")
    if len(ordered) > MAX_RECORDS:
        raise MerkleError(f"batch larger than {MAX_RECORDS} records rejected")

    record_ids: list[str] = []
    seen: set[str] = set()
    leaves: list[bytes] = []
    for record in ordered:
        record_id = record.get("recordId")
        if not isinstance(record_id, str) or not record_id:
            raise MerkleError("record is missing recordId")
        if record_id in seen:
            raise MerkleError(f"duplicate recordId rejected: {record_id}")
        seen.add(record_id)
        record_ids.append(record_id)
        leaves.append(leaf_hash(record, canonical_bytes_func))

    levels = [leaves]
    while len(levels[-1]) > 1:
        current = levels[-1]
        parents = [node_hash(current[i], current[i + 1]) for i in range(0, len(current) - 1, 2)]
        if len(current) % 2 == 1:
            parents.append(current[-1])  # odd node promoted unchanged
        levels.append(parents)

    return MerkleTree(rule_version=RULE_VERSION, record_ids=record_ids, levels=levels)


def _decode_digest(text: Any, what: str) -> bytes:
    if not isinstance(text, str) or len(text) != HEX_LEN:
        raise MerkleError(f"{what} must be a {HEX_LEN}-char hex digest")
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise MerkleError(f"{what} is not valid hex") from exc


def _expected_depth(index: int, tree_size: int) -> int:
    """Number of siblings a leaf consumes, accounting for odd-node promotion."""
    depth = 0
    size = tree_size
    position = index
    while size > 1:
        promoted = position == size - 1 and size % 2 == 1
        if not promoted:
            depth += 1
        position //= 2
        size = (size + 1) // 2
    return depth


def verify_proof(leaf_digest: bytes, proof: dict, expected_root: bytes) -> tuple[bool, str]:
    """Verify one proof against an externally supplied root.

    Returns ``(ok, reason)``.  The proof's own metadata cannot be trusted to
    establish the root; ``expected_root`` must come from the chain or another
    independently trusted source.  Path length and sibling sides are checked
    against the MERKLE-V1 promotion rule.
    """
    try:
        if proof.get("ruleVersion") != RULE_VERSION:
            return False, "RULE_VERSION_MISMATCH"
        index = proof.get("index")
        tree_size = proof.get("treeSize")
        siblings = proof.get("siblings")
        if not isinstance(index, int) or not isinstance(tree_size, int):
            return False, "PROOF_MALFORMED"
        if index < 0 or tree_size <= 0 or index >= tree_size:
            return False, "PROOF_MALFORMED"
        if not isinstance(siblings, list):
            return False, "PROOF_MALFORMED"
        if len(siblings) != _expected_depth(index, tree_size):
            return False, "PROOF_MALFORMED"

        node = leaf_digest
        size = tree_size
        position = index
        cursor = 0
        while size > 1:
            promoted = position == size - 1 and size % 2 == 1
            if not promoted:
                step = siblings[cursor]
                cursor += 1
                if not isinstance(step, dict):
                    return False, "PROOF_MALFORMED"
                sibling = _decode_digest(step.get("hash"), "sibling hash")
                expected_side = "left" if position % 2 == 1 else "right"
                if step.get("position") != expected_side:
                    return False, "PROOF_MALFORMED"
                node = node_hash(sibling, node) if expected_side == "left" else node_hash(node, sibling)
            position //= 2
            size = (size + 1) // 2

        if cursor != len(siblings):
            return False, "PROOF_MALFORMED"
        if node != expected_root:
            return False, "ROOT_MISMATCH"
        return True, "PASS"
    except MerkleError as exc:
        return False, f"PROOF_MALFORMED:{exc}"


def audit_batch(
    records: Sequence[dict],
    proofs: Sequence[dict],
    expected_root: bytes,
    canonical_bytes_func,
) -> dict[str, Any]:
    """Verify every record; never stop at the first failure.

    Returns a structured report listing every failing record id, which is what
    the grid platform uses to point at tampered off-chain rows.
    """
    failures: list[dict[str, str]] = []
    checked = 0
    proof_by_record = {}
    for proof in proofs:
        record_id = proof.get("recordId")
        if isinstance(record_id, str):
            proof_by_record.setdefault(record_id, proof)

    for record in freeze_order(records):
        record_id = str(record.get("recordId", ""))
        proof = proof_by_record.get(record_id)
        if proof is None:
            failures.append({"recordId": record_id, "reason": "PROOF_MISSING"})
            continue
        try:
            digest = leaf_hash(record, canonical_bytes_func)
            ok, reason = verify_proof(digest, proof, expected_root)
        except Exception as exc:  # noqa: BLE001 - report, never abort the audit
            ok, reason = False, f"LEAF_ERROR:{exc}"
        checked += 1
        if not ok:
            failures.append({"recordId": record_id, "reason": reason})

    extra = set(proof_by_record) - {str(r.get("recordId", "")) for r in records}
    for record_id in sorted(extra):
        failures.append({"recordId": record_id, "reason": "PROOF_WITHOUT_RECORD"})

    return {
        "checked": checked,
        "expected": len(records),
        "root": expected_root.hex(),
        "ok": not failures and checked == len(records),
        "failures": failures,
    }


def object_root_hex(obj: dict, canonical_bytes_func) -> str:
    """Root of a single-object batch: the object itself is the only leaf.

    Used for one-command-one-batch and one-acknowledgement-one-batch anchors.
    """
    return leaf_hash(obj, canonical_bytes_func).hex()
