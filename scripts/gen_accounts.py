#!/usr/bin/env python
"""Generate the test-only business keys and keep config/system.json in sync.

All keys produced here are **test identities**: they live under config/keys,
are excluded from version control and must never be used outside this lab.
Existing keys are kept (re-running is idempotent) unless --force is given.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from ecdsa import SECP256k1, SigningKey

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from src.common.compat import add_sdk_path  # noqa: E402

add_sdk_path()
from eth_account.account import Account  # noqa: E402

ROLES = ["grid", "vpp-a", "vpp-b", "edge-01", "edge-02", "edge-03", "edge-04"]


def ensure_key(name: str, force: bool) -> str:
    path = os.path.join(PROJECT_ROOT, "config", "keys", f"{name}.pem")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if force or not os.path.exists(path):
        key = SigningKey.generate(curve=SECP256k1)
        with open(path, "wb") as handle:
            handle.write(key.to_pem())
        os.chmod(path, 0o600)
        print(f"generated {path}")
    return path


def public_key(path: str) -> str:
    with open(path, "r", encoding="ascii") as handle:
        key = SigningKey.from_pem(handle.read())
    return key.get_verifying_key().to_string("uncompressed").hex()


def address(path: str) -> str:
    with open(path, "r", encoding="ascii") as handle:
        key = SigningKey.from_pem(handle.read())
    return Account.from_key(key.to_string()).address


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="regenerate keys (invalidates existing evidence)")
    args = parser.parse_args()

    keys = {role: ensure_key(role, args.force) for role in ROLES}
    pub = {role: public_key(path) for role, path in keys.items()}

    config_path = os.path.join(PROJECT_ROOT, "config", "system.json")
    with open(config_path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    for vpp in config["vpps"]:
        for edge_id, trusted in vpp["trustedEdges"].items():
            trusted["publicKey"] = pub[edge_id]
    for vpp_id, trusted in config["grid"]["trustedVpps"].items():
        trusted["publicKey"] = pub[vpp_id]
    for vpp in config["vpps"]:
        vpp.setdefault("trustedGrid", {"gridId": "grid", "keyId": "grid-key-v1"})
        vpp["trustedGrid"]["keyId"] = "grid-key-v1"
        vpp["trustedGrid"]["publicKey"] = pub["grid"]
    for edge in config["edges"]:
        edge.setdefault("trustedGrid", {"gridId": "grid", "keyId": "grid-key-v1"})
        edge["trustedGrid"]["keyId"] = "grid-key-v1"
        edge["trustedGrid"]["publicKey"] = pub["grid"]
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)

    print("\nrole        address                                      keyId")
    for role in ROLES:
        print(f"{role:10s}  {address(keys[role])}  {role}-key-v1")
    print(f"\nconfig updated: {config_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
