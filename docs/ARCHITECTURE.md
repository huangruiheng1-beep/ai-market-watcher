# 架构与文件说明

## 一句话架构

```text
股票池分批行情 → 4 个扫描器生成当日证据 → 状态链跨日维护 → HTML 查看/回放 → AI 解释
```

## 核心文件

| 文件 | 用途 |
|---|---|
| `nd100_resonance_scanner.py` | 多周期共振扫描器 |
| `five_rankings_daily.py` | 五榜单日报 |
| `divergence_td9_scanner.py` | 背离 + TD9 观察 |
| `skdj_scanner.py` | SKDJ 观察池 |
| `status_chain_tracker.py` | 底部状态追踪、SQLite 和 HTML 报告 |
| `status_chain_ingest.py` | 工具1–4 CSV 转状态信号包 |
| `status_chain_rules.py` | 状态、转移和候选阈值 |
| `run_demo.py` | 无 Key 离线一键演示 |
| `run_live.py` | 本地私有 Key 的分批真实行情入口；默认每批 50 只，路演可显式缩小 |

## 公开与本地边界

| 类型 | 可上传 GitHub | 说明 |
|---|---:|---|
| 核心代码、测试、README | 是 | 公开试用所需 |
| `demo_data/` | 是 | 精选的离线演示样例 |
| `.env.example` | 是 | 只有占位符，没有 Key |
| `.env`、`credentials/` | 否 | 用户本地凭据 |
| `cache/`、`output/`、SQLite | 否 | 本地行情、历史状态和生成报告 |
| `.venv/`、`__pycache__/` | 否 | 本地运行噪音 |

## 每日运行逻辑

1. 按配置的股票池分批获取已完成日 K；当前版本默认股票池为纳斯达克 100。
2. 更新工具 1–4 的 CSV/HTML 报告。
3. 状态链读取当日信号并追加到 SQLite。
4. 根据价格结构判断升级、降级、确认、失效或已走远。
5. 生成当日底部状态总览板；状态链可以跨多天持续累积。

跨日状态存在 SQLite 中，HTML 只是某一天的可视化入口。
