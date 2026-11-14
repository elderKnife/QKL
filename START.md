# QKL V2 本地运行

本版：3 个 VPP × 每 VPP 10 个边缘 × 每边缘 30 台设备，共 900 台设备；一个 GRID。沿用本地 FISCO BCOS 2.11 的 5 节点 PBFT 网络。

新电脑先阅读 [完整部署指南](docs/07-在其他电脑上运行.md)，也可将 [AI 部署任务](AI_DEPLOY.md) 交给 AI。下面两条命令适用于已经完成首次部署的电脑。

## 日常启动

```bash
# 在本机 QKL 仓库根目录运行
./scripts/start_chain.sh
./scripts/start_services.sh
```

打开 <http://127.0.0.1:8400>。

| 角色 | 账户 | 密码 |
|---|---|---|
| GRID | `grid` | `grid-local-password` |
| VPP | `vpp` | `vpp-local-password` |

VPP 账户已可登录，在右上角切换 VPP-A、VPP-B、VPP-C，可查看数据和控制采集；当前三个 VPP 共用此登录账户。手动批量校验使用 GRID 账户。页面所有时延统一为毫秒（ms），普通时延保留 2 位小数，极短的根比较耗时保留 6 位小数；原始证据和计时字段保持原精度。

- 「实验总览」可启动或暂停全部 VPP。
- 「边缘采集与 VPP」切换 `vpp-a/b/c`，分别启动、暂停或单轮采样；所属 10 个边缘每 30 秒上报一轮，首次启动约 2 秒后产生第一轮。
- 每个边缘上报 30 条；VPP 收齐所属 10 个边缘同一轮数据后，生成 300 条业务批次。GRID 自动验真。 GRID 在本地保存验真结果供页面查询，向 VPP 的上传请求只返回 HTTP 204（空响应）；VPP 只记录投递状态，不接收验真报告。40 批聚合上传同样处理。
- 「边缘采集与 VPP」的历史记录与「国网校验工作台」都按完整业务批次展示：一行一批，10 个边缘 × 30 条 = 300 条。点击「查看详情」先看到 10 个默认收起的边缘组，再点击某个组展开该边缘的 30 条记录、根哈希及验真时延；支持筛选设备、边缘或记录 ID。
- 列表与详情显示 VPP 组批/聚合时间、GRID 接收入库时间；详情还显示原始设备采集时间（批次及边缘组显示采集时间范围）。统一为北京时间 UTC+8；设备采集、VPP 组批/聚合和 GRID 接收时间均显示到秒，悬停可查看原始时间戳。显示格式不改变原始数据、签名或根哈希。
- 同一 VPP、同一会话每满连续 40 批，自动生成一个 12000 条聚合批次并将聚合根上链；仍保留各业务批次的根哈希与交易证据。
- 使用 GRID 账户点击「批量校验」，读取 GRID 保存的全部 12000 条原始数据重算两层根；主要显示最后一次根相等比较耗时（毫秒，ms），不计重算和读链。详情单独保留重算、读链与完整处理时间。可重复校验，聚合组「查看详情」的 40 批清单可继续打开各 300 条批次。
- 暂停停止后续调度，已经开始的交易和投递会完成。恢复沿用当前会话，轮次连续；业务服务重启后默认暂停。
- GRID 登录后，左侧 WORKSPACE 新增「指令下发」。单条模式依次选择 VPP、边缘、设备，点击「发布指令」；内容自动模拟为设置有功功率（光伏 10 kW、储能 −5 kW、充电桩 7 kW）。
- 批量模式每次只针对一个 VPP，选择设备后点击「加入发布清单」，可切换其所属边缘继续添加；点击「批量发布指令」后全部指令上链一个根并等待该 VPP 核验成功。切换 VPP 会清空清单。无需等待 40 批，最多选择该 VPP 的 300 台设备。
- 指令原文直传 VPP，VPP 验签、重算根、独立查询 GRID 链上根后直接返回签名的「核验成功」。指令不下发到边缘或设备。发布时间显示到秒，GRID 收到成功的时间显示到毫秒；主要验证耗时只计最终两个根相等判断，发布至收到成功另列（均为 ms）。完整处理、重算和读链耗时可在详情展开查看，不计入主要验证耗时。该指令成功返回与设备数据上传的 HTTP 204 空响应是两个独立流程。

停止业务服务（保留业务数据和链历史）：

