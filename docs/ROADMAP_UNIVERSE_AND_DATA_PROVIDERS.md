# AI Market Watcher Roadmap: Flexible Universes and Pluggable Data Providers

> This is a planning document only. The current release remains Nasdaq 100 with
> the default provider, the market-date gate, and human-triggered execution.

## How another user's AI Agent should use this file

This roadmap is intentionally packaged with the repository so that a new user
can ask their own AI Agent to plan a safe customization. The Agent should:

1. Read this roadmap, `PROJECT_CONTEXT.md`, `DATE_POLICY.md` and the local
   repository instructions before editing anything.
2. Treat the current Nasdaq 100/default-provider workflow as a protected
   baseline; do not replace it while designing a new scope or provider.
3. Ask the user to choose the target universe, provider type, credential
   location, data fields, refresh cadence and compatibility policy.
4. Turn those choices into a concrete phase plan and acceptance checklist.
5. Wait for explicit implementation approval before changing code, configuration
   or reports.
6. Implement in an isolated branch or copy, keep legacy ND100 behavior working,
   run the full tests and release checks, and only then publish changes.

The roadmap is guidance, not an instruction to perform a migration immediately.

### Copy-and-use Agent request

```text
请先读取项目中的：
1. docs/ROADMAP_UNIVERSE_AND_DATA_PROVIDERS.md
2. PROJECT_CONTEXT.md
3. DATE_POLICY.md
4. 当前项目的本地规则文件

我的改版目标：
- 股票池：<例如 Nasdaq 100 / S&P 500 / 全市场 / 自定义列表>
- 数据源：<默认数据源 / 自己的 API / CSV / 数据库 / 本地缓存>
- 凭据位置：<环境变量 / 项目外文件 / 不需要凭据>
- 更新频率：<说明>
- 是否保留旧版 ND100：<是/否>
- 是否需要迁移历史报告：<是/否>

先只输出改版方案、影响文件、兼容风险、验收标准和回滚方案。
未经我明确批准，不要修改代码、配置、历史报告或发布文件。
```

## Current release: do not change yet

| Area | Decision | Reason |
|---|---|---|
| Date policy | Keep the current `market_date` rules | They protect reports from China/US time-zone conflicts |
| Default universe | Keep Nasdaq 100 | Existing caches, reports, status-chain data and tests use it as the stable baseline |
| Default provider | Keep the current default provider | It remains the out-of-the-box path |
| Historical reports | Do not rescan or rename them | Preserve reproducibility and execution evidence |
| Background scheduling | Keep disabled | A human explicitly starts each workflow |

## Target architecture

The same workflow should eventually support:

1. Nasdaq 100 as the default.
2. S&P 500, full-market, sector, custom-symbol and imported universes.
3. The project default market-data provider.
4. A user's own API, CSV, database, cache or provider plugin.
5. The same date gate, cache checks, report naming and validation for every source.
6. Backward compatibility with existing Nasdaq 100 commands and reports.

## Phased plan

| Phase | Goal | Main design / files | Acceptance criteria | Status |
|---|---|---|---|---|
| 0 | Freeze the stable baseline | Capture the current Nasdaq 100 config, fixtures, tests and release package | Existing workflow remains green and historical reports are untouched | Planned |
| 1 | Abstract the universe | Add a `universe` configuration layer for `nasdaq100`, `sp500`, `full_market`, `sector` and `custom` | Changing configuration changes the universe; manifests record `universe_id`, count and source | Planned |
| 2 | Abstract market data | Add a `MarketDataProvider` interface and normalized OHLCV/as-of model | Default provider, user CSV, user API and local cache can feed the same scanners | Planned |
| 3 | Configure providers safely | Add public templates and local-only credential/path settings | Switching providers requires configuration, not scanner-core edits; secrets never enter Git or reports | Planned |
| 4 | Centralize dates and calendars | Add one service for exchange timezone, trading calendar, `market_date`, `scan_date` and `created_at` | Cross-time-zone and incomplete-session fixtures pass for every provider | Planned |
| 5 | Generalize reports and state | Extend names and manifests from ND100-only to `<universe_id>` while preserving legacy names | Different universes cannot collide; old ND100 reports remain readable | Planned |
| 6 | Generalize the agent entrypoint | Make the one-command workflow read universe and provider configuration | Users can change scope/provider without changing the agent prompt | Planned |
| 7 | Improve the public package | Update `PROJECT_CONTEXT.md`, `DATE_POLICY.md`, README, templates and offline demo | A new user can run the default path and has a documented custom-provider path | Planned |
| 8 | Compatibility and release migration | Add config versions, legacy readers, migration/rollback tools and release checks | Old reports are not overwritten; release packages contain no private state | Planned |
| 9 | Full acceptance | Add unit, cross-time-zone, provider-fixture, retry and small live-scope tests | Local agent, public CLI and release ZIP behave consistently | Planned |

## Proposed configuration shape

Universe and provider selection should live in configuration rather than in
scanner code:

```json
{
  "universe": {
    "id": "nasdaq100",
    "provider": "builtin",
    "symbols_file": "config/universes/nasdaq100.csv"
  },
  "market_data": {
    "provider": "default",
    "credentials": "environment_or_external_file",
    "timezone": "America/New_York"
  }
}
```

Planned provider types:

| Provider | Purpose |
|---|---|
| `default` | The project's default provider and easiest first-run path |
| `custom_api` | A user's own API or internal market-data service |
| `csv` | A user's normalized OHLCV CSV files |
| `cache` | An existing local cache or database |
| `plugin` | A future third-party or Python adapter |

## Compatibility rules that must remain

| Rule | Future requirement |
|---|---|
| Date semantics | `market_date` is the business date; never name reports with a local clock date |
| Legacy files | Existing `nd100_resonance_YYYYMMDD.*` files remain readable |
| Same-day protection | Do not overwrite the same `universe_id + market_date + report_type` |
| Missing data | Record data insufficiency; do not convert it into “no signal” |
| Credentials | Never write keys to code, logs, manifests, reports or release packages |
| Provenance | Reports identify provider, cache/API state and per-interval as-of dates |
| Trigger model | A human explicitly starts the workflow; no background scheduler is required |

## Start conditions for implementation

Before changing production code, choose:

- the first additional universe (custom or S&P 500 is safer than starting with full market);
- the first additional provider shape (normalized CSV or custom API is recommended);
- the authoritative universe source and refresh cadence;
- field mappings and minimum historical bars for each provider;
- at least one credential-free offline fixture;
- the coexistence and migration policy for old ND100 reports;
- an isolated development branch and a release-package acceptance run.

The implementation order should be:

```text
Universe / Provider / Date-Service design
→ code and configuration
→ public context and date policy
→ local agent instructions
→ tests, release package and GitHub publication
```
