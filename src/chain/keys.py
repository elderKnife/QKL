"""Chain account helpers (FISCO BCOS transaction accounts).

Business signing identities (edges, VPP packet signatures) and chain
transaction accounts are deliberately separate: only the grid platform and the
two VPPs hold chain accounts for this experiment; edges never do.
"""
from __future__ import annotations

import json
import os

from ecdsa import SECP256k1, SigningKey

from ..common.compat import add_sdk_path


def _account_module():
    add_sdk_path()
    from eth_account.account import Account  # type: ignore

    return Account


def private_key_from_pem(path: str) -> bytes:
    if not os.path.exists(path):
        raise FileNotFoundError(f"account key not found: {path}")
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(f"account key {path} must not be readable by group/others (mode {oct(mode)})")
    with open(path, "r", encoding="ascii") as handle:
        pem = handle.read()
    key = SigningKey.from_pem(pem)
    if key.curve != SECP256k1:
        raise ValueError(f"{path} is not a secp256k1 key")
    return key.to_string()


def address_from_pem(path: str) -> str:
    account = _account_module()
    return account.from_key(private_key_from_pem(path)).address


def load_artifacts(contract_dir: str, name: str = "RootHashRegistry") -> tuple[list, str]:
    abi_path = os.path.join(contract_dir, f"{name}.abi")
    bin_path = os.path.join(contract_dir, f"{name}.bin")
    if not os.path.exists(abi_path) or not os.path.exists(bin_path):
        raise FileNotFoundError(f"compiled artifacts missing under {contract_dir}; run scripts/compile_contract.sh")
    with open(abi_path, "r", encoding="utf-8") as handle:
        abi = json.load(handle)
    with open(bin_path, "r", encoding="ascii") as handle:
        bytecode = handle.read().strip()
    if not bytecode or not all(char in "0123456789abcdefABCDEF" for char in bytecode):
        raise ValueError("contract bytecode is not valid hex")
    return abi, bytecode