```bash
./scripts/stop_services.sh
# 确实需要停止链时再执行：
./scripts/stop_chain.sh
```

## 快速跑满 40 批

暂停全部自动采集后执行：

```bash
.venv/bin/python scripts/experiment_v2.py            # 三个 VPP 并行，各累计到一个完整 40 批组
# 或仅一个 VPP：
.venv/bin/python scripts/experiment_v2.py --vpp vpp-a
```

脚本使用「单轮采样」接口加速实验，仍然是实际 HTTP 数据传输和真实 FISCO 交易，不会改变自动采集的 30 秒间隔。若已有完整聚合组，脚本复用该组重验。输出 `data/reports/v2/experiment.json`，其中含实际交易哈希、区块号和各阶段计时。若要生成按新口径计时的完整聚合组，可执行 `scripts/experiment_v2.py --fresh-timing`；对应输出为 `data/reports/v2/timing-v2.json`。

上链确认以独立查询合约读到正确根和发布者为准，默认每 20 ms 查询一次，不再等待 SDK 的固定 1 秒回执轮询。仅 RPC 返回交易哈希不算成功上链。历史批次没有新口径时延时显示「历史记录未测」，原回执耗时保留在详情中；不回填推算值。

## 新环境 / 首次升级

新电脑完整步骤见 [V2 部署指南](docs/07-在其他电脑上运行.md)，包括 Python 3.10、锁定依赖、SDK 本地配置、Bash 5 建链及本平台二进制。下面是已有 Python 环境和 FISCO 链的首次升级顺序；编译可使用原生 `tools/solc` 0.6.10，或先在 `tools/solcjs` 安装 `solc@0.6.10`。Vite 7 要求 Node.js 20.19+ 或 22.12+，推荐 Node 22 LTS。

```bash
./scripts/stop_services.sh
.venv/bin/python scripts/prepare_local.py # 补齐 SDK 本地配置，不覆盖已有文件
.venv/bin/python scripts/configure_v2.py   # 已是 V2 时保持配置；首次升级归档旧配置
.venv/bin/python scripts/gen_accounts.py   # 保留已有 PEM，仅生成缺少的测试身份
./scripts/compile_contract.sh
./scripts/start_chain.sh
.venv/bin/python scripts/deploy_v2.py       # 部署新版根哈希合约并绑定 34 个测试身份
cd console-prototype/frontend
npm ci
npm run build
cd ../..
./scripts/start_services.sh
.venv/bin/python scripts/check_deployment.py # 只读自检：五节点、合约、34 个绑定、35 个服务
```

`config/chain-v2.json` 是不入库的本地部署证据，绑定当前链和账户。全新克隆不会带此文件，需要本机部署生成；已有文件会保留。迁移到新链时，先备份并明确处理密钥、部署证据、节点数据和业务数据库的配套关系，再部署，不能直接使用其他电脑的合约地址或只清理一侧继续旧会话。

## 端口与数据

| 服务 | 端口 / 路径 |
|---|---|
| edge-01～edge-30 | 8101～8130 |
| vpp-a / vpp-b / vpp-c | 8201 / 8202 / 8203 |
| GRID / Vue 控制台 | 8301 / 8400 |
| FISCO RPC | 8545～8549 |
| V2 业务数据库 | `data/v2/<角色>/pipeline.db` |
| V2 合约证据 | `config/chain-v2.json` |
| 旧配置 / 旧链证据 | `config/legacy/` / `config/chain.json` |
| 服务日志 | `logs/services/<角色>.log` |

35 个业务服务由配置动态启动；启动脚本要求所有健康检查通过。有服务在运行时会拒绝覆盖 PID 文件，应先停止再启动。前端修改后需重新 `npm run build`，后端修改后需重启业务服务。

## 验证命令

```bash
.venv/bin/python -m unittest discover -s tests -q
npm run build --prefix console-prototype/frontend
# 先运行 experiment_v2.py，再执行真实 UI 集成测试：
cd console-prototype/frontend
WEB_BASE_URL=http://127.0.0.1:8400 \
PLAYWRIGHT_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
npm run test:e2e
```

协议、哈希规则与时延定义见 [V2 实验说明](docs/10-V2三级数据验真.md)。旧 `demo_flow.py`、`feed_records.py`、`tamper_demo.py` 和旧文档是 V1 实验，不适用于 V2 采集接口。
