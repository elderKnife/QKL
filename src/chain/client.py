"""Structured FISCO BCOS chain adapter.

Every call returns a plain Python structure; callers never parse console text.
The adapter wraps the official FISCO BCOS 2.x Python SDK (vendored) and adds:

* per-instance account binding (no global "current account" leaks),
* structured receipt normalisation (``status`` hex -> success flag),
* stable error codes used by the HTTP layer.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..common.compat import add_sdk_path
from ..common.models import ProtocolError

add_sdk_path()

from client_config import client_config as SDK_CONFIG  # noqa: E402  (vendored SDK class)

# The SDK's logger creates its log directory at import time; point it at an
# absolute, existing directory before importing the client module.
_SDK_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "sdk")
os.makedirs(_SDK_LOG_DIR, exist_ok=True)
SDK_CONFIG.logdir = _SDK_LOG_DIR

from client.bcosclient import BcosClient  # noqa: E402
from client.datatype_parser import DatatypeParser  # noqa: E402

TX_SUCCESS = 0
_SDK_LOCK = threading.Lock()


@dataclass(frozen=True)
class SubmittedTransaction:
    """RPC acknowledgement, not proof that a transaction has committed."""
    tx_hash: str
    started_ns: int
    acknowledged_ns: int

    @property
    def broadcast_latency_ms(self) -> float:
        return (self.acknowledged_ns-self.started_ns)/1_000_000


@dataclass
class ChainResult:
    ok: bool
    status: str
    tx_hash: str | None
    block_number: int | None
    contract_address: str | None
    output: str | None
    raw: dict
    receipt_latency_ms: float | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "txHash": self.tx_hash,
            "blockNumber": self.block_number,
            "contractAddress": self.contract_address,
            "output": self.output,
            "receiptLatencyMs": self.receipt_latency_ms,
        }


def _hex_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return None


class ChainClient:
    """One chain client bound to one RPC endpoint and one transaction account."""

    def __init__(self, rpc_url: str, account_pem: str, group_id: int = 1, chain_id: int = 1, log_dir: str | None = None):
        self.rpc_url = rpc_url
        self.account_pem = os.path.abspath(account_pem)
        if not os.path.exists(self.account_pem):
            raise FileNotFoundError(f"chain account not found: {self.account_pem}")
        self.group_id = group_id
        self._lock = threading.RLock()
        log_dir = log_dir or os.path.join(os.path.dirname(account_pem), "..", "..", "logs", "sdk")
        os.makedirs(log_dir, exist_ok=True)

        with _SDK_LOCK:
            SDK_CONFIG.remote_rpcurl = rpc_url
            SDK_CONFIG.client_protocol = "rpc"
            SDK_CONFIG.account_keyfile_path = os.path.dirname(self.account_pem)
            SDK_CONFIG.account_keyfile = os.path.basename(self.account_pem)
            SDK_CONFIG.account_password = ""
            SDK_CONFIG.groupid = group_id
            SDK_CONFIG.fiscoChainId = chain_id
            SDK_CONFIG.logdir = os.path.abspath(log_dir)
            self._client = BcosClient()
            # Bind the account immediately, before another client mutates the
            # global config object.
            self._client.load_default_account()

    # -- read-only helpers -------------------------------------------------
    def close(self) -> None:
        try:
            self._client.finish()
        except Exception:  # noqa: BLE001 - best effort shutdown
            pass

    def address(self) -> str:
        return self._client.default_from_account_signer.get_keypair().address

    def block_number(self) -> int:
        with self._lock:
            return self._client.getBlockNumber()

    def node_version(self) -> dict:
        with self._lock:
            return self._client.getNodeVersion()

    def pbft_view(self) -> int:
        with self._lock:
            return self._client.getPbftView()

    def sealer_list(self) -> list[str]:
        with self._lock:
            return self._client.getSealerList()

    def get_receipt(self, tx_hash: str) -> dict:
        with self._lock:
            try:
                return self._client.getTransactionReceipt(tx_hash)
            except Exception as exc:
                raise ProtocolError('CHAIN_UNAVAILABLE', f'read receipt failed: {exc}', retryable=True) from exc

    # -- contract interaction ---------------------------------------------
    def deploy(self, abi: list, bytecode: str, args: list | None = None) -> ChainResult:
        with self._lock:
            try:
                receipt = self._client.deploy(bytecode, abi, args or [])
            except Exception as exc:  # noqa: BLE001
                raise ProtocolError("CHAIN_UNAVAILABLE", f"deploy failed: {exc}", retryable=True) from exc
        return self._normalise(receipt)

    def send(self, to_address: str, abi: list, fn_name: str, args: list | None = None) -> ChainResult:
        with self._lock:
            started = time.perf_counter_ns()
            try:
                receipt = self._client.sendRawTransactionGetReceipt(to_address, abi, fn_name, args or [])
            except Exception as exc:  # noqa: BLE001
                raise ProtocolError("CHAIN_UNAVAILABLE", f"send {fn_name} failed: {exc}", retryable=True) from exc
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
        result = self._normalise(receipt)
        result.receipt_latency_ms = elapsed
        return result

    def broadcast(self, to_address: str, abi: list, fn_name: str, args: list | None = None) -> SubmittedTransaction:
        """Submit once without the SDK's fixed one-second receipt polling sleep.

        Callers must independently establish committed state before reporting
        success; receiving a transaction hash is only RPC acknowledgement.
        """
        with self._lock:
            started = time.perf_counter_ns()
            try:
                tx_hash = self._client.sendRawTransaction(to_address, abi, fn_name, args or [])
            except Exception as exc:
                raise ProtocolError('TX_OUTCOME_UNKNOWN', f'broadcast {fn_name} outcome unknown: {exc}', retryable=True) from exc
            acknowledged = time.perf_counter_ns()
        if not isinstance(tx_hash, str) or re.fullmatch(r'0x[0-9a-fA-F]{64}', tx_hash) is None:
            raise ProtocolError('TX_OUTCOME_UNKNOWN', 'submission returned no valid transaction hash', retryable=True)
        return SubmittedTransaction(tx_hash, started, acknowledged)

    def call(self, to_address: str, abi: list, fn_name: str, args: list | None = None) -> Any:
        with self._lock:
            try:
                return self._client.call(to_address, abi, fn_name, args or [])
            except Exception as exc:  # noqa: BLE001
                raise ProtocolError("CHAIN_UNAVAILABLE", f"call {fn_name} failed: {exc}", retryable=True) from exc

    @staticmethod
    def _normalise(receipt: Any) -> ChainResult:
        if not isinstance(receipt, dict):
            raise ProtocolError("TX_OUTCOME_UNKNOWN", f"unexpected receipt: {receipt!r}")
        status_raw = receipt.get("status")
        try:
            status_code = int(status_raw, 16) if isinstance(status_raw, str) else int(status_raw)
        except (TypeError, ValueError):
            status_code = -1
        ok = status_code == TX_SUCCESS
        output = receipt.get("output")
        if isinstance(output, str) and len(output) > 512:
            output = output[:512]
        return ChainResult(
            ok=ok,
            status="CONFIRMED" if ok else "FAILED",
            tx_hash=receipt.get("transactionHash"),
            block_number=_hex_int(receipt.get("blockNumber")),
            contract_address=receipt.get("contractAddress") or None,
            output=output,
            raw=receipt,
        )
