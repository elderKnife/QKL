# 04 FISCO BCOS 链部署与证据

## 1. 选型与版本

| 项 | 值 |
|---|---|
| 软件 | FISCO BCOS（开源联盟链） |
| 版本 | **2.11.0**（`FISCO-BCOS Version : 2.11.0`，Build Time 20240520） |
| 许可证 | Apache-2.0 |
| 源码 | Gitee 镜像 `https://gitee.com/FISCO-BCOS/FISCO-BCOS`（GitHub 原仓库同步） |
| 共识 | PBFT，单群组 `group1`，chainId=1 |
| 节点数 | **5**（node0–node4），单机 5 个独立进程 |
| 二进制 | `chain/bin/fisco-bcos`（macOS arm64 原生，SHA 见下文） |
| 部署工具 | 官方 `build_chain.sh`（v2.11.0） |

**为什么选 2.11.0 而不是 3.x**：本机是 Apple Silicon，FISCO 2.11.0 官方提供
`fisco-bcos-macOS-aarch64` 原生二进制，且官方 Python SDK 能直接构造并签名交易；
3.x 的 Python 侧交易编码依赖 x86_64 的 C SDK 动态库，在当前机器上无法原生运行。
版本选择不影响“根哈希上链 + 独立校验”的结论，报告中应如实写明。

## 2. 建链过程（可复现）

```bash
# 1) 安装 bash 5（macOS 自带 bash 3.2 会在官方脚本中死循环）
brew install bash

# 2) 获取二进制（GitHub 直连不可用时经镜像）
curl -L -o /tmp/fisco-bcos-macOS-aarch64.tar.gz \
  https://github.com/FISCO-BCOS/FISCO-BCOS/releases/download/v2.11.0/fisco-bcos-macOS-aarch64.tar.gz
tar xzf /tmp/fisco-bcos-macOS-aarch64.tar.gz -C chain/bin/
chmod +x chain/bin/fisco-bcos

# 3) 生成 5 节点配置（-e 指定本地二进制，避免脚本联网下载）
cd chain
/opt/homebrew/bin/bash build_chain.sh -l 127.0.0.1:5 -p 30300,20200,8545 \
  -e ./bin/fisco-bcos -o ./nodes

# 4) 启动
bash nodes/127.0.0.1/start_all.sh
# 或：../scripts/start_chain.sh
```

### 对官方 `build_chain.sh` 的本地修补（必须记录）

本机 OpenSSL 为 4.1.0，官方脚本按 OpenSSL 1.1.1 的输出版本解析证书/密钥，
会导致**无限循环**或证书生成失败。`chain/build_chain.sh` 中做了两处最小修补：

| 位置 | 原逻辑 | 修补后 | 原因 |
|---|---|---|---|
| `gen_rsa_node_cert` | `openssl x509 -req … -extensions v4_req`（无 `-extfile`） | 增加 `-extfile "${cert_conf_path}"` | OpenSSL 3/4 下找不到扩展段直接失败 |
| nodeid 生成 | `openssl ec -text -noout \| sed -n '7,11p'` | `openssl ec -pubout -outform DER \| tail -c 64 \| xxd -p` | OpenSSL 4 输出行数变化，按行号解析会取错或为空 |
| 私钥长度校验 | `openssl ec -text \| sed -n '3,5p'` | `sed -n '/priv:/,/pub:/p'` 后去分隔符 | 同上，按行号解析会永远得不到 64 位 |

这些修补只影响**配置与证书生成**，不影响链逻辑与共识；已在脚本内以 `PATCH(openssl4)` 注释标注。

### 进程与端口

| 节点 | RPC | P2P | Channel | 数据目录 |
|---|---|---|---|---|
| node0 | 8545 | 30300 | 20200 | `chain/nodes/127.0.0.1/node0` |
| node1 | 8546 | 30301 | 20201 | … |
| node2 | 8547 | 30302 | 20202 | … |
| node3 | 8548 | 30303 | 20203 | … |
| node4 | 8549 | 30304 | 20204 | … |

## 3. 五节点共识证据

采集脚本：`scripts/chain_evidence.py`，输出 `data/reports/chain-evidence.json`。

实测（2026-11-04）：

```
fisco-bcos processes: 5
  node0 block=13 sealers=5 peers=5 pbft=61->62 nodeId=567f58655bd7347f...
  node1 block=13 sealers=5 peers=5 pbft=62->62 nodeId=df5ba3e5c4af2d1e...
  node2 block=13 sealers=5 peers=5 pbft=62->63 nodeId=ac9d3c8e3a24f56f...
  node3 block=13 sealers=5 peers=5 pbft=63->64 nodeId=f0744c43ce6708c4...
  node4 block=13 sealers=5 peers=5 pbft=64->65 nodeId=7c9e4c5f4520053b...
distinct sealers: 5 (all nodes agree: True)
node files match sealer list: True (5 ids)
tx cross-node: True
batch cross-node: True
```

