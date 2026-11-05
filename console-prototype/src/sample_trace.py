"""Build a single-record end-to-end conversion trace and write it to Markdown."""

from __future__ import annotations

import glob
import json
import os
import re

from . import chain
from .common import (
    CONTRACT_ADDRESS,
    CONTRACT_NAME,
    LEAF_DOMAIN,
    NODE_DOMAIN,
    deterministic_json_bytes,
    function_selector,
    keccak256,
    leaf_hash,
)
from .merkle import compute_root, generate_proof, verify_proof


SIGNATURE = "recordBatch(string,string,string,string,uint256,string,string,uint256)"


def _left_pad_hex(value: int, width: int = 32) -> str:
    return value.to_bytes(width, "big").hex()


def _pad_bytes(data: bytes) -> bytes:
    padded = data + b"\x00" * ((32 - len(data) % 32) % 32)
    return padded


def abi_encode_record_batch(args: list) -> str:
    """Encode recordBatch(...) exactly as Solidity ABI expects."""
    selector = keccak256(SIGNATURE.encode("utf-8"))[:4]
    head_size = 32 * len(args)
    dynamic = []
    head = []
    offset = head_size
    for arg in args:
        if isinstance(arg, str):
            head.append(_left_pad_hex(offset))
            data = _pad_bytes(arg.encode("utf-8"))
            dynamic.append(_left_pad_hex(len(arg.encode("utf-8"))) + data.hex())
            offset += 32 + len(data)
        elif isinstance(arg, int):
            head.append(_left_pad_hex(arg))
        else:
            raise TypeError(type(arg))
    return "0x" + selector.hex() + "".join(head) + "".join(dynamic)


def _latest_experiment_a() -> str:
    dirs = sorted(glob.glob("output/experiment-A-*/evidence.json"))
    if not dirs:
        raise RuntimeError("no experiment A evidence found")
    return os.path.dirname(dirs[-1])


def run(run_dir: str | None = None) -> dict:
    run_dir = run_dir or _latest_experiment_a()
    evidence_path = os.path.join(run_dir, "evidence.json")
    evidence = json.load(open(evidence_path, encoding="utf-8"))

    records_path = os.path.join(run_dir, "edge_records.json")
    records = json.load(open(records_path, encoding="utf-8"))["records"]

    chosen_id = "EDGE-BESS-001"
    chosen = next(r for r in records if r["recordId"] == chosen_id)
    root = compute_root(records)
    proof = generate_proof(records, chosen_id)

    # Actual on-chain transaction input.
    tx_hash = evidence["transaction"]["txHash"]
    tx_raw = chain.get_transaction_by_hash(tx_hash, port=20200)
    m_input = re.search(r"input='(0x[0-9a-fA-F]+)'", tx_raw)
    actual_input = m_input.group(1) if m_input else None

    batch = evidence["batchId"]
    ts = evidence["transaction"]["blockNumber"]  # placeholder replaced below
    # Use the actual on-chain record from the evidence for exact function args.
    read = evidence["nodeQueries"]["20200"]
    args = [
        read["batchId"],
        read["evidenceType"],
        read["dataType"],
        read["merkleRoot"],
        int(read["recordCount"]),
        read["submitter"],
        read["relatedBatchId"],
        int(read["timestamp"]),
    ]
    computed_input = abi_encode_record_batch(args)

    read_20200 = chain.get_batch(batch, port=20200)
    read_20202 = chain.get_batch(batch, port=20202)
    proof_ok = verify_proof(chosen, proof, root)

    leaf_preimage = LEAF_DOMAIN + deterministic_json_bytes(chosen)
    trace = {
        "record": chosen,
        "deterministicJsonText": deterministic_json_bytes(chosen).decode("utf-8"),
        "utf8BytesHex": deterministic_json_bytes(chosen).hex(),
        "leafPreimageHex": leaf_preimage.hex(),
        "leafSha256": leaf_hash(deterministic_json_bytes(chosen)).hex(),
        "allLeafDigests": [
            leaf_hash(deterministic_json_bytes(r)).hex()
            for r in sorted(records, key=lambda r: r["recordId"])
        ],
        "merkleRoot": root,
        "merkleProof": proof,
        "proofVerified": proof_ok,
        "contract": {
            "name": CONTRACT_NAME,
            "address": CONTRACT_ADDRESS,
            "function": SIGNATURE,
            "selector": function_selector(SIGNATURE),
            "arguments": args,
            "computedAbiEncoding": computed_input,
            "actualTransactionInput": actual_input,
            "abiMatches": computed_input.lower() == (actual_input or "").lower(),
        },
        "transaction": {
            "hash": tx_hash,
            "status": evidence["transaction"]["status"],
            "blockNumber": evidence["transaction"]["blockNumber"],
            "receipt": evidence["transaction"]["receipt"],
        },
        "onChainQuery": {
            "20200": read_20200,
            "20202": read_20202,
        },
    }
    write_trace(run_dir, trace, tx_raw)
    return trace


