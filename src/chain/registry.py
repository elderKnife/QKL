"""Typed wrapper around the RootHashRegistry contract."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.models import ProtocolError
from .client import ChainClient, ChainResult, SubmittedTransaction


@dataclass(frozen=True)
class ChainBatch:
    batch_id: str
    root: str
    record_count: int
    submitted_at: int
    vpp_id: str
    schema_version: str
    exists: bool

    def to_dict(self) -> dict:
        return {
            "batchId": self.batch_id,
            "rootHash": self.root,
            "recordCount": self.record_count,
            "submittedAt": self.submitted_at,
            "vppId": self.vpp_id,
            "schemaVersion": self.schema_version,
            "exists": self.exists,
        }


class RootHashRegistry:
    """Only root hashes (plus locating metadata) are written to the chain."""

    def __init__(self, client: ChainClient, address: str, abi: list):
        if not address or not address.startswith("0x") or len(address) != 42:
            raise ProtocolError("CONFIG_ERROR", f"invalid contract address {address!r}")
        self.client = client
        self.address = address
        self.abi = abi

    def register_batch(
        self,
        batch_id: str,
        root_hex: str,
        record_count: int,
        submitted_at: int,
        vpp_id: str,
        schema_version: str,
    ) -> ChainResult:
        if len(root_hex) != 64:
            raise ProtocolError("INVALID_SCHEMA", "root must be 64 hex chars")
        return self.client.send(
            self.address,
            self.abi,
            "registerBatch",
            [batch_id, "0x" + root_hex, int(record_count), int(submitted_at), vpp_id, schema_version],
        )

    def get_batch(self, batch_id: str) -> ChainBatch | None:
        result: Any = self.client.call(
            self.address,
            self.abi,
            "getBatch",
            [batch_id],
        )
        if not isinstance(result, (list, tuple)) or len(result) != 6:
            raise ProtocolError("CHAIN_UNAVAILABLE", f"unexpected getBatch result: {result!r}", retryable=True)
        exists, root, count, submitted_at, vpp_id, schema = result
        if not exists:
            return None
        root_hex = root.hex() if isinstance(root, (bytes, bytearray)) else str(root).removeprefix("0x")
        return ChainBatch(
            batch_id=batch_id,
            root=root_hex,
            record_count=int(count),
            submitted_at=int(submitted_at),
            vpp_id=str(vpp_id),
            schema_version=str(schema),
            exists=True,
        )

    def broadcast_batch(self, batch_id: str, root_hex: str, record_count: int,
                        submitted_at: int, subject_id: str, schema: str) -> SubmittedTransaction:
        if len(root_hex) != 64:
            raise ProtocolError('INVALID_SCHEMA', 'root must be 64 hex chars')
        return self.client.broadcast(self.address, self.abi, 'registerBatch',
                                     [batch_id, '0x'+root_hex, int(record_count), int(submitted_at), subject_id, schema])

    def set_authorized(self, account: str, allowed: bool) -> ChainResult:
        return self.client.send(self.address, self.abi, "setAuthorized", [account, bool(allowed)])

    def owner(self) -> str:
        value = self.client.call(self.address, self.abi, "owner", [])
        return str(value[0]) if isinstance(value, (list, tuple)) else str(value)

    def is_authorized(self, account: str) -> bool:
        value = self.client.call(self.address, self.abi, "authorized", [account])
        return bool(value[0]) if isinstance(value, (list, tuple)) else bool(value)