节点 ID（`chain/nodes/127.0.0.1/node*/conf/node.nodeid`，64 字节公钥 hex）：

| 节点 | nodeId |
|---|---|
| node0 | `567f58655bd7347f9b3315aaa4b0a54ea7f06c976fc04f82745301f4a73f8a470a7d3b52614b832fc93a65ba69de68540f7e939a4d11e6448ed20e98405e2468` |
| node1 | `df5ba3e5c4af2d1e0b4d2c06ba777a7c87b2cd1e4cdb4d7edeb5043c2f5f72e98d770915c4ba006ae9ae6321bae2681b1b8a046f4e417e9c6a2212fb2e6c983a` |
| node2 | `ac9d3c8e3a24f56f78e3ace68253793d61cff54f37eddb4d10b100da496bf6311222449274694ae20a7bdbfb52448201f0b1c6e5f83d7793e8209037236d2ffa` |
| node3 | `f0744c43ce6708c4da0791635ee060c73502781881ea897ab7fd0ca1435aab0616ac01e582896eaf30b28f4712e249dd07e8816eb7e4a488c5cdeb3dd529b409` |
| node4 | `7c9e4c5f4520053bb965ea46541281da23206a487e59b22864603a9125f328717d2b95c8ec7d193d1dbe1d231d5debd6e6f66bd8910cbd2cd1761f573f0cb4ff` |

`getSealerList` 返回的 5 个地址与上表**逐字一致**，即 5 个节点都具备并实际参与共识资格；
PBFT view 持续递增，说明共识在推进（空块也会产生，见节点日志 `[CONSENSUS]`）。

原始命令：

```bash
curl -s -X POST http://127.0.0.1:8545 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"getSealerList","params":[1],"id":1}'
curl -s -X POST http://127.0.0.1:8545 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"getPbftView","params":[1],"id":1}'
```

## 4. 合约部署与授权证据

脚本：`scripts/deploy_contract.py`；输出 `config/chain.json`。

| 项 | 值 |
|---|---|
| 合约 | `RootHashRegistry`（Solidity 0.6.10） |
| 地址 | `0x84a6b44075d322c9ec76f9a68fa194b0c0258c5e` |
| 部署账户（owner） | `0xEAbc348961939247532123544dcBC7140E5465Cc`（grid） |
| 部署交易 | `0x2b778039767c516bee8137b3bf48052a9e61ea4f44fa17f5403a96d411c17247`，区块 **1** |
| 授权 vpp-a | `0x540b6573ed642db1ddab7100306b0888d475b38714baffc52c6e6d66f7efe962`，区块 **2** |
| 授权 vpp-b | `0xedffa0dab756f6c674f0641d7b406d415d80146a5c2e96d79e2561ff8d86ea85`，区块 **3** |
| VPP A 地址 | `0xAa8d4CF29a7c8f209B196c461A0Ceb5D40A3B800` |
| VPP B 地址 | `0x7D89149E6cB72E3cCda68A8142B1f842E0c6E381` |
| 线上复核 | `owner()=grid`、`authorized(vppA)=true`、`authorized(vppB)=true` |

## 5. 数据上链格式分析（重点）

> 说明：本节样例取第一轮拓扑（2 边缘 × 5 设备/包）的一个批次，用于说明格式。
> 第二轮改为 4 边缘 × 10 设备/包后，规范化、Merkle 与上链字段完全不变，
> 只是每批记录数变为 10，另有指令/回执两类单对象锚定（见 `docs/03` §6.5）。

以批次 `BATCH-PKT-eeca430426ef43d48e81` 为例，跟踪“业务数据 → 提交接口 → 链上记录 → 查询结果”。

### 5.1 业务数据长什么样

5 条记录（节选）：

```json
{"recordId":"REC-RUN-MINI-001-121647-ES-101-002","deviceId":"ES-101","deviceType":"storage",
 "metric":"active_power_kw","value":-0.046,"unit":"kW","timestamp":"2026-11-04T12:16:49.210Z"}
```

### 5.2 上链前做了什么

