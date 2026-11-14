# 给 AI 的 QKL V2 部署任务

将下面这段交给有本机终端权限的 AI。先克隆 `https://github.com/elderKnife/QKL.git`，或告诉 AI 已有克隆的实际绝对路径；不需要原作者电脑上的文件。

```text
请在这台电脑部署并启动 https://github.com/elderKnife/QKL 的当前 V2 实验。
先检查操作系统、CPU架构、Python / Node / Bash / OpenSSL版本、端口及已有部署状态。
完整阅读仓库README.md、START.md和docs/07-在其他电脑上运行.md，以当前V2为准执行。

目标：3个VPP，每个10个边缘，每边缘30台设备，1个GRID；
底层为本机FISCO BCOS 2.11.0、5个PBFT节点、ECDSA、groupId=1 / chainId=1。
使用Python 3.10的.venv和requirements-lock.txt；Node推荐22.12+的22.x LTS；
建链用Bash 5+，编译器固定Solidity 0.6.10。不改成FISCO 3.x或假链。

新克隆顺序：创建venv / 安装依赖 → prepare_local.py（SDK配置） → configure_v2.py
→ gen_accounts.py（本机身份） → 下载匹配平台的FISCO → 编译合约
→ 新建并启动五节点链 → deploy_v2.py（本机合约和34个绑定）
→ npm ci / npm run build → start_services.sh → check_deployment.py。
遇到已有链、密钥、证据和数据库，先验证能否配套复用，不重复初始化。
只需要Python + Vue + FISCO，不启动console-prototype/compose.yaml的旧Go/MySQL原型。

功能保持：数据原文走HTTP，链只存根及定位元数据；边缘每30秒上报且可暂停；
VPP每批300条、GRID自动验真，40批/12000条可手动重算核验。
GRID指令每次只选一个VPP，可多选其所属边缘设备，整批一个根上链；
VPP独立读链核验后直接返回成功，不向边缘转发或执行设备动作。
普通时间戳到秒，指令成功接收时间到毫秒，时延单位ms；批量主要验证耗时只计最终根比较。

验收：35个业务服务健康；五节点共识身份一致、本机合约存在、34个发布者绑定正确；
浏览器8400能登录GRID/VPP，能生成并自动核验一批300条数据；
能完成单条和同一VPP多边缘的批量指令，确认成功返回、一个根和时间显示。
先暂停采集，再按docs/07用experiment_v2.py加速得到40批聚合组并核验12000条。
运行后端测试和前端构建；条件允许时运行Playwright集成测试。
用真实交易、根和报告证明端到端结果，单元测试链替身不算真实部署验收。

不删除或重置已有链/密钥/数据库，不用gen_accounts.py --force，不复用作者的合约地址。
需要改变已有部署状态或遇到系统安装权限时，说明具体原因后再处理。
不上传私钥、数据库或节点目录，不把演示服务暴露公网。
完成后给出实际路径、启动/停止命令、URL、登录账户、自检结果、报告位置和未完成项。
```

详细命令、平台要求和排错见 [新电脑部署指南](docs/07-在其他电脑上运行.md)。这些说明提供部署流程，不代表已在所有平台实测；遇到下载或系统依赖问题应报告实际阻塞。
