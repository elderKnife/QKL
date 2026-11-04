"""Build chain clients for a named business role from ``config/system.json``."""
from __future__ import annotations

import os

from ..common.config import PROJECT_ROOT, contract_address, load_chain_evidence, resolve_path
from ..common.models import ProtocolError
from .client import ChainClient
from .keys import load_artifacts
from .registry import RootHashRegistry


def build_registry(
    config: dict, role: str, rpc_node: str | None = None
) -> tuple[ChainClient, RootHashRegistry]:
    try:
        account = config["accounts"][role]
    except KeyError as exc:
        raise ProtocolError("CONFIG_ERROR", f"unknown chain role {role!r}") from exc
    node_name = rpc_node or account["rpcNode"]
    rpc_url = config["chain"]["rpcNodes"][node_name]
    client = ChainClient(
        rpc_url,
        resolve_path(account["pem"]),
        group_id=int(config["chain"].get("groupId", 1)),
        chain_id=int(config["chain"].get("chainId", 1)),
    )
    abi, _ = load_artifacts(os.path.join(PROJECT_ROOT, "contracts", "build"))
    registry = RootHashRegistry(client, contract_address(config), abi)
    return client, registry
