# TradingAgents

Local-first multi-agent investment research platform with:

- Multi-agent ticker analysis (market, fundamentals, news, sentiment, bull/bear, risk, trader)
- Watchlist screening (16 signals, presets, risk-aware overlays)
- Movers Intelligence (short + medium horizon)
- Long-Horizon institutional workflow (deterministic scan -> underwriting -> allocation)
- Backtesting, alerts, and operational tooling via `Makefile`

This repository is research-only. It does not place trades.

### About this repository

This is a **public fork and extension** of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents). It adds local screening, a web UI, reporting/backtests, and operator workflows on top of the upstream multi-agent research stack. To sync with the original project, use the `upstream` remote (`git fetch upstream`). Code is distributed under the [Apache License 2.0](LICENSE) where applicable.

---

## Minimal Docs Set (Canonical)

- `README.md` (this file): quickstart + system map
- `USER_MANUAL.md`: day-to-day operations, Make commands, troubleshooting
- `docs/ARCHITECTURE.md`: backend/frontend/data architecture
- `docs/CHANGELOG.md`: release-level history

---

## Quickstart

### 1) Install

```bash
git clone https://github.com/jon-vargas/TradingAgents.git
cd TradingAgents
pip install -r requirements.txt
pip install -e .
```

### 2) Configure environment

```bash
cp .env.example .env
```

Required (default config):

- `OPENAI_API_KEY`
- `FINNHUB_API_KEY`

Recommended:

- `ALPHA_VANTAGE_API_KEY`
- `PERPLEXITY_API_KEY` (Agent API; optional `PERPLEXITY_API_MODE=agent`)

### 3) Verify setup

```bash
make verify
```

### 4) Start UI

```bash
make ui
```

Open `http://localhost:8000`.

---

## Primary Workflows

### Single analysis

```bash
make analyze TICKER=NVDA DATE=2026-03-25 MODE=standard RISK=growth PROFILE=large_cap_core
```

### Screening

```bash
make screen WATCHLIST="AAPL MSFT NVDA" PRESET=momentum_hunter
make screen-watchlist NAME="NASDAQ 100"
make screen-all
make watchlist-metrics
make watchlist-migration-precheck
make watchlist-parity-diff LEFT="S&P 500" RIGHT="S&P 500"
```

`make screen-all` supports two modes via `screening.scan_all.mode`:

- `per_watchlist` (default): sequential per-list scans
- `union_buckets`: one union scan pass projected back to each bucket (faster for large index coverage)

### Movers Intelligence

```bash
make movers-scan TOP=25 INCLUDE_LOSERS=true
make movers-queue-top N=5 HORIZON=short
make movers-alerts HORIZON=medium TOP=5
```

### Long-Horizon Institutional

```bash
make long-horizon-prewarm PREWARM_MAX=600 PREWARM_SKIP_INFO=false
make long-horizon-scan LH_HORIZON=long_6to12m TOP=30 LH_USE_EXPANDED=true
make long-horizon-scan LH_HORIZON=long_6to12m TICKERS="AAPL MSFT NVDA" TOP=30 LH_USE_EXPANDED=false
make long-horizon-status
make long-horizon-runs
make long-horizon-underwrite RUN_ID=123 TOP=10
make long-horizon-allocate RUN_ID=123
make portfolio-risk RUN_ID=123
```

Recommended expanded-universe sequence:

1. Prewarm metadata/info caches (`long-horizon-prewarm`)
2. Run expanded scan (`LH_USE_EXPANDED=true`)
3. Underwrite top names
4. Allocate with constrained policy

### Institutional analysis (CLI, no server needed)

```bash
make institutional-data TICKER=NVDA
make upcoming-events TICKER=AAPL DAYS=90
```

`institutional-data` prints DCF fair value, implied growth vs. consensus, the 3×3 sensitivity grid, scenario targets with blended fair value, and peer comparables (P/E, P/S, EV/EBITDA) for the given ticker.

`upcoming-events` lists the next events from the `event_calendar` (earnings, ex-dividend, splits, catalysts) within the given day horizon.

### Backtesting and Calibration

```bash
make backtest
make backtest-report
make calibration-report
make calibration-by-decision
make calibration-by-factor
make reverse-dcf-accuracy
make calibration-all
make stabilization-gate
```

---

## Key Capabilities

### Screener

