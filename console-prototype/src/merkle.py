"""Deterministic Merkle tree construction, proof generation and verification.

All digests are domain-separated SHA-256 values; see ``common`` for the exact
prefixes.  Leaf records are hashed from their deterministic UTF-8 JSON bytes,
and internal nodes are hashed from the concatenation of their two child digests
in left-then-right order.

Rules
-----
* Records are sorted by ``record_id`` before the tree is built.
* The tree is a binary tree.  A parent's children are always ordered
  ``(left_digest, right_digest)``.
* If a level has an odd number of nodes, the last node is *promoted* unchanged
  to the next level (it is not duplicated and not hashed again).
* A proof is a list of ``{"side": "left"|"right", "hash": hex}`` entries plus
  the zero-based position of the record in the sorted record list.
"""

from __future__ import annotations

from dataclasses import dataclass

from .common import leaf_hash, node_hash


@dataclass
class MerkleTree:
    leaf_digests: list[bytes]
    root: bytes


def build_tree(records: list[dict], id_field: str = "recordId") -> MerkleTree:
    """Sort records by ``record_id`` and build a Merkle tree from their
    deterministic UTF-8 JSON bytes."""
    from .common import deterministic_json_bytes

    ordered = sorted(records, key=lambda r: str(r.get(id_field, "")))
    leaves = [leaf_hash(deterministic_json_bytes(record)) for record in ordered]
    root = _reduce_level([_b2h(d) for d in leaves])
    return MerkleTree(leaf_digests=leaves, root=bytes.fromhex(root))


def _b2h(digest: bytes) -> str:
    return digest.hex()


def _h2b(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def _reduce_level(level_hex: list[str]) -> str:
    """Reduce one Merkle level.  An odd final node is promoted unchanged."""
    if len(level_hex) == 1:
        return level_hex[0]

    parents: list[str] = []
    i = 0
    while i < len(level_hex):
        if i + 1 < len(level_hex):
            parent = node_hash(_h2b(level_hex[i]), _h2b(level_hex[i + 1])).hex()
        else:
            parent = level_hex[i]
        parents.append(parent)
        i += 2
    return _reduce_level(parents)


def compute_root(records: list[dict], id_field: str = "recordId") -> str:
    return build_tree(records, id_field=id_field).root.hex()


def generate_proof(
    records: list[dict], record_id: str, id_field: str = "recordId"
) -> dict:
    """Generate a Merkle membership proof for the record with ``record_id``."""
    from .common import deterministic_json_bytes

    ordered = sorted(records, key=lambda r: str(r.get(id_field, "")))
    position = next(
        i for i, r in enumerate(ordered) if str(r.get(id_field, "")) == str(record_id)
    )
    leaves = [leaf_hash(deterministic_json_bytes(r)) for r in ordered]
    level = [_b2h(d) for d in leaves]
    proof: list[dict] = []
    index = position

    while len(level) > 1:
        if index % 2 == 0:
            if index + 1 < len(level):
                proof.append({"side": "right", "hash": level[index + 1]})
            # else: sibling does not exist; promoted node keeps its position.
        else:
            proof.append({"side": "left", "hash": level[index - 1]})
        index //= 2
        level = _next_level(level)

    return {
        "recordId": str(record_id),
        "position": position,
        "proof": proof,
        "root": level[0],
    }


def _next_level(level_hex: list[str]) -> list[str]:
    if len(level_hex) == 1:
        return level_hex
    parents: list[str] = []
    i = 0
    while i < len(level_hex):
        if i + 1 < len(level_hex):
            parents.append(
                node_hash(_h2b(level_hex[i]), _h2b(level_hex[i + 1])).hex()
            )
        else:
            parents.append(level_hex[i])
        i += 2
    return parents


def verify_proof(record: dict, proof_obj: dict, expected_root: str) -> bool:
    """Verify that ``record`` is a member of the batch represented by
    ``expected_root`` using the supplied proof."""
    from .common import deterministic_json_bytes

    current = leaf_hash(deterministic_json_bytes(record))
    position = int(proof_obj["position"])
    for entry in proof_obj["proof"]:
        sibling = _h2b(entry["hash"])
        if entry["side"] == "left":
            current = node_hash(sibling, current)
        elif entry["side"] == "right":
            current = node_hash(current, sibling)
        else:
            raise ValueError("unknown proof side")
        position //= 2
    return current.hex() == expected_root.lower()
