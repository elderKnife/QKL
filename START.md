# 启动指南（Quick Start）

> 详细分平台步骤、下载地址、常见问题见 [docs/07-在其他电脑上运行.md](docs/07-在其他电脑上运行.md)。
> 前后端分离开发见 [docs/09-前后端分离与两人协作.md](docs/09-前后端分离与两人协作.md)。

## 1. 本机已经部署过（最快）

```bash
cd /Users/zyd/PycharmProjects/QKL
./scripts/start_chain.sh          # 5 个 FISCO 节点，8545–8549
./scripts/start_services.sh       # edge×4 + vpp×2 + grid + console，8101–8400
```

打开 <http://127.0.0.1:8400>：

| 角色 | 用户名 | 密码 |
|---|---|---|
| VPP | `vpp` | `vpp-local-password` |
| GRID | `grid` | `grid-local-password` |

登录后进入「实验控制」→「开始连续采集」，每 10 秒 4 个边缘各上报 10 条记录。
批次详情 →「日志」可看证书、哈希、链上记录和交易回执。

停止：

```bash
./scripts/stop_services.sh
./scripts/stop_chain.sh
```

## 2. 全新机器从零启动

仓库**不包含**私钥、链节点数据、FISCO/solc 二进制和前端 `dist`，需要自己生成/下载。

### 2.1 依赖

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | 3.10 | 后端与脚本 |
| Node.js / npm | ≥18 | 构建 Vue 控制台 |
| Bash | 5.x | `build_chain.sh`；macOS 自带 3.2 会卡住 |
| OpenSSL | 1.1/3.x | 可用 Homebrew 版本 |

macOS 建议：

```bash
brew install bash node python@3.10
```

### 2.2 克隆与 Python 环境

```bash
git clone https://github.com/elderKnife/QKL.git
cd QKL
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2.3 生成测试身份

```bash
.venv/bin/python scripts/gen_accounts.py
```

会在 `config/keys/` 生成边缘/VPP/GRID 的 ECDSA 密钥，并把公钥写回 `config/system.json`。

### 2.4 下载 FISCO 与 solc

```bash
mkdir -p chain/bin tools
# macOS Apple Silicon
curl -L -o /tmp/fisco.tar.gz \
  https://github.com/FISCO-BCOS/FISCO-BCOS/releases/download/v2.11.0/fisco-bcos-macOS-aarch64.tar.gz
tar -xzf /tmp/fisco.tar.gz -C chain/bin
chmod +x chain/bin/fisco-bcos

# solc 0.6.10：放到 tools/solc 并 chmod +x
# 具体下载地址/其他平台见 docs/07
chain/bin/fisco-bcos --version    # 应显示 2.11.0
tools/solc --version              # 应显示 0.6.10
```

### 2.5 编译合约

```bash
./scripts/compile_contract.sh
```

### 2.6 建链（只需一次）

```bash
cd chain
/opt/homebrew/bin/bash build_chain.sh -l 127.0.0.1:5 -p 30300,20200,8545 \
  -e ./bin/fisco-bcos -o ./nodes
cd ..
./scripts/start_chain.sh
```

看到 5 个节点的 `getBlockNumber` 响应即成功。

### 2.7 部署合约

```bash
.venv/bin/python scripts/deploy_contract.py
```

会写入 `config/chain.json`（合约地址、部署交易、端口）。

### 2.8 构建控制台前端

`dist/` 不入库，必须构建一次，否则 8400 打开是空白：

```bash
cd console-prototype/frontend
npm ci
npm run build          # vue-tsc + vite build
cd ../..
```

### 2.9 启动服务

```bash
./scripts/start_services.sh
```

打开 <http://127.0.0.1:8400>，用上面的 VPP/GRID 账号登录。

## 3. 开发模式（前后端分开调试）

```bash
# 终端 A：链 + 领域服务（edge/vpp/grid/console）
./scripts/start_chain.sh
./scripts/start_services.sh

# 终端 A：需要改后端时，用热重载接管 8400
./scripts/dev_console.sh

# 终端 B：前端
cd console-prototype/frontend
npm ci
npm run dev            # http://127.0.0.1:5173，/api 代理到 8400
```

跨机器时：

```bash
# 后端
CONSOLE_HOST=0.0.0.0 ./scripts/dev_console.sh
# 前端
VITE_API_TARGET=http://<后端IP>:8400 npm run dev -- --host 0.0.0.0
```

## 4. 端口

| 服务 | 端口 |
|---|---|
| edge-01..04 | 8101–8104 |
| vpp-a / vpp-b | 8201 / 8202 |
| grid | 8301 |
| console / Vue 页面 | 8400 |
| FISCO RPC | 8545–8549 |
| P2P / Channel | 30300–30304 / 20200–20204 |
| Vite dev（可选） | 5173 |

## 5. 常见问题

| 现象 | 处理 |
|---|---|
| 8400 页面空白/404 | 前端没构建：`cd console-prototype/frontend && npm run build` |
| `build_chain.sh` 卡在 Generating keys | 用 Bash 5：`/opt/homebrew/bin/bash build_chain.sh ...` |
| `deploy_contract.py` 连不上 | 链没起：先 `./scripts/start_chain.sh` |
| 登录后马上退出 | console 重启过，Session 失效，重新登录 |
| 端口被占用 | `lsof -nP -iTCP:<port> -sTCP:LISTEN` |
| 想清空本地业务数据 | `./scripts/reset_local_state.sh`（不清链上历史） |

## 6. 验证

```bash
.venv/bin/python -m unittest discover -s tests     # 75 项
.venv/bin/python scripts/demo_flow.py --rounds 1   # 端到端演示
.venv/bin/python scripts/chain_evidence.py         # 链上证据
```