- 16 signal model across OHLCV, fundamentals, and enhanced layers
- Preset-driven scoring with regime adjustments; `opportunity_score` blends composite fundamental (6 EQ-overlap signals removed), entry quality, and macro fit with graduated penalties and regime-weighted macro
- Sort/filter-heavy UI and run comparison support; factor scorecard (6 families: Value, Quality, Momentum, Revisions, Risk, Macro-Fit) displayed in each ticker's expand panel
- Registry-backed index watchlists (S&P 500 / 400 / 600 / Russell 2000) populated from `index_constituents` with deterministic 4-field sort (liquidity rank, dollar volume, market cap, ticker)
- Union-buckets `scan-all` mode for single-pass scans across the full index universe
- Universe diagnostics endpoints (overlap, hydration baselines, p95 scan-all latency)
- Upcoming Events panel per ticker (earnings, ex-dividend, splits, catalysts from `event_calendar`)
- Peer Comparables panel (P/E, P/S, EV/EBITDA vs. sector peers, with medians and percentile ranks)

### Movers

- Dual-horizon runs (`short`, `medium`)
- Manual-first operation by default
- Queue/alerts actions from latest movers runs

### Long-Horizon

- Deterministic-first ranking (`long_6to12m`, `long_12to36m`)
- Universe policy filters and reproducibility metadata hashes
- Universe policy explicitly enforces `exclude_leveraged` and logs exclusion reasons
- Two-pass execution profile for large expanded-universe scans (fast shortlist -> deep pass)
- Cache prewarm command for faster expanded runs (`make long-horizon-prewarm`)
- Bounded top-N underwriting with shared timeout/token budgets and bounded concurrency
- Deterministic constrained allocation with:
  - `market_cap_band_min` and `market_cap_band_max`
  - configurable relaxation order (`market_cap_band_min`, `beta_min`, `sector_soft_cap`)
  - hard-constraint semantics for caps/floors that must remain strict
- Regime snapshot now includes macro + sector momentum features in run metadata
- Batch metadata lookup in allocator removes per-ticker DB lookup overhead
- **Portfolio Risk Summary**: weighted beta, sector HHI, sector concentration bars, and portfolio-level factor exposures auto-computed post-allocation (`make portfolio-risk RUN_ID=`)
- Manual-only default with automation kill switches off

### Institutional Analysis

- **DCF with sensitivity grid**: 3×3 WACC × terminal-growth fair-value table; market-implied growth rate vs. analyst consensus delta
- **Scenario analysis**: bull/base/bear targets with probability-weighted blended fair value
- **Peer comps**: P/E, P/S, EV/EBITDA vs. up to 10 sector peers with medians and percentile rank
- **Event calendar** (`event_calendar`): unified earnings, ex-dividend, split, and catalyst events fed from both analysis and screening paths; `make upcoming-events` for CLI access
- **Calibration learning loop**: confidence-accuracy curves sliced by factor family and by BUY/HOLD/SELL decision class; signal performance aggregated by factor family; reverse-DCF accuracy vs. analyst consensus

### Operations

- SQLite (`research.db`) with additive strategy side tables
- Backup/cleanup utilities (`make db-backup`, `make clean-old`, `make storage-report`)
- API and scheduler status endpoints for runtime visibility
- Timezone-aware UTC timestamps project-wide (`datetime.now(timezone.utc)` everywhere)
- Process-wide yfinance rate-limit breaker with exponential backoff and health visibility (`make limiter-status`)
- Runtime metrics table (`runtime_metrics`) with p95 aggregations for scan-all duration

---

## Core API Surface (selected)

**Analysis:**
- `POST /api/analyze`
- `GET  /api/ticker/{symbol}/intrinsic-value`
- `GET  /api/ticker/{symbol}/scenario-analysis`
- `GET  /api/ticker/{symbol}/peer-comps`

**Screening:**
- `POST /api/screen`
- `POST /api/screen-all`
- `GET  /api/screen-all/latest`
- `GET  /api/screen-all/latest/export/tradingview`
- `GET  /api/screen/runs/{id}/export/tradingview`

**Screening insights:**
- `GET  /api/screening/upcoming-events?ticker=&days_ahead=90`
- `GET  /api/screening/signal-performance-by-factor`
- `GET  /api/screening/calibration-by-factor`
- `GET  /api/screening/calibration-by-decision`
- `GET  /api/screening/reverse-dcf-accuracy`

**Movers:**
- `POST /api/movers/scan`
- `GET  /api/movers/status`
- `GET  /api/movers/runs`

**Long-horizon:**
- `POST /api/long-horizon/scan`
- `GET  /api/long-horizon/status`
- `GET  /api/long-horizon/runs`
- `POST /api/long-horizon/runs/{id}/underwrite-top`
- `POST /api/long-horizon/runs/{id}/allocate`

**Watchlists and universe:**
- `GET  /api/watchlists/universe/metrics`
- `GET  /api/watchlists/migration/precheck`
- `GET  /api/watchlists/parity-diff?left=...&right=...`
- `POST /api/watchlists/builtins/refresh/propose`
- `POST /api/watchlists/builtins/refresh/proposals/{id}/apply`

**Health:**
- `GET  /health/ready` (includes `yfinance_limiter` state + total trips)

For full operational usage, see `USER_MANUAL.md`.

