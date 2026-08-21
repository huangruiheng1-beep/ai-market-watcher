# AI Market Watcher：通用项目背景

这是一份放在 GitHub 仓库中的通用项目背景，供其他用户、Claude、Coze、OpenCode、WorkBuddy、Codex 或其他终端 Agent 复制使用。

本文件只描述公开仓库的通用规则，不包含任何特定用户的本地绝对路径、API Key、缓存、私有报告或本地运行状态。

如果用户希望扩展股票池或更换数据源，Agent 必须先读取
`docs/ROADMAP_UNIVERSE_AND_DATA_PROVIDERS.md`，先输出定制化改版方案和验收标准；
未经用户明确批准，不得直接修改当前稳定版。

## 项目是什么

AI Market Watcher 用于美股市场观察、技术指标整理和历史状态追踪。

它只输出研究观察结果，不连接券商、不下单、不发送交易指令，也不构成投资建议。

主要流程：

```text
ND100 多周期扫描 → 五榜单 → T9/背离 → SKDJ → 状态链 → 统一日报
```

## 使用前准备

1. 把本仓库克隆到当前用户自己的目录。
2. 在当前仓库根目录配置自己的 Python 环境和数据源。
3. 运行前先读取仓库内的 `DATE_POLICY.md`。
4. 如果用户或本地工作区提供额外的运行说明，应先读取当前工作区的运行说明；不要假设其他用户拥有某台机器的绝对路径。

本 GitHub 仓库不是任何用户的私有本地运行目录。其他用户必须使用自己的缓存、输出目录、凭据配置和运行环境。

## 日期规则

所有报告文件名、目录名和日报日期中的 `YYYYMMDD`，统一表示：

> 美东最近一个已经完整收盘的美股交易日。

字段定义：

- `market_date`：唯一业务日期，用于文件名、目录名、日报和状态链。
- `scan_date`：实际扫描运行日期，仅用于记录。
- `created_at` / `generated_at`：实际生成时间，不能用于命名行情报告。
- `run_id`：实际运行实例编号，不代表行情日期。

例如：用户在北京时间 2026-08-20 扫描到美东 2026-08-19 收盘数据，报告应命名为 `20260819`。如果数据只截至 2026-08-18，就只能命名为 `20260818`。

## 启动前日期门禁

禁止直接裸跑类似命令：

```bash
nd100_resonance_scanner.py --limit 50
```

启动扫描前必须：

1. 从实际行情字段读取最新完整交易日：`*_数据截至`、`market_data_asof` 或 `market_data_asof_by_interval`。
2. 确定唯一 `market_date`。
3. 检查同一 `market_date` 是否已有正式 CSV、HTML、manifest 或 `output/daily/<market_date>/`。
4. 如果同日正式报告已经存在，立即停止，禁止覆盖和重复扫描。
5. 只有门禁通过后，才允许启动 API 或缓存扫描。

日期缺失、日期不一致、行情尚未完整收盘、缓存状态不明或 API 状态异常时，必须停止并报告证据，不能猜日期。

## 数据与安全规则

- 默认优先使用有效缓存。
- 不要自动使用 `--no-cache`。
- 已有 ND100 CSV 时，默认读取 CSV 后继续五榜单、T9、SKDJ，不重新扫描 ND100。
- 已有 ND100 CSV 时，先核对实际唯一 ticker 集合是否完整覆盖 ND100 universe；50/102 等 partial universe 必须先补齐剩余批次，禁止进入正式日报或被标记为 complete。
- 只有用户明确要求重新扫描 ND100 时，才允许启动 ND100 扫描器。
- 不读取、打印、复制、汇报或提交 API Key。
- 数据不足必须标记为数据不足并跳过，不能解释成“没有信号”。
- 不覆盖同一 `market_date` 的历史正式报告。

## 文件命名规则

正式报告必须使用 `market_date` 命名：

```text
output/nd100_resonance_<market_date>.csv
output/five_rankings_<market_date>_daily.csv
output/skdj_<market_date>_daily.csv
output/daily/<market_date>/
```

`output/runs/<run_id>/` 只代表实际运行实例；其中的报告文件仍必须使用 `market_date` 命名。

历史日期纠正必须先核对原始行情证据，再使用 `scripts/migrate_report_dates.py`。该工具只改报告引用、业务日期字段和标题，保留 `created_at`、`scan_date`、`run_id` 等执行事实；默认先审计，追加 `--apply` 才写入，并要求指定备份目录。

## 完整流程与验收

```text
确定 market_date
→ 检查同日是否已有正式报告
→ ND100 输入/扫描
→ 五榜单
→ T9/背离
→ SKDJ
→ 状态链
→ 发布到 output/daily/<market_date>/
→ 生成统一日报
→ 回读 manifest 验证日期
```

公开仓库的完整一键入口是 `run_live.py`：它负责行情获取、真实日期确认和后续日报；`run_daily.py` 是已经有 ND100 CSV 时的下游边界。Agent 不应各自复制一套聊天提示词或直接裸跑底层扫描器。

完成后必须核对：

```text
manifest.report_date == market_date 的 YYYYMMDD 格式
manifest.market_date == 真实最新完整行情日
报告文件名日期 == market_date
统一日报日期 == market_date

输入完整性还必须满足：

实际唯一 ticker 数 == ND100 universe 声明数量
partial universe == 阻断下游，不生成正式日报
```

任何一项不一致，都必须停止后续流程并报告错误，不得继续生成日报。

## 安全边界

- 不把任何用户的本地绝对路径写进公开报告或公开文档。
- 不把 API Key、缓存、SQLite、账户信息、持仓信息或私有日报提交到公开仓库。
- 不删除、不覆盖、不清空历史报告。
- 不连接券商，不执行交易，不发送交易指令。
