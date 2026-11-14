# QKL · 分布式能源根哈希验真实验（V2）

3 个虚拟电厂 VPP，每个 VPP 10 个边缘节点，每个边缘 30 台设备；一个国网 GRID 平台。900 台设备均为本地模拟，不引入 A1 终端。

| 层级 | 数据传输和验真 |
|---|---|
| 边缘 → VPP | 每 30 秒生成 30 条原始设备记录；边缘计算根并提交 FISCO；原始数据经 HTTP 直传 VPP；VPP 验签、独立读链、重算根验真 |
| VPP → GRID | 收齐所属 10 个边缘同一轮数据后形成 300 条批次；VPP 根上链、原始数据 HTTP 直传 GRID；GRID 自动验真 |
| 40 批聚合 | 每 VPP 独立将同一会话的连续 40 个 300 条根连同批次标识构成第二层 Merkle 树，聚合根上链；GRID 手动重算 12000 条原始数据及两层树后读链比对 |
| 时延 | 上链确认时延计至合约读到匹配根及发布者；手动批量校验仅计最终两根相等比较（ms），重算、读链与总处理时间另存详情 |
| GRID → VPP 指令 | 每次选择一个 VPP，可多选其所属边缘和设备；模拟指令整批上链一个根，原文 HTTP 直传；VPP 独立读链核验并直接返回签名的核验成功，主要验证耗时只计最终根比较 |
| 控制台 | 三个 VPP 分别选择和控制、全部启停、单轮采样、批次分页、每批 300 条记录表格与筛选、40 批清单下钻、根哈希与签名证据详情 |
| 区块链 | 本地 FISCO BCOS 2.11，5 个 PBFT 节点；`RootHashRegistryV2` 绑定实际交易账户与发布主体，不允许覆盖同一批次 |

链上保存根哈希和定位所需元数据（批次 ID、记录数、主体、时间、协议版本）。设备原始值、完整批次和签名通过 HTTP 传输并保存在各自业务数据库中。

打开 <http://127.0.0.1:8400>；日常启动、账户和端口见 [START.md](START.md)。新电脑先按 [完整部署指南](docs/07-在其他电脑上运行.md) 安装依赖、生成本机身份和部署合约；可直接把 [AI_DEPLOY.md](AI_DEPLOY.md) 的任务交给 AI 执行。

```bash
# 在已经完成首次部署的 QKL 仓库根目录运行
./scripts/start_chain.sh
./scripts/start_services.sh
```

GRID 演示账户 `grid / grid-local-password`；VPP 演示账户 `vpp / vpp-local-password`。两者均可查看实验和控制采集，GRID 账户可执行手动批量校验和指令发布。

```bash
# 自动采集暂停后，加速跑满三个 VPP 各 40 批并实际校验
.venv/bin/python scripts/experiment_v2.py
.venv/bin/python -m unittest discover -s tests -q
```

## 代码位置

- `src/pipeline/`：V2 边缘、VPP、GRID 业务与 HTTP API，独立 SQLite 状态、签名、链根和回执校验。
- `src/chain/`：实际 FISCO SDK 适配，交易回执和实际提交耗时。
- `contracts/RootHashRegistryV2.sol`：根存证合约与账户主体绑定。
- `config/system.json`：3×10×30 拓扑、端口、可信公钥、账户配置。
- `console-prototype/frontend/`：Vue 3 / Element Plus / TypeScript 前端。
- `scripts/experiment_v2.py`：真实链端到端实验，报告保存在 `data/reports/v2/`。
- `tests/test_pipeline.py`：V2 行为和篡改检测测试；单元测试使用明确的链替身，真实链证据由实验脚本产生。

[V2 协议与实验说明](docs/10-V2三级数据验真.md) 详细定义批次、哈希和耗时口径。V2 入口提供数据上传验真，以及 GRID → VPP 指令发布与核验成功返回；指令流程到 VPP 为止。V1 源码和旧协议保留；V2 使用独立的 `data/v2/` 和本机 `config/chain-v2.json`。`docs/07` 已更新为 V2 部署指南，其余 `docs/01～09` 保留早期背景，以 V2 文档为准。

推荐 Python 3.10、Node.js 22.12+ 的 22.x LTS、Bash 5+、FISCO 2.11.0 和 Solidity 0.6.10。当前 V2 不需要旧原型的 Go/MySQL/Docker。测试私钥、链节点数据、本机 V2 部署证据、编译产物、前端构建产物和实验数据不入 Git；新电脑首次运行时生成。
