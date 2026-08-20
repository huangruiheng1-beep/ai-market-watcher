# 博士雷达 · AI 投研市场观察助手

“博士雷达”是本项目的对外名称。当前版本以纳斯达克 100 作为默认示例股票池，把多周期趋势、五榜单、背离/TD9、SKDJ 与跨日状态追踪串联起来；股票池可按后续版本扩展到标普 500 或其他市场范围。

> 本项目只整理观察顺序和状态过程，不预测涨跌，不构成任何交易指令或投资建议。

## 公开入口

- GitHub 仓库：<https://github.com/huangruiheng1-beep/ai-market-watcher>
- 路演网页：<https://huangruiheng1-beep.github.io/ai-market-watcher/roadshow/>
- 每日研究日报入口：<https://huangruiheng1-beep.github.io/ai-market-watcher/daily/>
- 公开案例快照：日报入口默认展示 2026-08-19 的一次真实案例；后续个人日报默认不上传。
- 第一版 ZIP：<https://github.com/huangruiheng1-beep/ai-market-watcher/releases>

## 它包含什么

1. **多周期共振扫描器**：检查 60 分钟、日线和周线的 EMA20/60 排列。
2. **五榜单日报**：把标的分到已确认、背离观察、强趋势等待触发、C 级潜力、风险噪音五类。
3. **背离 + TD9**：标记动能疲劳与反转观察区。
4. **SKDJ 观察池**：区分下跌超跌、上升回调、顶部超买等场景。
5. **底部状态追踪器**：用 SQLite 跨多天记录 `OBSERVE → BASE → TRIGGER → CONFIRM / INVALID / EXPIRED`。

## 先用 60 秒离线试用

离线演示不需要 API Key，也不请求市场数据。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python run_demo.py
```

运行完成后，打开：

```text
output/demo/status_chain_demo.html
output/demo/status_chain_real_sample.html
```

`demo_data/` 保留了一份行情截至 2026-08-18 的纳指 100 示例报告和对应的真实数据状态看板。其他三个信号工具也会同时生成 synthetic 演示报告，便于无 Key 验证。

## 路演：现场输入，现场抓取真实行情

路演不以截图或静态 HTML 作为主要演示。主持人现场输入股票代码，程序直接调用 Twelve Data，输出本次扫描结果；随后打开生成的 HTML 看板，让 AI 根据结果继续解释。

### 1. 安装

需要 Python 3.10 或更高版本。建议在项目目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows PowerShell：.venv\\Scripts\\Activate.ps1
python -m pip install -r requirements.txt
```

### 2. 获取 Twelve Data API Key

打开官方注册页：<https://twelvedata.com/pricing>。注册后进入账户控制台，在 API Key 页面复制自己的 Key。免费额度和频率限制以官方页面当前说明为准。

不要把 Key 发到聊天、截图、代码、报告或 GitHub。

### 3. 本地配置

复制配置模板，并只在自己的电脑上填写 Key：

```bash
cp .env.example .env
chmod 600 .env
```

`.env` 已经被 `.gitignore` 排除，不会进入 GitHub。也可以使用环境变量 `TWELVE_DATA_API_KEY`，或用 `MARKET_WATCHER_CREDENTIALS_FILE` 指向仓库外的受保护凭据文件。

### 4. 路演时运行

正式运行直接启动一批扫描，默认每批 50 只：

```bash
python run_live.py
```

路演现场为了控制等待时间，可以显式指定一小批股票作为演示输入：

```bash
python run_live.py --tickers AAPL,MSFT,NVDA,META,AMZN
```

如果只是临时缩小本次输入范围，也可以使用 `--limit`：

```bash
python run_live.py --limit 5
python run_live.py --limit 10
```

`--tickers` 和 `--limit` 二选一。5 只只是路演演示参数，不是产品的扫描上限。正式使用时，按股票池分批运行，默认每批 50 只；理论上一个免费账号每天可扫描约 800 只股票的实时数据，实际每天能处理多少取决于数据服务商当日额度、频率限制、请求周期和运行配置。

完整现场链路是：

```text
Agent 启动 → 每批 50 只 → Twelve Data 真实行情 → 4 个扫描器 → 状态链与报告 → AI 解释
```

真实行情会写入本地 `cache/` 和 `output/`，这两个目录不会被 Git 提交。底部状态追踪器的 SQLite 会在本地持续累积，可以连续运行多天，不是每天重置。

## 数据与隐私

- API Key 只在本地读取，不写入代码、报告、日志或 SQLite。
- `cache/`、`output/`、`.env`、SQLite 和虚拟环境默认不公开。
- 公开示例只包含市场观察数据，不包含账户、持仓或凭据。

## Agent 使用方式

本工具是本地终端工作流，不绑定某一个 Agent。配置好环境和数据源 Key 后，可以配合 Codex、Claude、WorkBuddy 等支持终端和本地文件操作的 Agent 使用；Agent 负责启动命令、读取报告和继续解释结果，API Key 仍只保留在用户本地。

## 下一阶段路线图

- **7 天复盘工具**：工作区已提供 `seven_day_review.py`，可基于连续状态链汇总一周内的信号、状态变化、失效与人工复核记录，形成复盘报告。当前公开 v0.1.0 压缩包仍是此前快照，未随本次工作区改动重打包。

本地个人使用时，Daily 入口位于 `本地/daily/index.html`，7 天复盘入口位于 `本地/review/index.html`。GitHub Pages 保持现有地址不变；公开页面只保留上述一次案例快照，不同步本地 SQLite、缓存或后续个人日报。

日报整理采用两层结构：`output/runs/` 保留 T9 的原始运行证据，`output/daily/YYYYMMDD/` 汇总当天面向阅读的正式报告。`publish_daily_reports.py` 只复制报告，不重新扫描或请求行情。

## 当前边界

- SKDJ 参数、失效线容差、走远阈值等仍属候选规则，尚未完成充分历史验证。
- 项目不连接券商，不自动下单，不承诺收益率或胜率。
- 7 天复盘的有效性结论仍需积累真实记录；样本不足时工具会明确标记，不把部分数据当成完整验证。

## 项目资料

- [架构与文件说明](docs/ARCHITECTURE.md)
- [WB / Kimi 路演演示文件制作任务书](docs/WB_KIMI_ROADSHOW_BRIEF.md)
