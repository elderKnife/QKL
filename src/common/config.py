"""Configuration loading for the validation experiment.

One JSON file (``config/system.json``) describes every logical role.  Each
service loads it at start-up. In V2 every edge and VPP has an independently
bound chain publisher; the v1 topology remains archived in config/legacy.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_CONFIG = os.path.join(PROJECT_ROOT, "config", "system.json")


class ConfigError(RuntimeError):
    pass


def load_config(path: str | None = None) -> dict[str, Any]:
    config_path = path or os.environ.get("DERT_CONFIG", DEFAULT_CONFIG)
    if not os.path.exists(config_path):
        raise ConfigError(f"config not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    config["_configPath"] = config_path
    config["_projectRoot"] = PROJECT_ROOT
    return config


def resolve_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(PROJECT_ROOT, path)


def data_path(config: dict, *parts: str) -> str:
    return resolve_path(os.path.join(config.get('pipeline', {}).get('dataRoot', 'data'), *parts))


@dataclass(frozen=True)
class EdgeSpec:
    edgeId: str
    vppId: str
    port: int
    devices: tuple[str, ...]
    signingKey: str


def edge_specs(config: dict) -> dict[str, EdgeSpec]:
    specs = {}
    for item in config["edges"]:
        specs[item["edgeId"]] = EdgeSpec(
            edgeId=item["edgeId"],
            vppId=item["vppId"],
            port=int(item["port"]),
            devices=tuple(item["devices"]),
            signingKey=item["signingKey"],
        )
    return specs


def vpp_specs(config: dict) -> dict[str, dict]:
    return {item["vppId"]: item for item in config["vpps"]}


def load_chain_evidence(config: dict) -> dict:
    path = resolve_path(config.get('chain', {}).get('evidenceFile', 'config/chain.json'))
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        evidence = json.load(handle)
    evidence["_source"] = path
    return evidence


def contract_address(config: dict) -> str:
    address = (config.get("chain", {}).get("contract", {}) or {}).get("address") or load_chain_evidence(config).get("address")
    if not address:
        deploy_script = 'deploy_v2.py' if config.get('pipeline', {}).get('version') == 2 else 'deploy_contract.py'
        raise ConfigError(f"no deployed registry address; run scripts/{deploy_script} first")
    return address
