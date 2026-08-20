# 每日研究日报统一入口 · 文件说明

本目录是「博士雷达 · 每日研究日报」的前端入口。页面沿用 `roadshow/index.html` 的 MOSAIC 设计系统；当前默认展示一次 `2026-08-19` 公开案例，包含背离 + TD9，后续个人日报默认只留在本地。

设计语言完全沿用 `../roadshow/index.html` 的 MOSAIC 设计系统（4 色：Ink / Bone / Cobalt / Vermillion），保持同一产品的视觉连续性。

## 文件清单

| 文件 | 作用 |
|------|------|
| `index.html` | 统一日报入口页面框架（顶部状态栏 + 工具导航 tab + 今日总览 + 跨工具对比表 + 5 个工具详情占位 + 历史日历抽屉 + 页脚） |
| `daily.css` | 全部样式。`:root` 色彩 / 字体 / 间距 token 与 roadshow 完全一致 |
| `daily.js` | 交互逻辑（tab 切换、对比表搜索/排序、日历抽屉）+ 数据加载占位函数 |
| `data/daily_data.js` | 日报数据层；当前仅保留一次公开案例，后续个人数据默认不提交 |
| `README.md` | 本文件 |

本地正式日报采用 `output/daily/YYYYMMDD/` 统一目录；T9 的原始运行证据仍保留在 `output/runs/`，不会因为整理日报而丢失。

## 本地打开

直接双击 `index.html` 即可，断网可用。所有资源均为相对路径，无任何外部 CDN / 在线字体 / 网络服务。

产品主页位于 `../roadshow/index.html`，可通过右上角「← 产品主页」返回。

## 页面结构

1. **顶部导航** — 品牌标记 + 6 个工具 tab（今日总览 / 多周期共振 / 五榜日报 / 背离+TD9 / SKDJ 观察池 / 底部状态链）+ 历史日报按钮 + 返回产品主页。
2. **状态栏** — 日期、报告状态、扫描股票数、数据状态、行情截至。当前全部标为占位。
3. **今日总览** — 7 张摘要卡片（mosaic 网格）+ 跨工具股票对比表。
4. **跨工具对比表** — 支持搜索（股票代码/名称）、列排序、状态标签。空值统一显示「无结果」，不显示「无信号」。
5. **5 个工具详情面板** — 每个含工具名称、功能说明、报告状态、数据来源占位、行情截至占位、「打开完整报告」与「返回今日总览」按钮。
6. **历史日历抽屉** — 月份切换、日期网格、有日报日期可点击、状态用颜色+文字双重标识。
7. **页脚** — 边界声明（不预测涨跌 / 不连接券商 / 不自动下单 / 候选规则待验证）。

## 生成本地真实日报入口

日报数据由工作区根目录的生成器读取已有报告后生成。它只读 CSV、manifest 和 SQLite，不扫描股票、不调用 API：

```bash
cd "/Users/huangruiheng/WorkBuddy/博士ppt"
./.venv/bin/python daily_dashboard.py --date 20260819
```

生成文件：

```text
release/ai-market-watcher/daily/data/daily_data.js
release/ai-market-watcher/daily/data/daily_data_20260819.js
```

然后双击打开：

```text
release/ai-market-watcher/daily/index.html
```

入口会读取当天正式的 ND100、五榜、SKDJ 和状态链结果。带 `smoke`、`test`、`demo`、`synthetic` 的文件不会进入正式日报；缺少正式报告时会显示“未生成”。

当前 2026-08-19 的验证结果：

- 扫描股票：100 只；
- 正式工具：4/5；
- 背离 + TD9：未生成，现有 `*_test` 文件未纳入；
- API 请求：0；
- 状态链：读取本地 SQLite 和状态链 HTML。

## 演示占位说明

- 页面顶部有红色横幅标注「本页为演示占位数据，非真实行情」。
- 对比表 8 行数据为静态演示行，故意包含「全命中 / 部分命中 / 全无结果」三种情况。
- 如果没有生成 `daily/data/daily_data.js`，日历日期与状态为演示占位，点击仅弹出提示，不打开真实文件。
- 生成本地数据后，「报告状态」「数据来源」「行情截至」会来自当天正式报告。

---

## 后续真实数据接入位置

### 数据加载函数（历史占位说明）

页面当前通过 `data/daily_data.js` 接收生成器输出。下面的函数名和路径说明保留作后续扩展参考；不要让浏览器直接扫描本地目录或直接读取 SQLite。

| 函数 | 未来数据源 | 说明 |
|------|-----------|------|
| `loadDailyIndex()` | `output/daily_dashboard_YYYYMMDD.html` 清单 | 哪些日期有日报 + 状态（full/part/hist/miss） |
| `loadReportManifest()` | `output/manifest.json` | 各工具当日是否完成 + 文件路径 |
| `loadComparisonRows()` | 见下方 CSV 合并 | 跨工具对比表数据 |
| `loadResonanceReport()` | `output/nd100_resonance_YYYYMMDD.csv` | 多周期共振（详情面板/摘要） |
| `loadRankingsReport()` | `output/five_rankings_YYYYMMDD_daily.csv` | 五榜日报 |
| `loadDivergenceReport()` | `output/divergence_td9_YYYYMMDD.csv` | 背离 + TD9 |
| `loadSkdjReport()` | `output/skdj_YYYYMMDD_daily.csv` | SKDJ 观察池 |
| `loadStatusChain()` | `output/status_chain.sqlite` | 底部状态链 |

> 注：本地双击 HTML 时，浏览器不适合直接扫描目录或读取 SQLite；正式数据应由外部生成器先整理为 `daily_data.js`，页面只负责展示。

### 真实文件路径（由工作区生成器读取）

```
工作区根目录/output/nd100_resonance_YYYYMMDD.csv
工作区根目录/output/five_rankings_YYYYMMDD_daily.csv
工作区根目录/output/divergence_td9_YYYYMMDD.csv
工作区根目录/output/skdj_YYYYMMDD_daily.csv
工作区根目录/output/status_chain.sqlite
```

入口页面中的 HTML 链接由生成器写入相对路径，当前本地工作区从 `daily/index.html` 到根目录 `output/` 使用 `../../../output/`。

### 历史日历点击跳转

`daily.js` 的日历点击处理中已注释说明未来逻辑：

```js
// 未来：window.location.href = '../output/daily_dashboard_' + ds.replace(/-/g,'') + '.html';
```

当前为占位 `alert` 提示，不连接真实文件。

### 正式接入时必须排除的文件名片段

读取 `output/` 目录时，跳过文件名包含以下片段的文件：

```
smoke
test
demo
synthetic
旧日期文件（按业务定义的保留窗口）
```

---

## 边界

- 本阶段**不修改**任何核心扫描器、报告生成逻辑、`roadshow/index.html` 或 API 配置。
- 数据生成器**只读取**真实 CSV、manifest、SQLite，**不调用**任何 API。
- `daily/data/*.js` 是本地生成数据，已被 `.gitignore` 排除，不应提交真实日报或本地状态。
