# FISCO BCOS 可信存证与指令追溯实验

最小可行实验，基于真实五节点 PBFT 链。

## 目录

- `src/`：Python 实验程序，入口 `src/run_all.py`
- `contracts/`：`EvidenceStore.sol` 及编译产物 ABI/bin
- `data/`：模拟 JSON 样例（运行数据与调控指令）
- `output/`：每次运行的证据、JSON、链上输出
- `docs/`：[数据转换规范](docs/数据转换规范.md)、[导师汇报说明](docs/导师汇报说明.md)、[验收报告](docs/验收报告.md)

## 运行

```bash
cd /home/blockchain/fisco-bcos-lab
bash nodes/127.0.0.1/start_all.sh
python3 -m src.run_all A      # 或 B / all
```

链上交互经 `console/console.sh`（Console v3.7.0）完成，不使用未经验证的 Python 以太坊 Web3 库。
