# 美股行情日期门禁（所有 Agent 必须遵守）

本项目的报告日期只表示美东最近一个已经完整收盘的美股交易日。

- `market_date`：唯一业务日期；文件名和目录名使用 `YYYYMMDD`。
- `scan_date`：实际扫描运行日期，仅作运行记录。
- `created_at` / `generated_at`：实际生成时间，不能用于报告命名。

例如：北京时间 2026-08-20 扫描到美东 2026-08-19 收盘数据，文件必须命名为 `20260819`。

所有 Agent 必须先读取行情的 `*_数据截至`、`market_data_asof` 或 `market_data_asof_by_interval`，得到 `market_date` 后再运行：

1. 检查同日正式 CSV、HTML、manifest 是否存在；存在就停止，禁止覆盖。
2. 日期门禁通过后才允许扫描。
3. 完成后核对 `report_date == market_date(YYYYMMDD)`。
4. 日期缺失、不一致、行情未完整收盘或缓存/API状态不明时停止，不猜日期。

禁止使用 `datetime.now()`、中国自然日或美东当前自然日直接命名报告，也禁止直接裸跑扫描命令。