1. 按 `(deviceId,timestamp,recordId)` 排序；
2. 每条记录做 `CANON-JSON-V1` 规范化 → 加前缀 `FISCO-BCOS-EVIDENCE-LEAF-V1:` → SHA-256 得叶子；
3. 逐层两两合并（前缀 `FISCO-BCOS-EVIDENCE-NODE-V1:`）→ 32 字节根；
4. 为每条记录导出 proof（索引/树大小/兄弟路径）；
5. **只有根**进入链调用。

### 5.3 实际提交给链的是什么

`vpp-a` 用其交易账户对交易签名后，通过 JSON-RPC `sendRawTransaction` 提交。
交易结构是 FISCO 2.x 的 RLP 编码（`randomid, gasPrice, gasLimit, blockLimit, to, value, data, chainId, groupId, extraData` + 签名），
其中 `to` 是合约地址，`data` 是 ABI 编码的调用：

```
0x73f8c694                                                     ← registerBatch 选择器
0000…00c0                                                      ← string batchId 偏移
1630bb5364e57ff0ebe8653201b18dbc32c9a152810f52c9d1c959fd8d1ae193  ← bytes32 root（32 字节）
0000…0005                                                      ← uint64 recordCount = 5
0000…(ms)                                                      ← uint64 submittedAt
…
```

实测 `getTransactionByHash` 返回：

```
from  = 0xaa8d4cf29a7c8f209b196c461a0ceb5d40a3b800   (vpp-a)
to    = 0x84a6b44075d322c9ec76f9a68fa194b0c0258c5e   (合约)
block = 0xc (12)
input = 778 字符（≈389 字节）：选择器 + ABI 参数，根哈希在 calldata 中可见
```

### 5.4 链上实际保存什么

| 位置 | 内容 | 是否含明文业务数据 |
|---|---|---|
| 合约状态 `batches[batchId]` | `root, recordCount, submittedAt, vppId, schemaVersion` | 否 |
| 事件 `BatchRegistered` | 同上字段 | 否 |
| 交易 `input` | 上述 ABI 参数（含根） | 否 |
| 交易回执 | `status, blockNumber, gasUsed, to, logs` | 否 |
| 区块 | 交易列表与状态根 | 否 |

**结论：链上没有任何设备编号、功率数值或时间序列；只有根哈希和定位元数据。**
这意味着仅凭链无法还原业务明细，链下原文仍是必需的——这是本实验刻意验证的边界。

### 5.5 查询返回什么

- `getBatch(batchId)` → `(true, 0x1630…, 5, ts, "vpp-a", "MERKLE-V1/CANON-JSON-V1")`
- `getTransactionReceipt(txHash)` → `status=0x0`、`blockNumber`、`to=合约地址`
- 链下 envelope（VPP 提供）→ records、proofs、签名

“提交请求 / 交易 / 实际保存 / 查询返回值”四者不同：

| 概念 | 本实验中的形态 |
|---|---|
| 提交请求 | HTTP `POST /api/v1/edge-packets`（VPP 内部再发链交易） |
| 交易 | 一条 RLP 编码、签名后的 `registerBatch` 调用 |
| 实际保存 | 合约 storage 的 `BatchRecord` + 事件日志 |
| 查询返回 | `getBatch` 的结构化元组；GRID 再与离线记录比对 |

### 5.6 换一个节点查询

`chain-evidence.json` 中，同一个批次根分别通过 **node0（8545）** 与 **node4（8549）**
用两个独立 `ChainClient` 读取，根哈希完全一致；部署交易回执分别通过
node0/node3/node4 读取，`blockNumber` 相同。说明数据已由共识固化，而非单节点本地缓存。

### 5.7 修改链下原始数据之后

`scripts/tamper_demo.py` 把 `REC-RUN-MINI-001-121647-ES-101-002` 的 `value`
从 `-0.046` 改成 `6.954`，保留原证明：

- GRID 重算根 `2dec68ea…` ≠ 链上根 `1630bb53…` → `REJECTED`
- 逐条校验把失败定位到该 `recordId`（`ROOT_MISMATCH`），其余记录未被误报
- 伪造 proof 方向 → 同样 `REJECTED`

## 6. 局限与说明

1. 5 个节点跑在同一台 Mac 上（同一 Docker/主机网络栈），**不能**据此推断生产级高可用或跨机网络性能。
2. 5 个节点的证书由同一 CA 生成，属于实验环境，不是生产 PKI。
3. 链版本 2.11.0 为兼容本机 arm64 与 Python SDK 而选，不是“最新版”。
4. 本轮未做吞吐/延迟压测，任何性能结论都不适用于正式验收。
5. `chain/nodes/` 含节点私钥，已加入 `.gitignore`，不能提交到仓库。
