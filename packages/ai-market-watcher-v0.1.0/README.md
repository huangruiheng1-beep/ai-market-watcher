# AI 投研市场观察助手

一个面向纳斯达克 100 的本地市场观察工作流。它把多周期趋势、五榜单、背离/TD9、SKDJ 与跨日状态追踪串联起来，帮助用户确定“今天先看谁”以及“这只股票的状态正在怎样变化”。

> 本项目只整理观察顺序和状态过程，不预测涨跌，不构成任何交易指令或投资建议。

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

指定输入 5 只：

```bash
python run_live.py --tickers AAPL,MSFT,NVDA,META,AMZN
```

只想临时扫描 5 或 10 只纳指100成分股，也可以直接写：

```bash
python run_live.py --limit 5
python run_live.py --limit 10
```

`--tickers` 和 `--limit` 二选一。现场建议优先用 5 只；网络或 API 额度充足时再用 10 只。扫描数量变小不会改变规则，只是缩小本次输入范围。

完整现场链路是：

```text
输入股票代码/数量 → Twelve Data 真实行情 → 多周期与信号扫描 → HTML结果 → AI现场解释
```

真实行情会写入本地 `cache/` 和 `output/`，这两个目录不会被 Git 提交。底部状态追踪器的 SQLite 会在本地持续累积，不是每天重置。

## 数据与隐私

- API Key 只在本地读取，不写入代码、报告、日志或 SQLite。
- `cache/`、`output/`、`.env`、SQLite 和虚拟环境默认不公开。
- 公开示例只包含市场观察数据，不包含账户、持仓或凭据。

## 当前边界

- SKDJ 参数、失效线容差、走远阈值等仍属候选规则，尚未完成充分历史验证。
- 项目不连接券商，不自动下单，不承诺收益率或胜率。
- “7 天复盘闭环”不在本次路演版范围；它将在累积足够真实状态链后再开发。

## 项目资料

- [架构与文件说明](docs/ARCHITECTURE.md)
- [WB / Kimi 路演演示文件制作任务书](docs/WB_KIMI_ROADSHOW_BRIEF.md)
