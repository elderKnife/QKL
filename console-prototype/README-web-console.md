# 双边缘节点与 GRID 实验台

## 启动

Windows PowerShell 在项目目录运行：

```powershell
docker compose -f compose.yaml up --build -d
docker compose -f compose.yaml ps
```

浏览器打开 `http://127.0.0.1:18082`。登录账号由本机 `.env` 配置：`VPP_USERNAME` / `VPP_PASSWORD` 进入 VPP 工作台，`GRID_USERNAME` / `GRID_PASSWORD` 进入 GRID 工作台。首次本地演示默认账号为 `vpp` / `vpp-local-password` 和 `grid` / `grid-local-password`；共享主机前必须在 `.env` 设置自有随机密码与 `INTERNAL_CONTROL_TOKEN`。

## 数据流

- EDGE-001 保留 5 台设备；EDGE-002 运行 30 台设备（光伏 12、储能 12、充电桩 6）。设备固定每 600 秒生成记录，每 1800 秒按 UTC 窗口封批并签名。
- Edge 暂存数据库只服务于待封批及失败重试 outbox。EDGE-001 与 EDGE-002 使用 `edge_data`、`edge2_data` 两个 schema。
- VPP 验签并保存所有 Edge 批次原文、设备记录和模拟链摘要，再通过持久化 outbox 向 GRID 发送完整批次及 `batchId`、`txHash`、`blockNumber`。
- GRID 在本地 MySQL 持久化批次及记录后确认接收，核对边缘签名和每条记录，重算 Merkle 根，并凭链定位信息向 VPP 查询摘要；每次结果及检查项追加保存。

## 数据库

Compose 提供三个独立 MySQL 服务和数据卷：`edge-mysql` 保存 Edge 暂存队列，`vpp-mysql` 保存 VPP 权威全量数据，`grid-mysql` 保存 GRID 收件数据及核验历史。现有 Edge MySQL 的 `mysql-data` 卷保留。现有旧 `vpp_data` 留在该实例，不自动导入新建的 VPP 数据库；VPP/GRID 使用各自全新数据卷。新数据库没有主机端口映射，仅在 Compose 私网可访问。

查看服务与数据库：

```powershell
docker compose -f compose.yaml ps
docker exec -it fisco-bcos-lab-handoff-20261103-mysql-1 sh -c 'mysql -uprototype -p"$MYSQL_PASSWORD"'
```

可通过各自 MySQL 容器内的 `mysql` 客户端查询 `edge_data` / `edge2_data`、`vpp_data` 或 `grid_data`。Edge 的端口映射默认为 `127.0.0.1:3307`；若本机端口不同，以 `docker compose ps` 显示为准。

## 角色工作台

- VPP 角色查看两台 Edge 的配置、收件批次、验签结果、模拟登记摘要和 GRID 转发状态。
- GRID 角色查看来源 VPP、批次定位字段、本地 Merkle 根、VPP 返回摘要及本地重新核验历史。
- Web API 后端强制校验角色会话；浏览器不接触数据库账号、Edge 私钥或内部服务凭据。登录会话在 Web 进程重启后失效。

## 当前边界

VPP 与 GRID 数据库相互独立。当前链适配器为 VPP MySQL 中的 `simulated_chain`；`txHash` 与 `blockNumber` 是模拟定位字段，不代表 FISCO BCOS 交易、共识或链上证明。`PASS` 表示 GRID 对照本地整批数据与模拟适配器返回摘要通过，不是生产级链上有效性证明。

默认停止服务可运行 `docker compose -f compose.yaml down`，不带 `-v` 不会删除数据库、记录或身份密钥。不要对现有卷执行 `down -v`。
