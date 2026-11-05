"""Thin wrapper around the official FISCO BCOS Console CLI.

Python never talks to the chain directly.  All chain reads and writes are made
through the verified Console / Java SDK 3.7.0 installation.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

from .common import CONSOLE_DIR, CONTRACT_ADDRESS, CONTRACT_NAME, PROJECT_ROOT


RUNTIME_HOME = PROJECT_ROOT + "/runtime/java-home"
NODE_CONSOLE_ROOT = PROJECT_ROOT + "/runtime/console_nodes"


def _env() -> dict:
    env = os.environ.copy()
    env["JAVA_TOOL_OPTIONS"] = f"-Duser.home={RUNTIME_HOME}"
    return env


def _ensure_node_console(port: int) -> str:
    base = os.path.join(NODE_CONSOLE_ROOT, str(port))
    conf_dir = os.path.join(base, "conf")
    os.makedirs(conf_dir, exist_ok=True)
    os.makedirs(os.path.join(base, "classes"), exist_ok=True)

    def link(name: str, target: str) -> None:
        dst = os.path.join(base, name)
        if not os.path.islink(dst) and not os.path.exists(dst):
            os.symlink(target, dst)

    link("apps", os.path.join(CONSOLE_DIR, "apps"))
    link("lib", os.path.join(CONSOLE_DIR, "lib"))
    link("account", os.path.join(CONSOLE_DIR, "account"))
    link("contracts", os.path.join(CONSOLE_DIR, "contracts"))
    for name in ("ca.crt", "sdk.crt", "sdk.key", "clog.ini"):
        src = os.path.join(CONSOLE_DIR, "conf", name)
        dst = os.path.join(conf_dir, name)
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)

    config = os.path.join(conf_dir, "config.toml")
    with open(config, "w", encoding="utf-8") as f:
        f.write(
            "[cryptoMaterial]\n\n"
            'certPath = "conf"\n'
            'disableSsl = "false"\n'
            'useSMCrypto = "false"\n'
            'caCert = "conf/ca.crt"\n'
            'sslCert = "conf/sdk.crt"\n'
            'sslKey = "conf/sdk.key"\n\n'
            "[network]\n\n"
            'messageTimeout = "10000"\n'
            'defaultGroup = "group0"\n'
            f'peers = ["127.0.0.1:{port}"]\n\n'
            "[account]\n\n"
            'keyStoreDir = "account"\n'
            'accountFileFormat = "pem"\n'
            'accountAddress = "0x309438a7cd1cf204063e08a07eb384cf7826f349"\n\n'
            "[threadPool]\n\n"
            'threadPoolSize = "16"\n'
        )
    return base


def _run_default(args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "console/console.sh", *args],
        cwd=PROJECT_ROOT,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_node(port: int, args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    base = _ensure_node_console(port)
    cmd = (
        "java -cp 'apps/*:conf/:lib/*:account/:classes/' "
        "console.NonInteractiveConsole " + " ".join(args)
    )
    return subprocess.run(
        cmd,
        cwd=base,
        env=_env(),
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _call(args: list[str], port: int | None = None) -> subprocess.CompletedProcess:
    if port is None:
        return _run_default(args)
    return _run_node(port, args)


def _extract_tx_hash(output: str) -> str:
    m = re.search(r"transaction hash:\s*(0x[0-9a-fA-F]+)", output)
    if not m:
        raise RuntimeError("transaction hash not found in console output:\n" + output)
    return m.group(1)


def _extract_status(output: str) -> int:
    m = re.search(r"transaction status:\s*(\d+)", output)
    if m:
        return int(m.group(1))
    m = re.search(r"Return code:\s*(\d+)", output)
    if m:
        return int(m.group(1))
    raise RuntimeError("status not found in console output:\n" + output)


def record_batch(
    *,
    batch_id: str,
    evidence_type: str,
    data_type: str,
    merkle_root: str,
    record_count: int,
    submitter: str,
    related_batch_id: str,
    timestamp: int,
    port: int | None = None,
) -> dict:
    args = [
        "call",
        CONTRACT_NAME,
        CONTRACT_ADDRESS,
        "recordBatch",
        batch_id,
        evidence_type,
        data_type,
        merkle_root,
        str(record_count),
        submitter,
        related_batch_id,
        str(timestamp),
    ]
    proc = _call(args, port)
    out = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0:
        raise RuntimeError("console call failed:\n" + out)
    status = _extract_status(out)
    if status != 0:
        raise RuntimeError(f"recordBatch transaction status={status}:\n" + out)
    return {"tx_hash": _extract_tx_hash(out), "status": status, "output": out}


def get_batch(batch_id: str, port: int | None = None) -> dict:
    args = [
        "call",
        CONTRACT_NAME,
        CONTRACT_ADDRESS,
        "getBatch",
        batch_id,
    ]
    proc = _call(args, port)
    out = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0:
        raise RuntimeError("console getBatch failed:\n" + out)
    status = _extract_status(out)
    if status != 0:
        raise RuntimeError(f"getBatch status={status}:\n" + out)
    m = re.search(r"Return values:\((.*)\)", out)
    if not m:
        raise RuntimeError("getBatch return values not found:\n" + out)
    values = [v.strip() for v in m.group(1).split(", ")]
    if len(values) != 8:
        raise RuntimeError(f"unexpected getBatch tuple length {len(values)}: {values}")
    keys = [
        "batchId",
        "evidenceType",
        "dataType",
        "merkleRoot",
        "recordCount",
        "submitter",
        "relatedBatchId",
        "timestamp",
    ]
    result = dict(zip(keys, values))
    result["recordCount"] = int(result["recordCount"])
    result["timestamp"] = int(result["timestamp"])
    result["raw"] = out
    return result


def get_block_number(port: int | None = None) -> int:
    proc = _call(["getBlockNumber"], port)
    out = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0:
        raise RuntimeError("getBlockNumber failed:\n" + out)
    m = re.search(r"(\d+)\s*$", out.strip().splitlines()[-1])
    # The last non-empty line is the number.  Use a targeted parse instead.
    lines = [ln for ln in out.splitlines() if ln.strip() and "error" not in ln.lower()]
    for ln in reversed(lines):
        if re.fullmatch(r"\d+", ln.strip()):
            return int(ln.strip())
    raise RuntimeError("block number not found:\n" + out)


def get_transaction_receipt(tx_hash: str, port: int | None = None) -> str:
    proc = _call(["getTransactionReceipt", tx_hash], port)
    out = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0:
        raise RuntimeError("getTransactionReceipt failed:\n" + out)
    return out


def get_block_by_number(number: int, port: int | None = None) -> str:
    proc = _call(["getBlockByNumber", str(number)], port)
    out = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0:
        raise RuntimeError("getBlockByNumber failed:\n" + out)
    return out


def get_transaction_by_hash(tx_hash: str, port: int | None = None) -> str:
    proc = _call(["getTransactionByHash", tx_hash], port)
    out = proc.stdout + "\n" + proc.stderr
    if proc.returncode != 0:
        raise RuntimeError("getTransactionByHash failed:\n" + out)
    return out


def get_peers(port: int | None = None) -> str:
    proc = _call(["getPeers"], port)
    return proc.stdout + "\n" + proc.stderr


def get_sealer_list(port: int | None = None) -> str:
    proc = _call(["getSealerList"], port)
    return proc.stdout + "\n" + proc.stderr


def get_consensus_status(port: int | None = None) -> str:
    proc = _call(["getConsensusStatus"], port)
    return proc.stdout + "\n" + proc.stderr