def write_trace(run_dir: str, trace: dict, tx_raw: str) -> None:
    json_path = os.path.join(run_dir, "single_record_trace.json")
    md_path = os.path.join(run_dir, "single_record_trace.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)
        f.write("\n")

    chosen = trace["record"]
    c = trace["contract"]
    md = f"""# 单条数据完整转换过程（{chosen['recordId']}）

## 1. 原始 JSON

输入：EDGE 采集的业务对象（字段、类型、单位、时间格式固定）。
转换规则：无，业务侧按固定 schema 生成。
输出：Python dict，等价 JSON 如下。

```json
{json.dumps(chosen, ensure_ascii=False, indent=2)}
```

## 2. 确定性 UTF-8 字节

输入：上一步 dict。
转换规则：`json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 后 `.encode("utf-8")`。
输出（确定性 JSON 文本）：

`{trace['deterministicJsonText']}`

输出（十六进制 UTF-8 字节）：

`{trace['utf8BytesHex']}`

保存位置：`output/<run_id>/edge_records.json` 与 `single_record_trace.json`。

## 3. SHA-256 叶子哈希

输入：上一步 UTF-8 字节。
转换规则：`SHA256(LEAF_DOMAIN + utf8_bytes)`，域前缀为
`{LEAF_DOMAIN.decode()}`。
叶子预映像十六进制：

`{trace['leafPreimageHex']}`

输出叶子哈希：

`{trace['leafSha256']}`

说明：叶子哈希是带域前缀的 SHA-256，不是普通 SHA-256。

## 4. Merkle 证明与批次根

输入：按 `recordId` 升序排序后的 3 条记录叶子。
叶子顺序：

{chr(10).join('- ' + h for h in trace['allLeafDigests'])}

父节点规则：`SHA256(NODE_DOMAIN + left + right)`；奇数层最后一个节点直接提升。
输出批次根：

`{trace['merkleRoot']}`

成员证明（记录 {chosen['recordId']}）：

```json
{json.dumps(trace['merkleProof'], ensure_ascii=False, indent=2)}
```

重新计算验证：`verify_proof(...)` = {trace['proofVerified']}。

## 5. 智能合约函数参数

合约：`{c['name']}`，地址 `{c['address']}`。
函数：`{c['function']}`。
实参列表：

```json
{json.dumps(c['arguments'], ensure_ascii=False, indent=2)}
```

## 6. ABI 编码结果

函数选择器（Keccak-256 前 4 字节）：`{c['selector']}`。
完整 ABI 编码（本地按 Solidity 规则计算）：

`{c['computedAbiEncoding']}`

链上交易实际 input：

`{c['actualTransactionInput']}`

两者一致：{c['abiMatches']}。

## 7. 真实交易哈希与成功回执

交易哈希：`{trace['transaction']['hash']}`
状态：{trace['transaction']['status']}
所在区块：{trace['transaction']['blockNumber']}
回执：

```json
{trace['transaction']['receipt']}
```

## 8. 链上查询结果

节点 20200：

```json
{json.dumps(trace['onChainQuery']['20200'], ensure_ascii=False, indent=2)}
```

节点 20202：

```json
{json.dumps(trace['onChainQuery']['20202'], ensure_ascii=False, indent=2)}
```

## 9. 重新计算并验证

本地按同一规则重新计算批次根 `{trace['merkleRoot']}`，并用第 4 步证明验证记录
{chosen['recordId']} 属于该批次，结果为 {trace['proofVerified']}。
"""
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)


if __name__ == "__main__":
    import sys

    run_dir_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(run_dir_arg)
    print("trace written")
