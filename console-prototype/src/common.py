"""Shared constants and deterministic conversion helpers."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone


PROJECT_ROOT = "/home/blockchain/fisco-bcos-lab"
CONSOLE_DIR = PROJECT_ROOT + "/console"
CONTRACT_NAME = "EvidenceStore"
CONTRACT_ADDRESS = "0xc8ead4b26b2c6ac14c9fd90d9684c9bc2cc40085"

# Domain separation prefixes.  Every leaf and every internal node of a Merkle
# tree is hashed together with one of these fixed prefixes so that, for example,
# a leaf that happens to equal a pair of child digests can never be confused
# with an internal node.
LEAF_DOMAIN = b"FISCO-BCOS-EVIDENCE-LEAF-V1:"
NODE_DOMAIN = b"FISCO-BCOS-EVIDENCE-NODE-V1:"

TIMEZONE = timezone.utc


def deterministic_json_bytes(obj: dict) -> bytes:
    """Serialize a JSON object with a fixed, reproducible byte representation.

    Rules:
      - keys are sorted;
      - no optional whitespace (",", ":");
      - non-ASCII characters are not escaped (ensure_ascii=False);
      - the resulting text is encoded as UTF-8.
    """
    text = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def leaf_hash(record_bytes: bytes) -> bytes:
    """Return the raw 32-byte domain-separated leaf digest."""
    return hashlib.sha256(LEAF_DOMAIN + record_bytes).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    """Return the raw 32-byte domain-separated parent digest."""
    return hashlib.sha256(NODE_DOMAIN + left + right).digest()


def utc_now_iso() -> str:
    """Return a fixed-width, timezone-qualified ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def unique_batch_id(kind: str) -> str:
    """Create a non-conflicting batch id that includes a wall-clock run marker."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{kind}-{stamp}"


def keccak256(data: bytes) -> bytes:
    """Minimal Keccak-256 (not NIST SHA3-256) used for ABI function selectors."""
    # Keccak-f[1600] permutation.
    RC = [
        0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
        0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
        0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
        0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
        0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
        0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
        0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
        0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
    ]
    ROT = [
        [0, 36, 3, 41, 18],
        [1, 44, 10, 45, 2],
        [62, 6, 43, 15, 61],
        [28, 55, 25, 21, 56],
        [27, 20, 39, 8, 14],
    ]

    def rol(x: int, n: int) -> int:
        n &= 63
        return ((x << n) | (x >> (64 - n))) & 0xFFFFFFFFFFFFFFFF

    # State is kept as 25 64-bit little-endian lanes.
    state = [0] * 25
    rate = 1088 // 8  # 136 bytes

    def absorb_block(block: bytes) -> None:
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:(i + 1) * 8], "little")
            state[i] ^= lane
        permute()

    def permute() -> None:
        a = state
        for rnd in range(24):
            c = [0] * 5
            d = [0] * 5
            for x in range(5):
                c[x] = a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20]
            for x in range(5):
                d[x] = c[(x - 1) % 5] ^ rol(c[(x + 1) % 5], 1)
            for x in range(5):
                for y in range(5):
                    a[x + 5 * y] ^= d[x]
            b = [0] * 25
            for x in range(5):
                for y in range(5):
                    b[y + 5 * ((2 * x + 3 * y) % 5)] = rol(
                        a[x + 5 * y], ROT[x][y]
                    )
            for x in range(5):
                for y in range(5):
                    a[x + 5 * y] = b[x + 5 * y] ^ (
                        (~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y]
                    )
            a[0] ^= RC[rnd]

    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != rate - 1:
        padded.append(0x00)
    padded.append(0x80)
    for i in range(0, len(padded), rate):
        absorb_block(bytes(padded[i:i + rate]))
    out = bytearray()
    for i in range(4):
        out += state[i].to_bytes(8, "little")
    return bytes(out)


def function_selector(signature: str) -> str:
    """First 4 bytes of Keccak-256(signature), formatted as 0x... ."""
    return "0x" + keccak256(signature.encode("utf-8"))[:4].hex()
