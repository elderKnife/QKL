# 08 FISCO 控制台（Vue 界面 + 真实后端）

## 1. 它是什么

前端沿用 `console-prototype/frontend`（Vue 3 + Element Plus，来自交接包），
后端由 **`src/console/service.py`** 重写：保留原 UI 的 API 契约，但所有数据来自
真实的边缘 / VPP / GRID 服务与 FISCO BCOS 链，不再使用 Go + MySQL 模拟后端。

```
浏览器 (Vue) ── /api/... ──▶ src.console.service (:8400)
                                   │
                                   ├─ edge-01..04  (:8101-8104)
                                   ├─ vpp-a / vpp-b (:8201/8202)
                                   ├─ grid          (:8301)
                                   └─ FISCO JSON-RPC (:8548) 读交易/回执/区块
```

## 2. 启动与登录

```bash
cd console-prototype/frontend && npm ci && npx vite build   # dist 不入库
cd ../.. && ./scripts/start_services.sh                     # 启动 8 个服务（含 console）
```

打开 <http://127.0.0.1:8400>：

| 角色 | 用户名 | 密码 | 可见页面 |
|---|---|---|---|
| VPP | `vpp` | `vpp-local-password` | 运行总览、数据与批次、实验控制、VPP 工作台 |
| GRID | `grid` | `grid-local-password` | GRID 工作台 |

会话由 Flask cookie 维护，服务重启后需要重新登录。

## 3. 界面语义映射（重要）

原 UI 按“两个边缘 + 一个 VPP + 一个 GRID”设计，真实拓扑是 4 边缘 / 2 VPP：

| UI 概念 | 真实对象 | 说明 |
|---|---|---|
| `EDGE-001` | `edge-01` | 归属 `vpp-a`，10 台设备 |
| `EDGE-002` | `edge-02` | 归属 `vpp-a`，10 台设备 |
| VPP 工作台 | `vpp-a` | 展示这两个边缘及其收件批次 |
| GRID 工作台 | `grid` | 看到 `vpp-a` 和 `vpp-b` 的全部批次（edge-03/04 显示为 EDGE-003/004） |

所以 VPP 工作台只覆盖 vpp-a；vpp-b 的数据只会出现在 GRID 工作台。
这是为了保持原 UI 的双边缘逻辑，不是数据缺失。

## 4. 各页面数据来源

| 页面 | 数据 | 真实来源 |
|---|---|---|
| 运行总览 | 边缘设备数/记录数/批次状态、VPP 汇总、GRID 汇总 | edge/vpp/grid `/api/v1/state`、`/api/v1/packets`、`/api/v1/batches` |
| 数据与批次 | 批次列表、记录列表、原始包 | edge/vpp 包接口 + 明细 |
| 实验控制 | 启动/暂停/单轮采集、运行记录、投递事件 | edge `/api/v1/collection/*`、包投递结果 |
| VPP 工作台 | 边缘配置、设备、收件批次、验签与链登记 | vpp-a 包明细 + GRID 状态 |
| GRID 工作台 | 批次/记录筛选、本地与链上根、重新核验 | GRID `/api/v1/batches*`、链上 getBatch |

## 5. 日志按钮（区块链账本）

批次详情（数据页）和 GRID 批次详情新增 **日志** 按钮，调用
`GET /api/ledger/{packetId}`，展开：

1. **证书与签名**
   - 边缘 keyId、算法、**公钥（证书）**、包签名 `r‖s`；
   - VPP 交易账户、合约地址；
2. **哈希**
   - 包摘要 `packetDigest`、Merkle 根、GRID 重算根；
   - 交易哈希、区块哈希、父哈希、状态根、回执根；
   - 10 条记录各自的叶子哈希；
3. **链上记录**：`getBatch` 的 batchId / root / count / vppId / schema / 查询节点；
4. **交易回执**：status、from、to、blockNumber、gasUsed、remainGas、完整 calldata；
5. **GRID 核验检查**：9 项检查名称、结果、说明。

这些字段全部来自链上 RPC 与各服务，不使用模拟值。

## 6. 兼容 API 一览

| UI 调用 | 真实来源 |
|---|---|
| `POST /api/auth/login|logout`、`GET /api/auth/me` | Flask session（账号来自环境变量/默认值） |
| `GET /api/overview` | 汇总 edge-01/02、vpp-a、grid |
| `GET /api/edge(2)/devices` | 边缘状态 + 最新包记录 |
| `GET /api/edge/runs`、`/api/edge/events` | 采集状态、包投递结果 |
| `GET /api/{edge|edge2|vpp|grid}/batches` | 各侧批次列表（分页/筛选） |
| `GET .../batches/{id}` | 批次详情（边缘 / VPP / GRID 三种视角） |
| `GET .../batches/{id}/records|raw` | 记录与原始签名包 |
| `GET .../verifications` | GRID 核验结果与分项检查 |
| `POST /api/{edge|edge2}/simulation/start|pause|sample-once` | 边缘采集控制 |
| `POST .../verify` | 调用 GRID `/api/v1/verify` 重新核验 |
| `GET /api/ledger/{id}` | 新增：账本、证书、哈希、回执 |

## 7. 与原 UI 的差异

| 项 | 原包 | 现控制台 |
|---|---|---|
| 后端 | Go + MySQL（模拟链） | Python Flask + 真实 FISCO 服务 |
| 链 | `simulated_chain`，`realBlockchain=false` | FISCO BCOS 2.11.0 5 节点，真实交易 |
| 边缘 | EDGE-001(5) / EDGE-002(30，规划) | EDGE-001(10) / EDGE-002(10)，真实采集 |
| 登录 | Go 会话 | Flask cookie，账号 `vpp`/`grid` |
| 文案 | 模拟设备/模拟登记/MYSQL | 已改为 FISCO BCOS / 链上登记 |
| 新增 | — | **日志**按钮与账本抽屉 |

## 7.1 Merkle 根为什么在边缘视角显示“未记录”

边缘节点只负责**签名原始包**，Merkle 树是 VPP 收到包之后才构建并上链的；
原界面把“边缘保存的回执根”作为边缘视角的 Merkle 根，所以边缘侧天然为空。

现在的处理：

- 「数据与批次」边缘视角的列改为 **Merkle 根（VPP 构建）**，后端用 packetId 反查 VPP 的根后展示；
- 若包还没到 VPP（极短时间窗口），显示“尚未构树”，刷新即可；
- 批次详情里保留“边缘保存的回执根”为空，并明确提示“边缘只签名原文，不构建 Merkle 根（由 VPP 构建）”；
- 真正权威的根在 VPP 工作台「本地 Merkle 根」、GRID 工作台「本地/链上 Merkle root」和「日志」里。

## 8. 限制

1. VPP 工作台只映射 vpp-a；vpp-b 仅在 GRID 可见（保持原 UI 双边缘逻辑）。
2. GRID 批次列表接口当前最多返回 100 条（服务端 LIMIT），顶部计数来自独立 count 接口，是真实总数。
3. 控制台只监听 `127.0.0.1:8400`，账号为本地实验账号，不是生产鉴权。
4. 原 `console-prototype` 的 Docker 栈已停止，仅作为前端源码与文档保留。
5. 旧 `src/web` 面板仍保留在仓库中，但不再随 `start_services.sh` 启动。
